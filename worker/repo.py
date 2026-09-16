"""
repo.py — acesso a dados do Supabase (tabelas `batches` e `voter_records`).

Cliente criado com a SERVICE KEY (bypassa RLS — o portal nunca vê essa chave).
Em fases paralelas (Fase A) cada thread usa seu próprio cliente:
postgrest/httpx não garantem thread-safety num cliente compartilhado.

Status do voter_records (check constraint do banco):
    pending → enriching → ready_tse → checking → done
                                    ↘ error

`attempts` é incrementado ao marcar checking/error (conta de tentativas TSE).
"""

import threading
from datetime import datetime, timezone

from supabase import Client, ClientOptions, create_client

from config_worker import ConfigWorker, load_config

TABELA = "voter_records"
TABELA_LOTES = "batches"

STATUS_ABERTOS = ("pending", "enriching", "ready_tse", "checking")
TAMANHO_CHUNK = 200

_cfg: "ConfigWorker | None" = None
_cliente: "Client | None" = None
_tls = threading.local()


def _config() -> ConfigWorker:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def conectar() -> Client:
    """Cliente Supabase (service_role) com timeout generoso."""
    cfg = _config()
    return create_client(
        cfg.supabase_url,
        cfg.supabase_service_key,
        options=ClientOptions(postgrest_client_timeout=120),
    )


def cliente() -> Client:
    """Cliente compartilhado do processo (uso sequencial — Fase B / main)."""
    global _cliente
    if _cliente is None:
        _cliente = conectar()
    return _cliente


def cliente_threadlocal() -> Client:
    """Cliente PRÓPRIO da thread chamadora (cacheado). Use em ThreadPoolExecutor."""
    c = getattr(_tls, "cliente", None)
    if c is None:
        c = conectar()
        _tls.cliente = c
    return c


def _em_chunks(seq, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ─────────────────────────────────────────────
# LEITURA
# ─────────────────────────────────────────────
def pegar_pendentes_enriquecimento(limite: int = 500, sb: "Client | None" = None) -> list[dict]:
    """Registros status='pending' (id, cpf, batch_id + campos já existentes)."""
    sb = sb or cliente()
    r = (sb.table(TABELA)
         .select("id,cpf,batch_id,nome,nome_mae,data_nascimento")
         .eq("status", "pending")
         .limit(limite)
         .execute())
    return r.data or []


def pegar_prontos_tse(limite: int = 200, sb: "Client | None" = None) -> list[dict]:
    """Registros status='ready_tse' (já enriquecidos ou no modo CPF-only)."""
    sb = sb or cliente()
    r = (sb.table(TABELA)
         .select("id,cpf,batch_id,nome_mae,data_nascimento,attempts")
         .eq("status", "ready_tse")
         .limit(limite)
         .execute())
    return r.data or []


def contar_pendentes(sb: "Client | None" = None) -> int:
    """Total de registros ainda abertos (qualquer status não-terminal)."""
    sb = sb or cliente()
    r = (sb.table(TABELA)
         .select("id", count="exact")
         .in_("status", list(STATUS_ABERTOS))
         .execute())
    return r.count or 0


# ─────────────────────────────────────────────
# MARCAÇÃO DE STATUS
# ─────────────────────────────────────────────
def marcar(id_: int, status: str, sb: "Client | None" = None) -> None:
    """Marca 1 registro. Incrementa attempts em checking/error."""
    marcar_lote([id_], status, sb=sb)


def marcar_lote(ids: list[int], status: str, sb: "Client | None" = None) -> int:
    """Marca N registros em updates em chunks. Retorna qtd atualizada."""
    if not ids:
        return 0
    sb = sb or cliente()
    incrementa = status in ("checking", "error")
    atualizados = 0
    for chunk in _em_chunks(list(ids), TAMANHO_CHUNK):
        if incrementa:
            # PostgREST não faz attempts=attempts+1 direto em lote: leio e somo,
            # atualizando id a id (payload é por registro).
            rows = (sb.table(TABELA).select("id,attempts").in_("id", chunk).execute()).data or []
            tentativas = {r["id"]: (r.get("attempts") or 0) + 1 for r in rows}
            for i in chunk:
                (sb.table(TABELA)
                 .update({"status": status, "attempts": tentativas.get(i, 1)})
                 .eq("id", i)
                 .execute())
                atualizados += 1
        else:
            (sb.table(TABELA)
             .update({"status": status})
             .in_("id", chunk)
             .execute())
            atualizados += len(chunk)
    print(f"  💾 repo: {atualizados} registro(s) → status='{status}'"
          + (" (attempts+1)" if incrementa else ""))
    return atualizados


# ─────────────────────────────────────────────
# FASE A — gravação do enriquecimento
# ─────────────────────────────────────────────
def gravar_enriquecimento(id_: int, dados: dict, sb: "Client | None" = None) -> None:
    """
    Persiste o que a cascata (API B → Hashiro → DataSintese) trouxe.

    `dados`: {nome_mae, data_nascimento, nome, titulo_eleitor} (todos opcionais).
    Status vai a 'ready_tse' SEMPRE — com mãe+data o cliente TSE escolhe a
    consulta de TÍTULO; sem eles, a de SITUAÇÃO (só CPF). A decisão fica no
    tse_client, não aqui.

    ⚠️ O update SEMPRE acontece (mesmo com `dados` vazio): o registro entrou
    nesta função marcado 'enriching' e PRECISA sair de lá — sem dados, vai a
    'ready_tse' mesmo assim pra fase B fazer a consulta CPF-only. Guardar o
    update deixava registros travados em 'enriching' pra sempre.
    """
    sb = sb or cliente_threadlocal()
    payload: dict = {"status": "ready_tse"}
    if dados.get("nome_mae"):
        payload["nome_mae"] = dados["nome_mae"]
    if dados.get("data_nascimento"):
        payload["data_nascimento"] = dados["data_nascimento"]
    if dados.get("nome"):
        payload["nome"] = dados["nome"]
    titulo = (dados.get("titulo_eleitor") or "")
    if len(titulo) >= 10:
        payload["titulo_eleitoral"] = titulo
    sb.table(TABELA).update(payload).eq("id", id_).execute()


# ─────────────────────────────────────────────
# FASE B — gravação do resultado TSE
# ─────────────────────────────────────────────
def mapear_elegibilidade(situacao_texto: str) -> str:
    """Converte o texto de situação do TSE para o enum do banco.
    (Mesma regra de export_vps/supabase_repo.py:mapear_elegibilidade.)"""
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


def gravar_resultado_tse(id_: int, resultado: dict, raw_text: str,
                         sb: "Client | None" = None) -> None:
    """
    Mapeia a saída do parser (tse_extracao_texto.parse_resultado_texto) pras
    colunas de voter_records e fecha o registro (status='done').
    """
    sb = sb or cliente()
    payload: dict = {
        "status": "done",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "raw_text": raw_text,
    }
    if resultado.get("situacao"):
        payload["elegibilidade"] = mapear_elegibilidade(resultado["situacao"])
    else:
        payload["elegibilidade"] = "regularizar_tse"
    mapeamento = {
        "titulo_eleitoral": "titulo_eleitoral",
        "zona": "zona_eleitoral",
        "secao": "secao_eleitoral",
        "municipio": "municipio_votacao",
        "uf": "uf",
        "local_votacao": "local_votacao",
        "endereco_votacao": "endereco_votacao",
        "bairro": "bairro_votacao",
        "biometria": "biometria",
        "obrigacao_eleitoral": "obrigacao_eleitoral",
        "motivo_situacao": "motivo_situacao",
        "ano_situacao": "ano_situacao",
    }
    for chave_parser, coluna in mapeamento.items():
        if resultado.get(chave_parser):
            payload[coluna] = resultado[chave_parser]

    # ── Best-effort: nome/data que vieram do próprio TSE (onde-votar devolve
    # nomeCivil/dataNascimento) preenchem as colunas SÓ se estiverem null no
    # banco — nunca sobrescreve o que o enriquecimento já gravou. ──
    if resultado.get("nome") or resultado.get("data_nascimento"):
        try:
            row = (sb.table(TABELA).select("nome,data_nascimento")
                   .eq("id", id_).execute()).data or []
            atual = row[0] if row else {}
            if resultado.get("nome") and not (atual.get("nome") or "").strip():
                payload["nome"] = resultado["nome"]
            if resultado.get("data_nascimento") and not (atual.get("data_nascimento") or "").strip():
                payload["data_nascimento"] = resultado["data_nascimento"]
        except Exception:
            pass  # é enriquecimento best-effort — não pode derrubar a gravação

    sb.table(TABELA).update(payload).eq("id", id_).execute()


# ─────────────────────────────────────────────
# LOTES (batches)
# ─────────────────────────────────────────────
def atualizar_batches(sb: "Client | None" = None) -> list[str]:
    """
    Lotes com status='processing' e ZERO registros abertos
    (pending/enriching/ready_tse/checking) → status='done', finished_at=agora.

    Retorna a lista de batch_ids finalizados nesta chamada.
    """
    sb = sb or cliente()
    r = sb.table(TABELA_LOTES).select("id").eq("status", "processing").execute()
    lotes = r.data or []
    if not lotes:
        return []
    finalizados: list[str] = []
    for lote in lotes:
        bid = lote["id"]
        c = (sb.table(TABELA)
             .select("id", count="exact")
             .eq("batch_id", bid)
             .in_("status", list(STATUS_ABERTOS))
             .execute())
        abertos = c.count or 0
        if abertos == 0:
            (sb.table(TABELA_LOTES)
             .update({"status": "done",
                      "finished_at": datetime.now(timezone.utc).isoformat()})
             .eq("id", bid)
             .execute())
            finalizados.append(bid)
    if finalizados:
        print(f"  🏁 repo: {len(finalizados)} lote(s) finalizado(s): "
              + ", ".join(b[:8] for b in finalizados))
    return finalizados
