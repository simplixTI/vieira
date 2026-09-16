"""
enriquecer_todos_pendentes.py — enriquece TODOS os registros pendentes de
enriquecimento (status 'pendente' ou 'erro'), respeitando CLIENTE_ATIVO
de settings.py. Roda a cascata API B -> Hashiro (-> DataSintese se enabled),
em paralelo, em lotes, até zerar.

Uso:
    py -3 enriquecer_todos_pendentes.py                     # workers=8, lote=500
    py -3 enriquecer_todos_pendentes.py --workers 4
    py -3 enriquecer_todos_pendentes.py --lote 200 --max-rodadas 5
    py -3 enriquecer_todos_pendentes.py --dry-run           # nao grava
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

from supabase_repo import conectar, carregar_enriquecimento_pendente
from settings import nome_tenant
from utils import VERDE, VERMELHO, AMARELO, AZUL, NEGRITO
from enriquecimento_lite import enriquecer_cpf
from poc_clicker_main import _enriquecer_um, _status_enriq  # reusa persist + classificacao


def _worker_dry(reg):
    import re
    cpf = re.sub(r"\D", "", reg["cpf"] or "")
    falta_mae = not (reg.get("nome_mae") or "").strip()
    falta_data = not reg.get("data_nascimento")
    e = enriquecer_cpf(cpf, precisa_mae=falta_mae, precisa_data=falta_data)
    status = _status_enriq(e)
    origem = "+".join(e.get("origem") or []) or "-"
    erros = e.get("erros") or []
    return status, origem, erros


def main() -> int:
    ap = argparse.ArgumentParser(description="Enriquece TODOS pendentes (respeita CLIENTE_ATIVO)")
    ap.add_argument("--workers", type=int, default=8, help="workers paralelos (default: 8)")
    ap.add_argument("--lote", type=int, default=500, help="registros por rodada (default: 500)")
    ap.add_argument("--max-rodadas", type=int, default=100, help="rodadas maximas (default: 100)")
    ap.add_argument("--dry-run", action="store_true", help="nao grava no Supabase")
    args = ap.parse_args()

    os.system("color")
    print(NEGRITO(AZUL("=" * 70)))
    print(NEGRITO(AZUL("  ENRIQUECIMENTO TOTAL PENDENTE (API B -> Hashiro)")))
    print(NEGRITO(AZUL(f"  workers={args.workers} lote={args.lote} "
                      f"{'DRY-RUN' if args.dry_run else 'GRAVANDO'}")))
    print(NEGRITO(AZUL("=" * 70)))

    sb = conectar()
    global_stats = {"enriquecido": 0, "sem_dados": 0, "erro": 0, "erro_update": 0}

    for rodada in range(1, args.max_rodadas + 1):
        regs = carregar_enriquecimento_pendente(sb, args.lote)
        if not regs:
            print(NEGRITO(VERDE(f"\n  🎉 Zerado! Nada mais pendente ({rodada - 1} rodadas).")))
            break

        total = len(regs)
        print(NEGRITO(AZUL(f"\n  ── Rodada {rodada}: {total} registros ──")))

        stats = {"enriquecido": 0, "sem_dados": 0, "erro": 0, "erro_update": 0}
        concluidos = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            if args.dry_run:
                futs = {ex.submit(_worker_dry, r): r for r in regs}
            else:
                futs = {ex.submit(_enriquecer_um, sb, r): r for r in regs}

            for f in as_completed(futs):
                reg = futs[f]
                tenant = nome_tenant(reg.get("tenant_id"))
                erros = []
                try:
                    r = f.result()
                    if len(r) == 3:
                        status, origem, erros = r
                    else:
                        status, origem = r
                except Exception as e:
                    status, origem = "erro", f"exc:{type(e).__name__}"
                    erros = [str(e)]

                stats[status] = stats.get(status, 0) + 1
                concluidos += 1
                cor = VERDE if status == "enriquecido" else (
                    AMARELO if status == "sem_dados" else VERMELHO)
                extra = f" | erros={erros[:2]}" if status == "erro" and erros else ""
                print(cor(f"    [{concluidos}/{total}] [{tenant}] {status} ({origem}){extra}"))

        for k, v in stats.items():
            global_stats[k] = global_stats.get(k, 0) + v
        print(VERDE(f"  ✅ rodada {rodada}: {stats}"))

        if args.dry_run:
            print(AMARELO("  (dry-run: parando apos 1 rodada pra nao repetir os mesmos regs)"))
            break

    print(NEGRITO(VERDE(f"\n  ✅ FIM. Total geral: {global_stats}")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
