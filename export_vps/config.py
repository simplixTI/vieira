"""
config.py — carrega configuração do .env e valida o que é obrigatório.

Esta edição ADB + captcha MANUAL não usa 2captcha/NopeCHA/AdsPower/proxy,
então a config é enxuta: Supabase, APIs de enriquecimento e parâmetros do ADB.
"""

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    # python-dotenv é opcional; se não estiver instalado, lemos só do ambiente
    pass


def _req(nome: str) -> str:
    """Lê var obrigatória. Falha cedo, com mensagem clara, se faltar."""
    v = os.environ.get(nome, "").strip()
    if not v:
        raise RuntimeError(
            f"Variável de ambiente obrigatória ausente: {nome}. "
            f"Defina no arquivo .env (veja .env.example)."
        )
    return v


def _opt(nome: str, default: str = "") -> str:
    return os.environ.get(nome, default).strip()


def _bool(nome: str, default: bool = False) -> bool:
    v = os.environ.get(nome, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on", "sim")


@dataclass(frozen=True)
class Config:
    # ── Supabase ──
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str

    # ── APIs de enriquecimento (cascata A→B→C) ──
    HASHIRO_TOKEN: str
    API_B_KEY: str
    DATASINTESE_USER: str
    DATASINTESE_PASS: str

    # ── ADB / Chrome no celular ──
    # Pacote do Chrome no Android (com.android.chrome = Chrome estável).
    ANDROID_CHROME_PACKAGE: str
    # Serial do device (saída de `adb devices`). Vazio = usa o único conectado.
    ADB_DEVICE_SERIAL: str
    # Caminho do adb.exe. Vazio = assume que está no PATH.
    ADB_PATH: str
    # Caminho de um chromedriver fixo (opcional). Vazio = auto (selenium-manager
    # / webdriver-manager tenta resolver a versão compatível com o Chrome do device).
    CHROMEDRIVER_PATH: str


def load_config() -> Config:
    return Config(
        SUPABASE_URL=_req("SUPABASE_URL"),
        SUPABASE_SERVICE_KEY=_req("SUPABASE_SERVICE_KEY"),
        # APIs de enriquecimento podem ficar vazias se você desligar a etapa
        HASHIRO_TOKEN=_opt("HASHIRO_TOKEN"),
        API_B_KEY=_opt("API_B_KEY"),
        DATASINTESE_USER=_opt("DATASINTESE_USER"),
        DATASINTESE_PASS=_opt("DATASINTESE_PASS"),
        ANDROID_CHROME_PACKAGE=_opt("ANDROID_CHROME_PACKAGE", "com.android.chrome"),
        ADB_DEVICE_SERIAL=_opt("ADB_DEVICE_SERIAL"),
        ADB_PATH=_opt("ADB_PATH", "adb"),
        CHROMEDRIVER_PATH=_opt("CHROMEDRIVER_PATH"),
    )
