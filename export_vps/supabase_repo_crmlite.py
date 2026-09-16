"""
supabase_repo_crmlite.py — camada de TRADUÇÃO drop-in do schema antigo
(tabelas multi-tenant `eleitores`/`liderancas`) para o schema do CRM Lite
(tabela única `public.people`, sem tenant).

Expõe EXATAMENTE as mesmas funções que `poc_clicker_main.py` importa de
`supabase_repo`, com as mesmas assinaturas, mas lendo/gravando em `people`:

    conectar, conectar_threadlocal,
    carregar_pendentes, carregar_pendentes_n, contar_pendentes_eleg,
    carregar_enriquecimento_pendente, carregar_enriquecimento_pendente_por_tenant,
    carregar_pendentes_por_tenant,
    carregar_com_mae_para_tse, contar_com_mae_para_tse,
    gravar_resultado, mapear_elegibilidade

Mapeamento de colunas (antigo → CRM Lite):
    nome                    → full_name
    nome_mae                → mother_name
    data_nascimento         → birth_date
    elegibilidade           → voter_status (enum novo, ver _ELEG_PARA_NOVO)
    zona_eleitoral          → voter_zone
    secao_eleitoral         → voter_section
    municipio/municipio_votacao → voter_city
    uf                      → voter_state
    elegibilidade_verificado_em → voter_checked_at
    motivo_situacao         → voter_check_error

Não existem no CRM Lite (descartados na gravação, com aviso no console):
    titulo_eleitoral, local_votacao, endereco_votacao, bairro_votacao,
    biometria, obrigacao_eleitoral, ano_situacao, enriquecimento_status,
    data_nascimento_br, tenant_id.

Detalhe importante: o orquestrador (poc_clicker_main.py) grava o
ENRIQUECIMENTO direto via `sb.table(reg["_tabela"]).update(payload)` com os
nomes de colunas ANTIGOS. Por isso `conectar()` devolve um cliente embrulhado
(_ClienteCRMLite) que intercepta `.table("people").update(...)` e traduz o
payload antes de enviar. Os SELECTs passam direto (o proxy repassa tudo que
não é `update` para o builder real do postgrest).
"""

import re
import threading
from datetime import datetime, timezone

from supabase import create_client, Client, ClientOptions

from config import load_config
from utils import VERMELHO, AMARELO

try:
    from settings import LOTE_TAMANHO
except Exception:
    LOTE_TAMANHO = 50  # fallback se settings.py não estiver disponível

_cfg = load_config()

# Tabela única do CRM Lite — todos os registros carregados recebem _tabela=TABELA
TABELA = "people"


# ─────────────────────────────────────────────
# MAPEAMENTO DE ELEGIBILIDADE
# ─────────────────────────────────────────────
# Enum ANTIGO (eleitores/liderancas) → enum NOVO (people.voter_status)
_ELEG_PARA_NOVO = {
    "apto": "eligible",
    "inapto_cancelado": "not_eligible",
    "inapto_suspenso": "not_eligible",
    "inapto_transferido": "not_eligible",
    "regularizar_tse": "not_found",
    "nao_verificado": "pending",
    "pendente": "pending",
}

# Inverso (para devolver `elegibilidade` nos dicts com nomes antigos).
# 'error' volta como 'pendente': registro com erro técnico é RE-TENTADO no TSE.
_ELEG_PARA_ANTIGO = {
    "eligible": "apto",
    "not_eligible": "inapto_cancelado",
    "not_found": "regularizar_tse",
    "pending": "pendente",
    "error": "pendente",
}

# voter_status considerados "precisam de scraping TSE" (pendentes + retry de erro)
_STATUS_PENDENTES_TSE = ["pending", "error"]

# Filtro OR (postgrest) para quem TEM mãe+data e ainda precisa do TSE:
#   1) pendente/erro (nunca verificado ou falha técnica → retry)
#   2) not_found (regularizar_tse — antes faltava mãe/data; agora tem → retry)
#   3) eligible SEM voter_zone (rodar TÍTULO pra pegar zona/seção)
_COM_MAE_OR = (
    "voter_status.in.(pending,error),"
    "voter_status.eq.not_found,"
    "and(voter_status.eq.eligible,voter_zone.is.null)"
)

# Filtro OR (postgrest) para "pendente de enriquecimento": falta mãe OU data
_ENRIQ_OR = "mother_name.is.null,birth_date.is.null"

# Chaves antigas → novas na GRAVAÇÃO (update). Valor None = descartar.
_MAP_ESCRITA = {
    "nome": "full_name",
    "nome_mae": "mother_name",
    "data_nascimento": "birth_date",
    "elegibilidade": "voter_status",          # valor traduzido em _traduzir_escrita
    "zona_eleitoral": "voter_zone",
    "secao_eleitoral": "voter_section",
    "municipio": "voter_city",
    "municipio_votacao": "voter_city",
    "uf": "voter_state",
    "elegibilidade_verificado_em": "voter_checked_at",
    "motivo_situacao": "voter_check_error",
    # Sem coluna correspondente no CRM Lite — descartados:
    "data_nascimento_br": None,
    "enriquecimento_status": None,
    "titulo_eleitoral": None,
    "local_votacao": None,
    "endereco_votacao": None,
    "bairro_votacao": None,
    "biometria": None,
    "obrigacao_eleitoral": None,
    "ano_situacao": None,
    "tenant_id": None,
}

# Chaves descartadas já avisadas (evita spam de log a cada update)
_descartados_avisados: set = set()


def _traduzir_escrita(payload: dict) -> dict:
    """Traduz um payload com nomes de colunas ANTIGOS para colunas de `people`.

    - 'elegibilidade' vira 'voter_status' com o valor mapeado pro enum novo;
    - chaves sem equivalente são descartadas (aviso 1x por chave no console);
    - chaves já no formato novo passam intactas.
    """
    out: dict = {}
    for k, v in payload.items():
        if k in _MAP_ESCRITA:
            novo = _MAP_ESCRITA[k]
            if novo is None:
                if k not in _descartados_avisados:
                    _descartados_avisados.add(k)
                    print(AMARELO(f"  [info] coluna '{k}' não existe no CRM Lite — ignorada na gravação."))
                continue
            if k == "elegibilidade":
                v = _ELEG_PARA_NOVO.get(str(v), "pending")
            out[novo] = v
        elif k in _ELEG_PARA_NOVO.values() or k.startswith("voter_") or k in (
            "full_name", "mother_name", "birth_date", "cpf",
        ):
            out[k] = v  # já está no formato novo
        else:
            if k not in _descartados_avisados:
                _descartados_avisados.add(k)
                print(AMARELO(f"  [info] chave desconhecida '{k}' ignorada na gravação (CRM Lite)."))
    return out


# ─────────────────────────────────────────────
# CLIENTE EMBRULHADO — intercepta update() na tabela people
# ─────────────────────────────────────────────
class _TabelaPeopleProxy:
    """Proxy do table-builder: traduz o payload de .update(); o resto repassa.

    .select()/.insert() etc. caem no __getattr__ e devolvem o builder REAL do
    postgrest, então o encadeamento (.eq().in_().order().limit().execute())
    funciona nativamente.
    """

    def __init__(self, real):
        self._real = real

    def update(self, payload: dict):
        return self._real.update(_traduzir_escrita(dict(payload)))

    def __getattr__(self, nome):
        return getattr(self._real, nome)


class _ClienteCRMLite:
    """Embrulha o Client do supabase-py: .table('people') devolve proxy."""

    def __init__(self, real: Client):
        self._real = real

    def table(self, nome: str):
        t = self._real.table(nome)
        return _TabelaPeopleProxy(t) if nome == TABELA else t

    def from_(self, nome: str):  # supabase-py expõe from_() além de table()
        return self.table(nome)

    def __getattr__(self, nome):
        return getattr(self._real, nome)


def _cliente_real(sb) -> Client:
    """Desembrulha _ClienteCRMLite (queries internas usam o cliente cru)."""
    return getattr(sb, "_real", sb)


# ─────────────────────────────────────────────
# CONEXÃO
# ─────────────────────────────────────────────
def conectar() -> _ClienteCRMLite:
    """Cria o cliente Supabase (service_role) com timeout generoso."""
    return _ClienteCRMLite(create_client(
        _cfg.SUPABASE_URL,
        _cfg.SUPABASE_SERVICE_KEY,
        options=ClientOptions(postgrest_client_timeout=120),
    ))


_tls = threading.local()


def conectar_threadlocal() -> _ClienteCRMLite:
    """Cliente Supabase PRÓPRIO da thread chamadora (cacheado).

    postgrest-py/httpx não garantem thread-safety num cliente compartilhado —
    use esta função dentro de ThreadPoolExecutor (ex.: enriquecimento paralelo).
    """
    c = getattr(_tls, "client", None)
    if c is None:
        c = conectar()
        _tls.client = c
    return c


# ─────────────────────────────────────────────
# HELPERS DE LEITURA
# ─────────────────────────────────────────────
def _cpf_valido(cpf: "str | None") -> bool:
    return len(re.sub(r"\D", "", cpf or "")) == 11


def _iso_para_br(valor) -> str:
    """'YYYY-MM-DD' (date do Postgres) → 'DD/MM/AAAA'. '' se não parsear."""
    if not valor:
        return ""
    s = str(valor)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return ""


def _para_registro_antigo(r: dict) -> dict:
    """Converte uma linha de `people` para o dict com NOMES ANTIGOS que o
    orquestrador espera (nome, nome_mae, data_nascimento, data_nascimento_br,
    elegibilidade, _tabela...)."""
    return {
        "id": r.get("id"),
        "cpf": r.get("cpf"),
        "nome": r.get("full_name"),
        "nome_mae": r.get("mother_name"),
        "data_nascimento": r.get("birth_date"),
        "data_nascimento_br": _iso_para_br(r.get("birth_date")),
        "elegibilidade": _ELEG_PARA_ANTIGO.get(r.get("voter_status"), "pendente"),
        "zona_eleitoral": r.get("voter_zone"),
        "tenant_id": None,  # CRM Lite não tem tenant — nome_tenant(None) vira "?"
        "_tabela": TABELA,
    }


_SELECT_TSE = "id, cpf, full_name, mother_name, birth_date, voter_status, voter_zone"
_SELECT_ENRIQ = "id, cpf, full_name, mother_name, birth_date, voter_status"


# ─────────────────────────────────────────────
# SCRAPING TSE: registros pendentes
# ─────────────────────────────────────────────
def _carregar_pendentes_people(sb: Client, limite: int) -> list:
    """Registros de `people` que precisam de scraping TSE (pending + erro→retry)."""
    try:
        resp = (
            sb.table(TABELA)
            .select(_SELECT_TSE)
            .eq("status", "active")
            .in_("voter_status", _STATUS_PENDENTES_TSE)
            .not_.is_("cpf", "null")
            .neq("cpf", "")
            .order("voter_checked_at", desc=False, nullsfirst=True)  # nunca verificados 1º
            .limit(limite * 3)  # pedimos mais; filtramos CPF inválido em Python
            .execute()
        )
        registros = [
            _para_registro_antigo(r)
            for r in (resp.data or [])
            if _cpf_valido(r.get("cpf"))
        ][:limite]
        return registros
    except Exception as e:
        print(VERMELHO(f"  ⚠️  Falha ao ler {TABELA}: {e}"))
        return []


def carregar_pendentes(sb) -> list:
    """Pendentes de scraping TSE, até LOTE_TAMANHO registros."""
    return carregar_pendentes_n(sb, LOTE_TAMANHO)


def carregar_pendentes_n(sb, limite: int) -> list:
    """Como carregar_pendentes, mas com limite explícito (modo produção)."""
    return _carregar_pendentes_people(_cliente_real(sb), limite)


def contar_pendentes_eleg(sb) -> int:
    """Conta registros de `people` que ainda precisam de TSE (pending + error)."""
    try:
        resp = (
            _cliente_real(sb).table(TABELA)
            .select("id", count="exact")
            .eq("status", "active")
            .in_("voter_status", _STATUS_PENDENTES_TSE)
            .not_.is_("cpf", "null")
            .neq("cpf", "")
            .limit(1)
            .execute()
        )
        return resp.count or 0
    except Exception as e:
        print(VERMELHO(f"  ⚠️  Falha ao contar {TABELA}: {e}"))
        return 0


# ─────────────────────────────────────────────
# ENRIQUECIMENTO: registros pendentes
# ─────────────────────────────────────────────
def _carregar_enriquecimento_people(sb: Client, limite: int,
                                    priorizar_sem_mae: bool = False) -> list:
    """Pendentes de enriquecimento: `people` ativos SEM mother_name OU SEM birth_date.

    O CRM Lite não tem coluna enriquecimento_status — "pendente de
    enriquecimento" é derivado da ausência de mãe/data. A gravação do
    enriquecimento preenche essas colunas, tirando o registro da fila.
    """
    try:
        q = (
            sb.table(TABELA)
            .select(_SELECT_ENRIQ)
            .eq("status", "active")
            .not_.is_("cpf", "null")
            .neq("cpf", "")
            .or_(_ENRIQ_OR)
        )
        if priorizar_sem_mae:
            # Primeiro os que estão SEM nome da mãe — é o que trava zona/seção no TSE
            q = q.order("mother_name", desc=False, nullsfirst=True)
        resp = q.limit(limite * 3).execute()
        registros = [
            _para_registro_antigo(r)
            for r in (resp.data or [])
            if _cpf_valido(r.get("cpf"))
        ][:limite]
        return registros
    except Exception as e:
        print(VERMELHO(f"  ⚠️  Falha ao ler enriquecimento de {TABELA}: {e}"))
        return []


def carregar_enriquecimento_pendente(sb, limite: int) -> list:
    """Pendentes de enriquecimento (sem mãe ou sem data de nascimento)."""
    return _carregar_enriquecimento_people(_cliente_real(sb), limite)


# ─────────────────────────────────────────────
# AMOSTRAGEM "POR TENANT" — CRM Lite não tem tenant; delega pro lote global
# ─────────────────────────────────────────────
def carregar_enriquecimento_pendente_por_tenant(sb, por_tenant: int,
                                                priorizar_sem_mae: bool = True) -> list:
    """Sem tenants no CRM Lite: devolve até `por_tenant` registros (lote único)."""
    return _carregar_enriquecimento_people(_cliente_real(sb), por_tenant,
                                           priorizar_sem_mae=priorizar_sem_mae)


def carregar_pendentes_por_tenant(sb, por_tenant: int) -> list:
    """Sem tenants no CRM Lite: devolve até `por_tenant` pendentes de TSE."""
    return _carregar_pendentes_people(_cliente_real(sb), por_tenant)


# ─────────────────────────────────────────────
# TSE — só quem JÁ TEM mãe + data
# ─────────────────────────────────────────────
def carregar_com_mae_para_tse(sb, limite: int) -> list:
    """Registros prontos pra consulta de TÍTULO (mãe+data) que ainda precisam
    do TSE: pending/error, not_found (retry) ou eligible sem zona."""
    try:
        resp = (
            _cliente_real(sb).table(TABELA)
            .select(_SELECT_TSE)
            .eq("status", "active")
            .not_.is_("mother_name", "null")
            .not_.is_("birth_date", "null")
            .not_.is_("cpf", "null")
            .neq("cpf", "")
            .or_(_COM_MAE_OR)
            .order("voter_status", desc=False)  # error/pending antes de not_found/eligible
            .limit(limite * 3)
            .execute()
        )
        return [
            _para_registro_antigo(r)
            for r in (resp.data or [])
            if _cpf_valido(r.get("cpf"))
        ][:limite]
    except Exception as e:
        print(VERMELHO(f"  ⚠️  Falha ao ler com-mãe {TABELA}: {e}"))
        return []


def contar_com_mae_para_tse(sb) -> int:
    """Conta quantos têm mãe+data e precisam de TSE (novos + retry + eligible-sem-zona)."""
    try:
        resp = (
            _cliente_real(sb).table(TABELA)
            .select("id", count="exact")
            .eq("status", "active")
            .not_.is_("mother_name", "null")
            .not_.is_("birth_date", "null")
            .not_.is_("cpf", "null")
            .neq("cpf", "")
            .or_(_COM_MAE_OR)
            .limit(1)
            .execute()
        )
        return resp.count or 0
    except Exception as e:
        print(VERMELHO(f"  ⚠️  Falha ao contar com-mãe {TABELA}: {e}"))
        return 0


# ─────────────────────────────────────────────
# GRAVAÇÃO
# ─────────────────────────────────────────────
def gravar_resultado(sb, tabela: str, registro_id: str, dados: dict) -> None:
    """Atualiza `people` com o resultado do TSE.

    - traduz chaves antigas → novas (elegibilidade→voter_status etc.);
    - sempre grava voter_checked_at = agora (UTC);
    - incrementa voter_check_attempts via read-modify-write (supabase-py não
      tem incremento atômico; preferimos não criar função SQL no banco).
    O parâmetro `tabela` é ignorado — sempre grava em `people`.
    """
    cli = _cliente_real(sb)
    payload = _traduzir_escrita(dict(dados))
    payload["voter_checked_at"] = datetime.now(timezone.utc).isoformat()

    # read-modify-write do contador de tentativas
    try:
        resp = (
            cli.table(TABELA)
            .select("voter_check_attempts")
            .eq("id", registro_id)
            .limit(1)
            .execute()
        )
        atual = 0
        if resp.data:
            atual = resp.data[0].get("voter_check_attempts") or 0
        payload["voter_check_attempts"] = atual + 1
    except Exception as e:
        # Se a leitura falhar, grava o resultado mesmo assim (sem incrementar)
        print(AMARELO(f"  ⚠️  não consegui ler voter_check_attempts ({e}); gravando sem incrementar."))

    cli.table(TABELA).update(payload).eq("id", registro_id).execute()


def mapear_elegibilidade(situacao_texto: str) -> str:
    """Converte o texto de situação do TSE para o enum ANTIGO do banco.

    Idêntica à do supabase_repo original — a tradução para o enum novo do
    CRM Lite acontece só na gravação (_traduzir_escrita).
    """
    t = situacao_texto.upper() if situacao_texto else ""
    if "NAO_CADASTRADO_TSE" in t or "NÃO CADASTRADO" in t:
        return "regularizar_tse"
    if "REGULAR" in t:
        return "apto"
    if "CANCELADO" in t:
        return "inapto_cancelado"
    if "SUSPENSO" in t:
        return "inapto_suspenso"
    if "TRANSFER" in t:
        return "inapto_transferido"
    return "regularizar_tse"
