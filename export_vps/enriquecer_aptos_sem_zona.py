"""
enriquecer_aptos_sem_zona.py — enriquece SÓ os aptos-sem-zona que faltam mae/data.

Alvo (dos tenants ativos em TENANTS_ALVO):
    elegibilidade = 'apto'
    zona_eleitoral IS NULL
    ( nome_mae IS NULL OR data_nascimento IS NULL )

Roda em PARALELO (ThreadPoolExecutor) sem captcha. Usa a cascata API B → Hashiro
(respeita DATASINTESE_ENABLED da settings.py). Escreve nome_mae/data_nascimento/
enriquecimento_status por registro — cada worker persiste sozinho.

Uso:
    py -3 enriquecer_aptos_sem_zona.py                # todos (2.571 alvo)
    py -3 enriquecer_aptos_sem_zona.py --limite 500   # só 500
    py -3 enriquecer_aptos_sem_zona.py --workers 12   # + workers
    py -3 enriquecer_aptos_sem_zona.py --dry-run      # não grava nada
"""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from supabase_repo import conectar, _cpf_valido
from settings import TENANTS_ALVO, TABELAS_ALVO, nome_tenant
from utils import VERDE, VERMELHO, AMARELO, AZUL, NEGRITO


def _carregar_alvo(sb, limite: int) -> list:
    """Aptos-sem-zona que faltam mae ou data, filtrados por TENANTS_ALVO."""
    todos: list = []
    restante = limite
    for tabela in TABELAS_ALVO:
        if restante <= 0:
            break
        try:
            q = (
                sb.table(tabela)
                .select("id, tenant_id, cpf, nome_mae, data_nascimento, enriquecimento_status, nome")
                .eq("elegibilidade", "apto")
                .is_("zona_eleitoral", "null")
                .or_("nome_mae.is.null,data_nascimento.is.null")
                .not_.is_("cpf", "null")
                .neq("cpf", "")
            )
            if TENANTS_ALVO:
                q = q.in_("tenant_id", TENANTS_ALVO)
            resp = q.limit(restante * 3).execute()
            lote = [r for r in (resp.data or []) if _cpf_valido(r.get("cpf"))][:restante]
            for r in lote:
                r["_tabela"] = tabela
            todos.extend(lote)
            restante -= len(lote)
        except Exception as e:
            print(VERMELHO(f"  ⚠️  Falha ao ler {tabela}: {e}"))
    return todos


def _contar_alvo(sb) -> int:
    """Conta o pool total (sem gastar paginação)."""
    total = 0
    for tabela in TABELAS_ALVO:
        try:
            q = (
                sb.table(tabela)
                .select("id", count="exact")
                .eq("elegibilidade", "apto")
                .is_("zona_eleitoral", "null")
                .or_("nome_mae.is.null,data_nascimento.is.null")
                .not_.is_("cpf", "null")
                .neq("cpf", "")
            )
            if TENANTS_ALVO:
                q = q.in_("tenant_id", TENANTS_ALVO)
            total += q.limit(1).execute().count or 0
        except Exception as e:
            print(VERMELHO(f"  ⚠️  Falha ao contar {tabela}: {e}"))
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Enriquece aptos-sem-zona (API B → Hashiro)")
    ap.add_argument("--limite", type=int, default=10000, help="máx de registros a puxar (default: 10k)")
    ap.add_argument("--workers", type=int, default=8, help="workers paralelos (default: 8)")
    ap.add_argument("--dry-run", action="store_true", help="não grava no Supabase; só reporta")
    args = ap.parse_args()

    os.system("color")
    print(NEGRITO(AZUL("═" * 70)))
    print(NEGRITO(AZUL("  ENRIQUECIMENTO — aptos-sem-zona que faltam mãe/data")))
    print(NEGRITO(AZUL("═" * 70)))

    sb = conectar()
    pool_total = _contar_alvo(sb)
    print(AZUL(f"  🔎 Pool total no Supabase: {pool_total} registros"))

    regs = _carregar_alvo(sb, args.limite)
    if not regs:
        print(AMARELO("  (nada pra enriquecer)"))
        return 0
    total = len(regs)
    print(NEGRITO(AZUL(
        f"  🔎 Processando {total} (workers={args.workers}, "
        f"{'DRY-RUN' if args.dry_run else 'GRAVANDO'})"
    )))

    if args.dry_run:
        from enriquecimento_lite import enriquecer_cpf
        import re

        def _worker(reg):
            cpf = re.sub(r"\D", "", reg["cpf"] or "")
            falta_mae = not (reg.get("nome_mae") or "").strip()
            falta_data = not reg.get("data_nascimento")
            e = enriquecer_cpf(cpf, precisa_mae=falta_mae, precisa_data=falta_data)
            got = bool(e.get("nome_mae")) or bool(e.get("data_nascimento"))
            status = "enriquecido" if got else ("erro" if e.get("erros") else "sem_dados")
            return status, "+".join(e.get("origem") or []) or "-", e.get("erros") or []
    else:
        from poc_clicker_main import _enriquecer_um

        def _worker(reg):
            status, origem = _enriquecer_um(sb, reg)
            return status, origem, []

    stats = {"enriquecido": 0, "sem_dados": 0, "erro": 0, "erro_update": 0}
    concluidos = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_worker, r): r for r in regs}
        for f in as_completed(futs):
            reg = futs[f]
            tenant = nome_tenant(reg.get("tenant_id"))
            try:
                r = f.result()
                if len(r) == 3:
                    status, origem, erros = r
                else:
                    status, origem = r
                    erros = []
            except Exception as e:
                status, origem, erros = "erro", f"exc:{type(e).__name__}", [str(e)]
            stats[status] = stats.get(status, 0) + 1
            concluidos += 1
            cor = VERDE if status == "enriquecido" else (AMARELO if status == "sem_dados" else VERMELHO)
            extra = f" | erros={erros[:2]}" if status == "erro" and erros else ""
            print(cor(f"    [{concluidos}/{total}] [{tenant}] {status} ({origem}){extra}"))

    print(VERDE(f"\n  ✅ Fim. {stats}"))
    print(AZUL(f"  → rode agora o TSE-título nos que ficaram com mae+data."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
