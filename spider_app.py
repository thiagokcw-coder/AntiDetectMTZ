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


def _followers_sync(account, target, limit, progress, should_stop, code_provider, status, use_local):
    cl = _client(account, code_provider, status, use_local)
    if status:
        status("extraindo")
    uname = (target or "").strip().lstrip("@").lower()
    uid = cl.user_id_from_username(uname)
    rows, seen, max_id = [], set(), ""
    while True:
        if should_stop and should_stop():
            break
        chunk, max_id = cl.user_followers_v1_chunk(uid, max_amount=0, max_id=max_id)
        for u in chunk:
            pk = str(getattr(u, "pk", "") or "")
            if not pk or pk in seen:
                continue
            seen.add(pk)
            rows.append(_row(u))
            if limit and len(rows) >= limit:
                if progress:
                    progress(len(rows))
                return rows
        if progress:
            progress(len(rows))
        if not max_id:
            break
    return rows


async def extract(account: dict, kind: str, target: str, limit: int = 0,
                  progress=None, should_stop=None, code_provider=None, status=None,
                  use_local=False) -> dict:
    """Extrai via instagrapi. Fase 1: kind='followers'."""
    if kind != "followers":
        return {"error": f"tipo '{kind}' ainda não implementado no modo app (Fase 1 = followers)"}
    loop = asyncio.get_event_loop()
    try:
        rows = await loop.run_in_executor(
            None, _followers_sync, account, target, limit, progress, should_stop,
            code_provider, status, use_local)
        return {"rows": rows, "target": (target or "").lstrip("@"), "kind": kind}
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
