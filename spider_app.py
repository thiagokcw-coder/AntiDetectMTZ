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

from paths import APP_DIR

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


def _friendship_loop(cl, edge, uid, uname, limit, progress, should_stop, log, start_max_id, seed_rows):
    rows = list(seed_rows or [])
    seen = {str(r.get("User Id")) for r in rows if r.get("User Id")}
    max_id = start_max_id or ""
    cursor = max_id or None
    page_n = 0
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
                    log(f"✓ limite de {limit} atingido — total {len(rows)}")
                return rows, (max_id or None)
        page_n += 1
        if log:
            log(f"página {page_n}: +{len(rows) - before} (total {len(rows)})")
        if progress:
            progress(len(rows))
        if not max_id:
            if log:
                log(f"✓ fim da lista — total {len(rows)}")
            return rows, None
        cursor = max_id
    return rows, cursor


def _posts_loop(cl, uid, uname, limit, progress, should_stop, log, start_max_id, seed_rows):
    rows = list(seed_rows or [])
    seen = {str(r.get("Shortcode")) for r in rows if r.get("Shortcode")}
    end_cursor = start_max_id or ""
    cursor = end_cursor or None
    page_n = 0
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
                    log(f"✓ limite de {limit} atingido — total {len(rows)}")
                return rows, (end_cursor or None)
        page_n += 1
        if log:
            log(f"página {page_n}: +{len(rows) - before} (total {len(rows)})")
        if progress:
            progress(len(rows))
        if not end_cursor:
            if log:
                log(f"✓ fim dos posts — total {len(rows)}")
            return rows, None
        cursor = end_cursor
    return rows, cursor


def _extract_sync(account, kind, target, limit, progress, should_stop, code_provider, status,
                  use_local, log, start_max_id, seed_rows):
    """Loga uma vez e despacha por tipo. Retorna (rows, cursor)."""
    if log:
        log("logando na conta (instagrapi)…")
    cl = _client(account, code_provider, status, use_local)
    if status:
        status("extraindo")
    uname = (target or "").strip().lstrip("@").lower()
    if log:
        log(f"resolvendo @{uname}…")
    uid = cl.user_id_from_username(uname)
    if log:
        log((f"retomando: {len(seed_rows or [])} já coletado(s) — " if seed_rows else "")
            + f"alvo @{uname} (id {uid}) — iniciando extração…")
    if kind in ("followers", "following"):
        return _friendship_loop(cl, kind, uid, uname, limit, progress, should_stop, log, start_max_id, seed_rows)
    if kind == "posts":
        return _posts_loop(cl, uid, uname, limit, progress, should_stop, log, start_max_id, seed_rows)
    return list(seed_rows or []), None


async def extract(account: dict, kind: str, target: str, limit: int = 0,
                  progress=None, should_stop=None, code_provider=None, status=None,
                  use_local=False, log=None, start_max_id=None, seed_rows=None) -> dict:
    """Extrai via instagrapi: followers/following/posts. Suporta resume."""
    if kind not in ("followers", "following", "posts"):
        return {"error": f"tipo '{kind}' não suportado no modo app (use followers/following/posts)"}
    loop = asyncio.get_event_loop()
    try:
        rows, cursor = await loop.run_in_executor(
            None, _extract_sync, account, kind, target, limit, progress, should_stop,
            code_provider, status, use_local, log, start_max_id, seed_rows)
        return {"rows": rows, "cursor": cursor, "target": (target or "").lstrip("@"), "kind": kind}
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
