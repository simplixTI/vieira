"""
verificar_enriquecidos_sem_mae.py — conta, por tenant ativo, quantos registros
estão marcados como 'enriquecido' mas ainda estão SEM nome_mae. Isso é o que
impede a consulta de TÍTULO no TSE de retornar zona/seção.

Uso:
    py -3 verificar_enriquecidos_sem_mae.py

Só lê o Supabase, não grava nada.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from supabase_repo import conectar
from settings import TENANTS_ALVO, TABELAS_ALVO, CLIENTES

# nome → uuid → nome (para exibir bonito)
_NOMES = {uuid: nome for nome, uuid in CLIENTES.items()}


def _contar(sb, tabela: str, tenant_id: str, filtro: str) -> int:
    """Retorna a contagem de registros na tabela para o tenant, aplicando o filtro."""
    q = (
        sb.table(tabela)
        .select("id", count="exact")
        .eq("tenant_id", tenant_id)
    )
    if filtro == "enriquecido_sem_mae":
        q = q.eq("enriquecimento_status", "enriquecido").is_("nome_mae", "null")
    elif filtro == "enriquecido_com_mae":
        q = q.eq("enriquecimento_status", "enriquecido").not_.is_("nome_mae", "null")
    elif filtro == "total_enriquecido":
        q = q.eq("enriquecimento_status", "enriquecido")
    elif filtro == "total":
        pass
    resp = q.limit(1).execute()
    return resp.count or 0


def main() -> int:
    sb = conectar()
    print("=" * 90)
    print(f"  📊 Registros 'enriquecido' SEM nome_mae — por tenant ativo")
    print("=" * 90)

    header = f"  {'Tenant':<22} {'Tabela':<12} {'total':>8} {'enriq':>8} {'sem_mae':>10} {'% sem_mae':>10}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    totais = {"total": 0, "enriquecido": 0, "sem_mae": 0}
    por_tenant: dict = {}

    for tenant_id in TENANTS_ALVO:
        nome = _NOMES.get(tenant_id, tenant_id[:8])
        por_tenant[nome] = {"total": 0, "enriquecido": 0, "sem_mae": 0}
        for tabela in TABELAS_ALVO:
            total = _contar(sb, tabela, tenant_id, "total")
            enriq = _contar(sb, tabela, tenant_id, "total_enriquecido")
            sem_mae = _contar(sb, tabela, tenant_id, "enriquecido_sem_mae")
            pct = (sem_mae / enriq * 100) if enriq else 0.0
            print(f"  {nome:<22} {tabela:<12} {total:>8} {enriq:>8} {sem_mae:>10} {pct:>9.1f}%")
            por_tenant[nome]["total"] += total
            por_tenant[nome]["enriquecido"] += enriq
            por_tenant[nome]["sem_mae"] += sem_mae
            totais["total"] += total
            totais["enriquecido"] += enriq
            totais["sem_mae"] += sem_mae

    print("  " + "-" * (len(header) - 2))
    print(f"  {'SUBTOTAL POR TENANT':<22}")
    for nome, agg in por_tenant.items():
        pct = (agg["sem_mae"] / agg["enriquecido"] * 100) if agg["enriquecido"] else 0.0
        print(f"  {nome:<22} {'—':<12} {agg['total']:>8} {agg['enriquecido']:>8} {agg['sem_mae']:>10} {pct:>9.1f}%")

    print("  " + "=" * (len(header) - 2))
    pct_total = (totais["sem_mae"] / totais["enriquecido"] * 100) if totais["enriquecido"] else 0.0
    print(f"  {'TOTAL GERAL':<22} {'—':<12} {totais['total']:>8} {totais['enriquecido']:>8} {totais['sem_mae']:>10} {pct_total:>9.1f}%")
    print()
    print(f"  → {totais['sem_mae']} eleitores estão 'enriquecidos' mas SEM nome_mae")
    print(f"    (nesses o TSE cai no fallback SITUAÇÃO, que não traz zona/seção)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
