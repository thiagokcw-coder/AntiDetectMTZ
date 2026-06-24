"""
limpar_rows_json.py — esvazia spider_runs.rows_json e RECUPERA espaço no banco.

Por quê: as linhas extraídas agora vivem nos CSVs de data/<alvo>/, não no banco.
O rows_json antigo só ocupa espaço (inchava o Supabase/o SQLite a GBs).

Roda no banco CONFIGURADO:
  - PostgreSQL (Supabase) se DATABASE_URL (ou database_url.txt ao lado) existir;
  - senão o SQLite local (profiles.db).

Uso:
  # Supabase:
  DATABASE_URL="postgresql://...sslmode=require" ./venv/bin/python limpar_rows_json.py
  # ou tenha database_url.txt ao lado e rode:
  ./venv/bin/python limpar_rows_json.py
  # SQLite local (sem URL):
  ./venv/bin/python limpar_rows_json.py

Rode com o app FECHADO e sem extração em andamento (o VACUUM trava a tabela um instante).
As runs antigas perdem o resume/baixar do banco — mas o dado está nos CSVs de data/.
"""
import os
import pathlib


def _resolve_url():
    u = os.environ.get("DATABASE_URL") or os.environ.get("PROFILEMANAGER_DB_URL")
    if u:
        return u.strip()
    f = pathlib.Path(__file__).resolve().parent / "database_url.txt"
    if f.exists():
        line = (f.read_text().strip().splitlines() or [""])[0].strip()
        return line or None
    return None


def main():
    url = _resolve_url()
    if url:
        import psycopg
        print("Banco: PostgreSQL (Supabase)")
        conn = psycopg.connect(url, autocommit=True, connect_timeout=20)
        cur = conn.cursor()
        cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
        print("  DB antes :", cur.fetchone()[0])
        cur.execute("UPDATE spider_runs SET rows_json = NULL WHERE rows_json IS NOT NULL")
        print("  runs limpas:", cur.rowcount)
        try:
            cur.execute("VACUUM FULL spider_runs")
            print("  VACUUM FULL: ok (espaço devolvido ao disco)")
        except Exception as e:
            print("  VACUUM FULL falhou:", str(e)[:120], "— tentando VACUUM simples")
            try:
                cur.execute("VACUUM spider_runs")
                print("  VACUUM simples: ok (espaço marcado p/ reuso)")
            except Exception as e2:
                print("  VACUUM falhou:", str(e2)[:120])
        cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
        print("  DB depois:", cur.fetchone()[0])
        conn.close()
    else:
        import sqlite3
        from paths import DB_PATH
        if not os.path.exists(DB_PATH):
            print("SQLite local não encontrado:", DB_PATH)
            return
        before = os.path.getsize(DB_PATH) / 1e6
        print(f"Banco: SQLite local ({DB_PATH})")
        print(f"  arquivo antes: {before:.1f} MB")
        c = sqlite3.connect(DB_PATH)
        c.execute("UPDATE spider_runs SET rows_json = NULL WHERE rows_json IS NOT NULL")
        c.commit()
        c.execute("VACUUM")   # reescreve o arquivo → encolhe de verdade
        c.commit()
        c.close()
        after = os.path.getsize(DB_PATH) / 1e6
        print(f"  arquivo depois: {after:.1f} MB  (recuperado: {before - after:.1f} MB)")


if __name__ == "__main__":
    main()
