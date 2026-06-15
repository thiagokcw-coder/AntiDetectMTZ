"""
batch_import.py — importação de contas via CSV.

Cada linha do CSV vira (opcionalmente) um PERFIL novo + uma CONTA. O processamento
é SEQUENCIAL com intervalo entre linhas — criar várias contas em paralelo/rápido é
o caminho mais rápido para o Instagram detectar e banir tudo.

Colunas reconhecidas (cabeçalho flexível, pt/en):
  full_name | nome        (obrigatório p/ conta)
  email
  password  | senha
  username                 (opcional — gerado se vazio)
  proxy                    (opcional — ex.: http://user:pass@host:porta)
  os                       (opcional — windows|macos|linux|random)
  sms_key                  (opcional — chave sms-activate por linha)
  sms_country              (opcional)
  birthday  | nascimento   (opcional — MM/DD/AAAA)
"""
import asyncio
import re
import time
import unicodedata
import uuid
from urllib.parse import urlparse

# Nomes de mês → número (pt/en/es), para aceitar "Março", "march", etc.
_MONTH_NAMES = {
    1: ["january", "janeiro", "enero", "jan"],
    2: ["february", "fevereiro", "febrero", "feb", "fev"],
    3: ["march", "marco", "marzo", "mar"],
    4: ["april", "abril", "abr", "apr"],
    5: ["may", "maio", "mayo", "mai"],
    6: ["june", "junho", "junio", "jun"],
    7: ["july", "julho", "julio", "jul"],
    8: ["august", "agosto", "ago", "aug"],
    9: ["september", "setembro", "septiembre", "sep", "set"],
    10: ["october", "outubro", "octubre", "oct", "out"],
    11: ["november", "novembro", "noviembre", "nov"],
    12: ["december", "dezembro", "diciembre", "dec", "dez"],
}
_MONTH_TO_NUM = {name: num for num, names in _MONTH_NAMES.items() for name in names}

import profiles as prof
import session_manager as sm
import instagram as ig
import db

_jobs: dict[str, dict] = {}


def job_status(job_id: str):
    return _jobs.get(job_id)


def list_jobs() -> list:
    return sorted(_jobs.values(), key=lambda j: j.get("started", 0), reverse=True)


# ── Parsing ────────────────────────────────────────────────────────────────

def proxy_from_str(s: str):
    """
    Converte uma string de proxy em dict. Aceita URL (scheme://user:pass@host:porta),
    'host:porta' e o formato Webshare 'host:porta:usuario:senha'.
    """
    import proxies as _pool
    return _pool.parse_proxy_line(s)


def _g(row: dict, *keys):
    """Pega o primeiro valor não-vazio dentre as chaves (case-insensitive)."""
    low = {k.lower().strip(): v for k, v in row.items()}
    for k in keys:
        v = low.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return None


def _month_num(v) -> "int|None":
    if not v:
        return None
    v = str(v).strip().lower()
    if v.isdigit():
        n = int(v)
        return n if 1 <= n <= 12 else None
    v = "".join(c for c in unicodedata.normalize("NFD", v) if unicodedata.category(c) != "Mn")
    return _MONTH_TO_NUM.get(v) or _MONTH_TO_NUM.get(v[:3])


def _parse_birthday(row: dict) -> "dict|None":
    """Aceita colunas separadas (dia/mes/ano, mês por nome) ou data combinada."""
    dia = _g(row, "dia", "day", "dia_nascimento")
    mes = _g(row, "mes", "mês", "month", "mes_nascimento")
    ano = _g(row, "ano", "year", "ano_nascimento")
    if dia and mes and ano:
        mn = _month_num(mes)
        if mn and dia.isdigit() and ano.isdigit():
            y = int(ano)
            return {"month": mn, "day": int(dia), "year": y if y > 100 else 1900 + y}

    raw = _g(row, "birthday", "nascimento", "data_nascimento", "data nascimento")
    if raw:
        parts = re.findall(r"\d+", raw)
        if len(parts) >= 3:
            a, b, c = int(parts[0]), int(parts[1]), int(parts[2])
            if a > 31:
                # ISO: AAAA-MM-DD
                year, month, day = a, b, c
            else:
                # >12 indica dia. Padrão BR = DD/MM/AAAA.
                if a > 12 and b <= 12:
                    day, month = a, b
                elif b > 12 and a <= 12:
                    month, day = a, b
                else:
                    day, month = a, b
                year = c if c > 100 else 1900 + c
            if 1 <= month <= 12 and 1 <= day <= 31:
                return {"month": month, "day": day, "year": year}
    return None


def normalize_row(row: dict) -> dict:
    bd = _parse_birthday(row)
    return {
        "full_name":   _g(row, "full_name", "fullname", "nome", "name"),
        "email":       _g(row, "email", "e-mail"),
        "password":    _g(row, "password", "senha", "pass"),
        "username":    _g(row, "username", "usuario", "user"),
        "proxy":       _g(row, "proxy"),
        "os":          _g(row, "os", "sistema") or "random",
        "sms_key":     _g(row, "sms_key", "sms_activate_key", "smskey"),
        "sms_country": _g(row, "sms_country", "pais", "country"),
        "birthday":    bd,
    }


def validate_rows(rows: list) -> dict:
    """Valida linhas normalizadas; devolve {ok, total, valid, problems}."""
    problems = []
    valid = 0
    for i, r in enumerate(rows):
        errs = []
        if not r.get("full_name"):
            errs.append("nome ausente")
        if not r.get("email"):
            errs.append("email ausente")
        if not r.get("password"):
            errs.append("senha ausente")
        if r.get("proxy") and proxy_from_str(r["proxy"]) is None:
            errs.append("proxy inválido")
        if errs:
            problems.append({"row": i + 1, "errors": errs})
        else:
            valid += 1
    return {"ok": not problems, "total": len(rows), "valid": valid, "problems": problems}


# ── Worker ─────────────────────────────────────────────────────────────────

async def _ensure_page(profile: dict, url: str):
    pid = profile["id"]
    await sm.launch(profile, url=url, headless=False, mode="controlled", load_extension=False)
    for _ in range(60):
        await asyncio.sleep(0.5)
        page = sm._sessions.get(pid, {}).get("page")
        if page:
            return page
    return None


def start_job(rows: list, options: dict, now_ts: float) -> str:
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "total": len(rows),
        "done": 0,
        "created_profiles": 0,
        "created_accounts": 0,
        "errors": 0,
        "started": now_ts,
        "options": {k: options.get(k) for k in ("create_accounts", "delay_seconds", "warm_profile_minutes")},
        "rows": [
            {"row": i + 1, "label": r.get("full_name") or r.get("username") or r.get("email") or f"linha {i+1}",
             "status": "pending", "detail": ""}
            for i, r in enumerate(rows)
        ],
    }
    asyncio.create_task(_run(job_id, rows, options))
    return job_id


async def _run(job_id: str, rows: list, options: dict):
    job = _jobs[job_id]
    job["status"] = "running"
    delay = float(options.get("delay_seconds") or 30)
    create_accounts = bool(options.get("create_accounts"))
    warm_minutes = float(options.get("warm_profile_minutes") or 0)
    default_sms = options.get("sms_activate_key")
    default_country = int(options.get("sms_country") or 73)
    use_pool = bool(options.get("use_proxy_pool"))

    import profile_warmup as pw
    import proxies as _pool

    # Pré-distribui proxies do pool para as linhas que não têm proxy próprio
    pool_assign = []
    if use_pool:
        import db as _db
        limit = int(_db.get_setting("proxy_limit", "3") or 0)
        need = sum(1 for r in rows if not r.get("proxy"))
        pool_assign = _pool.assign(need, limit)
    pool_idx = 0

    for i, row in enumerate(rows):
        rstat = job["rows"][i]
        rstat["status"] = "processing"
        try:
            proxy = proxy_from_str(row.get("proxy"))
            if proxy is None and use_pool:
                proxy = pool_assign[pool_idx] if pool_idx < len(pool_assign) else None
                pool_idx += 1
                if proxy is None:
                    rstat["detail"] = "sem proxy livre no pool"
            name = row.get("full_name") or row.get("username") or f"import-{i+1}"
            profile = prof.create(name=name, os=row.get("os") or "random", proxy=proxy)
            job["created_profiles"] += 1

            # Aquecimento opcional do perfil antes de criar a conta
            if warm_minutes > 0:
                rstat["detail"] = "aquecendo perfil"
                locale = profile["fingerprint"].get("language", "")
                try:
                    res = await sm.run_profile_task(
                        profile, lambda pg: pw.browse(pg, warm_minutes, locale), headless=False)
                    if res and res.get("visited", 0) > 0:
                        db.mark_profile_warmed(profile["id"])
                except Exception:
                    pass

            if create_accounts:
                rstat["detail"] = "criando conta"
                page = await _ensure_page(profile, "https://www.instagram.com/accounts/emailsignup/")
                if not page:
                    raise RuntimeError("não foi possível abrir o navegador")
                data = {
                    "email": row.get("email"),
                    "password": row.get("password"),
                    "full_name": row.get("full_name"),
                    "username": row.get("username"),
                    "birthday": row.get("birthday"),
                    "sms_activate_key": row.get("sms_key") or default_sms,
                    "sms_country": int(row.get("sms_country") or default_country),
                }
                result = await ig.create_account(page, profile["id"], data)
                job["created_accounts"] += 1
                rstat["status"] = "done"
                rstat["detail"] = result.get("status", "criada") if isinstance(result, dict) else "criada"
                # Fecha a sessão antes de ir para a próxima linha
                try:
                    await sm.stop(profile["id"])
                except Exception:
                    pass
            else:
                rstat["status"] = "done"
                rstat["detail"] = "perfil criado"
        except Exception as e:
            rstat["status"] = "error"
            rstat["detail"] = str(e)[:120]
            job["errors"] += 1

        job["done"] = i + 1
        # Intervalo entre linhas (não dorme após a última)
        if i < len(rows) - 1 and (create_accounts or warm_minutes > 0):
            await asyncio.sleep(delay)

    job["status"] = "completed"
