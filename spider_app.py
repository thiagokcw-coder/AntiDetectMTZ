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

PARIDADE COM O WEB (spider.py): paginação por GraphQL (NÃO capa em ~50 como a v1
/friendships/), retry de rate limit/rede/bloqueio temporário, sinalização de
bloqueio (meta["blocked"]), sleep interrompível (Parar responde na hora) e
shift_pages (turno do handoff/rodízio).
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


def _sleep_stop(secs, should_stop, step: float = 0.4) -> bool:
    """Sleep SÍNCRONO interrompível (roda no executor): dorme em passos curtos e sai
    assim que should_stop() vira True. Retorna True se foi interrompido. Espelha o
    _sleep_stop do spider web — sem isto, o botão Parar só era visto depois do sleep
    longo entre páginas terminar."""
    waited = 0.0
    while waited < secs:
        if should_stop and should_stop():
            return True
        time.sleep(min(step, secs - waited))
        waited += step
    return bool(should_stop and should_stop())


def _classify_error(exc) -> str:
    """Classifica uma exceção do instagrapi no MESMO tratamento do Web:
    'rate' (429/espere alguns minutos) · 'net' (rede/timeout/JSON/5xx — transitório)
    · 'block' (bloqueio/desafio possivelmente TEMPORÁRIO — qualquer outra coisa).
    O login/senha/2FA acontece ANTES do loop (em _client) e é tratado no extract."""
    try:
        from instagrapi import exceptions as ig
    except Exception:
        return "block"
    if isinstance(exc, (ig.RateLimitError, ig.PleaseWaitFewMinutes, ig.ClientThrottledError)):
        return "rate"
    if isinstance(exc, (ig.ClientConnectionError, ig.ClientRequestTimeout,
                        ig.ClientIncompleteReadError, ig.ClientJSONDecodeError,
                        ig.GenericRequestError, ig.ClientUnknownError)):
        return "net"
    # ChallengeError, FeedbackRequired, ClientForbiddenError, ClientUnauthorizedError,
    # ClientBadRequestError, ProxyAddressIsBlocked, SentryBlock, ClientGraphqlError,
    # LoginRequired e qualquer outra → possível bloqueio TEMPORÁRIO (a conta às vezes
    # volta sozinha): espera e re-tenta o MESMO cursor; só marca suspensa se não voltar.
    return "block"


def _page_sleep(delay_ms, page_n, next_pause_ref, log, should_stop):
    """Pausa entre páginas (variância humana) + pausa de proteção periódica.
    Interrompível pelo Parar."""
    _sleep_stop(_human_delay_s(delay_ms), should_stop)
    if page_n >= next_pause_ref[0]:
        pause = round(random.uniform(4, 12), 1)
        if log:
            log(f"🛡️ pausa de proteção: {pause}s")
        _sleep_stop(pause, should_stop)
        next_pause_ref[0] = page_n + random.randint(25, 45)


def _session_cooldown(session_ref, max_pages, should_stop, log, on_pause=None):
    """Conta páginas; a cada max_pages faz um cooldown longo (3-8 min), interrompível.
    on_pause() é chamado ANTES da pausa longa (p/ gravar um checkpoint resume-safe)."""
    session_ref[0] += 1
    if session_ref[0] < max_pages:
        return
    session_ref[0] = 0
    if on_pause:
        try:
            on_pause()
        except Exception:
            pass
    secs = random.randint(180, 480)  # 3-8 min
    if log:
        log(f"🧊 cooldown de sessão: ~{secs // 60} min (deixa a conta descansar)…")
    if _sleep_stop(secs, should_stop):
        return
    if log:
        log("▶ retomando após o cooldown")


SESS_DIR = APP_DIR / "ig_sessions"

# SEM "Avatar URL" (igual ao Web): URL de avatar é gigante e temporária; só usamos
# Username + Nome (+ Profile URL leve).
FOLLOWERS_HEADERS = ["Username", "User Id", "Full Name", "Verified", "Private",
                     "Profile URL"]


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


def _fetch_friendship_page(cl, edge, uid, cursor):
    """UMA requisição = UMA página (para conseguirmos inserir delays/checkpoints/
    cooldown entre páginas). Retorna (chunk de UserShort, end_cursor).

    Usa a GraphQL PÚBLICA (web) — `user_followers_gql_chunk`/`user_following_gql_chunk`,
    mesmo query_hash que o método Web usa pra puxar MILHÕES. Validado ao vivo: a API v1
    `/friendships/followers/` E a GraphQL PRIVADA mobile (`*_private_gql_chunk`) capam em
    ~48-50 pra quem não é DONO da lista; só a GraphQL pública pagina fundo (com a conta
    SEGUINDO o alvo). `max_amount=1` força UMA requisição por chamada — senão a lib
    acumularia tudo num loop interno, sem as nossas pausas/checkpoints entre páginas."""
    if edge == "following":
        return cl.user_following_gql_chunk(uid, max_amount=1, end_cursor=cursor or None)
    return cl.user_followers_gql_chunk(uid, max_amount=1, end_cursor=cursor or None)


def _friendship_loop(cl, edge, uid, uname, limit, progress, should_stop, log, start_max_id,
                     seed_rows, delay_ms=2500, total=None, checkpoint=None,
                     shift_pages=None, meta=None):
    rows = list(seed_rows or [])
    seen = {str(r.get("User Id")) for r in rows if r.get("User Id")}
    cursor = start_max_id or None
    page_n = 0
    rate_hits = net_hits = block_hits = 0
    next_pause = [random.randint(25, 45)]
    max_pages = _max_pages_for(delay_ms)
    session_ref = [0]
    _tot = (lambda n: f"{n}/{total}" if total else str(n))
    if log:
        # Diagnóstico: deixa explícito qual paginador está em uso (e prova que o código
        # NOVO está carregado — se esta linha não aparece, a app não foi reiniciada).
        log(f"📄 paginando {edge} via GraphQL pública (web, sem cap)")
    if seed_rows and log:
        log(f"retomando: {len(rows)} já coletado(s), seguindo do cursor salvo…")
    while True:
        if should_stop and should_stop():
            if log:
                log("⏹ parado pelo usuário")
            return rows, cursor
        try:
            chunk, next_cursor = _fetch_friendship_page(cl, edge, uid, cursor)
        except Exception as e:
            cls = _classify_error(e)
            if cls == "rate":
                rate_hits += 1
                if log:
                    log(f"⚠ rate limit ({type(e).__name__}) — aguardando 60s [{rate_hits}/5]")
                if rate_hits > 5:
                    if log:
                        log("✖ muitas vezes em rate limit — encerrando")
                    return rows, cursor
                _sleep_stop(60, should_stop)
                continue
            if cls == "net":
                net_hits += 1
                wait = min(60, 15 * net_hits)
                if log:
                    log(f"⚠ falha de rede ({type(e).__name__}: {str(e)[:60]}) — "
                        f"tentando de novo em {wait}s [{net_hits}/5]")
                if net_hits > 5:
                    if log:
                        log("✖ rede instável demais — encerrando (dá pra continuar)")
                    return rows, cursor
                _sleep_stop(wait, should_stop)
                continue
            # block: bloqueio/desafio possivelmente TEMPORÁRIO — espera e re-tenta o
            # MESMO cursor algumas vezes; só desiste (e marca suspensa) se não voltar.
            block_hits += 1
            if block_hits <= 3:
                wait = min(300, 120 * block_hits)
                if log:
                    log(f"⚠ resposta inesperada ({type(e).__name__}: {str(e)[:60]}) — possível "
                        f"bloqueio TEMPORÁRIO. Esperando {wait}s e verificando se a conta volta "
                        f"[{block_hits}/3]…")
                if _sleep_stop(wait, should_stop):
                    return rows, cursor
                continue
            if log:
                log("✖ a conta não voltou após 3 tentativas — encerrando "
                    "(dá pra continuar de onde parou).")
            if meta is not None:
                meta["blocked"] = True   # sinaliza p/ marcar a conta como suspensa
            return rows, cursor
        rate_hits = net_hits = block_hits = 0   # página OK → zera os contadores
        before = len(rows)
        for u in (chunk or []):
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
                return rows, (next_cursor or None)
        page_n += 1
        if log:
            log(f"página {page_n}: +{len(rows) - before} ({_tot(len(rows))})")
        if progress:
            progress(len(rows))
        cursor = next_cursor or None
        if not cursor:
            if log:
                log(f"✓ fim da lista — {_tot(len(rows))}")
            return rows, None
        if checkpoint and page_n % _CHECKPOINT_EVERY == 0:   # resume-safe num crash
            checkpoint(rows, cursor)
        # Rodízio/handoff multi-conta: fim do turno → devolve o cursor p/ a próxima conta.
        if shift_pages and page_n >= shift_pages:
            if log:
                log(f"⏸ turno de {shift_pages} páginas concluído — passando p/ a próxima conta")
            return rows, cursor
        _page_sleep(delay_ms, page_n, next_pause, log, should_stop)
        _session_cooldown(session_ref, max_pages, should_stop, log,
                          on_pause=((lambda: checkpoint(rows, cursor)) if checkpoint else None))
    return rows, cursor


def _posts_loop(cl, uid, uname, limit, progress, should_stop, log, start_max_id, seed_rows,
                delay_ms=2500, total=None, checkpoint=None, shift_pages=None, meta=None):
    rows = list(seed_rows or [])
    seen = {str(r.get("Shortcode")) for r in rows if r.get("Shortcode")}
    end_cursor = start_max_id or ""
    cursor = end_cursor or None
    page_n = 0
    rate_hits = net_hits = block_hits = 0
    next_pause = [random.randint(25, 45)]
    max_pages = _max_pages_for(delay_ms)
    session_ref = [0]
    _tot = (lambda n: f"{n}/{total}" if total else str(n))
    if seed_rows and log:
        log(f"retomando: {len(rows)} post(s) já coletado(s)…")
    while True:
        if should_stop and should_stop():
            if log:
                log("⏹ parado pelo usuário")
            return rows, (end_cursor or None)
        try:
            medias, end_cursor = cl.user_medias_paginated_v1(uid, amount=33, end_cursor=end_cursor or "")
        except Exception as e:
            cls = _classify_error(e)
            if cls == "rate":
                rate_hits += 1
                if log:
                    log(f"⚠ rate limit ({type(e).__name__}) — aguardando 60s [{rate_hits}/5]")
                if rate_hits > 5:
                    if log:
                        log("✖ muitas vezes em rate limit — encerrando")
                    return rows, cursor
                _sleep_stop(60, should_stop)
                continue
            if cls == "net":
                net_hits += 1
                wait = min(60, 15 * net_hits)
                if log:
                    log(f"⚠ falha de rede ({type(e).__name__}: {str(e)[:60]}) — "
                        f"tentando de novo em {wait}s [{net_hits}/5]")
                if net_hits > 5:
                    if log:
                        log("✖ rede instável demais — encerrando (dá pra continuar)")
                    return rows, cursor
                _sleep_stop(wait, should_stop)
                continue
            block_hits += 1
            if block_hits <= 3:
                wait = min(300, 120 * block_hits)
                if log:
                    log(f"⚠ resposta inesperada ({type(e).__name__}: {str(e)[:60]}) — possível "
                        f"bloqueio TEMPORÁRIO. Esperando {wait}s e verificando se a conta volta "
                        f"[{block_hits}/3]…")
                if _sleep_stop(wait, should_stop):
                    return rows, cursor
                continue
            if log:
                log("✖ a conta não voltou após 3 tentativas — encerrando "
                    "(dá pra continuar de onde parou).")
            if meta is not None:
                meta["blocked"] = True
            return rows, cursor
        rate_hits = net_hits = block_hits = 0
        before = len(rows)
        for m in (medias or []):
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
        cursor = end_cursor or None
        if not cursor:
            if log:
                log(f"✓ fim dos posts — {_tot(len(rows))}")
            return rows, None
        if checkpoint and page_n % _CHECKPOINT_EVERY == 0:   # resume-safe num crash
            checkpoint(rows, cursor)
        if shift_pages and page_n >= shift_pages:
            if log:
                log(f"⏸ turno de {shift_pages} páginas concluído — passando p/ a próxima conta")
            return rows, cursor
        _page_sleep(delay_ms, page_n, next_pause, log, should_stop)
        _session_cooldown(session_ref, max_pages, should_stop, log,
                          on_pause=((lambda: checkpoint(rows, cursor)) if checkpoint else None))
    return rows, cursor


def _follow_app(cl, uid, uname, log=None) -> bool:
    """Segue o alvo no método App/instagrapi (igual ao Web — libera o acesso à lista
    profunda). No-op se já seguir. Best-effort: tolera falha (segue a extração)."""
    try:
        fr = cl.user_friendship_v1(uid)
        already = bool(getattr(fr, "following", None)
                       or (isinstance(fr, dict) and fr.get("following")))
        if already:
            if log:
                log(f"✓ a conta já segue @{uname}")
            return True
    except Exception:
        pass   # não deu pra checar → tenta seguir mesmo assim (user_follow é idempotente)
    try:
        cl.user_follow(uid)
        if log:
            log(f"➕ agora seguindo @{uname} (libera o acesso à lista)")
        return True
    except Exception as e:
        if log:
            log(f"⚠ não consegui seguir @{uname} ({str(e)[:50]}) — seguindo a extração")
        return False


def _extract_sync(account, kind, target, limit, progress, should_stop, code_provider, status,
                  use_local, log, start_max_id, seed_rows, delay_ms, on_total, checkpoint=None,
                  shift_pages=None):
    """Loga uma vez e despacha por tipo. Retorna (rows, cursor, total, blocked)."""
    meta = {}   # os loops sinalizam aqui (ex.: blocked=True em bloqueio/desafio)
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
    # SEGUIR o alvo antes de coletar (igual ao Web — conta que segue acessa a lista
    # profunda sem queimar). No-op se já seguir; best-effort.
    try:
        _follow_app(cl, uid, uname, log)
        time.sleep(round(random.uniform(1.5, 3.0), 1))
    except Exception:
        pass
    if log:
        log((f"retomando: {len(seed_rows or [])} já coletado(s) — " if seed_rows else "")
            + f"alvo @{uname} (id {uid})"
            + (f" — {total} {kind}" if total is not None else "") + " — iniciando extração…")
    if kind in ("followers", "following"):
        rows, cursor = _friendship_loop(cl, kind, uid, uname, limit, progress, should_stop, log,
                                        start_max_id, seed_rows, delay_ms, total=total,
                                        checkpoint=checkpoint, shift_pages=shift_pages, meta=meta)
    elif kind == "posts":
        rows, cursor = _posts_loop(cl, uid, uname, limit, progress, should_stop, log, start_max_id,
                                   seed_rows, delay_ms, total=total, checkpoint=checkpoint,
                                   shift_pages=shift_pages, meta=meta)
    else:
        rows, cursor = list(seed_rows or []), None
    return rows, cursor, total, bool(meta.get("blocked"))


async def extract(account: dict, kind: str, target: str, limit: int = 0,
                  progress=None, should_stop=None, code_provider=None, status=None,
                  use_local=False, log=None, start_max_id=None, seed_rows=None,
                  delay_ms=2500, on_total=None, checkpoint=None, shift_pages=None) -> dict:
    """Extrai via instagrapi: followers/following/posts. Suporta resume + velocidade.
    shift_pages: se setado, encerra o TURNO após N páginas devolvendo o cursor (para o
    handoff/rodízio multi-conta — paridade com o Web; o roteamento atual usa só single)."""
    if kind not in ("followers", "following", "posts"):
        return {"error": f"tipo '{kind}' não suportado no modo app (use followers/following/posts)"}
    loop = asyncio.get_event_loop()
    try:
        rows, cursor, total, blocked = await loop.run_in_executor(
            None, _extract_sync, account, kind, target, limit, progress, should_stop,
            code_provider, status, use_local, log, start_max_id, seed_rows, delay_ms, on_total,
            checkpoint, shift_pages)
        return {"rows": rows, "cursor": cursor, "total": total, "blocked": blocked,
                "target": (target or "").lstrip("@"), "kind": kind}
    except Exception as e:
        name = type(e).__name__
        if "Challenge" in name or "challenge_required" in str(e):
            return {"error": "O Instagram exigiu verificação que o instagrapi não resolve automaticamente "
                             "(fluxo novo / IP suspeito). Tente: (1) marcar 'usar conexão local' (IP do celular); "
                             "(2) se persistir, use o método WEB (loga no Camoufox e resolve o desafio na tela)."}
        if "TwoFactor" in name or "two_factor" in str(e).lower():
            return {"error": "A conta tem verificação em duas etapas (2FA). O modo app ainda não trata 2FA — use o método Web."}
        if "BadPassword" in name or "BadCredentials" in name or "bad_password" in str(e).lower():
            return {"error": "Usuário ou senha incorretos para esta conta."}
        if "LoginRequired" in name:
            return {"error": "A sessão da conta expirou e o re-login não passou (a conta pode estar em "
                             "suspensão/desafio). Verifique a conta ou use o método Web."}
        return {"error": f"instagrapi: {e}"}
