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
from urllib.parse import quote

import comportamento as comp
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
    # Sem teto por hora nem por dia. O teto/hora era redundante com o intervalo,
    # e o teto/dia travaria a conta por 24h -- o oposto do que se quer: o DESCANSO
    # de cada conta e o tempo em que as OUTRAS trabalham (ver `followsPerAccount`).
    # Travar por dia impediria a conta de voltar quando o rodizio fechasse a volta.
    # O limitador continua no codigo: por um numero aqui e a trava volta.
    "maxPerHour": 0,
    "maxPerDay": 0,
    # Respiro depois que TODAS as contas passaram a vez. O descanso normal de
    # cada conta e o tempo das outras; isto e um extra no fim da volta, para a
    # frota inteira nao ficar em atividade ininterrupta. 0 = sem extra.
    "restAfterRoundMin": 60,
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
    # ── Comportamento humano (ver comportamento.py) ──────────────────────────
    # Medido no nosso banco em 2026-08-21: 6.894 ações e UM verbo só (follow),
    # intervalos 99,9% dentro de 7-30s, e o corpo do POST declarando
    # `container_module=profile` para uma tela que a sessão nunca carregava.
    # Estes ajustes existem para quebrar esse padrão. Os valores efetivos moram
    # em comportamento.PADROES; aqui ficam só as chaves que a UI edita.
    **comp.PADROES,
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
    "conservador": {"baseDelayMs": 180000, "maxPerHour": 0, "maxPerDay": 0},
    "equilibrado": {"baseDelayMs": 90000, "maxPerHour": 0, "maxPerDay": 0},
    "rapido": {"baseDelayMs": 60000, "maxPerHour": 0, "maxPerDay": 0},
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
async ([url, referer, body, extra]) => {
  const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  const csrf = m ? m[1] : '';
  // Base + os headers DINAMICOS colhidos do trafego da propria interface
  // (x-ig-www-claim, x-instagram-ajax, x-web-session-id, x-asbd-id atual). Eles
  // rotacionam e sao emitidos pelo IG; sem eles o POST de escrita dava 404.
  const h = {
    'accept': '*/*',
    'x-csrftoken': csrf,
    'x-ig-app-id': '936619743392459',
    'x-asbd-id': '129477',
    'x-requested-with': 'XMLHttpRequest',
    'content-type': 'application/x-www-form-urlencoded',
  };
  try {
    if (extra) {
      Object.keys(extra).forEach((k) => {
        if (extra[k]) h[String(k).toLowerCase()] = String(extra[k]);
      });
    }
  } catch (e) {}
  try {
    const r = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: h,
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


async def _follow_one(page, user_id, username, extra=None, tokens=None) -> dict:
    """Segue uma conta. Tenta a API v1 e cai para /web/ se a resposta vier inválida.
    Retorna {ok, following, requested, status, payload}."""
    referer = f"{IG}/{username}/"
    # Corpo e headers MEDIDOS da interface real (captura de 2026-08-18, usuário seguindo
    # na mão): container_module + include_follow_friction_check + user_id + jazoest +
    # fb_dtsg. O `fb_dtsg` é o token anti-CSRF da Meta e é o que destrava a escrita — sem
    # ele o Instagram devolve 404 (não 403), o que mandou o diagnóstico para o lado errado.
    # Histórico que importa: este follow FUNCIONOU em 2026-08-16 (12 contas seguidas, no
    # banco) com body simples, e parou quando o IG passou a exigir o token. Foi regressão
    # de verdade — do lado deles, não do nosso código.
    res = await page.evaluate(_POST_JS, [f"{IG}/api/v1/friendships/create/{user_id}/",
                                         referer, corpo_follow(user_id, tokens),
                                         extra or {}])
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

    fb = await page.evaluate(_POST_JS, [f"{IG}/web/friendships/{user_id}/follow/",
                                        referer, "", extra or {}])
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


async def _unfollow_one(page, user_id, username, extra=None, tokens=None) -> dict:
    """Desfaz o follow. Espelha _follow_one: v1 `friendships/destroy/` e fallback
    `/web/friendships/<id>/unfollow/`. Sucesso = status ok e `following` FALSO."""
    referer = f"{IG}/{username}/"
    # Mesmo tratamento do follow: body vazio (a forma comprovada) e os headers
    # dinamicos colhidos da interface.
    res = await page.evaluate(_POST_JS, [f"{IG}/api/v1/friendships/destroy/{user_id}/",
                                         referer, corpo_follow(user_id, tokens),
                                         extra or {}])
    j = res.get("json") or {}
    fs = j.get("friendship_status") or {}
    if res.get("ok") and j.get("status") == "ok" and fs.get("following") is not True:
        return {"ok": True, "status": res.get("status"), "payload": j}

    st = res.get("status")
    if isinstance(st, int) and 400 <= st < 500:
        return {"ok": False, "status": st, "payload": j}

    fb = await page.evaluate(_POST_JS, [f"{IG}/web/friendships/{user_id}/unfollow/",
                                        referer, "", extra or {}])
    fj = fb.get("json") or {}
    if fb.get("ok") and fj.get("status") == "ok":
        return {"ok": True, "status": fb.get("status"), "payload": fj}

    return {"ok": False, "status": st if st not in (None, 200) else fb.get("status"),
            "payload": j or fj}


# Estado de login lido da PRÓPRIA PÁGINA — zero requisição HTTP e nenhuma rota para
# adivinhar. Duas evidências que o Instagram publica sobre si mesmo:
#   1) a classe do <html>: a shell logada NÃO tem `not-logged-in` (a deslogada tem);
#   2) o cookie `ds_user_id`, escrito no login e legível pelo JS (não é httpOnly).
# Só NOMES/ids de cookie são lidos — o valor do sessionid nunca é exposto (e ele é
# httpOnly, invisível aqui de qualquer forma).
_CTX_JS = r"""
() => {
  const nomes = document.cookie.split(';')
      .map(s => s.split('=')[0].trim()).filter(Boolean);
  const m = document.cookie.match(/(?:^|;\s*)ds_user_id=([^;]+)/);
  const cls = String(document.documentElement.className || '');
  return {
    href: location.href,
    origem: location.origin,
    classe_html: cls.slice(0, 120),
    marcado_deslogado: /not-logged-in/.test(cls),
    csrftoken: nomes.includes('csrftoken'),
    ds_user_id: m ? m[1] : null,
    cookies: nomes.length,
  };
}
"""


async def _session_ok(page, user_id=None) -> dict:
    """O perfil está logado? Lê o estado da PÁGINA, sem gastar requisição.

    Antes isto usava o GraphQL `query_hash` de seguidores (first:1). Esse é o GraphQL
    PÚBLICO legado e responde para perfil público mesmo DESLOGADO — então dava
    "✓ sessão confirmada" num perfil sem sessão: **falso positivo** que sustentou por
    dias o diagnóstico errado de "a leitura funciona, só a escrita dá 404".

    Deliberadamente NÃO usa um endpoint de identidade (ex.: `/api/v1/accounts/
    current_user/`): não há certeza de que essa rota exista no `www`, e um 404 de rota
    inexistente seria indistinguível de deslogado — o mesmo erro, de novo. A classe do
    <html> e o cookie `ds_user_id` são afirmações do próprio Instagram e custam zero.

    Retorna {ok, quem, ctx, motivo} — `quem` é o ds_user_id (pk) da conta logada."""
    try:
        ctx = await page.evaluate(_CTX_JS) or {}
    except Exception as e:
        return {"ok": False, "status": -1, "error": str(e), "quem": None,
                "ctx": {}, "motivo": None}
    quem = ctx.get("ds_user_id")
    if ctx.get("marcado_deslogado"):
        return {"ok": False, "status": 200, "quem": quem, "ctx": ctx,
                "motivo": "a página do Instagram se declara deslogada (not-logged-in)"}
    if not quem:
        return {"ok": False, "status": 200, "quem": None, "ctx": ctx,
                "motivo": "não há cookie ds_user_id (nenhuma conta nesta sessão)"}
    return {"ok": True, "status": 200, "quem": quem, "ctx": ctx, "motivo": None}


# Espião de requisições: embrulha fetch e XHR e guarda no localStorage o que a PRÓPRIA
# interface do Instagram manda ao seguir alguém. Serve para parar de adivinhar o formato
# da requisição de escrita — a UI consegue seguir, então ela é a fonte da verdade.
#
# Filtra com cuidado para não afogar no tráfego normal: POST em friendships/follow, OU
# POST no /graphql/query cujo corpo mencione follow (se o follow virou mutação GraphQL,
# a URL não tem "friendship" — é por isso que este segundo caso existe).
_SPY_BODY = r"""
  if (window.__ifSpy) return true;
  window.__ifSpy = true;

  const guardar = (chave, reg, limite) => {
    try {
      if (limite) {
        const atual = JSON.parse(localStorage.getItem(chave) || '[]');
        atual.push(reg);
        localStorage.setItem(chave, JSON.stringify(atual.slice(-limite)));
      } else {
        localStorage.setItem(chave, JSON.stringify(reg));
      }
    } catch (e) {}
  };

  const cabecalhos = (h) => {
    const out = {};
    try {
      if (!h) return out;
      if (typeof h.forEach === 'function') h.forEach((v, k) => { out[String(k).toLowerCase()] = String(v); });
      else Object.keys(h).forEach((k) => { out[String(k).toLowerCase()] = String(h[k]); });
    } catch (e) {}
    return out;
  };

  // ESCRITA de follow -- e so isto que encerra a captura. Antes o filtro pegava
  // qualquer URL com "friendship", e o `friendships/show_many` disparado ao abrir a
  // lista de seguidores fechava o navegador antes de o usuario clicar em Seguir.
  const ehEscritaFollow = (u, m, corpo) => {
    if (String(m || 'GET').toUpperCase() !== 'POST') return false;
    const url = String(u || '');
    const b = String(corpo || '');
    if (/\/friendships\/(create|destroy)\//i.test(url)) return true;
    if (/\/(un)?follow\/?(\?|$)/i.test(url)) return true;
    // GraphQL: SO mutacao. "followers"/"following" sao QUERIES de lista.
    if (/graphql/i.test(url) && /mutation/i.test(b) && /follow/i.test(b)) return true;
    return false;
  };

  // Headers dinamicos do IG (rotacionam; nao se inventam). Guarda os mais recentes
  // de QUALQUER requisicao da interface que os carregue, para o nosso POST reusar.
  const INTERESSANTES = ['x-ig-www-claim', 'x-instagram-ajax', 'x-web-session-id',
                         'x-asbd-id', 'x-ig-max-touch-points', 'x-ig-app-id'];
  // Tokens anti-CSRF que a interface manda no CORPO (medido: sem eles o POST de
  // escrita era recusado). O corpo do trafego real e a fonte mais fresca -- o
  // bootstrap da pagina e do momento do carregamento e pode ter envelhecido.
  const doCorpo = (corpo) => {
    const out = {};
    try {
      const b = String(corpo || '');
      const d = b.match(/(?:^|&)fb_dtsg=([^&]+)/);
      if (d) out.fb_dtsg = decodeURIComponent(d[1]);
      const j = b.match(/(?:^|&)jazoest=([^&]+)/);
      if (j) out.jazoest = decodeURIComponent(j[1]);
    } catch (e) {}
    return out;
  };

  const colher = (hs, corpo) => {
    try {
      const doB = doCorpo(corpo);
      const temHeader = hs && (hs['x-ig-www-claim'] || hs['x-instagram-ajax']);
      if (!temHeader && !doB.fb_dtsg && !doB.jazoest) return;
      const guardados = JSON.parse(localStorage.getItem('__if_hdrs') || '{}');
      if (hs) INTERESSANTES.forEach((k) => { if (hs[k]) guardados[k] = hs[k]; });
      if (doB.fb_dtsg) guardados.fb_dtsg = doB.fb_dtsg;
      if (doB.jazoest) guardados.jazoest = doB.jazoest;
      guardar('__if_hdrs', guardados, 0);
    } catch (e) {}
  };

  const registrar = (via, m, url, hs, corpo) => {
    colher(hs, corpo);
    if (ehEscritaFollow(url, m, corpo)) {
      guardar('__if_cap', { via: via, method: String(m).toUpperCase(), url: String(url),
                            headers: hs, body: String(corpo || '').slice(0, 600) }, 20);
    }
  };

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const m = (init && init.method) || (input && input.method) || 'GET';
      let corpo = '';
      const b = init && init.body;
      if (typeof b === 'string') corpo = b;
      else if (b && typeof b.toString === 'function') corpo = String(b);
      registrar('fetch', m, url,
                cabecalhos((init && init.headers) || (input && input.headers)), corpo);
    } catch (e) {}
    return origFetch.apply(this, arguments);
  };

  const xOpen = XMLHttpRequest.prototype.open;
  const xSend = XMLHttpRequest.prototype.send;
  const xHead = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__m = m; this.__u = u; this.__h = {};
    return xOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try { (this.__h = this.__h || {})[String(k).toLowerCase()] = String(v); } catch (e) {}
    return xHead.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (b) {
    try {
      registrar('xhr', this.__m, this.__u, this.__h || {},
                typeof b === 'string' ? b : '');
    } catch (e) {}
    return xSend.apply(this, arguments);
  };
  return true;
"""

_SPY_INIT = "(() => {" + _SPY_BODY + "})()"
_SPY_EVAL = "() => {" + _SPY_BODY + "}"
_LER_CAP = ("() => { try { const v = localStorage.getItem('__if_cap');"
            " return v ? JSON.parse(v) : []; } catch (e) { return []; } }")
_LIMPAR_CAP = "() => { try { localStorage.removeItem('__if_cap'); } catch (e) {} }"
_LER_HDRS = ("() => { try { const v = localStorage.getItem('__if_hdrs');"
             " return v ? JSON.parse(v) : {}; } catch (e) { return {}; } }")


async def capture_follow(profile, log=None, should_stop=None, timeout=300) -> dict:
    """Abre o perfil VISÍVEL e captura a requisição de follow que a interface manda.

    Por que existe: o POST que montamos (`/api/v1/friendships/create/`) devolve 404 mesmo
    com o perfil logado, a família v1 respondendo 200 para leitura e os headers idênticos
    à chamada que funcionou em 2026-06-21. Sucessivas hipóteses (body, id, sessão, rota)
    morreram no teste. A interface web CONSEGUE seguir — então ela sabe o formato certo,
    e medir é mais barato que continuar adivinhando header por header.

    O usuário segue alguém na mão; o espião registra método/URL/headers/corpo."""
    achado = {"ok": False, "capturas": [], "erro": None}

    async def _job(page):
        # Reinstala em CADA documento: o usuário vai navegar até o perfil do alvo, e um
        # wrapper preso ao documento atual morreria nessa navegação.
        try:
            await page.add_init_script(_SPY_INIT)
        except Exception:
            pass
        await page.goto(IG + "/", wait_until="domcontentloaded")
        try:
            await page.evaluate(_LIMPAR_CAP)
            await page.evaluate(_SPY_EVAL)
        except Exception as e:
            achado["erro"] = f"não consegui instalar o espião: {e}"
            return
        if log:
            log("🔬 espião instalado. AGORA, nesta janela do navegador:")
            log("   1) vá até o perfil de alguém que você NÃO segue")
            log("   2) clique em Seguir (de verdade — vai seguir mesmo)")
            log(f"   esperando até {timeout}s… (não feche a janela)")
        esperou = 0
        while esperou < timeout:
            if should_stop and should_stop():
                achado["erro"] = "cancelado"
                return
            try:
                caps = await page.evaluate(_LER_CAP)
            except Exception:
                caps = []
            if caps:
                achado["capturas"] = caps
                achado["ok"] = True
                # Os headers dinâmicos são o achado mais reaproveitável: o motor passou a
                # colhê-los sozinho, e aqui eles ficam visíveis para conferência.
                try:
                    achado["headers_ui"] = await page.evaluate(_LER_HDRS) or {}
                except Exception:
                    achado["headers_ui"] = {}
                if log:
                    log(f"✓ capturei {len(caps)} requisição(ões) de follow da interface:")
                    for c in caps[:3]:
                        log(f"   ▸ {c.get('method')} {c.get('url')}")
                        log(f"     corpo: {(c.get('body') or '(vazio)')[:200]}")
                        for k, v in sorted((c.get("headers") or {}).items()):
                            log(f"     {k}: {v[:90]}")
                return
            if await spider._sleep_stop(3, should_stop):
                achado["erro"] = "cancelado"
                return
            esperou += 3
        achado["erro"] = "tempo esgotado — nenhum follow foi clicado na janela"
        if log:
            log(f"✖ {achado['erro']}")

    try:
        if log:
            log("\U0001f310 abrindo a janela do perfil — "
                "pode levar alguns segundos…")
        await sm.run_profile_task(profile, _job, headless=False)
    except Exception as e:
        achado["erro"] = str(e)
    return achado


# Tokens anti-CSRF da Meta que a interface manda no CORPO do follow (medido). Sao
# por sessao e rotacionam. O `jazoest` NAO deriva do `fb_dtsg` pelo algoritmo classico
# ("2" + soma dos charCodes da string) -- conferido contra a captura real: daria 26330,
# o valor real era 22845. Portanto: colher os dois, nunca calcular.
_TOKENS_JS = r"""
() => {
  const out = { fb_dtsg: '', jazoest: '' };
  try {
    if (typeof require === 'function') {
      const d = require('DTSGInitialData');
      if (d && d.token) out.fb_dtsg = String(d.token);
    }
  } catch (e) {}
  try {
    const i = document.querySelector('input[name="fb_dtsg"]');
    if (i && i.value && !out.fb_dtsg) out.fb_dtsg = String(i.value);
    const j = document.querySelector('input[name="jazoest"]');
    if (j && j.value) out.jazoest = String(j.value);
  } catch (e) {}
  try {
    const html = document.documentElement.innerHTML;
    if (!out.fb_dtsg) {
      const m = html.match(/"dtsg":\s*\{\s*"token":\s*"([^"]+)"/)
             || html.match(/"token":"(NA[A-Za-z0-9_\-:]{20,})"/);
      if (m) out.fb_dtsg = m[1];
    }
    if (!out.jazoest) {
      const m2 = html.match(/jazoest["'=:\s]+(\d{4,})/);
      if (m2) out.jazoest = m2[1];
    }
  } catch (e) {}
  return out;
}
"""


async def colher_tokens_ui(page, log=None) -> dict:
    """Colhe `fb_dtsg` e `jazoest` — os tokens anti-CSRF que a interface manda no CORPO
    do follow. Duas fontes, a mais fresca vence: o que o espão viu no tráfego real
    (`__if_hdrs`) e, como reserva, o bootstrap da própria página."""
    achado = {"fb_dtsg": "", "jazoest": ""}
    try:
        do_trafego = await page.evaluate(_LER_HDRS) or {}
    except Exception:
        do_trafego = {}
    for k in ("fb_dtsg", "jazoest"):
        if do_trafego.get(k):
            achado[k] = str(do_trafego[k])
    if not achado["fb_dtsg"] or not achado["jazoest"]:
        try:
            da_pagina = await page.evaluate(_TOKENS_JS) or {}
        except Exception:
            da_pagina = {}
        for k in ("fb_dtsg", "jazoest"):
            if not achado[k] and da_pagina.get(k):
                achado[k] = str(da_pagina[k])
    if log:
        if achado["fb_dtsg"]:
            log(f"🔑 tokens da interface: fb_dtsg={achado['fb_dtsg'][:18]}… "
                f"jazoest={achado['jazoest'] or '(não achei)'}")
        else:
            log("⚠ não achei o fb_dtsg da página — a interface manda esse token no corpo do "
                "follow; sem ele o POST pode ser recusado (foi a causa medida do 404)")
    return achado


def corpo_follow(user_id, tokens=None) -> str:
    """Monta o corpo do follow como a INTERFACE manda (medido ao vivo em 2026-08-18).

    `nav_chain` fica de fora de propósito: é telemetria da navegação (de qual tela o
    clique veio), não credencial — e inventar uma trilha falsa seria pior que omitir."""
    partes = ["container_module=profile", "include_follow_friction_check=true",
              f"user_id={quote(str(user_id))}"]
    t = tokens or {}
    if t.get("jazoest"):
        partes.append(f"jazoest={quote(str(t['jazoest']))}")
    if t.get("fb_dtsg"):
        partes.append(f"fb_dtsg={quote(str(t['fb_dtsg']), safe='')}")
    return "&".join(partes)


async def colher_headers_dinamicos(page, log=None) -> dict:
    """Colhe do tráfego da PRÓPRIA interface os headers que o IG exige e que rotacionam.

    Medido ao vivo (POST `/api/v1/friendships/show_many/` que a UI dispara ao abrir a
    lista de seguidores): a interface manda `x-ig-www-claim`, `x-instagram-ajax`,
    `x-web-session-id`, `x-ig-max-touch-points` e `x-asbd-id: 359341` — **nenhum deles
    ia no nosso POST**, e o nosso `x-asbd-id` constante (129477) estava velho. Esses
    valores são emitidos pelo Instagram e mudam; por isso são COLHIDOS em vez de
    escritos no código, o que mantém o follow funcionando quando o IG rotacionar.

    Requer o espão instalado (`_SPY_INIT` via add_init_script) antes da navegação."""
    try:
        hs = await page.evaluate(_LER_HDRS) or {}
    except Exception:
        hs = {}
    if log:
        if hs.get("x-ig-www-claim") or hs.get("x-instagram-ajax"):
            log("🧬 headers da interface colhidos: "
                + " · ".join(f"{k}={str(v)[:22]}" for k, v in sorted(hs.items())))
        else:
            log("⚠ não colhi os headers dinâmicos da interface (x-ig-www-claim etc.) — "
                "seguindo com os headers básicos; se der 404, é provavelmente isso")
    return hs


# Paginacao da PROPRIA lista de seguidores: 50 por requisicao, igual ao Spider.
_MINHA_LISTA_PAGINA = 50
# Teto de paginas por execucao. 200 paginas = 10.000 seguidores; alem disso a
# checagem viraria uma extracao longa, que e outro tipo de risco.
_MINHA_LISTA_MAX_PAGINAS = 200


async def _meus_seguidores(page, meu_id, *, log=None, should_stop=None) -> dict:
    """Usernames de quem segue a CONTA LOGADA (ela e dona da propria lista).

    Por que assim e nao por `friendships/show_many`: medido em producao, o
    show_many devolveu 1500 status com ZERO `followed_by` num perfil de 60
    seguidores. Ele e o endpoint que a interface usa para pintar o botao
    Seguir/Seguindo -- responde `following` (eu sigo?), nao `followed_by` (me
    segue?). A propria lista responde a pergunta certa, e mais barato: 60
    seguidores = 2 requisicoes, contra uma consulta por pessoa seguida."""
    achados, cursor, pagina = set(), None, 0
    while pagina < _MINHA_LISTA_MAX_PAGINAS:
        if should_stop and should_stop():
            return {"ok": False, "erro": "cancelado", "usernames": achados}
        variaveis = {"id": str(meu_id), "first": _MINHA_LISTA_PAGINA}
        if cursor:
            variaveis["after"] = cursor
        res = await spider._graphql(page, spider._GQL["followers"], variaveis, f"{IG}/")
        if not res.get("ok"):
            return {"ok": False, "usernames": achados,
                    "erro": f"o Instagram recusou a lista (status {res.get('status')})"}
        edge = (((res.get("json") or {}).get("data") or {})
                .get("user") or {}).get("edge_followed_by") or {}
        for e in edge.get("edges") or []:
            u = str(((e or {}).get("node") or {}).get("username") or "").strip().lower()
            if u:
                achados.add(u)
        info = edge.get("page_info") or {}
        pagina += 1
        if log and pagina % 5 == 0:
            log(f"   lendo meus seguidores... {len(achados)}")
        if not info.get("has_next_page"):
            return {"ok": True, "usernames": achados, "paginas": pagina}
        cursor = info.get("end_cursor")
        if await spider._sleep_stop(round(random.uniform(2.0, 5.0), 1), should_stop):
            return {"ok": False, "erro": "cancelado", "usernames": achados}
    return {"ok": True, "usernames": achados, "paginas": pagina, "truncado": True}


async def checar_seguiram_de_volta(profile, candidatos: list, *, log=None,
                                   should_stop=None, use_local=True) -> dict:
    """Quais dos que ESTE perfil seguiu retribuiram.

    `candidatos` = [{username, user_id, by_account}]. Le a lista de seguidores da
    propria conta e cruza — exato, e sem custo por pessoa seguida.

    So LEITURA: nao segue, nao desfaz."""
    saida = {"resultados": {}, "verificados": 0, "retribuiram": 0, "erro": None}
    if not candidatos:
        return saida
    seguidos = {}
    for c in candidatos:
        u = str(c.get("username") or "").strip().lower()
        if u:
            seguidos[u] = True

    async def _job(page):
        try:
            await page.add_init_script(_SPY_INIT)
        except Exception:
            pass
        await page.goto(IG + "/", wait_until="domcontentloaded")
        await asyncio.sleep(round(random.uniform(1.0, 2.0), 1))
        sonda = await _session_ok(page)
        if not sonda["ok"] and sonda.get("motivo"):
            saida["erro"] = f"perfil nao esta logado ({sonda['motivo']})"
            if log:
                log(f"\u2716 {saida['erro']}")
            return
        # O ds_user_id do cookie E o id da propria conta — nao custa requisicao.
        meu_id = sonda.get("quem")
        if not meu_id:
            saida["erro"] = "nao consegui descobrir o id da conta logada (ds_user_id)"
            if log:
                log(f"\u2716 {saida['erro']}")
            return
        if log:
            log(f"\U0001f50e lendo a lista de seguidores da conta (id {meu_id}) "
                f"para cruzar com {len(seguidos)} pessoa(s) que ela seguiu")
        r = await _meus_seguidores(page, meu_id, log=log, should_stop=should_stop)
        meus = r.get("usernames") or set()
        if not r.get("ok") and not meus:
            saida["erro"] = r.get("erro") or "nao consegui ler a lista de seguidores"
            if log:
                log(f"\u26a0 {saida['erro']}")
            return
        if r.get("truncado") and log:
            log("\u26a0 lista muito longa: parei no teto de paginas — o resultado cobre "
                "so a parte lida")
        for u in seguidos:
            retribuiu = u in meus
            saida["resultados"][u] = retribuiu
            saida["verificados"] += 1
            if retribuiu:
                saida["retribuiram"] += 1
        if log:
            log(f"\u2713 {len(meus)} seguidor(es) na conta \u00b7 "
                f"{saida['retribuiram']} de {saida['verificados']} seguidos retribuiram")
        if not r.get("ok"):
            saida["erro"] = r.get("erro")

    try:
        if log:
            log("\U0001f310 abrindo o navegador do perfil (headless) — "
                "pode levar alguns segundos…")
        await sm.run_profile_task(profile, _job, headless=True)
    except Exception as e:
        saida["erro"] = str(e)
    return saida


async def _diag_rotas(page, alvo_id=None) -> dict:
    """A família de rotas `/api/v1/` está VIVA nesta superfície?

    Os dados de 2026-08-18 mostram um padrão: tudo em `/api/v1/*` e `/web/*` falha
    (follow, `web_profile_info`, `topsearch`), enquanto `/graphql/query` funciona (a
    coleta trouxe 1000 contas com ele). Se uma LEITURA v1 pública também der 404, o
    problema não é a nossa requisição de follow nem a conta — é a rota v1 não existir
    mais nesta superfície, e o follow precisa migrar para o que a interface usa hoje.

    Duas requisições GET, uma vez por execução, nada de escrita."""
    out = {}
    try:
        r = await spider._api_get(
            page, f"{IG}/api/v1/users/web_profile_info/?username=instagram", f"{IG}/")
        out["v1_status"] = r.get("status")
        out["v1_body"] = (r.get("text") or "")[:120]
        out["v1_tem_user"] = bool(((r.get("json") or {}).get("data") or {}).get("user"))
    except Exception as e:
        out["v1_status"], out["v1_erro"] = -1, str(e)
    try:
        g = await spider._graphql(page, spider._GQL["followers"],
                                  {"id": str(alvo_id or "25025320"), "first": 1}, f"{IG}/")
        out["gql_status"] = g.get("status")
        out["gql_tem_user"] = bool(((g.get("json") or {}).get("data") or {}).get("user"))
    except Exception as e:
        out["gql_status"], out["gql_erro"] = -1, str(e)

    # Cuidado com o status -1: ele significa que o fetch ESTOUROU (rede), não que a rota
    # respondeu. Tratá-lo como "v1 viva" faria o log afirmar o contrário do que houve.
    v1_ok = out.get("v1_status") == 200 and out.get("v1_tem_user")
    v1_rejeitada = isinstance(out.get("v1_status"), int) and out["v1_status"] >= 400
    gql_viva = out.get("gql_status") == 200 and out.get("gql_tem_user")
    if v1_rejeitada and gql_viva:
        out["conclusao"] = ("as rotas /api/v1/ NÃO respondem nesta superfície (o GraphQL "
                            "responde) — não é a conta nem o corpo do POST: é a rota. O follow "
                            "precisa usar o que a interface web usa hoje.")
    elif v1_ok:
        out["conclusao"] = ("/api/v1/ responde para LEITURA — então o 404 é específico da rota "
                            "de ESCRITA (friendships/create), não da família v1 inteira.")
    else:
        out["conclusao"] = ("inconclusivo — alguma das duas leituras não chegou a responder "
                            "(rede/IP no momento do teste). Não dá para culpar a rota.")
    return out


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
        # Espiao ANTES de navegar: colhe do trafego da propria interface os headers
        # dinamicos que o IG exige na escrita (x-ig-www-claim, x-instagram-ajax...).
        try:
            await page.add_init_script(_SPY_INIT)
        except Exception:
            pass
        await page.goto(IG + "/", wait_until="domcontentloaded")
        await asyncio.sleep(round(random.uniform(1.0, 2.5), 1))
        hdrs_ui = await colher_headers_dinamicos(page, log=log)
        tokens_ui = await colher_tokens_ui(page, log=log)

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
                res = await _unfollow_one(page, uid, uname, extra=hdrs_ui, tokens=tokens_ui)
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
        if log:
            log("\U0001f310 abrindo o navegador do perfil (headless) — "
                "pode levar alguns segundos…")
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
        if log:
            log("\U0001f310 abrindo o navegador do perfil (headless) — "
                "pode levar alguns segundos…")
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
              yield_on_cap: bool = False, chave_conta: str = "") -> dict:
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
    # Ruído humano em volta do follow: leitura do perfil antes de escrever nele,
    # intervalo com cauda, curtida ocasional, janela de sono. A chave da CONTA (não
    # o nome do perfil) é o que dá a cada conta um horário de dormir próprio — sem
    # isso a frota inteira dormiria no mesmo minuto, trocando uma assinatura por outra.
    humano = comp.Comportamento(s, chave_conta=chave_conta or profile.get("name") or "",
                                log=log)

    def _perfil_aleatorio_da_fila():
        """Um nome da fila para a conta ESPIAR sem seguir.

        Medido em 2026-08-21: a conta seguia 100% dos perfis que abria — conversão
        perfeita é assinatura, gente real abre muito e segue pouco. Sai da própria
        fila porque é o que uma pessoa faria: percorre uma lista, abre vários, segue
        alguns. Quem for espiado continua na fila e será seguido depois, na vez dele."""
        pend = [a.get("username") for a in queue
                if a.get("status") == "pending" and a.get("username")]
        return random.choice(pend) if pend else None

    humano.fonte_de_perfis = _perfil_aleatorio_da_fila

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
        # Espão ANTES de navegar: ele colhe, do tráfego que a própria interface gera ao
        # carregar, os headers dinâmicos que o IG exige na escrita (medido: sem eles o
        # POST de follow devolvia 404). Reinstala em cada documento.
        try:
            await page.add_init_script(_SPY_INIT)
        except Exception:
            pass
        # Medidor de dados ANTES de navegar: o plano e de 30 GB/mes e o modem e a
        # internet do PC INTEIRO, entao quanto cada turno consome virou restricao
        # de projeto, nao curiosidade. Ligado aqui para contar a sessao inteira.
        humano.dados.ligar(page)
        await page.goto(IG + "/", wait_until="domcontentloaded")
        await asyncio.sleep(round(random.uniform(1.0, 2.5), 1))
        hdrs_ui = await colher_headers_dinamicos(page, log=log)
        tokens_ui = await colher_tokens_ui(page, log=log)

        # ── sonda de sessão (1 requisição, antes de agir) ──
        # Sem isto, perfil deslogado só era descoberto queimando itens da fila.
        if not dry:
            sonda = await _session_ok(page)
            ctx = sonda.get("ctx") or {}
            # Aborta em falha CLARA de autenticação: 401/403, OU 200/404 sem identidade
            # (deslogado), OU sem o cookie ds_user_id — que é o que marca "há uma conta
            # nesta sessão". Um piscar de rede (-1/5xx) segue sendo inconclusivo e NÃO
            # derruba a execução; para isso já existe o tratamento de erro do laço.
            # Aborta só com evidência DEFINITIVA de deslogado (o Instagram se declarando
            # deslogado, ou ausência do cookie da conta). Se a leitura do contexto falhar
            # (status -1), fica inconclusivo e NÃO derruba a execução — o tratamento de
            # erro do laço cobre o resto.
            if not sonda["ok"] and sonda.get("motivo"):
                meta["error"] = (f"este perfil NÃO está logado no Instagram "
                                 f"({sonda['motivo']}) — o 404 no follow é consequência "
                                 f"disso, não bloqueio de ação da conta. Abra o perfil e "
                                 f"faça login, ou importe a sessão em Contas → Detalhes.")
                meta["aborted"] = True
                meta["blocked"] = True
                if log:
                    log(f"✖ {meta['error']}")
                    log(f"   ↳ contexto: origem={ctx.get('origem')} · "
                        f"csrftoken={'sim' if ctx.get('csrftoken') else 'NÃO'} · "
                        f"ds_user_id={ctx.get('ds_user_id') or 'AUSENTE'} · "
                        f"{ctx.get('cookies')} cookie(s) visíveis ao JS · "
                        f"classe do html={ctx.get('classe_html')!r}")
                    log("   (nenhum item da fila foi tocado)")
                return
            if log:
                if sonda["ok"]:
                    log(f"✓ perfil logado (ds_user_id {sonda.get('quem')}) · "
                        f"csrftoken={'sim' if ctx.get('csrftoken') else 'NÃO — o POST sai sem CSRF!'}"
                        f" · {ctx.get('cookies')} cookie(s)")
                else:
                    log(f"⚠ sonda de sessão inconclusiva (status {sonda.get('status')}) — "
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

            # ── janela de sono da conta ──
            # Medido: a frota seguia às 02h no mesmo ritmo das 15h, 35h seguidas.
            # Vem DESLIGADA (custa ~35% do volume); ligada, cada conta tem o seu
            # horário — o desvio é derivado do nome, então a frota não dorme junto.
            falta_dormir = 0.0 if dry else humano.dormindo()
            if falta_dormir > 0:
                if yield_on_cap:
                    meta["cap_atingido"] = True
                    if log:
                        log(f"🌙 esta conta está no horário de dormir — passando a vez "
                            f"(acorda em {human_duration(falta_dormir)})")
                    break
                if log:
                    log(f"🌙 horário de dormir — retomando em {human_duration(falta_dormir)}")
                if await spider._sleep_stop(falta_dormir, should_stop):
                    break
                continue

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

            # ── contexto: LER o perfil antes de escrever nele ──
            # O nosso POST declara `container_module=profile` e um referer da página
            # do perfil — uma tela que a sessão nunca carregava. A Meta tem os dois
            # lados (o que a sessão leu e o que ela escreveu); esta leitura faz a
            # declaração virar verdade. `ja_leu` evita pagar a requisição duas vezes
            # quando o enriquecimento dos filtros já leu esse perfil.
            ctx_perfil = await humano.antes_de_seguir(
                page, acct["username"], ja_leu=bool(acct.get("enriched")))
            if ctx_perfil.get("ok") and ctx_perfil.get("tipo") == "completo":
                # Documento novo = tokens novos. O `fb_dtsg` é por documento, e foi
                # justamente a falta dele que devolvia 404 no follow.
                novos_h = await colher_headers_dinamicos(page, log=None)
                if novos_h:
                    hdrs_ui = novos_h
                novos_t = await colher_tokens_ui(page, log=None)
                if novos_t.get("fb_dtsg"):
                    tokens_ui = novos_t

            # ── follow ──
            try:
                res = await _follow_one(page, acct["id"], acct["username"], extra=hdrs_ui,
                                        tokens=tokens_ui)
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
                except Exception as e:
                    pk, erro_pk = None, str(e)
                else:
                    erro_pk = None
                # Sem esta linha o log ficava MUDO quando o resolve falhava (foi o que
                # aconteceu em 2026-08-18: nenhuma das duas linhas de id apareceu, e a
                # ausência era a pista — resolver perfil também estava falhando, ou seja
                # o problema não era só a escrita).
                if not pk and log:
                    log(f"   ↳ não consegui resolver o pk de @{acct['username']}"
                        + (f" ({erro_pk})" if erro_pk else
                           " (web_profile_info + topsearch + página todos falharam)")
                        + " — isso indica que a LEITURA de perfil também não funciona"
                          " nesta sessão, não só a escrita")
                if pk and str(pk) != str(acct.get("id")):
                    if log:
                        log(f"   ↳ id da fila {acct.get('id')} ≠ id resolvido {pk} — "
                            f"tentando de novo com o id real")
                    acct["id"] = str(pk)
                    try:
                        res = await _follow_one(page, acct["id"], acct["username"], extra=hdrs_ui,
                                        tokens=tokens_ui)
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
                            # Duas LEITURAS (zero escrita) que separam "a rota v1 morreu"
                            # de "a nossa requisição de escrita está errada".
                            d = await _diag_rotas(page, acct.get("id"))
                            log(f"   🔬 leitura /api/v1/ (web_profile_info): "
                                f"status {d.get('v1_status')}"
                                f"{' com usuário' if d.get('v1_tem_user') else ' SEM usuário'}"
                                f" · GraphQL: status {d.get('gql_status')}"
                                f"{' com usuário' if d.get('gql_tem_user') else ' SEM usuário'}")
                            log(f"   🔬 conclusão: {d.get('conclusao')}")
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

            # Curtida ocasional: outro verbo no log de ações da conta, e é o gesto
            # natural de quem acabou de seguir alguém. Feita CLICANDO na interface —
            # quem monta a requisição é o Instagram, então não há rota a adivinhar.
            r_curtida = await humano.depois_de_seguir(page, ctx_perfil)

            feitos = stats["followed"] + stats["requested"]
            if cota and feitos >= cota:
                meta["turno_cheio"] = True
                if log:
                    log(f"🔁 {feitos} follow(s) nesta conta — turno completo, passando a vez")
                break

            wait, modo = humano.proximo_intervalo(s["baseDelayMs"])
            # O que PRECEDEU este follow vai inline: sem isso a leitura do perfil —
            # que acontece em todo follow e é o principal do disfarce — era invisível.
            antes_disso = comp.descrever_leitura_de_perfil(ctx_perfil)
            eh_pausa = str(modo).startswith("pausa")
            if log:
                log(f"✓ @{acct['username']} — {'solicitada' if requested else 'seguida'}"
                    + (f" · {antes_disso}" if antes_disso else "")
                    + f" · {feitos}{('/' + str(cota)) if cota else ''} no turno"
                    + f" · {counts['hour']}/h {counts['day']}/d"
                    + ("" if eh_pausa else f" · próximo em {human_duration(wait)}"))
            # A curtida acontece logo APÓS o follow, antes de o intervalo começar —
            # por isso tem linha própria e não é dobrada na linha da pausa, que
            # descreve um tempo posterior.
            if log and (r_curtida or {}).get("ok"):
                log("   ♥ curtiu um post do perfil")
            r_espera = await humano.esperar(page, wait, should_stop, modo=modo)
            # A pausa é narrada SEMPRE, inclusive quando não se fez nada nela. Ficar
            # parado é a maioria das pausas, e era justamente esse silêncio que fazia
            # o terminal parecer morto no tempo em que mais tempo passa.
            if log and r_espera.get("na_pausa"):
                log(comp.descrever_pausa(r_espera.get("segundos"),
                                         r_espera.get("acoes")))
            if r_espera.get("parado"):
                break

        # O que a conta fez ALÉM de seguir. Sem esta linha o ruído humano é
        # trabalho invisível — e trabalho invisível já foi confundido com trabalho
        # quebrado três vezes neste projeto.
        if log:
            extras = humano.resumo()
            if extras:
                log(f"🧑 além dos follows, esta conta: {extras}")
            # A sequência CRUA de rajadas, pausas e buscas. Média não revela
            # repetição; a sequência revela — e foi para poder "prevenir
            # comportamento repetitivo" que o usuário pediu esta visibilidade.
            for linha in humano.relatorio_de_variedade():
                log(linha)

    try:
        if log:
            log("\U0001f310 abrindo o navegador do perfil (headless) — "
                "pode levar alguns segundos…")
        await sm.run_profile_task(profile, _run, headless=True)
    except Exception as e:
        meta["error"] = meta["error"] or f"erro ao abrir o perfil: {e}"
        if log:
            log(f"✖ {meta['error']}")

    # Bytes MEDIDOS neste turno (piso -- ver MedidorDeDados). Quem acumula por
    # mes e a api, do mesmo jeito que ela acumula historico e acoes.
    return {"stats": stats, **meta,
            "dados_bytes": humano.dados.bytes,
            "dados_mb": humano.dados.mb,
            "dados_sem_tamanho": humano.dados.sem_tamanho}
