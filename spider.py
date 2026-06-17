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

FOLLOWERS_HEADERS = ["Username", "User Id", "Full Name", "Verified", "Private",
                     "Profile URL", "Avatar URL"]


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


async def _followers(page, user_id, username, limit, progress, should_stop, log=None,
                     start_max_id=None, seed_rows=None):
    """Retorna (rows, cursor). cursor=None => lista completa; cursor=<max_id> =>
    parou no meio (pode continuar de onde parou passando start_max_id=cursor)."""
    rows = list(seed_rows or [])
    seen = {str(r.get("User Id")) for r in rows if r.get("User Id")}
    max_id = start_max_id or None
    cursor = max_id
    rate_hits = 0
    page_n = 0
    if seed_rows and log:
        log(f"retomando: {len(rows)} já coletado(s), seguindo do cursor salvo…")
    while True:
        if should_stop and should_stop():
            if log:
                log("⏹ parado pelo usuário")
            cursor = max_id
            break
        url = f"{IG}/api/v1/friendships/{user_id}/followers/?count=50"
        if max_id:
            url += f"&max_id={max_id}"
        res = await _api_get(page, url, f"{IG}/{username}/")
        st = res.get("status")
        if st in (429, 560):                 # rate limit → espera e tenta de novo
            rate_hits += 1
            if log:
                log(f"⚠ rate limit (status {st}) — aguardando 60s [{rate_hits}/5]")
            if rate_hits > 5:
                if log:
                    log("✖ muitas vezes em rate limit — encerrando")
                cursor = max_id
                break
            await asyncio.sleep(60)
            continue
        rate_hits = 0
        if not res.get("ok") or not res.get("json"):
            if log:
                log(f"✖ resposta inesperada (status {st}) — encerrando")
            cursor = max_id
            break
        j = res["json"]
        before = len(rows)
        for u in (j.get("users") or []):
            pk = str(u.get("pk") or u.get("id") or "")
            if not pk or pk in seen:
                continue
            seen.add(pk)
            uname = u.get("username", "")
            rows.append({
                "Username": uname,
                "User Id": pk,
                "Full Name": u.get("full_name", ""),
                "Verified": bool(u.get("is_verified")),
                "Private": bool(u.get("is_private")),
                "Profile URL": f"{IG}/{uname}/" if uname else "",
                "Avatar URL": u.get("profile_pic_url", ""),
            })
            if limit and len(rows) >= limit:
                if progress:
                    progress(len(rows))
                if log:
                    log(f"✓ limite de {limit} atingido — total {len(rows)}")
                return rows, j.get("next_max_id")
        page_n += 1
        if log:
            log(f"página {page_n}: +{len(rows) - before} (total {len(rows)})")
        if progress:
            progress(len(rows))
        max_id = j.get("next_max_id")
        if not max_id:
            if log:
                log(f"✓ fim da lista — total {len(rows)}")
            cursor = None
            break
        cursor = max_id
        await asyncio.sleep(round(random.uniform(0.8, 1.7), 2))   # jitter humano
    return rows, cursor


async def extract(profile: dict, kind: str, target: str, limit: int = 0,
                  progress=None, should_stop=None, log=None,
                  start_max_id=None, seed_rows=None) -> dict:
    """Abre o perfil (logado) e extrai. Fase 1: kind='followers'.
    Retorna {rows, cursor, target, kind, user} ou {error}. start_max_id/seed_rows
    permitem CONTINUAR de onde parou (cursor salvo + linhas já coletadas)."""
    async def _run(page):
        if log:
            log("abrindo instagram.com no perfil…")
        await page.goto(IG + "/", wait_until="domcontentloaded")
        await asyncio.sleep(round(random.uniform(2.0, 4.0), 2))
        if log:
            log(f"resolvendo @{clean_username(target)}…")
        uid, info = await _resolve_user(page, target)
        if not uid:
            return {"error": "Perfil não encontrado/privado, ou a conta do perfil não está logada no Instagram."}
        uname = clean_username(target)
        if log:
            log(f"alvo @{uname} (id {uid}) resolvido — aquecendo (visitando o perfil)…")
        # Warm-up: visita o perfil alvo antes de raspar (parece navegação humana).
        try:
            await page.goto(f"{IG}/{uname}/", wait_until="domcontentloaded")
            await asyncio.sleep(round(random.uniform(2.0, 4.0), 2))
        except Exception:
            pass
        if kind == "followers":
            if log:
                log("iniciando extração de seguidores…")
            rows, cursor = await _followers(page, uid, uname, limit, progress, should_stop, log,
                                            start_max_id=start_max_id, seed_rows=seed_rows)
        else:
            return {"error": f"tipo '{kind}' ainda não implementado (Fase 1 = followers)"}
        return {"rows": rows, "cursor": cursor, "target": uname, "kind": kind, "user": info}

    try:
        return await sm.run_profile_task(profile, _run, headless=True)
    except Exception as e:
        if log:
            log(f"✖ erro: {e}")
        return {"error": f"falha ao abrir/raspar: {e}"}
