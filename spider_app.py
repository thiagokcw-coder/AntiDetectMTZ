"""
spider_app.py — extração via instagrapi (API MOBILE privada do Instagram).

Diferente do spider.py (que raspa a API WEB via fetch dentro de um perfil Camoufox
logado), aqui usamos a biblioteca instagrapi: loga emulando o app oficial com
usuário/senha da CONTA, roteado pelo proxy da conta, com a sessão salva em disco
(evita re-login). Roda no backend Python (sem navegador) — chamadas bloqueantes
são executadas num executor.

Trade-off (ver CLAUDE.md): superfície mobile é mais rica/menos vigiada, mas o login
programático é um evento de "novo dispositivo" — use em contas DESCARTÁVEIS de
scraping, com device/IP coerentes e ritmo lento.
"""
import asyncio
import random
import time

from paths import APP_DIR


def _human_delay_s(delay_ms) -> float:
    """Variância humana trimodal (scan/leitura/distraído) — igual ao reference.
    Capped igual ao web spider para evitar páginas de vários minutos no 'distraído'."""
    base = max(0.3, (delay_ms or 2500) / 1000.0)
    r = random.random()
    if r < 0.55:
        f = 0.55 + random.random() * 0.60
    elif r < 0.87:
        f = 1.00 + random.random() * 1.00
    else:
        f = 2.00 + random.random() * 4.00
    # Mesmo cap do spider web: evita 6x em presets rápidos virar minutos por página.
    return round(base * min(f, 2.5), 2)


def _max_pages_for(delay_ms) -> int:
    d = delay_ms or 2500
    if d >= 100000:
        return 8
    if d >= 30000:
        return 20
    if d >= 10000:
        return 100
    if d >= 6000:
        return 400
    return 1600


# A cada quantas páginas gravar um checkpoint (linhas + cursor) no banco — assim um
# reinício/crash não perde o progresso e a run pode CONTINUAR do cursor salvo.
_CHECKPOINT_EVERY = 10


def _page_sleep(delay_ms, page_n, next_pause_ref, log):
    """Pausa entre páginas (variância humana) + pausa de proteção periódica. Sync."""
    time.sleep(_human_delay_s(delay_ms))
    if page_n >= next_pause_ref[0]:
        pause = round(random.uniform(4, 12), 1)
        if log:
            log(f"🛡️ pausa de proteção: {pause}s")
        time.sleep(pause)
        next_pause_ref[0] = page_n + random.randint(25, 45)


def _session_cooldown(session_ref, max_pages, should_stop, log):
    """Cooldown longo (10–25 min) a cada max_pages páginas. Sync, interrompível."""
    session_ref[0] += 1
    if session_ref[0] < max_pages:
        return
    session_ref[0] = 0
    secs = random.randint(180, 480)  # 3-8 min (reduzido)
    if log:
        log(f"🧊 cooldown de sessão: ~{secs // 60} min (deixa a conta descansar)…")
    waited = 0
    while waited < secs:
        if should_stop and should_stop():
            return
        time.sleep(min(20, secs - waited))
        waited += 20
    if log:
        log("▶ retomando após o cooldown")


SESS_DIR = APP_DIR / "ig_sessions"

FOLLOWERS_HEADERS = ["Username", "User Id", "Full Name", "Verified", "Private",
                     "Profile URL", "Avatar URL"]


def _proxy_url(proxy: dict):
    if not proxy or not proxy.get("host"):
        return None
    scheme = (proxy.get("protocol") or "http").replace("https", "http")
    auth = ""
    if proxy.get("username"):
        auth = f"{proxy['username']}:{proxy.get('password','')}@"
    return f"{scheme}://{auth}{proxy['host']}:{proxy['port']}"


def _client(account: dict, code_provider=None, status=None, use_local=False):
    """Cria/loga um Client instagrapi para a conta, reaproveitando a sessão salva.

    - code_provider(username, choice) -> str: chamado pelo instagrapi quando o IG
      pede CÓDIGO de confirmação (checkpoint de "novo aparelho"). Deve BLOQUEAR até
      o usuário fornecer o código (vem da UI) e retorná-lo.
    - status(estado): callback p/ a UI saber a etapa ('logando'/'extraindo'/...).
    - use_local=True: NÃO usa o proxy da conta — sai pela conexão local do PC (ex.:
      tethering do celular = IP coerente com onde a conta vive → menos desafio).
    """
    from instagrapi import Client
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    username = (account.get("username") or "").strip()
    password = account.get("password") or ""
    if not username or not password:
        raise ValueError("conta sem usuário/senha cadastrados")
    sess = SESS_DIR / f"{account['id']}.json"
    cl = Client()
    cl.delay_range = [1, 3]                       # pausa humana entre chamadas
    if code_provider:
        cl.challenge_code_handler = code_provider   # resolve checkpoint por código
    pu = None if use_local else _proxy_url(account.get("proxy"))
    if pu:
        cl.set_proxy(pu)
    if sess.exists():
        try:
            cl.load_settings(str(sess))
        except Exception:
            pass
    if status:
        status("logando")
    cl.login(username, password)                  # usa a sessão salva se válida
    cl.dump_settings(str(sess))
    return cl


def _row(u) -> dict:
    uname = getattr(u, "username", "") or ""
    return {
        "Username": uname,
        "User Id": str(getattr(u, "pk", "") or ""),
        "Full Name": getattr(u, "full_name", "") or "",
        "Verified": bool(getattr(u, "is_verified", False)),
        "Private": bool(getattr(u, "is_private", False)),
        "Profile URL": f"https://www.instagram.com/{uname}/" if uname else "",
        "Avatar URL": str(getattr(u, "profile_pic_url", "") or ""),
    }


def _post_row_app(m) -> dict:
    code = getattr(m, "code", "") or ""
    ts = getattr(m, "taken_at", None)
    when = ""
    try:
        if ts:
            when = ts.strftime("%Y-%m-%d %H:%M")
    except Exception:
        when = str(ts or "")
    mt = getattr(m, "media_type", None)
    return {
        "Shortcode": code,
        "URL": f"https://www.instagram.com/p/{code}/" if code else "",
        "Caption": (getattr(m, "caption_text", "") or "").replace("\n", " ").strip(),
        "Likes": getattr(m, "like_count", 0) or 0,
        "Comments": getattr(m, "comment_count", 0) or 0,
        "Views": getattr(m, "view_count", 0) or "",
        "Taken At": when,
        "Media Type": {1: "foto", 2: "vídeo", 8: "carrossel"}.get(mt, mt),
        "Product Type": getattr(m, "product_type", "") or "",
    }


def _friendship_loop(cl, edge, uid, uname, limit, progress, should_stop, log, start_max_id,
                     seed_rows, delay_ms=2500, total=None, checkpoint=None):
    rows = list(seed_rows or [])
    seen = {str(r.get("User Id")) for r in rows if r.get("User Id")}
    max_id = start_max_id or ""
    cursor = max_id or None
    page_n = 0
    next_pause = [random.randint(25, 45)]
    max_pages = _max_pages_for(delay_ms)
    session_ref = [0]
    _tot = (lambda n: f"{n}/{total}" if total else str(n))
    while True:
        if should_stop and should_stop():
            if log:
                log("⏹ parado pelo usuário")
            return rows, (max_id or None)
        if edge == "following":
            chunk, max_id = cl.user_following_v1_chunk(uid, max_amount=0, max_id=max_id)
        else:
            chunk, max_id = cl.user_followers_v1_chunk(uid, max_amount=0, max_id=max_id)
        before = len(rows)
        for u in chunk:
            pk = str(getattr(u, "pk", "") or "")
            if not pk or pk in seen:
                continue
            seen.add(pk)
            rows.append(_row(u))
            if limit and len(rows) >= limit:
                if progress:
                    progress(len(rows))
                if log:
                    log(f"✓ limite de {limit} atingido — {_tot(len(rows))}")
                return rows, (max_id or None)
        page_n += 1
        if log:
            log(f"página {page_n}: +{len(rows) - before} ({_tot(len(rows))})")
        if progress:
            progress(len(rows))
        if not max_id:
            if log:
                log(f"✓ fim da lista — {_tot(len(rows))}")
            return rows, None
        cursor = max_id
        if checkpoint and page_n % _CHECKPOINT_EVERY == 0:   # resume-safe num crash
            checkpoint(rows, cursor)
        _page_sleep(delay_ms, page_n, next_pause, log)
        _session_cooldown(session_ref, max_pages, should_stop, log)
    return rows, cursor


def _posts_loop(cl, uid, uname, limit, progress, should_stop, log, start_max_id, seed_rows,
                delay_ms=2500, total=None, checkpoint=None):
    rows = list(seed_rows or [])
    seen = {str(r.get("Shortcode")) for r in rows if r.get("Shortcode")}
    end_cursor = start_max_id or ""
    cursor = end_cursor or None
    page_n = 0
    next_pause = [random.randint(25, 45)]
    max_pages = _max_pages_for(delay_ms)
    session_ref = [0]
    _tot = (lambda n: f"{n}/{total}" if total else str(n))
    while True:
        if should_stop and should_stop():
            if log:
                log("⏹ parado pelo usuário")
            return rows, (end_cursor or None)
        medias, end_cursor = cl.user_medias_paginated_v1(uid, amount=33, end_cursor=end_cursor)
        before = len(rows)
        for m in medias:
            code = str(getattr(m, "code", "") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            rows.append(_post_row_app(m))
            if limit and len(rows) >= limit:
                if progress:
                    progress(len(rows))
                if log:
                    log(f"✓ limite de {limit} atingido — {_tot(len(rows))}")
                return rows, (end_cursor or None)
        page_n += 1
        if log:
            log(f"página {page_n}: +{len(rows) - before} ({_tot(len(rows))})")
        if progress:
            progress(len(rows))
        if not end_cursor:
            if log:
                log(f"✓ fim dos posts — {_tot(len(rows))}")
            return rows, None
        cursor = end_cursor
        if checkpoint and page_n % _CHECKPOINT_EVERY == 0:   # resume-safe num crash
            checkpoint(rows, cursor)
        _page_sleep(delay_ms, page_n, next_pause, log)
        _session_cooldown(session_ref, max_pages, should_stop, log)
    return rows, cursor


def _extract_sync(account, kind, target, limit, progress, should_stop, code_provider, status,
                  use_local, log, start_max_id, seed_rows, delay_ms, on_total, checkpoint=None):
    """Loga uma vez e despacha por tipo. Retorna (rows, cursor, total)."""
    if log:
        _who = (account.get("username") or account.get("id") or "conta")
        _conn = "IP local (sem proxy)" if use_local else "proxy da conta"
        log(f"🧭 raspando com a conta: @{_who} · {_conn}")
        log("logando na conta (instagrapi)…")
    cl = _client(account, code_provider, status, use_local)
    if status:
        status("extraindo")
    uname = (target or "").strip().lstrip("@").lower()
    if log:
        log(f"resolvendo @{uname}…")
    uid = cl.user_id_from_username(uname)
    total = None
    try:
        ui = cl.user_info(uid)
        total = {"followers": getattr(ui, "follower_count", None),
                 "following": getattr(ui, "following_count", None),
                 "posts": getattr(ui, "media_count", None)}.get(kind)
    except Exception:
        total = None
    if on_total and total is not None:
        on_total(total)
    if log:
        log((f"retomando: {len(seed_rows or [])} já coletado(s) — " if seed_rows else "")
            + f"alvo @{uname} (id {uid})"
            + (f" — {total} {kind}" if total is not None else "") + " — iniciando extração…")
    if kind in ("followers", "following"):
        rows, cursor = _friendship_loop(cl, kind, uid, uname, limit, progress, should_stop, log,
                                        start_max_id, seed_rows, delay_ms, total=total,
                                        checkpoint=checkpoint)
    elif kind == "posts":
        rows, cursor = _posts_loop(cl, uid, uname, limit, progress, should_stop, log, start_max_id,
                                   seed_rows, delay_ms, total=total, checkpoint=checkpoint)
    else:
        rows, cursor = list(seed_rows or []), None
    return rows, cursor, total


async def extract(account: dict, kind: str, target: str, limit: int = 0,
                  progress=None, should_stop=None, code_provider=None, status=None,
                  use_local=False, log=None, start_max_id=None, seed_rows=None,
                  delay_ms=2500, on_total=None, checkpoint=None) -> dict:
    """Extrai via instagrapi: followers/following/posts. Suporta resume + velocidade."""
    if kind not in ("followers", "following", "posts"):
        return {"error": f"tipo '{kind}' não suportado no modo app (use followers/following/posts)"}
    loop = asyncio.get_event_loop()
    try:
        rows, cursor, total = await loop.run_in_executor(
            None, _extract_sync, account, kind, target, limit, progress, should_stop,
            code_provider, status, use_local, log, start_max_id, seed_rows, delay_ms, on_total,
            checkpoint)
        return {"rows": rows, "cursor": cursor, "total": total,
                "target": (target or "").lstrip("@"), "kind": kind}
    except Exception as e:
        name = type(e).__name__
        if "Challenge" in name or "challenge_required" in str(e):
            return {"error": "O Instagram exigiu verificação que o instagrapi não resolve automaticamente "
                             "(fluxo novo / IP suspeito). Tente: (1) marcar 'usar conexão local' (IP do celular); "
                             "(2) se persistir, use o método WEB (loga no Camoufox e resolve o desafio na tela)."}
        if "TwoFactor" in name or "two_factor" in str(e).lower():
            return {"error": "A conta tem verificação em duas etapas (2FA). O modo app ainda não trata 2FA — use o método Web."}
        if "Login" in name or "BadPassword" in name or "bad_password" in str(e).lower():
            return {"error": "Usuário ou senha incorretos para esta conta."}
        return {"error": f"instagrapi: {e}"}
