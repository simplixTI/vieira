"""
config_worker.py — configuração do worker do portal (fila batches/voter_records).

Carrega DOIS arquivos de ambiente, com caminhos explícitos relativos à raiz do
repo (não depende do cwd):

  1. `.env.portal` (raiz do repo)  → SUPABASE_URL, SUPABASE_SERVICE_KEY
                                     (projeto NOVO do portal)
  2. `export_vps/.env`             → API_B_KEY, HASHIRO_TOKEN,
                                     DATASINTESE_USER/PASS, NOTIFY_WEBHOOK_URL
                                     (reaproveitado do export_vps)

Carregados com override=False: quem carrega primeiro tem prioridade. Por isso
`.env.portal` entra primeiro. IMPORTANTE: SUPABASE_URL/SUPABASE_SERVICE_KEY são
lidos EXCLUSIVAMENTE do .env.portal (lidos via dotenv_values, sem fallback pro
.env antigo) — o export_vps/.env tem chaves de OUTRO projeto Supabase e ser
usado aqui apontaria o worker pro banco errado.

Nunca imprima os valores das chaves — apenas carregue.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Raiz do repo = pasta pai de worker/
ROOT = Path(__file__).resolve().parent.parent
EXPORT_VPS = ROOT / "export_vps"


def carregar_envs() -> None:
    """Carrega .env.portal (se existir) e export_vps/.env no ambiente."""
    portal = ROOT / ".env.portal"
    if portal.exists():
        load_dotenv(portal, override=False)
    antigo = EXPORT_VPS / ".env"
    if antigo.exists():
        load_dotenv(antigo, override=False)


def _req_portal(nome: str) -> str:
    """
    Lê var do .env.portal (projeto NOVO do portal), com fallback pro ambiente
    JÁ carregado de .env.portal (nunca do .env antigo: aquele é de OUTRO
    projeto Supabase e não pode ser usado aqui).
    """
    portal = ROOT / ".env.portal"
    if not portal.exists():
        raise RuntimeError(
            "⚠️  Arquivo .env.portal NÃO encontrado na raiz do repo. "
            "Crie-o com SUPABASE_URL e SUPABASE_SERVICE_KEY do projeto do "
            "portal (veja worker/README.md). As chaves do export_vps/.env são "
            "de outro projeto e não são usadas aqui de propósito."
        )
    from dotenv import dotenv_values
    vals = {k: (v or "").strip() for k, v in dotenv_values(portal).items()}
    v = vals.get(nome) or os.environ.get(nome, "").strip()
    if not v:
        raise RuntimeError(
            f"⚠️  Variável obrigatória ausente no .env.portal: {nome}. "
            f"Preencha no arquivo (projeto do portal) e rode de novo."
        )
    return v


def garantir_export_vps_no_path() -> None:
    """Coloca export_vps/ no sys.path pra importar enriquecimento_lite etc.

    Esses módulos usam imports 'from config import ...' / 'from settings import ...',
    então precisam da pasta deles no path (não dá pra importar como pacote).
    """
    p = str(EXPORT_VPS)
    if p not in sys.path:
        sys.path.insert(0, p)


def _opt(nome: str) -> "str | None":
    v = os.environ.get(nome, "").strip()
    return v or None


@dataclass(frozen=True)
class ConfigWorker:
    # ── Supabase (projeto NOVO do portal) ──
    supabase_url: str
    supabase_service_key: str

    # ── APIs de enriquecimento (reaproveitadas do export_vps) ──
    api_b_key: "str | None"
    hashiro_token: "str | None"
    datasintese_user: "str | None"
    datasintese_pass: "str | None"

    # ── Notificação (webhook Discord/Slack, opcional) ──
    notify_webhook_url: "str | None"


def load_config() -> ConfigWorker:
    carregar_envs()
    return ConfigWorker(
        supabase_url=_req_portal("SUPABASE_URL"),
        supabase_service_key=_req_portal("SUPABASE_SERVICE_KEY"),
        api_b_key=_opt("API_B_KEY"),
        hashiro_token=_opt("HASHIRO_TOKEN"),
        datasintese_user=_opt("DATASINTESE_USER"),
        datasintese_pass=_opt("DATASINTESE_PASS"),
        notify_webhook_url=_opt("NOTIFY_WEBHOOK_URL"),
    )
