"""
API REST — iniciar com:
  uvicorn api:app --reload --port 8000
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
from pathlib import Path
import asyncio
import copy
import json
import os
import random
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
import backup_data as bdata
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
    try:
        # Spider: runs que ficaram 'running' após um reinício/crash → 'stopped'
        # (o checkpoint já gravou linhas + cursor; assim a UI libera o "continuar").
        n = db.recover_orphan_spider_runs()
        if n:
            print(f"DEBUG: {n} extração(ões) do Spider órfã(s) marcada(s) como 'stopped'")
    except Exception:
        logger.exception("Falha ao recuperar runs órfãs do Spider")


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


# Aquecimento de NAVEGADOR em andamento (em memória) — p/ a UI mostrar a contagem.
# {profile_id: epoch_de_termino}
_profile_warming: dict[str, float] = {}


def _profile_warming_status(profile_id: str) -> dict:
    end = _profile_warming.get(profile_id)
    if not end:
        return {"active": False}
    remaining = end - time.time()
    if remaining <= 0:
        _profile_warming.pop(profile_id, None)
        return {"active": False}
    return {"active": True, "remaining_seconds": int(remaining)}


@app.post("/profiles/{profile_id}/warmup-profile")
async def warmup_profile_endpoint(profile_id: str, body: WarmupProfileRequest = WarmupProfileRequest()):
    """Aquece o PERFIL: navega por sites comuns acumulando cookies/histórico."""
    p = get_profile(profile_id)
    if not p:
        raise HTTPException(404, "Perfil não encontrado")
    import profile_warmup as pw
    locale = p.get("fingerprint", {}).get("language", "")
    _profile_warming[profile_id] = time.time() + max(0.0, float(body.minutes or 0)) * 60

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
        finally:
            _profile_warming.pop(profile_id, None)

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


@app.get("/sessions/activity")
def sessions_activity():
    """Atividade do app: janelas interativas (sm.list_active) + tarefas de FUNDO
    que abrem um Camoufox headless e NÃO contam como 'sessão' (aquecimento de
    navegador e jobs do Spider rodando). Dá visibilidade do que está rodando
    mesmo sem janela aberta na tela."""
    sessions = []
    for s in sm.list_active():
        pr = get_profile(s.get("profile_id"))
        sessions.append({**s, "name": (pr or {}).get("name") or s.get("profile_id")})
    warming = []
    for pid in list(_profile_warming.keys()):
        st = _profile_warming_status(pid)
        if not st.get("active"):
            continue
        pr = get_profile(pid)
        warming.append({"profile_id": pid, "name": (pr or {}).get("name") or pid,
                        "remaining_seconds": st.get("remaining_seconds", 0)})
    spider_jobs = []
    for j in _spider_jobs.values():
        if j.get("status") in ("running", "needs_code"):
            spider_jobs.append({"id": j.get("id"), "target": j.get("target"),
                                "kind": j.get("kind"), "status": j.get("status"),
                                "profile": j.get("profile"), "count": j.get("count", 0),
                                "total": j.get("total")})
    return {"sessions": sessions, "warming": warming, "spider": spider_jobs}


@app.post("/sessions/kill-orphans")
async def kill_orphans():
    """Destrava: fecha as sessões rastreadas (libera os locks do app) e mata
    processos Camoufox órfãos que seguram o user_data_dir ("Firefox is already
    running, but is not responding"). Multiplataforma. Use quando "Abrir"
    reclamar de processo travado de uma execução/crash anterior."""
    import subprocess
    import sys
    tracked = len(sm.list_active())
    # 1) Fecha de forma limpa as sessões rastreadas (libera DB locks + __aexit__).
    try:
        await sm.shutdown_all()
    except Exception:
        pass
    # 2) Mata processos Camoufox que sobraram (órfãos). Só "camoufox" — não toca
    #    no Firefox normal do usuário.
    killed = 0
    try:
        if sys.platform.startswith("win"):
            r = subprocess.run(["taskkill", "/F", "/IM", "camoufox.exe"],
                               capture_output=True, text=True)
            killed = r.stdout.count("SUCCESS")
        else:
            r = subprocess.run(["pkill", "-if", "camoufox"],
                               capture_output=True, text=True)
            killed = 1 if r.returncode == 0 else 0
    except Exception as e:
        raise HTTPException(500, f"Falha ao encerrar processos: {e}")
    return {"tracked_closed": tracked, "processes_killed": killed}


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

    # E-mail: SUGERE um e-mail comprado já cadastrado (livre) e o liga ao perfil.
    # Não gera mais catch-all. Se não houver e-mail livre, segue sem e-mail (você
    # digita o seu na janela) e a UI avisa para cadastrar na aba Emails.
    em = eb.claim_free_email(profile_id)
    email_addr = em["address"] if em else ""

    # 2) Gera os dados (mesmos geradores do fluxo automático)
    base = email_addr.split("@")[0].split("+")[0] if email_addr else (body.base or "")
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
        "email": email_addr,          # sugestão (e-mail comprado livre) ou "" se não houver
        "email_suggested": bool(email_addr),
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
    rows = acc.list_accounts()
    # Anexa o estado do aquecimento do NAVEGADOR (contagem regressiva) p/ a UI.
    for a in rows:
        pid = a.get("profile_id")
        if pid:
            st = _profile_warming_status(pid)
            if st.get("active"):
                a["profile_warming"] = st
    return rows


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
    profile_id: Optional[str] = None  # usado no modo web (1 conta)
    profile_ids: Optional[list] = None  # modo web RODÍZIO: vários perfis revezando no mesmo alvo
    account_id: Optional[str] = None  # usado no modo app (precisa usuário+senha)
    use_local: bool = False           # ignora o proxy do perfil (sai pelo IP local). No rodízio: True=todas no MESMO IP local; False=cada conta pelo SEU proxy
    kind: str = "followers"
    target: str
    limit: int = 0
    delay_ms: int = 2500              # velocidade: delay base entre páginas (ms)


def _spider_pool_from_run(run: dict) -> list:
    """Reconstrói o pool de perfis de uma execução salva. Usa profile_ids (rodízio)
    se houver; senão cai no profile_id único. Pula perfis que não existem mais."""
    try:
        pids = json.loads(run.get("profile_ids") or "[]")
    except Exception:
        pids = []
    if not pids and run.get("profile_id"):
        pids = [run.get("profile_id")]
    return [pr for pr in (get_profile(x) for x in pids) if pr]


def _spider_mark_blocked(profile=None, account=None,
                         reason="Spider: possível bloqueio/desafio na extração"):
    """O Spider bateu em bloqueio/desafio (não 429/rede) → normalmente a conta foi
    suspensa. Marca a conta (do perfil, no Web; ou a conta, no App) como 'checkpoint'
    = 'Em suspensão' → entra no contador de suspensões e nas métricas."""
    try:
        a = account
        if a is None and profile:
            a = db.get_account_by_profile(profile.get("id"))
        if a and a.get("id"):
            acc.set_status(a["id"], "checkpoint", reason)
            return a.get("username") or a.get("id")
    except Exception:
        logger.exception("spider: falha ao marcar conta como suspensa")
    return None


_SPIDER_SPEED_TIERS = (2000, 4500, 7200, 14400, 45000, 120000)


def _spider_recommended_delay(by_speed: list):
    """Regra (estatística, sem IA): a velocidade MAIS RÁPIDA testada que NUNCA bloqueou.
    Se alguma bloqueou, recomenda uma mais LENTA (delay maior) que a pior que bloqueou.
    Sem dados → None (mantém o default)."""
    tested = [t for t in by_speed if t["runs"] >= 1]
    if not tested:
        return None
    blocked = [t["delay_ms"] for t in tested if t["blocked_runs"] > 0]
    safe = [t["delay_ms"] for t in tested if t["blocked_runs"] == 0]
    if blocked:
        worst = max(blocked)   # a velocidade mais lenta (maior delay) que AINDA bloqueou
        slower = [d for d in _SPIDER_SPEED_TIERS if d > worst]
        return min(slower) if slower else max(_SPIDER_SPEED_TIERS)
    return min(safe) if safe else None


@app.get("/spider/insights")
def spider_insights():
    """Aprendizado do Spider: agrega o histórico de extrações por VELOCIDADE (delay_ms)
    — quantas runs, itens, taxa de bloqueio, itens até bloquear, ritmo efetivo — e
    recomenda a velocidade segura. Regras + estatística (sem IA)."""
    import datetime as _dt2

    def _dur_min(a, b):
        try:
            ta = _dt2.datetime.strptime(a, "%Y-%m-%d %H:%M:%S")
            tb = _dt2.datetime.strptime(b, "%Y-%m-%d %H:%M:%S")
            return max(0.0, (tb - ta).total_seconds() / 60.0)
        except Exception:
            return None

    tiers = {}
    for r in db.spider_runs_stats():
        d = int(r.get("delay_ms") or 2500)
        t = tiers.setdefault(d, {"runs": 0, "items": 0, "blocked": 0,
                                 "before_block": [], "minutes": 0.0})
        t["runs"] += 1
        t["items"] += int(r.get("count") or 0)
        if r.get("blocked"):
            t["blocked"] += 1
            t["before_block"].append(int(r.get("count") or 0))
        dm = _dur_min(r.get("started_at"), r.get("finished_at"))
        if dm:
            t["minutes"] += dm
    by_speed = []
    for d, t in sorted(tiers.items()):
        br = round(t["blocked"] / t["runs"] * 100, 1) if t["runs"] else 0.0
        avg_before = round(sum(t["before_block"]) / len(t["before_block"])) if t["before_block"] else None
        eff = round(t["items"] / (t["minutes"] / 60.0)) if t["minutes"] > 0 else None
        by_speed.append({"delay_ms": d,
                         "req_per_hour_nominal": (round(3600 / (d / 1000.0)) if d else 0),
                         "runs": t["runs"], "items": t["items"],
                         "blocked_runs": t["blocked"], "block_rate_pct": br,
                         "avg_items_before_block": avg_before,
                         "effective_items_per_hour": eff})
    return {"by_speed": by_speed,
            "recommended_delay_ms": _spider_recommended_delay(by_speed),
            "total_runs": sum(t["runs"] for t in tiers.values())}


def _launch_spider_job(*, method, account, profile, label, kind, target, limit, use_local,
                       delay_ms=2500, profile_id=None, account_id=None, seed_rows=None,
                       start_cursor=None, job_id=None, started_at=None, profiles=None):
    """Cria e dispara um job do Spider (usado por /spider/extract e /spider/resume).
    seed_rows/start_cursor permitem CONTINUAR de onde parou; delay_ms = velocidade.
    job_id/started_at: ao CONTINUAR, reusa o MESMO id da execução → soma no mesmo
    registro/CSV (não cria uma nova entrada no histórico).
    profiles: lista de perfis (>1) para RODÍZIO — revezam no mesmo alvo, cada um faz
    um turno (do cursor salvo) e passa para o próximo (use_local controla o IP)."""
    job_id = job_id or uuid.uuid4().hex[:12]
    started_at = started_at or time.strftime("%Y-%m-%d %H:%M:%S")
    seed_rows = seed_rows or []
    job = {"id": job_id, "status": "running", "kind": kind, "target": target,
           "count": len(seed_rows), "rows": list(seed_rows), "error": None, "stop": False,
           "profile": label, "method": method, "detail": "", "code": None,
           "code_method": None, "log": [], "started_at": started_at,
           "cursor": start_cursor}   # preserva o cursor ao CONTINUAR (senão o save inicial o zerava)
    _spider_jobs[job_id] = job

    def _save(status_extra=None):
        try:
            d = {"id": job_id, "method": method, "profile": label,
                 "profile_id": profile_id, "account_id": account_id, "use_local": use_local,
                 "profile_ids": json.dumps([p.get("id") for p in profiles]) if profiles else None,
                 "limit_n": limit, "delay_ms": delay_ms, "cursor": job.get("cursor"),
                 "target": target, "kind": kind,
                 "count": job.get("count", 0), "total": job.get("total"),
                 "status": job.get("status"),
                 "error": job.get("error"), "rows_json": json.dumps(job.get("rows") or []),
                 "started_at": started_at, "finished_at": job.get("finished_at"),
                 "blocked": job.get("blocked")}
            if status_extra:
                d.update(status_extra)
            db.spider_run_upsert(d)
        except Exception:
            logger.exception("falha ao salvar spider run")

    _save()   # registra início (running)

    def _log(msg):
        job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(job["log"]) > 500:
            del job["log"][:len(job["log"]) - 500]

    def _progress(n): job["count"] = n
    def _should_stop(): return job.get("stop", False)
    def _on_total(t): job["total"] = t      # total de itens do alvo (p/ mostrar X/Y)

    def _checkpoint(rows, cursor):
        # Chamado pelo spider a cada N páginas.
        # LIGHT update (só count + cursor) — evita serializar JSON gigante de todas as rows
        # a cada 10 páginas (que estava causando lentidão extrema em runs longas: "1min por página").
        # Full rows_json (para resume + CSV fallback) é salvo em momentos importantes:
        # início de turnos, fim de turnos (relay), final da job, e ocasionalmente a cada ~2500 itens.
        job["rows"] = rows
        job["count"] = len(rows)
        job["cursor"] = cursor
        try:
            db.spider_run_update_progress(job_id, job["count"], cursor)
        except Exception:
            pass
        # Snapshot completo de rows_json só de vez em quando (proteção contra crash sem matar o throughput)
        try:
            if (job.get("count", 0) % 2500 == 0):
                _save()
        except Exception:
            pass

    def _status(state):
        job["detail"] = {"logando": "Logando na conta (o IG pode pedir um código)…",
                         "extraindo": "Extraindo seguidores…"}.get(state, state or "")

    def _code_provider(username, choice):
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

    async def _relay():
        """Rodízio: as contas do pool revezam no MESMO alvo. Cada conta faz um turno
        (do cursor salvo) e passa o cursor para a próxima — a conta 'descansa'
        enquanto as outras trabalham (espalha o orçamento de req/h por conta).
        use_local=True → todas saem pelo MESMO IP local; False → cada uma pelo SEU proxy."""
        pool = profiles
        rows = list(seed_rows or [])
        cursor = start_cursor
        shift = max(2, spider._max_pages_for(delay_ms))   # páginas por turno = cap do tier
        last_user = None
        last_error = None
        made_progress = False
        _log(f"🔁 rodízio entre {len(pool)} conta(s): "
             + ", ".join(str(p.get('name')) for p in pool)
             + f" — turno ≈ {shift} páginas por conta · "
             + ("IP local (mesmo IP p/ todas)" if use_local else "proxy por conta"))
        idx = 0
        fails = 0
        while True:
            if _should_stop():
                _log("⏹ parado pelo usuário")
                break
            prof = pool[idx % len(pool)]
            idx += 1
            _log(f"▶ turno da conta '{prof.get('name')}'")
            r = await spider.extract(
                prof, kind, target, limit,
                progress=_progress, should_stop=_should_stop, log=_log,
                start_max_id=cursor, seed_rows=rows, delay_ms=delay_ms,
                on_total=_on_total, use_local=use_local, shift_pages=shift,
                checkpoint=_checkpoint)
            if r.get("error"):
                fails += 1
                last_error = r.get("error")
                _log(f"⚠ conta '{prof.get('name')}' falhou: {str(last_error)[:120]}")
                if fails >= len(pool):
                    # TODAS as contas do rodízio falharam no mesmo ponto = provável
                    # suspensão de todas → marca o run como bloqueado (alimenta o
                    # aprendizado: essa velocidade derrubou as contas).
                    job["blocked"] = True
                    _log("✖ TODAS as contas do rodízio falharam — provável suspensão "
                         "(velocidade alta demais p/ essas contas). Marcado como bloqueado.")
                    if not made_progress:
                        return {"rows": rows, "cursor": cursor, "total": job.get("total"),
                                "target": target, "kind": kind, "user": last_user,
                                "error": last_error}
                    break
                await asyncio.sleep(round(random.uniform(5, 12), 1))
                continue
            fails = 0
            made_progress = True
            rows = r.get("rows", rows)
            cursor = r.get("cursor")
            last_user = r.get("user") or last_user
            job["rows"] = rows; job["count"] = len(rows); job["cursor"] = cursor
            if r.get("total") is not None:
                job["total"] = r.get("total")
            if r.get("blocked"):   # bloqueio/desafio nesta conta → marca suspensa
                job["blocked"] = True
                u = _spider_mark_blocked(profile=prof)
                if u:
                    _log(f"⚠ '{u}' marcada como 'em suspensão' (possível bloqueio/desafio)")
            _save()   # persiste após CADA turno (resume-safe; soma no mesmo CSV)
            if not cursor:
                _log(f"✓ rodízio concluído — {len(rows)} registro(s)")
                break
            if limit and len(rows) >= limit:
                break
            await asyncio.sleep(round(random.uniform(3, 8), 1))   # troca de conta (reduzido)
        return {"rows": rows, "cursor": cursor, "total": job.get("total"),
                "target": target, "kind": kind, "user": last_user}

    async def _worker():
        try:
            if method == "app":
                res = await spider_app.extract(
                    account, kind, target, limit,
                    progress=_progress, should_stop=_should_stop,
                    code_provider=_code_provider, status=_status, use_local=use_local,
                    log=_log, start_max_id=start_cursor, seed_rows=seed_rows, delay_ms=delay_ms,
                    on_total=_on_total, checkpoint=_checkpoint)
            elif profiles and len(profiles) > 1:
                res = await _relay()
            else:
                res = await spider.extract(
                    profile, kind, target, limit,
                    progress=_progress, should_stop=_should_stop, log=_log,
                    start_max_id=start_cursor, seed_rows=seed_rows, delay_ms=delay_ms,
                    on_total=_on_total, use_local=use_local, checkpoint=_checkpoint)
            if res.get("error"):
                job["status"] = "error"; job["error"] = res["error"]
                _log("✖ " + str(res["error"])[:160])
            else:
                job["rows"] = res.get("rows", [])
                job["count"] = len(job["rows"])
                job["cursor"] = res.get("cursor")
                if res.get("total") is not None:
                    job["total"] = res.get("total")
                # Parou no meio (cursor presente) e não foi erro → 'stopped' (continuável)
                job["status"] = "stopped" if job.get("cursor") else "done"
                job["detail"] = ""
                # Bloqueio/desafio detectado (modo single; o rodízio já marca por turno)
                # → marca a conta como suspensa p/ entrar nas métricas.
                if res.get("blocked") and not (profiles and len(profiles) > 1):
                    job["blocked"] = True
                    u = _spider_mark_blocked(account=account if method == "app" else None,
                                             profile=None if method == "app" else profile)
                    if u:
                        _log(f"⚠ '{u}' marcada como 'em suspensão' (possível bloqueio/desafio)")
        except Exception as e:
            job["status"] = "error"; job["error"] = str(e)
            _log("✖ erro: " + str(e)[:160])
        finally:
            job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save()

    asyncio.create_task(_worker())
    return job_id


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
        ids = [i for i in (body.profile_ids or ([body.profile_id] if body.profile_id else [])) if i]
        if not ids:
            raise HTTPException(400, "Selecione ao menos um perfil")
        pool = [pr for pr in (get_profile(i) for i in ids) if pr]
        if not pool:
            raise HTTPException(404, "Perfil não encontrado")
        p = pool[0]
        label = f"{len(pool)} contas (rodízio)" if len(pool) > 1 else p.get("name")

    job_id = _launch_spider_job(
        method=method, account=a, profile=p, label=label, kind=body.kind,
        target=body.target, limit=body.limit, use_local=body.use_local,
        delay_ms=body.delay_ms,
        profile_id=(p.get("id") if method != "app" else None), account_id=body.account_id,
        profiles=(pool if method != "app" and len(pool) > 1 else None))
    return {"job_id": job_id}


class SpiderResumeRequest(BaseModel):
    delay_ms: Optional[int] = None   # velocidade ATUAL do seletor; sobrepõe a salva na run


@app.post("/spider/resume/{run_id}")
async def spider_resume(run_id: str, body: SpiderResumeRequest = SpiderResumeRequest()):
    """Continua uma execução que parou no meio (cursor salvo + linhas já coletadas).
    A velocidade vem do seletor ATUAL (body.delay_ms) — antes reusava a salva na run,
    então mudar o tier no 'Continuar' não fazia efeito."""
    run = db.get_spider_run(run_id)
    if not run:
        raise HTTPException(404, "Execução não encontrada")
    if not run.get("cursor"):
        raise HTTPException(400, "Esta execução já está completa — nada para continuar.")
    method = (run.get("method") or "web").lower()
    a = p = None
    if method == "app":
        a = db.get_account(run.get("account_id")) if run.get("account_id") else None
        if not a:
            raise HTTPException(404, "Conta original não existe mais — não dá para continuar.")
        label = a.get("username")
    else:
        pool = _spider_pool_from_run(run)
        if not pool:
            raise HTTPException(404, "Perfil original não existe mais — não dá para continuar.")
        p = pool[0]
        label = f"{len(pool)} contas (rodízio)" if len(pool) > 1 else p.get("name")
    try:
        seed_rows = json.loads(run.get("rows_json") or "[]")
    except Exception:
        seed_rows = []
    job_id = _launch_spider_job(
        method=method, account=a, profile=p, label=label, kind=run.get("kind") or "followers",
        target=run.get("target"), limit=int(run.get("limit_n") or 0),
        use_local=bool(run.get("use_local")),
        delay_ms=(body.delay_ms if body.delay_ms else int(run.get("delay_ms") or 2500)),
        profile_id=run.get("profile_id"), account_id=run.get("account_id"),
        profiles=(pool if method != "app" and len(pool) > 1 else None),
        seed_rows=seed_rows, start_cursor=run.get("cursor"),
        job_id=run_id, started_at=run.get("started_at"))   # MESMO id → soma no mesmo CSV
    return {"job_id": job_id}


@app.post("/spider/rerun/{run_id}")
async def spider_rerun(run_id: str):
    """Roda de NOVO uma execução do histórico (do zero, mesmo alvo/tipo/perfil)."""
    run = db.get_spider_run(run_id)
    if not run:
        raise HTTPException(404, "Execução não encontrada")
    method = (run.get("method") or "web").lower()
    a = p = None
    if method == "app":
        a = db.get_account(run.get("account_id")) if run.get("account_id") else None
        if not a:
            raise HTTPException(404, "Conta original não existe mais.")
        label = a.get("username")
    else:
        pool = _spider_pool_from_run(run)
        if not pool:
            raise HTTPException(404, "Perfil original não existe mais.")
        p = pool[0]
        label = f"{len(pool)} contas (rodízio)" if len(pool) > 1 else p.get("name")
    job_id = _launch_spider_job(
        method=method, account=a, profile=p, label=label, kind=run.get("kind") or "followers",
        target=run.get("target"), limit=int(run.get("limit_n") or 0),
        use_local=bool(run.get("use_local")), delay_ms=int(run.get("delay_ms") or 2500),
        profile_id=run.get("profile_id"), account_id=run.get("account_id"),
        profiles=(pool if method != "app" and len(pool) > 1 else None))
    return {"job_id": job_id}


@app.get("/spider/jobs/{job_id}")
def spider_job(job_id: str):
    j = _spider_jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job não encontrado")
    return {"id": j["id"], "status": j["status"], "count": j["count"],
            "total": j.get("total"),                   # total do alvo (X/Y)
            "kind": j["kind"], "target": j["target"], "error": j["error"],
            "detail": j.get("detail", ""), "code_method": j.get("code_method"),
            "cursor": j.get("cursor"),                 # != null => dá p/ continuar
            "log": j.get("log", [])[-300:]}            # terminal do processo (tail)


@app.get("/spider/history")
def spider_history(limit: int = 50):
    """Histórico persistente das execuções do Spider (metadados)."""
    return {"items": db.list_spider_runs(limit)}


class SpiderBlockedRequest(BaseModel):
    blocked: bool = True


@app.post("/spider/history/{run_id}/blocked")
def spider_history_set_blocked(run_id: str, body: SpiderBlockedRequest):
    """Corrige o desfecho de um run (terminou em suspensão?) — ENSINA a recomendação
    de velocidade. Útil quando a suspensão não foi auto-detectada (ex.: parou no meio
    porque as contas caíram, mas o app registrou como 'parado')."""
    if not db.get_spider_run(run_id):
        raise HTTPException(404, "Execução não encontrada")
    db.set_spider_run_blocked(run_id, body.blocked)
    j = _spider_jobs.get(run_id)
    if j is not None:
        j["blocked"] = body.blocked
    return {"id": run_id, "blocked": body.blocked}


@app.delete("/spider/history/{run_id}", status_code=204)
def spider_history_delete(run_id: str):
    db.delete_spider_run(run_id)
    _spider_jobs.pop(run_id, None)


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


SPIDER_DATA_DIR = Path(__file__).resolve().parent / "data"


def _save_spider_csv(content: str, target: str, fname: str) -> str:
    """Salva o CSV em data/<user_alvo>/<arquivo>.csv (ao lado do app) e devolve o
    caminho. Pasta por alvo p/ organizar várias extrações do mesmo perfil."""
    sub = spider.clean_username(target) or "sem_alvo"
    folder = SPIDER_DATA_DIR / re.sub(r"[^A-Za-z0-9_.-]", "_", sub)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / fname
    path.write_text(content, encoding="utf-8-sig")   # BOM → abre certo no Excel
    return str(path)


@app.get("/spider/jobs/{job_id}/csv")
def spider_job_csv(job_id: str):
    from fastapi.responses import Response
    j = _spider_jobs.get(job_id)
    if j:
        rows = j.get("rows") or []
        target = j.get("target", "")
        kind = j.get("kind", "followers")
    else:
        # Fallback: execução antiga do histórico (rows salvas no banco)
        run = db.get_spider_run(job_id)
        if not run:
            raise HTTPException(404, "Job não encontrado")
        try:
            rows = json.loads(run.get("rows_json") or "[]")
        except Exception:
            rows = []
        target = run.get("target", "")
        kind = run.get("kind", "followers")
    # Dedup defensivo final (garante CSV sem duplicados, qualquer que seja a origem)
    dkey = "Shortcode" if kind == "posts" else "User Id"
    seen, deduped = set(), []
    for r in rows:
        k = str(r.get(dkey) or "")
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        deduped.append(r)
    rows = deduped
    buf = _io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=spider.headers_for(kind), extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    prefix = {"followers": "IGFollowers", "following": "IGFollowing", "posts": "IGPosts"}.get(kind, "IGData")
    fname = f"{prefix}_{spider.clean_username(target)}_{len(rows)}.csv"
    content = buf.getvalue()
    # Salva SEMPRE no disco (data/<alvo>/) — o app roda em webview e o download do
    # navegador costuma falhar; assim o CSV fica garantido em arquivo.
    saved_path = ""
    try:
        saved_path = _save_spider_csv(content, target, fname)
    except Exception:
        logger.exception("falha ao salvar CSV em data/")
    return Response(content=content, media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}",
                             "X-Saved-Path": saved_path})


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
    """Verifica o status das contas VIVAS, sequencial e headless. Pula terminais
    (banida/descartada — sem volta), sem perfil, ou em uso agora; pausa entre contas."""
    try:
        busy = {s.get("profile_id") for s in sm.list_active()}
    except Exception:
        busy = set()
    terminal = {"banned", "discarded"}
    pending = [a for a in acc.list_accounts()
               if (a.get("status") or "").lower() not in terminal
               and a.get("profile_id") and a.get("profile_id") not in busy]
    for i, a in enumerate(pending):
        if _creation_stop:
            break
        try:
            await acc.check_status(a["id"], headless=True)
        except Exception:
            pass
        if i < len(pending) - 1:
            await asyncio.sleep(8)   # ritmo leve entre contas


@app.post("/accounts/check-all")
async def accounts_check_all():
    """Dispara verificação de todas as contas em segundo plano (sequencial)."""
    asyncio.create_task(_check_all_accounts())
    return {"status": "checking", "count": len(acc.list_accounts())}


class AutoRecheckRequest(BaseModel):
    hours: int = 0


@app.get("/settings/auto-recheck")
def auto_recheck_get():
    """Config da verificação periódica automática (0 = desligada)."""
    hours = int(db.get_setting("auto_recheck_hours", "0") or 0)
    last = float(db.get_setting("auto_recheck_last", "0") or 0)
    nxt = int(last + hours * 3600) if (hours > 0 and last > 0) else 0
    return {"hours": hours, "last": int(last) if last else 0, "next": nxt}


@app.post("/settings/auto-recheck")
def auto_recheck_set(body: AutoRecheckRequest):
    h = max(0, int(body.hours or 0))
    db.set_setting("auto_recheck_hours", str(h))
    if h > 0:                                   # ao ligar, conta a partir de agora
        db.set_setting("auto_recheck_last", str(int(time.time())))
    return {"hours": h}


# ─── Informações locais: salvar/carregar todo o local data (migrar de máquina) ──
@app.get("/local-data/info")
def local_data_info():
    """Resumo do que será salvo no backup (caminhos, tamanhos, contagens)."""
    items = []
    for src, prefix in bdata._items():
        exists = src.exists()
        size = files = 0
        if exists:
            if src.is_file():
                size = src.stat().st_size
                files = 1
            else:
                for f in src.rglob("*"):
                    if f.is_file() and not bdata._is_junk(f.relative_to(src)):
                        try:
                            size += f.stat().st_size
                        except Exception:
                            pass
                        files += 1
        items.append({"name": prefix, "path": str(src), "exists": exists,
                      "size": size, "files": files})
    try:
        summary = {
            "profiles": len(db.list_profiles()),
            "accounts": len(db.list_accounts()),
            "emails": len(eb.list_emails()),
            "proxies": pool.summary().get("count", 0),
        }
    except Exception:
        summary = {}
    return {"app_dir": str(APP_DIR), "items": items, "summary": summary,
            "total_size": sum(i["size"] for i in items)}


@app.get("/local-data/backup")
def local_data_backup():
    """Gera o ZIP com todo o local data e devolve para download."""
    import tempfile
    from starlette.background import BackgroundTask
    name = f"profilemanager-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    tmp = os.path.join(tempfile.gettempdir(), name)
    bdata.backup(tmp)
    return FileResponse(tmp, media_type="application/zip", filename=name,
                        background=BackgroundTask(lambda: _safe_unlink(tmp)))


def _safe_unlink(path):
    try:
        os.remove(path)
    except Exception:
        pass


@app.post("/local-data/restore")
async def local_data_restore(request: Request):
    """Restaura o local data de um ZIP enviado no corpo (sobrescreve). Recomendado
    parar as sessões e reiniciar o app depois."""
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "profilemanager-restore.zip")
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        if os.path.getsize(tmp) == 0:
            raise HTTPException(400, "Envie o arquivo .zip do backup.")
        bdata.restore(tmp)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Falha ao restaurar: {e}")
    finally:
        _safe_unlink(tmp)
    return {"status": "restored"}


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


def _proxy_status_block(pk: str, host: str, summary_items=None, outcomes=None, flags=None) -> dict:
    """Monta {level, health, manual, country, status} de um proxy a partir de
    pool.summary() + desfechos reais (proxy_outcomes) + reachability (cache) + país +
    MARCAÇÃO MANUAL (proxy_flags). `health`: limpo/sujo/queimado/sem-dados. Se você
    marcou como queimado, `manual=True` e `health='banned'` (vence a regra automática).
    `summary_items`/`outcomes`/`flags` podem ser pré-computados p/ evitar re-scan."""
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
    if flags is None:
        try:
            flags = db.list_proxy_flags()
        except Exception:
            flags = {}
    out_d = outcomes.get(pk, {}) or {}
    reach = (_proxy_test_cache.get("results") or {}).get(pk)
    reachable = reach.get("ok") if reach else None
    try:
        country = pool.proxy_country(host) if host else ""
    except Exception:
        country = ""
    ban = st.get("ban_rate_pct")
    blocked = bool(st.get("blocked"))
    fl = flags.get(pk) or {}
    manual = fl.get("status") == "banned"
    health = "banned" if manual else _derive_proxy_health(out_d, 0, reachable)
    # Vermelho (atenção) se: marcado, não responde, no limite, ban alto, ou sujo/queimado.
    warn = (manual or blocked or (reachable is False)
            or (isinstance(ban, (int, float)) and ban >= 20)
            or health in ("dirty", "banned"))
    return {
        "level": "warn" if warn else "ok",
        "health": health,
        "manual": manual,
        "manual_reason": fl.get("reason") if manual else None,
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


def _active_core():
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
    clean_n = sum(1 for _, _, b in blocks if b.get("health") == "clean")
    return {"mode": "pool_many", "local": False, "proxy_key": wk, "host": whost,
            "count": len(blocks), "dirty_count": dirty_n, "clean_count": clean_n, **wb}


# IP público REAL do PC (sem proxy) — p/ comparar com o proxy em uso. Cache ~120s.
_myip_cache = {"at": 0.0, "ip": None, "country": None}


def _my_public_ip() -> dict:
    now = time.time()
    if _myip_cache["ip"] and (now - _myip_cache["at"] < 120):
        return {"ip": _myip_cache["ip"], "country": _myip_cache["country"]}
    try:
        import urllib.request
        with urllib.request.urlopen(
                "http://ip-api.com/json/?fields=query,countryCode", timeout=4) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        _myip_cache.update(at=now, ip=d.get("query") or _myip_cache["ip"],
                           country=(d.get("countryCode") or _myip_cache["country"]))
    except Exception:
        _myip_cache["at"] = now   # evita martelar o serviço em falha
    return {"ip": _myip_cache["ip"], "country": _myip_cache["country"]}


def _pool_health_counts() -> dict:
    """Resumo de saúde do pool (total/limpos/sujos/queimados/sem-dados) p/ a caixa.
    Respeita a marcação manual (queimado pelo usuário vence a regra automática)."""
    out = {"total": 0, "clean": 0, "dirty": 0, "banned": 0, "unknown": 0}
    try:
        items = pool.summary().get("items", [])
        outcomes = acc.proxy_outcomes()
        flags = db.list_proxy_flags()
    except Exception:
        return out
    out["total"] = len(items)
    for it in items:
        k = it.get("key")
        if (flags.get(k) or {}).get("status") == "banned":
            h = "banned"
        else:
            out_d = outcomes.get(k, {}) or {}
            reach = (_proxy_test_cache.get("results") or {}).get(k)
            h = _derive_proxy_health(out_d, 0, reach.get("ok") if reach else None)
        out[h] = out.get(h, 0) + 1
    return out


@app.get("/proxies/active")
def proxies_active():
    """Endpoint do indicador do topo: junta o proxy ATUAL (_active_core) com o
    MEU IP real (sem proxy, p/ comparar) e o resumo de saúde do pool — tudo o que
    a caixa de status mostra."""
    core = _active_core()
    mip = _my_public_ip()
    core["my_ip"] = mip.get("ip")
    core["my_ip_country"] = mip.get("country")
    core["pool"] = _pool_health_counts()
    # Meu IP real marcado manualmente como queimado? (ex.: criação via conexão local)
    try:
        core["my_ip_flagged"] = bool(core.get("my_ip")
                                     and (db.list_proxy_flags().get(core["my_ip"]) or {}).get("status") == "banned")
    except Exception:
        core["my_ip_flagged"] = False
    return core


class ProxyMarkRequest(BaseModel):
    key: str                     # host:porta de um proxy OU um IP cru (ex.: meu IP real)
    status: str = "banned"       # 'banned' (queimado) | 'clean' (libera/remove a marca)
    reason: Optional[str] = None


@app.post("/proxies/mark")
def proxies_mark(body: ProxyMarkRequest):
    """Marca/libera MANUALMENTE um proxy (host:porta) ou IP. 'banned' = queimado
    (assign() para de usar e a UI mostra queimado); 'clean' remove a marcação."""
    key = (body.key or "").strip()
    if not key:
        raise HTTPException(400, "Informe o proxy (host:porta) ou IP a marcar")
    if body.status == "banned":
        db.set_proxy_flag(key, "banned", body.reason or "marcado pelo usuário")
        return {"key": key, "status": "banned"}
    db.delete_proxy_flag(key)
    return {"key": key, "status": "clean"}


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
    try:
        flags = db.list_proxy_flags()              # marcação manual (queimado etc.)
    except Exception:
        flags = {}

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

        manual = (flags.get(k) or {}).get("status") == "banned"
        health = "banned" if manual else _derive_proxy_health(out_d, fn.get("total", 0), reachable)
        ban_rate = pit.get("ban_rate_pct")
        warn = (manual or health in ("dirty", "banned") or reachable is False or bool(pit.get("blocked"))
                or (isinstance(ban_rate, (int, float)) and ban_rate >= 20))

        items.append({
            "proxy_key": k,
            "host": host,
            "country": country or None,
            "in_pool": k in pool_keys,
            "reachable": reachable,
            "health": health,
            "manual": manual,
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
        fields["account_id"] = body.account_id or None   # "" → desvincula
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
