"""
migrate_to_postgres.py — copia os dados do SQLite local para o PostgreSQL.

Use UMA vez, ao migrar do modo single-user (SQLite) para o modo compartilhado
(PostgreSQL). Preserva os timestamps originais (created_at, banned_at, etc.).

Como usar (PowerShell):
    $env:DATABASE_URL = "postgresql://user:senha@host:5432/dbname?sslmode=require"
    .\venv\Scripts\python.exe migrate_to_postgres.py

É idempotente: linhas já existentes (mesmo id/chave) são ignoradas (ON CONFLICT).
"""
import os
import sys
import sqlite3

from paths import DB_PATH

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("PROFILEMANAGER_DB_URL")
if not DATABASE_URL:
    print("ERRO: defina DATABASE_URL antes de rodar a migração.")
    sys.exit(1)

if not DB_PATH.exists():
    print(f"Nada a migrar: SQLite local não encontrado em {DB_PATH}")
    sys.exit(0)

import psycopg
from psycopg.rows import dict_row

# Garante que o esquema exista no PostgreSQL (init_db usa DATABASE_URL).
import db as _db
assert _db.USE_PG, "db.py não está em modo PostgreSQL — confira a DATABASE_URL."
_db.init_db()

# Tabelas a migrar e a coluna de conflito (chave primária).
TABLES = [
    ("profiles", "id"),
    ("accounts", "id"),
    ("emails", "id"),
    ("settings", "key"),
    ("proxy_pool", "key"),
]


def _pg_columns(pg, table: str) -> set:
    rows = pg.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,)).fetchall()
    return {r["column_name"] for r in rows}


def _sqlite_table_exists(lite, table: str) -> bool:
    row = lite.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def copy_table(lite, pg, table: str, pk: str) -> int:
    if not _sqlite_table_exists(lite, table):
        return 0
    pg_cols = _pg_columns(pg, table)
    rows = lite.execute(f"SELECT * FROM {table}").fetchall()
    n = 0
    for row in rows:
        d = {k: row[k] for k in row.keys() if k in pg_cols}
        if not d:
            continue
        cols = list(d.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(cols)
        sql = (f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
               f"ON CONFLICT ({pk}) DO NOTHING")
        cur = pg.execute(sql, [d[c] for c in cols])
        n += getattr(cur, "rowcount", 0) or 0
    return n


def main():
    lite = sqlite3.connect(DB_PATH)
    lite.row_factory = sqlite3.Row
    print(f"Origem (SQLite): {DB_PATH}")
    print(f"Destino (PostgreSQL): {DATABASE_URL.split('@')[-1]}")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as pg:
        for table, pk in TABLES:
            try:
                inserted = copy_table(lite, pg, table, pk)
                print(f"  {table:<12}: {inserted} linha(s) migrada(s)")
            except Exception as e:
                print(f"  {table:<12}: ERRO — {e}")
    lite.close()
    print("Migração concluída.")


if __name__ == "__main__":
    main()
