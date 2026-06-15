"""
warm_pool.py — pool de perfis pré-aquecidos prontos para criar contas.

Como o aquecimento demora (~15 min), aquecer na hora da criação trava cada conta.
Aqui um MANTENEDOR roda em background e garante sempre N perfis no estado 'ready'
(perfil criado + proxy do pool + navegador aquecido em headless, SEM conta ainda).

A criação de contas chama `claim()` para reservar um perfil pronto (ready -> in_use)
e cria a conta sem aquecer na hora. Se o pool estiver vazio, o chamador cai no modo
antigo (cria + aquece na hora) — decisão do usuário.

Estados (coluna profiles.pool_status):
  warming  → sendo aquecido agora
  ready    → pronto e livre para uso
  in_use   → reservado/already usado por uma criação
  (NULL)   → perfil normal, fora do pool
"""
import asyncio
import datetime
import logging

import db
import profiles as prof
import proxies as pool
import profile_warmup as pw
import session_manager as sm
import accounts as acc

logger = logging.getLogger(__name__)

MAX_CONCURRENT_WARM = 2      # quantos perfis aquecer ao mesmo tempo
_CYCLE_SLEEP = 30            # intervalo entre ciclos do mantenedor (s)
_BACKOFF_MAX = 300           # espera máxima quando há falhas seguidas (s)

_lock = asyncio.Lock()
_maintainer_task = None


# ── Config (settings, com defaults) ────────────────────────────────────────────

def _target_size() -> int:
    try:
        return max(0, int(db.get_setting("warm_pool_size", "5") or 5))
    except Exception:
        return 5


def _warm_minutes() -> float:
    try:
        return max(1.0, float(db.get_setting("warm_pool_minutes", "15") or 15))
    except Exception:
        return 15.0


def enabled() -> bool:
    return (db.get_setting("warm_pool_enabled", "1") or "1") not in ("0", "false", "False", "")


# ── Estado do pool ──────────────────────────────────────────────────────────────

def status() -> dict:
    ready = warming = in_use = 0
    for p in db.list_profiles():
        s = p.get("pool_status")
        if s == "ready" and not db.get_account_by_profile(p["id"]):
            ready += 1
        elif s == "warming":
            warming += 1
        elif s == "in_use":
            in_use += 1
    return {"target": _target_size(), "ready": ready, "warming": warming,
            "in_use": in_use, "enabled": enabled()}


# ── Reserva de perfil pronto ────────────────────────────────────────────────────

async def claim():
    """Reserva atomicamente um perfil pronto (ready -> in_use). Retorna o dict do
    perfil (com proxy) ou None se não houver nenhum pronto."""
    async with _lock:
        for p in db.list_profiles():
            if p.get("pool_status") == "ready" and not db.get_account_by_profile(p["id"]):
                db.update_profile(p["id"], pool_status="in_use")
                logger.info("warm_pool: perfil %s reservado (in_use)", p["id"])
                return db.get_profile(p["id"])
    return None


# ── Aquecimento de um novo perfil ────────────────────────────────────────────────

async def _warm_one() -> bool:
    """Cria 1 perfil com proxy do pool e aquece em headless. Marca 'ready' no fim.
    Em caso de falha, remove o perfil para não acumular lixo. Retorna True se ficou pronto."""
    limit = acc.get_limits().get("proxy_limit", 3)
    try:
        proxies = pool.assign(1, limit)
    except Exception as e:
        logger.warning("warm_pool: assign de proxy falhou: %s", e)
        return False
    proxy = proxies[0] if proxies else None
    if proxy is None:
        logger.info("warm_pool: sem proxy livre — não vou criar perfil agora")
        return False

    p = prof.create(name="pool", proxy=proxy)
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")  # UTC (igual a db._now_str)
    db.update_profile(p["id"], pool_status="warming", warm_started_at=now)
    try:
        profile = db.get_profile(p["id"])
        locale = (profile.get("fingerprint") or {}).get("language", "")
        # Timeout de segurança: se o aquecimento travar (proxy ruim, página pendurada),
        # não deixa o perfil preso em 'warming' para sempre bloqueando os slots do pool.
        timeout = _warm_minutes() * 60 + 120
        res = await asyncio.wait_for(
            sm.run_profile_task(
                profile, lambda pg: pw.browse(pg, _warm_minutes(), locale),
                headless=True, load_extension=False),
            timeout=timeout)
        if res and res.get("visited", 0) > 0:
            db.mark_profile_warmed(p["id"])
        db.update_profile(p["id"], pool_status="ready")
        logger.info("warm_pool: perfil %s pronto (ready)", p["id"])
        return True
    except Exception as e:
        logger.warning("warm_pool: falha ao aquecer %s — removendo: %s", p["id"], e)
        try:
            db.delete_profile(p["id"])
        except Exception:
            pass
        return False


# ── Mantenedor em background ──────────────────────────────────────────────────────

async def maintainer_loop():
    logger.info("warm_pool: mantenedor iniciado")
    backoff = _CYCLE_SLEEP
    while True:
        produced = attempted = 0
        try:
            if enabled():
                st = status()
                deficit = st["target"] - st["ready"] - st["warming"]
                slots = MAX_CONCURRENT_WARM - st["warming"]
                n = max(0, min(deficit, slots))
                if n > 0:
                    attempted = n
                    results = await asyncio.gather(*[_warm_one() for _ in range(n)],
                                                   return_exceptions=True)
                    produced = sum(1 for r in results if r is True)
        except Exception:
            logger.exception("warm_pool: erro no ciclo do mantenedor")

        # Backoff: se tentou e nada ficou pronto (ex.: proxies ruins), espera mais.
        if attempted and not produced:
            backoff = min(_BACKOFF_MAX, max(backoff * 2, _CYCLE_SLEEP))
        else:
            backoff = _CYCLE_SLEEP
        await asyncio.sleep(backoff)


def recover_orphans():
    """No boot, perfis presos em 'warming' são órfãos (o app reiniciou no meio do
    aquecimento e a task morreu). Remove para não travar os slots do pool nem poluir a
    lista de perfis. Seguro: perfis do pool não têm conta associada."""
    removed = 0
    for p in db.list_profiles():
        if p.get("pool_status") == "warming" and not db.get_account_by_profile(p["id"]):
            try:
                db.delete_profile(p["id"])
                removed += 1
            except Exception:
                pass
    if removed:
        logger.info("warm_pool: %d perfil(is) orfao(s) em 'warming' removido(s) no boot", removed)


def start():
    """Inicia o mantenedor (idempotente). Chamar de um contexto async (startup)."""
    global _maintainer_task
    recover_orphans()
    if _maintainer_task is None or _maintainer_task.done():
        _maintainer_task = asyncio.create_task(maintainer_loop())
        logger.info("warm_pool: task do mantenedor agendada")
