"""
spider.py — extração de dados do Instagram via API privada, rodando DENTRO de um
perfil Camoufox JÁ LOGADO (fetch autenticado via page.evaluate).

Reimplementa a lógica do mtzSpider SEM extensão: no nosso modelo cada perfil
Camoufox já é UMA conta logada (cookies + proxy próprios), então não há pool de
cookies/offscreen/service-worker — só abrir o perfil e fazer o fetch na origem
instagram.com. "Rotação" = alternar entre PERFIS (cada um com seu IP) — Fase 3.

Fase 1: seguidores. (curtidas/comentários/posts/perfil/hashtag/local vêm depois.)
"""
import asyncio
import random
import re

import session_manager as sm

IG = "https://www.instagram.com"

# JS executado NO contexto da página instagram.com (mesma origem) — fetch autenticado.
# Headers mínimos que já autenticam (csrftoken do cookie + app-id fixo). credentials
# 'include' manda os cookies da sessão logada do perfil.
_FETCH_JS = r"""
async ([url, referer]) => {
  const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  const csrf = m ? m[1] : '';
  try {
    const r = await fetch(url, {
      method: 'GET',
      credentials: 'include',
      headers: {
        'x-csrftoken': csrf,
        'x-ig-app-id': '936619743392459',
        'x-asbd-id': '129477',
        'x-requested-with': 'XMLHttpRequest',
      },
      referrer: referer,
    });
    let body = null;
    try { body = await r.json(); } catch (e) {}
    return { ok: r.ok, status: r.status, json: body };
  } catch (e) {
    return { ok: false, status: -1, error: String(e) };
  }
}
"""

# GraphQL (headers MÍNIMOS, método IGFollow) — paginação real (end_cursor), NÃO
# capa em 50 como a API v1 /friendships/. Sem x-ig-app-id de propósito (menos
# fingerprint = menos 'should_limit_list_of_followers').
_GQL_JS = r"""
async ([url, referer]) => {
  const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  const csrf = m ? m[1] : '';
  try {
    const r = await fetch(url, {
      method: 'GET',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRFToken': csrf,
        'x-requested-with': 'XMLHttpRequest',
        'x-instagram-ajax': '1',
        'referer': referer,
      },
      referrer: referer,
    });
    let body = null;
    try { body = await r.json(); } catch (e) {}
    return { ok: r.ok, status: r.status, json: body };
  } catch (e) {
    return { ok: false, status: -1, error: String(e) };
  }
}
"""

# query_hash confirmados (engenharia reversa, ref. "SPIDER NEW"/IGFollow).
_GQL = {"followers": "37479f2b8209594dde7facb0d904896a",
        "following": "58712303d941c6855d4e888c5f0cd22f"}


async def _graphql(page, query_hash: str, variables: dict, referer: str) -> dict:
    import json as _json
    from urllib.parse import quote
    url = f"{IG}/graphql/query/?query_hash={query_hash}&variables={quote(_json.dumps(variables))}"
    return await page.evaluate(_GQL_JS, [url, referer])


FOLLOWERS_HEADERS = ["Username", "User Id", "Full Name", "Verified", "Private",
                     "Profile URL", "Avatar URL"]
# 'following' usa os mesmos campos de 'followers'.
POSTS_HEADERS = ["Shortcode", "URL", "Caption", "Likes", "Comments", "Views",
                 "Taken At", "Media Type", "Product Type"]

_MEDIA_TYPE = {1: "foto", 2: "vídeo", 8: "carrossel"}

# A cada quantas páginas gravar um checkpoint (linhas + cursor) no banco. Assim um
# crash/reinício perde no máximo ~N páginas e a run pode CONTINUAR do cursor salvo.
_CHECKPOINT_EVERY = 10


def headers_for(kind: str) -> list:
    """Cabeçalhos do CSV conforme o tipo extraído."""
    return POSTS_HEADERS if kind == "posts" else FOLLOWERS_HEADERS


def _page_delay_s(delay_ms) -> float:
    """Delay entre páginas com variância HUMANA trimodal (mais difícil de detectar
    que jitter uniforme): scan 55% (0,55–1,15×), leitura 32% (1–2×), distraído 13%
    (2–6×). Mesma distribuição do reference SPIDER NEW."""
    base = max(0.3, (delay_ms or 2500) / 1000.0)
    r = random.random()
    if r < 0.55:
        f = 0.55 + random.random() * 0.60
    elif r < 0.87:
        f = 1.00 + random.random() * 1.00
    else:
        f = 2.00 + random.random() * 4.00
    return round(base * f, 2)


def _max_pages_for(delay_ms) -> int:
    """Páginas (~50 itens cada) por sessão antes de um COOLDOWN longo — por tier de
    confiança (espelha o maxPerSession do reference). Sessões menores = menos risco."""
    d = delay_ms or 2500
    if d >= 100000:
        return 8        # ultra (~400 itens)
    if d >= 30000:
        return 20       # conta nova (~1000)
    if d >= 10000:
        return 100      # média (~5000)
    if d >= 6000:
        return 400      # boa (~20000)
    return 1600         # forte (~80000)


async def _sleep_stop(secs, should_stop, step: float = 0.4) -> bool:
    """Sleep INTERROMPÍVEL: dorme em passos curtos e sai assim que should_stop()
    fica True. Retorna True se foi interrompido. Sem isto, o botão Parar só era
    visto depois que o sleep longo entre páginas (até ~45s+) terminava."""
    waited = 0.0
    while waited < secs:
        if should_stop and should_stop():
            return True
        await asyncio.sleep(min(step, secs - waited))
        waited += step
    return bool(should_stop and should_stop())


async def _session_cooldown(should_stop, log):
    """Pausa longa (10–25 min) entre sessões — interrompível. Deixa a conta 'descansar'
    para não estourar o limite e ser suspensa."""
    secs = random.randint(600, 1500)
    if log:
        log(f"🧊 cooldown de sessão: ~{secs // 60} min (deixa a conta descansar)…")
    if await _sleep_stop(secs, should_stop):
        return
    if log:
        log("▶ retomando após o cooldown")


def clean_username(raw: str) -> str:
    raw = (raw or "").strip()
    m = re.search(r"instagram\.com/([^/?#]+)", raw)
    if m:
        raw = m.group(1)
    return raw.lstrip("@").strip().lower()


async def _api_get(page, url: str, referer: str) -> dict:
    return await page.evaluate(_FETCH_JS, [url, referer])


async def _resolve_user(page, username: str):
    """username -> (user_id, info) usando web_profile_info."""
    username = clean_username(username)
    url = f"{IG}/api/v1/users/web_profile_info/?username={username}"
    res = await _api_get(page, url, f"{IG}/{username}/")
    if res.get("ok") and res.get("json"):
        u = (res["json"].get("data") or {}).get("user")
        if u:
            return str(u.get("id")), u
    return None, res


async def _friendship_list(page, user_id, username, edge, limit, progress, should_stop,
                           log=None, start_max_id=None, seed_rows=None, delay_ms=2500, total=None,
                           shift_pages=None, checkpoint=None):
    """Lista de relacionamento (edge='followers' ou 'following') via GraphQL.
    Pagina de verdade pelo `end_cursor` (a API v1 /friendships/ capava em ~50).
    Retorna (rows, cursor); cursor=None => completo. cursor = end_cursor do GraphQL."""
    rows = list(seed_rows or [])
    seen = {str(r.get("User Id")) for r in rows if r.get("User Id")}
    cursor = start_max_id or None
    _tot = (lambda n: f"{n}/{total}" if total else str(n))   # "50/19000" ou "50"
    qhash = _GQL["following"] if edge == "following" else _GQL["followers"]
    edge_key = "edge_follow" if edge == "following" else "edge_followed_by"
    referer = f"{IG}/{username}/{'following' if edge == 'following' else 'followers'}/"
    rate_hits = 0
    net_hits = 0
    page_n = 0
    next_pause = random.randint(15, 25)   # pausa de proteção a cada ~15-25 páginas
    max_pages = _max_pages_for(delay_ms)  # páginas por sessão antes do cooldown longo
    session_pages = 0
    if seed_rows and log:
        log(f"retomando: {len(rows)} já coletado(s), seguindo do cursor salvo…")
    while True:
        if should_stop and should_stop():
            if log:
                log("⏹ parado pelo usuário")
            break
        variables = {"id": str(user_id), "first": 50}
        if cursor:
            variables["after"] = cursor
        res = await _graphql(page, qhash, variables, referer)
        st = res.get("status")
        if st in (429, 560):                 # rate limit → espera e tenta de novo
            rate_hits += 1
            if log:
                log(f"⚠ rate limit (status {st}) — aguardando 60s [{rate_hits}/5]")
            if rate_hits > 5:
                if log:
                    log("✖ muitas vezes em rate limit — encerrando")
                break
            await _sleep_stop(60, should_stop)
            continue
        # Falha TRANSITÓRIA (fetch lançou exceção / erro de servidor 5xx): NÃO é
        # bloqueio — espera e tenta de novo o MESMO cursor, sem matar a run. Uma
        # run de milhares de páginas não pode morrer por um piscar de rede.
        if st == -1 or (isinstance(st, int) and st >= 500):
            net_hits += 1
            wait = min(60, 15 * net_hits)
            if log:
                log(f"⚠ falha de rede (status {st}: {res.get('error') or '?'}) — "
                    f"tentando de novo em {wait}s [{net_hits}/5]")
            if net_hits > 5:
                if log:
                    log("✖ rede instável demais — encerrando (dá pra continuar)")
                break
            await _sleep_stop(wait, should_stop)
            continue
        net_hits = 0
        rate_hits = 0
        data = ((((res.get("json") or {}).get("data") or {}).get("user") or {}).get(edge_key))
        if not res.get("ok") or not data:
            if log:
                log(f"✖ resposta inesperada (status {st}) — possível bloqueio/desafio "
                    f"na conta. Encerrando (dá pra continuar de onde parou).")
            break
        before = len(rows)
        for e in (data.get("edges") or []):
            node = e.get("node") or {}
            pk = str(node.get("id") or "")
            if not pk or pk in seen:
                continue
            seen.add(pk)
            uname = node.get("username", "")
            rows.append({
                "Username": uname,
                "User Id": pk,
                "Full Name": node.get("full_name", ""),
                "Verified": bool(node.get("is_verified")),
                "Private": bool(node.get("is_private")),
                "Profile URL": f"{IG}/{uname}/" if uname else "",
                "Avatar URL": node.get("profile_pic_url", ""),
            })
            if limit and len(rows) >= limit:
                if progress:
                    progress(len(rows))
                if log:
                    log(f"✓ limite de {limit} atingido — {_tot(len(rows))}")
                pi = data.get("page_info") or {}
                return rows, (pi.get("end_cursor") if pi.get("has_next_page") else None)
        page_n += 1
        if log:
            log(f"página {page_n}: +{len(rows) - before} ({_tot(len(rows))})")
        if progress:
            progress(len(rows))
        pi = data.get("page_info") or {}
        cursor = pi.get("end_cursor") if pi.get("has_next_page") else None
        if not cursor:
            if log:
                log(f"✓ fim da lista — {_tot(len(rows))}")
            break
        # Checkpoint: grava linhas + cursor no banco a cada N páginas, pra um
        # reinício/crash não perder o progresso (e poder continuar do cursor).
        if checkpoint and page_n % _CHECKPOINT_EVERY == 0:
            checkpoint(rows, cursor)
        # Rodízio multi-conta: fim do turno → devolve o cursor p/ a próxima conta
        # continuar (em vez do cooldown longo na mesma conta).
        if shift_pages and page_n >= shift_pages:
            if log:
                log(f"⏸ turno de {shift_pages} páginas concluído — passando p/ a próxima conta")
            return rows, cursor
        await _sleep_stop(_page_delay_s(delay_ms), should_stop)
        if page_n >= next_pause:                       # pausa de proteção curta
            pause = round(random.uniform(5, 20), 1)
            if log:
                log(f"🛡️ pausa de proteção: {pause}s")
            await _sleep_stop(pause, should_stop)
            next_pause = page_n + random.randint(15, 25)
        session_pages += 1
        if session_pages >= max_pages:                 # cooldown longo de sessão
            if checkpoint:                             # salva ANTES da pausa longa
                checkpoint(rows, cursor)
            await _session_cooldown(should_stop, log)
            session_pages = 0
    return rows, cursor


def _post_row(it) -> dict:
    import time as _t
    code = it.get("code") or ""
    cap = (it.get("caption") or {}) or {}
    ts = it.get("taken_at")
    when = ""
    try:
        if ts:
            when = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(int(ts)))
    except Exception:
        when = ""
    return {
        "Shortcode": code,
        "URL": f"{IG}/p/{code}/" if code else "",
        "Caption": (cap.get("text") or "").replace("\n", " ").strip(),
        "Likes": it.get("like_count", 0),
        "Comments": it.get("comment_count", 0),
        "Views": it.get("view_count") or it.get("play_count") or "",
        "Taken At": when,
        "Media Type": _MEDIA_TYPE.get(it.get("media_type"), it.get("media_type")),
        "Product Type": it.get("product_type") or "",
    }


async def _posts(page, user_id, username, limit, progress, should_stop, log=None,
                 start_max_id=None, seed_rows=None, delay_ms=2500, total=None,
                 shift_pages=None, checkpoint=None):
    """Posts do perfil (`/api/v1/feed/user/{id}/`). Retorna (rows, cursor)."""
    rows = list(seed_rows or [])
    seen = {str(r.get("Shortcode")) for r in rows if r.get("Shortcode")}
    max_id = start_max_id or None
    cursor = max_id
    rate_hits = 0
    net_hits = 0
    page_n = 0
    next_pause = random.randint(15, 25)
    max_pages = _max_pages_for(delay_ms)
    session_pages = 0
    _tot = (lambda n: f"{n}/{total}" if total else str(n))
    if seed_rows and log:
        log(f"retomando: {len(rows)} post(s) já coletado(s)…")
    while True:
        if should_stop and should_stop():
            if log:
                log("⏹ parado pelo usuário")
            cursor = max_id
            break
        url = f"{IG}/api/v1/feed/user/{user_id}/?count=12"
        if max_id:
            url += f"&max_id={max_id}"
        res = await _api_get(page, url, f"{IG}/{username}/")
        st = res.get("status")
        if st in (429, 560):
            rate_hits += 1
            if log:
                log(f"⚠ rate limit (status {st}) — aguardando 60s [{rate_hits}/5]")
            if rate_hits > 5:
                if log:
                    log("✖ muitas vezes em rate limit — encerrando")
                cursor = max_id
                break
            await _sleep_stop(60, should_stop)
            continue
        # Falha transitória (rede/5xx) → retry do mesmo max_id, sem matar a run.
        if st == -1 or (isinstance(st, int) and st >= 500):
            net_hits += 1
            wait = min(60, 15 * net_hits)
            if log:
                log(f"⚠ falha de rede (status {st}: {res.get('error') or '?'}) — "
                    f"tentando de novo em {wait}s [{net_hits}/5]")
            if net_hits > 5:
                if log:
                    log("✖ rede instável demais — encerrando (dá pra continuar)")
                cursor = max_id
                break
            await _sleep_stop(wait, should_stop)
            continue
        net_hits = 0
        rate_hits = 0
        if not res.get("ok") or not res.get("json"):
            if log:
                log(f"✖ resposta inesperada (status {st}) — possível bloqueio/desafio "
                    f"na conta. Encerrando (dá pra continuar de onde parou).")
            cursor = max_id
            break
        j = res["json"]
        before = len(rows)
        for it in (j.get("items") or []):
            code = str(it.get("code") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            rows.append(_post_row(it))
            if limit and len(rows) >= limit:
                if progress:
                    progress(len(rows))
                if log:
                    log(f"✓ limite de {limit} atingido — {_tot(len(rows))}")
                return rows, j.get("next_max_id")
        page_n += 1
        if log:
            log(f"página {page_n}: +{len(rows) - before} ({_tot(len(rows))})")
        if progress:
            progress(len(rows))
        max_id = j.get("next_max_id")
        if not max_id or not j.get("more_available", True):
            if log:
                log(f"✓ fim dos posts — {_tot(len(rows))}")
            cursor = None
            break
        cursor = max_id
        # Checkpoint: grava linhas + cursor a cada N páginas (resume-safe num crash).
        if checkpoint and page_n % _CHECKPOINT_EVERY == 0:
            checkpoint(rows, cursor)
        # Rodízio multi-conta: fim do turno → devolve o cursor p/ a próxima conta.
        if shift_pages and page_n >= shift_pages:
            if log:
                log(f"⏸ turno de {shift_pages} páginas concluído — passando p/ a próxima conta")
            return rows, cursor
        await _sleep_stop(_page_delay_s(delay_ms), should_stop)
        if page_n >= next_pause:
            pause = round(random.uniform(5, 20), 1)
            if log:
                log(f"🛡️ pausa de proteção: {pause}s")
            await _sleep_stop(pause, should_stop)
            next_pause = page_n + random.randint(15, 25)
        session_pages += 1
        if session_pages >= max_pages:
            if checkpoint:                             # salva ANTES da pausa longa
                checkpoint(rows, cursor)
            await _session_cooldown(should_stop, log)
            session_pages = 0
    return rows, cursor


def _total_for(info: dict, kind: str):
    """Total de itens do alvo (p/ mostrar X/Y) a partir do web_profile_info."""
    info = info or {}
    if kind == "following":
        return (info.get("edge_follow") or {}).get("count")
    if kind == "posts":
        return (info.get("edge_owner_to_timeline_media") or {}).get("count")
    return (info.get("edge_followed_by") or {}).get("count")


async def extract(profile: dict, kind: str, target: str, limit: int = 0,
                  progress=None, should_stop=None, log=None,
                  start_max_id=None, seed_rows=None, delay_ms=2500, on_total=None,
                  use_local=False, shift_pages=None, checkpoint=None) -> dict:
    """Abre o perfil (logado) e extrai (followers/following/posts).
    Retorna {rows, cursor, total, target, kind, user} ou {error}. start_max_id/seed_rows
    permitem CONTINUAR de onde parou; delay_ms = velocidade; on_total(t) avisa o total.
    use_local=True abre SEM o proxy do perfil (IP local/limpo) — o app só lê dados,
    não loga (a sessão já está logada via cookies).
    shift_pages: se setado, encerra o TURNO após N páginas devolvendo o cursor
    (usado pelo rodízio multi-conta — a próxima conta continua daquele cursor)."""
    if use_local:
        profile = {**profile, "proxy": None}
    _actor = profile.get("name") or profile.get("id") or "perfil"
    _px = profile.get("proxy") or {}
    _conn = "IP local (sem proxy)" if use_local else (f"proxy {_px.get('host')}" if _px.get("host") else "sem proxy")
    async def _run(page):
        if log:
            log(f"🧭 raspando com a conta/perfil: '{_actor}' · {_conn}")
            log("abrindo instagram.com no perfil…")
        await page.goto(IG + "/", wait_until="domcontentloaded")
        await asyncio.sleep(round(random.uniform(2.0, 4.0), 2))
        if log:
            log(f"resolvendo @{clean_username(target)}…")
        uid, info = await _resolve_user(page, target)
        if not uid:
            return {"error": "Perfil não encontrado/privado, ou a conta do perfil não está logada no Instagram."}
        uname = clean_username(target)
        total = _total_for(info, kind)
        if on_total and total is not None:
            on_total(total)
        if log:
            log(f"alvo @{uname} (id {uid}) resolvido"
                + (f" — {total} {kind}" if total is not None else "") + " — aquecendo…")
        # Warm-up: visita o perfil alvo antes de raspar (parece navegação humana).
        try:
            await page.goto(f"{IG}/{uname}/", wait_until="domcontentloaded")
            await asyncio.sleep(round(random.uniform(2.0, 4.0), 2))
        except Exception:
            pass
        if kind in ("followers", "following"):
            if log:
                log(f"iniciando extração de {'seguindo' if kind == 'following' else 'seguidores'}…")
            rows, cursor = await _friendship_list(page, uid, uname, kind, limit, progress,
                                                  should_stop, log, start_max_id=start_max_id,
                                                  seed_rows=seed_rows, delay_ms=delay_ms, total=total,
                                                  shift_pages=shift_pages, checkpoint=checkpoint)
        elif kind == "posts":
            if log:
                log("iniciando extração de posts…")
            rows, cursor = await _posts(page, uid, uname, limit, progress, should_stop, log,
                                        start_max_id=start_max_id, seed_rows=seed_rows,
                                        delay_ms=delay_ms, total=total, shift_pages=shift_pages,
                                        checkpoint=checkpoint)
        else:
            return {"error": f"tipo '{kind}' não suportado (use followers/following/posts)"}
        return {"rows": rows, "cursor": cursor, "total": total, "target": uname, "kind": kind, "user": info}

    try:
        return await sm.run_profile_task(profile, _run, headless=True)
    except Exception as e:
        if log:
            log(f"✖ erro: {e}")
        return {"error": f"falha ao abrir/raspar: {e}"}
