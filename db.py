"""
db.py — camada de dados com suporte a PostgreSQL (compartilhado) e SQLite (local).

- Se a variável de ambiente DATABASE_URL estiver definida, usa PostgreSQL.
  É o modo COMPARTILHADO: contas, emails, configurações e o pool de proxies
  ficam num banco central acessível por vários usuários.
- Caso contrário, cai no SQLite local (modo single-user, sem mudar nada).

OBS de segurança: senhas de contas, senha IMAP e senhas de proxy são gravadas
em TEXTO PURO. Num banco compartilhado pela internet, use SEMPRE SSL
(ex.: ...?sslmode=require) e trate a DATABASE_URL como segredo — quem tem a URL
enxerga todos esses dados.
"""
import os
import json
import datetime as _dt

from paths import DB_PATH

# ── Seleção do backend ────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("PROFILEMANAGER_DB_URL")
USE_PG = bool(DATABASE_URL)
print(f"DEBUG: DB mode: {'PostgreSQL (shared)' if USE_PG else 'SQLite (local)'}  URL={DATABASE_URL[:30]+'...' if DATABASE_URL else 'None'}")

if USE_PG:
    import psycopg
    from psycopg.rows import dict_row
else:
    import sqlite3


def _now_str() -> str:
    """Timestamp UTC em formato portável e compatível com accounts._parse."""
    return _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# Status que indicam que a conta foi REALMENTE criada (código de e-mail usado e além).
# Só essas consomem a cota de um proxy — tentativas incompletas (creating, espera de
# verificação, erro, cancelada) NÃO contam como uso do proxy.
COMPLETED_ACCOUNT_STATUSES = (
    "created", "active", "banned", "suspended", "checkpoint", "login_required",
)


def get_conn():
    """Conexão usada como context manager (`with get_conn() as conn:`).

    - PostgreSQL: a transação é commitada (ou revertida) e a conexão fechada ao
      sair do bloco `with`.
    - SQLite: a transação é commitada ao sair do bloco `with`.
    """
    if USE_PG:
        # connect_timeout prevents the whole app from hanging forever on startup if the Postgres server is unreachable or DATABASE_URL is misconfigured (e.g. from database_url.txt).
        return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ex(conn, sql: str, params=()):
    """Executa uma query adaptando o placeholder de parâmetros por dialeto.

    Escrevemos SQL com `?` (estilo SQLite) e convertemos para `%s` no PostgreSQL.
    Retorna o cursor (ambos os drivers expõem fetchone/fetchall no resultado).
    """
    if USE_PG:
        sql = sql.replace("?", "%s")
    return conn.execute(sql, params)


# ── Inicialização / migrações ─────────────────────────────────────────────────

def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                proxy       TEXT,
                user_data   TEXT NOT NULL,
                created_at  TEXT
            )
        """)
        # Contas criadas (1 linha por conta de rede social)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id              TEXT PRIMARY KEY,
                profile_id      TEXT,
                platform        TEXT DEFAULT 'instagram',
                username        TEXT,
                email           TEXT,
                password        TEXT,
                whatsapp_number TEXT,
                proxy           TEXT,
                proxy_key       TEXT,
                status          TEXT DEFAULT 'creating',
                status_detail   TEXT,
                created_at      TEXT,
                banned_at       TEXT,
                last_checked_at TEXT,
                last_check_result TEXT
            )
        """)
        # Configurações globais (limites, config de email)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Emails gerados (catch-all)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                id          TEXT PRIMARY KEY,
                address     TEXT UNIQUE,
                profile_id  TEXT,
                account_id  TEXT,
                status      TEXT DEFAULT 'free',
                created_at  TEXT,
                notes       TEXT
            )
        """)
        # Histórico de contas (append-only): preserva métricas mesmo após apagar perfil/conta.
        # Uma linha por conta (account_id), atualizada a cada mudança de status; NUNCA apagada
        # quando o perfil/conta é removido. As métricas leem daqui ∪ contas vivas.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_history (
                account_id      TEXT PRIMARY KEY,
                username        TEXT,
                email           TEXT,
                proxy_key       TEXT,
                status          TEXT,
                status_detail   TEXT,
                created_at      TEXT,
                banned_at       TEXT,
                last_checked_at TEXT,
                recorded_at     TEXT
            )
        """)
        # Tentativas de criação (aprendizado): condições + onde falhou + desfecho de CADA criação.
        # Base para o app aprender ao longo do tempo (append-only, nunca apagada).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS creation_attempts (
                id             TEXT PRIMARY KEY,
                profile_id     TEXT,
                account_id     TEXT,
                email          TEXT,
                username       TEXT,
                proxy_key      TEXT,
                os             TEXT,
                warmup_minutes REAL,
                from_pool      INTEGER,
                outcome        TEXT,
                gate           TEXT,
                step           TEXT,
                detail         TEXT,
                created_at     TEXT
            )
        """)
        # Números de WhatsApp cadastrados manualmente (controle de uso: free | used)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_numbers (
                id          TEXT PRIMARY KEY,
                number      TEXT,
                status      TEXT DEFAULT 'free',
                account_id  TEXT,
                notes       TEXT,
                created_at  TEXT,
                used_at     TEXT
            )
        """)
        # Pool de proxies COMPARTILHADO (substitui a leitura só-local da pasta proxys/)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS proxy_pool (
                key       TEXT PRIMARY KEY,
                protocol  TEXT DEFAULT 'http',
                host      TEXT NOT NULL,
                port      INTEGER NOT NULL,
                username  TEXT,
                password  TEXT,
                added_at  TEXT
            )
        """)
        # Lock COMPARTILHADO de perfil em uso (impede 2 máquinas abrirem o mesmo
        # perfil ao mesmo tempo). heartbeat_at é atualizado enquanto a sessão roda;
        # se a máquina travar, o lock expira pelo TTL e pode ser reaproveitado.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profile_locks (
                profile_id   TEXT PRIMARY KEY,
                holder       TEXT,
                token        TEXT,
                acquired_at  TEXT,
                heartbeat_at TEXT
            )
        """)
        # Marcação MANUAL de proxy/IP (ex.: "queimado" pelo usuário). 'key' = host:porta
        # de um proxy do pool OU um IP cru (ex.: o IP real da conexão). status='banned'
        # faz o assign() parar de usar e a UI mostrar como queimado.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS proxy_flags (
                key       TEXT PRIMARY KEY,
                status    TEXT NOT NULL,
                reason    TEXT,
                marked_at TEXT
            )
        """)
        # Histórico de execuções do Spider (sobrevive a reiniciar o app). rows_json
        # guarda os registros extraídos (p/ baixar CSV depois e, futuramente, resume).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS spider_runs (
                id          TEXT PRIMARY KEY,
                method      TEXT,
                profile     TEXT,
                profile_id  TEXT,
                account_id  TEXT,
                use_local   INTEGER DEFAULT 0,
                limit_n     INTEGER DEFAULT 0,
                delay_ms    INTEGER DEFAULT 2500,
                cursor      TEXT,
                target      TEXT,
                kind        TEXT,
                count       INTEGER DEFAULT 0,
                status      TEXT,
                error       TEXT,
                rows_json   TEXT,
                started_at  TEXT,
                finished_at TEXT
            )
        """)
        # Migração aditiva (caso a tabela já exista de uma versão anterior)
        for _c, _d in (("profile_id", "TEXT"), ("account_id", "TEXT"),
                       ("use_local", "INTEGER DEFAULT 0"), ("limit_n", "INTEGER DEFAULT 0"),
                       ("delay_ms", "INTEGER DEFAULT 2500"), ("cursor", "TEXT")):
            _ensure_column(conn, "spider_runs", _c, _d)
        # Defaults de limite (não sobrescreve se já existir)
        _ex(conn, "INSERT INTO settings (key, value) VALUES ('proxy_limit', '3') "
                  "ON CONFLICT (key) DO NOTHING")
        _ex(conn, "INSERT INTO settings (key, value) VALUES ('whatsapp_limit', '1') "
                  "ON CONFLICT (key) DO NOTHING")
        # Migrações aditivas
        _ensure_column(conn, "profiles", "warmup_count", "INTEGER DEFAULT 0")
        _ensure_column(conn, "profiles", "last_warmup_at", "TEXT")
        _ensure_column(conn, "accounts", "warmup_count", "INTEGER DEFAULT 0")
        _ensure_column(conn, "accounts", "last_warmup_at", "TEXT")
        # Pool de perfis pré-aquecidos: NULL=normal, 'warming' | 'ready' | 'in_use'
        _ensure_column(conn, "profiles", "pool_status", "TEXT")
        # Momento em que o aquecimento começou (para mostrar tempo decorrido na UI)
        _ensure_column(conn, "profiles", "warm_started_at", "TEXT")


def _ensure_column(conn, table: str, col: str, decl: str):
    if USE_PG:
        # PostgreSQL suporta ADD COLUMN IF NOT EXISTS
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {decl}")
        return
    # SQLite: checa via PRAGMA antes de adicionar
    existing = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def mark_profile_warmed(profile_id: str):
    """Incrementa o contador de aquecimento do PERFIL (navegador) e marca a data."""
    with get_conn() as conn:
        _ex(conn,
            "UPDATE profiles SET warmup_count = COALESCE(warmup_count, 0) + 1, "
            "last_warmup_at = ? WHERE id = ?",
            (_now_str(), profile_id))


def mark_account_warmed(account_id: str):
    """Incrementa o contador de aquecimento da CONTA (atividade no IG) e marca a data."""
    with get_conn() as conn:
        _ex(conn,
            "UPDATE accounts SET warmup_count = COALESCE(warmup_count, 0) + 1, "
            "last_warmup_at = ? WHERE id = ?",
            (_now_str(), account_id))


# ── Profiles ──────────────────────────────────────────────────────────────────

def save_profile(profile: dict):
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO profiles (id, name, fingerprint, proxy, user_data, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                profile["id"],
                profile["name"],
                json.dumps(profile["fingerprint"]),
                json.dumps(profile["proxy"]) if profile.get("proxy") else None,
                profile["user_data"],
                _now_str(),
            ))


def get_profile(profile_id: str):
    with get_conn() as conn:
        row = _ex(conn, "SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def list_profiles():
    with get_conn() as conn:
        rows = _ex(conn, "SELECT * FROM profiles ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def update_profile(profile_id: str, **fields):
    """Atualiza campos do perfil (ex.: name, proxy). proxy deve vir já serializado
    como JSON string (ou None para remover)."""
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        _ex(conn, f"UPDATE profiles SET {cols} WHERE id = ?",
            list(fields.values()) + [profile_id])


def delete_profile(profile_id: str):
    with get_conn() as conn:
        _ex(conn, "DELETE FROM profiles WHERE id = ?", (profile_id,))


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["fingerprint"] = json.loads(d["fingerprint"])
    d["proxy"] = json.loads(d["proxy"]) if d["proxy"] else None
    return d


# ── Accounts ──────────────────────────────────────────────────────────────────

def _account_row_to_dict(row) -> dict:
    d = dict(row)
    d["proxy"] = json.loads(d["proxy"]) if d.get("proxy") else None
    return d


def save_account(acc: dict):
    with get_conn() as conn:
        _ex(conn,
            """INSERT INTO accounts
               (id, profile_id, platform, username, email, password,
                whatsapp_number, proxy, proxy_key, status, status_detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                acc["id"], acc.get("profile_id"), acc.get("platform", "instagram"),
                acc.get("username"), acc.get("email"), acc.get("password"),
                acc.get("whatsapp_number"),
                json.dumps(acc["proxy"]) if acc.get("proxy") else None,
                acc.get("proxy_key"),
                acc.get("status", "creating"), acc.get("status_detail"),
                _now_str(),
            ))


def update_account(account_id: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [account_id]
    with get_conn() as conn:
        _ex(conn, f"UPDATE accounts SET {cols} WHERE id = ?", vals)


def get_account(account_id: str):
    with get_conn() as conn:
        row = _ex(conn, "SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return _account_row_to_dict(row) if row else None


def get_account_by_profile(profile_id: str):
    with get_conn() as conn:
        row = _ex(conn,
            "SELECT * FROM accounts WHERE profile_id = ? ORDER BY created_at DESC LIMIT 1",
            (profile_id,)).fetchone()
    return _account_row_to_dict(row) if row else None


def list_accounts():
    with get_conn() as conn:
        rows = _ex(conn, "SELECT * FROM accounts ORDER BY created_at DESC").fetchall()
    return [_account_row_to_dict(r) for r in rows]


def delete_account(account_id: str):
    with get_conn() as conn:
        _ex(conn, "DELETE FROM accounts WHERE id = ?", (account_id,))


# ── Histórico de contas (append-only, sobrevive à exclusão) ─────────────────────

def record_account_history(acc: dict):
    """Grava/atualiza o snapshot da conta no histórico (upsert por account_id).
    Chamado em criação, mudança de status e antes de apagar — para a métrica nunca se perder."""
    if not acc or not acc.get("id"):
        return
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO account_history "
            "(account_id, username, email, proxy_key, status, status_detail, created_at, banned_at, last_checked_at, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "username=excluded.username, email=excluded.email, proxy_key=excluded.proxy_key, "
            "status=excluded.status, status_detail=excluded.status_detail, created_at=excluded.created_at, "
            "banned_at=excluded.banned_at, last_checked_at=excluded.last_checked_at, recorded_at=excluded.recorded_at",
            (acc.get("id"), acc.get("username"), acc.get("email"), acc.get("proxy_key"),
             acc.get("status"), acc.get("status_detail"), acc.get("created_at"),
             acc.get("banned_at"), acc.get("last_checked_at"), _now_str()))


def list_account_history() -> list:
    with get_conn() as conn:
        rows = _ex(conn, "SELECT * FROM account_history").fetchall()
    return [dict(r) for r in rows]


# ── Tentativas de criação (aprendizado) ─────────────────────────────────────────

def record_creation_attempt(d: dict):
    """Registra uma tentativa de criação (condições + desfecho). Append-only."""
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO creation_attempts "
            "(id, profile_id, account_id, email, username, proxy_key, os, warmup_minutes, from_pool, outcome, gate, step, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (d.get("id"), d.get("profile_id"), d.get("account_id"), d.get("email"),
             d.get("username"), d.get("proxy_key"), d.get("os"), d.get("warmup_minutes"),
             1 if d.get("from_pool") else 0, d.get("outcome"), d.get("gate"),
             d.get("step"), (d.get("detail") or "")[:300], _now_str()))


def list_creation_attempts(limit: int = 500) -> list:
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT * FROM creation_attempts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_accounts_by_proxy(proxy_key: str) -> int:
    """Quantas contas CONCLUÍDAS usam este proxy (tentativas incompletas não contam)."""
    if not proxy_key:
        return 0
    qs = ",".join("?" for _ in COMPLETED_ACCOUNT_STATUSES)
    with get_conn() as conn:
        row = _ex(conn,
            f"SELECT COUNT(*) AS n FROM accounts WHERE proxy_key = ? AND status IN ({qs})",
            (proxy_key, *COMPLETED_ACCOUNT_STATUSES)).fetchone()
    return (row["n"] if row else 0) or 0


def completed_accounts_by_proxy() -> dict:
    """Mapa {proxy_key: nº de contas concluídas} — base da contagem de uso do proxy."""
    qs = ",".join("?" for _ in COMPLETED_ACCOUNT_STATUSES)
    with get_conn() as conn:
        rows = _ex(conn,
            f"SELECT proxy_key, COUNT(*) AS n FROM accounts "
            f"WHERE status IN ({qs}) AND proxy_key IS NOT NULL AND proxy_key <> '' "
            f"GROUP BY proxy_key", tuple(COMPLETED_ACCOUNT_STATUSES)).fetchall()
    return {r["proxy_key"]: (r["n"] or 0) for r in rows}


def count_accounts_by_whatsapp(number: str) -> int:
    if not number:
        return 0
    with get_conn() as conn:
        row = _ex(conn,
            "SELECT COUNT(*) AS n FROM accounts WHERE whatsapp_number = ?", (number,)).fetchone()
    return (row["n"] if row else 0) or 0


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = _ex(conn, "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value):
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, str(value)))


# ── Emails ────────────────────────────────────────────────────────────────────

def save_email(em: dict):
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO emails (id, address, profile_id, account_id, status, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (em["id"], em["address"], em.get("profile_id"), em.get("account_id"),
             em.get("status", "free"), em.get("notes"), _now_str()))


def list_emails():
    with get_conn() as conn:
        rows = _ex(conn, "SELECT * FROM emails ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_email(email_id: str):
    with get_conn() as conn:
        row = _ex(conn, "SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    return dict(row) if row else None


def get_email_by_profile(profile_id: str):
    with get_conn() as conn:
        row = _ex(conn,
            "SELECT * FROM emails WHERE profile_id = ? ORDER BY created_at DESC LIMIT 1",
            (profile_id,)).fetchone()
    return dict(row) if row else None


def update_email(email_id: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        _ex(conn, f"UPDATE emails SET {cols} WHERE id = ?", list(fields.values()) + [email_id])


def delete_email(email_id: str):
    with get_conn() as conn:
        _ex(conn, "DELETE FROM emails WHERE id = ?", (email_id,))


# ── Números de WhatsApp (cadastro manual + controle de uso) ─────────────────────

def save_whatsapp(wa: dict):
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO whatsapp_numbers (id, number, status, account_id, notes, created_at, used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (wa["id"], wa["number"], wa.get("status", "free"), wa.get("account_id"),
             wa.get("notes"), _now_str(), wa.get("used_at")))


def list_whatsapps():
    with get_conn() as conn:
        rows = _ex(conn, "SELECT * FROM whatsapp_numbers ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_whatsapp(wa_id: str):
    with get_conn() as conn:
        row = _ex(conn, "SELECT * FROM whatsapp_numbers WHERE id = ?", (wa_id,)).fetchone()
    return dict(row) if row else None


def get_whatsapp_by_number(number: str):
    with get_conn() as conn:
        row = _ex(conn, "SELECT * FROM whatsapp_numbers WHERE number = ?", (number,)).fetchone()
    return dict(row) if row else None


def update_whatsapp(wa_id: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        _ex(conn, f"UPDATE whatsapp_numbers SET {cols} WHERE id = ?", list(fields.values()) + [wa_id])


def delete_whatsapp(wa_id: str):
    with get_conn() as conn:
        _ex(conn, "DELETE FROM whatsapp_numbers WHERE id = ?", (wa_id,))


# ── Pool de proxies (compartilhado) ────────────────────────────────────────────

def add_pool_proxy(p: dict) -> bool:
    """Adiciona um proxy ao pool compartilhado. Idempotente por host:porta.
    Retorna True se inseriu, False se já existia."""
    key = f"{p['host']}:{p.get('port', '')}"
    with get_conn() as conn:
        cur = _ex(conn,
            "INSERT INTO proxy_pool (key, protocol, host, port, username, password, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (key) DO NOTHING",
            (key, p.get("protocol", "http"), p["host"], int(p["port"]),
             p.get("username"), p.get("password"), _now_str()))
        return bool(getattr(cur, "rowcount", 0))


def list_pool_proxies() -> list:
    with get_conn() as conn:
        rows = _ex(conn, "SELECT * FROM proxy_pool ORDER BY added_at").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out.append({
            "protocol": d.get("protocol") or "http",
            "host": d["host"],
            "port": d["port"],
            "username": d.get("username"),
            "password": d.get("password"),
        })
    return out


def delete_pool_proxy(key: str):
    with get_conn() as conn:
        _ex(conn, "DELETE FROM proxy_pool WHERE key = ?", (key,))


def count_pool() -> int:
    with get_conn() as conn:
        row = _ex(conn, "SELECT COUNT(*) AS n FROM proxy_pool").fetchone()
    return (row["n"] if row else 0) or 0


# ── Marcação manual de proxy/IP (queimado etc.) ────────────────────────────────

def set_proxy_flag(key: str, status: str, reason: str = ""):
    """Marca um proxy (host:porta) ou IP como, ex.: 'banned' (queimado). Upsert."""
    if not key:
        return
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO proxy_flags (key, status, reason, marked_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET status=excluded.status, "
            "reason=excluded.reason, marked_at=excluded.marked_at",
            (key, status, reason or "", _now_str()))


def delete_proxy_flag(key: str):
    """Remove a marcação manual (libera o proxy/IP de volta)."""
    if not key:
        return
    with get_conn() as conn:
        _ex(conn, "DELETE FROM proxy_flags WHERE key = ?", (key,))


def list_proxy_flags() -> dict:
    """{key: {status, reason, marked_at}} — marcações manuais ativas."""
    with get_conn() as conn:
        rows = _ex(conn, "SELECT * FROM proxy_flags").fetchall()
    return {r["key"]: {"status": r["status"], "reason": r["reason"],
                       "marked_at": r["marked_at"]} for r in rows}


# ── Histórico de execuções do Spider ───────────────────────────────────────────

def spider_run_upsert(d: dict):
    """Insere/atualiza uma execução do Spider (PK=id). Salva metadados + rows_json +
    contexto de resume (profile_id/account_id/use_local/limit_n/cursor)."""
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO spider_runs (id, method, profile, profile_id, account_id, "
            "use_local, limit_n, delay_ms, cursor, target, kind, count, status, error, "
            "rows_json, started_at, finished_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET method=excluded.method, profile=excluded.profile, "
            "profile_id=excluded.profile_id, account_id=excluded.account_id, "
            "use_local=excluded.use_local, limit_n=excluded.limit_n, delay_ms=excluded.delay_ms, "
            "cursor=excluded.cursor, target=excluded.target, kind=excluded.kind, "
            "count=excluded.count, status=excluded.status, error=excluded.error, "
            "rows_json=excluded.rows_json, finished_at=excluded.finished_at",
            (d.get("id"), d.get("method"), d.get("profile"), d.get("profile_id"),
             d.get("account_id"), 1 if d.get("use_local") else 0, int(d.get("limit_n") or 0),
             int(d.get("delay_ms") or 2500), d.get("cursor"), d.get("target"), d.get("kind"),
             int(d.get("count") or 0), d.get("status"), d.get("error"), d.get("rows_json"),
             d.get("started_at"), d.get("finished_at")))


def list_spider_runs(limit: int = 50) -> list:
    """Histórico (metadados + cursor, SEM rows_json) — mais recentes primeiro.
    `cursor` não-nulo indica que a execução parou no meio (dá p/ continuar)."""
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT id, method, profile, target, kind, count, status, error, "
            "cursor, started_at, finished_at FROM spider_runs "
            "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_spider_run(run_id: str) -> dict:
    with get_conn() as conn:
        row = _ex(conn, "SELECT * FROM spider_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def delete_spider_run(run_id: str):
    with get_conn() as conn:
        _ex(conn, "DELETE FROM spider_runs WHERE id = ?", (run_id,))


# ── Lock de perfil em uso (compartilhado) ──────────────────────────────────────

def _stale_str(ttl_seconds: int) -> str:
    """Timestamp-limite: locks com heartbeat anterior a isto são considerados expirados."""
    return (_dt.datetime.utcnow() - _dt.timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def acquire_profile_lock(profile_id: str, holder: str, token: str, ttl_seconds: int = 90) -> dict:
    """Tenta adquirir o lock do perfil de forma atômica.

    Adquire se: não há lock, o lock é nosso (mesmo token) ou o lock está expirado
    (heartbeat mais antigo que o TTL). Retorna {ok: True} se conseguiu, senão
    {ok: False, holder, since} com quem detém o lock no momento.
    """
    now_s = _now_str()
    stale_s = _stale_str(ttl_seconds)
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO profile_locks (profile_id, holder, token, acquired_at, heartbeat_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (profile_id) DO UPDATE "
            "  SET holder = excluded.holder, token = excluded.token, "
            "      acquired_at = excluded.acquired_at, heartbeat_at = excluded.heartbeat_at "
            "  WHERE profile_locks.token = excluded.token "
            "     OR profile_locks.heartbeat_at < ?",
            (profile_id, holder, token, now_s, now_s, stale_s))
        row = _ex(conn,
            "SELECT holder, token, acquired_at FROM profile_locks WHERE profile_id = ?",
            (profile_id,)).fetchone()
    if row and row["token"] == token:
        return {"ok": True}
    return {"ok": False,
            "holder": row["holder"] if row else None,
            "since": row["acquired_at"] if row else None}


def refresh_profile_lock(profile_id: str, token: str):
    """Atualiza o heartbeat enquanto a sessão segue ativa (mantém o lock vivo)."""
    with get_conn() as conn:
        _ex(conn,
            "UPDATE profile_locks SET heartbeat_at = ? WHERE profile_id = ? AND token = ?",
            (_now_str(), profile_id, token))


def release_profile_lock(profile_id: str, token: str):
    """Libera o lock — só remove se for o detentor (mesmo token)."""
    with get_conn() as conn:
        _ex(conn,
            "DELETE FROM profile_locks WHERE profile_id = ? AND token = ?",
            (profile_id, token))


def get_profile_lock(profile_id: str, ttl_seconds: int = 90) -> dict | None:
    """Retorna o lock ativo (não expirado) do perfil, ou None."""
    stale_s = _stale_str(ttl_seconds)
    with get_conn() as conn:
        row = _ex(conn,
            "SELECT holder, token, acquired_at, heartbeat_at FROM profile_locks "
            "WHERE profile_id = ?", (profile_id,)).fetchone()
    if not row or not row["heartbeat_at"] or row["heartbeat_at"] < stale_s:
        return None
    return {"holder": row["holder"], "token": row["token"], "since": row["acquired_at"]}


def get_active_profile_locks(ttl_seconds: int = 90) -> dict:
    """Todos os locks ATIVOS em 1 consulta → {profile_id: {holder, token, since}}.
    Evita N+1 (1 query por perfil) ao listar muitos perfis no banco remoto."""
    stale_s = _stale_str(ttl_seconds)
    out = {}
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT profile_id, holder, token, acquired_at, heartbeat_at FROM profile_locks").fetchall()
    for row in rows:
        if row["heartbeat_at"] and row["heartbeat_at"] >= stale_s:
            out[row["profile_id"]] = {"holder": row["holder"], "token": row["token"], "since": row["acquired_at"]}
    return out

