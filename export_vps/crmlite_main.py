"""
crmlite_main.py — WRAPPER que roda o orquestrador ORIGINAL (poc_clicker_main.py)
contra o projeto Supabase do CRM Lite, sem tocar em nenhum arquivo dele.

O que faz, na ordem:
  1. Carrega as variáveis de ambiente do arquivo indicado por WORKER_ENV
     (padrão: `.env.crmlite` aqui na pasta; se não existir, cai pro `.env`).
     O config.py carrega `.env` com load_dotenv(override=False), então as
     variáveis que carregamos aqui ANTES têm prioridade.
  2. Injeta `supabase_repo_crmlite` em sys.modules sob o nome "supabase_repo"
     ANTES de importar o orquestrador — todos os `from supabase_repo import ...`
     dele (e de qualquer módulo que ele puxe) caem na camada de tradução.
  3. Repassa os argumentos de linha de comando intactos (argparse do orquestrador
     lê sys.argv diretamente).

USO (idêntico ao original):
  python crmlite_main.py --producao
  python crmlite_main.py --loop 5 --gravar
  python crmlite_main.py --cpf 12345678901          # avulso, sem banco
  WORKER_ENV=.env.crmlite python crmlite_main.py --producao
"""

import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def _carregar_env_manual(arq: Path) -> None:
    """Parser .env mínimo (fallback se python-dotenv não estiver instalado)."""
    try:
        for linha in arq.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            k, _, v = linha.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:  # não sobrescreve o ambiente
                os.environ[k] = v
    except Exception:
        pass


def _carregar_env() -> "Path | None":
    """Carrega WORKER_ENV (padrão .env.crmlite), com fallback pro .env."""
    nome = os.environ.get("WORKER_ENV", ".env.crmlite").strip() or ".env.crmlite"
    for arq in (BASE / nome, BASE / ".env"):
        if arq.exists():
            try:
                from dotenv import load_dotenv
                load_dotenv(arq, override=False)  # ambiente real tem prioridade
            except ImportError:
                _carregar_env_manual(arq)
            return arq
    return None


_env_usado = _carregar_env()

# Patch ANTES de qualquer import do orquestrador/enriquecimento:
# todo `from supabase_repo import ...` passa a vir da camada CRM Lite.
import supabase_repo_crmlite  # noqa: E402

sys.modules["supabase_repo"] = supabase_repo_crmlite

import poc_clicker_main  # noqa: E402

if __name__ == "__main__":
    if _env_usado:
        print(f"  🔧 env carregado de: {_env_usado.name}")
    else:
        print("  ⚠️  nenhum .env.crmlite/.env encontrado — usando só variáveis de ambiente.")
    # sys.argv já contém os argumentos (ex.: --producao) — o argparse do
    # orquestrador os lê diretamente.
    raise SystemExit(poc_clicker_main.main())
