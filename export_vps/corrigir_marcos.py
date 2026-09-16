"""
corrigir_marcos.py — utilitário DEDICADO ao tenant Marcos Tavares.

Contexto: numa rodada ruim, a consulta de TÍTULO do Marcos voltou REGULAR sem
zona/seção e o fallback congelou os registros como 'apto' (sem zona). Como
'apto' é estado "resolvido", o --producao nunca mais os reprocessa. Este script
descongela (reset) esses travados e/ou reprocessa SÓ o Marcos, sem tocar nos
outros tenants.

Ações (nenhuma escreve sem --confirmar, exceto --reprocessar que sempre grava):

  # 1) Ver o estado atual (read-only, sem captcha):
  python corrigir_marcos.py

  # 2) Reset dos travados → nao_verificado (DRY-RUN mostra quantos; --confirmar grava):
  python corrigir_marcos.py --reset
  python corrigir_marcos.py --reset --confirmar
  python corrigir_marcos.py --reset --limite 500 --confirmar   # lote menor primeiro

  # 3) Reprocessar N pendentes do Marcos AO VIVO (grava; precisa AHK + captcha):
  python corrigir_marcos.py --reprocessar 50

Use o venv que funciona:
  "d:\\CLAUDE\\AUTOMACAO TSE\\.venv\\Scripts\\python.exe" corrigir_marcos.py ...
"""
import argparse
import sys
import time

sys.path.insert(0, ".")

from config import load_config
from supabase import create_client, ClientOptions
import poc_clicker_main as maestro

MARCOS = "0b479271-aac5-46ba-93f0-24ea55ebf66c"
TABELAS = ("eleitores", "liderancas")
RESOLVIDO_INAPTO = ["inapto_cancelado", "inapto_suspenso", "inapto_transferido"]
TRAVADO_ELEG = ["apto"] + RESOLVIDO_INAPTO
PENDENTE_ELEG = ["nao_verificado", "pendente"]
_has = lambda v: v not in (None, "", " ")


def _sb():
    cfg = load_config()
    return create_client(cfg.SUPABASE_URL, cfg.SUPABASE_SERVICE_KEY,
                         options=ClientOptions(postgrest_client_timeout=120))


def _ids_travados(sb, tabela: str, limite: int | None) -> list:
    """IDs do Marcos: resolvido (apto/inapto) + mae + data, mas SEM zona."""
    ids, off = [], 0
    while limite is None or len(ids) < limite:
        q = (sb.table(tabela)
               .select("id")
               .eq("tenant_id", MARCOS)
               .in_("elegibilidade", TRAVADO_ELEG)
               .not_.is_("nome_mae", "null").neq("nome_mae", "")
               .not_.is_("data_nascimento", "null")
               .is_("zona_eleitoral", "null")
               .range(off, off + 499))
        rows = q.execute().data or []
        if not rows:
            break
        ids.extend(r["id"] for r in rows)
        if len(rows) < 500:
            break
        off += 500
    return ids[:limite] if limite is not None else ids


def _status(sb) -> None:
    print("═" * 60)
    print("  MARCOS TAVARES — estado atual")
    print("═" * 60)
    for tabela in TABELAS:
        def cnt(**filtros):
            q = sb.table(tabela).select("id", count="exact").eq("tenant_id", MARCOS)
            if "eleg_in" in filtros:
                q = q.in_("elegibilidade", filtros["eleg_in"])
            if filtros.get("com_dados"):
                q = (q.not_.is_("nome_mae", "null").neq("nome_mae", "")
                       .not_.is_("data_nascimento", "null"))
            if filtros.get("sem_zona"):
                q = q.is_("zona_eleitoral", "null")
            if filtros.get("com_zona"):
                q = q.not_.is_("zona_eleitoral", "null")
            return q.limit(1).execute().count or 0

        total = cnt()
        pend = cnt(eleg_in=PENDENTE_ELEG)
        com_zona = cnt(com_zona=True)
        travados = len(_ids_travados(sb, tabela, None))
        print(f"  [{tabela}] total={total} | pendentes={pend} | "
              f"com_zona={com_zona} | travados(sem zona)={travados}")
    print("═" * 60)


def _reset(sb, limite: int | None, confirmar: bool) -> None:
    total = 0
    plano = {}
    for tabela in TABELAS:
        ids = _ids_travados(sb, tabela, limite)
        plano[tabela] = ids
        total += len(ids)
        if limite is not None:
            limite -= len(ids)
            if limite <= 0:
                limite = 0
    print(f"  Travados a resetar (apto/inapto → nao_verificado): {total}")
    for t, ids in plano.items():
        print(f"     {t}: {len(ids)}")
    if not confirmar:
        print("  (DRY-RUN — nada gravado. Rode com --confirmar para aplicar.)")
        return
    if total == 0:
        print("  Nada a resetar.")
        return
    feitos = 0
    for tabela, ids in plano.items():
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            (sb.table(tabela)
               .update({"elegibilidade": "nao_verificado"})
               .in_("id", chunk).execute())
            feitos += len(chunk)
            print(f"     {tabela}: {feitos}/{total} resetados...")
    print(f"  ✅ Reset concluído: {feitos} registros → nao_verificado.")
    print("     Rode o --producao (ou --reprocessar aqui) p/ coletar zona/seção.")


def _carregar_pendentes_marcos(sb, limite: int) -> list:
    """Pendentes do Marcos (nao_verificado/pendente) com CPF válido, priorizando enriquecidos."""
    import re
    valido = lambda c: len(re.sub(r"\D", "", c or "")) == 11
    alvo = []
    for tabela in TABELAS:
        if len(alvo) >= limite:
            break
        resp = (sb.table(tabela)
                  .select("id, tenant_id, cpf, nome_mae, data_nascimento, "
                          "data_nascimento_br, enriquecimento_status, elegibilidade, nome")
                  .eq("tenant_id", MARCOS)
                  .in_("elegibilidade", PENDENTE_ELEG)
                  .not_.is_("cpf", "null").neq("cpf", "")
                  .order("enriquecimento_status", desc=False)
                  .limit(limite * 3).execute())
        for r in (resp.data or []):
            if valido(r.get("cpf")):
                r["_tabela"] = tabela
                alvo.append(r)
                if len(alvo) >= limite:
                    break
    return alvo[:limite]


def _reprocessar(sb, n: int, enriquecer: bool) -> None:
    regs = _carregar_pendentes_marcos(sb, n)
    if not regs:
        print("  Nada pendente no Marcos. (Rode --reset antes, se quiser descongelar.)")
        return
    print("═" * 60)
    print(f"  REPROCESSANDO {len(regs)} pendente(s) do Marcos AO VIVO (GRAVA)")
    print("  Precisa do tse_clicker.ahk rodando + Chrome + captcha.")
    print("═" * 60)
    stats = {"apto": 0, "regularizar_tse": 0, "pendente": 0, "outros": 0, "com_zona": 0}
    for i, reg in enumerate(regs, start=1):
        try:
            dados = maestro._processar(reg, gravar=True, sb=sb, idx=i, total=len(regs),
                                       enriquecer=enriquecer)
            if dados is None:
                stats["pendente"] += 1
            else:
                eleg = dados.get("elegibilidade")
                stats[eleg if eleg in stats else "outros"] += 1
                if _has(dados.get("zona_eleitoral")):
                    stats["com_zona"] += 1
        except TimeoutError as e:
            print(maestro.VERMELHO(f"  ⏭️  {e} — mantém PENDENTE.")); stats["pendente"] += 1
        except RuntimeError as e:
            print(maestro.VERMELHO(f"  ❌ {e} — pulando.")); stats["pendente"] += 1
        if i < len(regs):
            time.sleep(2.5)
    print("\n" + "═" * 60)
    print(f"  FIM: apto={stats['apto']} (com zona={stats['com_zona']}) | "
          f"regularizar_tse={stats['regularizar_tse']} | pendente={stats['pendente']} | "
          f"outros={stats['outros']}")
    print("═" * 60)


def _carregar_apto_sem_zona(sb, limite: int | None) -> list:
    """Registros do Marcos 'só aptidão' (apto/inapto, SEM zona) — alvo do re-enriquecimento."""
    import re
    valido = lambda c: len(re.sub(r"\D", "", c or "")) == 11
    alvo, off_tab = [], {}
    for tabela in TABELAS:
        off = 0
        while limite is None or len(alvo) < limite:
            resp = (sb.table(tabela)
                      .select("id, cpf, nome_mae, data_nascimento, enriquecimento_status")
                      .eq("tenant_id", MARCOS)
                      .in_("elegibilidade", TRAVADO_ELEG)
                      .is_("zona_eleitoral", "null")
                      .range(off, off + 499).execute())
            rows = resp.data or []
            if not rows:
                break
            for r in rows:
                if valido(r.get("cpf")):
                    r["_tabela"] = tabela
                    alvo.append(r)
            if len(rows) < 500:
                break
            off += 500
    return alvo[:limite] if limite is not None else alvo


def _enriquecer_lote_marcos(sb, workers: int, limite: int | None) -> None:
    """
    Enriquece (API B → DataSintese) os registros 'só aptidão' do Marcos, em
    paralelo e SEM captcha. Não-destrutivo: só preenche mãe/data que faltam
    (quem já tem não é sobrescrito e não gasta a API paga). Não muda a
    elegibilidade — depois use --reset + --loop p/ coletar a zona.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    regs = _carregar_apto_sem_zona(sb, limite)
    if not regs:
        print("  Nada 'só aptidão sem zona' encontrado.")
        return
    print("═" * 60)
    print(f"  RE-ENRIQUECENDO {len(regs)} registro(s) 'só aptidão' do Marcos")
    print(f"  (API B → DataSintese, {workers} workers, SEM captcha, não-destrutivo)")
    print("═" * 60)
    stats = {"enriquecido": 0, "sem_dados": 0, "erro": 0, "erro_update": 0}
    ganhou_mae = 0
    feitos = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(maestro._enriquecer_um, sb, r): r for r in regs}
        for f in as_completed(futs):
            reg = futs[f]
            tinha_mae = _has(reg.get("nome_mae"))
            try:
                status, _origem = f.result()
            except Exception:
                status = "erro"
            stats[status] = stats.get(status, 0) + 1
            if status == "enriquecido" and not tinha_mae:
                ganhou_mae += 1
            feitos += 1
            if feitos % 200 == 0:
                print(f"     ... {feitos}/{len(regs)}")
    print(f"  ✅ enriquecimento: {stats}")
    print(f"     dos que NÃO tinham mãe, ganharam mãe agora: {ganhou_mae}")
    print("     ➜ agora rode:  --reset --confirmar   e depois  --loop")


def _contar_pendentes_marcos(sb) -> int:
    """Quantos registros do Marcos estão pendentes (nao_verificado/pendente) com CPF."""
    total = 0
    for tabela in TABELAS:
        q = (sb.table(tabela).select("id", count="exact").eq("tenant_id", MARCOS)
               .in_("elegibilidade", PENDENTE_ELEG)
               .not_.is_("cpf", "null").neq("cpf", ""))
        total += q.limit(1).execute().count or 0
    return total


def _loop(sb, lote: int, enriquecer: bool, max_rodadas: int) -> None:
    """
    Reprocessa o Marcos em rodadas de `lote` até ZERAR os pendentes dele.
    Para sozinho se uma rodada não reduzir os pendentes (captcha não resolvido /
    stale / sessão ruim) — evita ficar em loop infinito gastando captcha.
    """
    anterior = None
    for rodada in range(1, max_rodadas + 1):
        restantes = _contar_pendentes_marcos(sb)
        print("\n" + "█" * 60)
        print(f"  RODADA {rodada} — Marcos tem {restantes} pendente(s)")
        print("█" * 60)
        if restantes == 0:
            print("  🎉 ZERADO! Nenhum pendente no Marcos.")
            return
        if anterior is not None and restantes >= anterior:
            print(f"  ⚠️  Sem progresso desde a rodada anterior ({restantes}). "
                  "Pendentes travados (captcha não resolvido / stale). Parando.")
            return
        anterior = restantes
        _reprocessar(sb, min(lote, restantes), enriquecer)
    print(f"  ⚠️  Atingiu o máximo de {max_rodadas} rodadas. Parando.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Corrige/reprocessa SÓ o tenant Marcos Tavares")
    ap.add_argument("--reset", action="store_true", help="reseta travados (apto/inapto sem zona) → nao_verificado")
    ap.add_argument("--limite", type=int, default=None, help="limita o reset a N registros (lote menor)")
    ap.add_argument("--confirmar", action="store_true", help="aplica o reset (sem isto, é dry-run)")
    ap.add_argument("--reprocessar", type=int, metavar="N", help="reprocessa N pendentes do Marcos ao vivo (grava)")
    ap.add_argument("--loop", action="store_true", help="reprocessa em rodadas até ZERAR os pendentes do Marcos (grava)")
    ap.add_argument("--lote", type=int, default=200, help="tamanho do lote por rodada no --loop (default 200)")
    ap.add_argument("--max-rodadas", type=int, default=1000, help="máx. de rodadas no --loop")
    ap.add_argument("--enriquecer", action="store_true", help="enriquece antes de reprocessar (se faltar mãe/data)")
    ap.add_argument("--enriquecer-lote", action="store_true",
                    help="re-enriquece em paralelo os 'só aptidão sem zona' do Marcos (sem captcha)")
    ap.add_argument("--workers", type=int, default=8, help="workers do --enriquecer-lote (default 8)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    sb = _sb()
    if getattr(args, "enriquecer_lote", False):
        _enriquecer_lote_marcos(sb, args.workers, args.limite)
    elif args.reset:
        _reset(sb, args.limite, args.confirmar)
    elif args.loop:
        _loop(sb, args.lote, args.enriquecer, args.max_rodadas)
    elif args.reprocessar is not None:
        _reprocessar(sb, args.reprocessar, args.enriquecer)
    else:
        _status(sb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
