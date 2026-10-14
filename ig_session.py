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


def do_dominio(c: dict, dominios) -> bool:
    """O cookie pertence a algum destes domínios?

    O filtro virou parâmetro quando a importação passou a valer para qualquer
    rede. Compara por sufixo porque o Cookie-Editor exporta tanto `.tiktok.com`
    quanto `www.tiktok.com`, e os dois são a mesma sessão.
    """
    d = (c.get("domain") or "").lstrip(".").lower()
    return any(d == x or d.endswith("." + x) for x in (dominios or []))


def _clean(c: dict, dominio_padrao: str = ".instagram.com") -> dict:
    """Normaliza um cookie para o formato do add_cookies."""
    out = {k: c[k] for k in _PW_FIELDS if k in c}
    # sameSite só aceita 'Strict' | 'Lax' | 'None'
    ss = str(out.get("sameSite") or "").capitalize()
    out["sameSite"] = ss if ss in ("Strict", "Lax", "None") else "Lax"
    if not out.get("domain"):
        out["domain"] = dominio_padrao
    if not out.get("path"):
        out["path"] = "/"
    return out


def summarize(cookies: list, dominios=None) -> dict:
    """Resumo seguro p/ log/UI: NUNCA expõe o VALOR de cookie nenhum."""
    doms = list(dominios) if dominios else ["instagram.com"]
    names = sorted({c.get("name") for c in cookies if do_dominio(c, doms)})
    ds = next((c.get("value") for c in cookies if c.get("name") == "ds_user_id"), None)
    return {"total": len(cookies), "ig": len(names), "names": names,
            "ds_user_id": ds, "has_sessionid": any(c.get("name") == "sessionid"
                                                   for c in cookies if is_ig_cookie(c))}


# nomes de cookie do IG que são httpOnly (p/ quando o input não traz o flag)
_HTTPONLY = {"sessionid", "ig_did", "mid", "datr", "rur", "ps_l", "ps_n"}
_SAMESITE_MAP = {"no_restriction": "None", "none": "None", "unspecified": "Lax",
                 "lax": "Lax", "strict": "Strict"}


def _mk(name: str, value: str) -> dict:
    return {"name": name, "value": value, "domain": ".instagram.com", "path": "/",
            "secure": True, "httpOnly": name in _HTTPONLY, "sameSite": "Lax"}


def _from_editor(c: dict) -> dict:
    """Normaliza um cookie do Cookie-Editor/EditThisCookie p/ o formato do Playwright."""
    out = {"name": c.get("name"), "value": c.get("value") or "",
           "domain": c.get("domain") or ".instagram.com",
           "path": c.get("path") or "/",
           "secure": bool(c.get("secure", True)),
           "httpOnly": bool(c.get("httpOnly", c.get("name") in _HTTPONLY))}
    exp = c.get("expirationDate") or c.get("expires")
    if isinstance(exp, (int, float)) and exp > 0:
        out["expires"] = int(exp)
    ss = str(c.get("sameSite") or "").strip().lower()
    out["sameSite"] = _SAMESITE_MAP.get(ss, "Lax")
    return out


def parse_cookies_input(text: str) -> list:
    """Aceita o que o usuário colar e devolve cookies normalizados de instagram.com:
    (a) JSON array do Cookie-Editor/EditThisCookie; (b) JSON objeto {nome: valor};
    (c) string de header 'a=b; c=d'; (d) só o valor do sessionid."""
    import json as _json
    text = (text or "").strip()
    if not text:
        return []
    cookies = []
    if text[:1] in "[{":
        try:
            parsed = _json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            cookies = [_from_editor(c) for c in parsed if isinstance(c, dict) and c.get("name")]
        elif isinstance(parsed, dict):
            cookies = [_mk(k, str(v)) for k, v in parsed.items() if k]
    if not cookies:
        if "=" in text:                      # header "a=b; c=d" (ou "a=b")
            for part in text.split(";"):
                k, sep, v = part.strip().partition("=")
                if sep and k.strip():
                    cookies.append(_mk(k.strip(), v.strip()))
        else:                                # só o valor do sessionid
            cookies = [_mk("sessionid", text)]
    return [c for c in cookies if c.get("name") and c.get("value")]


async def export_cookies(profile: dict, only_ig: bool = True) -> list:
    """Lê os cookies do perfil SEM navegar (zero requisição ao Instagram)."""
    async def _run(page):
        cookies = await page.context.cookies()
        return [c for c in cookies if (not only_ig or is_ig_cookie(c))]
    return await sm.run_profile_task(profile, _run, headless=True)


async def import_cookies(profile: dict, cookies: list, only_auth: bool = False,
                         dominios=None) -> dict:
    """Injeta cookies de sessão no perfil (persistem no user_data_dir do perfil).

    `dominios` restringe o que entra; vazio = Instagram, que era o único caso
    quando isto nasceu. O resto do mecanismo sempre foi genérico — quem prendia
    à rede era só o filtro.

    only_auth=True importa apenas os cookies de autenticação (AUTH_NAMES) —
    só faz sentido no Instagram, que é de quem essa lista é.
    """
    doms = list(dominios) if dominios else ["instagram.com"]
    src = [c for c in cookies if do_dominio(c, doms)]
    if only_auth:
        src = [c for c in src if c.get("name") in AUTH_NAMES]
    padrao = "." + doms[0].lstrip(".")
    payload = [_clean(c, padrao) for c in src]
    if not payload:
        return {"imported": 0,
                "error": "nenhum cookie de %s no material recebido" % " / ".join(doms)}

    async def _run(page):
        await page.context.add_cookies(payload)
        # Confere o que ficou de fato gravado no contexto.
        now = await page.context.cookies()
        return summarize(now, doms)
    got = await sm.run_profile_task(profile, _run, headless=True)
    return {"imported": len(payload), "profile_now": got}


async def descobrir_conta(profile: dict, use_local: bool = True) -> dict:
    """Descobre QUAL conta esta logada neste perfil.

    Existia um furo: a sessao fica guardada sozinha no `user_data_dir` (e o proprio
    perfil do Firefox), e ao FECHAR um perfil aberto na mao o app ja verifica o
    status da conta -- mas so quando o perfil ja esta vinculado a alguma. Logando
    uma conta num perfil orfao, nada era registrado, porque o `detect_status`
    devolve o ESTADO (ativa/suspensa/banida) e nunca o username.

    Aqui: o `ds_user_id` sai do cookie (sem requisicao) e o username vem de UMA
    requisicao autenticada. Se nem o cookie existir, o perfil nao esta logado e
    nem chegamos a gastar a requisicao.
    """
    import spider
    prof = {**profile, "proxy": None} if use_local else profile

    async def _run(page):
        await page.goto(IG + "/", wait_until="domcontentloaded")
        await asyncio.sleep(1.2)
        cookies = await page.context.cookies()
        info = summarize(cookies)
        uid = info.get("ds_user_id")
        if not uid:
            return {"logado": False, "ds_user_id": None, "username": None,
                    "detalhe": "sem ds_user_id nos cookies — este perfil nao esta logado",
                    "cookies": info}
        r = await page.evaluate(spider._FETCH_JS,
                                [f"{IG}/api/v1/users/{uid}/info/", f"{IG}/"])
        u = (((r.get("json") or {}).get("user")) or {})
        nome = u.get("username")
        return {"logado": bool(nome), "ds_user_id": str(uid), "username": nome,
                "full_name": u.get("full_name"), "status_http": r.get("status"),
                "detalhe": "" if nome else "cookie presente mas a sessao nao respondeu "
                                           "(status %s) — pode estar expirada" % r.get("status"),
                "cookies": info}

    return await sm.run_profile_task(prof, _run, headless=True)


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
