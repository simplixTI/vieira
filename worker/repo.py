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

import os
import threading
from datetime import datetime, timedelta, timezone

from supabase import Client, ClientOptions, create_client

from config_worker import ConfigWorker, load_config

TABELA = "voter_records"
TABELA_LOTES = "batches"

STATUS_ABERTOS = ("pending", "enriching", "ready_tse", "checking")
TAMANHO_CHUNK = 200

# Marca do lote "Consultas avulsas" (1 por usuário no portal — CPFs digitados um
# a um). Processados PRIMEIRO nas duas fases para o cliente ver o resultado em
# segundos. O lote nunca é fechado por `atualizar_batches`.
AVULSA_FILENAME = "__avulsas__"

# ── Re-tentativa de erros transientes ──
# records status='error' ficam de fora por RETRY_COOLDOWN_MIN minutos após o
# updated_at (mantido por trigger no banco) e reentram na fila enquanto
# attempts < MAX_TENTATIVAS; ao atingir MAX, 'error' é terminal.
RETRY_COOLDOWN_MIN = int(os.environ.get("RETRY_COOLDOWN_MIN", "30") or "30")
MAX_TENTATIVAS = 5

# ── Teto diário de consultas TSE por cliente (batches.user_id) ──
# Cada dono de lote consome no máximo N consultas TSE por dia; registros além
# do teto ficam na fila (ready_tse/pending) e continuam sozinhos amanhã.
# <= 0 = ilimitado. Vale só pra FASE B (TSE); enriquecimento não consome teto.
LIMITE_DIARIO_POR_CLIENTE = int(os.environ.get("LIMITE_DIARIO_POR_CLIENTE", "6000") or "6000")

# ── Teto MENSAL por cliente (camada por cima do diário) ──
# Ao atingir, o cliente para até liberação comercial e o admin recebe UM aviso
# por mês (tabela avisos_limite — ver avisar_limite_mensal). <= 0 = ilimitado.
LIMITE_MENSAL_POR_CLIENTE = int(os.environ.get("LIMITE_MENSAL_POR_CLIENTE", "50000") or "50000")

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
def _ids_lotes_avulsas(sb: "Client | None" = None) -> list[str]:
    """batch_ids de todos os lotes 'Consultas avulsas' (1 por usuário)."""
    sb = sb or cliente()
    r = (sb.table(TABELA_LOTES).select("id")
         .eq("filename", AVULSA_FILENAME).execute())
    return [b["id"] for b in (r.data or [])]


def pegar_pendentes_enriquecimento(limite: int = 500, sb: "Client | None" = None) -> list[dict]:
    """Registros status='pending' (id, cpf, batch_id + campos já existentes).

    Avulsas primeiro (cliente digitou 1 CPF e está aguardando); depois o resto.
    """
    sb = sb or cliente()
    cols = "id,cpf,batch_id,nome,nome_mae,data_nascimento"
    avulsa_ids = _ids_lotes_avulsas(sb)

    prio: list[dict] = []
    if avulsa_ids:
        prio = (sb.table(TABELA).select(cols)
                .eq("status", "pending")
                .in_("batch_id", avulsa_ids)
                .order("id")
                .limit(limite)
                .execute()).data or []
        if prio:
            print(f"  ⚡ repo: {len(prio)} avulsa(s) pendente(s) — processando primeiro")

    restante = limite - len(prio)
    if restante <= 0:
        return prio
    q = sb.table(TABELA).select(cols).eq("status", "pending")
    if avulsa_ids:
        # excluir os já pegos acima
        pegos = [r["id"] for r in prio]
        if pegos:
            q = q.not_.in_("id", pegos)
    resto = (q.limit(restante).execute()).data or []
    return prio + resto


def pegar_prontos_tse(limite: int = 200, sb: "Client | None" = None) -> list[dict]:
    """
    Registros a consultar no TSE, em duas fatias (merge determinístico):
      1. status='ready_tse' (já enriquecidos ou modo CPF-only), por id;
      2. se sobrar limite: status='error' ELEGÍVEIS p/ re-tentativa —
         updated_at mais velho que RETRY_COOLDOWN_MIN (a marcação de error
         refresha updated_at via trigger, então um erro recém-criado fica de
         fora — sem loop infinito) E attempts < MAX_TENTATIVAS, do mais
         antigo pro mais novo.

    Registros da fatia 2 vêm com a chave auxiliar `_retry=True` (main.py
    loga como re-tentativa). Quando attempts atinge MAX_TENTATIVAS, 'error'
    é terminal: nada aqui os pega de volta.
    """
    sb = sb or cliente()
    limite = max(1, int(limite or 1))
    cols = "id,cpf,batch_id,nome_mae,data_nascimento,attempts"

    # Avulsas primeiro (independente do id) — o cliente está aguardando.
    avulsa_ids = _ids_lotes_avulsas(sb)
    prio_avulsa: list[dict] = []
    if avulsa_ids:
        prio_avulsa = (sb.table(TABELA).select(cols)
                       .eq("status", "ready_tse")
                       .in_("batch_id", avulsa_ids)
                       .order("id")
                       .limit(limite)
                       .execute()).data or []
        if prio_avulsa:
            print(f"  ⚡ repo: {len(prio_avulsa)} avulsa(s) prontas p/ TSE — primeiro")

    resto_limite = limite - len(prio_avulsa)
    prontos_regulares: list[dict] = []
    if resto_limite > 0:
        q = (sb.table(TABELA).select(cols)
             .eq("status", "ready_tse")
             .order("id")
             .limit(resto_limite))
        if prio_avulsa:
            q = q.not_.in_("id", [r["id"] for r in prio_avulsa])
        prontos_regulares = (q.execute()).data or []

    prontos = prio_avulsa + prontos_regulares

    retries: list[dict] = []
    restante = limite - len(prontos)
    if restante > 0:
        corte = (datetime.now(timezone.utc) - timedelta(minutes=RETRY_COOLDOWN_MIN)).isoformat()
        retries = (sb.table(TABELA)
                   .select(cols)
                   .eq("status", "error")
                   .lt("updated_at", corte)
                   .lt("attempts", MAX_TENTATIVAS)
                   .order("updated_at")
                   .limit(restante)
                   .execute()).data or []
        if retries:
            print(f"  ♻️  repo: {len(retries)} re-tentativa(s) de erro na fila "
                  f"(cooldown {RETRY_COOLDOWN_MIN}min · attempts<{MAX_TENTATIVAS})")

    for r in retries:
        r["_retry"] = True
    return prontos + retries


# ─────────────────────────────────────────────
# TETO DIÁRIO/MENSAL DE CONSULTAS TSE POR CLIENTE
# ─────────────────────────────────────────────
def mapa_lotes_donos(sb: "Client | None" = None) -> dict:
    """batch_id → batches.user_id (donos dos lotes). 1 query; cachear por lote."""
    sb = sb or cliente()
    r = sb.table(TABELA_LOTES).select("id,user_id").execute()
    return {b["id"]: b.get("user_id") for b in (r.data or [])}


def _meia_noite_local() -> datetime:
    return datetime.now().astimezone().replace(hour=0, minute=0,
                                               second=0, microsecond=0)


def _consultas_desde(sb, inicio_iso: str, mapa: dict) -> dict:
    """
    Contagem de consultas TSE consumidas desde `inicio_iso`, por dono de lote.
    Regra (aproximação de negócio, não contabilidade) — um record conta se:
      • checked_at >= inicio (virou 'done' no período), OU
      • status='error' E updated_at >= inicio (falhou no período — o erro
        também consumiu uma consulta).
    'ready_tse'/'pending' mexidos no período NÃO contam (enriquecimento não
    consome consulta TSE).
    """
    feitos = (sb.table(TABELA)
              .select("id,batch_id")
              .gte("checked_at", inicio_iso)
              .execute()).data or []
    falhos = (sb.table(TABELA)
              .select("id,batch_id")
              .eq("status", "error")
              .gte("updated_at", inicio_iso)
              .execute()).data or []
    contagem: dict = {}
    for row in feitos + falhos:
        dono = mapa.get(row.get("batch_id"))
        if dono is not None:
            contagem[dono] = contagem.get(dono, 0) + 1
    return contagem


def consultas_hoje_por_cliente(sb: "Client | None" = None,
                               mapa: "dict | None" = None) -> dict:
    """Consultas TSE consumidas HOJE (meia-noite local) por cliente."""
    sb = sb or cliente()
    mapa = mapa or mapa_lotes_donos(sb)
    return _consultas_desde(sb, _meia_noite_local().isoformat(), mapa)


def consultas_mes_por_cliente(sb: "Client | None" = None,
                              mapa: "dict | None" = None) -> dict:
    """Consultas TSE consumidas no MÊS CORRENTE (1º dia, meia-noite local)."""
    sb = sb or cliente()
    mapa = mapa or mapa_lotes_donos(sb)
    inicio = _meia_noite_local().replace(day=1).isoformat()
    return _consultas_desde(sb, inicio, mapa)


# ── Aviso de limite mensal (1 por cliente por mês) ──
TABELA_AVISOS = "avisos_limite"


def mes_atual() -> str:
    """Primeiro dia do mês corrente (coluna `mes` date da tabela avisos_limite)."""
    return datetime.now().astimezone().strftime("%Y-%m-01")


def avisar_limite_mensal(user_id: str, limite: int,
                         sb: "Client | None" = None) -> bool:
    """
    Marca que o cliente atingiu o limite mensal. Retorna True se o aviso é
    NOVO (caller deve notificar o admin); False se (user_id, mês) já consta —
    assim o webhook dispara no máximo 1x por cliente por mês, mesmo com várias
    execuções do worker. 'Reset' mensal = novo mês (ou apagar a linha / subir
    o LIMITE_MENSAL_POR_CLIENTE).
    """
    sb = sb or cliente()
    mes = mes_atual()
    ex = (sb.table(TABELA_AVISOS)
          .select("user_id")
          .eq("user_id", user_id)
          .eq("mes", mes)
          .execute()).data or []
    if ex:
        return False
    sb.table(TABELA_AVISOS).insert(
        {"user_id": user_id, "mes": mes, "limite": limite}).execute()
    return True


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
    r = (sb.table(TABELA_LOTES).select("id,filename")
         .eq("status", "processing").execute())
    lotes = r.data or []
    if not lotes:
        return []
    finalizados: list[str] = []
    for lote in lotes:
        # Lote de "Consultas avulsas" NUNCA fecha — o cliente pode digitar
        # mais CPFs a qualquer momento; deixar em 'processing' mantém o
        # auto-refresh do portal ativo.
        if (lote.get("filename") or "") == AVULSA_FILENAME:
            continue
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
