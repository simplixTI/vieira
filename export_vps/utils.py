"""
utils.py — helpers compartilhados: cores no terminal, normalização de datas,
heurísticas de nome, screenshots e contadores de estatística (thread-safe).

Estes helpers são independentes do browser e foram portados/adaptados do
scraper desktop original.
"""

import os
import re
import threading
from datetime import datetime, date

# ─────────────────────────────────────────────
# CORES NO TERMINAL (Windows compatível — chame os.system("color") no main)
# ─────────────────────────────────────────────
def cor(texto, codigo):
    return f"\033[{codigo}m{texto}\033[0m"

VERDE    = lambda t: cor(t, "92")
AMARELO  = lambda t: cor(t, "93")
VERMELHO = lambda t: cor(t, "91")
AZUL     = lambda t: cor(t, "94")
NEGRITO  = lambda t: cor(t, "1")

# ─────────────────────────────────────────────
# SCREENSHOTS / DEBUG
# ─────────────────────────────────────────────
PASTA_PRINTS = os.environ.get("PASTA_PRINTS", "prints")


def tirar_print(driver, nome: str) -> None:
    """Salva screenshot do device em PASTA_PRINTS/HHMMSS_<nome>.png. Nunca lança."""
    try:
        os.makedirs(PASTA_PRINTS, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        caminho = os.path.join(PASTA_PRINTS, f"{ts}_{nome}.png")
        driver.save_screenshot(caminho)
    except Exception:
        # screenshot é só debug — nunca deve derrubar a consulta
        pass


# ─────────────────────────────────────────────
# DATAS
# ─────────────────────────────────────────────
def normalizar_data_br(valor) -> str | None:
    """
    Normaliza QUALQUER formato de data para 'DD/MM/AAAA' (formato brasileiro).
    Aceita date/datetime, ISO 'YYYY-MM-DD', BR 'DD/MM/AAAA', 'DD-MM-AAAA',
    'DDMMYYYY' e 'YYYYMMDD'. Retorna None se não conseguir normalizar.
    """
    if valor is None:
        return None
    if isinstance(valor, date):
        return valor.strftime("%d/%m/%Y")
    if not isinstance(valor, str):
        valor = str(valor)

    digits = re.sub(r"\D", "", valor)
    if len(digits) != 8:
        return None

    try:
        ano_inicio = int(digits[0:4])
        ano_fim = int(digits[4:8])
    except Exception:
        return None

    if 1900 <= ano_inicio <= 2100 and not (1900 <= ano_fim <= 2100):
        yyyy, mm, dd = digits[0:4], digits[4:6], digits[6:8]   # YYYYMMDD
    elif 1900 <= ano_fim <= 2100:
        dd, mm, yyyy = digits[0:2], digits[2:4], digits[4:8]   # DDMMYYYY
    else:
        return None

    try:
        dd_i, mm_i, yyyy_i = int(dd), int(mm), int(yyyy)
        if not (1 <= dd_i <= 31 and 1 <= mm_i <= 12 and 1900 <= yyyy_i <= 2100):
            return None
    except Exception:
        return None

    return f"{dd}/{mm}/{yyyy}"


def converter_data_datasintese(valor) -> str | None:
    """Converte 'DD-MM-YYYY' (DataSintese) para 'YYYY-MM-DD' (Supabase)."""
    if not valor:
        return None
    try:
        partes = valor.split("-")
        if len(partes) == 3 and len(partes[0]) == 2:  # DD-MM-YYYY
            return f"{partes[2]}-{partes[1]}-{partes[0]}"
        return valor
    except Exception:
        return None


# ─────────────────────────────────────────────
# HEURÍSTICAS DE NOME
# ─────────────────────────────────────────────
def tem_nome_abreviado(nome: str) -> bool:
    """Detecta iniciais soltas (abreviações). 'MARIA DA C RIBEIRO' → True."""
    if not nome:
        return False
    ligacoes = {"E", "DE", "DA", "DO", "DAS", "DOS"}
    for palavra in nome.upper().split():
        p = palavra.rstrip(".")
        if len(p) == 1 and p.isalpha() and p not in ligacoes:
            return True
    return False


def melhor_nome_mae(mae_atual: str | None, mae_novo: str | None) -> tuple[str | None, str]:
    """
    Decide se `mae_novo` é melhor que `mae_atual`. Retorna (vencedor, motivo).
    Regras: sem-abreviação > abreviado; depois mais longo (margem > 2 chars).
    """
    a = (mae_atual or "").strip()
    b = (mae_novo or "").strip()
    if not a and b:
        return (b, "preencheu_vazio")
    if a and not b:
        return (a, "manteve (novo vazio)")
    if not a and not b:
        return (None, "ambos vazios")

    abrev_a = tem_nome_abreviado(a)
    abrev_b = tem_nome_abreviado(b)
    if abrev_a and not abrev_b:
        return (b, "desabreviou")
    if not abrev_a and abrev_b:
        return (a, "manteve (novo abreviado)")
    if len(b) > len(a) + 2:
        return (b, f"mais_longo ({len(b)}>{len(a)})")
    if len(a) > len(b) + 2:
        return (a, "manteve (atual mais longo)")
    return (a, "equivalente")


# ─────────────────────────────────────────────
# ESTATÍSTICAS (thread-safe) — captcha + APIs
# ─────────────────────────────────────────────
_stats_lock = threading.Lock()
_captcha_stats = {"sucesso": 0, "falha": 0}
_api_stats: dict[str, dict[str, int]] = {}


def registrar_sucesso_captcha() -> None:
    with _stats_lock:
        _captcha_stats["sucesso"] += 1


def registrar_falha_captcha() -> None:
    with _stats_lock:
        _captcha_stats["falha"] += 1


def registrar_sucesso_api(nome: str) -> None:
    with _stats_lock:
        d = _api_stats.setdefault(nome, {"ok": 0, "fail": 0})
        d["ok"] += 1


def registrar_falha_api(nome: str, _msg: str = "") -> None:
    with _stats_lock:
        d = _api_stats.setdefault(nome, {"ok": 0, "fail": 0})
        d["fail"] += 1


def resumo_stats() -> str:
    with _stats_lock:
        partes = [f"captcha ok={_captcha_stats['sucesso']} falha={_captcha_stats['falha']}"]
        for api, d in _api_stats.items():
            partes.append(f"{api} ok={d['ok']} fail={d['fail']}")
    return " | ".join(partes)


# ─────────────────────────────────────────────
# NOTIFICAÇÃO (webhook opcional — Discord/Slack)
# ─────────────────────────────────────────────
def notificar(msg: str) -> None:
    """
    POST fire-and-forget pra URL em NOTIFY_WEBHOOK_URL (.env).
    Formato compatível com Discord e Slack. Nunca lança exceção — notificação
    é conveniência, não pode derrubar a consulta.
    """
    try:
        url = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
        if not url:
            return
        import requests
        requests.post(url, json={"content": msg, "text": msg}, timeout=5)
    except Exception:
        pass
