"""
follower.py — automação de FOLLOW no Instagram, rodando DENTRO de um perfil
Camoufox JÁ LOGADO (fetch autenticado via page.evaluate).

Porte da extensão InstaFollow (Chrome MV3) para o app. A extensão resolvia a
autenticação rodando como content script dentro de instagram.com; aqui o
equivalente já existe e é melhor: `spider.py` faz o mesmo fetch autenticado
dentro do perfil Camoufox, e cada perfil é UMA conta com SEU proxy/fingerprint.
Consequência prática: a "rotação de contas" da extensão (regravar cookies do
navegador inteiro — que o próprio HANDOFF aponta como o comportamento mais
arriscado dela) vira simplesmente ABRIR OUTRO PERFIL, sem troca de cookies e
com IP distinto por conta.

Fluxo: coleta (followers/following/likers) → fila → filtros → follow, com
intervalo aleatorizado, tetos por janela deslizante e recuo por código de erro.
NÃO faz unfollow (mesmo escopo deliberado da extensão).

⚠ Seguir é MUITO mais arriscado que ler (o Spider só lê). Os defaults aqui são
o preset Conservador de propósito, e o modo SIMULAÇÃO (dry_run) vem ligado.
"""
import asyncio
import random
import re
import time

import session_manager as sm
import spider

IG = spider.IG

# ── Defaults (§8 do HANDOFF) ──────────────────────────────────────────────────
# Ritmo padrão = Conservador (20/h · 100/dia), não o Equilibrado da extensão:
# o aprendizado do projeto é que a saúde da conta é o fator nº 1 e que ela se
# gasta — e follow queima muito mais rápido que leitura.
DEFAULT_SETTINGS = {
    "baseDelayMs": 180000,          # 3 min entre follows (≈ 60/maxPerHour)
    "jitterPercent": 0.5,           # ±50% → nenhum par de follows equidistante
    "skipDelayMs": 2000,
    "enrichDelayMs": 5000,
    "maxPerHour": 20,
    "maxPerDay": 100,
    "backoff429Ms": 60000,          # 1 min
    "backoff403Ms": 600000,         # 10 min (limite leve)
    "backoff400Ms": 3600000,        # 1 h (limite duro)
    "maxConsecutiveFailures": 3,
    "maxAttemptsPerAccount": 3,
    "harvestLimit": 200,
    "harvestPageDelayMs": 2500,
    "skipAlreadyAttempted": True,
    "stopOnActionBlocked": True,
    "dryRun": True,                 # simulação: decide tudo, não segue de verdade
    # ── Unfollow ─────────────────────────────────────────────────────────────
    # O Instagram limita a conta a seguir no máximo 7.500 perfis. Sem desfazer
    # follows, qualquer campanha morre nesse teto — por isso o unfollow não é
    # opcional a longo prazo, é o que mantém espaço na conta.
    "unfollowAfterDays": 3,         # só desfaz o que foi seguido há N dias (1-90)
    "protectMutuals": True,         # não desfaz quem retribuiu o follow
    "unfollowOnlyOurs": True,       # só desfaz o que o app seguiu (nunca follows seus)
    # Lembra o que estava na tela (alvos digitados, fonte, fatia) — sem isto, fechar
    # o app apagava a lista de alvos e o usuário tinha que redigitar tudo.
    "harvestTargets": "",           # um alvo por linha
    "harvestSourceType": "followers",
    "harvestPerTarget": 50,
    # Rodízio: quantos follows cada conta faz antes de passar a vez. Com 3 contas e
    # 100 aqui, uma fila de 300 sai 100/100/100. O teto por hora/dia da conta
    # continua valendo — vale o que vier primeiro.
    "followsPerAccount": 100,
}

# Teto rígido do Instagram para "seguindo". Não é configurável: é da plataforma.
IG_FOLLOWING_LIMIT = 7500

DEFAULT_FILTERS = {
    "followersMin": 0,
    "followersMax": 1000000,
    "followingMin": 0,
    "followingMax": 1000000,
    "ratioMin": 0,
    "postsMin": 0,
    "postsMax": 100000,
    "lastPostMaxDays": 0,
    "skipPrivate": False,
    "skipVerified": True,
    "skipBusiness": False,
    "skipNoProfilePic": True,
    "skipFollowedByViewer": True,
    "requireBioText": "",
    "excludeBioText": "",
}

# Intervalo e teto/hora descrevem a MESMA velocidade: intervalo_min ≈ 60/maxPerHour.
# Baixar só o intervalo não aumenta a produção — gera rajada + ociosidade.
PRESETS = {
    "conservador": {"baseDelayMs": 180000, "maxPerHour": 20, "maxPerDay": 100},
    "equilibrado": {"baseDelayMs": 90000, "maxPerHour": 40, "maxPerDay": 150},
    "rapido": {"baseDelayMs": 60000, "maxPerHour": 60, "maxPerDay": 200},
}

HOUR_MS = 3600000
DAY_MS = 86400000

# Fragmento presente na URL do avatar padrão do IG (conta sem foto).
DEFAULT_AVATAR_MARKER = "44884218_345707102882519"

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


# ── Helpers puros ─────────────────────────────────────────────────────────────

def jitter(base_ms, pct) -> float:
    """Valor uniforme em [base·(1-pct), base·(1+pct)], piso de 1s. Em segundos."""
    base = max(0, float(base_ms or 0))
    p = max(0.0, float(pct or 0))
    lo, hi = base * (1 - p), base * (1 + p)
    return max(1.0, random.uniform(lo, hi) / 1000.0)


def shortcode_from_url(raw: str) -> str:
    """Aceita URL de post/reel ou o shortcode cru."""
    raw = (raw or "").strip()
    m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", raw)
    if m:
        return m.group(1)
    m = re.fullmatch(r"[A-Za-z0-9_-]{5,}", raw)
    return m.group(0) if m else ""


def shortcode_to_media_id(shortcode: str) -> str:
    """Shortcode → id numérico da mídia: base64url dos 11 primeiros caracteres."""
    sc = (shortcode or "")[:11]
    if not sc:
        return ""
    n = 0
    for ch in sc:
        i = _B64.find(ch)
        if i < 0:
            return ""
        n = n * 64 + i
    return str(n)


def human_duration(secs) -> str:
    s = int(max(0, secs))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}min"
    h, m = divmod(s // 60, 60)
    return f"{h}h{m:02d}"


def _terms(value) -> list:
    return [t.strip().lower() for t in str(value or "").split(",") if t.strip()]


def _contains_any(haystack, needles):
    text = str(haystack or "").lower()
    for t in needles:
        if t and t in text:
            return t
    return None


# ── Filtros (§6 — funções puras; campo None NUNCA reprova) ────────────────────

def needs_enrichment(f: dict) -> bool:
    """Estes filtros dependem de dados que as listas não trazem (contadores, bio,
    categoria, último post) → exigem UMA leitura de perfil por conta. É o que mais
    pesa no risco de rate limit, então só se o filtro realmente mudou do default."""
    d = DEFAULT_FILTERS
    return (int(f.get("followersMin", d["followersMin"])) != d["followersMin"]
            or int(f.get("followersMax", d["followersMax"])) != d["followersMax"]
            or int(f.get("followingMin", d["followingMin"])) != d["followingMin"]
            or int(f.get("followingMax", d["followingMax"])) != d["followingMax"]
            or float(f.get("ratioMin") or 0) > 0
            or int(f.get("postsMin", d["postsMin"])) != d["postsMin"]
            or int(f.get("postsMax", d["postsMax"])) != d["postsMax"]
            or int(f.get("lastPostMaxDays") or 0) > 0
            or f.get("skipBusiness") is True
            or len(_terms(f.get("requireBioText"))) > 0
            or len(_terms(f.get("excludeBioText"))) > 0)


def describe_filters(f: dict) -> list:
    """Lista legível dos filtros ativos (para a UI)."""
    d = DEFAULT_FILTERS
    out = []
    if (int(f.get("followersMin", d["followersMin"])) != d["followersMin"]
            or int(f.get("followersMax", d["followersMax"])) != d["followersMax"]):
        out.append(f"seguidores {f.get('followersMin')}–{f.get('followersMax')}")
    if (int(f.get("followingMin", d["followingMin"])) != d["followingMin"]
            or int(f.get("followingMax", d["followingMax"])) != d["followingMax"]):
        out.append(f"seguindo {f.get('followingMin')}–{f.get('followingMax')}")
    if float(f.get("ratioMin") or 0) > 0:
        out.append(f"ratio ≥ {f.get('ratioMin')}")
    if (int(f.get("postsMin", d["postsMin"])) != d["postsMin"]
            or int(f.get("postsMax", d["postsMax"])) != d["postsMax"]):
        out.append(f"posts {f.get('postsMin')}–{f.get('postsMax')}")
    if int(f.get("lastPostMaxDays") or 0) > 0:
        out.append(f"postou nos últimos {f.get('lastPostMaxDays')}d")
    if f.get("skipPrivate"):
        out.append("sem privadas")
    if f.get("skipVerified"):
        out.append("sem verificadas")
    if f.get("skipBusiness"):
        out.append("sem business")
    if f.get("skipNoProfilePic"):
        out.append("exige foto")
    if f.get("skipFollowedByViewer"):
        out.append("pula já seguidas")
    if _terms(f.get("requireBioText")):
        out.append("bio contém")
    if _terms(f.get("excludeBioText")):
        out.append("bio não contém")
    return out


def evaluate(acct: dict, f: dict) -> tuple:
    """(pass, motivo). Campos ainda desconhecidos (None) nunca reprovam — quem
    garante o dado é o enriquecimento, não o filtro."""
    if f.get("skipPrivate") and acct.get("isPrivate") is True:
        return False, "conta privada"
    if f.get("skipVerified") and acct.get("isVerified") is True:
        return False, "conta verificada"
    if f.get("skipBusiness") and acct.get("isBusiness") is True:
        return False, "conta business"
    if f.get("skipNoProfilePic") and acct.get("hasPic") is False:
        return False, "sem foto de perfil"
    if f.get("skipFollowedByViewer") and acct.get("followedByViewer") is True:
        return False, "já seguida"
    if f.get("skipFollowedByViewer") and acct.get("requestedByViewer") is True:
        return False, "solicitação pendente"

    followers, following = acct.get("followers"), acct.get("following")
    if followers is not None:
        if followers < int(f.get("followersMin") or 0):
            return False, f"seguidores < {f.get('followersMin')}"
        if followers > int(f.get("followersMax") or 0):
            return False, f"seguidores > {f.get('followersMax')}"
    if following is not None:
        if following < int(f.get("followingMin") or 0):
            return False, f"seguindo < {f.get('followingMin')}"
        if following > int(f.get("followingMax") or 0):
            return False, f"seguindo > {f.get('followingMax')}"

    ratio_min = float(f.get("ratioMin") or 0)
    if ratio_min > 0 and followers is not None and following is not None:
        ratio = (followers / following) if following > 0 else followers
        if ratio < ratio_min:
            return False, f"ratio {ratio:.2f} < {ratio_min}"

    posts = acct.get("posts")
    if posts is not None:
        if posts < int(f.get("postsMin") or 0):
            return False, f"posts < {f.get('postsMin')}"
        if posts > int(f.get("postsMax") or 0):
            return False, f"posts > {f.get('postsMax')}"

    max_days = int(f.get("lastPostMaxDays") or 0)
    if max_days > 0 and acct.get("lastPostTs") is not None:
        days = (time.time() * 1000 - acct["lastPostTs"]) / DAY_MS
        if days > max_days:
            return False, f"último post há {round(days)}d"

    bio = acct.get("bio")
    if bio is not None:
        required = _terms(f.get("requireBioText"))
        if required and not _contains_any(bio, required):
            return False, "bio sem os termos exigidos"
        excluded = _terms(f.get("excludeBioText"))
        hit = _contains_any(bio, excluded) if excluded else None
        if hit:
            return False, f'bio contém "{hit}"'

    return True, ""


# ── Limitador: janela deslizante REAL, por conta (§5) ─────────────────────────

def _prune(actions: list) -> list:
    cutoff = time.time() * 1000 - DAY_MS
    return [t for t in actions if t > cutoff]


def limiter_counts(actions: list) -> dict:
    now = time.time() * 1000
    lst = _prune(actions)
    return {"hour": sum(1 for t in lst if now - t < HOUR_MS), "day": len(lst)}


def limiter_check(actions: list, settings: dict) -> tuple:
    """(ok, motivo, espera_s). A espera é até a ação mais antiga sair da janela —
    não 'reseta à meia-noite'."""
    now = time.time() * 1000
    lst = _prune(actions)
    max_day = int(settings.get("maxPerDay") or 0)
    max_hour = int(settings.get("maxPerHour") or 0)

    if max_day > 0 and len(lst) >= max_day:
        oldest = min(lst)
        return False, f"diário ({len(lst)}/{max_day})", max(1.0, (oldest + DAY_MS - now) / 1000.0)

    if max_hour > 0:
        in_hour = [t for t in lst if now - t < HOUR_MS]
        if len(in_hour) >= max_hour:
            oldest = min(in_hour)
            return False, f"horário ({len(in_hour)}/{max_hour})", max(1.0, (oldest + HOUR_MS - now) / 1000.0)

    return True, "", 0.0


# ── Camada de acoplamento com a API do IG (isolada, como o ig.js) ─────────────

# POST autenticado (mesma origem, cookies da sessão do perfil).
_POST_JS = r"""
async ([url, referer, body]) => {
  const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  const csrf = m ? m[1] : '';
  try {
    const r = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'accept': '*/*',
        'x-csrftoken': csrf,
        'x-ig-app-id': '936619743392459',
        'x-asbd-id': '129477',
        'x-requested-with': 'XMLHttpRequest',
        'content-type': 'application/x-www-form-urlencoded',
      },
      body: body || '',
      referrer: referer,
    });
    let text = '';
    try { text = await r.text(); } catch (e) {}
    let json = null;
    try { json = JSON.parse(text); } catch (e) {}
    return { ok: r.ok, status: r.status, json: json,
             text: (r.ok || !text) ? '' : text.slice(0, 300) };
  } catch (e) {
    return { ok: false, status: -1, error: String(e) };
  }
}
"""


def is_session_problem(status, payload) -> bool:
    """Sessão caiu / checkpoint / challenge: não adianta repetir, só parar."""
    if status == 401:
        return True
    payload = payload or {}
    if payload.get("login_required") is True or payload.get("checkpoint_url"):
        return True
    msg = str(payload.get("message") or "").lower()
    return ("login_required" in msg) or ("checkpoint" in msg) or ("challenge" in msg)


def is_action_blocked(status, payload) -> bool:
    """'Action blocked' / feedback_required: o Instagram sinalizou automação."""
    payload = payload or {}
    if payload.get("feedback_required") is True or payload.get("spam") is True:
        return True
    blob = " ".join(str(payload.get(k) or "").lower()
                    for k in ("message", "feedback_title", "feedback_message"))
    return any(t in blob for t in ("feedback_required", "action blocked",
                                   "try again later", "blocked", "spam"))


def classify_error(status, payload, settings: dict) -> dict:
    """Traduz a resposta do IG numa decisão: esperar quanto, abortar, re-tentar (§4)."""
    if is_session_problem(status, payload):
        return {"kind": "session", "wait": 0, "abort": True, "retry": False,
                "label": "sessão do Instagram expirada ou em checkpoint"}
    if is_action_blocked(status, payload):
        return {"kind": "blocked", "wait": int(settings.get("backoff400Ms") or 0) / 1000,
                "abort": settings.get("stopOnActionBlocked") is True, "retry": True,
                "label": "o Instagram bloqueou a ação (feedback_required)"}
    if status == 404:
        return {"kind": "notfound", "wait": 2, "abort": False, "retry": False,
                "label": "conta não encontrada"}
    if status == 429:
        return {"kind": "rate429", "wait": int(settings.get("backoff429Ms") or 0) / 1000,
                "abort": False, "retry": True, "label": "HTTP 429 (muitas requisições)"}
    if status == 403:
        return {"kind": "rate403", "wait": int(settings.get("backoff403Ms") or 0) / 1000,
                "abort": False, "retry": True, "label": "HTTP 403 (limite leve)"}
    if status == 400:
        return {"kind": "rate400", "wait": int(settings.get("backoff400Ms") or 0) / 1000,
                "abort": False, "retry": True, "label": "HTTP 400 (limite duro)"}
    return {"kind": "other", "wait": 15, "abort": False, "retry": True,
            "label": f"erro {status or 'de rede'}"}


def to_account(user: dict, source: str) -> dict:
    """Normaliza um usuário de lista (GraphQL node ou v1 users[]) para o formato interno."""
    uid = str(user.get("id") or user.get("pk") or user.get("pk_id") or "")
    username = str(user.get("username") or "").strip().lower()
    pic = user.get("profile_pic_url") or ""
    fs = user.get("friendship_status") or None

    followed = user.get("followed_by_viewer")
    requested = user.get("requested_by_viewer")
    if fs:
        followed = fs.get("following") is True
        requested = fs.get("outgoing_request") is True

    return {
        "id": uid,
        "username": username,
        "fullName": user.get("full_name") or "",
        "picUrl": pic,
        "isPrivate": user.get("is_private") is True,
        "isVerified": user.get("is_verified") is True,
        "hasPic": (DEFAULT_AVATAR_MARKER not in pic) if pic else False,
        "followedByViewer": (followed is True) if followed is not None else None,
        "requestedByViewer": (requested is True) if requested is not None else None,
        "followers": None, "following": None, "posts": None, "bio": None,
        "isBusiness": None, "externalUrl": None, "lastPostTs": None,
        "enriched": False,
        "source": source or "",
        "status": "pending",
        "note": "",
        "attempts": 0,
        "actedAt": None,
    }


def apply_profile(acct: dict, user: dict) -> dict:
    """Aplica os dados do web_profile_info sobre uma conta já na fila."""
    if not user:
        return acct
    acct["id"] = str(user.get("id") or acct.get("id") or "")
    acct["fullName"] = user.get("full_name") or acct.get("fullName") or ""
    acct["isPrivate"] = user.get("is_private") is True
    acct["isVerified"] = user.get("is_verified") is True
    acct["bio"] = user.get("biography") or ""
    acct["isBusiness"] = user.get("is_business_account") is True
    acct["externalUrl"] = user.get("external_url") or ""
    acct["followers"] = (user.get("edge_followed_by") or {}).get("count")
    acct["following"] = (user.get("edge_follow") or {}).get("count")
    media = user.get("edge_owner_to_timeline_media") or {}
    acct["posts"] = media.get("count")

    pic = user.get("profile_pic_url")
    if pic:
        acct["picUrl"] = pic
        acct["hasPic"] = DEFAULT_AVATAR_MARKER not in pic
    if user.get("followed_by_viewer") is not None:
        acct["followedByViewer"] = user.get("followed_by_viewer") is True
    if user.get("requested_by_viewer") is not None:
        acct["requestedByViewer"] = user.get("requested_by_viewer") is True

    edges = media.get("edges") or []
    if edges and (edges[0].get("node") or {}).get("taken_at_timestamp"):
        acct["lastPostTs"] = int(edges[0]["node"]["taken_at_timestamp"]) * 1000

    acct["enriched"] = True
    return acct


async def _follow_one(page, user_id, username) -> dict:
    """Segue uma conta. Tenta a API v1 e cai para /web/ se a resposta vier inválida.
    Retorna {ok, following, requested, status, payload}."""
    referer = f"{IG}/{username}/"
    # Body VAZIO de propósito. Esta é a ÚNICA forma desta requisição comprovada contra o
    # Instagram real: o auto-follow do spider.py (`_follow`) seguiu @xerifedoconsumidor ao
    # vivo em 2026-06-21 com body ''. O `container_module=profile` veio da extensão, cuja
    # camada de follow nunca rodou contra a API real (HANDOFF §11) — e era a única
    # diferença entre o caminho que funciona e este, que devolve 404 em tudo.
    res = await page.evaluate(_POST_JS, [f"{IG}/api/v1/friendships/create/{user_id}/",
                                         referer, ""])
    j = res.get("json") or {}
    fs = j.get("friendship_status") or {}
    if res.get("ok") and j.get("status") == "ok" and (fs.get("following") is True
                                                      or fs.get("outgoing_request") is True):
        return {"ok": True, "following": fs.get("following") is True,
                "requested": fs.get("outgoing_request") is True,
                "status": res.get("status"), "payload": j}

    st = res.get("status")
    # 400/403/429 são resposta DEFINITIVA do IG (limite/bloqueio): insistir noutro
    # endpoint só piora. Já o 404 é diferente — "não existe" no endpoint v1 pode ser
    # a ROTA errada para esta sessão, não a pessoa. A rota /web/ é a que a interface
    # web usava, então vale tentar (medido: v1 devolvendo 404 em TODA a fila).
    if isinstance(st, int) and st in (400, 401, 403, 429):
        return {"ok": False, "status": st, "payload": j, "body": res.get("text") or ""}

    fb = await page.evaluate(_POST_JS, [f"{IG}/web/friendships/{user_id}/follow/", referer, ""])
    fj = fb.get("json") or {}
    if fb.get("ok") and fj.get("status") == "ok":
        result = str(fj.get("result") or "").lower()
        ffs = fj.get("friendship_status") or {}
        return {"ok": True, "via": "web",
                "following": (ffs.get("following") is True) or result == "following",
                "requested": (ffs.get("outgoing_request") is True) or result == "requested",
                "status": fb.get("status"), "payload": fj}

    # Falhou nos dois: devolve o corpo das DUAS respostas para o log dizer o porquê
    # (HTML de 404 = rota errada; JSON com mensagem = a conta/limite é que barra).
    return {"ok": False, "status": st if st not in (None, 200) else fb.get("status"),
            "payload": j or fj,
            "body": (res.get("text") or "")[:200],
            "body_web": (fb.get("text") or "")[:200],
            "status_web": fb.get("status")}


async def _unfollow_one(page, user_id, username) -> dict:
    """Desfaz o follow. Espelha _follow_one: v1 `friendships/destroy/` e fallback
    `/web/friendships/<id>/unfollow/`. Sucesso = status ok e `following` FALSO."""
    referer = f"{IG}/{username}/"
    res = await page.evaluate(_POST_JS, [f"{IG}/api/v1/friendships/destroy/{user_id}/",
                                         referer, "container_module=profile"])
    j = res.get("json") or {}
    fs = j.get("friendship_status") or {}
    if res.get("ok") and j.get("status") == "ok" and fs.get("following") is not True:
        return {"ok": True, "status": res.get("status"), "payload": j}

    st = res.get("status")
    if isinstance(st, int) and 400 <= st < 500:
        return {"ok": False, "status": st, "payload": j}

    fb = await page.evaluate(_POST_JS, [f"{IG}/web/friendships/{user_id}/unfollow/", referer, ""])
    fj = fb.get("json") or {}
    if fb.get("ok") and fj.get("status") == "ok":
        return {"ok": True, "status": fb.get("status"), "payload": fj}

    return {"ok": False, "status": st if st not in (None, 200) else fb.get("status"),
            "payload": j or fj}


async def _session_ok(page, user_id) -> dict:
    """Sessão viva? UMA requisição GraphQL de seguidores (first:1) — o mesmo teste do
    ig_session.probe_login: só responde 200 autenticado, 401 quando a sessão caiu.
    Não segue ninguém, não pagina. Serve para NÃO queimar itens da fila descobrindo
    na marra que o perfil está deslogado."""
    try:
        res = await spider._graphql(page, spider._GQL["followers"],
                                    {"id": str(user_id), "first": 1}, f"{IG}/")
        st = res.get("status")
        tem_dados = bool((((res.get("json") or {}).get("data") or {})
                          .get("user") or {}).get("edge_followed_by"))
        return {"ok": bool(res.get("ok")) and tem_dados, "status": st}
    except Exception as e:
        return {"ok": False, "status": -1, "error": str(e)}


async def _friendship_show(page, user_id) -> dict:
    """Estado do relacionamento com um usuário: {following, followed_by, ...}.
    UMA requisição — é o que diz se a pessoa retribuiu o follow."""
    res = await spider._api_get(page, f"{IG}/api/v1/friendships/show/{user_id}/", f"{IG}/")
    return {"ok": bool(res.get("ok")), "status": res.get("status"),
            "data": res.get("json") or {}}


async def viewer_following_count(page, username: str):
    """Quantos perfis a CONTA LOGADA segue hoje — para o teto de 7.500 do IG.
    Retorna (seguindo, total_seguidores) ou (None, None)."""
    uid, info = await spider._resolve_user(page, username, log=None)
    if not uid:
        return None, None
    return ((info.get("edge_follow") or {}).get("count"),
            (info.get("edge_followed_by") or {}).get("count"))


async def run_unfollow(profile: dict, candidatos: list, settings: dict, *,
                       viewer: str = "", use_local: bool = False, actions: list = None,
                       should_stop=None, log=None, on_action=None, on_counts=None) -> dict:
    """Desfaz follows dos `candidatos` (vindos do nosso histórico: quem ESTA conta
    seguiu há N dias e ainda não foi desfeito).

    Regras, espelhando o GrowBot: não desfaz quem retribuiu (`protectMutuals`),
    nunca toca na whitelist (filtrada na consulta), e só mexe no que o app seguiu
    (o histórico É a fonte, então isso vem de graça).

    Cada item é um dict {username, user_id, ts_ms}. Muta-os com status/note.
    Retorna {stats, blocked, aborted, error, seguindo_antes, seguindo_depois}."""
    s = {**DEFAULT_SETTINGS, **(settings or {})}
    actions = actions if actions is not None else []
    dry = s.get("dryRun") is True
    proteger = s.get("protectMutuals") is True
    if use_local:
        profile = {**profile, "proxy": None}

    stats = {"unfollowed": 0, "skipped": 0, "failed": 0, "simulated": 0}
    meta = {"blocked": False, "aborted": False, "error": None,
            "seguindo_antes": None, "seguindo_depois": None}

    def _finish(item, status, note, kind):
        item["status"] = status
        item["note"] = note
        item["actedAt"] = int(time.time() * 1000)
        stats[kind] = stats.get(kind, 0) + 1
        if on_action:
            on_action(item, kind, item["actedAt"])

    async def _run(page):
        if log:
            log(f"{'🔎 SIMULAÇÃO — ' if dry else ''}{len(candidatos)} candidato(s) a deixar de seguir"
                + (" · protegendo quem retribuiu" if proteger else " · SEM proteger mútuos"))
        await page.goto(IG + "/", wait_until="domcontentloaded")
        await asyncio.sleep(round(random.uniform(1.0, 2.5), 1))

        if viewer:
            seguindo, _ = await viewer_following_count(page, viewer)
            meta["seguindo_antes"] = seguindo
            if seguindo is not None and log:
                livre = IG_FOLLOWING_LIMIT - seguindo
                log(f"📊 @{viewer} segue {seguindo}/{IG_FOLLOWING_LIMIT} "
                    f"({livre} vaga(s) livre(s) até o teto do Instagram)")

        consecutive = 0
        for item in candidatos:
            if should_stop and should_stop():
                if log:
                    log("⏹ parado pelo usuário")
                break
            if item.get("status") not in (None, "", "pending"):
                continue

            if not dry:
                ok, reason, wait = limiter_check(actions, s)
                if not ok:
                    if log:
                        log(f"⏳ teto {reason} atingido. Retomando em {human_duration(wait)}.")
                    if await spider._sleep_stop(wait, should_stop):
                        break
                    continue

            uid = item.get("user_id")
            uname = item.get("username")

            # Estado atual: ela retribuiu? ainda seguimos? (1 requisição)
            if proteger or not uid:
                try:
                    if not uid:
                        uid, _info = await spider._resolve_user(page, uname, log=None)
                        item["user_id"] = uid
                    if not uid:
                        _finish(item, "skipped", "perfil não resolvido (apagado?)", "skipped")
                        continue
                    fs = await _friendship_show(page, uid)
                    d = fs.get("data") or {}
                    if fs["ok"] and d.get("following") is False:
                        _finish(item, "skipped", "já não seguíamos", "skipped")
                        if await spider._sleep_stop(1.5, should_stop):
                            break
                        continue
                    if proteger and fs["ok"] and d.get("followed_by") is True:
                        _finish(item, "skipped", "retribuiu o follow — protegida", "skipped")
                        if log:
                            log(f"🤝 @{uname} — mantida: ela te segue de volta")
                        if await spider._sleep_stop(jitter(s["skipDelayMs"], s["jitterPercent"]),
                                                    should_stop):
                            break
                        continue
                except Exception as e:
                    if log:
                        log(f"⚠ @{uname}: não consegui checar o relacionamento ({str(e)[:50]})")

            if dry:
                _finish(item, "simulated", "deixaria de seguir (simulação)", "simulated")
                if log:
                    log(f"🔎 @{uname} — DEIXARIA de seguir ({stats['simulated']})")
                if await spider._sleep_stop(jitter(s["skipDelayMs"], s["jitterPercent"]), should_stop):
                    break
                continue

            try:
                res = await _unfollow_one(page, uid, uname)
            except Exception as e:
                res = {"ok": False, "status": -1, "payload": None, "error": str(e)}

            if not res.get("ok"):
                verdict = classify_error(res.get("status"), res.get("payload"), s)
                if verdict["abort"]:
                    meta["error"] = verdict["label"]
                    meta["aborted"] = True
                    if verdict["kind"] in ("blocked", "session"):
                        meta["blocked"] = True
                    if log:
                        log(f"✖ abortando: {verdict['label']}")
                    break
                if verdict["kind"] == "notfound":
                    _finish(item, "skipped", "conta não existe mais", "skipped")
                    continue
                consecutive += 1
                _finish(item, "failed", verdict["label"], "failed")
                if log:
                    log(f"⚠ @{uname}: {verdict['label']}. Pausa de {human_duration(verdict['wait'])}.")
                if consecutive >= int(s["maxConsecutiveFailures"]):
                    meta["error"] = f"{consecutive} falhas seguidas"
                    meta["aborted"] = True
                    if log:
                        log(f"✖ abortando após {consecutive} falhas seguidas.")
                    break
                if await spider._sleep_stop(verdict["wait"], should_stop):
                    break
                continue

            consecutive = 0
            _finish(item, "unfollowed", "", "unfollowed")
            ts = item["actedAt"]
            actions.append(ts)          # unfollow gasta o MESMO orçamento de ações
            counts = limiter_counts(actions)
            if on_counts:
                on_counts(counts)
            wait = jitter(s["baseDelayMs"], s["jitterPercent"])
            if log:
                log(f"✓ @{uname} — deixou de seguir · {counts['hour']}/h {counts['day']}/d "
                    f"· próximo em {human_duration(wait)}")
            if await spider._sleep_stop(wait, should_stop):
                break

        if viewer and not dry and stats["unfollowed"]:
            try:
                seguindo, _ = await viewer_following_count(page, viewer)
                meta["seguindo_depois"] = seguindo
            except Exception:
                pass

    try:
        await sm.run_profile_task(profile, _run, headless=True)
    except Exception as e:
        meta["error"] = meta["error"] or f"erro ao abrir o perfil: {e}"
        if log:
            log(f"✖ {meta['error']}")

    return {"stats": stats, **meta}


# ── Coleta (§ fontes) ─────────────────────────────────────────────────────────

async def _harvest_friendships(page, user_id, username, edge, limit, page_delay_ms,
                               jitter_percent, should_stop, log, progress, source,
                               start_cursor=None, conhecidos=None, max_pages=20):
    """Pagina followers/following via GraphQL (o mesmo caminho do Spider — a API v1
    /friendships/ capa em ~50). Mantém os campos crus que os filtros grátis usam.

    `limit` conta **só gente NOVA**: quem está em `conhecidos` é descartado e não
    ocupa vaga. Sem isso, um alvo cujo topo já foi todo seguido "enchia" a fatia com
    conhecidos e a visita terminava sem trazer ninguém — era o "pula antes de pegar
    o número necessário". `max_pages` limita quantas páginas por VISITA, para o
    rodízio entre alvos continuar existindo em vez de cavar fundo num só.

    Retorna (itens_novos, cursor, pulados). `cursor` != None = o alvo ainda tem mais."""
    qhash = spider._GQL["following" if edge == "following" else "followers"]
    edge_key = "edge_follow" if edge == "following" else "edge_followed_by"
    referer = f"{IG}/{username}/{'following' if edge == 'following' else 'followers'}/"
    conhecidos = conhecidos if conhecidos is not None else set()
    items, seen, cursor, page_n, pulados = [], set(), start_cursor, 0, 0

    while len(items) < limit and page_n < max_pages:
        if should_stop and should_stop():
            if log:
                log("⏹ parado pelo usuário")
            break
        # Sempre pede a página cheia (50): pedir só o que falta desperdiçaria a
        # requisição quando a maioria da página já é conhecida.
        variables = {"id": str(user_id), "first": 50}
        if cursor:
            variables["after"] = cursor
        res = await spider._graphql(page, qhash, variables, referer)
        st = res.get("status")
        data = ((((res.get("json") or {}).get("data") or {}).get("user") or {}).get(edge_key))
        if not res.get("ok") or not data:
            if log:
                log(f"✖ a coleta parou (status {st}) — possível bloqueio, sessão ou alvo indisponível")
            break

        for e in (data.get("edges") or []):
            node = e.get("node") or {}
            acct = to_account(node, source)
            if not acct["id"] or not acct["username"] or acct["id"] in seen:
                continue
            seen.add(acct["id"])
            if acct["username"] in conhecidos:   # já seguida/blacklist: não ocupa vaga
                pulados += 1
                continue
            items.append(acct)
            if len(items) >= limit:
                break

        page_n += 1
        if progress:
            progress(len(items))
        if log:
            log(f"📄 página {page_n} · {len(items)}/{limit} nova(s)"
                + (f" · {pulados} já conhecida(s)" if pulados else ""))

        info = data.get("page_info") or {}
        cursor = info.get("end_cursor") if info.get("has_next_page") else None
        if not cursor or len(items) >= limit:
            break
        if await spider._sleep_stop(jitter(page_delay_ms, jitter_percent), should_stop):
            break

    if log and page_n >= max_pages and cursor and len(items) < limit:
        log(f"   ⏭ @{username}: {max_pages} páginas nesta visita — passando a vez "
            f"(volta aqui na próxima rodada, do ponto onde parou)")
    return items, cursor, pulados


async def _harvest_one(page, source_type: str, target: str, per_target: int,
                       page_delay_ms, jitter_percent, should_stop, log, progress,
                       estado: dict = None, conhecidos=None) -> dict:
    """Coleta de UM alvo, dentro de uma sessão já aberta. Fatia RASA por design.

    `estado` guarda o que já se sabe do alvo entre as rodadas (uid resolvido e o
    cursor de onde parou), para que uma 2ª passada CONTINUE em vez de repetir a
    mesma fatia. Retorna {items, source, error, cursor, esgotado}."""
    estado = estado if estado is not None else {}
    if source_type == "likers":
        shortcode = shortcode_from_url(target)
        if not shortcode:
            return {"items": [], "source": "", "error": "URL de post inválida (use o link /p/...)"}
        media_id = shortcode_to_media_id(shortcode)
        if not media_id:
            return {"items": [], "source": "", "error": f'shortcode "{shortcode}" inválido'}
        source = f"likers:{shortcode}"
        if log:
            log(f"❤ curtidas do post {shortcode}…")
        res = await spider._api_get(page, f"{IG}/api/v1/media/{media_id}/likers/",
                                    f"{IG}/p/{shortcode}/")
        if not res.get("ok"):
            return {"items": [], "source": source,
                    "error": f"o Instagram recusou as curtidas (status {res.get('status')})"}
        users = (res.get("json") or {}).get("users") or []
        conhecidos = conhecidos if conhecidos is not None else set()
        items, seen, pulados = [], set(), 0
        for u in users:
            acct = to_account(u, source)
            if not acct["id"] or not acct["username"] or acct["id"] in seen:
                continue
            seen.add(acct["id"])
            if acct["username"] in conhecidos:
                pulados += 1
                continue
            items.append(acct)
            if len(items) >= per_target:
                break
        # /likers/ não pagina: uma passada e acabou.
        return {"items": items, "source": source, "error": None, "pulados": pulados,
                "cursor": None, "esgotado": True}

    username = spider.clean_username(target)
    if not username:
        return {"items": [], "source": "", "error": "alvo vazio", "esgotado": True}

    uid = estado.get("uid")
    if not uid:
        uid, info = await spider._resolve_user(page, target, log=None)
        if not uid:
            return {"items": [], "source": f"{source_type}:{username}", "esgotado": True,
                    "error": f"não resolveu (status {(info or {}).get('status')})"}
        if info.get("is_private") and not info.get("followed_by_viewer"):
            return {"items": [], "source": f"{source_type}:{username}", "esgotado": True,
                    "error": "perfil privado e esta conta não segue"}
        estado["uid"] = uid
        estado["total"] = spider._total_for(info, source_type)
        if log:
            log(f"👥 @{username} ({estado['total'] if estado['total'] is not None else '?'} "
                f"{'seguidores' if source_type == 'followers' else 'seguindo'}) — "
                f"pegando até {per_target} do topo")
    elif log:
        log(f"↩ voltando em @{username} para completar o total (+{per_target})")

    source = f"{source_type}:{username}"
    items, cursor, pulados = await _harvest_friendships(
        page, uid, username, source_type, per_target, page_delay_ms, jitter_percent,
        should_stop, log, progress, source, start_cursor=estado.get("cursor"),
        conhecidos=conhecidos)
    estado["cursor"] = cursor
    return {"items": items, "source": source, "error": None, "pulados": pulados,
            "cursor": cursor, "esgotado": not cursor}


async def harvest(profile: dict, source_type: str, targets, per_target: int = 50, *,
                  limit: int = 0, conhecidos=None, use_local: bool = False,
                  page_delay_ms: int = 2500, jitter_percent: float = 0.3,
                  should_stop=None, log=None, progress=None) -> dict:
    """Coleta contas para a fila, ALTERNANDO entre vários alvos. NADA é seguido aqui.

    `targets` aceita string (um alvo) ou lista. De cada alvo pega só uma FATIA RASA
    (o topo da lista, `per_target` contas) e passa para o próximo, em vez de paginar
    fundo num alvo só. Duas razões:
      1. Risco: paginar fundo é o que queima conta (o Spider mostrou que a lista
         profunda exige seguir o alvo e ainda assim derruba contas). O topo da lista
         é a leitura mais banal que existe — é o que qualquer usuário vê ao abrir o
         perfil.
      2. Qualidade: o topo traz os seguidores MAIS RECENTES do alvo (contas ativas
         agora), enquanto o fundo é gente que seguiu há anos e pode estar morta.
    Alternar entre N alvos rasos dá o mesmo volume que um alvo fundo, com uma fração
    do risco e material melhor.

    `limit` é o total pedido e é ENTREGUE: a divisão entre alvos é feita sem truncar
    (o resto é distribuído) e, se ainda faltar — por dedup entre alvos ou por um alvo
    render menos —, o laço VOLTA nos alvos que ainda têm lista, continuando do cursor
    de cada um, até fechar o total ou esgotar todos.

    `conhecidos` (usernames já seguidos/na blacklist/já na fila) são descartados
    DURANTE a coleta e NÃO contam para o total — senão pedir 1000 devolvia 1000
    brutos que viravam ~500 depois do filtro. O total pedido é de gente NOVA.

    source_type: 'followers' | 'following' | 'likers'.
    Retorna {items, por_alvo, erros, error, ja_conhecidos}."""
    if use_local:
        profile = {**profile, "proxy": None}
    if isinstance(targets, str):
        targets = [targets]
    alvos = [t.strip() for t in (targets or []) if t and t.strip()]
    per_target = max(1, int(per_target or 50))
    limit = max(0, int(limit or 0))
    conhecidos = set(conhecidos or ())
    out = {"items": [], "source": "", "error": None, "por_alvo": {}, "erros": {},
           "ja_conhecidos": 0}
    if not alvos:
        out["error"] = "Informe ao menos um alvo."
        return out

    # Cota por alvo = o total dividido IGUALMENTE entre os alvos, sem truncar (1000
    # em 7 = 5×143 + 2×142 = 1000). O antigo "quantas por alvo" fixo virava um teto
    # menor que a divisão (50 × 7 = 350) e a coleta parava cedo — por isso a divisão
    # manda quando há total definido. Sem total, cai no valor por alvo.
    if limit:
        base, resto = divmod(limit, len(alvos))
        cotas = [base + (1 if i < resto else 0) for i in range(len(alvos))]
        fatia = max(1, base + (1 if resto else 0))   # fatia por rodada nas seguintes
    else:
        cotas = [per_target] * len(alvos)
        fatia = per_target

    async def _run(page):
        actor = profile.get("name") or "perfil"
        conn = "IP local (sem proxy)" if use_local else (
            f"proxy {(profile.get('proxy') or {}).get('host')}" if profile.get("proxy") else "sem proxy")
        if log:
            log(f"🧭 coletando com a conta/perfil: '{actor}' · {conn}")
            if limit:
                log(f"🎯 {limit} contas novas ÷ {len(alvos)} alvo(s) = "
                    f"~{cotas[0]} de cada (divisão igual; sobra o resto nos primeiros)")
            else:
                log(f"🎯 {len(alvos)} alvo(s), até {per_target} do topo de cada")
        await page.goto(IG + "/", wait_until="domcontentloaded")
        await asyncio.sleep(round(random.uniform(1.0, 2.5), 1))

        vistos = set()
        estados = {a: {} for a in alvos}        # uid + cursor de cada alvo
        vivos = list(alvos)                     # alvos que ainda têm lista
        rodada = 0

        def _falta():
            return (limit - len(out["items"])) if limit else 0

        while vivos:
            rodada += 1
            antes_da_rodada = len(out["items"])
            if rodada > 1 and log:
                log(f"↻ rodada {rodada}: faltam {_falta()} para fechar {limit} — "
                    f"voltando nos alvos que ainda têm lista")
            for i, alvo in enumerate(list(vivos)):
                if should_stop and should_stop():
                    if log:
                        log("⏹ parado pelo usuário")
                    vivos = []
                    break
                if limit and _falta() <= 0:
                    vivos = []
                    break
                # 1ª rodada usa a cota calculada; nas seguintes pega o que falta
                # (limitado pela fatia, para não cavar fundo num alvo só).
                if rodada == 1:
                    pedir = cotas[alvos.index(alvo)]
                else:
                    pedir = min(fatia, _falta()) if limit else fatia
                pedir = max(1, min(pedir, _falta()) if limit else pedir)

                r = await _harvest_one(page, source_type, alvo, pedir, page_delay_ms,
                                       jitter_percent, should_stop, log, progress,
                                       estado=estados[alvo], conhecidos=conhecidos)
                if r.get("error"):
                    out["erros"][alvo] = r["error"]
                    if log:
                        log(f"⚠ {alvo}: {r['error']} — indo para o próximo alvo")
                out["ja_conhecidos"] += r.get("pulados") or 0
                novos = 0
                for a in r.get("items") or []:
                    if a["id"] in vistos:      # dedup ENTRE alvos (seguidores em comum)
                        continue
                    vistos.add(a["id"])
                    # A filtragem por "já conhecido" acontece na paginação (não ocupa
                    # vaga lá); aqui é só rede de segurança.
                    if a["username"] in conhecidos:
                        out["ja_conhecidos"] += 1
                        continue
                    out["items"].append(a)
                    conhecidos.add(a["username"])   # não repetir em rodadas seguintes
                    novos += 1
                    if limit and len(out["items"]) >= limit:
                        break
                if r.get("source"):
                    out["por_alvo"][r["source"]] = out["por_alvo"].get(r["source"], 0) + novos
                    out["source"] = out["source"] or r["source"]
                if log and not r.get("error"):
                    pl = r.get("pulados") or 0
                    log(f"   ✓ {novos} nova(s) de {r.get('source')}"
                        + (f" ({pl} já conhecida(s), não contam)" if pl else "")
                        + f" · total {len(out['items'])}" + (f"/{limit}" if limit else ""))
                if progress:
                    progress(len(out["items"]))
                # Alvo sem mais lista (ou com erro) sai do rodízio.
                if r.get("esgotado") or r.get("error"):
                    vivos.remove(alvo)
                # Pausa entre alvos: trocar de perfil é navegação humana normal, mas
                # emendar um no outro sem respirar não é.
                if vivos and i < len(vivos) - 1:
                    if await spider._sleep_stop(jitter(page_delay_ms * 2, jitter_percent),
                                                should_stop):
                        vivos = []
                        break
            if not limit:
                break                          # sem teto: uma passada só por alvo
            if limit and len(out["items"]) >= limit:
                break
            # Rodada inteira sem UMA conta nova: os alvos que sobraram só têm gente
            # que já seguimos. Alvo assim nunca "esgota" (o cursor sempre existe), o
            # que sem esta parada viraria laço infinito.
            if len(out["items"]) == antes_da_rodada:
                if log:
                    log("⏹ a rodada não trouxe ninguém novo — os alvos restantes só "
                        "têm gente que já seguimos. Acrescente alvos novos para ir além.")
                break

        if log and limit:
            extra = (f" · {out['ja_conhecidos']} já conhecida(s) foram puladas no caminho"
                     if out["ja_conhecidos"] else "")
            if len(out["items"]) >= limit:
                log(f"🎯 total fechado: {len(out['items'])}/{limit} novas{extra}")
            else:
                log(f"⚠ só deu {len(out['items'])}/{limit} novas{extra} — os alvos acabaram "
                    f"(listas menores que o pedido, ou já seguimos quase todo mundo deles)")

        if not out["items"] and out["erros"]:
            out["error"] = "nenhum alvo devolveu contas — " + \
                "; ".join(f"{k}: {v}" for k, v in list(out["erros"].items())[:3])

    try:
        await sm.run_profile_task(profile, _run, headless=True)
    except Exception as e:
        out["error"] = f"erro ao abrir o perfil: {e}"
        if log:
            log(f"✖ {out['error']}")
    return out


# ── Motor (§9 — o laço) ───────────────────────────────────────────────────────

async def run(profile: dict, queue: list, settings: dict, filters: dict, *,
              history: dict = None, blacklist=None, actions: list = None,
              use_local: bool = False, should_stop=None, log=None,
              on_action=None, on_counts=None, max_actions: int = 0,
              yield_on_cap: bool = False) -> dict:
    """Consome a fila: teto → pré-checagens locais → enriquecimento → filtros →
    follow → intervalo com jitter. Muta os itens da fila (status/note/attempts).

    on_action(acct, kind, ts) é chamado a cada desfecho (followed/requested/
    skipped/failed/simulated) — quem persiste (fila, histórico, contadores) é o
    chamador, para o motor continuar agnóstico de storage.

    max_actions > 0 encerra o TURNO após N follows efetivados (não conta pulados),
    devolvendo `turno_cheio=True` — é o que faz o rodízio trocar de conta a cada N.
    yield_on_cap: ao bater o teto horário/diário, ENCERRA o turno (`cap_atingido`)
    em vez de dormir até abrir vaga. Com várias contas é o certo — dormir seguraria
    a fila por horas enquanto as outras contas estão livres.
    Retorna {stats, blocked, aborted, error, turno_cheio, cap_atingido}."""
    s = {**DEFAULT_SETTINGS, **(settings or {})}
    f = {**DEFAULT_FILTERS, **(filters or {})}
    history = history if history is not None else {}
    blacklist = set(blacklist or [])
    actions = actions if actions is not None else []
    dry = s.get("dryRun") is True

    if use_local:
        profile = {**profile, "proxy": None}

    stats = {"followed": 0, "requested": 0, "skipped": 0, "failed": 0, "simulated": 0}
    meta = {"blocked": False, "aborted": False, "error": None, "turno_cheio": False,
            "cap_atingido": False, "conta_404": False}
    enrich = needs_enrichment(f)
    cota = max(0, int(max_actions or 0))

    def _finish(acct, status, note, kind):
        acct["status"] = status
        acct["note"] = note
        acct["actedAt"] = int(time.time() * 1000)
        stats[kind] = stats.get(kind, 0) + 1
        if on_action:
            on_action(acct, kind, acct["actedAt"])

    async def _run(page):
        actor = profile.get("name") or "perfil"
        conn = "IP local (sem proxy)" if use_local else (
            f"proxy {(profile.get('proxy') or {}).get('host')}" if profile.get("proxy") else "sem proxy")
        pending = [a for a in queue if a.get("status") == "pending"]
        if log:
            log(f"🧭 conta/perfil: '{actor}' · {conn}")
            log(f"{'🔎 SIMULAÇÃO (nada será seguido) — ' if dry else ''}"
                f"{len(pending)} conta(s) pendente(s)."
                + (" Enriquecimento de perfil ligado (1 requisição extra por conta)." if enrich else ""))
        await page.goto(IG + "/", wait_until="domcontentloaded")
        await asyncio.sleep(round(random.uniform(1.0, 2.5), 1))

        # ── sonda de sessão (1 requisição, antes de agir) ──
        # Sem isto, perfil deslogado só era descoberto queimando itens da fila.
        if not dry:
            primeiro = next((a for a in queue if a.get("status") == "pending"), None)
            if primeiro and primeiro.get("id"):
                sonda = await _session_ok(page, primeiro["id"])
                # Só aborta em falha CLARA de autenticação. Um piscar de rede (-1/5xx)
                # ou rate limit não pode derrubar a execução inteira — para isso já
                # existe o tratamento de erro do laço.
                if not sonda["ok"] and sonda.get("status") in (401, 403):
                    meta["error"] = (f"a sessão deste perfil não está autenticada "
                                     f"(status {sonda['status']}) — abra o perfil e confirme "
                                     f"que o Instagram está logado antes de disparar.")
                    meta["aborted"] = True
                    meta["blocked"] = True
                    if log:
                        log(f"✖ {meta['error']}")
                        log("   (nenhum item da fila foi tocado)")
                    return
                if log:
                    log("✓ sessão do perfil confirmada" if sonda["ok"] else
                        f"⚠ sonda de sessão inconclusiva (status {sonda.get('status')}) — "
                        f"seguindo mesmo assim")

        consecutive = 0
        seguidas_404 = 0        # 404 em sequência = sinal sistêmico, não conta apagada
        ultimos_404 = []        # devolvidas à fila se a sequência virar abortar
        while True:
            if should_stop and should_stop():
                if log:
                    log("⏹ parado pelo usuário")
                break

            acct = next((a for a in queue if a.get("status") == "pending"), None)
            if not acct:
                if log:
                    log("✓ fila sem contas pendentes")
                break

            # ── tetos (só valem para ação real) ──
            if not dry:
                ok, reason, wait = limiter_check(actions, s)
                if not ok:
                    if yield_on_cap:
                        meta["cap_atingido"] = True
                        if log:
                            log(f"🛑 teto {reason} atingido — encerrando o turno desta conta "
                                f"(vaga só em {human_duration(wait)})")
                        break
                    if log:
                        log(f"⏳ teto {reason} atingido. Retomando em {human_duration(wait)}.")
                    if await spider._sleep_stop(wait, should_stop):
                        break
                    continue

            # ── pré-checagens locais (não gastam requisição) ──
            if acct["username"] in blacklist:
                _finish(acct, "skipped", "na blacklist", "skipped")
                if log:
                    log(f"@{acct['username']} — pulada: na blacklist")
                if await spider._sleep_stop(0.5, should_stop):
                    break
                continue

            if s.get("skipAlreadyAttempted") and history.get(acct["username"]):
                _finish(acct, "skipped", "já tentada antes", "skipped")
                if log:
                    log(f"@{acct['username']} — pulada: já tentada antes")
                if await spider._sleep_stop(0.5, should_stop):
                    break
                continue

            # ── enriquecimento sob demanda ──
            if enrich and not acct.get("enriched"):
                try:
                    uid, info = await spider._resolve_user(page, acct["username"])
                    if not uid:
                        _finish(acct, "failed", "perfil indisponível", "failed")
                        if log:
                            log(f"@{acct['username']} — falhou: perfil indisponível")
                        if await spider._sleep_stop(3, should_stop):
                            break
                        continue
                    apply_profile(acct, info)
                except Exception as e:
                    verdict = classify_error(0, None, s)
                    if log:
                        log(f"@{acct['username']}: falha ao ler o perfil ({e}). "
                            f"Aguardando {human_duration(verdict['wait'])}.")
                    acct["attempts"] = acct.get("attempts", 0) + 1
                    if acct["attempts"] >= int(s["maxAttemptsPerAccount"]):
                        _finish(acct, "failed", "perfil: falha repetida", "failed")
                    if await spider._sleep_stop(verdict["wait"], should_stop):
                        break
                    continue
                if await spider._sleep_stop(jitter(s["enrichDelayMs"], s["jitterPercent"]), should_stop):
                    break

            # ── filtros ──
            passed, reason = evaluate(acct, f)
            if not passed:
                _finish(acct, "skipped", reason, "skipped")
                if log:
                    log(f"@{acct['username']} — pulada: {reason}")
                if await spider._sleep_stop(jitter(s["skipDelayMs"], s["jitterPercent"]), should_stop):
                    break
                continue

            # ── simulação: decide tudo, não age ──
            if dry:
                _finish(acct, "simulated", "seguiria (simulação)", "simulated")
                if log:
                    log(f"🔎 @{acct['username']} — SERIA seguida ({stats['simulated']})")
                if await spider._sleep_stop(jitter(s["skipDelayMs"], s["jitterPercent"]), should_stop):
                    break
                continue

            # ── follow ──
            try:
                res = await _follow_one(page, acct["id"], acct["username"])
            except Exception as e:
                res = {"ok": False, "status": -1, "payload": None, "error": str(e)}

            # 404 pode ser o ID errado, não a pessoa. O id da fila vem do `node.id` do
            # GraphQL da coleta; o caminho que comprovadamente funciona (o auto-follow
            # do Spider) usa o pk do web_profile_info. Então resolve o username e tenta
            # UMA vez com o pk real — se der certo, o bloco de erro abaixo nem roda.
            if (not res.get("ok") and res.get("status") == 404
                    and not acct.get("id_resolvido")):
                acct["id_resolvido"] = True
                try:
                    pk, _info = await spider._resolve_user(page, acct["username"], log=None)
                except Exception:
                    pk = None
                if pk and str(pk) != str(acct.get("id")):
                    if log:
                        log(f"   ↳ id da fila {acct.get('id')} ≠ id resolvido {pk} — "
                            f"tentando de novo com o id real")
                    acct["id"] = str(pk)
                    try:
                        res = await _follow_one(page, acct["id"], acct["username"])
                    except Exception as e:
                        res = {"ok": False, "status": -1, "payload": None, "error": str(e)}
                elif pk and log:
                    log(f"   ↳ id resolvido é o MESMO ({pk}) — não é o id que está errado")

            if not res.get("ok"):
                verdict = classify_error(res.get("status"), res.get("payload"), s)

                if verdict["abort"]:
                    meta["error"] = verdict["label"]
                    meta["aborted"] = True
                    if verdict["kind"] in ("blocked", "session"):
                        meta["blocked"] = True
                    acct["note"] = verdict["label"]
                    if log:
                        log(f"✖ abortando: {verdict['label']}. Não insista agora — espere algumas horas.")
                    break

                if verdict["kind"] == "notfound":
                    _finish(acct, "failed", "conta não encontrada", "failed")
                    seguidas_404 += 1
                    ultimos_404.append(acct)
                    if log:
                        log(f"@{acct['username']} — falhou: conta não encontrada"
                            + (f" (id {acct.get('id')})" if seguidas_404 == 1 else ""))
                        if seguidas_404 == 1:
                            # O corpo distingue as duas causas possíveis: HTML de 404
                            # = rota errada para esta sessão; JSON com mensagem = é a
                            # conta/limite que barra.
                            log(f"   ↳ v1 {res.get('status')}: {(res.get('body') or '(vazio)')[:140]}")
                            log(f"   ↳ /web/ {res.get('status_web')}: "
                                f"{(res.get('body_web') or '(vazio)')[:140]}")
                    # Uma 404 avulsa é normal (conta apagada). Várias SEGUIDAS não:
                    # a fila foi coletada minutos antes, essa gente existe. Sinal de
                    # sessão caída/ids inválidos — parar em vez de varrer a fila
                    # inteira marcando tudo como falha.
                    if seguidas_404 >= max(3, int(s["maxConsecutiveFailures"])):
                        # Devolve à fila quem foi marcado nesta sequência: a causa é
                        # sistêmica, essas contas provavelmente existem.
                        for a in ultimos_404:
                            a["status"] = "pending"
                            a["note"] = "404 em sequência — devolvida à fila"
                            stats["failed"] = max(0, stats["failed"] - 1)
                        # Leitura funciona (a sonda passou) mas o follow devolve 404 em
                        # tudo: cheira a ESTA CONTA com escrita bloqueada, não à fila.
                        # Encerra o TURNO e deixa o rodízio testar a próxima conta — se
                        # ela seguir normalmente, o problema era a conta; se todas derem
                        # 404, é sistêmico (rota/código). O diagnóstico sai de graça.
                        meta["error"] = (f"{seguidas_404} follows seguidos deram 404 nesta conta "
                                         f"(a leitura funciona, a escrita não) — provável "
                                         f"bloqueio de ação nela.")
                        meta["conta_404"] = True
                        meta["aborted"] = True
                        if log:
                            log(f"✖ turno encerrado: {meta['error']}")
                            log(f"   ↩ as {len(ultimos_404)} voltaram para a fila (nada perdido).")
                        break
                    if await spider._sleep_stop(verdict["wait"], should_stop):
                        break
                    continue
                seguidas_404 = 0
                ultimos_404.clear()

                acct["attempts"] = acct.get("attempts", 0) + 1
                consecutive += 1
                if acct["attempts"] >= int(s["maxAttemptsPerAccount"]):
                    _finish(acct, "failed", verdict["label"], "failed")
                else:
                    acct["note"] = f"{verdict['label']} (tentativa {acct['attempts']})"

                if log:
                    log(f"⚠ @{acct['username']}: {verdict['label']}. "
                        f"Pausa de {human_duration(verdict['wait'])}.")

                if consecutive >= int(s["maxConsecutiveFailures"]):
                    meta["error"] = f"{consecutive} falhas seguidas"
                    meta["aborted"] = True
                    if log:
                        log(f"✖ abortando após {consecutive} falhas seguidas.")
                    break

                if await spider._sleep_stop(verdict["wait"], should_stop):
                    break
                continue

            # ── sucesso ──
            consecutive = 0
            seguidas_404 = 0
            ultimos_404.clear()
            requested = res.get("requested") is True
            acct["followedByViewer"] = res.get("following") is True
            acct["requestedByViewer"] = requested
            _finish(acct, "requested" if requested else "followed",
                    "solicitação enviada" if requested else "",
                    "requested" if requested else "followed")

            ts = acct["actedAt"]
            history[acct["username"]] = ts
            actions.append(ts)
            counts = limiter_counts(actions)
            if on_counts:
                on_counts(counts)

            feitos = stats["followed"] + stats["requested"]
            if cota and feitos >= cota:
                meta["turno_cheio"] = True
                if log:
                    log(f"🔁 {feitos} follow(s) nesta conta — turno completo, passando a vez")
                break

            wait = jitter(s["baseDelayMs"], s["jitterPercent"])
            if log:
                log(f"✓ @{acct['username']} — {'solicitada' if requested else 'seguida'}"
                    f" · {feitos}{('/' + str(cota)) if cota else ''} no turno"
                    f" · {counts['hour']}/h {counts['day']}/d · próximo em {human_duration(wait)}")
            if await spider._sleep_stop(wait, should_stop):
                break

    try:
        await sm.run_profile_task(profile, _run, headless=True)
    except Exception as e:
        meta["error"] = meta["error"] or f"erro ao abrir o perfil: {e}"
        if log:
            log(f"✖ {meta['error']}")

    return {"stats": stats, **meta}
