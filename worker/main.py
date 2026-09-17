"""
main.py — worker do portal: processa a fila batches/voter_records em 2 fases.

    Fase A (enriquecimento): pending → ready_tse  (APIs pagas, paralelo)
    Fase B (TSE):            ready_tse → done     (TSE autoatendimento, sequencial)

USO:
    python main.py --stub --limite 5        # dry-run: TSE falsificado, 5 registros
    python main.py --limite 20              # 1 rodada (5 padrão? não: lote A 500/B 200)
    python main.py --producao               # loop até zerar / sem progresso
    python main.py --producao --fase b      # só a fase B

Parada elegante: Ctrl+C ou criando o arquivo worker/STOP (checado entre
registros). Logs em worker/logs/worker_YYYYMMDD.log. Webhook (se
NOTIFY_WEBHOOK_URL estiver no .env) avisa de lotes concluídos e erros.

⚠️  Sem --stub, usa TseHttpClient: consultas de SITUAÇÃO (CPF-only) e de
    TÍTULO/ONDE-VOTAR (zona/seção/município, quando o registro tem mãe+data)
    implementadas contra o cad-api.tse.jus.br. Se o TSE rejeitar mãe+data
    (401), o worker faz fallback sozinho pra situação CPF-only.
"""

import argparse
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Console do Windows costuma ser cp1252 e quebra com acentos/emoji. Força UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WORKER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKER_DIR))

from config_worker import garantir_export_vps_no_path, load_config  # noqa: E402

garantir_export_vps_no_path()  # path p/ tse_extracao_texto / utils

from tse_extracao_texto import parse_resultado_texto  # noqa: E402,F401  (fallback texto; o HTTP real devolve dict já parseado)
from utils import notificar, normalizar_data_br  # noqa: E402

import fase_a  # noqa: E402
import repo  # noqa: E402
import tse_client  # noqa: E402

LOTE_A = 500          # registros por rodada na fase A
LOTE_B = 200          # registros por rodada na fase B
PAUSA_TSE = 2.5       # pacing entre consultas TSE (jitter até +1.5s → 2.5–4s)
DAEMON_IDLE_SEG = int(os.environ.get("DAEMON_IDLE_SEG", "5") or "5")
# MAX_TENTATIVAS vem de repo.py (mesmo valor que filtra as re-tentativas de
# erro): stale/erro técnico repetido → status='error'; 'error' é terminal ao
# atingir MAX — mas reentra na fila enquanto attempts < MAX (cooldown no repo).
MAX_TENTATIVAS = repo.MAX_TENTATIVAS
ARQUIVO_STOP = WORKER_DIR / "STOP"


# ─────────────────────────────────────────────
# LOG EM ARQUIVO — Tee: console + logs/worker_YYYYMMDD.log
# ─────────────────────────────────────────────
class _Tee:
    """Duplica stdout/stderr pra um arquivo de log, prefixando hora em cada linha."""

    def __init__(self, stream, arq):
        self._stream = stream
        self._arq = arq
        self._inicio_linha = True

    def write(self, s):
        self._stream.write(s)
        try:
            for ch in s:
                if self._inicio_linha and ch not in ("\r", "\n"):
                    self._arq.write(datetime.now().strftime("[%H:%M:%S] "))
                    self._inicio_linha = False
                self._arq.write(ch)
                if ch == "\n":
                    self._inicio_linha = True
            self._arq.flush()
        except Exception:
            pass  # log é conveniência — nunca pode quebrar o fluxo

    def flush(self):
        self._stream.flush()


def _ativar_log() -> Path:
    pasta = WORKER_DIR / "logs"
    pasta.mkdir(exist_ok=True)
    caminho = pasta / f"worker_{datetime.now():%Y%m%d}.log"
    arq = open(caminho, "a", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.stdout, arq)
    sys.stderr = _Tee(sys.stderr, arq)
    return caminho


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _so_digitos(cpf: str) -> str:
    return re.sub(r"\D", "", cpf or "")


def _parar_pedido() -> bool:
    """True se o arquivo STOP existe (o arquivo é consumido ao parar)."""
    if ARQUIVO_STOP.exists():
        try:
            ARQUIVO_STOP.unlink()
        except Exception:
            pass
        return True
    return False


# cache de e-mail por user_id (evita bater no GoTrue 2x no mesmo mês)
_cache_emails: dict = {}


def _email_do_cliente(user_id) -> str:
    """
    Resolve e-mail do cliente via GoTrue admin API (service key). Qualquer
    falha → devolve o próprio user_id (o aviso continua, só menos legível).
    """
    if user_id in _cache_emails:
        return _cache_emails[user_id]
    email = user_id
    try:
        from config_worker import load_config
        cfg = load_config()
        import requests
        r = requests.get(f"{cfg.supabase_url}/auth/v1/admin/users/{user_id}",
                         headers={"Authorization": f"Bearer {cfg.supabase_service_key}"},
                         timeout=10)
        if r.status_code == 200:
            email = (r.json() or {}).get("email") or user_id
    except Exception:
        pass
    _cache_emails[user_id] = email
    return email


def _classificar(res: dict, cpf_esperado: str) -> str:
    """
    Destino do resultado (porta de poc_clicker_main._classificar):
      'ok'             → válido, pode gravar
      'stale'          → página não corresponde ao CPF; NÃO grava, re-tenta
      'nao_cadastrado' → TSE não localizou → regularizar_tse (gravado como done)
      'erro'           → página de erro técnico do TSE → re-tentar; esgota → error
    """
    situacao_raw = (res.get("situacao") or "").upper()
    if res.get("erro_tecnico"):
        return "erro"
    if "NAO_CADASTRADO" in situacao_raw or "NÃO CADASTRADO" in situacao_raw:
        return "nao_cadastrado"
    cpf_pagina = res.get("cpf_pagina")
    if not cpf_pagina or cpf_pagina != _so_digitos(cpf_esperado):
        return "stale"
    return "ok"


# ─────────────────────────────────────────────
# FASE B — consulta TSE (sequencial, com pacing e anti-stale)
# ─────────────────────────────────────────────
def _rodar_fase_b(limite: int, sb, processados_antes: int = 0,
                  limite_total: "int | None" = None) -> dict:
    regs = repo.pegar_prontos_tse(limite, sb=sb)
    stats = {"processados": 0, "apto": 0, "inapto": 0, "regularizar_tse": 0,
             "stale": 0, "erro": 0, "retries": 0, "aguardando_limite": 0,
             "aguardando_diario": 0, "aguardando_mensal": 0,
             "interrompido": False}
    if not regs:
        print("  🧾 (fase B) nada com status='ready_tse' nem erros elegíveis.")
        return stats

    # ── Tetos diário E mensal de consultas TSE por cliente (<= 0 = ilimitado) ──
    # Registros além do teto NÃO são tocados: ficam 'ready_tse' (sem attempts)
    # e continuam sozinhos (amanhã p/ o diário; após liberação p/ o mensal).
    # Só consulta que efetivamente rodou conta (sucesso, stale ou erro).
    # O check DIÁRIO roda antes do MENSAL — qualquer um dos dois barra.
    limite_diario = int(getattr(repo, "LIMITE_DIARIO_POR_CLIENTE", 0) or 0)
    limite_mensal = int(getattr(repo, "LIMITE_MENSAL_POR_CLIENTE", 0) or 0)
    mapa_donos: dict = {}
    usados_hoje: dict = {}
    usados_mes: dict = {}
    avisados_limite: set = set()    # donos já avisados de teto DIÁRIO (por lote)
    avisados_mensal: set = set()    # donos já avisados de teto MENSAL (por lote)
    if limite_diario > 0 or limite_mensal > 0:
        mapa_donos = repo.mapa_lotes_donos(sb)
        if limite_diario > 0:
            usados_hoje = repo.consultas_hoje_por_cliente(sb, mapa=mapa_donos)
        if limite_mensal > 0:
            usados_mes = repo.consultas_mes_por_cliente(sb, mapa=mapa_donos)
        partes = []
        if limite_diario > 0:
            partes.append("hoje: " + ", ".join(f"{str(u)[:8]}={n}" for u, n in
                            sorted(usados_hoje.items(), key=lambda kv: str(kv[0])))
                          + f" (teto {limite_diario})")
        if limite_mensal > 0:
            partes.append("mês: " + ", ".join(f"{str(u)[:8]}={n}" for u, n in
                            sorted(usados_mes.items(), key=lambda kv: str(kv[0])))
                          + f" (teto {limite_mensal})")
        print("  📊 fase B — consumo " + " | ".join(partes))

    cliente_tse = tse_client.cliente()
    paca = cliente_tse.pacing_interno  # HTTP real já faz pacing interno
    aviso_titulo = False  # loga 1x o fallback de título→situação por lote

    print(f"  🧾 Fase B — consultando TSE de {len(regs)} registro(s)...")
    for i, reg in enumerate(regs, start=1):
        if limite_total is not None and processados_antes + stats["processados"] >= limite_total:
            stats["interrompido"] = True
            break
        if _parar_pedido():
            print("  🛑 Arquivo STOP detectado — interrompendo entre registros.")
            stats["interrompido"] = True
            break

        cpf = _so_digitos(reg["cpf"])
        mae = (reg.get("nome_mae") or "").strip() or None
        data_iso = (reg.get("data_nascimento") or "").strip() or None
        data_br = normalizar_data_br(data_iso) if data_iso else None
        tentativas = reg.get("attempts") or 0

        # ── Tetos por dono do lote: além do limite, NÃO toca o registro ──
        # DIÁRIO primeiro; MENSAL depois (qualquer um barra). Skip mensal
        # dispara aviso único ao admin (tabela avisos_limite garante 1x/mês).
        if limite_diario > 0 or limite_mensal > 0:
            dono = mapa_donos.get(reg.get("batch_id"))
            if dono is not None:
                if limite_diario > 0 and usados_hoje.get(dono, 0) >= limite_diario:
                    stats["aguardando_limite"] += 1
                    stats["aguardando_diario"] += 1
                    if dono not in avisados_limite:
                        avisados_limite.add(dono)
                        print(f"  ⏸️  cliente {str(dono)[:8]} atingiu o limite diário "
                              f"({limite_diario}) — registros aguardam amanhã.")
                    continue
                if limite_mensal > 0 and usados_mes.get(dono, 0) >= limite_mensal:
                    stats["aguardando_limite"] += 1
                    stats["aguardando_mensal"] += 1
                    if dono not in avisados_mensal:
                        avisados_mensal.add(dono)
                        print(f"  🚧 cliente {str(dono)[:8]} atingiu o LIMITE MENSAL "
                              f"({limite_mensal}) — aguardando liberação comercial (ADM).")
                    try:
                        if repo.avisar_limite_mensal(dono, limite_mensal, sb=sb):
                            try:
                                email = _email_do_cliente(dono)
                            except Exception:
                                email = dono  # resolução falhou → avisa com user_id mesmo
                            try:
                                notificar(f"🚨 LIMITE MENSAL ATINGIDO — cliente {email} "
                                          f"({dono}) consumiu {limite_mensal} consultas em "
                                          f"{datetime.now():%m/%Y}. CPFs aguardando nova "
                                          f"cobrança; processamento pausado para este "
                                          f"cliente até liberação.")
                            except Exception as e:
                                print(f"      ⚠️  webhook falhou (avisar limite mensal): {e}")
                    except Exception as e:
                        print(f"      ⚠️  falha ao registrar limite mensal "
                              f"(não quebra a fila): {e}")
                    continue

        # Escolha da consulta: título rica (se implementada) → fallback situação
        usar_titulo = bool(mae and data_br and getattr(cliente_tse, "tem_titulo", False))
        modo = "TÍTULO" if usar_titulo else "SITUAÇÃO"
        if mae and data_br and not usar_titulo and not aviso_titulo:
            print("      ℹ️  endpoint de título/zona ainda pendente do probe — "
                  "registros com mãe+data usam a consulta de SITUAÇÃO "
                  "(sem zona/seção/município) por enquanto.")
            aviso_titulo = True

        print(f"    [{i}/{len(regs)}] CPF …{cpf[-4:]} ({modo}"
              f"{', re-tentativa' if reg.get('_retry') else ''})")
        stats["processados"] += 1  # conta TODA tentativa (sucesso, stale ou erro)
        if reg.get("_retry"):
            stats["retries"] += 1
        if limite_diario > 0 or limite_mensal > 0:
            dono = mapa_donos.get(reg.get("batch_id"))
            if dono is not None:
                usados_hoje[dono] = usados_hoje.get(dono, 0) + 1   # consulta consumida
                usados_mes[dono] = usados_mes.get(dono, 0) + 1
        try:
            repo.marcar(reg["id"], "checking", sb=sb)
            if usar_titulo:
                try:
                    res = cliente_tse.consultar_titulo(cpf, data_br, mae)
                except NotImplementedError:
                    usar_titulo = False
                    modo = "SITUAÇÃO"
                    res = tse_client.consultar_tse(cpf, nome_mae=mae,
                                                   data_nascimento=data_br)
            else:
                res = tse_client.consultar_tse(cpf, nome_mae=mae,
                                               data_nascimento=data_br)
            raw_text = res.pop("_raw", "") or ""
        except Exception as e:
            print(f"      ❌ erro na consulta: {type(e).__name__}: {e}")
            repo.marcar(reg["id"], "error", sb=sb)
            stats["erro"] += 1
            notificar(f"❌ TSE worker: erro consultando CPF …{cpf[-4:]}: {e}")
            continue

        desfecho = _classificar(res, cpf)

        if desfecho == "stale":
            stats["stale"] += 1
            # anti-stale: NÃO grava. Volta p/ ready_tse; esgotando tentativas → error
            if tentativas + 1 >= MAX_TENTATIVAS:
                repo.marcar(reg["id"], "error", sb=sb)
                print(f"      ⏭️  STALE (cpf_pagina={res.get('cpf_pagina')}) — "
                      f"tentativas esgotadas → error.")
            else:
                repo.marcar_lote([reg["id"]], "ready_tse", sb=sb)
                print(f"      ⏭️  STALE (cpf_pagina={res.get('cpf_pagina')}) — "
                      f"mantém ready_tse, tenta na próxima rodada.")
        elif desfecho == "erro":
            stats["erro"] += 1
            if tentativas + 1 >= MAX_TENTATIVAS:
                repo.marcar(reg["id"], "error", sb=sb)
                print(f"      ❌ erro técnico persistente → error "
                      f"({res.get('motivo_situacao')})")
            else:
                repo.marcar_lote([reg["id"]], "ready_tse", sb=sb)
                print(f"      ⚠️  erro técnico ({res.get('motivo_situacao')}) — "
                      f"re-tenta na próxima rodada.")
        else:  # ok / nao_cadastrado → persiste e fecha o registro
            try:
                repo.gravar_resultado_tse(reg["id"], res, raw_text, sb=sb)
                eleg = repo.mapear_elegibilidade(res.get("situacao") or "")
                if eleg == "apto":
                    stats["apto"] += 1
                elif eleg == "regularizar_tse":
                    stats["regularizar_tse"] += 1
                else:
                    stats["inapto"] += 1
                extras = [f"{k}={res[k]}" for k in ("titulo_eleitoral", "zona", "secao")
                          if res.get(k)]
                print(f"      ✅ elegibilidade={eleg}"
                      + (f" | {' | '.join(extras)}" if extras else ""))
            except Exception as e:
                stats["erro"] += 1
                repo.marcar(reg["id"], "error", sb=sb)
                print(f"      ❌ falha ao gravar resultado: {e}")
                notificar(f"❌ TSE worker: falha ao gravar CPF …{cpf[-4:]}: {e}")

        if i < len(regs) and not paca:
            time.sleep(PAUSA_TSE + random.uniform(0, 1.5))  # 2.5–4s anti-ban

    print(f"  ✅ fase B: {stats['processados']} processados "
          f"(re-tentativas: {stats['retries']}) | apto={stats['apto']} | "
          f"inapto={stats['inapto']} | regularizar_tse={stats['regularizar_tse']} | "
          f"stale={stats['stale']} | erro={stats['erro']} | "
          f"aguardando (limite diário/mensal): "
          f"{stats['aguardando_diario']}/{stats['aguardando_mensal']}")
    return stats


# ─────────────────────────────────────────────
# LOOP DE PRODUÇÃO
# ─────────────────────────────────────────────
def _rodada(fase: str, lote_a: int, lote_b: int, sb,
            processados_b: int, limite_total: "int | None") -> dict:
    feito = {"a": 0, "b": 0}
    if fase in ("a", "ambas"):
        stats_a = fase_a.rodar_fase_a(lote_a, sb=sb)
        feito["a"] = stats_a["processados"]
    if fase in ("b", "ambas"):
        stats_b = _rodar_fase_b(lote_b, sb,
                                processados_antes=processados_b,
                                limite_total=limite_total)
        feito["b"] = stats_b["processados"]
        feito["interrompido"] = stats_b.get("interrompido", False)
    return feito


def main() -> int:
    ap = argparse.ArgumentParser(description="Worker do portal — fila TSE (2 fases)")
    ap.add_argument("--producao", action="store_true",
                    help="loop contínuo até zerar a fila / parar sem progresso "
                         "(padrão: 1 rodada)")
    ap.add_argument("--limite", type=int, default=None,
                    help="máx. de registros processados por fase nesta execução")
    ap.add_argument("--fase", choices=["a", "b", "ambas"], default="ambas",
                    help="qual fase rodar (padrão: ambas)")
    ap.add_argument("--stub", action="store_true",
                    help="usa TseStubClient (dry-run sem rede — consulta de "
                         "situação real já funciona sem este flag)")
    ap.add_argument("--daemon", action="store_true",
                    help="modo servico 24/7: em vez de sair quando a fila zerar, "
                         "dorme DAEMON_IDLE_SEG (default 5s) e checa de novo. "
                         "Ideal p/ systemd; latencia de avulsa ~=5s.")
    args = ap.parse_args()
    if args.daemon:
        args.producao = True  # daemon exige o loop de producao

    os.system("color")  # habilita ANSI no terminal do Windows
    log_path = _ativar_log()
    print("═" * 60)
    print("  WORKER PORTAL — enriquecimento + consulta TSE (fila batches)")
    print("═" * 60)
    print(f"  📝 log: {log_path}")

    try:
        load_config()  # falha cedo (mensagem pt-BR) se .env.portal faltar
    except RuntimeError as e:
        print(f"  {e}")
        return 1

    tse_client.usar_cliente(tse_client.TseStubClient() if args.stub
                            else tse_client.TseHttpClient())
    modo = "STUB (dry-run)" if args.stub else "HTTP REAL (situação + onde-votar)"
    print(f"  🔌 cliente TSE: {modo}")

    sb = repo.conectar()
    lote_a = min(LOTE_A, args.limite) if args.limite else LOTE_A
    lote_b = min(LOTE_B, args.limite) if args.limite else LOTE_B

    processados_b_total = 0
    try:
        if not args.producao:
            _rodada(args.fase, lote_a, lote_b, sb, 0, args.limite)
        else:
            anterior = None
            rodada = 0
            avisou_zerada = False  # em daemon, não spamar webhook a cada checagem
            while True:
                rodada += 1
                restantes = repo.contar_pendentes(sb)
                print(f"\n══════ RODADA {rodada} — {restantes} registro(s) aberto(s) ══════")
                if _parar_pedido():
                    print("🛑 Arquivo STOP detectado — encerrando.")
                    break
                if restantes == 0:
                    if not avisou_zerada:
                        print("🎉 Fila ZERADA.")
                        notificar("🎉 TSE worker: fila zerada — nada mais a processar.")
                        avisou_zerada = True
                    if args.daemon:
                        time.sleep(DAEMON_IDLE_SEG)
                        anterior = None
                        continue
                    break
                if anterior is not None and restantes >= anterior:
                    print(f"⚠️  Sem progresso ({restantes} abertos, igual à rodada "
                          f"anterior).")
                    if args.daemon:
                        time.sleep(DAEMON_IDLE_SEG)
                        anterior = None
                        continue
                    notificar(f"⚠️ TSE worker: parado sem progresso "
                              f"({restantes} registros abertos).")
                    break
                anterior = restantes
                avisou_zerada = False
                feito = _rodada(args.fase, lote_a, lote_b, sb,
                                processados_b_total, args.limite)
                processados_b_total += feito["b"]
                finalizados = repo.atualizar_batches(sb)
                for bid in finalizados:
                    notificar(f"🏁 TSE worker: lote {bid[:8]} concluído (todos os "
                              f"registros processados).")
                if feito.get("interrompido"):
                    break
                if feito["a"] == 0 and feito["b"] == 0:
                    if args.daemon:
                        time.sleep(DAEMON_IDLE_SEG)
                        anterior = None
                        continue
                    print("⚠️  Rodada sem trabalho — encerrando.")
                    break

        finalizados = repo.atualizar_batches(sb)
        print(f"\n✔ Fim. Lotes finalizados nesta execução: {len(finalizados)}.")
        return 0
    except KeyboardInterrupt:
        print("\n⏹️  Interrompido (Ctrl+C). O que já foi gravado está salvo; o resto "
              "continua na fila para o próximo run.")
        notificar("⏹️ TSE worker: interrompido (Ctrl+C).")
        return 130
    except Exception as e:
        print(f"\n❌ Erro fatal: {type(e).__name__}: {e}")
        notificar(f"❌ TSE worker: erro fatal: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
