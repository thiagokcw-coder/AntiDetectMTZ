"""
API REST — iniciar com:
  uvicorn api:app --reload --port 8000
"""
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from pathlib import Path
import asyncio
import copy
import json
import re
import socket
import threading
import uuid

import profiles as prof
from db import (init_db, get_profile, list_profiles, delete_profile,
                update_profile, mark_profile_warmed, get_account_by_profile)
import db
from paths import APP_DIR
import session_manager as sm
import instagram as ig
import accounts as acc
import batch_import as bi
import emails as eb
import proxies as pool
import warm_pool
import spider
import spider_app
import time

import logging
from logsetup import setup_logging, tail, APP_LOG, ERROR_LOG
setup_logging()  # grava logs (incl. tracebacks) em %LOCALAPPDATA%\ProfileManager\logs
logger = logging.getLogger("profilemanager.api")

_creation_stop = False
_ig_creation_tasks: dict[str, asyncio.Task] = {}

app = FastAPI(title="Browser Profile Manager", version="1.0.0")

import sys
import platform as _platform
_base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent
STATIC_DIR = _base / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def public_profile(profile: dict) -> dict:
    item = copy.deepcopy(profile)
    proxy = item.get("proxy")
    if proxy and proxy.get("password"):
        proxy["password"] = None
        proxy["has_password"] = True
    return item


def _pool_proxy_by_key(key: str) -> dict:
    """Resolve as credenciais completas de um proxy do pool pela chave 'host:porta'."""
    from db import list_pool_proxies
    full = next((pp for pp in list_pool_proxies()
                 if f"{pp['host']}:{pp['port']}" == key), None)
    if not full:
        raise HTTPException(400, "Proxy do pool não encontrado")
    return full


def _lock_info_from(lk: dict | None) -> dict:
    """Monta o estado do lock a partir de um registro já buscado (ou None)."""
    if not lk:
        return {"in_use": False}
    return {
        "in_use": True,
        "holder": lk["holder"],
        "since": lk["since"],
        "is_self": lk.get("token") == sm.TOKEN,
    }


def lock_info(profile_id: str) -> dict:
    """Estado do lock compartilhado do perfil (para o aviso de 'em uso')."""
    from db import get_profile_lock
    return _lock_info_from(get_profile_lock(profile_id, sm.LOCK_TTL))

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    """Prova de prontidão LEVE (sem banco) — usada pelo main.py p/ saber que a API
    subiu. Não pingar /profiles (faz dezenas de queries no banco remoto)."""
    return {"ok": True}


@app.on_event("startup")
def on_startup():
    print("DEBUG: FastAPI on_startup started")
    try:
        init_db()
        print("DEBUG: init_db() completed successfully")
    except Exception:
        logger.exception("Failed to initialize database on startup. If using shared Postgres (DATABASE_URL), ensure the server is running and reachable. Falling back behavior may be limited.")
        print("DEBUG: init_db() FAILED - see exception above")
        # Do not re-raise; allow the server to start so the error page / logs can be seen. First requests will fail with DB errors.


@app.on_event("startup")
async def _startup_proxy_test():
    """Ao abrir o app, testa a conectividade dos proxies em segundo plano (não bloqueia)."""
    asyncio.create_task(test_pool_proxies())


@app.on_event("startup")
async def _startup_warm_pool():
    """Mantém sempre N perfis pré-aquecidos prontos para criar contas (background)."""
    try:
        warm_pool.start()
    except Exception:
        logger.exception("Falha ao iniciar o mantenedor do pool de perfis aquecidos")


@app.on_event("startup")
async def _startup_auto_recheck():
    """Re-checa o status de todas as contas periodicamente (setting auto_recheck_hours; 0 = off)."""
    async def _loop():
        while True:
            try:
                hours = int(db.get_setting("auto_recheck_hours", "0") or 0)
                if hours > 0 and acc.list_accounts():
                    last = float(db.get_setting("auto_recheck_last", "0") or 0)
                    now = time.time()
                    if last <= 0:
                        # primeira referência: espera um intervalo inteiro antes da 1a rodada
                        db.set_setting("auto_recheck_last", str(int(now)))
                    elif now - last >= hours * 3600:
                        logger.info("auto-recheck: verificando o status de todas as contas")
                        await _check_all_accounts()
                        db.set_setting("auto_recheck_last", str(int(time.time())))
            except Exception:
                logger.exception("auto-recheck: erro no loop")
            await asyncio.sleep(900)  # a cada 15 min, checa se já está na hora
    asyncio.create_task(_loop())


# ─── Models ───────────────────────────────────────────────

class ProxyModel(BaseModel):
    protocol: str = "http"
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None


class CreateProfileRequest(BaseModel):
    name: str
    os: str = "random"          # "random" varia o SO entre perfis
    proxy: Optional[ProxyModel] = None
    proxy_pool_key: Optional[str] = None   # "host:porta" de um proxy do pool compartilhado


class BulkCreateProfileRequest(BaseModel):
    profiles: list[CreateProfileRequest]


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = None
    proxy: Optional[ProxyModel] = None
    proxy_pool_key: Optional[str] = None   # "host:porta" de um proxy do pool compartilhado
    remove_proxy: bool = False             # True remove o proxy do perfil


class LaunchRequest(BaseModel):
    url: str = "https://abrahamjuliot.github.io/creepjs/"
    headless: bool = False
    mode: str = "manual"
    actions: Optional[list[dict]] = None


class ProxyCheckRequest(BaseModel):
    proxy: ProxyModel


# ─── Profiles ─────────────────────────────────────────────

@app.post("/profiles", status_code=201)
def create_profile(body: CreateProfileRequest):
    if body.proxy_pool_key:
        proxy = _pool_proxy_by_key(body.proxy_pool_key)
    else:
        proxy = body.proxy.model_dump() if body.proxy else None
    profile = prof.create(name=body.name, os=body.os, proxy=proxy)
    return public_profile(profile)


@app.post("/profiles/bulk", status_code=201)
def create_profiles_bulk(body: BulkCreateProfileRequest):
    created = []
    for item in body.profiles:
        proxy = item.proxy.model_dump() if item.proxy else None
        created.append(public_profile(prof.create(name=item.name, os=item.os, proxy=proxy)))
    return {"created": created, "count": len(created)}


@app.get("/profiles")
def get_profiles():
    items = list_profiles()
    try:
        outcomes = acc.proxy_outcomes()      # computa 1x e reusa por perfil
    except Exception:
        outcomes = {}
    # Mapas em 1 consulta cada (evita N+1 — caro no banco remoto): contas (versão
    # CRUA da tabela, não a 'merged' com histórico que é lenta) e locks ativos.
    acct_by_profile = {}
    try:
        for a in db.list_accounts():
            pid = a.get("profile_id")
            if pid and pid not in acct_by_profile:
                acct_by_profile[pid] = a
    except Exception:
        pass
    try:
        locks = db.get_active_profile_locks(sm.LOCK_TTL)
    except Exception:
        locks = {}
    for item in items:
        item["risk"] = assess_profile(item, outcomes)
        item["lock"] = _lock_info_from(locks.get(item["id"]))
        a = acct_by_profile.get(item["id"])
        item["account_status"] = (a.get("status") if a else None)
        item["account_username"] = (a.get("username") if a else None)
    return [public_profile(item) for item in items]


@app.get("/profiles/{profile_id}")
def get_profile_by_id(profile_id: str):
    p = get_profile(profile_id)
    if not p:
        raise HTTPException(404, "Perfil não encontrado")
    p["risk"] = assess_profile(p)
    p["lock"] = lock_info(profile_id)
    return public_profile(p)


@app.get("/profiles/{profile_id}/lock")
def get_profile_lock_status(profile_id: str):
    if not get_profile(profile_id):
        raise HTTPException(404, "Perfil não encontrado")
    return lock_info(profile_id)


@app.patch("/profiles/{profile_id}")
def edit_profile(profile_id: str, body: ProfileUpdateRequest):
    """Edita o perfil (nome e/ou proxy). A senha do proxy é preservada quando o
    host:porta não muda e a senha não é reenviada (vem mascarada na listagem)."""
    p = get_profile(profile_id)
    if not p:
        raise HTTPException(404, "Perfil não encontrado")

    fields = {}
    if body.name is not None and body.name.strip():
        fields["name"] = body.name.strip()

    if body.proxy_pool_key:
        fields["proxy"] = json.dumps(_pool_proxy_by_key(body.proxy_pool_key))
    elif body.remove_proxy:
        fields["proxy"] = None
    elif body.proxy is not None:
        np = body.proxy.model_dump()
        # Preserva a senha atual se o usuário não reenviou e é o mesmo host:porta.
        if not np.get("password"):
            old = p.get("proxy") or {}
            if (old.get("host") == np.get("host")
                    and str(old.get("port")) == str(np.get("port"))
                    and old.get("password")):
                np["password"] = old["password"]
        fields["proxy"] = json.dumps(np)

    if fields:
        update_profile(profile_id, **fields)

    updated = get_profile(profile_id)
    updated["risk"] = assess_profile(updated)
    updated["lock"] = lock_info(profile_id)
    return public_profile(updated)


def assess_profile(p: dict, outcomes: dict = None) -> dict:
    """
    Avalia o risco do perfil. Considera o que o usuário controla (o proxy) E o HISTÓRICO
    REAL daquele proxy (contas banidas/suspensas/checkpoint/descartadas) — um proxy que já
    queimou contas é mais arriscado, mesmo configurado corretamente.
    NOTA: fuso/idioma NÃO são avaliados — o geoip do Camoufox os deriva do IP em runtime.
    `outcomes` pode ser pré-computado (accounts.proxy_outcomes()) para evitar recomputar por perfil.
    """
    issues = []
    proxy = p.get("proxy")

    if not proxy:
        issues.append({"level": "high", "message": "Sem proxy: IP real exposto e fuso/idioma não acompanham um IP coerente."})
    else:
        if not proxy.get("host") or not proxy.get("port"):
            issues.append({"level": "high", "message": "Proxy incompleto (host/porta)."})
        if proxy.get("username") and not proxy.get("password"):
            issues.append({"level": "medium", "message": "Proxy tem usuário sem senha."})

    score = 100
    for item in issues:
        score -= {"high": 35, "medium": 18, "low": 8}.get(item["level"], 5)

    # Histórico real do proxy: penaliza proxies que já produziram contas ruins.
    if proxy and proxy.get("host") and proxy.get("port"):
        try:
            if outcomes is None:
                outcomes = acc.proxy_outcomes()
            po = outcomes.get(f"{proxy['host']}:{proxy['port']}")
        except Exception:
            po = None
        if po:
            bad = po.get("bad", 0)
            if bad:
                issues.append({"level": "high",
                    "message": f"Proxy já teve {bad} conta(s) banida(s)/suspensa(s) ({po.get('bad_rate', 0)}%)."})
                score -= 30 + min(40, bad * 10)          # escalona com a quantidade
                if po.get("bad_rate", 0) >= 50:
                    score -= 15                            # taxa alta de ban: pior ainda
            if po.get("checkpoint"):
                issues.append({"level": "medium",
                    "message": f"Proxy com {po['checkpoint']} conta(s) em checkpoint (verificação pendente)."})
                score -= 12
            if po.get("discarded"):
                issues.append({"level": "low",
                    "message": f"Proxy com {po['discarded']} conta(s) descartada(s) (pediram telefone na criação)."})
                score -= 6

        # Reachability: se o último teste do proxy falhou, é risco alto (proxy caído/instável).
        try:
            tr = _proxy_test_cache["results"].get(f"{proxy['host']}:{proxy['port']}")
        except Exception:
            tr = None
        if tr and not tr.get("ok"):
            issues.append({"level": "high",
                "message": f"Proxy não respondeu no último teste ({tr.get('error') or 'sem conexão'})."})
            score -= 35

    score = max(score, 0)
    status = "baixo" if score >= 80 else "medio" if score >= 55 else "alto"
    return {"score": score, "risk": status, "issues": issues}


@app.get("/profiles/{profile_id}/risk")
def get_profile_risk(profile_id: str):
    p = get_profile(profile_id)
    if not p:
        raise HTTPException(404, "Perfil nÃ£o encontrado")
    return assess_profile(p)


@app.post("/tools/proxy-check")
def check_proxy(body: ProxyCheckRequest):
    proxy = body.proxy.model_dump()
    host = proxy.get("host")
    port = proxy.get("port")
    try:
        with socket.create_connection((host, port), timeout=6):
            return {"ok": True, "host": host, "port": port}
    except Exception as e:
        return {"ok": False, "host": host, "port": port, "error": str(e)}


@app.delete("/profiles/{profile_id}", status_code=204)
async def remove_profile(profile_id: str):
    """Exclui o perfil E, em cascata, a(s) conta(s) e os e-mails vinculados a ele
    (espelho da exclusão de conta)."""
    p = get_profile(profile_id)
    if not p:
        raise HTTPException(404, "Perfil não encontrado")

    # 1. Encerra a sessão (libera user_data/lock antes de apagar)
    try:
        await sm.stop(profile_id, reason="delete_profile")
    except Exception:
        pass

    # 2. Apaga e-mails vinculados ao perfil
    for em in eb.list_emails():
        if em.get("profile_id") == profile_id:
            try:
                eb.delete_email(em["id"])
            except Exception:
                pass

    # 3. Apaga conta(s) vinculada(s) ao perfil
    for a in acc.list_accounts():
        if a.get("profile_id") == profile_id:
            try:
                acc.delete_account(a["id"])
            except Exception:
                pass

    # 4. Apaga o perfil (e remove o user_data do disco)
    prof.remove(profile_id)


# ─── Sessions (browser control) ───────────────────────────

@app.post("/profiles/{profile_id}/launch")
async def launch_profile(profile_id: str, body: LaunchRequest):
    p = get_profile(profile_id)
    if not p:
        raise HTTPException(404, "Perfil não encontrado")
    try:
        # A extensão ig-filler fica desligada por padrão: em alguns renders do IG ela
        # interfere na página. O fluxo automático usa filler interno via page.evaluate.
        auto_load_filler = False
        result = await sm.launch(p, url=body.url, headless=body.headless, actions=body.actions, mode=body.mode,
                                 load_ig_filler=auto_load_filler)
        return result
    except RuntimeError as e:
        raise HTTPException(409, str(e))


class WarmupProfileRequest(BaseModel):
    minutes: float = 10.0
    headless: bool = True   # segundo plano por padrão


@app.post("/profiles/{profile_id}/warmup-profile")
async def warmup_profile_endpoint(profile_id: str, body: WarmupProfileRequest = WarmupProfileRequest()):
    """Aquece o PERFIL: navega por sites comuns acumulando cookies/histórico."""
    p = get_profile(profile_id)
    if not p:
        raise HTTPException(404, "Perfil não encontrado")
    import profile_warmup as pw
    locale = p.get("fingerprint", {}).get("language", "")

    async def _task():
        try:
            res = await sm.run_profile_task(
                p, lambda page: pw.browse(page, body.minutes, locale), headless=body.headless
            )
            # Marca o perfil como aquecido se navegou em ao menos 1 site
            if res and res.get("visited", 0) > 0:
                mark_profile_warmed(profile_id)
        except Exception:
            pass

    asyncio.create_task(_task())
    return {"profile_id": profile_id, "status": "warming_profile", "minutes": body.minutes}


@app.post("/profiles/{profile_id}/stop")
async def stop_profile(profile_id: str):
    try:
        await sm.stop(profile_id, reason="manual_stop_endpoint")
        return {"profile_id": profile_id, "status": "stopped"}
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/profiles/{profile_id}/status")
def profile_status(profile_id: str):
    s = sm._sessions.get(profile_id)
    if not s:
        raise HTTPException(404, "Sessão não encontrada")
    result = {"profile_id": profile_id, "status": s["status"], "url": s.get("url")}
    if s.get("traceback"):
        result["error"] = s["traceback"]
    return result


@app.get("/sessions")
def list_sessions():
    return sm.list_active()


@app.get("/diagnostics")
def diagnostics():
    profiles = list_profiles()
    sessions = sm.list_active()
    try:
        from instagram import DETECTION_VERSION as CURRENT_DETECTION_VERSION
    except Exception:
        CURRENT_DETECTION_VERSION = "unknown"

    return {
        "browser": sm.browser_info(),
        "profile_count": len(profiles),
        "active_sessions": len(sessions),
        "app_dir": str(APP_DIR),
        "app_version": "1.0.0",
        "detection_version": CURRENT_DETECTION_VERSION,
    }


@app.get("/logs")
def get_logs(kind: str = "errors", lines: int = 200):
    """Últimas linhas do log para investigar erros (kind=errors|app)."""
    path = ERROR_LOG if kind != "app" else APP_LOG
    return {"kind": kind, "path": str(path), "content": tail(path, max(1, min(lines, 2000)))}


# ─── Bulk launch ──────────────────────────────────────────

class BulkLaunchRequest(BaseModel):
    profile_ids: list[str]
    url: str = "about:blank"
    headless: bool = False
    mode: str = "manual"
    actions: Optional[list[dict]] = None


@app.post("/sessions/bulk-launch")
async def bulk_launch(body: BulkLaunchRequest):
    results = []
    tasks = []
    for pid in body.profile_ids:
        p = get_profile(pid)
        if not p:
            results.append({"profile_id": pid, "error": "não encontrado"})
            continue
        auto_load_filler = False
        tasks.append(sm.launch(p, url=body.url, headless=body.headless, actions=body.actions, mode=body.mode, load_ig_filler=auto_load_filler))

    launched = await asyncio.gather(*tasks, return_exceptions=True)
    for item in launched:
        if isinstance(item, Exception):
            results.append({"error": str(item)})
        else:
            results.append(item)
    return results


# ─── Instagram ────────────────────────────────────────────────────────────────

class IgCreateRequest(BaseModel):
    profile_id: str
    email: Optional[str] = None             # se vazio + auto_email, gera um catch-all
    auto_email: bool = False                # gera o email automaticamente (catch-all)
    password: str
    full_name: str
    username: Optional[str] = None
    birthday: Optional[dict] = None          # {month, day, year}
    email_code: Optional[str] = None        # código recebido no email (opcional)
    sms_activate_key: Optional[str] = None  # chave API sms-activate.org
    sms_country: int = 73                   # 73=Brasil, 12=Indonésia, 0=qualquer
    warmup_minutes: int = 8
    force: bool = False                     # ignora limite de uso do proxy


class IgWarmupRequest(BaseModel):
    profile_id: str
    minutes: int = 8


def _get_active_page(profile_id: str):
    """Retorna a page Playwright da sessão ativa, ou None."""
    return sm._sessions.get(profile_id, {}).get("page")


async def _ensure_session_with_page(profile_id: str, url: str = "about:blank"):
    """Garante que o perfil tenha uma sessão rodando com page CDP.

    IMPORTANTE: lançamos em about:blank de propósito. Quem chama (create_account)
    faz a navegação. Se o launch também navegasse para a mesma URL, as duas
    navegações concorreriam na mesma page — o _run trataria a interrupção como erro
    e fecharia o navegador, derrubando o create_account com 'browser has been closed'.
    """
    s = sm._sessions.get(profile_id, {})
    if s.get("status") == "running" and s.get("page"):
        return s["page"]
    p = get_profile(profile_id)
    if not p:
        return None
    # Não carregamos extensões durante criação. O fluxo automático usa um filler
    # interno via page.evaluate, sem injetar/carregar ig-filler-extension.
    await sm.launch(p, url="about:blank", headless=False, mode="controlled", load_extension=False, load_ig_filler=False)
    # Aguarda até a page estar disponível. O launch headful + geoip (sobretudo em
    # conexão móvel/lenta) pode levar 20-30s+ — 20s era curto e dava falso
    # "não foi possível abrir o navegador". Espera generosa (~60s), mas sai rápido
    # se a sessão morrer (status error/stopped), sem ficar 60s à toa.
    for _ in range(120):
        await asyncio.sleep(0.5)
        s = sm._sessions.get(profile_id, {})
        page = s.get("page")
        if page:
            return page
        if s.get("status") in ("error", "stopped"):
            break
    return None


@app.post("/instagram/create-account")
async def ig_create(body: IgCreateRequest):
    p = get_profile(body.profile_id)
    if not p:
        raise HTTPException(404, "Perfil não encontrado")

    # Regra: 1 conta por perfil (a menos que force=True)
    astat = acc.profile_account_status(body.profile_id)
    if astat["has_account"] and not body.force:
        raise HTTPException(409, {
            "error": "profile_in_use",
            "message": f"Este perfil já tem a conta '{astat['username']}' "
                       f"(status: {astat['status']}). Cada conta deve ter seu próprio "
                       f"perfil. Crie um novo perfil ou envie force=true.",
            "profile_account": astat,
        })

    # Enforcement do limite de uso do proxy (a menos que force=True)
    pstat = acc.proxy_limit_status(p.get("proxy"))
    if pstat["blocked"] and not body.force:
        limit_source = "recomendação baseada em ban rate" if pstat.get("is_recommendation_based") else "limite global"
        raise HTTPException(409, {
            "error": "proxy_limit",
            "message": f"Proxy {pstat['proxy_key']} já foi usado em {pstat['used']} "
                       f"conta(s) (limite efetivo {pstat['limit']} - {limit_source}). "
                       f"Use outro proxy ou envie force=true.",
            "proxy_usage": pstat,
        })

    # Email: usa o informado ou gera um catch-all automaticamente (vinculado ao perfil)
    email = (body.email or "").strip()
    if body.auto_email or not email:
        if not eb.get_config().get("email_domain"):
            raise HTTPException(400, "Configure o domínio de email primeiro (aba Emails) "
                                     "para gerar emails automaticamente")
        em = eb.create_email(base="", profile_id=body.profile_id)
        email = em["address"]

    page = await _ensure_session_with_page(
        body.profile_id, "https://www.instagram.com/accounts/emailsignup/"
    )
    if not page:
        raise HTTPException(503, "Não foi possível abrir o navegador para este perfil")

    data = {
        "email":              email,
        "password":           body.password,
        "full_name":          body.full_name,
        "username":           body.username,
        "birthday":           body.birthday or ig.gen_birthday(),
        "email_code":         body.email_code,
        "sms_activate_key":   body.sms_activate_key,
        "sms_country":        body.sms_country,
    }

    global _creation_stop, _ig_creation_tasks
    _creation_stop = False
    task = asyncio.create_task(ig.create_account(page, body.profile_id, data))
    _ig_creation_tasks[body.profile_id] = task
    return {"profile_id": body.profile_id, "status": "started", "email": email}


# ─── Criação MANUAL (você preenche; o app só prepara o básico e abre a janela) ─
class IgCreateManualRequest(BaseModel):
    base: Optional[str] = None
    use_proxy_pool: bool = True   # False = conexão local (celular/tethering)
    os: str = "random"
    warmup_minutes: float = 15    # aquece o navegador antes de abrir p/ você preencher


@app.post("/instagram/create-manual")
async def ig_create_manual(body: IgCreateManualRequest):
    """Modo manual — o app só prepara o BÁSICO (perfil + e-mail) e gera os dados
    para você copiar. Em background: aquece o navegador (a UI mostra a barra) e
    abre o Camoufox na tela de cadastro SEM preencher — você digita."""
    # 1) Provisiona perfil (reaproveita perfil aquecido do pool quando houver)
    claimed = await warm_pool.claim()
    if claimed and not body.use_proxy_pool:
        try:
            db.update_profile(claimed["id"], proxy=None)
        except Exception:
            pass
    if claimed:
        profile_id = claimed["id"]
        already_warm = True   # perfil do pool já vem aquecido
    else:
        proxy = None
        if body.use_proxy_pool:
            assigned = pool.assign(1, acc.get_limits()["proxy_limit"])
            proxy = assigned[0] if assigned else None
            if proxy is None:
                raise HTTPException(400, "Sem proxy livre no pool. Use a conexão do celular ou adicione proxies.")
        profile_id = prof.create(name=(body.base or "conta"), os=body.os, proxy=proxy)["id"]
        already_warm = False

    em = eb.create_email(base=body.base or "", profile_id=profile_id)

    # 2) Gera os dados (mesmos geradores do fluxo automático)
    base = em["address"].split("@")[0].split("+")[0]
    full_name = ig.gen_full_name(base=base)
    username  = ig._gen_username(full_name)
    password  = ig.gen_password()
    bd        = ig.gen_birthday()

    # Nomeia o perfil pelo username gerado (em vez de "manual"/"conta"/"pool").
    try:
        db.update_profile(profile_id, name=(username or base or "conta")[:40])
    except Exception:
        pass

    warm_minutes = 0.0 if already_warm else max(0.0, float(body.warmup_minutes or 0))

    # 3) Em background: aquece o navegador (se preciso) e abre a janela do cadastro.
    #    Não bloqueia a resposta — a UI mostra a barra de aquecimento e a janela
    #    abre sozinha ao final.
    async def _prep():
        try:
            if warm_minutes > 0:
                profile = get_profile(profile_id)
                locale = (profile.get("fingerprint") or {}).get("language", "")
                import profile_warmup as pw
                try:
                    await sm.run_profile_task(
                        profile, lambda pg: pw.browse(pg, warm_minutes, locale),
                        headless=True, load_extension=False)
                except Exception:
                    logger.warning("manual: falha ao aquecer %s", profile_id)
            page = await _ensure_session_with_page(profile_id, "about:blank")
            if page:
                try:
                    await ig._goto_retry(page, "https://www.instagram.com/accounts/emailsignup/")
                except Exception:
                    pass
        except Exception:
            logger.exception("manual: erro ao preparar %s", profile_id)

    asyncio.create_task(_prep())

    return {
        "profile_id": profile_id,
        "email": em["address"],
        "password": password,
        "full_name": full_name,
        "username": username,
        "birthday": bd,            # {month, day, year}
        "warmup_minutes": warm_minutes,
    }


@app.get("/instagram/manual/email-code")
async def ig_manual_email_code(email: str):
    """Busca o código de verificação do e-mail (catch-all/IMAP) para o modo manual.
    Faz uma checagem curta; a UI chama isto em loop até o código chegar."""
    if not eb.get_config().get("configured"):
        return {"code": None, "configured": False}
    try:
        code = await eb.fetch_code(email, timeout=8, poll=4)
    except Exception:
        code = None
    return {"code": code, "configured": True}


class IgCreateBatchRequest(BaseModel):
    count: int = 1
    base: Optional[str] = None
    use_proxy_pool: bool = True            # distribui proxies do pool compartilhado
    os: str = "random"
    sms_activate_key: Optional[str] = None
    sms_country: int = 73
    delay_seconds: float = 90              # intervalo entre contas (mais lento = menos sinal de bot)
    warmup_minutes: float = 15            # aquece o NAVEGADOR antes de criar (cookies/histórico → parece humano)
    account_warmup_minutes: float = 5     # aquece a CONTA após criar (se logada)


# Jobs de criação em lote (em memória) — para acompanhamento de progresso/ETA na UI
_ig_batch_jobs: dict[str, dict] = {}
_IG_ROUGH_PER_ACCOUNT = 75   # estimativa de trabalho por conta (s), fora o delay


@app.post("/instagram/create-batch")
async def ig_create_batch(body: IgCreateBatchRequest):
    """Cria N contas DO ZERO: gera perfil + email catch-all + conta para cada uma.
    Roda em segundo plano, sequencial, com intervalo entre contas. Retorna um
    job_id para acompanhar o progresso em /instagram/batch/{job_id}."""
    global _creation_stop
    if not eb.get_config().get("email_domain"):
        raise HTTPException(400, "Configure o domínio de email primeiro (aba Emails)")
    _creation_stop = False   # zera o sinal de parada ao iniciar um novo lote

    n = max(1, min(body.count, 100))
    proxy_limit = acc.get_limits()["proxy_limit"]

    # Provisiona N contas priorizando perfis JÁ pré-aquecidos do pool (claim).
    # O que o pool não cobrir cai no modo antigo: cria perfil + proxy e aquece no worker.
    items = []
    short_of_proxy = 0
    from_pool_count = 0
    fallback_needed = 0
    for i in range(n):
        # Sempre tenta reaproveitar um perfil JÁ aquecido do pool (cookies/histórico
        # = mais humano). No modo local (sem proxy), usa o perfil aquecido mas REMOVE
        # o proxy dele: a criação sai pela conexão local (ex.: tethering do celular).
        claimed = await warm_pool.claim()
        if claimed and not body.use_proxy_pool:
            try:
                db.update_profile(claimed["id"], proxy=None)
                claimed["proxy"] = None
            except Exception:
                pass
        if claimed:
            em = eb.create_email(base=body.base or "", profile_id=claimed["id"])
            items.append({"email": em, "profile_id": claimed["id"], "from_pool": True})
            from_pool_count += 1
        else:
            fallback_needed += 1

    if fallback_needed:
        pool_proxies = (pool.assign(fallback_needed, proxy_limit)
                        if body.use_proxy_pool else [None] * fallback_needed)
        for j in range(fallback_needed):
            proxy = pool_proxies[j] if j < len(pool_proxies) else None
            if body.use_proxy_pool and proxy is None:
                short_of_proxy += 1
                continue
            p = prof.create(name=f"{body.base or 'conta'}-{len(items)+1}", os=body.os, proxy=proxy)
            em = eb.create_email(base=body.base or "", profile_id=p["id"])
            items.append({"email": em, "profile_id": p["id"], "from_pool": False})

    if not items:
        raise HTTPException(400, "Nenhuma conta pôde ser preparada — o pool de proxies "
                                 "atingiu o limite. Adicione proxies ou aumente o limite.")

    job_id = uuid.uuid4().hex[:12]
    delay = body.delay_seconds
    warm_minutes = max(0.0, body.warmup_minutes or 0)
    acct_warm_minutes = max(0.0, body.account_warmup_minutes or 0)
    total = len(items)
    job = {
        "id": job_id,
        "status": "running",
        "total": total,
        "done": 0,
        "created": 0,
        "errors": 0,
        "started_at": time.time(),
        "current": None,
        # estimativa inicial (refinada conforme as contas concluem)
        "eta_seconds": round((_IG_ROUGH_PER_ACCOUNT + (warm_minutes + acct_warm_minutes) * 60 + delay) * total),
        "delay_seconds": delay,
        "items": [
            {"index": i, "email": it["email"]["address"], "username": None,
             "status": "pending", "detail": "", "proxy_key": None, "proxy_host": None}
            for i, it in enumerate(items)
        ],
    }
    if short_of_proxy:
        job["warning"] = (f"{short_of_proxy} conta(s) não criadas: o pool ficou sem "
                          f"proxy livre dentro do limite.")
    _ig_batch_jobs[job_id] = job

    async def worker():
        import profile_warmup as pw_mod
        try:
            for i, it in enumerate(items):
                if _creation_stop:
                    break
                row = job["items"][i]
                row["status"] = "creating"
                job["current"] = it["email"]["address"]
                try:
                    # Checagem rápida do proxy — proxy morto é pulado já (poupa minutos)
                    prof_row = get_profile(it["profile_id"])
                    px = (prof_row or {}).get("proxy")
                    # Registra o proxy desta conta no item (alimenta o indicador global do topo)
                    if px and px.get("host"):
                        pkey = pool.proxy_key(px)
                        row["proxy_key"] = pkey
                        row["proxy_host"] = px["host"]
                        try:
                            t0 = time.time()
                            with socket.create_connection((px["host"], int(px["port"])), timeout=8):
                                pass
                            # Observação real → atualiza o cache de reachability (Parte 3)
                            _mark_proxy_reachable(pkey, True, ms=round((time.time() - t0) * 1000))
                        except Exception as e:
                            _mark_proxy_reachable(pkey, False, error=str(e))
                            raise RuntimeError(f"proxy não responde ({px['host']}:{px['port']}): {e}")

                    # Aquece o navegador antes de criar (acumula cookies/histórico).
                    # Perfis vindos do pool (from_pool) JÁ estão aquecidos → pula.
                    if warm_minutes > 0 and not it.get("from_pool"):
                        row["detail"] = "aquecendo navegador"
                        profile = get_profile(it["profile_id"])
                        locale = (profile.get("fingerprint") or {}).get("language", "")
                        try:
                            res = await sm.run_profile_task(
                                profile, lambda pg: pw_mod.browse(pg, warm_minutes, locale),
                                headless=True, load_extension=False)
                            if res and res.get("visited", 0) > 0:
                                mark_profile_warmed(it["profile_id"])
                        except Exception:
                            logger.warning("lote %s: falha ao aquecer %s", job_id, it["email"]["address"])

                    if _creation_stop:
                        break

                    email_addr = it["email"]["address"]
                    base = email_addr.split('@')[0].split('+')[0]
                    fn = ig.gen_full_name(base=base)
                    un = ig._gen_username(fn)
                    pw = ig.gen_password()
                    row["username"] = un
                    row["detail"] = "criando conta"
                    _from_pool = bool(it.get("from_pool"))
                    _eff_warm = warm_pool._warm_minutes() if _from_pool else warm_minutes
                    await _create_account_for_email(
                        it["email"], fn, un, pw, body.sms_activate_key, body.sms_country,
                        warmup_minutes=_eff_warm, from_pool=_from_pool)
                    # status real da conta criada (creating/waiting_verification/created…)
                    a = get_account_by_profile(it["profile_id"])
                    row["status"] = "done"
                    row["detail"] = (a.get("status") if a else "criada") or "criada"
                    job["created"] += 1
                    logger.info("lote %s: conta %s -> %s", job_id, it["email"]["address"], row["detail"])

                    # Passo 5: aquece a CONTA — só faz sentido se ela ficou logada
                    if acct_warm_minutes > 0 and a and a.get("status") in ("created", "active"):
                        row["detail"] = "aquecendo conta"
                        try:
                            await acc.run_account_warmup(a["id"], acct_warm_minutes, headless=True)
                            row["detail"] = "conta aquecida"
                        except Exception:
                            logger.warning("lote %s: falha ao aquecer conta %s", job_id, it["email"]["address"])
                    elif acct_warm_minutes > 0:
                        row["detail"] = ((a.get("status") if a else "criada") or "criada") + " (sem aquecer: não logada)"
                except Exception as e:
                    row["status"] = "error"
                    row["detail"] = str(e)[:140]
                    job["errors"] += 1
                    logger.exception("lote %s: erro criando conta %s", job_id, it["email"]["address"])
                job["done"] = i + 1
                # ETA refinada: tempo médio real por conta concluída × restantes
                elapsed = time.time() - job["started_at"]
                remaining = total - job["done"]
                avg = elapsed / job["done"] if job["done"] else (_IG_ROUGH_PER_ACCOUNT + delay)
                job["eta_seconds"] = round(avg * remaining)
                if i < total - 1 and not _creation_stop:
                    await asyncio.sleep(delay)
            else:
                job["status"] = "completed"   # só se o for terminou sem break
        except asyncio.CancelledError:
            pass   # cancelado pelo /creations/stop
        finally:
            _ig_creation_tasks.pop(job_id, None)
            if job["status"] == "running":
                job["status"] = "stopped"
            job["current"] = None
            job["eta_seconds"] = 0

    _ig_creation_tasks[job_id] = asyncio.create_task(worker())
    out = {"status": "started", "count": total, "job_id": job_id}
    if short_of_proxy:
        out["warning"] = job["warning"]
    return out


@app.get("/instagram/batch/{job_id}")
def ig_batch_status(job_id: str):
    j = _ig_batch_jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job de criação não encontrado")
    elapsed = round(time.time() - j["started_at"])
    return {**j, "elapsed_seconds": elapsed}


@app.get("/instagram/batch")
def ig_batch_list():
    return sorted(_ig_batch_jobs.values(), key=lambda j: j.get("started_at", 0), reverse=True)


@app.get("/instagram/status/{profile_id}")
def ig_status(profile_id: str):
    s = ig.flow_status(profile_id)
    if not s:
        raise HTTPException(404, "Nenhum fluxo Instagram para este perfil")
    return s


@app.get("/sms-activate/balance")
async def sms_balance(api_key: str):
    from sms_activate import SmsActivate, COUNTRIES
    client = SmsActivate(api_key)
    try:
        bal = await client.balance()
        return {"balance": bal, "countries": COUNTRIES}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/instagram/warmup/{profile_id}")
async def ig_warmup(profile_id: str, body: IgWarmupRequest = IgWarmupRequest(profile_id="")):
    minutes = body.minutes if body.profile_id else 8
    page = _get_active_page(profile_id)
    if not page:
        raise HTTPException(503, "Nenhuma sessão ativa para este perfil")
    asyncio.create_task(ig.warmup(page, profile_id, minutes))
    return {"profile_id": profile_id, "status": "warmup_started", "minutes": minutes}


# ─── Monitoração de contas ──────────────────────────────────────────────────

class LimitsRequest(BaseModel):
    proxy_limit: Optional[int] = None
    whatsapp_limit: Optional[int] = None
    target_country: Optional[str] = None
    auto_recheck_hours: Optional[int] = None


class AccountCreateRequest(BaseModel):
    profile_id: Optional[str] = None   # id existente | "__new__" (cria perfil) | None (sem perfil)
    platform: str = "instagram"
    username: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    whatsapp_number: Optional[str] = None
    status: str = "created"
    proxy: Optional[str] = None        # só p/ profile_id="__new__"; vazio = conexão local (internet do celular)


def _host_profile_os() -> str:
    """SO do fingerprint do perfil novo = SO real do PC (coerência ao abrir no PC)."""
    s = _platform.system()
    return "macos" if s == "Darwin" else "windows" if s == "Windows" else "linux"


@app.get("/accounts")
def accounts_list():
    return acc.list_accounts()


class AccountUpdateRequest(BaseModel):
    profile_id: Optional[str] = None
    platform: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    whatsapp_number: Optional[str] = None
    status: Optional[str] = None


@app.post("/accounts", status_code=201)
def accounts_create(body: AccountCreateRequest):
    pid = body.profile_id
    if pid == "__new__":
        # Conta criada no celular: cria um perfil Camoufox p/ poder abrir no PC.
        # Conexão local (proxy=None) = usa a internet do PC, que será o tethering
        # do celular → mesmo IP da criação. Proxy é opcional (não recomendado).
        proxy_dict = None
        if body.proxy and body.proxy.strip():
            proxy_dict = pool.parse_proxy_line(body.proxy.strip())
            if proxy_dict is None:
                raise HTTPException(400, "Proxy inválido (use host:porta:usuário:senha ou URL)")
        name = (body.username or body.email or "conta").strip()[:40] or "conta"
        pid = prof.create(name=name, os=_host_profile_os(), proxy=proxy_dict)["id"]
    elif pid and not get_profile(pid):
        raise HTTPException(404, "Perfil informado não existe")
    data = body.model_dump()
    data.pop("proxy", None)
    data["profile_id"] = pid or None
    try:
        acct = acc.add_manual(data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Marca o e-mail correspondente (se existir no registro da aba Emails) como
    # 'usado' — senão ficava 'livre' mesmo já vinculado a uma conta.
    try:
        eb.mark_used_by_address(acct.get("email"), acct.get("id"))
    except Exception:
        pass
    return acct


@app.patch("/accounts/{account_id}")
def accounts_update(account_id: str, body: AccountUpdateRequest):
    if not acc.get_account(account_id):
        raise HTTPException(404, "Conta não encontrada")
    if body.profile_id == "__new__":
        body.profile_id = None  # edição não cria perfil; ignora o sentinela
    if body.profile_id and not get_profile(body.profile_id):
        raise HTTPException(404, "Perfil informado não existe")
    try:
        # exclude_unset: só os campos enviados são alterados
        return acc.edit(account_id, body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(400, str(e))


class CreateProfileForAccountRequest(BaseModel):
    proxy: Optional[str] = None   # vazio = conexão local (internet do celular)


@app.post("/accounts/{account_id}/create-profile")
def accounts_create_profile(account_id: str, body: CreateProfileForAccountRequest = CreateProfileForAccountRequest()):
    """Cria um perfil Camoufox (conexão local por padrão) e vincula à conta, para
    poder abrir no PC. Usado em contas cadastradas sem perfil (criadas no celular)."""
    a = acc.get_account(account_id)
    if not a:
        raise HTTPException(404, "Conta não encontrada")
    if a.get("profile_id") and get_profile(a["profile_id"]):
        raise HTTPException(400, "Esta conta já tem um perfil vinculado")
    proxy_dict = None
    if body.proxy and body.proxy.strip():
        proxy_dict = pool.parse_proxy_line(body.proxy.strip())
        if proxy_dict is None:
            raise HTTPException(400, "Proxy inválido (use host:porta:usuário:senha ou URL)")
    name = (a.get("username") or a.get("email") or "conta")[:40] or "conta"
    pid = prof.create(name=name, os=_host_profile_os(), proxy=proxy_dict)["id"]
    return acc.edit(account_id, {"profile_id": pid})


@app.get("/accounts/export")
def accounts_export():
    """Exporta todas as contas em CSV (download)."""
    from fastapi.responses import Response
    csv_data = acc.export_accounts_to_csv()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=contas_export.csv"},
    )


# ─── Spider: extração de dados do IG rodando DENTRO de um perfil logado ───────
import csv as _csv
import io as _io

_spider_jobs: dict[str, dict] = {}


class SpiderExtractRequest(BaseModel):
    method: str = "web"               # "web" (perfil logado) | "app" (instagrapi)
    profile_id: Optional[str] = None  # usado no modo web
    account_id: Optional[str] = None  # usado no modo app (precisa usuário+senha)
    use_local: bool = False           # modo app: ignora o proxy da conta (sai pelo IP local/celular)
    kind: str = "followers"
    target: str
    limit: int = 0


@app.post("/spider/extract")
async def spider_extract(body: SpiderExtractRequest):
    if not (body.target or "").strip():
        raise HTTPException(400, "Informe o alvo (@usuário ou URL)")
    method = (body.method or "web").lower()
    a = p = None
    if method == "app":
        a = db.get_account(body.account_id) if body.account_id else None
        if not a:
            raise HTTPException(404, "Conta não encontrada")
        if not (a.get("username") and a.get("password")):
            raise HTTPException(400, "A conta precisa de usuário e senha cadastrados para o modo app (instagrapi).")
        label = a.get("username")
    else:
        p = get_profile(body.profile_id) if body.profile_id else None
        if not p:
            raise HTTPException(404, "Perfil não encontrado")
        label = p.get("name")

    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "status": "running", "kind": body.kind, "target": body.target,
           "count": 0, "rows": [], "error": None, "stop": False, "profile": label,
           "method": method, "detail": "", "code": None, "code_method": None}
    _spider_jobs[job_id] = job

    def _progress(n): job["count"] = n
    def _should_stop(): return job.get("stop", False)

    def _status(state):
        job["detail"] = {"logando": "Logando na conta (o IG pode pedir um código)…",
                         "extraindo": "Extraindo seguidores…"}.get(state, state or "")

    def _code_provider(username, choice):
        # Chamado (em thread) quando o IG pede código de confirmação. Pausa o job,
        # a UI mostra o campo, e bloqueia até o usuário enviar o código.
        ev = threading.Event()
        job["_code_event"] = ev
        job["code"] = None
        job["code_method"] = str(getattr(choice, "name", choice) or "")
        job["status"] = "needs_code"
        job["detail"] = "O Instagram pediu um código de confirmação — digite o código enviado."
        ev.wait(timeout=300)
        job["status"] = "running"
        job["detail"] = "Código enviado, continuando…"
        return job.get("code") or ""

    async def _worker():
        try:
            if method == "app":
                res = await spider_app.extract(
                    a, body.kind, body.target, body.limit,
                    progress=_progress, should_stop=_should_stop,
                    code_provider=_code_provider, status=_status, use_local=body.use_local)
            else:
                res = await spider.extract(
                    p, body.kind, body.target, body.limit,
                    progress=_progress, should_stop=_should_stop)
            if res.get("error"):
                job["status"] = "error"; job["error"] = res["error"]
            else:
                job["rows"] = res.get("rows", [])
                job["count"] = len(job["rows"])
                job["status"] = "done"
                job["detail"] = ""
        except Exception as e:
            job["status"] = "error"; job["error"] = str(e)

    asyncio.create_task(_worker())
    return {"job_id": job_id}


@app.get("/spider/jobs/{job_id}")
def spider_job(job_id: str):
    j = _spider_jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job não encontrado")
    return {"id": j["id"], "status": j["status"], "count": j["count"],
            "kind": j["kind"], "target": j["target"], "error": j["error"],
            "detail": j.get("detail", ""), "code_method": j.get("code_method")}


class SpiderCodeRequest(BaseModel):
    code: str


@app.post("/spider/jobs/{job_id}/code")
def spider_job_code(job_id: str, body: SpiderCodeRequest):
    """Recebe o código de confirmação do IG (checkpoint) digitado pelo usuário e
    libera o login (modo app/instagrapi)."""
    j = _spider_jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job não encontrado")
    j["code"] = (body.code or "").strip()
    ev = j.get("_code_event")
    if ev:
        ev.set()
    return {"ok": True}


@app.post("/spider/jobs/{job_id}/stop")
def spider_job_stop(job_id: str):
    j = _spider_jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job não encontrado")
    j["stop"] = True
    return {"ok": True}


@app.get("/spider/jobs/{job_id}/csv")
def spider_job_csv(job_id: str):
    from fastapi.responses import Response
    j = _spider_jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job não encontrado")
    rows = j.get("rows") or []
    buf = _io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=spider.FOLLOWERS_HEADERS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    fname = f"IGFollowers_{spider.clean_username(j['target'])}_{len(rows)}.csv"
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.get("/accounts/{account_id}")
def accounts_get(account_id: str):
    a = acc.get_account(account_id)
    if not a:
        raise HTTPException(404, "Conta não encontrada")
    return a


@app.delete("/accounts/{account_id}", status_code=204)
async def accounts_delete(account_id: str):
    """Exclui a conta E, em cascata, o perfil vinculado (com o user_data em disco)
    e os e-mails ligados a ela/ao perfil."""
    a = acc.get_account(account_id)
    if not a:
        raise HTTPException(404, "Conta não encontrada")
    profile_id = a.get("profile_id")

    # 1. Encerra a sessão do perfil (libera user_data/lock antes de apagar)
    if profile_id:
        try:
            await sm.stop(profile_id, reason="bulk_launch_cleanup")
        except Exception:
            pass

    # 2. Apaga e-mails vinculados (pela conta ou pelo perfil)
    for em in eb.list_emails():
        if em.get("account_id") == account_id or (profile_id and em.get("profile_id") == profile_id):
            try:
                eb.delete_email(em["id"])
            except Exception:
                pass

    # 3. Apaga a conta
    acc.delete_account(account_id)

    # 4. Apaga o perfil (e remove o user_data do disco)
    if profile_id and get_profile(profile_id):
        try:
            prof.remove(profile_id)
        except Exception as e:
            logger.warning("falha ao remover perfil %s ao excluir conta: %s", profile_id, e)


@app.post("/accounts/{account_id}/check")
async def accounts_check(account_id: str):
    if not acc.get_account(account_id):
        raise HTTPException(404, "Conta não encontrada")
    result = await acc.check_status(account_id, headless=True)
    return result


class AccountWarmupRequest(BaseModel):
    minutes: float = 8
    headless: bool = True   # roda em segundo plano por padrão


@app.post("/accounts/{account_id}/warmup")
async def accounts_warmup(account_id: str, body: AccountWarmupRequest = AccountWarmupRequest()):
    a = acc.get_account(account_id)
    if not a:
        raise HTTPException(404, "Conta não encontrada")
    if not a.get("profile_exists"):
        raise HTTPException(409, "Conta sem perfil vinculado — não há navegador para aquecer")
    w = acc.warming_status(account_id)
    if w and w.get("active"):
        raise HTTPException(409, "Já está aquecendo esta conta")
    asyncio.create_task(acc.run_account_warmup(account_id, body.minutes, body.headless))
    return {"account_id": account_id, "status": "warming", "minutes": body.minutes}


async def _check_all_accounts():
    """Verifica o status de todas as contas, sequencial e headless."""
    for a in acc.list_accounts():
        try:
            await acc.check_status(a["id"], headless=True)
        except Exception:
            pass


@app.post("/accounts/check-all")
async def accounts_check_all():
    """Dispara verificação de todas as contas em segundo plano (sequencial)."""
    asyncio.create_task(_check_all_accounts())
    return {"status": "checking", "count": len(acc.list_accounts())}


@app.get("/proxies/usage")
def proxies_usage():
    return acc.proxy_usage_summary()


@app.get("/proxies/pool")
def proxies_pool():
    """Resumo do pool de proxies compartilhado (tabela proxy_pool no banco)."""
    return pool.summary()


class PoolImportRequest(BaseModel):
    text: str   # uma linha por proxy (host:porta[:user:senha] ou scheme://...)


@app.post("/proxies/pool")
def proxies_pool_add(body: PoolImportRequest):
    """Adiciona proxies ao pool COMPARTILHADO (uma linha por proxy)."""
    result = pool.import_from_text(body.text)
    result["count"] = pool.summary()["count"]
    return result


@app.delete("/proxies/pool/{key}", status_code=204)
def proxies_pool_delete(key: str):
    """Remove um proxy do pool compartilhado pela chave 'host:porta'."""
    from db import delete_pool_proxy
    delete_pool_proxy(key)


# ── Teste de conectividade dos proxies ──────────────────────────────────────
_proxy_test_cache = {"tested_at": None, "running": False, "results": {}}


def _mark_proxy_reachable(key: str, ok: bool, error: str = None, ms: int = None):
    """Atualiza o cache de reachability a partir de uma observação REAL (ex.: a
    checagem TCP do worker de criação). Assim a saúde/indicador refletem proxy
    caído sem depender de rodar /proxies/test manualmente."""
    if not key:
        return
    try:
        _proxy_test_cache["results"][key] = {
            "key": key, "ok": bool(ok),
            "ms": ms, "error": (str(error)[:80] if error else None),
        }
        _proxy_test_cache["tested_at"] = time.time()
    except Exception:
        pass


async def _test_one_proxy(p: dict, timeout: float = 7.0) -> dict:
    """Conecta no host:porta do proxy (TCP) e mede a latência."""
    host, port = p.get("host"), int(p.get("port") or 0)
    key = f"{host}:{port}"
    t0 = time.time()
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        return {"key": key, "ok": True, "ms": round((time.time() - t0) * 1000), "error": None}
    except Exception as e:
        return {"key": key, "ok": False, "ms": None, "error": str(e)[:80] or type(e).__name__}
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


async def test_pool_proxies() -> dict:
    """Testa todos os proxies do pool em paralelo (limitado) e guarda no cache."""
    if _proxy_test_cache["running"]:
        return _proxy_test_cache
    _proxy_test_cache["running"] = True
    try:
        items = pool.load_pool()
        sem = asyncio.Semaphore(12)

        async def guarded(p):
            async with sem:
                return await _test_one_proxy(p)

        results = await asyncio.gather(*[guarded(p) for p in items]) if items else []
        _proxy_test_cache["results"] = {r["key"]: r for r in results}
        _proxy_test_cache["tested_at"] = time.time()
    finally:
        _proxy_test_cache["running"] = False
    return _proxy_test_cache


@app.get("/proxies/test")
async def proxies_test(force: bool = False):
    """Status de conectividade dos proxies. Usa cache (<5 min) salvo se force=true."""
    fresh = (_proxy_test_cache["tested_at"]
             and (time.time() - _proxy_test_cache["tested_at"]) < 300)
    if not force and fresh:
        results = list(_proxy_test_cache["results"].values())
    else:
        await test_pool_proxies()
        results = list(_proxy_test_cache["results"].values())
    ok = sum(1 for r in results if r["ok"])
    return {
        "tested_at": _proxy_test_cache["tested_at"],
        "running": _proxy_test_cache["running"],
        "ok": ok,
        "down": len(results) - ok,
        "total": len(results),
        "results": results,
    }


def _proxy_status_block(pk: str, host: str, summary_items=None, outcomes=None) -> dict:
    """Monta {level, health, country, status} de um proxy a partir de pool.summary()
    + desfechos reais (proxy_outcomes) + reachability (cache, só leitura) + país.
    `health`: limpo/sujo/queimado/sem-dados (mesma regra de /proxies/health) — é o
    LEMBRETE de proxy sujo. `summary_items`/`outcomes` podem ser pré-computados para
    evitar re-scan quando avaliando vários proxies."""
    if summary_items is None:
        try:
            summary_items = pool.summary().get("items", [])
        except Exception:
            summary_items = []
    st = next((i for i in summary_items if i.get("key") == pk), {}) or {}
    if outcomes is None:
        try:
            outcomes = acc.proxy_outcomes()
        except Exception:
            outcomes = {}
    out_d = outcomes.get(pk, {}) or {}
    reach = (_proxy_test_cache.get("results") or {}).get(pk)
    reachable = reach.get("ok") if reach else None
    try:
        country = pool.proxy_country(host) if host else ""
    except Exception:
        country = ""
    ban = st.get("ban_rate_pct")
    blocked = bool(st.get("blocked"))
    health = _derive_proxy_health(out_d, 0, reachable)
    # Vermelho (atenção) se: proxy não responde, no limite, ban alto, ou sujo/queimado.
    warn = (blocked or (reachable is False)
            or (isinstance(ban, (int, float)) and ban >= 20)
            or health in ("dirty", "banned"))
    return {
        "level": "warn" if warn else "ok",
        "health": health,
        "country": country or None,
        "status": {
            "used": st.get("used"),
            "ban_rate_pct": ban,
            "blocked": blocked,
            "reachable": reachable,
            "recommended_remaining": st.get("recommended_remaining"),
            "accounts": out_d,
        },
    }


@app.get("/proxies/active")
def proxies_active():
    """Proxy ATUAL + saúde para o indicador do topo (LEMBRETE de proxy sujo),
    em ordem de prioridade:
      1) criação em andamento -> proxy da conta sendo criada (mode=creating);
      2) sessão aberta no PC   -> proxy do perfil aberto agora (mode=in_use);
      3) parado, 1 proxy no pool -> mostra ELE + saúde (mode=pool_one);
      4) parado, vários proxies  -> resume pelo PIOR (mode=pool_many, dirty_count);
      5) pool vazio              -> mode=empty.
    NUNCA mostra "próximo" do assign() — mostra o proxy REAL em uso ou o que você TEM
    no pool, sempre com `health` (limpo/sujo/queimado) pra ver se o IP está limpo.

    Reaproveita: _ig_batch_jobs + sm.list_active() + pool.summary() +
    proxy_outcomes() + proxy_country() + cache de reachability (só LEITURA).
    """
    # 1) Criação em andamento — item sendo criado no job em execução mais recente.
    cur = None
    for j in sorted(_ig_batch_jobs.values(), key=lambda x: x.get("started_at", 0), reverse=True):
        if j.get("status") != "running":
            continue
        for it in j.get("items", []):
            if it.get("status") == "creating":
                cur = it
                break
        if cur:
            break
    if cur:
        pk = cur.get("proxy_key")
        host = cur.get("proxy_host")
        if not pk:  # conexão local (sem proxy) = tethering do celular
            return {"mode": "creating", "level": "ok", "local": True,
                    "email": cur.get("email"), "username": cur.get("username"),
                    "proxy_key": None, "host": None, "country": None, "status": {}}
        return {"mode": "creating", "local": False,
                "email": cur.get("email"), "username": cur.get("username"),
                "proxy_key": pk, "host": host, **_proxy_status_block(pk, host)}

    # 2) Sessão aberta no PC — proxy do perfil que você está operando agora.
    try:
        sessions = sm.list_active()
    except Exception:
        sessions = []
    for s in sessions:
        prof = get_profile(s.get("profile_id"))
        px = (prof or {}).get("proxy") or {}
        if px.get("host"):
            pk = pool.proxy_key(px)
            return {"mode": "in_use", "local": False, "proxy_key": pk, "host": px["host"],
                    "profile_id": s.get("profile_id"), **_proxy_status_block(pk, px["host"])}

    # 3) Nada em uso AGORA → LEMBRETE: mostra o(s) proxy(s) do pool e a saúde deles,
    #    pra você sempre ver se seu IP está limpo ou sujo (não é "próximo", é o que
    #    você TEM). 1 proxy → mostra ele; vários → resume pelo PIOR (lembrete de sujo).
    try:
        summary_items = pool.summary().get("items", [])
    except Exception:
        summary_items = []
    if not summary_items:
        return {"mode": "empty", "level": "idle"}

    try:
        outcomes = acc.proxy_outcomes()
    except Exception:
        outcomes = {}

    if len(summary_items) == 1:
        k = summary_items[0].get("key")
        host = k.split(":")[0] if k else ""
        return {"mode": "pool_one", "local": False, "proxy_key": k, "host": host,
                **_proxy_status_block(k, host, summary_items, outcomes)}

    # Vários proxies: avalia todos (sem re-scan) e resume pelo pior.
    rank = {"banned": 0, "dirty": 1, "unknown": 2, "clean": 3}
    blocks = []
    for it in summary_items:
        k = it.get("key")
        host = k.split(":")[0] if k else ""
        blocks.append((k, host, _proxy_status_block(k, host, summary_items, outcomes)))
    blocks.sort(key=lambda x: rank.get(x[2].get("health"), 9))
    wk, whost, wb = blocks[0]
    dirty_n = sum(1 for _, _, b in blocks if b.get("health") in ("dirty", "banned"))
    return {"mode": "pool_many", "local": False, "proxy_key": wk, "host": whost,
            "count": len(blocks), "dirty_count": dirty_n, **wb}


def _derive_proxy_health(accounts_d: dict, attempts_total: int, reachable) -> str:
    """Rotula a saude do proxy por REGRA (sem IA, sem coluna nova).
    Combina desfechos reais das contas (proxy_outcomes) com volume de tentativas.
      - unknown: nunca usado (sem conta nem tentativa).
      - banned:  queimado (bad_rate alto com volume) — evitar.
      - dirty:   sinais ruins (bad_rate medio) — atencao.
      - clean:   sem sinais ruins relevantes.
    'reachable' nao muda o rotulo de saude (e separado), mas a UI usa os dois.
    """
    total = accounts_d.get("total", 0)
    bad_rate = accounts_d.get("bad_rate", 0.0)
    if total == 0 and attempts_total == 0:
        return "unknown"
    if total >= 3 and bad_rate >= 60:
        return "banned"
    if total >= 1 and bad_rate >= 30:
        return "dirty"
    return "clean"


@app.get("/proxies/health")
def proxies_health():
    """Saude por proxy (regua do teste 4G/5G): junta uso/ban do pool + desfechos
    reais das contas (proxy_outcomes) + funil de criacao (creation_attempts) +
    reachability (cache) + pais (offline). NAO dispara teste de proxy.

    Reaproveita tudo o que ja existe; nenhuma tabela/coluna nova. Rotulo de saude
    derivado por regra em _derive_proxy_health.
    """
    summ = pool.summary()
    by_outcome = acc.proxy_outcomes()              # {key: {total,banned,suspended,checkpoint,discarded,bad,bad_rate}}
    reach = _proxy_test_cache.get("results") or {}

    # Funil de criacao por proxy (gates: success/phone/captcha/email_pending/...).
    funnel: dict = {}
    try:
        for r in db.list_creation_attempts(5000):
            pk = r.get("proxy_key") or "(sem proxy)"
            g = r.get("gate") or "?"
            d = funnel.setdefault(pk, {"total": 0, "success": 0, "by_gate": {}})
            d["total"] += 1
            d["by_gate"][g] = d["by_gate"].get(g, 0) + 1
            if g == "success":
                d["success"] += 1
    except Exception:
        funnel = {}

    # Chaves: pool + quaisquer que apareceram em contas/tentativas (historicas).
    keys = [it["key"] for it in summ.get("items", [])]
    pool_keys = set(keys)
    for extra in list(by_outcome.keys()) + list(funnel.keys()):
        if extra and extra != "(sem proxy)" and extra not in pool_keys:
            keys.append(extra)

    pool_item = {it["key"]: it for it in summ.get("items", [])}
    items = []
    for k in keys:
        host = k.split(":")[0] if k and ":" in k else k
        out_d = by_outcome.get(k, {"total": 0, "banned": 0, "suspended": 0,
                                   "checkpoint": 0, "discarded": 0, "bad": 0, "bad_rate": 0.0})
        fn = funnel.get(k, {"total": 0, "success": 0, "by_gate": {}})
        pit = pool_item.get(k, {})
        r = reach.get(k)
        reachable = r.get("ok") if r else None
        try:
            country = pool.proxy_country(host) if host else ""
        except Exception:
            country = ""

        health = _derive_proxy_health(out_d, fn.get("total", 0), reachable)
        ban_rate = pit.get("ban_rate_pct")
        warn = (health in ("dirty", "banned") or reachable is False or bool(pit.get("blocked"))
                or (isinstance(ban_rate, (int, float)) and ban_rate >= 20))

        items.append({
            "proxy_key": k,
            "host": host,
            "country": country or None,
            "in_pool": k in pool_keys,
            "reachable": reachable,
            "health": health,
            "level": "warn" if warn else "ok",
            "used": pit.get("used", 0),
            "ban_rate_pct": ban_rate,
            "blocked": bool(pit.get("blocked")),
            "recommended_remaining": pit.get("recommended_remaining"),
            "accounts": out_d,
            "attempts": {
                "total": fn.get("total", 0),
                "success": fn.get("success", 0),
                "success_rate_pct": (round(fn["success"] / fn["total"] * 100, 1)
                                     if fn.get("total") else 0.0),
                "by_gate": fn.get("by_gate", {}),
            },
        })

    # Pior primeiro: banned > dirty > clean > unknown; depois por bad_rate.
    rank = {"banned": 0, "dirty": 1, "clean": 2, "unknown": 3}
    items.sort(key=lambda x: (rank.get(x["health"], 9), -x["accounts"].get("bad_rate", 0)))
    return {"count": len(items), "items": items}


@app.get("/whatsapp/usage")
def whatsapp_usage():
    return acc.whatsapp_usage_summary()


@app.get("/limits")
def limits_get():
    return acc.get_limits()


@app.post("/limits")
def limits_set(body: LimitsRequest):
    return acc.set_limits(proxy_limit=body.proxy_limit, whatsapp_limit=body.whatsapp_limit,
                          target_country=body.target_country, auto_recheck_hours=body.auto_recheck_hours)


# ── Métricas / Ban Analytics ──────────────────────────────────────────────────
@app.get("/analytics/bans")
def get_ban_analytics():
    """Métricas agregadas de bans, sobrevivência e performance por proxy."""
    return acc.get_ban_analytics()


@app.get("/analytics/proxy-recommendations")
def get_proxy_recommendations():
    """Recomendações inteligentes por proxy baseadas em ban rate real (para usar o máximo possível)."""
    return acc.get_proxy_recommendations()


# ─── Importação CSV ─────────────────────────────────────────────────────────

class ImportValidateRequest(BaseModel):
    rows: list[dict]


class ImportStartRequest(BaseModel):
    rows: list[dict]
    create_accounts: bool = True
    delay_seconds: float = 30
    warm_profile_minutes: float = 0
    sms_activate_key: Optional[str] = None
    sms_country: int = 73
    use_proxy_pool: bool = False   # usa proxies da pasta proxys/ quando a linha não tem proxy


@app.post("/import/validate")
def import_validate(body: ImportValidateRequest):
    rows = [bi.normalize_row(r) for r in body.rows]
    result = bi.validate_rows(rows)
    return {"rows": rows, **result}


@app.post("/import/start")
async def import_start(body: ImportStartRequest):
    rows = [bi.normalize_row(r) for r in body.rows]
    if body.create_accounts:
        rows = [r for r in rows if r.get("full_name") and r.get("email") and r.get("password")]
    if not rows:
        raise HTTPException(400, "Nenhuma linha válida para importar (verifique nome/email/senha)")
    job_id = bi.start_job(rows, body.model_dump(), time.time())
    return {"job_id": job_id, "queued": len(rows)}


@app.get("/import/status/{job_id}")
def import_status(job_id: str):
    j = bi.job_status(job_id)
    if not j:
        raise HTTPException(404, "Job de importação não encontrado")
    return j


@app.get("/import/jobs")
def import_jobs():
    return bi.list_jobs()


# ─── Emails (catch-all + IMAP) ──────────────────────────────────────────────

class EmailConfigRequest(BaseModel):
    email_domain: Optional[str] = None
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    imap_user: Optional[str] = None
    imap_pass: Optional[str] = None


class EmailGenerateRequest(BaseModel):
    count: int = 1
    base: Optional[str] = None
    with_profile: bool = True
    proxy: Optional[ProxyModel] = None
    use_proxy_pool: bool = False   # distribui proxies do pool (pasta proxys/)
    os: str = "random"


@app.get("/email/config")
def email_config_get():
    # app local: devolve a senha para o campo manter o valor (facilita conferência)
    return eb.get_config(reveal=True)


@app.post("/email/config")
def email_config_set(body: EmailConfigRequest):
    return eb.set_config(**body.model_dump())


@app.post("/email/test-imap")
def email_test_imap():
    return eb.test_imap()


@app.get("/email/inbox")
async def email_inbox(limit: int = 10):
    """Últimas mensagens da caixa catch-all (testar recebimento + extração de código)."""
    try:
        return {"messages": await eb.list_recent(limit)}
    except Exception as e:
        raise HTTPException(400, f"Erro ao ler a caixa: {e}")


@app.get("/email/list")
def email_list():
    items = eb.list_emails()
    # anexa o nome do perfil vinculado
    for it in items:
        if it.get("profile_id"):
            p = get_profile(it["profile_id"])
            it["profile_name"] = p["name"] if p else None
            it["profile_exists"] = bool(p)
        else:
            it["profile_name"] = None
            it["profile_exists"] = False
    return items


@app.post("/email/generate", status_code=201)
def email_generate(body: EmailGenerateRequest):
    if not eb.get_config().get("email_domain"):
        raise HTTPException(400, "Configure o domínio de email primeiro")
    n = max(1, min(body.count, 100))
    manual_proxy = body.proxy.model_dump() if body.proxy else None
    # Pool: distribui 1 proxy por perfil (menos usado primeiro, respeitando o limite)
    pool_proxies = pool.assign(n, acc.get_limits()["proxy_limit"]) if body.use_proxy_pool else None

    created = []
    short_of_proxy = 0
    for i in range(n):
        proxy = pool_proxies[i] if pool_proxies is not None else manual_proxy
        if body.use_proxy_pool and proxy is None:
            short_of_proxy += 1
        profile_id = None
        profile_name = None
        if body.with_profile:
            p = prof.create(name=f"{body.base or 'email'}-{i+1}", os=body.os, proxy=proxy)
            profile_id = p["id"]
            profile_name = p["name"]
        em = eb.create_email(base=body.base or "", profile_id=profile_id)
        em["profile_name"] = profile_name
        created.append(em)
    out = {"created": created, "count": len(created)}
    if short_of_proxy:
        out["warning"] = (f"{short_of_proxy} perfil(is) ficaram SEM proxy: o pool "
                          f"atingiu o limite de contas por proxy. Adicione mais proxies "
                          f"ou aumente o limite.")
    return out


class EmailCreateAccountRequest(BaseModel):
    sms_activate_key: Optional[str] = None
    sms_country: int = 73


class EmailBatchCreateRequest(BaseModel):
    sms_activate_key: Optional[str] = None
    sms_country: int = 73
    delay_seconds: float = 90


def _derive_gate(status: str, step: str) -> str:
    """Categoriza ONDE a criação parou/terminou — sinal de aprendizado."""
    s = (status or "").lower()
    st = (step or "").lower()
    if s in ("logged_in", "created", "active"):
        return "success"
    if s == "flagged_phone" or "phone" in st:
        return "phone"
    if "captcha" in s or "captcha" in st:
        return "captcha"
    if s in ("waiting_for_email_code", "waiting_for_verification") or "email_code" in st or "verify" in st:
        return "email_pending"
    if "submit" in st:
        return "submit_failed"
    if s == "error":
        return "error"
    return s or "unknown"


async def _create_account_for_email(em: dict, full_name: str, username: str,
                                     password: str, sms_key, sms_country,
                                     warmup_minutes: float = 0, from_pool: bool = False):
    """Abre o perfil vinculado ao email e cria a conta usando esse email.
    - Mantém a janela do navegador ABERTA até a criação realmente terminar com login no Instagram
      (create_account agora espera a transição para o feed após o código).
    - Em caso de sucesso (logado), inicia o AQUECIMENTO em background (asyncio task) para não
      bloquear o lote e deixar a atividade rodar na mesma sessão do browser.
    - Só fecha a janela em caso de erro duro ou cancelamento explícito.
    - Marca o email como 'used' quando há uma conta associada.
    """
    page = await _ensure_session_with_page(
        em["profile_id"], "https://www.instagram.com/accounts/emailsignup/")
    if not page:
        # Levanta para que o chamador (lote/CSV) registre como erro em vez de silenciar
        raise RuntimeError("não foi possível abrir o navegador (proxy/lock/Camoufox?)")
    data = {
        "email": em["address"],
        "password": password,
        "full_name": full_name,
        "username": username,
        "birthday": ig.gen_birthday(),
        "sms_activate_key": sms_key,
        "sms_country": sms_country,
    }
    flow = None
    try:
        flow = await ig.create_account(page, em["profile_id"], data)
    finally:
        a = get_account_by_profile(em["profile_id"])
        if a:
            # Só marca como usado quando temos uma conta real associada
            eb.mark_used(em["id"], a["id"])

        final_status = (flow or ig.flow_status(em["profile_id"]) or {}).get("status", "")
        if a:
            final_status = final_status or a.get("status", "")

        # ── Aprendizado: registra a tentativa (condições + onde parou + desfecho) ──
        try:
            flow_d = ig.flow_status(em["profile_id"]) or {}
            prof_row = get_profile(em["profile_id"])
            fp = (prof_row or {}).get("fingerprint") or {}
            pk = (a or {}).get("proxy_key")
            if not pk:
                # Mesma funcao de chave do pool/contas -> join consistente em
                # /proxies/health, proxy_outcomes e learning/summary.
                pk = pool.proxy_key((prof_row or {}).get("proxy") or {}) or None
            db.record_creation_attempt({
                "id": uuid.uuid4().hex,
                "profile_id": em.get("profile_id"),
                "account_id": (a or {}).get("id"),
                "email": em.get("address"),
                "username": (a or {}).get("username") or username,
                "proxy_key": pk,
                "os": fp.get("platform"),
                "warmup_minutes": warmup_minutes,
                "from_pool": from_pool,
                "outcome": final_status,
                "gate": _derive_gate(final_status, flow_d.get("step")),
                "step": flow_d.get("step"),
                "detail": flow_d.get("error") or flow_d.get("message") or "",
            })
        except Exception:
            logger.exception("falha ao registrar tentativa de criação (aprendizado)")

        if final_status in ("created", "active", "logged_in"):
            # Sucesso: conta logada. Deixa a janela aberta e dispara aquecimento em BACKGROUND.
            # O worker do lote pode seguir para a próxima conta imediatamente.
            try:
                warmup_mins = 6
                asyncio.create_task(ig.warmup(page, em["profile_id"], warmup_mins))
                logger.info(f"[{em['profile_id']}] criação finalizada com login — aquecimento em background iniciado (janela mantida)")
            except Exception:
                logger.exception("falha ao agendar warmup em background")
            # NÃO chamamos sm.stop aqui — só feche quando realmente logado (e o usuário/UI decide depois)
        elif final_status == "cancelled":
            # Cancelado pelo usuário: fecha para liberar o perfil/lock.
            try:
                await sm.stop(em["profile_id"], reason="create_account_cancelled")
            except Exception:
                pass
        elif final_status == "flagged_phone":
            # Conta sinalizada (IG exigiu telefone/WhatsApp) — descartada. Fecha a janela para
            # liberar proxy/lock e não acumular janelas no lote. Não tentamos verificação por telefone.
            try:
                await sm.stop(em["profile_id"], reason="flagged_phone_discarded")
            except Exception:
                pass
            logger.warning(f"[{em['profile_id']}] conta sinalizada (telefone/WhatsApp) — descartada e janela fechada")
        elif final_status == "error":
            # Erro de cadastro: mantém a janela aberta para inspeção manual.
            # Fechar aqui escondia exatamente a tela que precisamos diagnosticar.
            logger.warning(f"[{em['profile_id']}] criação terminou com erro; janela mantida aberta para diagnóstico")
        else:
            # Ainda em waiting_verification, código manual necessário, etc.
            # Mantemos a janela ABERTA exatamente como o usuário pediu (para intervenção manual se necessário).
            logger.info(f"[{em['profile_id']}] criação retornou em estado intermediário ({final_status}) — janela deixada aberta para o usuário")
            # Não paramos o perfil.


@app.post("/email/{email_id}/create-account")
async def email_create_account(email_id: str, body: EmailCreateAccountRequest = EmailCreateAccountRequest()):
    em = eb.get_email(email_id)
    if not em:
        raise HTTPException(404, "Email não encontrado")
    if not em.get("profile_id") or not get_profile(em["profile_id"]):
        raise HTTPException(409, "Email sem perfil vinculado — gere o email com a opção de perfil")
    email_addr = em["address"]
    base = email_addr.split('@')[0].split('+')[0]
    full_name = ig.gen_full_name(base=base)
    username = ig._gen_username(full_name)
    password = ig.gen_password()
    asyncio.create_task(_create_account_for_email(
        em, full_name, username, password, body.sms_activate_key, body.sms_country))
    return {"status": "started", "email": em["address"],
            "full_name": full_name, "username": username, "password": password}


@app.post("/email/create-accounts-free")
async def email_create_accounts_free(body: EmailBatchCreateRequest = EmailBatchCreateRequest()):
    # Considera "pronto para uso" qualquer email que:
    # - tenha perfil associado
    # - ainda não tenha uma conta IG criada (account_id é nulo)
    free = [
        e for e in eb.list_emails()
        if e.get("profile_id")
        and get_profile(e["profile_id"])
        and not e.get("account_id")   # só emails sem conta vinculada
    ]
    if not free:
        raise HTTPException(400, "Nenhum email pronto para uso com perfil vinculado")

    global _creation_stop
    _creation_stop = False

    async def worker():
        for em in free:
            if _creation_stop:
                logger.info("Criação de contas em lote interrompida pelo usuário")
                break
            try:
                email_addr = em["address"]
                base = email_addr.split('@')[0].split('+')[0]
                fn = ig.gen_full_name(base=base)
                un = ig._gen_username(fn)
                pw = ig.gen_password()
                await _create_account_for_email(em, fn, un, pw, body.sms_activate_key, body.sms_country)
            except Exception:
                pass
            await asyncio.sleep(body.delay_seconds)

    asyncio.create_task(worker())
    return {"status": "started", "count": len(free)}


@app.post("/creations/stop")
async def stop_creations():
    """Para TODAS as criações em andamento (lotes, individuais e import) na hora."""
    global _creation_stop, _ig_creation_tasks
    _creation_stop = True
    try:
        ig.request_creation_stop()
    except Exception:
        pass

    # 1) Cancela as tarefas registradas (workers de lote + criações individuais).
    for key, task in list(_ig_creation_tasks.items()):
        if not task.done():
            task.cancel()
        _ig_creation_tasks.pop(key, None)

    # 2) Fecha TODAS as sessões de navegador ativas. É o que interrompe o Playwright
    #    (preenchimento, waits, aquecimento) imediatamente e libera os locks.
    for pid in list(sm._sessions.keys()):
        try:
            await sm.stop(pid, reason="stop_creations_endpoint")
        except Exception:
            pass

    # 3) Marca os jobs de lote em andamento como parados.
    for j in _ig_batch_jobs.values():
        if j.get("status") == "running":
            j["status"] = "stopped"
            j["current"] = None
            j["eta_seconds"] = 0

    return {"status": "stopped"}


@app.delete("/email/{email_id}", status_code=204)
def email_delete(email_id: str):
    if not any(e["id"] == email_id for e in eb.list_emails()):
        raise HTTPException(404, "Email não encontrado")
    eb.delete_email(email_id)


@app.post("/email/fetch-code")
async def email_fetch_code(address: str):
    code = await eb.fetch_code(address, timeout=60, poll=5)
    if not code:
        raise HTTPException(404, "Nenhum código encontrado (verifique o IMAP/catch-all)")
    return {"address": address, "code": code}


# ─── WhatsApp (números cadastrados manualmente + controle de uso) ─────────────

import datetime as _dt


class WhatsappAddRequest(BaseModel):
    numbers: str = ""          # um por linha (ou separados por vírgula/; ) — aceita lote
    notes: Optional[str] = None


class WhatsappUpdateRequest(BaseModel):
    status: Optional[str] = None       # 'free' | 'used'
    notes: Optional[str] = None
    account_id: Optional[str] = None


def _wa_summary(items: list) -> dict:
    free = sum(1 for w in items if (w.get("status") or "free") == "free")
    used = sum(1 for w in items if (w.get("status") or "free") == "used")
    return {"total": len(items), "free": free, "used": used}


@app.get("/whatsapps")
def whatsapps_list():
    items = db.list_whatsapps()
    return {"items": items, "summary": _wa_summary(items)}


@app.post("/whatsapps", status_code=201)
def whatsapps_add(body: WhatsappAddRequest):
    # Aceita vários números de uma vez (quebra por linha, vírgula ou ponto-e-vírgula).
    raw = re.split(r"[\n,;]+", body.numbers or "")
    seen_existing = {w["number"] for w in db.list_whatsapps()}
    added, skipped = [], []
    for token in raw:
        num = token.strip()
        if not num:
            continue
        if num in seen_existing:
            skipped.append(num)
            continue
        wa = {"id": str(uuid.uuid4()), "number": num, "status": "free", "notes": body.notes}
        db.save_whatsapp(wa)
        seen_existing.add(num)
        added.append(num)
    if not added and not skipped:
        raise HTTPException(400, "Nenhum número informado.")
    items = db.list_whatsapps()
    return {"added": added, "skipped_duplicates": skipped, "summary": _wa_summary(items)}


@app.patch("/whatsapps/{wa_id}")
def whatsapps_update(wa_id: str, body: WhatsappUpdateRequest):
    wa = db.get_whatsapp(wa_id)
    if not wa:
        raise HTTPException(404, "Número não encontrado")
    fields = {}
    if body.status is not None:
        if body.status not in ("free", "used"):
            raise HTTPException(400, "status deve ser 'free' ou 'used'")
        fields["status"] = body.status
        # marca/limpa o carimbo de uso
        fields["used_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S") if body.status == "used" else None
    if body.notes is not None:
        fields["notes"] = body.notes
    if body.account_id is not None:
        fields["account_id"] = body.account_id
    db.update_whatsapp(wa_id, **fields)
    return db.get_whatsapp(wa_id)


@app.delete("/whatsapps/{wa_id}", status_code=204)
def whatsapps_delete(wa_id: str):
    if not db.get_whatsapp(wa_id):
        raise HTTPException(404, "Número não encontrado")
    db.delete_whatsapp(wa_id)


# ─── Emails (registro manual dos emails das contas + controle de uso) ─────────
# Substitui o antigo fluxo de catch-all/IMAP (criação automática desativada).
# A aba Emails agora é só um registro: guardar os emails das contas, livres ou usados.

class EmailAddRequest(BaseModel):
    addresses: str = ""        # um por linha / vírgula / ; / espaço — aceita lote
    notes: Optional[str] = None


class EmailRegistryUpdateRequest(BaseModel):
    status: Optional[str] = None       # 'free' | 'used'
    notes: Optional[str] = None
    account_id: Optional[str] = None


def _email_summary(items: list) -> dict:
    free = sum(1 for e in items if (e.get("status") or "free") == "free")
    used = sum(1 for e in items if (e.get("status") or "free") == "used")
    return {"total": len(items), "free": free, "used": used}


@app.get("/emails")
def emails_registry_list():
    items = db.list_emails()
    accs = {a["id"]: a for a in acc.list_accounts()}
    for e in items:
        a = accs.get(e.get("account_id"))
        e["account_username"] = (a.get("username") if a else None)
    return {"items": items, "summary": _email_summary(items)}


@app.post("/emails", status_code=201)
def emails_registry_add(body: EmailAddRequest):
    raw = re.split(r"[\n,;\s]+", body.addresses or "")
    existing = {(e.get("address") or "").lower() for e in db.list_emails()}
    added, skipped = [], []
    for token in raw:
        addr = token.strip()
        if not addr:
            continue
        if addr.lower() in existing:
            skipped.append(addr)
            continue
        db.save_email({"id": str(uuid.uuid4()), "address": addr, "status": "free", "notes": body.notes})
        existing.add(addr.lower())
        added.append(addr)
    if not added and not skipped:
        raise HTTPException(400, "Nenhum email informado.")
    items = db.list_emails()
    return {"added": added, "skipped_duplicates": skipped, "summary": _email_summary(items)}


@app.patch("/emails/{email_id}")
def emails_registry_update(email_id: str, body: EmailRegistryUpdateRequest):
    if not db.get_email(email_id):
        raise HTTPException(404, "Email não encontrado")
    fields = {}
    if body.status is not None:
        if body.status not in ("free", "used"):
            raise HTTPException(400, "status deve ser 'free' ou 'used'")
        fields["status"] = body.status
        if body.status == "free":
            fields["account_id"] = None   # liberar desvincula da conta
    if body.notes is not None:
        fields["notes"] = body.notes
    if body.account_id is not None:
        fields["account_id"] = body.account_id
    db.update_email(email_id, **fields)
    return db.get_email(email_id)


@app.delete("/emails/{email_id}", status_code=204)
def emails_registry_delete(email_id: str):
    if not db.get_email(email_id):
        raise HTTPException(404, "Email não encontrado")
    db.delete_email(email_id)


# ─── Pool de perfis pré-aquecidos ─────────────────────────────────────────────

@app.get("/warm-pool/status")
def warm_pool_status():
    """Quantos perfis estão prontos/aquecendo/em uso no pool."""
    return warm_pool.status()


# ─── Aprendizado: tentativas de criação registradas ──────────────────────────

@app.get("/learning/attempts")
def learning_attempts(limit: int = 200):
    """Histórico bruto das tentativas de criação (condições + onde parou + desfecho)."""
    return {"items": db.list_creation_attempts(limit)}


@app.get("/learning/summary")
def learning_summary():
    """Agregados do que o app vem registrando — base para aprender com o tempo."""
    rows = db.list_creation_attempts(5000)
    total = len(rows)
    by_gate, by_outcome, by_proxy = {}, {}, {}
    for r in rows:
        g = r.get("gate") or "?"
        by_gate[g] = by_gate.get(g, 0) + 1
        o = r.get("outcome") or "?"
        by_outcome[o] = by_outcome.get(o, 0) + 1
        pk = r.get("proxy_key") or "(sem proxy)"
        d = by_proxy.setdefault(pk, {"total": 0, "success": 0})
        d["total"] += 1
        if g == "success":
            d["success"] += 1
    success = by_gate.get("success", 0)
    return {
        "total": total,
        "success": success,
        "success_rate_pct": round(success / total * 100, 1) if total else 0.0,
        "by_gate": by_gate,
        "by_outcome": by_outcome,
        "by_proxy": by_proxy,
    }
