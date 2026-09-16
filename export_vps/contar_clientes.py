"""Conta registros por cliente/tabela/status para os 4 clientes-alvo."""
from config import load_config
from supabase import create_client, ClientOptions

CLIENTES = {
    "Diego Alba":       "4fd158f8-75a0-4e60-ba64-a770b76f15a9",
    "Renato de Paula":  "33342657-04bb-45c3-8790-0dcc893ed0c1",
    "Reimont":          "33fc00b6-ad0f-47a7-8d7f-d64a71cee36f",
    "Leo Gadelha":      "67a80233-3a0a-4618-b387-7ffca7793d2a",
}
TABELAS = ["eleitores", "liderancas"]

cfg = load_config()
sb = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_SERVICE_KEY,
                   options=ClientOptions(postgrest_client_timeout=120))


def q_count(tabela, tenant, filtros):
    q = sb.table(tabela).select("id", count="exact").eq("tenant_id", tenant)
    for f in filtros:
        q = f(q)
    return q.limit(1).execute().count or 0


def enriq_pendente(q):
    return q.in_("enriquecimento_status", ["pendente", "erro"]).not_.is_("cpf", "null").neq("cpf", "")

def enriq_ok(q):
    return q.eq("enriquecimento_status", "enriquecido")

def enriq_sem_dados(q):
    return q.eq("enriquecimento_status", "sem_dados")

def total_com_cpf(q):
    return q.not_.is_("cpf", "null").neq("cpf", "")

def eleg_pendente(q):
    return q.in_("elegibilidade", ["nao_verificado", "pendente"]).not_.is_("cpf", "null").neq("cpf", "")


print(f"{'Cliente':<18} {'Tabela':<12} {'TOTAL':>7} {'ENRIQ':>7} {'SDADOS':>7} {'PEND':>7} {'ELEG_P':>7}")
print("-" * 75)
grand = {"total": 0, "enriq": 0, "sdados": 0, "pend": 0, "eleg_p": 0}
por_cliente = {}
for nome, tid in CLIENTES.items():
    por_cliente[nome] = {"total": 0, "enriq": 0, "sdados": 0, "pend": 0, "eleg_p": 0}
    for t in TABELAS:
        total  = q_count(t, tid, [total_com_cpf])
        enriq  = q_count(t, tid, [enriq_ok])
        sdados = q_count(t, tid, [enriq_sem_dados])
        pend   = q_count(t, tid, [enriq_pendente])
        eleg_p = q_count(t, tid, [eleg_pendente])
        print(f"{nome:<18} {t:<12} {total:>7} {enriq:>7} {sdados:>7} {pend:>7} {eleg_p:>7}")
        por_cliente[nome]["total"]  += total
        por_cliente[nome]["enriq"]  += enriq
        por_cliente[nome]["sdados"] += sdados
        por_cliente[nome]["pend"]   += pend
        por_cliente[nome]["eleg_p"] += eleg_p
        for k, v in [("total",total),("enriq",enriq),("sdados",sdados),("pend",pend),("eleg_p",eleg_p)]:
            grand[k] += v

print("-" * 75)
print("SUBTOTAIS POR CLIENTE:")
for nome, d in por_cliente.items():
    print(f"  {nome:<18} total={d['total']:>6} enriq={d['enriq']:>6} sem_dados={d['sdados']:>6} pendente_enriq={d['pend']:>6} pendente_TSE={d['eleg_p']:>6}")

print("-" * 75)
print(f"{'GRAND TOTAL':<30} total={grand['total']} enriq={grand['enriq']} sem_dados={grand['sdados']} pendente_enriq={grand['pend']} pendente_TSE={grand['eleg_p']}")
