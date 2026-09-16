"""
supabase_repo.py — leitura e gravação no Supabase (tabelas `eleitores` e
`liderancas`). Porta direta da camada de dados do scraper original.
"""

import re
import threading
from datetime import datetime

from supabase import create_client, Client, ClientOptions

from config import load_config
from settings import TENANTS_ALVO, TABELAS_ALVO, LOTE_TAMANHO
from utils import VERMELHO

_cfg = load_config()


def conectar() -> Client:
    """Cria o cliente Supabase (service_role) com timeout generoso."""
    return create_client(
        _cfg.SUPABASE_URL,
        _cfg.SUPABASE_SERVICE_KEY,
        options=ClientOptions(postgrest_client_timeout=120),
    )


_tls = threading.local()


def conectar_threadlocal() -> Client:
    """Cliente Supabase PRÓPRIO da thread chamadora (cacheado).

    postgrest-py/httpx não garantem thread-safety num cliente compartilhado —
    use esta função dentro de ThreadPoolExecutor (ex.: enriquecimento paralelo).
    """
    c = getattr(_tls, "client", None)
    if c is None:
        c = conectar()
        _tls.client = c
    return c


def _cpf_valido(cpf: str | None) -> bool:
    return len(re.sub(r"\D", "", cpf or "")) == 11


# ─────────────────────────────────────────────
# SCRAPING TSE: registros pendentes
# ─────────────────────────────────────────────
def _carregar_pendentes_tabela(sb: Client, tabela: str, limite: int) -> list:
    """Registros de UMA tabela que precisam de scraping TSE. Marca _tabela."""
    try:
        q = (
            sb.table(tabela)
            .select(
                "id, tenant_id, cpf, nome_mae, data_nascimento, "
                "data_nascimento_br, enriquecimento_status, elegibilidade, nome"
            )
            .in_("elegibilidade", ["nao_verificado", "pendente"])
            .not_.is_("cpf", "null")
            .neq("cpf", "")
            # Prioriza enriquecidos: a consulta "Número do Título" retorna MAIS dados
            .order("enriquecimento_status", desc=False)
        )
        if TENANTS_ALVO:
            q = q.in_("tenant_id", TENANTS_ALVO)
        resp = q.limit(limite * 3).execute()  # pedimos mais; filtramos CPF inválido
        registros = [r for r in (resp.data or []) if _cpf_valido(r.get("cpf"))][:limite]
        for r in registros:
            r["_tabela"] = tabela
        return registros
    except Exception as e:
        print(VERMELHO(f"  ⚠️  Falha ao ler {tabela}: {e}"))
        return []


def carregar_pendentes(sb: Client) -> list:
    """Pendentes de scraping de TODAS as tabelas alvo, até LOTE_TAMANHO no total."""
    return carregar_pendentes_n(sb, LOTE_TAMANHO)


def carregar_pendentes_n(sb: Client, limite: int) -> list:
    """Como carregar_pendentes, mas com limite explícito (usado no modo produção)."""
    todos: list = []
    restante = limite
    for tabela in TABELAS_ALVO:
        if restante <= 0:
            break
        lote = _carregar_pendentes_tabela(sb, tabela, restante)
        todos.extend(lote)
        restante -= len(lote)
    return todos


def contar_pendentes_eleg(sb: Client) -> int:
    """Conta registros SEM elegibilidade (nao_verificado/pendente) nas tabelas alvo."""
    total = 0
    for tabela in TABELAS_ALVO:
        try:
            q = (
                sb.table(tabela)
                .select("id", count="exact")
                .in_("elegibilidade", ["nao_verificado", "pendente"])
                .not_.is_("cpf", "null")
                .neq("cpf", "")
            )
            if TENANTS_ALVO:
                q = q.in_("tenant_id", TENANTS_ALVO)
            resp = q.limit(1).execute()
            total += resp.count or 0
        except Exception as e:
            print(VERMELHO(f"  ⚠️  Falha ao contar {tabela}: {e}"))
    return total


# ─────────────────────────────────────────────
# ENRIQUECIMENTO: registros pendentes
# ─────────────────────────────────────────────
def _carregar_enriquecimento_tabela(sb: Client, tabela: str, limite: int) -> list:
    """Registros de UMA tabela que precisam de enriquecimento (cascata). Marca _tabela."""
    try:
        q = (
            sb.table(tabela)
            .select("id, tenant_id, cpf, nome_mae, data_nascimento, enriquecimento_status, nome")
            .in_("enriquecimento_status", ["pendente", "erro"])  # 'erro' permite retry
            .not_.is_("cpf", "null")
            .neq("cpf", "")
        )
        if TENANTS_ALVO:
            q = q.in_("tenant_id", TENANTS_ALVO)
        resp = q.limit(limite * 3).execute()
        registros = [r for r in (resp.data or []) if _cpf_valido(r.get("cpf"))][:limite]
        for r in registros:
            r["_tabela"] = tabela
        return registros
    except Exception as e:
        print(VERMELHO(f"  ⚠️  Falha ao ler enriquecimento de {tabela}: {e}"))
        return []


def carregar_enriquecimento_pendente(sb: Client, limite: int) -> list:
    """
    Pendentes de enriquecimento em TODAS as tabelas alvo.
    Inclui status 'pendente' e 'erro' (retry); exclui 'sem_dados' e 'enriquecido'.
    """
    todos: list = []
    restante = limite
    for tabela in TABELAS_ALVO:
        if restante <= 0:
            break
        lote = _carregar_enriquecimento_tabela(sb, tabela, restante)
        todos.extend(lote)
        restante -= len(lote)
    return todos


# ─────────────────────────────────────────────
# AMOSTRAGEM POR TENANT (10 por tenant, etc.)
# ─────────────────────────────────────────────
def _enriq_tabela_tenant(sb: Client, tabela: str, tenant_id: str, limite: int,
                         priorizar_sem_mae: bool = True) -> list:
    """Enriquecimento pendente para 1 tenant + 1 tabela. Prioriza nome_mae IS NULL."""
    try:
        q = (
            sb.table(tabela)
            .select("id, tenant_id, cpf, nome_mae, data_nascimento, enriquecimento_status, nome")
            .in_("enriquecimento_status", ["pendente", "erro"])
            .not_.is_("cpf", "null")
            .neq("cpf", "")
            .eq("tenant_id", tenant_id)
        )
        if priorizar_sem_mae:
            # Primeiro os que estão SEM nome_mae — que é o que trava zona/seção no TSE
            q = q.order("nome_mae", desc=False, nullsfirst=True)
        resp = q.limit(limite * 3).execute()
        registros = [r for r in (resp.data or []) if _cpf_valido(r.get("cpf"))][:limite]
        for r in registros:
            r["_tabela"] = tabela
        return registros
    except Exception as e:
        print(VERMELHO(f"  ⚠️  Falha ao ler enriquecimento {tabela}/{tenant_id[:8]}: {e}"))
        return []


def carregar_enriquecimento_pendente_por_tenant(sb: Client, por_tenant: int,
                                                 priorizar_sem_mae: bool = True) -> list:
    """
    Retorna até `por_tenant` registros de CADA tenant em TENANTS_ALVO,
    distribuídos entre as TABELAS_ALVO. Prioriza quem está sem nome_mae
    (que é o que impede o TSE de retornar zona/seção).
    """
    todos: list = []
    for tenant_id in (TENANTS_ALVO or []):
        restante = por_tenant
        for tabela in TABELAS_ALVO:
            if restante <= 0:
                break
            lote = _enriq_tabela_tenant(sb, tabela, tenant_id, restante, priorizar_sem_mae)
            todos.extend(lote)
            restante -= len(lote)
    return todos


def _pendentes_tabela_tenant(sb: Client, tabela: str, tenant_id: str, limite: int) -> list:
    """Registros que precisam de scraping TSE para 1 tenant + 1 tabela."""
    try:
        q = (
            sb.table(tabela)
            .select(
                "id, tenant_id, cpf, nome_mae, data_nascimento, "
                "data_nascimento_br, enriquecimento_status, elegibilidade, nome"
            )
            .in_("elegibilidade", ["nao_verificado", "pendente"])
            .not_.is_("cpf", "null")
            .neq("cpf", "")
            .eq("tenant_id", tenant_id)
            .order("enriquecimento_status", desc=False)
        )
        resp = q.limit(limite * 3).execute()
        registros = [r for r in (resp.data or []) if _cpf_valido(r.get("cpf"))][:limite]
        for r in registros:
            r["_tabela"] = tabela
        return registros
    except Exception as e:
        print(VERMELHO(f"  ⚠️  Falha ao ler TSE-pendentes {tabela}/{tenant_id[:8]}: {e}"))
        return []


def carregar_pendentes_por_tenant(sb: Client, por_tenant: int) -> list:
    """Registros pendentes de TSE, até `por_tenant` para CADA tenant ativo."""
    todos: list = []
    for tenant_id in (TENANTS_ALVO or []):
        restante = por_tenant
        for tabela in TABELAS_ALVO:
            if restante <= 0:
                break
            lote = _pendentes_tabela_tenant(sb, tabela, tenant_id, restante)
            todos.extend(lote)
            restante -= len(lote)
    return todos


# ─────────────────────────────────────────────
# TSE — só quem JÁ ESTÁ ENRIQUECIDO COM NOME DA MÃE
# ─────────────────────────────────────────────
# Filtro OR (postgrest): pega quem TEM mae+data E ainda precisa de algo do TSE:
#   1) nunca verificado (nao_verificado/pendente)
#   2) regularizar_tse (retry — antes falhou, agora tem mae+data)
#   3) apto mas SEM zona_eleitoral (rodar TÍTULO pra pegar zona/seção)
_COM_MAE_OR = (
    "elegibilidade.in.(nao_verificado,pendente),"
    "elegibilidade.eq.regularizar_tse,"
    "and(elegibilidade.eq.apto,zona_eleitoral.is.null)"
)


def _com_mae_tabela(sb: Client, tabela: str, limite: int) -> list:
    """
    Registros prontos pra consulta de TÍTULO no TSE (com fallback SITUAÇÃO já embutido
    no _processar do poc_clicker_main):
      nome_mae IS NOT NULL AND data_nascimento IS NOT NULL AND
      ( elegibilidade IN ('nao_verificado','pendente')
        OR elegibilidade = 'regularizar_tse'
        OR (elegibilidade = 'apto' AND zona_eleitoral IS NULL) )
    Filtra por TENANTS_ALVO.
    """
    try:
        q = (
            sb.table(tabela)
            .select(
                "id, tenant_id, cpf, nome_mae, data_nascimento, "
                "data_nascimento_br, enriquecimento_status, elegibilidade, nome, "
                "zona_eleitoral"
            )
            .not_.is_("nome_mae", "null")
            .not_.is_("data_nascimento", "null")
            .not_.is_("cpf", "null")
            .neq("cpf", "")
            .or_(_COM_MAE_OR)
        )
        if TENANTS_ALVO:
            q = q.in_("tenant_id", TENANTS_ALVO)
        # Prioriza os que nunca foram verificados, depois retry, depois apto-sem-zona
        q = q.order("elegibilidade", desc=False)
        resp = q.limit(limite * 3).execute()
        registros = [r for r in (resp.data or []) if _cpf_valido(r.get("cpf"))][:limite]
        for r in registros:
            r["_tabela"] = tabela
        return registros
    except Exception as e:
        print(VERMELHO(f"  ⚠️  Falha ao ler com-mãe {tabela}: {e}"))
        return []


def carregar_com_mae_para_tse(sb: Client, limite: int) -> list:
    """Distribui `limite` entre TABELAS_ALVO. Só quem tem mãe+data e precisa de TSE."""
    todos: list = []
    restante = limite
    for tabela in TABELAS_ALVO:
        if restante <= 0:
            break
        lote = _com_mae_tabela(sb, tabela, restante)
        todos.extend(lote)
        restante -= len(lote)
    return todos


def contar_com_mae_para_tse(sb: Client) -> int:
    """Conta quantos têm mae+data e precisam de TSE (novos + retry + apto-sem-zona)."""
    total = 0
    for tabela in TABELAS_ALVO:
        try:
            q = (
                sb.table(tabela)
                .select("id", count="exact")
                .not_.is_("nome_mae", "null")
                .not_.is_("data_nascimento", "null")
                .not_.is_("cpf", "null")
                .neq("cpf", "")
                .or_(_COM_MAE_OR)
            )
            if TENANTS_ALVO:
                q = q.in_("tenant_id", TENANTS_ALVO)
            resp = q.limit(1).execute()
            total += resp.count or 0
        except Exception as e:
            print(VERMELHO(f"  ⚠️  Falha ao contar com-mãe {tabela}: {e}"))
    return total


# ─────────────────────────────────────────────
# GRAVAÇÃO
# ─────────────────────────────────────────────
_CAMPOS_NAO_DB = frozenset({
    "html_resposta", "status", "tipo", "cpf", "timestamp",
    "erro_extracao", "_tabela",
    "nascimento", "nome_mae_input", "cpf_pagina", "erro",
    "situacao",
})


def gravar_resultado(sb: Client, tabela: str, registro_id: str, dados: dict) -> None:
    """Atualiza o registro com os dados do TSE (filtra campos que não são colunas)."""
    payload = {
        "elegibilidade_verificado_em": datetime.now().isoformat(),
        **{k: v for k, v in dados.items() if k not in _CAMPOS_NAO_DB},
    }
    sb.table(tabela).update(payload).eq("id", registro_id).execute()


def mapear_elegibilidade(situacao_texto: str) -> str:
    """Converte o texto de situação do TSE para o enum do banco."""
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
