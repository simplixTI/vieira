"""
settings.py — parâmetros operacionais que você edita à mão (tenants, tabelas,
tamanhos de lote, delays). Segredos ficam no .env (config.py); aqui é só ajuste
de comportamento.
"""

import os

# ═══════════════════════════════════════════════════════════════════
# 🎯 CLIENTE(S) ATIVO(S) — EDITE AQUI PARA RODAR OUTRO CLIENTE
# ═══════════════════════════════════════════════════════════════════
#
# Coloque o NOME do cliente do dicionário CLIENTES abaixo.
# Exemplos:
#     CLIENTE_ATIVO = "Alfredo do Belo"                     # 1 cliente
#     CLIENTE_ATIVO = ["Alfredo do Belo", "Byanca Pitzer"]  # vários
#     CLIENTE_ATIVO = "TODOS"                               # todos do dict
#
# Também dá pra sobrescrever via variável de ambiente (sem editar arquivo):
#     PowerShell:  $env:CLIENTE_ATIVO="Alfredo do Belo"
#     Linux/Mac:   export CLIENTE_ATIVO="Alfredo do Belo"
#     Vários:      CLIENTE_ATIVO="Alfredo do Belo,Byanca Pitzer"
#
CLIENTE_ATIVO: "str | list[str]" = "Filipe Pereira"


# ═══════════════════════════════════════════════════════════════════
# ⏸️  CLIENTES PAUSADOS — só aplica quando CLIENTE_ATIVO = "TODOS"
# ═══════════════════════════════════════════════════════════════════
#
# Descomente o nome pra PULAR esse cliente na rodada "TODOS".
# Pra reativar: comente a linha de novo. Ninguém é apagado do cadastro.
#
CLIENTES_PAUSADOS: "list[str]" = [
    # "Alfredo do Belo",       ← ATIVO
    "Byanca Pitzer",
    "Diego Alba",
    "Dudu Padrinho",
    "Edmundo",
    "Elton Cristo",
    "Felipinho Ravis",
    # "Filipe Pereira",        ← ATIVO
    # "Leo Gadelha",           ← ATIVO
    # "Luciana Polati",        ← ATIVO
    "Marcos Tavares",
    "Marquinhos Dentista",
    # "Reimont",               ← ATIVO
    # "Renato de Paula",       ← ATIVO
    # "Walace Presidente",     ← ATIVO
]


# ═══════════════════════════════════════════════════════════════════
# 📋 REGISTRO DE CLIENTES — nome → UUID do tenant no Supabase
# ═══════════════════════════════════════════════════════════════════
# Para adicionar novo cliente: cole aqui  "Nome do Cliente": "uuid",
CLIENTES: "dict[str, str]" = {
    "Alfredo do Belo":     "34aa62aa-5812-45f9-b66d-974a037ac730",
    "Byanca Pitzer":       "3f0b4562-0730-4a54-9f32-7c73e3c5699f",
    "Diego Alba":          "4fd158f8-75a0-4e60-ba64-a770b76f15a9",
    "Dudu Padrinho":       "c80c20ec-4e17-4da4-b538-138648fb70d5",
    "Edmundo":             "3bc86bba-7752-4bdb-9dab-d4c23707503b",
    "Elton Cristo":        "f795f0b2-5ffb-42ed-9a9d-38ca29b14182",
    "Felipinho Ravis":     "fcc9f81d-3155-4595-be65-105392bac242",
    "Filipe Pereira":      "23f65b2e-966e-4d40-ad5a-4b80b848d1c5",
    "Leo Gadelha":         "67a80233-3a0a-4618-b387-7ffca7793d2a",
    "Luciana Polati":      "145eaa1f-0538-4c5d-b220-daadc60bbc9f",
    "Marcos Tavares":      "0b479271-aac5-46ba-93f0-24ea55ebf66c",
    "Marquinhos Dentista": "a42e9b3b-2899-4978-8388-0852e6135de7",
    "Reimont":             "33fc00b6-ad0f-47a7-8d7f-d64a71cee36f",
    "Renato de Paula":     "33342657-04bb-45c3-8790-0dcc893ed0c1",
    "Walace Presidente":   "33042010-c7bc-485f-bf59-28b950f05a1c",
}


# ── Resolução automática — NÃO precisa editar daqui pra baixo ─────────

def _resolver_tenants_alvo() -> list:
    """CLIENTE_ATIVO (nome/lista/'TODOS') → lista de UUIDs. Env var tem prioridade."""
    env = os.environ.get("CLIENTE_ATIVO", "").strip()
    selecao = env if env else CLIENTE_ATIVO

    if isinstance(selecao, str):
        if selecao.upper() == "TODOS":
            pausados = {p.strip() for p in CLIENTES_PAUSADOS if p.strip()}
            return [uuid for nome, uuid in CLIENTES.items() if nome not in pausados]
        nomes = [n.strip() for n in selecao.split(",") if n.strip()]
    else:
        nomes = [str(n).strip() for n in selecao if str(n).strip()]

    uuids: list = []
    desconhecidos: list = []
    for nome in nomes:
        if nome in CLIENTES:
            uuids.append(CLIENTES[nome])
        elif nome in CLIENTES.values():  # aceita UUID direto também
            uuids.append(nome)
        else:
            desconhecidos.append(nome)

    if desconhecidos:
        disponiveis = ", ".join(CLIENTES.keys())
        raise RuntimeError(
            f"CLIENTE_ATIVO desconhecido: {desconhecidos}. "
            f"Clientes disponíveis: {disponiveis}"
        )
    return uuids


TENANTS_ALVO = _resolver_tenants_alvo()
TENANTS_NOMES = {uuid: nome for nome, uuid in CLIENTES.items()}


def nome_tenant(tenant_id):
    """Nome legível do tenant, ou prefixo do UUID se desconhecido."""
    if not tenant_id:
        return "?"
    return TENANTS_NOMES.get(tenant_id, tenant_id[:8])


# ── Tabelas processadas a cada lote (mesmas colunas de scraping) ──
TABELAS_ALVO = ["eleitores", "liderancas"]

# ── Tamanhos de lote ──
LOTE_TAMANHO        = 50    # registros do TSE (scraping) por rodada
LOTE_ENRIQUECIMENTO = 500   # registros enriquecidos pela cascata por rodada

# ── Delays (segundos) ──
DELAY_ENTRE_CONSULTAS = 2     # entre consultas TSE
DELAY_ENRICHMENT      = 1     # entre chamadas de enriquecimento (modo sequencial)
TIMEOUT_ELEMENTO      = 15    # espera de elementos no browser
PAUSA_ENTRE_LOTES     = 10    # pausa entre lotes

# ── Captcha MANUAL: tempo máximo que o script espera você resolver no celular ──
CAPTCHA_MANUAL_TIMEOUT = 240  # segundos
TIMEOUT_RESULTADO      = 40   # espera o resultado renderizar após resolver o captcha

# ── Enriquecimento paralelo ──
ENRIQUECIMENTO_PARALELO = True
ENRIQUECIMENTO_WORKERS  = 5

# ── Flags da cascata de enriquecimento (liga/desliga cada API) ──
HASHIRO_ENABLED     = True
API_B_ENABLED       = True
DATASINTESE_ENABLED = False

# ── Endpoints das APIs de enriquecimento ──
HASHIRO_BASE_URL = "https://hashirosearch.squareweb.app/"
HASHIRO_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
API_B_URL_BASE = "http://200.9.155.126:3000/api/v1/pessoa"
DATASINTESE_AUTH_URL  = "https://api.datasintese.com/auth"
DATASINTESE_QUERY_URL = "https://api.datasintese.com/pfmaster"

# ── URLs do TSE ──
URL_SITUACAO = (
    "https://www.tse.jus.br/servicos-eleitorais/autoatendimento-eleitoral"
    "#/atendimento-eleitor/consultar-situacao-titulo-eleitor"
)
URL_NUMERO_TITULO = (
    "https://www.tse.jus.br/servicos-eleitorais/autoatendimento-eleitoral"
    "#/atendimento-eleitor/consultar-numero-titulo-eleitor"
)
