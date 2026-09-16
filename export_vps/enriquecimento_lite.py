"""
enriquecimento_lite.py — enriquecimento de nome_mae + data_nascimento ANTES do TSE.

Cascata (respeita flags de settings.py):
    PRINCIPAL: API B (200.9.155.126)
    FALLBACK 1: Hashiro (se HASHIRO_ENABLED)
    FALLBACK 2: DataSintese (se DATASINTESE_ENABLED)

Objetivo: preencher nome_mae/data_nascimento pra que a consulta de NÚMERO DO
TÍTULO fique disponível (traz mais dados). Sem mãe/data, o registro cai na
consulta de SITUAÇÃO (só CPF).

Usa as credenciais do .env via config.load_config():
    API_B_KEY, HASHIRO_TOKEN, DATASINTESE_USER, DATASINTESE_PASS
Se faltarem, a etapa correspondente é pulada com segurança.
"""

import re
import threading
import time

import requests

from config import load_config
from settings import (
    HASHIRO_ENABLED, HASHIRO_BASE_URL, HASHIRO_UA,
    API_B_ENABLED, DATASINTESE_ENABLED,
)

_cfg = load_config()

# ── Sessão HTTP por thread + retry de erros transitórios ──
# O enriquecimento roda em ThreadPoolExecutor: Session não é thread-safe,
# então cada thread tem a sua (reuso de conexão dentro da thread).
_tls = threading.local()


def _session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        _tls.session = s
    return s


def _request(method: str, url: str, tentativas: int = 2, **kw) -> requests.Response:
    """
    GET/POST com retry de erros TRANSITÓRIOS (timeout, conexão, 5xx, 429).
    Erros definitivos (4xx) retornam na hora pra camada de cima classificar.
    """
    ult_exc = None
    for tentativa in range(tentativas):
        try:
            r = _session().request(method, url, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                if tentativa + 1 < tentativas:
                    time.sleep(1.5 * (tentativa + 1))  # backoff simples
                    continue
            return r
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            ult_exc = e
            if tentativa + 1 < tentativas:
                time.sleep(1.5 * (tentativa + 1))
                continue
            raise
    raise ult_exc  # inalcançável, mas deixa o tipo claro

# API B — principal
API_B_URL_BASE = "https://consulta.tabajarachecks.vip/api/v1/pessoa"
API_B_KEY = _cfg.API_B_KEY

# Hashiro — fallback 1
HASHIRO_TOKEN = _cfg.HASHIRO_TOKEN

# DataSintese — fallback 2 (desabilitado por padrão hoje)
DATASINTESE_AUTH_URL = "https://api.datasintese.com/auth"
DATASINTESE_QUERY_URL = "https://api.datasintese.com/pfmaster"
DATASINTESE_USER = _cfg.DATASINTESE_USER
DATASINTESE_PASS = _cfg.DATASINTESE_PASS

_ds_token_cache = {"token": None, "obtido_em": 0.0}
_ds_token_lock = threading.Lock()


def _pick(d: dict, *keys):
    """Retorna o 1º valor não-vazio das chaves candidatas (case-insensitive)."""
    if not isinstance(d, dict):
        return None
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v not in (None, "", []):
            return v
    return None


def _norm_data_iso(valor):
    """Aceita 'YYYY-MM-DD', 'DD-MM-YYYY' ou 'DD/MM/YYYY' → 'YYYY-MM-DD'. None se não parsear."""
    if not valor:
        return None
    s = str(valor).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{2})[/-](\d{2})[/-](\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def consultar_hashiro(cpf: str) -> dict:
    """
    Hashiro (fallback 1). GET https://hashirosearch.squareweb.app/?token=X&cpf1=CPF.
    Retorna nome_mae, data_nascimento (YYYY-MM-DD), nome, titulo_eleitor.
    Faz parsing defensivo (chaves em maiúsculas/minúsculas, resposta como dict ou lista,
    com/sem nesting por CPF).
    """
    cpf_limpo = re.sub(r"\D", "", cpf or "")
    if len(cpf_limpo) != 11:
        return {"status": "erro", "erro": f"CPF inválido ({len(cpf_limpo)} díg)"}
    if not HASHIRO_TOKEN:
        return {"status": "erro", "erro": "HASHIRO_TOKEN ausente no .env"}
    try:
        r = _request(
            "GET",
            HASHIRO_BASE_URL,
            params={"token": HASHIRO_TOKEN, "cpf1": cpf_limpo},
            headers={"User-Agent": HASHIRO_UA},
            timeout=(5, 10),  # connect=5s, read=10s
        )
        if r.status_code in (401, 403):
            return {"status": "erro", "erro": f"Hashiro HTTP {r.status_code} (token inválido?)"}
        if r.status_code == 404:
            return {"status": "sem_dados", "erro": "Hashiro 404"}
        if r.status_code != 200:
            return {"status": "erro", "erro": f"Hashiro HTTP {r.status_code}"}
        try:
            data = r.json()
        except Exception:
            return {"status": "erro", "erro": f"Hashiro resposta não-JSON: {(r.text or '')[:200]}"}

        # Schema real: {"ok": bool, "result": {...} | [{...}] | {cpf: {...}}}
        # Também aceita: {cpf1: {...}} | {CPF_LIMPO: {...}} | [{...}] | {...}
        if isinstance(data, dict) and data.get("ok") is False:
            return {"status": "erro", "erro": f"Hashiro ok=false: {str(data.get('result') or data)[:200]}"}
        alvo = data
        # Alguns endpoints devolvem {"ok":true,"result":{"result":{...}}} — descende até 3x
        for _ in range(3):
            if isinstance(alvo, dict) and set(alvo.keys()) == {"result"}:
                alvo = alvo["result"]
                continue
            if isinstance(alvo, dict) and "result" in alvo and not any(
                k in alvo for k in ("nome", "nome_mae", "data_nascimento", "cpf")
            ):
                alvo = alvo["result"]
                continue
            break
        if isinstance(alvo, dict) and cpf_limpo in alvo and isinstance(alvo[cpf_limpo], dict):
            alvo = alvo[cpf_limpo]
        elif isinstance(alvo, dict) and "cpf1" in alvo and isinstance(alvo["cpf1"], dict):
            alvo = alvo["cpf1"]
        elif isinstance(alvo, list) and alvo:
            alvo = alvo[0] if isinstance(alvo[0], dict) else {}

        if not isinstance(alvo, dict):
            return {"status": "sem_dados", "erro": f"Hashiro resposta inesperada: {str(data)[:200]}"}

        nome_mae = _pick(alvo, "nome_mae", "mae", "nome_da_mae", "mother", "mother_name")
        nome = _pick(alvo, "nome", "name", "nome_completo")
        dt_raw = _pick(alvo, "data_nascimento", "nascimento", "dt_nascimento", "birthday", "dob")
        titulo = _pick(alvo, "titulo_eleitor", "titulo", "titulo_eleitoral")

        nome_mae = (str(nome_mae).strip() or None) if nome_mae else None
        nome = (str(nome).strip() or None) if nome else None
        dt_iso = _norm_data_iso(dt_raw)
        if titulo:
            titulo = re.sub(r"\D", "", str(titulo)) or None

        if not nome_mae and not dt_iso and not titulo:
            return {"status": "sem_dados", "erro": f"Hashiro sem mae/data/titulo. Chaves: {list(alvo.keys())[:10]}"}
        return {"status": "enriquecido", "nome_mae": nome_mae,
                "data_nascimento": dt_iso, "nome": nome, "titulo_eleitor": titulo}
    except Exception as e:
        return {"status": "erro", "erro": f"Hashiro: {type(e).__name__}: {e}"}


def _converter_data_ds(valor):
    """'DD-MM-YYYY' (DataSintese) → 'YYYY-MM-DD'."""
    if not valor:
        return None
    try:
        p = valor.split("-")
        if len(p) == 3 and len(p[0]) == 2:
            return f"{p[2]}-{p[1]}-{p[0]}"
        return valor
    except Exception:
        return None


def consultar_api_b(cpf: str) -> dict:
    """API B (principal). Retorna nome_mae, data_nascimento (YYYY-MM-DD), nome, titulo_eleitor."""
    cpf_limpo = re.sub(r"\D", "", cpf or "")
    if len(cpf_limpo) != 11:
        return {"status": "erro", "erro": f"CPF inválido ({len(cpf_limpo)} díg)"}
    if not API_B_KEY:
        return {"status": "erro", "erro": "API_B_KEY ausente no .env"}
    try:
        url = f"{API_B_URL_BASE}/{cpf_limpo}?key={API_B_KEY}"
        r = _request("GET", url, timeout=20)
        if r.status_code == 404:
            return {"status": "sem_dados", "erro": "API B 404"}
        if r.status_code == 401:
            return {"status": "erro", "erro": "API B 401 (key inválida)"}
        if r.status_code != 200:
            return {"status": "erro", "erro": f"API B HTTP {r.status_code}"}
        data = r.json()
        if not isinstance(data, dict):
            return {"status": "sem_dados", "erro": "API B resposta inesperada"}
        nome_mae = (data.get("nome_mae") or "").strip() or None
        nome = (data.get("nome") or "").strip() or None
        dt_nasc = data.get("data_nascimento")  # já YYYY-MM-DD
        titulo = (data.get("titulo_eleitor") or "").strip() or None
        if titulo:
            titulo = re.sub(r"\D", "", titulo) or None
        if not nome_mae and not dt_nasc and not titulo:
            return {"status": "sem_dados", "erro": "API B sem mae/data/titulo"}
        return {"status": "enriquecido", "nome_mae": nome_mae,
                "data_nascimento": dt_nasc, "nome": nome, "titulo_eleitor": titulo}
    except Exception as e:
        return {"status": "erro", "erro": f"API B: {type(e).__name__}: {e}"}


def _ds_auth() -> str:
    agora = time.time()
    if _ds_token_cache["token"] and (agora - _ds_token_cache["obtido_em"]) < 3000:
        return _ds_token_cache["token"]
    # lock: sem ele, as threads do enriquecimento autenticariam em rajada
    with _ds_token_lock:
        agora = time.time()
        if _ds_token_cache["token"] and (agora - _ds_token_cache["obtido_em"]) < 3000:
            return _ds_token_cache["token"]
        r = _request("POST", DATASINTESE_AUTH_URL,
                     json={"user": DATASINTESE_USER, "password": DATASINTESE_PASS},
                     timeout=20)
        r.raise_for_status()
        token = r.json().get("token")
        if not token:
            raise RuntimeError(f"DataSintese auth sem token: {r.text[:200]}")
        _ds_token_cache.update({"token": token, "obtido_em": agora})
        return token


def consultar_datasintese(cpf: str) -> dict:
    """DataSintese (fallback). Retorna nome_mae, data_nascimento (YYYY-MM-DD), nome."""
    cpf_limpo = re.sub(r"\D", "", cpf or "")
    if len(cpf_limpo) != 11:
        return {"status": "erro", "erro": f"CPF inválido ({len(cpf_limpo)} díg)"}
    if not (DATASINTESE_USER and DATASINTESE_PASS):
        return {"status": "erro", "erro": "DataSintese sem credenciais no .env"}
    try:
        token = _ds_auth()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        r = _request("POST", DATASINTESE_QUERY_URL, headers=headers, json={"cpf": cpf_limpo}, timeout=30)
        if r.status_code != 200:
            low = (r.text or "").lower()
            sem = ("dados não encontrados", "dados nao encontrados", "bloqueado para divul",
                   "não localizado", "nao localizado")
            if r.status_code == 451 or any(m in low for m in sem):
                return {"status": "sem_dados", "erro": f"HTTP {r.status_code}"}
            return {"status": "erro", "erro": f"HTTP {r.status_code}: {(r.text or '')[:200]}"}
        data = r.json()
        if isinstance(data, list) and data:
            data = data[0]
        pessoas = data.get("sintese", {}).get("pessoa") or []
        if not pessoas:
            return {"status": "sem_dados", "erro": "pessoa[] vazio"}
        p = pessoas[0]
        nome_mae = (p.get("nome_mae") or "").strip() or None
        dt = _converter_data_ds(p.get("data_nascimento"))
        if not nome_mae and not dt:
            return {"status": "sem_dados", "erro": "DataSintese sem mae/data"}
        return {"status": "enriquecido", "nome_mae": nome_mae,
                "data_nascimento": dt, "nome": (p.get("nome") or "").strip() or None}
    except Exception as e:
        return {"status": "erro", "erro": f"DataSintese: {type(e).__name__}: {e}"}


def enriquecer_cpf(cpf: str, precisa_mae: bool = True, precisa_data: bool = True) -> dict:
    """
    Cascata (respeita flags de settings.py):
        API B (principal) → Hashiro (fallback, se HASHIRO_ENABLED)
                         → DataSintese (fallback, se DATASINTESE_ENABLED)
    Só chama fallbacks se o passo anterior não cobrir o que falta.

    Retorna: {nome_mae, data_nascimento, titulo_eleitor, nome, origem(list), erros(list)}
    """
    out = {"nome_mae": None, "data_nascimento": None, "titulo_eleitor": None,
           "nome": None, "origem": [], "erros": []}

    def _falta():
        return (precisa_mae and not out["nome_mae"]) or (precisa_data and not out["data_nascimento"])

    def _absorver(src: dict, tag_mae: str, tag_data: str, tag_titulo: str = "", tag_nome: str = ""):
        falta_mae = precisa_mae and not out["nome_mae"]
        falta_data = precisa_data and not out["data_nascimento"]
        if falta_mae and src.get("nome_mae"):
            out["nome_mae"] = src["nome_mae"]
            out["origem"].append(tag_mae)
        if falta_data and src.get("data_nascimento"):
            out["data_nascimento"] = src["data_nascimento"]
            out["origem"].append(tag_data)
        if not out["titulo_eleitor"] and src.get("titulo_eleitor"):
            out["titulo_eleitor"] = src["titulo_eleitor"]
            if tag_titulo:
                out["origem"].append(tag_titulo)
        if not out["nome"] and src.get("nome"):
            out["nome"] = src["nome"]
            if tag_nome:
                out["origem"].append(tag_nome)

    # ── PRINCIPAL: API B ──
    if API_B_ENABLED:
        rb = consultar_api_b(cpf)
        if rb.get("status") == "enriquecido":
            _absorver(rb, "API-B(mae)", "API-B(data)", "API-B(titulo)", "API-B(nome)")
        else:
            out["erros"].append(f"API-B:{rb.get('erro')}")

    # ── FALLBACK 1: Hashiro ──
    if HASHIRO_ENABLED and _falta():
        rh = consultar_hashiro(cpf)
        if rh.get("status") == "enriquecido":
            _absorver(rh, "Hashiro(mae)", "Hashiro(data)", "Hashiro(titulo)", "Hashiro(nome)")
        else:
            out["erros"].append(f"Hashiro:{rh.get('erro')}")

    # ── FALLBACK 2: DataSintese (só se enabled) ──
    if DATASINTESE_ENABLED and _falta():
        rd = consultar_datasintese(cpf)
        if rd.get("status") == "enriquecido":
            _absorver(rd, "DataSintese(mae)", "DataSintese(data)", "", "DataSintese(nome)")
        else:
            out["erros"].append(f"DataSintese:{rd.get('erro')}")

    return out
