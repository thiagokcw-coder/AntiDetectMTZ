"""
ig_session.py — transferência de SESSÃO do Instagram por COOKIE (sem login novo).

MOTIVO (validado pelo usuário em 2026-07-26): conta que nasce no celular é
**bloqueada ao logar pelo navegador** — a mesma conta entra no app do Android e é
recusada/desafiada no Camoufox. Então o caminho não é "logar melhor" no PC: é **não
logar**. Importa-se a sessão que JÁ existe (os cookies) para dentro do perfil
Camoufox. O navegador passa a estar logado sem gerar evento de "login de novo
dispositivo", e o Spider Web (que só precisa de um perfil logado) funciona sem
nenhuma alteração.

Cookies que compõem a sessão de instagram.com:
  sessionid    → o token que realmente autentica (httpOnly)
  ds_user_id   → id do usuário logado
  csrftoken    → exigido nos headers das chamadas de API
  mid, ig_did  → identificadores de dispositivo/browser (coerência)
  rur, datr    → roteamento/telemetria (ajudam na coerência)

Uso futuro (celular → PC): basta obter esses cookies do aparelho (com root, do
cookie jar do app; sem root, exportando do navegador do celular) e chamar
`import_cookies(perfil, cookies)`.
"""
import asyncio

import session_manager as sm

IG = "https://www.instagram.com"

# Cookies que de fato carregam a sessão. sessionid é o essencial; os outros dão
# coerência (o IG cruza mid/ig_did/datr com o histórico da conta).
AUTH_NAMES = ("sessionid", "ds_user_id", "csrftoken", "mid", "ig_did", "rur", "datr",
              "shbid", "shbts")

# Campos aceitos pelo add_cookies do Playwright (descarta o resto p/ não dar erro).
_PW_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


def is_ig_cookie(c: dict) -> bool:
    return "instagram.com" in (c.get("domain") or "")


def _clean(c: dict) -> dict:
    """Normaliza um cookie para o formato do add_cookies."""
    out = {k: c[k] for k in _PW_FIELDS if k in c}
    # sameSite só aceita 'Strict' | 'Lax' | 'None'
    ss = str(out.get("sameSite") or "").capitalize()
    out["sameSite"] = ss if ss in ("Strict", "Lax", "None") else "Lax"
    if not out.get("domain"):
        out["domain"] = ".instagram.com"
    if not out.get("path"):
        out["path"] = "/"
    return out


def summarize(cookies: list) -> dict:
    """Resumo seguro p/ log/UI: NUNCA expõe o valor do sessionid."""
    names = sorted({c.get("name") for c in cookies if is_ig_cookie(c)})
    ds = next((c.get("value") for c in cookies if c.get("name") == "ds_user_id"), None)
    return {"total": len(cookies), "ig": len(names), "names": names,
            "ds_user_id": ds, "has_sessionid": any(c.get("name") == "sessionid"
                                                   for c in cookies if is_ig_cookie(c))}


async def export_cookies(profile: dict, only_ig: bool = True) -> list:
    """Lê os cookies do perfil SEM navegar (zero requisição ao Instagram)."""
    async def _run(page):
        cookies = await page.context.cookies()
        return [c for c in cookies if (not only_ig or is_ig_cookie(c))]
    return await sm.run_profile_task(profile, _run, headless=True)


async def import_cookies(profile: dict, cookies: list, only_auth: bool = False) -> dict:
    """Injeta cookies de sessão no perfil (persistem no user_data_dir do perfil).
    only_auth=True importa apenas os cookies de autenticação (AUTH_NAMES)."""
    src = [c for c in cookies if is_ig_cookie(c)]
    if only_auth:
        src = [c for c in src if c.get("name") in AUTH_NAMES]
    payload = [_clean(c) for c in src]
    if not payload:
        return {"imported": 0, "error": "nenhum cookie do instagram.com no material recebido"}

    async def _run(page):
        await page.context.add_cookies(payload)
        # Confere o que ficou de fato gravado no contexto.
        now = await page.context.cookies()
        return summarize(now)
    got = await sm.run_profile_task(profile, _run, headless=True)
    return {"imported": len(payload), "profile_now": got}


async def probe_login(profile: dict, target: str = "instagram", use_local: bool = True) -> dict:
    """Diz se o perfil está LOGADO, do jeito que importa: faz UMA requisição GraphQL
    de seguidores (`first:1`) — que só responde com sessão autenticada. NÃO segue
    ninguém, NÃO pagina. É exatamente a chamada de que o Spider depende."""
    import spider
    prof = {**profile, "proxy": None} if use_local else profile

    async def _run(page):
        await page.goto(IG + "/", wait_until="domcontentloaded")
        await asyncio.sleep(1.5)
        cookies = await page.context.cookies()
        info = summarize(cookies)
        out = {"url": page.url, "cookies": info,
               "login_page": "/accounts/login" in (page.url or "")}
        uid, _ = await spider._resolve_user(page, target)
        if not uid:
            out.update(logged=False, why="não resolveu o alvo (sem sessão ou alvo inválido)")
            return out
        res = await spider._graphql(page, spider._GQL["followers"],
                                    {"id": uid, "first": 1},
                                    f"{IG}/{target}/followers/")
        edge = ((((res.get("json") or {}).get("data") or {}).get("user") or {})
                .get("edge_followed_by") or {})
        n = len(edge.get("edges") or [])
        out.update(logged=bool(edge) and n > 0, status=res.get("status"), edges=n)
        return out
    return await sm.run_profile_task(prof, _run, headless=True)
