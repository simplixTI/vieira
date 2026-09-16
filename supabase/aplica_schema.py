"""
aplica_schema.py — Aplica as migrations de supabase/migrations/ direto no Postgres
do projeto (sem depender de CLI do Supabase).

Uso:
    python aplica_schema.py

Pega SUPABASE_URL e SUPABASE_DB_PASSWORD do .env.portal da raiz do repo.
"""

import re
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values

RAIZ = Path(__file__).resolve().parent.parent
MIGRATIONS = RAIZ / "supabase" / "migrations"


def main() -> int:
    env = dotenv_values(RAIZ / ".env.portal")
    url = (env.get("SUPABASE_URL") or "").strip().rstrip("/")
    senha = env.get("SUPABASE_DB_PASSWORD") or ""
    if not url or not senha:
        print("Faltam SUPABASE_URL/SUPABASE_DB_PASSWORD no .env.portal")
        return 1

    ref = re.sub(r"https?://(?:db\.)?", "", url).split(".")[0]
    host = f"db.{ref}.supabase.co"

    sql_files = sorted(MIGRATIONS.glob("*.sql"))
    if not sql_files:
        print(f"Nenhuma migration em {MIGRATIONS}")
        return 1

    conninfo = (
        f"host={host} port=5432 dbname=postgres user=postgres "
        f"password={senha} sslmode=require connect_timeout=30"
    )
    print(f"Conectando em {host} ...")
    try:
        conn = psycopg.connect(conninfo)
    except psycopg.OperationalError as e:
        print(f"Falha na conexão: {str(e)[:300]}")
        print("Se o projeto não aceita conexão direta (IPv6), use o Session Pooler")
        return 1

    with conn:
        for arq in sql_files:
            print(f"Aplicando {arq.name} ...")
            conn.execute(arq.read_text(encoding="utf-8"))

        tabelas = conn.execute(
            "select tablename, rowsecurity from pg_tables "
            "where schemaname='public' and tablename in ('batches','voter_records')"
        ).fetchall()
        print("Verificação:", tabelas)
    conn.close()
    print("OK — schema aplicado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
