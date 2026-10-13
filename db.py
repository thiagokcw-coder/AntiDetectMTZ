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
                total       INTEGER,
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
                       ("delay_ms", "INTEGER DEFAULT 2500"), ("cursor", "TEXT"),
                       ("total", "INTEGER"), ("profile_ids", "TEXT"),
                       ("blocked", "INTEGER DEFAULT 0"),    # desfecho: terminou em bloqueio/desafio
                       ("stopped_by_user", "INTEGER DEFAULT 0")):  # parada manual (não é falha)
            _ensure_column(conn, "spider_runs", _c, _d)
        # Perfis MONITORADOS: alvos cujos seguidores a gente acompanha/extrai de
        # tempos em tempos. followers_total = total do alvo (atualizado pelo Spider);
        # o quanto já foi extraído é derivado de spider_runs (por target).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monitored_profiles (
                id              TEXT PRIMARY KEY,
                username        TEXT UNIQUE,
                user_id         TEXT,
                followers_total INTEGER,
                last_total_at   TEXT,
                added_at        TEXT,
                notes           TEXT
            )
        """)
        # extracted_known: total REAL de seguidores únicos que temos do alvo (ex.: a
        # união deduplicada de várias runs/CSVs). Usado quando o MAX(count) de uma run
        # isolada subestima (re-runs deixam dados espalhados). Migração aditiva.
        _ensure_column(conn, "monitored_profiles", "extracted_known", "INTEGER")
        # ── Seguir (porte do InstaFollow) ──────────────────────────────────────
        # Execuções do motor de follow. queue_json guarda a fila inteira (com o
        # status de cada conta) → recarregar/reiniciar não perde progresso.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS follow_runs (
                id            TEXT PRIMARY KEY,
                profile       TEXT,
                profile_id    TEXT,
                account_id    TEXT,
                account_key   TEXT,
                use_local     INTEGER DEFAULT 0,
                dry_run       INTEGER DEFAULT 1,
                source_type   TEXT,
                source_target TEXT,
                status        TEXT,
                error         TEXT,
                followed      INTEGER DEFAULT 0,
                requested     INTEGER DEFAULT 0,
                skipped       INTEGER DEFAULT 0,
                failed        INTEGER DEFAULT 0,
                simulated     INTEGER DEFAULT 0,
                queue_json    TEXT,
                settings_json TEXT,
                filters_json  TEXT,
                blocked       INTEGER DEFAULT 0,
                stopped_by_user INTEGER DEFAULT 0,
                started_at    TEXT,
                finished_at   TEXT
            )
        """)
        # Histórico GLOBAL: uma conta já seguida (por qualquer conta nossa) nunca é
        # seguida de novo — é o que faz cada conta pegar uma fatia distinta da fila.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS follow_history (
                username    TEXT PRIMARY KEY,
                ts_ms       BIGINT,
                by_account  TEXT,
                followed_at TEXT
            )
        """)
        # Timestamps das ações POR CONTA — alimentam o limitador de janela deslizante.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS follow_actions (
                account_key TEXT NOT NULL,
                ts_ms       BIGINT NOT NULL,
                PRIMARY KEY (account_key, ts_ms)
            )
        """)
        # Nunca seguir, venha de onde vier.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS follow_blacklist (
                username TEXT PRIMARY KEY,
                added_at TEXT
            )
        """)
        # Nunca DEIXAR de seguir (whitelist do unfollow) — o oposto da blacklist.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS follow_whitelist (
                username TEXT PRIMARY KEY,
                added_at TEXT
            )
        """)
        # Quando desfizemos o follow (NULL = pelos nossos registros ainda seguimos).
        # É o que permite "unfollow N dias depois" sem reprocessar quem já saiu.
        _ensure_column(conn, "follow_history", "unfollowed_ts_ms", "BIGINT")
        _ensure_column(conn, "follow_history", "user_id", "TEXT")
        # De qual alvo/fonte a pessoa veio ("followers:@fulano") — permite medir qual
        # alvo rende mais e exportar isso no CSV.
        _ensure_column(conn, "follow_history", "source", "TEXT")
        # Quem retribuiu o follow. Duas colunas de proposito: sem a de verificacao,
        # "ainda nao conferi" seria indistinguivel de "nao retribuiu" e a taxa de
        # retorno apareceria menor do que e.
        _ensure_column(conn, "follow_history", "followed_back_ts_ms", "BIGINT")
        _ensure_column(conn, "follow_history", "followback_checked_ms", "BIGINT")
        # Execuções de unfollow reusam follow_runs (mesmo motor/limitador).
        _ensure_column(conn, "follow_runs", "mode", "TEXT DEFAULT 'follow'")
        _ensure_column(conn, "follow_runs", "unfollowed", "INTEGER DEFAULT 0")
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
        # Quantas vezes a conta entrou em suspensão (suspended/checkpoint). Persiste
        # no histórico (nunca diminui) — suspensão é recuperável, mas o histórico conta.
        _ensure_column(conn, "accounts", "suspension_count", "INTEGER DEFAULT 0")
        _ensure_column(conn, "account_history", "suspension_count", "INTEGER DEFAULT 0")


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


def get_account_by_profile(profile_id: str, platform: str = "instagram"):
    """A conta de uma REDE neste perfil (a mais recente, se houver várias).

    `platform` tem padrão 'instagram' de propósito, e não None. Desde que um
    perfil pode ter conta em várias redes, a consulta sem rede devolveria "a
    conta mais recente do perfil" — então cadastrar um TikTok num perfil faria
    o limitador do InstaFollow, a auto-verificação e o Spider passarem a contar
    pela conta do TikTok, calados. Todos os chamadores antigos falam de
    Instagram; o padrão preserva o que eles esperam.

    `platform=None` volta ao comportamento antigo (qualquer rede), para quem
    quiser de fato "a conta mais recente, seja qual for".
    """
    sql = "SELECT * FROM accounts WHERE profile_id = ?"
    args: list = [profile_id]
    if platform:
        sql += " AND LOWER(COALESCE(platform, 'instagram')) = ?"
        args.append(platform.strip().lower())
    sql += " ORDER BY created_at DESC LIMIT 1"
    with get_conn() as conn:
        row = _ex(conn, sql, tuple(args)).fetchone()
    return _account_row_to_dict(row) if row else None


def list_accounts_by_profile(profile_id: str):
    """TODAS as contas do perfil, uma por rede — a base da publicação em leva.

    Ordenada por rede e, dentro da rede, da mais nova para a mais velha: se
    alguém cadastrou duas contas da mesma rede no mesmo perfil, a leva usa a
    mais recente e a outra aparece na lista em vez de desaparecer.
    """
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT * FROM accounts WHERE profile_id = ? "
            "ORDER BY COALESCE(platform, 'instagram'), created_at DESC",
            (profile_id,)).fetchall()
    return [_account_row_to_dict(r) for r in rows]


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
            "(account_id, username, email, proxy_key, status, status_detail, created_at, banned_at, last_checked_at, recorded_at, suspension_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "username=excluded.username, email=excluded.email, proxy_key=excluded.proxy_key, "
            "status=excluded.status, status_detail=excluded.status_detail, created_at=excluded.created_at, "
            "banned_at=excluded.banned_at, last_checked_at=excluded.last_checked_at, recorded_at=excluded.recorded_at, "
            # espelha o contador da conta viva (que só cresce); compatível SQLite+Postgres
            "suspension_count=excluded.suspension_count",
            (acc.get("id"), acc.get("username"), acc.get("email"), acc.get("proxy_key"),
             acc.get("status"), acc.get("status_detail"), acc.get("created_at"),
             acc.get("banned_at"), acc.get("last_checked_at"), _now_str(),
             int(acc.get("suspension_count") or 0)))


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


def whatsapp_counts() -> dict:
    """{whatsapp_number: count} — grouped for fast enrichment in lists."""
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT whatsapp_number, COUNT(*) AS n FROM accounts "
            "WHERE whatsapp_number IS NOT NULL AND whatsapp_number <> '' "
            "GROUP BY whatsapp_number").fetchall()
    return {r["whatsapp_number"]: (r["n"] or 0) for r in rows if r.get("whatsapp_number")}


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


# ── Perfis monitorados ─────────────────────────────────────────────────────────

def _clean_uname(username: str) -> str:
    return (username or "").strip().lstrip("@").lower()


def add_monitored(username: str, user_id=None, followers_total=None, notes=None):
    """Adiciona um alvo monitorado (idempotente por username). Retorna a linha."""
    import uuid
    username = _clean_uname(username)
    if not username:
        return None
    existing = get_monitored_by_username(username)
    if existing:
        return existing
    mid = uuid.uuid4().hex[:12]
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO monitored_profiles (id, username, user_id, followers_total, "
            "last_total_at, added_at, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, username, user_id,
             int(followers_total) if followers_total else None,
             _now_str() if followers_total else None, _now_str(), notes))
    return get_monitored_by_username(username)


def list_monitored():
    with get_conn() as conn:
        rows = _ex(conn, "SELECT * FROM monitored_profiles "
                         "ORDER BY COALESCE(followers_total, 0) DESC, username").fetchall()
    return [dict(r) for r in rows]


def get_monitored(mid: str):
    with get_conn() as conn:
        row = _ex(conn, "SELECT * FROM monitored_profiles WHERE id = ?", (mid,)).fetchone()
    return dict(row) if row else None


def get_monitored_by_username(username: str):
    with get_conn() as conn:
        row = _ex(conn, "SELECT * FROM monitored_profiles WHERE username = ?",
                  (_clean_uname(username),)).fetchone()
    return dict(row) if row else None


def update_monitored(mid: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        _ex(conn, f"UPDATE monitored_profiles SET {cols} WHERE id = ?",
            list(fields.values()) + [mid])


def set_monitored_total(username: str, total):
    """Atualiza o total de seguidores de um alvo monitorado (chamado pelo Spider
    quando resolve o alvo). No-op se o alvo não estiver sendo monitorado."""
    if total is None:
        return
    with get_conn() as conn:
        _ex(conn, "UPDATE monitored_profiles SET followers_total = ?, last_total_at = ? "
                  "WHERE username = ?", (int(total), _now_str(), _clean_uname(username)))


def delete_monitored(mid: str):
    with get_conn() as conn:
        _ex(conn, "DELETE FROM monitored_profiles WHERE id = ?", (mid,))


def spider_extracted_for(username: str) -> int:
    """Maior 'count' já extraído para um alvo (de spider_runs) — usado p/ mostrar o
    progresso de extração de cada perfil monitorado."""
    with get_conn() as conn:
        row = _ex(conn,
            "SELECT MAX(count) AS c FROM spider_runs "
            "WHERE LOWER(REPLACE(target, '@', '')) = ?", (_clean_uname(username),)).fetchone()
    d = dict(row) if row else {}
    return int(d.get("c") or 0)


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
            "use_local, limit_n, delay_ms, cursor, target, kind, count, total, status, error, "
            "rows_json, started_at, finished_at, profile_ids, blocked, stopped_by_user) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET method=excluded.method, profile=excluded.profile, "
            "profile_id=excluded.profile_id, account_id=excluded.account_id, "
            "use_local=excluded.use_local, limit_n=excluded.limit_n, delay_ms=excluded.delay_ms, "
            "cursor=excluded.cursor, target=excluded.target, kind=excluded.kind, "
            "count=excluded.count, total=excluded.total, status=excluded.status, "
            "error=excluded.error, rows_json=excluded.rows_json, finished_at=excluded.finished_at, "
            "profile_ids=excluded.profile_ids, blocked=excluded.blocked, "
            "stopped_by_user=excluded.stopped_by_user",
            (d.get("id"), d.get("method"), d.get("profile"), d.get("profile_id"),
             d.get("account_id"), 1 if d.get("use_local") else 0, int(d.get("limit_n") or 0),
             int(d.get("delay_ms") or 2500), d.get("cursor"), d.get("target"), d.get("kind"),
             int(d.get("count") or 0), d.get("total"), d.get("status"), d.get("error"),
             d.get("rows_json"), d.get("started_at"), d.get("finished_at"), d.get("profile_ids"),
             1 if d.get("blocked") else 0, 1 if d.get("stopped_by_user") else 0))


def spider_run_update_progress(run_id: str, count: int, cursor: str = None):
    """Atualização BARATA de progresso (count + cursor) sem re-serializar rows_json gigante.
    Usado nos checkpoints frequentes durante extração longa. Evita 1min/page causado por
    json.dumps de dezenas/centenas de milhares de linhas + write no DB a cada 10 páginas."""
    with get_conn() as conn:
        _ex(conn,
            "UPDATE spider_runs SET count = ?, cursor = ? WHERE id = ?",
            (int(count or 0), cursor, run_id))


def list_spider_runs(limit: int = 50) -> list:
    """Histórico (metadados + cursor, SEM rows_json) — mais recentes primeiro.
    `cursor` não-nulo indica que a execução parou no meio (dá p/ continuar)."""
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT id, method, profile, target, kind, count, total, status, error, "
            "cursor, started_at, finished_at, delay_ms, blocked, stopped_by_user FROM spider_runs "
            "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def set_spider_run_blocked(run_id: str, blocked: bool):
    """Marca/desmarca um run como 'terminou em suspensão/bloqueio' — o usuário pode
    corrigir o desfecho p/ ENSINAR a recomendação de velocidade do Spider."""
    with get_conn() as conn:
        _ex(conn, "UPDATE spider_runs SET blocked = ? WHERE id = ?",
            (1 if blocked else 0, run_id))


def spider_runs_stats() -> list:
    """Campos por execução p/ o aprendizado do Spider (velocidade/itens/desfecho/duração).
    SEM rows_json (leve). Base para recomendar a velocidade segura."""
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT delay_ms, count, total, blocked, stopped_by_user, status, kind, cursor, "
            "account_id, profile_id, started_at, finished_at FROM spider_runs").fetchall()
    return [dict(r) for r in rows]


def spider_runs_export() -> list:
    """Todos os runs com o contexto analítico (SEM rows_json), cronológico — base p/
    o log de eventos por extração/conta (análise caso a caso, incl. por uma IA)."""
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT id, method, profile, profile_id, account_id, use_local, delay_ms, "
            "limit_n, target, kind, count, total, status, error, cursor, blocked, "
            "stopped_by_user, started_at, finished_at FROM spider_runs "
            "ORDER BY started_at ASC").fetchall()
    return [dict(r) for r in rows]


def get_spider_run(run_id: str) -> dict:
    with get_conn() as conn:
        row = _ex(conn, "SELECT * FROM spider_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def delete_spider_run(run_id: str):
    with get_conn() as conn:
        _ex(conn, "DELETE FROM spider_runs WHERE id = ?", (run_id,))


def recover_orphan_spider_runs() -> int:
    """No boot, runs presas em 'running'/'needs_code' (o processo morreu/reiniciou)
    viram 'stopped' — assim a UI mostra o botão de continuar (o checkpoint já salvou
    linhas + cursor). Retorna quantas foram recuperadas."""
    with get_conn() as conn:
        cur = _ex(conn,
            "UPDATE spider_runs SET status = 'stopped' "
            "WHERE status IN ('running', 'needs_code')")
        try:
            return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        except Exception:
            return 0


# ── Seguir (follow_runs / histórico / limitador / blacklist) ──────────────────

def follow_run_upsert(d: dict):
    """Insere/atualiza uma execução do motor de follow (PK=id). queue_json carrega a
    fila inteira → dá para retomar depois de fechar o app."""
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO follow_runs (id, profile, profile_id, account_id, account_key, "
            "use_local, dry_run, source_type, source_target, status, error, followed, "
            "requested, skipped, failed, simulated, queue_json, settings_json, filters_json, "
            "blocked, stopped_by_user, started_at, finished_at, mode, unfollowed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET profile=excluded.profile, "
            "profile_id=excluded.profile_id, account_id=excluded.account_id, "
            "account_key=excluded.account_key, use_local=excluded.use_local, "
            "dry_run=excluded.dry_run, source_type=excluded.source_type, "
            "source_target=excluded.source_target, status=excluded.status, "
            "error=excluded.error, followed=excluded.followed, requested=excluded.requested, "
            "skipped=excluded.skipped, failed=excluded.failed, simulated=excluded.simulated, "
            "queue_json=excluded.queue_json, settings_json=excluded.settings_json, "
            "filters_json=excluded.filters_json, blocked=excluded.blocked, "
            "stopped_by_user=excluded.stopped_by_user, finished_at=excluded.finished_at, "
            "mode=excluded.mode, unfollowed=excluded.unfollowed",
            (d.get("id"), d.get("profile"), d.get("profile_id"), d.get("account_id"),
             d.get("account_key"), 1 if d.get("use_local") else 0, 1 if d.get("dry_run") else 0,
             d.get("source_type"), d.get("source_target"), d.get("status"), d.get("error"),
             int(d.get("followed") or 0), int(d.get("requested") or 0),
             int(d.get("skipped") or 0), int(d.get("failed") or 0), int(d.get("simulated") or 0),
             d.get("queue_json"), d.get("settings_json"), d.get("filters_json"),
             1 if d.get("blocked") else 0, 1 if d.get("stopped_by_user") else 0,
             d.get("started_at"), d.get("finished_at"),
             d.get("mode") or "follow", int(d.get("unfollowed") or 0)))


def list_follow_runs(limit: int = 50) -> list:
    """Histórico de execuções (SEM queue_json) — mais recentes primeiro."""
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT id, profile, account_key, source_type, source_target, status, error, "
            "dry_run, followed, requested, skipped, failed, simulated, blocked, "
            "stopped_by_user, started_at, finished_at, mode, unfollowed FROM follow_runs "
            "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_follow_run(run_id: str) -> dict:
    with get_conn() as conn:
        row = _ex(conn, "SELECT * FROM follow_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def delete_follow_run(run_id: str):
    with get_conn() as conn:
        _ex(conn, "DELETE FROM follow_runs WHERE id = ?", (run_id,))


def recover_orphan_follow_runs() -> int:
    """No boot, execuções presas em 'running' (o app morreu) viram 'stopped' — a fila
    está salva em queue_json, então dá para retomar."""
    with get_conn() as conn:
        cur = _ex(conn, "UPDATE follow_runs SET status = 'stopped' WHERE status = 'running'")
        try:
            return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        except Exception:
            return 0


def follow_history_map() -> dict:
    """{username: ts_ms} de tudo que já foi seguido — histórico GLOBAL."""
    with get_conn() as conn:
        rows = _ex(conn, "SELECT username, ts_ms FROM follow_history").fetchall()
    return {r["username"]: r["ts_ms"] for r in rows}


def follow_history_first_ts(by_account: str):
    """ts_ms do PRIMEIRO follow que esta conta fez. None se ela nunca seguiu ninguém.

    Serve para estimar a idade da conta quando o cadastro no app é posterior à
    criação dela (conta nascida no celular e cadastrada aqui depois)."""
    if not by_account:
        return None
    with get_conn() as conn:
        r = _ex(conn, "SELECT MIN(ts_ms) AS t FROM follow_history WHERE by_account = ?",
                (by_account,)).fetchone()
    return (r["t"] if r else None) or None


def add_follow_history(username: str, ts_ms: int, by_account: str = None, user_id: str = None,
                       source: str = None):
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO follow_history (username, ts_ms, by_account, followed_at, user_id, "
            "source, unfollowed_ts_ms) VALUES (?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT (username) DO UPDATE SET "
            "ts_ms=excluded.ts_ms, by_account=excluded.by_account, "
            "followed_at=excluded.followed_at, user_id=excluded.user_id, "
            "source=excluded.source, unfollowed_ts_ms=NULL",
            (username, int(ts_ms or 0), by_account, _now_str(), user_id, source))


def follow_history_export() -> list:
    """Histórico completo (1 linha por pessoa seguida) — base do CSV e das métricas."""
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT username, user_id, by_account, source, ts_ms, followed_at, "
            "unfollowed_ts_ms FROM follow_history ORDER BY ts_ms").fetchall()
    return [dict(r) for r in rows]


def follow_followback_candidates(by_account: str = "", limit: int = 500,
                                 revalidar_apos_ms: int = 0) -> list:
    """Quem checar para saber se retribuiu: seguidos que ainda seguimos.

    Prioriza NUNCA verificados (checked IS NULL) e, depois, os verificados ha mais
    tempo -- assim a primeira passada cobre o desconhecido antes de re-conferir
    quem ja se sabe. `revalidar_apos_ms` = so devolve re-checagem mais velha que
    isso (0 = re-checa qualquer um depois dos nao verificados).

    Exclui quem ja retribuiu: seguir de volta e um evento praticamente estavel, e
    re-checar todo mundo a cada rodada gastaria requisicao para confirmar o que ja
    se sabe. Quem deixar de seguir aparece na proxima checagem completa."""
    onde = ["unfollowed_ts_ms IS NULL", "followed_back_ts_ms IS NULL",
            "user_id IS NOT NULL", "user_id <> ''"]
    args = []
    if by_account:
        onde.append("by_account = ?")
        args.append(by_account)
    if revalidar_apos_ms:
        onde.append("(followback_checked_ms IS NULL OR followback_checked_ms < ?)")
        args.append(int(revalidar_apos_ms))
    args.append(int(limit or 500))
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT username, user_id, by_account FROM follow_history WHERE "
            + " AND ".join(onde)
            + " ORDER BY followback_checked_ms IS NULL DESC, followback_checked_ms ASC,"
              " ts_ms ASC LIMIT ?",
            tuple(args)).fetchall()
    return [dict(r) for r in rows]


def mark_followback(resultados: dict, checked_ms: int):
    """Grava o resultado da checagem. `resultados` = {username: retribuiu(bool)}.

    Marca TODOS como verificados; grava o carimbo de retribuicao so em quem
    retribuiu, e nao o apaga de quem ja tinha (o registro e de que aconteceu)."""
    if not resultados:
        return
    ts = int(checked_ms or 0)
    with get_conn() as conn:
        for username, retribuiu in resultados.items():
            u = str(username or "").strip().lower()
            if not u:
                continue
            if retribuiu:
                _ex(conn,
                    "UPDATE follow_history SET followback_checked_ms = ?, "
                    "followed_back_ts_ms = COALESCE(followed_back_ts_ms, ?) "
                    "WHERE username = ?", (ts, ts, u))
            else:
                _ex(conn,
                    "UPDATE follow_history SET followback_checked_ms = ? "
                    "WHERE username = ?", (ts, u))
        conn.commit()


def follow_followback_totals() -> dict:
    """Total geral de retribuicao, com o denominador honesto (so o que foi checado)."""
    with get_conn() as conn:
        r = _ex(conn,
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN followed_back_ts_ms IS NOT NULL THEN 1 ELSE 0 END) AS seguiram_de_volta, "
            "SUM(CASE WHEN followback_checked_ms IS NOT NULL THEN 1 ELSE 0 END) AS verificados "
            "FROM follow_history").fetchone()
    d = dict(r) if r else {}
    total = int(d.get("total") or 0)
    volta = int(d.get("seguiram_de_volta") or 0)
    verif = int(d.get("verificados") or 0)
    return {"seguiram_de_volta": volta, "verificados": verif,
            "nao_verificados": max(0, total - verif),
            # A taxa usa VERIFICADOS como denominador: dividir pelo total trataria
            # "ainda nao conferido" como "nao retribuiu" e subestimaria o resultado.
            "taxa_retorno_pct": round(volta * 100.0 / verif, 1) if verif else None}


def follow_metrics_by_account() -> list:
    """Agregado POR CONTA: seguidos, ainda seguindo (= o que conta no teto de 7.500),
    desfeitos, e quando foi a primeira/última ação."""
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT by_account, COUNT(*) AS seguidos, "
            "SUM(CASE WHEN unfollowed_ts_ms IS NULL THEN 1 ELSE 0 END) AS ainda_seguindo, "
            "SUM(CASE WHEN unfollowed_ts_ms IS NOT NULL THEN 1 ELSE 0 END) AS desfeitos, "
            "SUM(CASE WHEN followed_back_ts_ms IS NOT NULL THEN 1 ELSE 0 END) AS seguiram_de_volta, "
            "SUM(CASE WHEN followback_checked_ms IS NOT NULL THEN 1 ELSE 0 END) AS verificados, "
            "MIN(ts_ms) AS primeiro_ms, MAX(ts_ms) AS ultimo_ms "
            "FROM follow_history GROUP BY by_account ORDER BY seguidos DESC").fetchall()
    return [dict(r) for r in rows]


def follow_metrics_by_source(limit: int = 20) -> list:
    """Quanto cada alvo/fonte rendeu — qual alvo vale a pena repetir."""
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT COALESCE(source, '(sem origem)') AS source, COUNT(*) AS seguidos, "
            "SUM(CASE WHEN unfollowed_ts_ms IS NULL THEN 1 ELSE 0 END) AS ainda_seguindo, "
            "SUM(CASE WHEN followed_back_ts_ms IS NOT NULL THEN 1 ELSE 0 END) AS seguiram_de_volta, "
            "SUM(CASE WHEN followback_checked_ms IS NOT NULL THEN 1 ELSE 0 END) AS verificados "
            "FROM follow_history GROUP BY source ORDER BY seguidos DESC LIMIT ?",
            (int(limit or 20),)).fetchall()
    return [dict(r) for r in rows]


def follow_actions_counts(account_key: str, now_ms: int) -> dict:
    """Ações (follow+unfollow) desta conta na última hora e nas últimas 24h."""
    with get_conn() as conn:
        row = _ex(conn,
            "SELECT SUM(CASE WHEN ts_ms > ? THEN 1 ELSE 0 END) AS hora, "
            "COUNT(*) AS dia FROM follow_actions WHERE account_key = ? AND ts_ms > ?",
            (int(now_ms - 3600000), account_key, int(now_ms - 86400000))).fetchone()
    d = dict(row) if row else {}
    return {"hora": int(d.get("hora") or 0), "dia": int(d.get("dia") or 0)}


def follow_unfollow_candidates(by_account: str, older_than_ms: int, limit: int = 500) -> list:
    """Quem ESTA conta seguiu há tempo suficiente e ainda não foi desfeito.

    Filtra por `by_account` de propósito: quem segue X é a conta A, então só A pode
    deixar de seguir X — pedir isso à conta B não faria sentido nenhum.
    A whitelist ("nunca deixar de seguir") é aplicada aqui, no SQL."""
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT username, user_id, ts_ms, followed_at FROM follow_history "
            "WHERE by_account = ? AND unfollowed_ts_ms IS NULL AND ts_ms > 0 AND ts_ms <= ? "
            "AND username NOT IN (SELECT username FROM follow_whitelist) "
            "ORDER BY ts_ms LIMIT ?",                    # mais antigos primeiro
            (by_account, int(older_than_ms or 0), int(limit or 500))).fetchall()
    return [dict(r) for r in rows]


def mark_follow_unfollowed(username: str, ts_ms: int):
    with get_conn() as conn:
        _ex(conn, "UPDATE follow_history SET unfollowed_ts_ms = ? WHERE username = ?",
            (int(ts_ms or 0), username))


def follow_following_stats(by_account: str) -> dict:
    """Quantos esta conta seguiu pelo app e quantos ainda estão de pé."""
    with get_conn() as conn:
        row = _ex(conn,
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN unfollowed_ts_ms IS NULL THEN 1 ELSE 0 END) AS ativos "
            "FROM follow_history WHERE by_account = ?", (by_account,)).fetchone()
    d = dict(row) if row else {}
    return {"seguidos_pelo_app": int(d.get("total") or 0),
            "ainda_seguindo": int(d.get("ativos") or 0)}


def list_follow_whitelist() -> list:
    with get_conn() as conn:
        rows = _ex(conn, "SELECT username FROM follow_whitelist ORDER BY username").fetchall()
    return [r["username"] for r in rows]


def add_follow_whitelist(username: str):
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO follow_whitelist (username, added_at) VALUES (?, ?) "
            "ON CONFLICT (username) DO NOTHING", (username, _now_str()))


def delete_follow_whitelist(username: str):
    with get_conn() as conn:
        _ex(conn, "DELETE FROM follow_whitelist WHERE username = ?", (username,))


def follow_history_count() -> int:
    with get_conn() as conn:
        row = _ex(conn, "SELECT COUNT(*) AS n FROM follow_history").fetchone()
    return int(dict(row).get("n") or 0) if row else 0


def list_follow_actions(account_key: str, since_ms: int = 0) -> list:
    """Timestamps das ações da conta (para o limitador de janela deslizante)."""
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT ts_ms FROM follow_actions WHERE account_key = ? AND ts_ms > ? "
            "ORDER BY ts_ms", (account_key, int(since_ms or 0))).fetchall()
    return [int(r["ts_ms"]) for r in rows]


def follow_actions_resumo(since_ms: int = 0) -> dict:
    """{account_key: (quantas ações desde `since_ms`, ts da ÚLTIMA ação)}.

    Uma consulta só para TODAS as contas, em vez de uma por conta — serve para
    ordenar o rodízio pela conta mais descansada antes de começar."""
    # A contagem é da janela, mas o MAX é de SEMPRE — de propósito. Filtrar tudo pela
    # janela faria duas contas paradas há dias empatarem em (0, 0), e o desempate por
    # "quem está parada há mais tempo" se perderia justo entre as mais descansadas.
    with get_conn() as conn:
        rows = _ex(conn,
            "SELECT account_key, SUM(CASE WHEN ts_ms > ? THEN 1 ELSE 0 END) AS n, "
            "MAX(ts_ms) AS ult FROM follow_actions GROUP BY account_key",
            (int(since_ms or 0),)).fetchall()
    return {r["account_key"]: (int(r["n"] or 0), int(r["ult"] or 0)) for r in rows}


def add_follow_action(account_key: str, ts_ms: int):
    """Registra UMA ação da conta (alimenta o limitador).

    A PK é (account_key, ts_ms), então duas ações no MESMO milissegundo colidiriam e
    a segunda sumiria — o limitador contaria a menos, justamente o contador que
    protege a conta. Na prática o intervalo entre follows é de minutos, mas aqui o
    empate é resolvido deslocando 1ms, para a contagem nunca ficar devendo."""
    ts = int(ts_ms or 0)
    with get_conn() as conn:
        for _ in range(50):
            cur = _ex(conn,
                      "INSERT INTO follow_actions (account_key, ts_ms) VALUES (?, ?) "
                      "ON CONFLICT (account_key, ts_ms) DO NOTHING",
                      (account_key, ts))
            try:
                if cur.rowcount != 0:
                    return
            except Exception:
                return
            ts += 1


def clear_follow_actions(account_key: str = None):
    with get_conn() as conn:
        if account_key:
            _ex(conn, "DELETE FROM follow_actions WHERE account_key = ?", (account_key,))
        else:
            conn.execute("DELETE FROM follow_actions")


def list_follow_blacklist() -> list:
    with get_conn() as conn:
        rows = _ex(conn, "SELECT username FROM follow_blacklist ORDER BY username").fetchall()
    return [r["username"] for r in rows]


def add_follow_blacklist(username: str):
    with get_conn() as conn:
        _ex(conn,
            "INSERT INTO follow_blacklist (username, added_at) VALUES (?, ?) "
            "ON CONFLICT (username) DO NOTHING", (username, _now_str()))


def delete_follow_blacklist(username: str):
    with get_conn() as conn:
        _ex(conn, "DELETE FROM follow_blacklist WHERE username = ?", (username,))


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

