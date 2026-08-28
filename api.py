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
import follower
import comportamento
import modem
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
    """A UI inteira vive neste arquivo. Servido SEM CACHE de propósito: o webview
    guardava a versão antiga e, depois de uma atualização, a tela continuava rodando
    o JS velho — sintoma real (2026-08-16): o campo de alvos não salvava porque a
    página em cache era anterior ao auto-save. Reabrir o app tem que trazer a UI atual."""
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                                 "Pragma": "no-cache", "Expires": "0"})


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
    try:
        # Seguir: idem — execuções presas em 'running' viram 'stopped' (a fila está
        # salva em queue_json, então dá para retomar de onde parou).
        n = db.recover_orphan_follow_runs()
        if n:
            print(f"DEBUG: {n} execução(ões) de follow órfã(s) marcada(s) como 'stopped'")
    except Exception:
        logger.exception("Falha ao recuperar runs órfãs de Seguir")


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
    # O launch DESTRAVA o perfil (fecha o anterior + mata processo órfão preso nele).
    # Se o Spider está extraindo com ESTE perfil, destravar mataria a extração —
    # então avisamos em vez de matar a run por acidente.
    busy = [j for j in _spider_jobs.values()
            if j.get("status") in ("running", "needs_code")
            and profile_id in (j.get("profile_ids") or [])]
    if busy:
        raise HTTPException(409,
                            "O Spider está extraindo com este perfil agora. Pare a extração "
                            "na aba Spider antes de abrir o navegador (abrir agora encerraria "
                            "a extração em andamento).")
    # Mesma proteção para o InstaFollow: ele roda um Camoufox HEADLESS neste perfil,
    # e o launch (close_others + kill de processos presos no user_data_dir) mataria
    # a execução no meio. Faltava esta guarda — abrir um perfil derrubava o follow.
    busy_fw = [j for j in _follow_jobs.values()
               if j.get("status") in ("running", "harvesting")
               and profile_id in (j.get("profile_ids") or [])]
    if busy_fw:
        raise HTTPException(409,
                            "O InstaFollow está usando este perfil agora (seguir/coletar). "
                            "Pare a operação na aba InstaFollow antes de abrir o navegador — "
                            "abrir agora encerraria a execução em andamento.")
    # IP novo ANTES de abrir: medido (2026-08-16) que o pool da operadora NÃO se
    # renova sozinho — 3 min de amostragem devolveram exatamente os mesmos 4 IPs.
    # Sem isto, abrir o perfil A e depois o B faria os dois saírem pelos MESMOS
    # IPs (o sinal clássico de "várias contas no mesmo IP"). Como o launch já fecha
    # o navegador anterior, os perfis são sequenciais e cada um ganha sua época.
    rot = await _rotate_ip_for(p)
    try:
        # A extensão ig-filler fica desligada por padrão: em alguns renders do IG ela
        # interfere na página. O fluxo automático usa filler interno via page.evaluate.
        auto_load_filler = False
        # close_others=True: pedido do usuário — abrir um navegador FECHA o anterior
        # (além de destravar este perfil de processos órfãos / parent.lock).
        result = await sm.launch(p, url=body.url, headless=body.headless, actions=body.actions, mode=body.mode,
                                 load_ig_filler=auto_load_filler, close_others=True)
        if isinstance(result, dict) and not rot.get("skipped"):
            result["ip_rotation"] = {"ok": rot.get("ok"), "mudou": rot.get("mudou"),
                                     "ips": rot.get("ips_depois"), "segundos": rot.get("segundos")}
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
    """DORMENTE (2026-08-16): o "Abrir Todos" foi removido da UI a pedido do usuário
    e nenhuma tela chama mais este endpoint. Mantido só para não quebrar chamadas
    externas/antigas. NÃO rotaciona o IP de propósito — perfis abertos ao mesmo tempo
    saem pela MESMA época do modem, então rotacionar por perfil só somaria queda sem
    separar nada (a rotação só faz sentido com perfis sequenciais)."""
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


# SO com que nasce o perfil de uma CONTA. `host` = o SO real do PC.
#
# Foi ANDROID por algumas horas em 27/08 e VOLTOU para desktop em 28/08, depois do
# teste ao vivo. O motivo da volta não é estética — é que a justificativa caiu: o
# mobile web TEM criação de story, mas **não tem o sticker de link**, que era o
# ponto. E o disfarce mostrou-se fino num ponto que não dá para consertar: o
# Instagram pediu "gire seu dispositivo", lendo `screen.orientation`, que sai do
# monitor real e para o qual o motor do Camoufox não tem propriedade nenhuma.
#
# Some-se que o perfil mobile quebra o que funciona (follow por clique, leitura de
# perfil e o ruído do comportamento.py foram medidos contra a visão desktop), e que
# o único caminho que ainda pode dar o sticker — instagrapi via `sessionid` — não
# precisa de perfil mobile para nada.
#
# O suporte a mobile continua no código, testado e dormente: basta o setting
# `novo_perfil_os` = windows | macos | linux | android | host.
_OS_PADRAO_CONTA = "host"


def _os_perfil_novo() -> str:
    """SO do fingerprint de um perfil criado pela aba Contas."""
    escolhido = str(db.get_setting("novo_perfil_os", "") or "").strip().lower()
    if escolhido not in ("windows", "macos", "linux", "android", "host"):
        escolhido = _OS_PADRAO_CONTA
    if escolhido != "host":
        return escolhido
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
        pid = prof.create(name=name, os=_os_perfil_novo(), proxy=proxy_dict)["id"]
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
    pid = prof.create(name=name, os=_os_perfil_novo(), proxy=proxy_dict)["id"]
    return acc.edit(account_id, {"profile_id": pid})


@app.get("/accounts/{account_id}/export-session")
async def accounts_export_session(account_id: str):
    """Lê a SESSÃO do Instagram do perfil da conta, para colar em outro perfil.

    Existia só o import: pegar os cookies dependia da extensão Cookie-Editor dentro
    do navegador — que fica inutilizável em janela estreita (o caso do perfil
    mobile). Este endpoint não precisa de janela: `export_cookies` abre o perfil
    headless e lê o jar, **sem nenhuma requisição ao Instagram**.

    O formato devolvido é o mesmo que o `parse_cookies_input` aceita, então dá para
    colar direto no campo de importar. O valor do sessionid vai no corpo (é o
    ponto), mas NUNCA em log — só o resumo, que o omite.
    """
    import ig_session as igs
    a = acc.get_account(account_id)
    if not a:
        raise HTTPException(404, "Conta não encontrada")
    p = get_profile(a.get("profile_id")) if a.get("profile_id") else None
    if not p:
        raise HTTPException(400, "Esta conta não tem perfil vinculado.")
    try:
        cookies = await igs.export_cookies(p)
    except Exception as e:
        raise HTTPException(500, f"Não consegui ler os cookies do perfil: {e}")
    resumo = igs.summarize(cookies)
    logger.info("export-session conta=%s perfil=%s resumo=%s", account_id, p.get("id"), resumo)
    if not resumo.get("has_sessionid"):
        raise HTTPException(400, "O perfil não tem o cookie 'sessionid' — ou seja, não "
                                 "há sessão do Instagram para exportar. Abra o perfil e "
                                 "confirme que a conta está logada.")
    return {"cookies": cookies, "resumo": resumo}


class ImportSessionRequest(BaseModel):
    cookies: str = ""   # texto colado: JSON do Cookie-Editor, header, ou só o sessionid


@app.post("/accounts/{account_id}/import-session")
async def accounts_import_session(account_id: str, body: ImportSessionRequest):
    """Importa a SESSÃO do Instagram (cookies, incl. sessionid) no perfil da conta —
    sem login novo. Uso: conta que só loga no app/celular → exporta os cookies de
    instagram.com pelo Cookie-Editor (Firefox Android) e cola aqui. Confirma logando
    de leve (1 requisição GraphQL). NÃO registra o valor do sessionid em log."""
    import ig_session as igs
    a = acc.get_account(account_id)
    if not a:
        raise HTTPException(404, "Conta não encontrada")
    p = get_profile(a.get("profile_id")) if a.get("profile_id") else None
    if not p:
        raise HTTPException(400, "Esta conta não tem perfil vinculado. Crie um perfil "
                                 "primeiro (botão '➕ Criar perfil').")
    cookies = igs.parse_cookies_input(body.cookies)
    if not any(c.get("name") == "sessionid" for c in cookies):
        raise HTTPException(400, "Não achei o cookie 'sessionid' no que você colou. "
                                 "Exporte os cookies de instagram.com pelo Cookie-Editor "
                                 "(o sessionid é httpOnly — precisa da extensão, não dá "
                                 "pra copiar do navegador).")
    imported = await igs.import_cookies(p, cookies)
    # Confirma o login: usa o proxy do perfil se houver (coerência), senão IP local.
    probe = {"logged": None}
    try:
        probe = await igs.probe_login(p, target="instagram", use_local=not bool(p.get("proxy")))
    except Exception as e:
        probe = {"logged": None, "error": str(e)}

    # PERSISTE o resultado — sem isto o usuário recarregava a tela e não sabia se a
    # importação tinha funcionado. Sessão confirmada = a conta está logada no perfil,
    # que é exatamente o que o status 'active' significa aqui.
    quando = time.strftime("%Y-%m-%d %H:%M:%S")
    n = imported.get("imported")
    try:
        if probe.get("logged"):
            acc.set_status(account_id, "active",
                           f"sessão importada e confirmada em {quando} ({n} cookies)")
        else:
            # Não sobrescreve o status (pode ser suspensa etc.), só registra a tentativa.
            db.update_account(account_id,
                              last_checked_at=quando,
                              last_check_result=f"sessão importada ({n} cookies), mas NÃO "
                                                f"confirmou login (status {probe.get('status')})")
    except Exception:
        logger.exception("falha ao registrar a importação de sessão")

    return {"imported": n, "logged": probe.get("logged"),
            "status": probe.get("status"), "error": probe.get("error"),
            "ds_user_id": (probe.get("cookies") or {}).get("ds_user_id"),
            "registrado_em": quando}


class SetAccountProxyRequest(BaseModel):
    proxy: Optional[str] = None   # "host:porta[:user:pass]" ou URL; vazio = conexão local


@app.post("/accounts/{account_id}/set-proxy")
def accounts_set_proxy(account_id: str, body: SetAccountProxyRequest = SetAccountProxyRequest()):
    """Define/troca o proxy do PERFIL da conta — ex.: apontar pra um proxy MÓVEL 4G/LTE
    (IP de operadora = trust alto, conta dura mais). Vazio = conexão local (sem proxy).
    O Spider usa o proxy do perfil quando 'Proxy por conta' está ligado. Re-snapshota o
    proxy na conta (exibição/métricas) reusando acc.edit(profile_id)."""
    import json as _json
    a = acc.get_account(account_id)
    if not a:
        raise HTTPException(404, "Conta não encontrada")
    pid = a.get("profile_id")
    if not (pid and get_profile(pid)):
        raise HTTPException(400, "Esta conta não tem perfil vinculado. Crie um perfil primeiro.")
    proxy_dict = None
    if body.proxy and body.proxy.strip():
        proxy_dict = pool.parse_proxy_line(body.proxy.strip())
        if proxy_dict is None:
            raise HTTPException(400, "Proxy inválido (use host:porta:usuário:senha ou URL http/socks5).")
    db.update_profile(pid, proxy=(_json.dumps(proxy_dict) if proxy_dict else None))
    return acc.edit(account_id, {"profile_id": pid})   # re-snapshota o proxy novo na conta


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

# Handoff: cada conta extrai até ~1,5M itens (≈30000 págs a ~50/pág) e passa pra
# próxima. Fica ABAIXO do teto de ~1,6M onde a conta cai (validado: cobbyw524 caiu
# em ~1,6M). Parar antes preserva a conta; a próxima segue o alvo e continua.
_SPIDER_SAFE_PAGES_PER_ACCOUNT = 30000


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

    def _failed(r):
        """Falhou = a sessão parou no MEIO (cursor salvo) sem ter sido você (a conta
        caiu), ou bloqueio/erro explícito. CHAVE: sem cursor = chegou ao FIM da lista
        = completou (o 'total' do IG é aproximado/flutua, então count um pouco abaixo
        do total NÃO é falha). Só julga runs já encerradas."""
        if r.get("blocked"):
            return True
        if r.get("stopped_by_user"):
            return False
        if r.get("status") == "error":
            return True
        if not r.get("cursor"):
            return False   # sem cursor = terminou a lista = completou
        finished = (r.get("status") in ("done", "stopped")) or r.get("finished_at")
        return bool(finished)   # tem cursor e já encerrou (não foi você) = caiu no meio

    tiers = {}
    for r in db.spider_runs_stats():
        d = int(r.get("delay_ms") or 2500)
        t = tiers.setdefault(d, {"runs": 0, "items": 0, "blocked": 0,
                                 "before_block": [], "minutes": 0.0})
        t["runs"] += 1
        t["items"] += int(r.get("count") or 0)
        if _failed(r):
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


async def _rotate_ip_for(profile, log=None, use_local=None) -> dict:
    """Rotaciona o IP do modem 4G antes de uma conta começar a trabalhar, para que
    cada conta atue numa ÉPOCA de saída própria (o modem não tem IP fixo: a
    operadora faz CGNAT e responde de vários IPs; o que a rotação garante é que o
    CONJUNTO seja renovado).

    Só faz sentido quando a conta sai PELO MODEM: se o perfil usa proxy próprio, o
    IP dele não vem daqui e rotacionar só causaria queda à toa. No-op silencioso
    quando a rotação está desligada nos ajustes."""
    try:
        if not modem.config()["enabled"]:
            return {"skipped": True}
        via_modem = bool(use_local) or not (profile or {}).get("proxy")
        if not via_modem:
            if log:
                log("↷ rotação de IP pulada: este perfil sai pelo proxy dele, não pelo modem")
            return {"skipped": True, "reason": "perfil com proxy próprio"}
        return await modem.rotate(log=log)
    except Exception as e:
        if log:
            log(f"⚠ rotação de IP falhou ({str(e)[:80]}) — seguindo com o IP atual")
        return {"ok": False, "error": str(e)}


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
    # Perfis que ESTE job está usando — para o "Abrir navegador" saber que não pode
    # destravar/matar o processo deste perfil sem encerrar a extração.
    _pids_used = [p.get("id") for p in (profiles or []) if p.get("id")] \
        or ([profile_id] if profile_id else [])
    job = {"id": job_id, "status": "running", "kind": kind, "target": target,
           "count": len(seed_rows), "rows": list(seed_rows), "error": None, "stop": False,
           "profile": label, "method": method, "detail": "", "code": None,
           "code_method": None, "log": [], "started_at": started_at,
           "profile_ids": _pids_used,
           "cursor": start_cursor}   # preserva o cursor ao CONTINUAR (senão o save inicial o zerava)
    _spider_jobs[job_id] = job

    # Todo alvo que o Spider extrai entra nos Perfis Monitorados (idempotente — no-op
    # se já estiver). Cobre extração, lote, continuar e re-rodar (todos passam aqui).
    try:
        _mon = spider.clean_username(target)
        if _mon:
            db.add_monitored(_mon)
    except Exception:
        pass

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
                 "blocked": job.get("blocked"), "stopped_by_user": job.get("stopped_by_user")}
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
    def _on_total(t):
        job["total"] = t      # total de itens do alvo (p/ mostrar X/Y)
        if kind == "followers":               # se o alvo é monitorado, atualiza o total
            try: db.set_monitored_total(target, t)
            except Exception: pass

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
        # Snapshot completo de rows_json a cada ~2500 itens NOVOS (proteção contra
        # crash sem matar o throughput). Conta itens desde o último save completo —
        # robusto a páginas de tamanho variável (a dedup faz o count somar 48/49/50,
        # então `count % 2500 == 0` quase nunca caía no múltiplo exato e o save
        # completo não disparava → era a brecha que perdia dados num crash).
        try:
            job["_last_full_save"] = job.get("_last_full_save", job["count"])
            if job["count"] - job["_last_full_save"] >= 2500:
                job["_last_full_save"] = job["count"]
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

    async def _failover():
        """Handoff seguro (mesmo alvo, várias contas): cada conta extrai até ~1,5M
        itens e passa o cursor pra PRÓXIMA da lista — abaixo do teto de ~1,6M onde a
        conta cai. A conta nova SEGUE o alvo antes (spider.extract faz isso), o que
        libera a lista profunda. Cada conta é usada UMA vez, em ordem (não recicla,
        pra não re-queimar). Para quando o alvo termina (cursor=None) ou as contas
        acabam (cursor salvo → dá pra continuar com mais contas depois)."""
        pool = profiles
        rows = list(seed_rows or [])
        cursor = start_cursor
        last_user = None
        last_error = None
        made_progress = False
        _log(f"🛟 handoff seguro entre {len(pool)} conta(s) no mesmo alvo: cada uma "
             f"extrai até ~1,5M e passa pra próxima (segue o alvo antes) · "
             + ("IP local" if use_local else "proxy por conta"))
        for i, prof in enumerate(pool):
            if _should_stop():
                _log("⏹ parado pelo usuário")
                break
            start_count = len(rows)
            # Troca de conta = troca de IP: cada conta assume numa ÉPOCA de saída
            # própria, pra não compartilhar IP exato com a anterior. No-op se a
            # rotação estiver desligada nos ajustes.
            await _rotate_ip_for(prof, _log, use_local)
            _log(f"▶ conta '{prof.get('name')}' assumindo (de {start_count} já coletados)")
            r = await spider.extract(
                prof, kind, target, limit,
                progress=_progress, should_stop=_should_stop, log=_log,
                start_max_id=cursor, seed_rows=rows, delay_ms=delay_ms,
                on_total=_on_total, use_local=use_local,
                shift_pages=_SPIDER_SAFE_PAGES_PER_ACCOUNT, checkpoint=_checkpoint)
            if r.get("blocked"):
                job["blocked"] = True
                u = _spider_mark_blocked(profile=prof)
                if u:
                    _log(f"⚠ '{u}' marcada como 'em suspensão' (possível bloqueio/desafio)")
            if r.get("error"):
                last_error = r.get("error")
                _log(f"⚠ conta '{prof.get('name')}' falhou: {str(last_error)[:120]} — tentando a próxima")
                continue
            rows = r.get("rows", rows)
            cursor = r.get("cursor")
            last_user = r.get("user") or last_user
            if len(rows) > start_count:
                made_progress = True
            job["rows"] = rows; job["count"] = len(rows); job["cursor"] = cursor
            if r.get("total") is not None:
                job["total"] = r.get("total")
            _save()   # resume-safe; soma no mesmo CSV
            if not cursor:
                _log(f"✓ alvo concluído — {len(rows)} registro(s)")
                break
            if limit and len(rows) >= limit:
                break
            if i < len(pool) - 1:
                _log(f"⏭ '{prof.get('name')}' atingiu ~1,5M (ou parou) — passando pra próxima conta")
                await asyncio.sleep(round(random.uniform(5, 12), 1))
            else:
                _log("✖ acabaram as contas da lista — pausado (dá pra continuar com mais contas depois)")
        return {"rows": rows, "cursor": cursor, "total": job.get("total"),
                "target": target, "kind": kind, "user": last_user,
                **({"error": last_error} if (not made_progress and last_error) else {})}

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
                res = await _failover()   # handoff seguro: ~1,5M por conta, passa pra próxima
            else:
                await _rotate_ip_for(profile, _log, use_local)   # IP novo para esta conta
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


class SpiderBatchRequest(BaseModel):
    """Lote 'alvos diferentes': pares conta↔alvo rodando em PARALELO (sem rodízio).
    Cada par vira um job single-account independente — não encadeia contas no mesmo
    alvo, então não há correlação/suspensão em cluster entre elas."""
    pairs: list = []                  # [{profile_id, target}, ...]
    kind: str = "followers"
    use_local: bool = False
    delay_ms: int = 2500
    limit: int = 0


@app.post("/spider/extract-batch")
async def spider_extract_batch(body: SpiderBatchRequest):
    launched = []
    seen_pid = set()
    for pr in (body.pairs or []):
        target = (pr.get("target") or "").strip()
        pid = pr.get("profile_id")
        if not target or not pid or pid in seen_pid:
            continue
        prof = get_profile(pid)
        if not prof:
            continue
        seen_pid.add(pid)
        job_id = _launch_spider_job(
            method="web", account=None, profile=prof, label=prof.get("name"),
            kind=body.kind, target=target, limit=body.limit, use_local=body.use_local,
            delay_ms=body.delay_ms, profile_id=pid, account_id=None, profiles=None)
        launched.append({"job_id": job_id, "target": target, "profile": prof.get("name")})
    if not launched:
        raise HTTPException(400, "Nenhum par válido (informe alvo + perfil logado)")
    return {"jobs": launched}


class SpiderResumeRequest(BaseModel):
    delay_ms: Optional[int] = None   # velocidade ATUAL do seletor; sobrepõe a salva na run
    profile_ids: Optional[list] = None  # web: CONTINUAR com OUTRA(s) conta(s) (handoff p/ conta fresca)
    use_local: Optional[bool] = None    # idem: modo de IP ATUAL ao trocar de conta
    account_id: Optional[str] = None    # app (instagrapi): CONTINUAR com OUTRA conta


@app.post("/spider/resume/{run_id}")
async def spider_resume(run_id: str, body: SpiderResumeRequest = SpiderResumeRequest()):
    """Continua uma execução que parou no meio (cursor salvo + linhas já coletadas).
    A velocidade vem do seletor ATUAL (body.delay_ms). body.profile_ids permite
    CONTINUAR com OUTRA(s) conta(s) (handoff manual: a conta original caiu, você passa
    pra uma conta fresca continuando do MESMO cursor — sem rodízio simultâneo).
    OBS: o cursor de seguidores expira com o tempo; trocar de conta não revive um
    cursor já morto (resume tem que ser logo após a conta cair)."""
    run = db.get_spider_run(run_id)
    if not run:
        raise HTTPException(404, "Execução não encontrada")
    if not run.get("cursor"):
        raise HTTPException(400, "Esta execução já está completa — nada para continuar.")
    method = (run.get("method") or "web").lower()
    a = p = None
    profile_id = run.get("profile_id")
    if method == "app":
        # Override de conta: continua com a conta marcada (body.account_id); senão a da run.
        aid = body.account_id or run.get("account_id")
        a = db.get_account(aid) if aid else None
        if not a:
            raise HTTPException(404, "Conta não encontrada — marque a conta com que quer continuar.")
        if not (a.get("username") and a.get("password")):
            raise HTTPException(400, "A conta precisa de usuário e senha cadastrados (modo app/instagrapi).")
        label = a.get("username")
        pool = None
    else:
        # Override de conta(s): continua com as que o usuário marcou; senão, as da run.
        if body.profile_ids:
            pool = [pr for pr in (get_profile(i) for i in body.profile_ids) if pr]
            if not pool:
                raise HTTPException(404, "Conta(s) selecionada(s) não encontrada(s) — marque um perfil logado.")
        else:
            pool = _spider_pool_from_run(run)
            if not pool:
                raise HTTPException(404, "Perfil original não existe mais — marque a conta com que quer continuar.")
        p = pool[0]
        profile_id = p.get("id")
        label = f"{len(pool)} contas (rodízio)" if len(pool) > 1 else p.get("name")
    try:
        seed_rows = json.loads(run.get("rows_json") or "[]")
    except Exception:
        seed_rows = []
    job_id = _launch_spider_job(
        method=method, account=a, profile=p, label=label, kind=run.get("kind") or "followers",
        target=run.get("target"), limit=int(run.get("limit_n") or 0),
        use_local=(body.use_local if body.use_local is not None else bool(run.get("use_local"))),
        delay_ms=(body.delay_ms if body.delay_ms else int(run.get("delay_ms") or 2500)),
        profile_id=profile_id, account_id=(a.get("id") if a else run.get("account_id")),
        profiles=(pool if method != "app" and pool and len(pool) > 1 else None),
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


# ── Perfis monitorados ─────────────────────────────────────────────────────────

class MonitoredRequest(BaseModel):
    username: str
    followers_total: Optional[int] = None   # opcional: total conhecido (senão o Spider preenche)
    notes: Optional[str] = None


@app.get("/monitored")
def monitored_list():
    """Lista os perfis monitorados + progresso de extração (de spider_runs por alvo)."""
    items = []
    tot_followers = tot_extracted = 0
    for m in db.list_monitored():
        # extraído = o MAIOR entre a maior run isolada e o "extraído conhecido"
        # (união deduplicada de várias runs/CSVs, quando setado) — evita subestimar.
        extracted = max(db.spider_extracted_for(m.get("username")),
                        int(m.get("extracted_known") or 0))
        total = m.get("followers_total")
        pct = (round(100 * extracted / total, 1) if total else None)
        tot_followers += int(total or 0)
        tot_extracted += int(extracted or 0)
        items.append({**m, "extracted": extracted, "pct": pct})
    return {"items": items, "count": len(items),
            "total_followers": tot_followers, "total_extracted": tot_extracted,
            "resolving": _monitored_resolve["running"]}


@app.post("/monitored")
def monitored_add(body: MonitoredRequest):
    uname = (body.username or "").strip().lstrip("@")
    if not uname:
        raise HTTPException(400, "Informe o @usuário")
    m = db.add_monitored(uname, followers_total=body.followers_total, notes=body.notes)
    if not m:
        raise HTTPException(400, "Usuário inválido")
    return m


@app.delete("/monitored/{mid}")
def monitored_delete(mid: str):
    db.delete_monitored(mid)
    return {"ok": True}


_monitored_csv_cache = {}   # username -> (assinatura dos CSVs, nº de únicos) — evita re-contar à toa


def _scan_monitored_csv(username: str):
    """Conta seguidores ÚNICOS (por User Id) no MAIOR CSV da pasta data/<username>/.
    Usado pra refletir CSVs que você jogou na pasta (ex.: export do mobiliza).
    Cacheia por assinatura (nome+tamanho+mtime) — só re-conta se o arquivo mudou.
    Retorna o nº de únicos, ou None se não há pasta/CSV."""
    uname = spider.clean_username(username) or ""
    folder = SPIDER_DATA_DIR / re.sub(r"[^A-Za-z0-9_.-]", "_", uname)
    if not folder.exists():
        return None
    # Conta o MAIOR CSV da pasta (o mais completo) — rápido (1 arquivo) e cobre o
    # caso comum (você joga 1 CSV na pasta). Cacheia por assinatura: só re-conta se
    # esse arquivo mudou de tamanho/data.
    f = max(folder.glob("*.csv"), key=lambda p: p.stat().st_size, default=None)
    if not f:
        return None
    sig = (f.name, f.stat().st_size, int(f.stat().st_mtime))
    cached = _monitored_csv_cache.get(uname)
    if cached and cached[0] == sig:
        return cached[1]                      # nada mudou → usa o cache
    ids = set()
    try:
        with open(f, newline="", encoding="utf-8-sig") as fh:
            r = _csv.reader(fh)
            hdr = next(r, None)
            uid_col = hdr.index("User Id") if (hdr and "User Id" in hdr) else 1
            for row in r:
                if len(row) > uid_col:
                    v = (row[uid_col] or "").strip()
                    if v:
                        ids.add(v)
    except Exception:
        logger.exception("falha ao ler CSV de monitorado %s", f)
        return None
    n = len(ids)
    _monitored_csv_cache[uname] = (sig, n)
    return n


_monitored_resolve = {"running": False}


def _first_logged_profile():
    """Acha o 1º perfil com conta IG LOGADA (status active) — pra resolver totais."""
    for a in db.list_accounts():
        if a.get("status") == "active" and a.get("profile_id"):
            p = get_profile(a["profile_id"])
            if p:
                return p
    return None


@app.post("/monitored/resolve-totals")
async def monitored_resolve_totals():
    """Resolve o total REAL de seguidores (web_profile_info) dos monitorados que
    estão sem total — abre UM perfil logado em 2º plano e atualiza o banco."""
    if _monitored_resolve["running"]:
        return {"running": True, "already": True}
    missing = [m["username"] for m in db.list_monitored() if not m.get("followers_total")]
    if not missing:
        return {"running": False, "pending": 0}
    prof = _first_logged_profile()
    if not prof:
        raise HTTPException(400, "Nenhum perfil logado disponível. Abra/logue um perfil pra resolver os totais.")
    _monitored_resolve["running"] = True

    async def _run():
        try:
            res = await spider.resolve_totals(prof, missing, use_local=True)
            for u, tot in (res or {}).items():
                if tot:
                    db.set_monitored_total(u, tot)
        except Exception:
            logger.exception("falha ao resolver totais dos monitorados")
        finally:
            _monitored_resolve["running"] = False

    asyncio.create_task(_run())
    return {"running": True, "pending": len(missing)}


@app.post("/monitored/scan")
def monitored_scan():
    """Varre as pastas data/<perfil>/ e atualiza o 'extraído conhecido' de cada
    monitorado a partir dos CSVs ali (útil quando você adiciona um CSV na pasta)."""
    updated = []
    for m in db.list_monitored():
        n = _scan_monitored_csv(m.get("username"))
        if n and n != int(m.get("extracted_known") or 0):
            db.update_monitored(m["id"], extracted_known=n)
            updated.append({"username": m["username"], "extracted_known": n})
    return {"updated": updated}


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
    j["stopped_by_user"] = True   # parada manual → NÃO conta como falha/suspensão
    # o finally do worker chama _save() ao encerrar o loop → persiste o flag
    return {"ok": True}


SPIDER_DATA_DIR = Path(__file__).resolve().parent / "data"


def _save_spider_csv(content: str, target: str, fname: str) -> str:
    """Salva o CSV em data/<user_alvo>/<arquivo>.csv (ao lado do app) e devolve o
    caminho. Pasta por alvo p/ organizar várias extrações do mesmo perfil."""
    sub = spider.clean_username(target) or "sem_alvo"
    folder = SPIDER_DATA_DIR / re.sub(r"[^A-Za-z0-9_.-]", "_", sub)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / fname
    # newline="" → NÃO traduz \n: o csv já vem com \r\n; sem isso o write_text (modo
    # texto, Windows) viraria \r\r\n = uma linha EM BRANCO entre cada registro.
    path.write_text(content, encoding="utf-8-sig", newline="")   # BOM → abre certo no Excel
    return str(path)


def _run_outcome(r: dict) -> str:
    """Desfecho DERIVADO de um run (mesma regra do histórico da UI). Sem cursor (ou
    status 'done') = chegou ao FIM = completou. Falhou = bloqueio/erro OU parou no
    meio (cursor salvo) sem ter sido o usuário."""
    blocked = bool(r.get("blocked"))
    by_user = bool(r.get("stopped_by_user"))
    status = r.get("status")
    cursor = r.get("cursor")
    running = status in ("running", "needs_code")
    done = status == "done" or (not cursor and not blocked and status != "error" and not running)
    failed = blocked or status == "error" or (not by_user and not running and not done and bool(cursor))
    if running:
        return "rodando"
    if failed:
        return "falhou"
    if done:
        return "completou"
    if by_user:
        return "voce_parou"
    return status or "?"


def _run_account(r: dict) -> dict:
    """Conta de um run: por account_id (app) ou via perfil (web). Devolve o dict da
    conta (ou {} com o nome do perfil como fallback)."""
    aid = r.get("account_id")
    if aid:
        a = acc.get_account(aid)
        if a:
            return a
    pid = r.get("profile_id")
    if pid:
        a = db.get_account_by_profile(pid)
        if a:
            return a
    return {"username": r.get("profile") or ""}


def _duration_min(started: str, finished: str):
    if not started or not finished:
        return ""
    try:
        from datetime import datetime
        fmt = "%Y-%m-%d %H:%M:%S"
        d = (datetime.strptime(finished, fmt) - datetime.strptime(started, fmt)).total_seconds()
        return round(d / 60.0, 1) if d >= 0 else ""
    except Exception:
        return ""


@app.get("/spider/log-export")
def spider_log_export():
    """Exporta o LOG por extração (uma linha por run, com desfecho, req/h, conta, %,
    duração) — a base determinística p/ analisar CADA extração e ban caso a caso (por
    você ou por uma IA). Substitui a agregação cega das Métricas antigas. Salva em
    data/_analise/spider_log.csv e devolve o CSV."""
    from fastapi.responses import Response
    runs = db.spider_runs_export()
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["quando", "conta", "conta_criada_em", "conta_suspensoes", "perfil",
                "metodo", "conexao", "alvo", "tipo", "req_h", "extraidos", "total",
                "pct", "desfecho", "bloqueio", "voce_parou", "fim", "duracao_min",
                "tem_cursor", "erro"])
    for r in runs:
        delay = r.get("delay_ms") or 0
        req_h = round(3600000 / delay) if delay else ""
        total = r.get("total")
        cnt = r.get("count") or 0
        pct = (round(100 * cnt / total) if (total and total > 0) else "")
        a = _run_account(r)
        w.writerow([
            r.get("started_at") or "", a.get("username") or "",
            (a.get("created_at") or "")[:10], a.get("suspension_count") or 0,
            r.get("profile") or "", r.get("method") or "",
            ("local" if r.get("use_local") else "proxy"),
            (r.get("target") or "").lstrip("@"), r.get("kind") or "", req_h, cnt,
            (total if total is not None else ""), pct, _run_outcome(r),
            (1 if r.get("blocked") else 0), (1 if r.get("stopped_by_user") else 0),
            r.get("finished_at") or "", _duration_min(r.get("started_at"), r.get("finished_at")),
            (1 if r.get("cursor") else 0), (r.get("error") or "")[:200],
        ])
    content = buf.getvalue()
    saved = ""
    try:
        folder = SPIDER_DATA_DIR / "_analise"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "spider_log.csv"
        path.write_text(content, encoding="utf-8-sig", newline="")
        saved = str(path)
    except Exception:
        pass
    return Response(content=content, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=spider_log.csv",
                             "X-Saved-Path": saved})


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


# ─── Seguir: motor de follow rodando DENTRO de um perfil logado ───────────────
# Porte da extensão InstaFollow. Mesmo transporte do Spider (fetch autenticado no
# perfil Camoufox), então não há gerência de cookies/sessão aqui: cada perfil já é
# UMA conta com seu IP. Seguir é bem mais arriscado que ler — daí o modo simulação
# ligado por padrão e o preset Conservador.

_follow_jobs: dict[str, dict] = {}


class FollowHarvestRequest(BaseModel):
    profile_id: str
    source_type: str = "followers"        # followers | following | likers
    target: str = ""                      # compat: um alvo só
    targets: Optional[list] = None        # VÁRIOS alvos — alterna entre eles
    per_target: int = 50                  # fatia RASA de cada um (topo da lista)
    limit: int = 200                      # teto total da coleta
    use_local: bool = False
    run_id: Optional[str] = None          # ACUMULA na fila existente (várias fontes)


class FollowStartRequest(BaseModel):
    profile_id: str                       # conta que começa (ou única)
    profile_ids: Optional[list] = None    # VÁRIAS contas: dividem a mesma fila
    run_id: Optional[str] = None          # retomar uma execução existente
    use_local: bool = False
    settings: Optional[dict] = None
    filters: Optional[dict] = None


class FollowConfigRequest(BaseModel):
    settings: Optional[dict] = None
    filters: Optional[dict] = None


class FollowBlacklistRequest(BaseModel):
    username: str = ""


class UnfollowStartRequest(BaseModel):
    profile_id: str
    use_local: bool = False
    limit: int = 200                      # quantos desfazer nesta execução
    settings: Optional[dict] = None


def _follow_config() -> dict:
    """Ajustes + filtros salvos (settings do banco), sobre os defaults do módulo."""
    try:
        s = json.loads(db.get_setting("follow_settings", "") or "{}")
    except Exception:
        s = {}
    try:
        f = json.loads(db.get_setting("follow_filters", "") or "{}")
    except Exception:
        f = {}
    return {"settings": {**follower.DEFAULT_SETTINGS, **s},
            "filters": {**follower.DEFAULT_FILTERS, **f}}


def _ciclo_dados(quando=None) -> str:
    """Chave do CICLO de cobrança atual ('AAAA-MM-DD' = o dia em que ele começou).

    Não é o mês do calendário: o plano do usuário renova num dia qualquer (o dele
    virou dia 22). Contar por mês faria o medidor zerar em 1º de setembro enquanto o
    plano ainda tinha três semanas de uso acumulado — e a leitura ficaria otimista
    justo perto do teto, que é onde ela precisa ser certa."""
    try:
        dia = int(db.get_setting("data_plan_dia", "1") or 1)
    except Exception:
        dia = 1
    dia = max(1, min(28, dia))          # 28 evita o buraco de fevereiro
    t = time.localtime(quando if quando is not None else time.time())
    ano, mes = t.tm_year, t.tm_mon
    if t.tm_mday < dia:                 # ainda no ciclo aberto no mês passado
        mes -= 1
        if mes == 0:
            ano, mes = ano - 1, 12
    return f"{ano:04d}-{mes:02d}-{dia:02d}"


def _follow_dados_mes(quando=None) -> dict:
    """Consumo de dados acumulado por CICLO (chave = início do ciclo → bytes).

    Fica numa setting em vez de tabela: é um contador, não histórico — e assim não
    há migração. O plano do usuário é de 30 GB e o modem é a internet do PC inteiro,
    então este número é restrição de projeto."""
    try:
        d = json.loads(db.get_setting("follow_data_usage", "") or "{}")
    except Exception:
        d = {}
    return d if isinstance(d, dict) else {}


def _follow_somar_dados(job, res, feitos, log=None):
    """Acumula os bytes MEDIDOS de um turno no total do mês e conta no terminal."""
    b = int(res.get("dados_bytes") or 0)
    if b <= 0:
        return
    mes = _ciclo_dados()
    d = _follow_dados_mes()
    d[mes] = int(d.get(mes) or 0) + b
    try:
        db.set_setting("follow_data_usage", json.dumps(d))
    except Exception:
        logger.exception("follow: falha ao gravar consumo de dados")
    job["dados_mb_turno"] = res.get("dados_mb")
    job["dados_gb_mes"] = round(d[mes] / (1024 ** 3), 2)
    job["dados_ciclo"] = mes
    if log:
        por = f" · ~{(b / 1024 / max(1, feitos)):.0f} KB por follow" if feitos else ""
        log(f"📶 este turno gastou {res.get('dados_mb')} MB{por} · "
            f"{job['dados_gb_mes']} GB no mês")


def _idade_da_conta_em_dias(profile: dict):
    """Há quantos dias esta conta existe. `None` quando não dá para saber.

    Usa a MENOR data entre o cadastro e o primeiro follow registrado: se a conta foi
    criada no celular e cadastrada aqui depois, o primeiro follow é a evidência mais
    antiga que temos, e considerá-la mais nova do que é seria injusto com ela."""
    datas = []
    try:
        conta = db.get_account_by_profile(profile.get("id")) or {}
        if conta.get("created_at"):
            datas.append(str(conta["created_at"])[:10])
    except Exception:
        conta = {}
    try:
        u = (conta or {}).get("username")
        if u:
            primeiro = db.follow_history_first_ts(u)
            if primeiro:
                datas.append(time.strftime("%Y-%m-%d", time.localtime(primeiro / 1000)))
    except Exception:
        pass
    if not datas:
        return None
    try:
        d0 = min(datas)
        nasceu = time.mktime(time.strptime(d0, "%Y-%m-%d"))
        hoje = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        return int(round((hoje - nasceu) / 86400)) + 1     # dia do cadastro = dia 1
    except Exception:
        return None


def _quarentena_ler() -> dict:
    """{account_key: {"ate": epoch, "motivo": str}} — contas em descanso forçado."""
    try:
        d = json.loads(db.get_setting("follow_quarentena", "") or "{}")
    except Exception:
        d = {}
    return d if isinstance(d, dict) else {}


def _quarentena_por(key: str, dias: int, motivo: str):
    """Põe a conta de molho por `dias`.

    MOTIVO (2026-08-27): a `10122.cap.martim` devolvia 404 no follow desde 22/08 —
    e aquele 404 constante era, na prática, um freio. Quando um conserto nosso a
    fez voltar a agir, ela emplacou 81 follows em 23 minutos e foi **banida três
    horas depois**. Conta que responde "não" precisa ser deixada em paz, não
    retestada a cada volta do ródizio: insistir é o que transforma bloqueio de ação
    em banimento."""
    if not key or dias <= 0:
        return
    q = _quarentena_ler()
    q[key] = {"ate": int(time.time() + dias * 86400), "motivo": motivo}
    try:
        db.set_setting("follow_quarentena", json.dumps(q))
    except Exception:
        logger.exception("follow: falha ao gravar quarentena")


def _quarentena_ativa(key: str):
    """Segundos que faltam, ou 0 se a conta está liberada."""
    info = _quarentena_ler().get(key) or {}
    falta = int(info.get("ate") or 0) - int(time.time())
    return falta if falta > 0 else 0


def _follow_account_key(profile: dict) -> str:
    """Chave do limitador: a CONTA logada no perfil (o teto é por conta, que é o que
    faz a rotação entre perfis valer a pena). Cai no id do perfil se não houver conta."""
    try:
        a = db.get_account_by_profile(profile.get("id")) if profile.get("id") else None
        if a and a.get("username"):
            return a["username"].lower()
    except Exception:
        pass
    return f"profile:{profile.get('id')}"


def _follow_podar_fila(job: dict, manter_recentes: int = 150) -> int:
    """Tira da fila o que já foi seguido/solicitado. Devolve quantos saíram.

    A fila era append-only: o que foi processado ficava nela para sempre e cada nova
    coleta empilhava por cima. Medido em 2026-08-21 na instância do usuário: **4.997
    itens, 4.138 já processados**, e um `queue_json` de **4,65 MB regravado no banco a
    cada follow** (o `_follow_save` roda em toda ação). Além do desperdício, a tela
    dizia "859 pendentes de 4997", que não é informação útil.

    Poda só o que está DURAVELMENTE registrado em `follow_history` — `followed` e
    `requested`. `skipped` e `failed` ficam: eles não vão para o histórico global,
    então removê-los faria a próxima coleta trazer a mesma gente de volta. Guarda os
    `manter_recentes` mais novos para a tabela da tela não ficar vazia."""
    fila = job.get("queue") or []
    concluidos = [i for i, a in enumerate(fila)
                  if a.get("status") in ("followed", "requested")]
    if len(concluidos) <= manter_recentes:
        return 0
    # A fila é cronológica (coleta acrescenta no fim, o motor consome do começo),
    # então os índices menores são os mais antigos.
    remover = set(concluidos[:-manter_recentes])
    # Guarda os NOMES podados: a coleta usa a fila para não repetir ninguém, e sem
    # isto podar reabriria a porta para recoletar quem já foi seguido. É uma lista de
    # strings — ordens de grandeza mais leve que os itens completos.
    podados = job.setdefault("podados", [])
    podados.extend(fila[i].get("username") for i in remover if fila[i].get("username"))
    job["queue"] = [a for i, a in enumerate(fila) if i not in remover]
    return len(remover)


def _follow_save(job: dict):
    try:
        db.follow_run_upsert({
            "id": job["id"], "profile": job.get("profile"), "profile_id": job.get("profile_id"),
            "account_id": job.get("account_id"), "account_key": job.get("account_key"),
            "use_local": job.get("use_local"), "dry_run": job.get("dry_run"),
            "source_type": job.get("source_type"), "source_target": job.get("source_target"),
            "status": job.get("status"), "error": job.get("error"),
            "followed": job["stats"].get("followed"), "requested": job["stats"].get("requested"),
            "skipped": job["stats"].get("skipped"), "failed": job["stats"].get("failed"),
            "simulated": job["stats"].get("simulated"),
            "mode": job.get("mode", "follow"), "unfollowed": job["stats"].get("unfollowed"),
            "queue_json": json.dumps(job.get("queue") or []),
            "settings_json": json.dumps(job.get("settings") or {}),
            "filters_json": json.dumps(job.get("filters") or {}),
            "blocked": job.get("blocked"), "stopped_by_user": job.get("stopped_by_user"),
            "started_at": job.get("started_at"), "finished_at": job.get("finished_at")})
    except Exception:
        logger.exception("falha ao salvar follow run")


def _follow_dry_padrao() -> bool:
    """Simulacao conforme a configuracao salva. Em duvida, True: melhor um job
    nascer marcado como simulacao e o `follow_start` liberar do que o contrario."""
    try:
        return (_follow_config()["settings"].get("dryRun") is True)
    except Exception:
        return True


def _follow_new_job(profile: dict, *, source_type="", source_target="", use_local=False,
                    job_id=None, queue=None, started_at=None) -> dict:
    job = {"id": job_id or uuid.uuid4().hex[:12],
           "status": "idle", "profile": profile.get("name"), "profile_id": profile.get("id"),
           "account_id": None, "account_key": _follow_account_key(profile),
           # Nasce com o valor REAL da config. Antes era True chumbado, e entre a
           # coleta e o inicio do follow o painel anunciava "SIMULACAO" numa
           # execucao que ia seguir de verdade.
           "use_local": use_local, "dry_run": _follow_dry_padrao(), "mode": "follow",
           "source_type": source_type, "source_target": source_target,
           "queue": queue or [], "log": [], "error": None, "stop": False,
           "stats": {"followed": 0, "requested": 0, "skipped": 0, "failed": 0, "simulated": 0},
           "counts": {"hour": 0, "day": 0}, "blocked": False, "stopped_by_user": False,
           "settings": {}, "filters": {},
           "started_at": started_at or time.strftime("%Y-%m-%d %H:%M:%S"),
           "finished_at": None, "profile_ids": [profile.get("id")]}
    try:
        a = db.get_account_by_profile(profile.get("id"))
        job["account_id"] = a.get("id") if a else None
    except Exception:
        pass
    _follow_jobs[job["id"]] = job
    return job


def _follow_log(job: dict):
    def _log(msg):
        job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(job["log"]) > 500:
            del job["log"][:len(job["log"]) - 500]
    return _log


@app.post("/follow/harvest")
async def follow_harvest(body: FollowHarvestRequest):
    """Coleta contas de uma fonte para a fila. Só LÊ listas — nada é seguido aqui."""
    prof = get_profile(body.profile_id)
    if not prof:
        raise HTTPException(404, "Perfil não encontrado")
    # Vários alvos (um por linha na UI); `target` continua aceito p/ um alvo só.
    alvos = [t.strip() for t in (body.targets or [body.target]) if t and t.strip()]
    if not alvos:
        raise HTTPException(400, "Informe ao menos um alvo (@usuário ou URL do post)")
    rotulo = alvos[0] if len(alvos) == 1 else f"{len(alvos)} alvos"

    cfg = _follow_config()
    # Com run_id de uma fila parada, ACUMULA nela (a extensão junta várias fontes na
    # mesma fila); sem ele, começa uma fila nova.
    job = _follow_jobs.get(body.run_id) if body.run_id else None
    if job is None and body.run_id:
        # Fila salva no banco (app foi reiniciado): reidrata para ACUMULAR nela.
        antigo = _follow_job_from_db(body.run_id)
        if antigo:
            job = _follow_new_job(prof, source_type=antigo.get("source_type") or "",
                                  source_target=antigo.get("source_target") or "",
                                  use_local=body.use_local, job_id=antigo["id"],
                                  queue=antigo.get("queue") or [])
    if job is not None and job.get("status") in ("running", "harvesting"):
        raise HTTPException(409, "Esta fila está em uso agora. Pare a operação antes de coletar.")
    if job is None:
        job = _follow_new_job(prof, source_type=body.source_type,
                              source_target=rotulo, use_local=body.use_local)
    else:
        job.update({"source_type": body.source_type, "source_target": rotulo,
                    "use_local": body.use_local, "error": None})
    job["status"] = "harvesting"
    job["settings"], job["filters"] = cfg["settings"], cfg["filters"]
    log = _follow_log(job)
    # Enxuga ANTES de coletar: sem isto a fila só crescia e cada coleta empilhava
    # por cima do que já tinha sido feito ("pega novos em vez de zerar", relatado
    # pelo usuário em 2026-08-21). Quem sai daqui continua no histórico global.
    podados = _follow_podar_fila(job)
    if podados:
        log(f"🧹 fila enxugada: {podados} já concluída(s) saíram "
            f"(seguem no histórico) · {len(job['queue'])} ficam")
    _follow_save(job)

    async def _worker():
        try:
            # per_target = fatia rasa por RODADA; quem divide o teto sem truncar (e
            # volta nos alvos até fechar o total) é o próprio harvest.
            por_alvo = max(1, int(body.per_target or 50))
            # Quem já conhecemos vai JUNTO para a coleta: assim o filtro acontece
            # durante e o total pedido é de gente NOVA (antes, pedir 1000 devolvia
            # 1000 brutos que viravam ~500 depois do filtro).
            hist = db.follow_history_map()
            black = set(db.list_follow_blacklist())
            conhecidos = set(black) | {a.get("username") for a in job["queue"] if a.get("username")}
            # Quem saiu da fila na poda continua conhecido — senão enxugar faria a
            # coleta trazer de volta gente que já foi seguida.
            conhecidos |= {u for u in (job.get("podados") or []) if u}
            if cfg["settings"].get("skipAlreadyAttempted"):
                conhecidos |= set(hist)
            res = await follower.harvest(
                prof, body.source_type, alvos, por_alvo,
                limit=body.limit, conhecidos=conhecidos, use_local=body.use_local,
                page_delay_ms=cfg["settings"]["harvestPageDelayMs"],
                jitter_percent=cfg["settings"]["jitterPercent"],
                should_stop=lambda: job.get("stop", False), log=log)
            if res.get("error"):
                job["error"] = res["error"]
                job["status"] = "error"
            else:
                # O filtro pesado já rodou DENTRO da coleta (conhecidos). Isto aqui é
                # só a rede de segurança contra corrida (algo entrou no histórico
                # enquanto coletávamos) — normalmente não descarta nada.
                known = {a.get("id") for a in job["queue"]}
                fresh = [a for a in res["items"]
                         if a["id"] not in known and a["username"] not in conhecidos]
                job["queue"].extend(fresh)
                job["status"] = "idle"
                pulados = res.get("ja_conhecidos") or 0
                log(f"✓ {len(fresh)} conta(s) NOVA(S) na fila, de {len(alvos)} alvo(s)"
                    + (f" · {pulados} já conhecida(s) puladas durante a coleta" if pulados else ""))
                if len(alvos) > 1:
                    log("   por alvo: " + " · ".join(f"{k}={v}" for k, v in (res.get("por_alvo") or {}).items()))
        except Exception as e:
            logger.exception("follow harvest")
            job["error"] = str(e)
            job["status"] = "error"
            log(f"✖ erro: {e}")
        finally:
            _follow_save(job)

    asyncio.create_task(_worker())
    return {"job_id": job["id"]}


@app.post("/follow/start")
async def follow_start(body: FollowStartRequest):
    """Inicia (ou retoma) o motor de follow sobre a fila do job/execução."""
    prof = get_profile(body.profile_id)
    if not prof:
        raise HTTPException(404, "Perfil não encontrado")

    busy = [j for j in _follow_jobs.values() if j.get("status") in ("running", "harvesting")]
    if busy:
        raise HTTPException(409, "Já existe uma operação de follow em andamento. "
                                 "Pare a atual antes de iniciar outra.")

    cfg = _follow_config()
    settings = {**cfg["settings"], **(body.settings or {})}
    filters = {**cfg["filters"], **(body.filters or {})}

    job = _follow_jobs.get(body.run_id) if body.run_id else None
    if job is None and body.run_id:
        run = db.get_follow_run(body.run_id)
        if not run:
            raise HTTPException(404, "Execução não encontrada")
        try:
            queue = json.loads(run.get("queue_json") or "[]")
        except Exception:
            queue = []
        job = _follow_new_job(prof, source_type=run.get("source_type") or "",
                              source_target=run.get("source_target") or "",
                              use_local=body.use_local, job_id=run["id"], queue=queue,
                              started_at=run.get("started_at"))
    if job is None:
        raise HTTPException(400, "Nada na fila — colete contas de uma fonte primeiro.")

    pending = [a for a in job["queue"] if a.get("status") == "pending"]
    if not pending:
        raise HTTPException(400, "A fila não tem contas pendentes.")

    job.update({"status": "running", "stop": False, "error": None, "finished_at": None,
                "use_local": body.use_local, "dry_run": settings.get("dryRun") is True,
                "settings": settings, "filters": filters, "stopped_by_user": False})
    log = _follow_log(job)

    # Pool de contas que vão DIVIDIR esta fila. Com 1 conta é o comportamento de
    # sempre; com várias, elas se revezam (ver _dividir abaixo).
    pool = [p for p in (get_profile(i) for i in (body.profile_ids or [])) if p] or [prof]
    if not any(p["id"] == prof["id"] for p in pool):
        pool.insert(0, prof)
    job["profile_ids"] = [p["id"] for p in pool]
    if len(pool) > 1:
        job["profile"] = f"{len(pool)} contas (revezando)"

    history = db.follow_history_map()
    blacklist = db.list_follow_blacklist()
    job["counts"] = follower.limiter_counts(
        db.list_follow_actions(job["account_key"], int((time.time() - 86400) * 1000)))

    def _fazedor(key):
        """on_action amarrado à conta que está agindo AGORA (o histórico precisa
        registrar QUEM seguiu, senão o unfollow depois não encontra o dono)."""
        def _on_action(acct, kind, ts):
            job["stats"][kind] = job["stats"].get(kind, 0) + 1
            if kind in ("followed", "requested"):
                try:
                    db.add_follow_history(acct["username"], ts, key, acct.get("id"),
                                          acct.get("source"))
                    db.add_follow_action(key, ts)
                except Exception:
                    logger.exception("follow: falha ao gravar histórico/ação")
            _follow_save(job)
        return _on_action

    async def _dividir():
        """Reveza as contas na MESMA fila, em BLOCOS: cada conta faz até
        `followsPerAccount` follows e passa a vez para a próxima, em ciclo.

        Com 3 contas e bloco de 100, uma fila de 300 sai 100/100/100. O teto por
        hora/dia da conta continua valendo — vale o que vier primeiro. O histórico é
        GLOBAL, então duas contas nunca seguem a mesma pessoa."""
        # `nonlocal` porque a quarentena REATRIBUI o pool logo abaixo. Sem isto o
        # Python trata `pool` como local em toda a função e o primeiro `len(pool)`
        # estoura UnboundLocalError antes de qualquer turno rodar — foi o que dois
        # testes pegaram assim que a quarentena entrou.
        nonlocal pool
        centro_bloco = max(0, int(settings.get("followsPerAccount") or 0))

        # QUARENTENA: tira do rodízio quem está de molho, antes de qualquer coisa.
        # Sem isto a conta que recusa era retestada a cada volta — três follows
        # queimados por volta, e a insistência que precedeu o banimento de 27/08.
        if len(pool) > 1:
            liberados, presos = [], []
            for p in pool:
                falta = _quarentena_ativa(_follow_account_key(p))
                (presos if falta else liberados).append((p, falta))
            if presos and liberados:
                for p, falta in presos:
                    info = _quarentena_ler().get(_follow_account_key(p)) or {}
                    log(f"🚧 {_follow_account_key(p)} de molho por mais "
                        f"{follower.human_duration(falta)} — {info.get('motivo') or 'quarentena'}")
                pool = [p for p, _ in liberados]
            elif presos:
                # Todas de molho: prefere trabalhar com a que sai primeiro a parar tudo.
                presos.sort(key=lambda x: x[1])
                log(f"⚠ todas as contas estão de molho — usando {_follow_account_key(presos[0][0])}, "
                    f"a que sai primeiro")
                pool = [presos[0][0]]

        # ORDEM: começa pela conta mais DESCANSADA, não pela primeira da lista.
        #
        # O pool vinha na ordem dos checkboxes, então toda execução abria com a mesma
        # conta — ela levava a maior carga e "a conta X sempre abre" é um padrão por si.
        # Ordena por menos ações nas últimas 24h e, no empate, por quem está parada há
        # mais tempo. Como quem trabalha sobe no contador, a ordem se reveza sozinha
        # nas execuções seguintes, sem precisar guardar estado.
        if len(pool) > 1:
            try:
                resumo = db.follow_actions_resumo(int((time.time() - 86400) * 1000))
                pool.sort(key=lambda p: resumo.get(_follow_account_key(p), (0, 0)))
                ordem = " → ".join(
                    f"{_follow_account_key(p)}({resumo.get(_follow_account_key(p), (0, 0))[0]}/24h)"
                    for p in pool)
                log(f"🔀 ordem por descanso: {ordem}")
            except Exception:
                logger.exception("follow: falha ao ordenar o pool por descanso")

        if len(pool) > 1:
            log(f"👥 {len(pool)} contas revezando"
                + (f", trocando a cada ~{centro_bloco} follow(s)" if centro_bloco else
                   " (cada uma até o próprio teto)"))
        idx, sem_progresso, turno, contas_404 = 0, 0, 0, 0
        while not job.get("stop"):
            if not any(a.get("status") == "pending" for a in job["queue"]):
                log("✓ fila esvaziada")
                break
            if sem_progresso >= len(pool):
                log("⏹ todas as contas estão no teto — nada a fazer agora")
                break

            # Fechou uma volta (todas as contas ja tiveram a vez) e ainda ha fila:
            # respiro extra antes de recomecar pela primeira.
            extra = max(0, int(settings.get("restAfterRoundMin") or 0))
            if extra and idx and len(pool) > 1 and idx % len(pool) == 0:
                log(f"😴 todas as {len(pool)} contas passaram a vez — descanso extra de "
                    f"{extra} min antes de recomeçar")
                job["descansou_volta"] = True
                # Publica ATE QUANDO vai dormir: sem isto a tela fica uma hora muda,
                # que e o que faz parecer travado.
                job["descanso_ate"] = int(time.time() + extra * 60)
                parado = await spider._sleep_stop(extra * 60, lambda: job.get("stop", False))
                job["descanso_ate"] = None
                if parado:
                    break

            p = pool[idx % len(pool)]
            idx += 1
            key = _follow_account_key(p)
            job["account_key"] = key
            job["profile_atual"] = p.get("name")

            acoes = db.list_follow_actions(key, int((time.time() - 86400) * 1000))
            # RAMPA: conta recém-nascida tem teto diário menor, que cresce com a
            # idade. Medido em 2026-08-22 — o que separou quem caiu de quem sobreviveu
            # não foi o volume total, foi o volume no PRIMEIRO dia de vida. Entra como
            # `maxPerDay` desta conta, então o limitador que já existe faz o resto:
            # com várias contas ela passa a vez, com uma só ela espera.
            s_conta = settings
            if settings.get("rampaAtiva") is not False:
                dias = _idade_da_conta_em_dias(p)
                teto = comportamento.teto_por_idade(dias)
                if teto:
                    atual = int(settings.get("maxPerDay") or 0)
                    # Respeita um teto manual ainda menor, se houver.
                    s_conta = {**settings, "maxPerDay": min(teto, atual) if atual else teto}
                    log(f"🌱 {comportamento.descrever_rampa(dias, teto)}")
            cabe, motivo, espera = follower.limiter_check(acoes, s_conta)
            if not cabe:
                # Guarda QUANTO falta para esta conta voltar. O ciclo usa o menor
                # valor entre as contas para dormir exatamente ate a primeira
                # liberar, em vez de acordar de tempo em tempo no escuro.
                ate = job.get("espera_teto_s")
                job["espera_teto_s"] = espera if ate is None else min(ate, espera)
                log(f"⏭ conta {key} descansando — no teto {motivo}, "
                    f"volta em {follower.human_duration(int(espera))}")
                sem_progresso += 1
                continue
            job["espera_teto_s"] = None

            # Bloco de tamanho VARIÁVEL: com 100 fixo, todo turno terminava no mesmo
            # número redondo — assinatura de bot tão boa quanto o intervalo fixo.
            por_conta = comportamento.tamanho_do_bloco(
                centro_bloco, settings.get("blocoVariacaoPct", 25))
            turno += 1
            if len(pool) > 1:
                log(f"▶ turno {turno}: conta {key}"
                    + (f" (até {por_conta} follows)" if por_conta else ""))
            # Conta nova assumindo = IP novo (mesma regra do Spider: só rotaciona
            # se esta conta sai pelo modem, e só se estiver ligado nos ajustes).
            await _rotate_ip_for(p, log, body.use_local)

            antes = job["stats"].get("followed", 0) + job["stats"].get("requested", 0)
            res = await follower.run(
                p, job["queue"], s_conta, filters,
                history=history, blacklist=blacklist, actions=acoes,
                use_local=body.use_local, max_actions=por_conta,
                yield_on_cap=len(pool) > 1,   # com várias contas, teto = passa a vez
                should_stop=lambda: job.get("stop", False), log=log,
                chave_conta=key,             # dá a esta conta o seu horário de dormir
                on_action=_fazedor(key), on_counts=lambda c: job.update({"counts": c}))
            feitos = (job["stats"].get("followed", 0) + job["stats"].get("requested", 0)) - antes
            job["error"] = res.get("error")
            _follow_somar_dados(job, res, feitos, log)

            if res.get("blocked"):
                job["blocked"] = True
                u = _spider_mark_blocked(
                    profile=p, reason="InstaFollow: o Instagram bloqueou a ação (feedback_required)")
                if u:
                    log(f"⚠ conta @{u} marcada como 'Em suspensão'")
                sem_progresso += 1
                if len(pool) > 1:
                    log("↪ passando para a próxima conta")
                continue
            # 404 em tudo NESTA conta: passa para a próxima em vez de encerrar a
            # campanha. Se a próxima seguir normalmente, o problema era a conta; se
            # todas derem 404, o resumo no fim diz que é sistêmico.
            if res.get("conta_404"):
                contas_404 += 1
                # De molho: insistir numa conta que responde "não" foi o que precedeu
                # o banimento da 10122.cap.martim em 2026-08-27.
                dias_q = max(0, int(settings.get("quarentenaDias") or 3))
                if dias_q:
                    _quarentena_por(key, dias_q, "404 no follow (bloqueio de ação)")
                    log(f"🚧 {key} entra em quarentena por {dias_q} dia(s) — insistir "
                        f"numa conta que recusa é o que vira banimento")
                log(f"↪ testando a próxima conta para saber se o 404 é desta conta ou geral")
                if contas_404 >= len(pool):
                    job["error"] = ("todas as contas deram 404 no follow (a leitura funciona) — "
                                    "não é a conta: é a rota/permissão de escrita. "
                                    "A fila está intacta.")
                    log(f"✖ {job['error']}")
                    break
                continue
            if res.get("aborted"):
                break            # sessão caída / falhas seguidas: não insiste com outra
            # Turno rendeu? Zera o contador de "ninguém consegue trabalhar".
            sem_progresso = 0 if feitos else sem_progresso + 1
            if len(pool) == 1 and not res.get("turno_cheio"):
                break            # conta única já percorreu a fila inteira
        return

    async def _worker():
        try:
            await _dividir()
            job["status"] = "stopped" if job.get("stop") else (
                "error" if job.get("error") else "done")
        except Exception as e:
            logger.exception("follow run")
            job["error"] = str(e)
            job["status"] = "error"
            log(f"✖ erro: {e}")
        finally:
            job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _follow_save(job)

    _follow_save(job)
    asyncio.create_task(_worker())
    return {"job_id": job["id"], "pending": len(pending), "dry_run": job["dry_run"],
            "contas": [_follow_account_key(p) for p in pool]}


class FollowCycleRequest(BaseModel):
    profile_id: str
    profile_ids: Optional[list] = None
    source_type: str = "followers"
    targets: Optional[list] = None
    limit: int = 200                      # quantas contas NOVAS por coleta
    use_local: bool = False
    settings: Optional[dict] = None
    filters: Optional[dict] = None
    fila_minima: int = 20                 # abaixo disto, coleta mais antes de seguir
    run_id: Optional[str] = None          # APROVEITA a fila ja coletada, em vez de refazer
    espera_teto_min: int = 20             # quanto dormir quando todas as contas no teto
    piso_entre_rodadas_s: float = 10      # respiro minimo entre uma rodada e a proxima


# Estado do ciclo. Um por processo: rodar dois em paralelo brigaria pelos mesmos
# perfis e pelo mesmo teto por conta.
_follow_cycle = {"running": False, "stop": False, "rodada": 0, "job_id": None,
                 "log": [], "started_at": None, "erro": None, "fase": "",
                 "total_seguidas": 0, "total_coletadas": 0, "proxima_em": None}


def _acoes_do_job(job) -> int:
    """Acoes efetivadas: seguidas + solicitadas + simuladas. As SOLICITADAS entram
    porque conta privada recebe pedido de follow -- e acao gasta, e a tela mostra."""
    st = (job or {}).get("stats") or {}
    return (int(st.get("followed") or 0) + int(st.get("requested") or 0)
            + int(st.get("simulated") or 0))


def _cycle_log(msg: str):
    _follow_cycle["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    if len(_follow_cycle["log"]) > 300:
        del _follow_cycle["log"][:len(_follow_cycle["log"]) - 300]
    print(f"[ciclo] {msg}", flush=True)


async def _esperar_job(job_id: str, *, timeout_s=86400) -> dict:
    """Espera um job de follow/coleta terminar. O ciclo precisa disso porque os
    endpoints devolvem na hora e o trabalho roda em background."""
    esperou = 0.0
    while esperou < timeout_s:
        j = _follow_jobs.get(job_id)
        if not j:
            return {}
        if j.get("status") not in ("running", "harvesting"):
            return j
        if _follow_cycle["stop"]:
            j["stop"] = True
            j["stopped_by_user"] = True
        await asyncio.sleep(2)
        esperou += 2
    return _follow_jobs.get(job_id) or {}


@app.post("/follow/cycle/start")
async def follow_cycle_start(body: FollowCycleRequest):
    """Liga o ciclo automatico: coleta, segue, volta a coletar, ate voce parar."""
    if _follow_cycle["running"]:
        raise HTTPException(409, "O ciclo automatico ja esta rodando.")
    busy = [j for j in _follow_jobs.values() if j.get("status") in ("running", "harvesting")]
    if busy:
        raise HTTPException(409, "Ha uma operacao de follow em andamento. Pare antes.")
    prof = get_profile(body.profile_id)
    if not prof:
        raise HTTPException(404, "Perfil nao encontrado")
    alvos = [t.strip() for t in (body.targets or []) if t and t.strip()]
    if not alvos:
        raise HTTPException(400, "Informe pelo menos um alvo para o ciclo coletar.")

    _follow_cycle.update({"running": True, "stop": False, "rodada": 0, "job_id": None,
                          "log": [], "erro": None, "fase": "iniciando",
                          "total_seguidas": 0, "total_coletadas": 0, "proxima_em": None,
                          "started_at": time.strftime("%Y-%m-%d %H:%M:%S")})

    async def _supervisor():
        # Aproveita a fila que ja existe. Sem isto o ciclo ignorava 1000 contas
        # coletadas e saia colhendo outras 1000 do zero -- regressao reportada.
        run_id = body.run_id or None
        if run_id and not _follow_jobs.get(run_id):
            reidratado = _follow_job_from_db(run_id)
            if reidratado:
                _follow_jobs[run_id] = reidratado
            else:
                run_id = None
        if run_id:
            pendentes = len([x for x in (_follow_jobs[run_id].get("queue") or [])
                             if x.get("status") == "pending"])
            # Enxuga o que ja foi feito ANTES de comecar: o ciclo reaproveita o job,
            # e sem isto a fila carregaria milhares de itens concluidos rodada apos
            # rodada (medido: 4.997 itens, 4.138 ja processados).
            podados = _follow_podar_fila(_follow_jobs[run_id])
            if podados:
                _cycle_log(f"fila enxugada: {podados} ja concluida(s) sairam "
                           f"(seguem no historico)")
            _cycle_log(f"aproveitando a fila ja coletada: {pendentes} pendente(s)")
            # Marca no terminal DO JOB que o ciclo religou. Reaproveitar o mesmo job
            # mantém o log anterior, e sem esta linha o usuário religa e vê o texto
            # velho parado na tela — indistinguível de "não reiniciou".
            # Blindado: é cosmético, e cosmético não pode derrubar o supervisor (um
            # job sem a chave `log` chegou a matar o ciclo antes de a 1ª rodada rodar).
            try:
                _follow_jobs[run_id].setdefault("log", [])
                _follow_log(_follow_jobs[run_id])(
                    f"═══ ciclo religado — retomando com {pendentes} pendente(s) ═══")
            except Exception:
                logger.exception("follow: falha ao marcar o religamento no log do job")
            # (o `stop` do job já é zerado pelo /follow/start, não repetir aqui)
        secas = 0
        try:
            _cycle_log(f"ciclo ligado - {len(alvos)} alvo(s), {body.limit} nova(s) por coleta")
            while not _follow_cycle["stop"]:
                _follow_cycle["rodada"] += 1
                _follow_cycle["job_id"] = run_id or _follow_cycle.get("job_id")
                rodada = _follow_cycle["rodada"]

                # ── 1. garantir fila ────────────────────────────────────────
                pend = 0
                if run_id and _follow_jobs.get(run_id):
                    pend = len([a for a in _follow_jobs[run_id]["queue"]
                                if a.get("status") == "pending"])
                if pend < max(1, int(body.fila_minima)):
                    _follow_cycle["fase"] = "coletando"
                    _cycle_log(f"rodada {rodada}: coletando (fila com {pend})")
                    try:
                        h = await follow_harvest(FollowHarvestRequest(
                            profile_id=body.profile_id, source_type=body.source_type,
                            targets=alvos, limit=int(body.limit),
                            use_local=body.use_local, run_id=run_id))
                    except HTTPException as e:
                        _follow_cycle["erro"] = f"coleta recusada: {e.detail}"
                        _cycle_log(f"x {_follow_cycle['erro']}")
                        break
                    run_id = h.get("job_id") or run_id
                    _follow_cycle["job_id"] = run_id
                    jh = await _esperar_job(run_id)
                    if _follow_cycle["stop"]:
                        _cycle_log("parado por voce durante a coleta")
                        break
                    novos = len([a for a in (jh.get("queue") or [])
                                 if a.get("status") == "pending"])
                    _follow_cycle["total_coletadas"] += max(0, novos - pend)
                    if novos <= pend:
                        secas += 1
                        _cycle_log(f"rodada {rodada}: a coleta nao trouxe gente nova "
                                   f"({secas}/2)")
                        # Alvos secos E fila vazia: girar de novo so gastaria
                        # requisicao relendo as mesmas listas.
                        if secas >= 2 and novos == 0:
                            _follow_cycle["erro"] = (
                                "os alvos secaram - ja seguimos praticamente todo mundo "
                                "deles. Acrescente alvos novos e ligue o ciclo de novo.")
                            _cycle_log(f"x {_follow_cycle['erro']}")
                            break
                    else:
                        secas = 0
                    pend = novos

                if pend <= 0:
                    _cycle_log(f"rodada {rodada}: fila vazia, tentando coletar de novo")
                    if await _dormir_ciclo(2):
                        break
                    continue

                # ── 2. seguir a fila ────────────────────────────────────────
                _follow_cycle["fase"] = "seguindo"
                antes = _acoes_do_job(_follow_jobs.get(run_id))
                _follow_cycle["_antes"] = antes
                try:
                    await follow_start(FollowStartRequest(
                        profile_id=body.profile_id, profile_ids=body.profile_ids,
                        run_id=run_id, use_local=body.use_local,
                        settings=body.settings, filters=body.filters))
                except HTTPException as e:
                    _follow_cycle["erro"] = f"nao consegui iniciar o follow: {e.detail}"
                    _cycle_log(f"x {_follow_cycle['erro']}")
                    break
                j = await _esperar_job(run_id)
                if _follow_cycle["stop"]:
                    # Credita o que a rodada JA fez antes de sair, senao o resumo
                    # anuncia 0 depois de dezenas de follows reais.
                    parcial = max(0, _acoes_do_job(j or _follow_jobs.get(run_id)) - antes)
                    _follow_cycle["total_seguidas"] += parcial
                    _follow_cycle["_antes"] = None
                    _cycle_log(f"parado por voce durante o follow "
                               f"({parcial} nesta rodada)")
                    break

                feitos = max(0, _acoes_do_job(j) - antes)
                _follow_cycle["total_seguidas"] += feitos
                _follow_cycle["_antes"] = None
                restam = len([a for a in (j.get("queue") or [])
                              if a.get("status") == "pending"])
                _cycle_log(f"rodada {rodada}: +{feitos} seguida(s) - {restam} na fila")

                # Bloqueio/sessao: nao insiste.
                if j.get("blocked") or (j.get("error") and not j.get("stopped_by_user")):
                    _follow_cycle["erro"] = j.get("error") or "bloqueio detectado"
                    _cycle_log(f"x ciclo encerrado: {_follow_cycle['erro']}")
                    break

                # ── 3. teto: dorme em vez de martelar ───────────────────────
                # Ninguem seguiu com fila cheia: ou as contas estao descansando (teto
                # diario) ou houve recusa. Se o motor disse quanto falta para a
                # primeira conta voltar, dorme exatamente isso -- e o "descanso ate
                # retornar". Senao, cai no intervalo configurado.
                if feitos == 0 and restam > 0:
                    ate = j.get("espera_teto_s")
                    if ate:
                        _cycle_log(f"contas descansando - voltando em "
                                   f"{follower.human_duration(int(ate))}")
                        if await _dormir_ciclo_s(float(ate) + 5):
                            break
                    else:
                        _cycle_log(f"nenhuma conta conseguiu seguir nesta rodada - "
                                   f"dormindo {body.espera_teto_min} min antes de tentar de novo")
                        if await _dormir_ciclo(int(body.espera_teto_min)):
                            break

                # Descanso extra de fim de volta. No ciclo, uma rodada normalmente E
                # uma volta (a fila acaba quando todas passaram), entao o respiro cai
                # aqui. Se o `_dividir` ja descansou na virada da volta, nao repete.
                # NAO usar `or` aqui: 0 e um valor VALIDO (desliga o descanso) e
                # `0 or 60` devolveria 60, ignorando quem pediu para emendar as voltas.
                extra = (body.settings or {}).get("restAfterRoundMin")
                if extra is None:
                    extra = follower.DEFAULT_SETTINGS.get("restAfterRoundMin", 0)
                extra = int(extra or 0)
                if extra and feitos and not j.get("descansou_volta"):
                    _cycle_log(f"todas as contas passaram a vez - descanso extra de "
                               f"{extra} min")
                    if await _dormir_ciclo(extra):
                        break
                if j:
                    j["descansou_volta"] = False

                # Respiro entre rodadas. Alem de nao martelar, garante um ponto de
                # yield: sem ele o supervisor segurava o event loop e o Parar nao
                # chegava a ser processado.
                if await _dormir_ciclo_s(body.piso_entre_rodadas_s):
                    break
        except Exception as e:
            logger.exception("ciclo de follow")
            _follow_cycle["erro"] = str(e)
            _cycle_log(f"x erro: {e}")
        finally:
            # Rede de seguranca: qualquer saida que tenha deixado uma rodada pela
            # metade ainda credita o que ela produziu.
            pend = _follow_cycle.pop("_antes", None)
            if pend is not None:
                _follow_cycle["total_seguidas"] += max(
                    0, _acoes_do_job(_follow_jobs.get(_follow_cycle.get("job_id"))) - pend)
            _follow_cycle["running"] = False
            _follow_cycle["fase"] = "parado"
            _follow_cycle["proxima_em"] = None
            # Sempre dizer COMO terminou: um "encerrado" seco fez o usuario achar
            # que estava quebrado quando tinha acabado de coletar 419 contas.
            if _follow_cycle.get("erro"):
                _cycle_log(f"ciclo encerrado: {_follow_cycle['erro']}")
            elif _follow_cycle["stop"]:
                _cycle_log(f"ciclo encerrado por voce apos {_follow_cycle['rodada']} rodada(s) "
                           f"- {_follow_cycle['total_seguidas']} seguida(s) no total")
            else:
                _cycle_log("ciclo encerrado")

    asyncio.create_task(_supervisor())
    return {"ok": True, "alvos": len(alvos)}


async def _dormir_ciclo_s(segundos: float) -> bool:
    """Espera interrompivel, em segundos. O `sleep(0)` inicial CEDE o controle mesmo
    com piso zero: sem ele, um supervisor cujas etapas retornam na hora monopoliza o
    event loop e nem o Parar consegue rodar (medido: 337 mil rodadas em 40s)."""
    await asyncio.sleep(0)
    fim = time.time() + max(0.0, float(segundos or 0))
    while time.time() < fim:
        if _follow_cycle["stop"]:
            return True
        await asyncio.sleep(min(1.0, max(0.05, fim - time.time())))
    return _follow_cycle["stop"]


async def _dormir_ciclo(minutos: int) -> bool:
    """Dorme em fatias para o Parar responder na hora. True = pararam."""
    fim = time.time() + max(0, minutos) * 60
    _follow_cycle["proxima_em"] = int(fim)
    _follow_cycle["fase"] = "esperando"
    while time.time() < fim:
        if _follow_cycle["stop"]:
            _follow_cycle["proxima_em"] = None
            return True
        await asyncio.sleep(2)
    _follow_cycle["proxima_em"] = None
    return _follow_cycle["stop"]


@app.post("/follow/cycle/stop")
def follow_cycle_stop():
    """Para o ciclo E o trabalho em andamento (o supervisor propaga o stop)."""
    _follow_cycle["stop"] = True
    jid = _follow_cycle.get("job_id")
    j = _follow_jobs.get(jid) if jid else None
    if j:
        j["stop"] = True
        j["stopped_by_user"] = True
    return {"ok": True}


@app.get("/follow/cycle/status")
def follow_cycle_status():
    faltam = None
    if _follow_cycle.get("proxima_em"):
        faltam = max(0, int(_follow_cycle["proxima_em"] - time.time()))
    return {**{k: v for k, v in _follow_cycle.items() if k != "log"},
            "log": _follow_cycle["log"][-60:], "faltam_s": faltam}


@app.post("/follow/stop/{job_id}")
def follow_stop(job_id: str):
    j = _follow_jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Job não encontrado")
    j["stop"] = True
    j["stopped_by_user"] = True
    return {"ok": True}


def _follow_job_from_db(run_id: str) -> dict:
    """Reconstrói um job a partir do banco. Necessário porque `_follow_jobs` é
    memória: ao fechar o app, a fila coletada existia em `queue_json` mas a tela
    não tinha como buscá-la de volta — parecia que os dados tinham sumido."""
    run = db.get_follow_run(run_id)
    if not run:
        return None
    try:
        fila = json.loads(run.get("queue_json") or "[]")
    except Exception:
        fila = []
    try:
        cfg_s = json.loads(run.get("settings_json") or "{}")
    except Exception:
        cfg_s = {}
    return {"id": run["id"], "status": run.get("status") or "idle",
            "error": run.get("error"), "dry_run": bool(run.get("dry_run")),
            "profile": run.get("profile"), "account_key": run.get("account_key"),
            "mode": run.get("mode") or "follow",
            "source_type": run.get("source_type"), "source_target": run.get("source_target"),
            "queue": fila, "log": [], "settings": cfg_s,
            "stats": {"followed": run.get("followed") or 0,
                      "requested": run.get("requested") or 0,
                      "skipped": run.get("skipped") or 0,
                      "failed": run.get("failed") or 0,
                      "simulated": run.get("simulated") or 0,
                      "unfollowed": run.get("unfollowed") or 0},
            "counts": {"hour": 0, "day": 0}, "from_db": True}


@app.get("/follow/jobs/{job_id}")
def follow_job(job_id: str):
    j = _follow_jobs.get(job_id) or _follow_job_from_db(job_id)
    if not j:
        raise HTTPException(404, "Job não encontrado")
    q = j.get("queue") or []
    return {"id": j["id"], "status": j["status"], "error": j.get("error"),
            "dry_run": j.get("dry_run"), "profile": j.get("profile"),
            "account_key": j.get("account_key"), "mode": j.get("mode", "follow"),
            "seguindo_antes": j.get("seguindo_antes"), "seguindo_depois": j.get("seguindo_depois"),
            "limite_ig": follower.IG_FOLLOWING_LIMIT,
            "source_type": j.get("source_type"), "source_target": j.get("source_target"),
            "stats": j.get("stats"), "counts": j.get("counts"),
            "queue_total": len(q),
            "queue_pending": sum(1 for a in q if a.get("status") == "pending"),
            "queue": q[:200],                       # amostra p/ a tabela da UI
            # Quantos segundos faltam do descanso de fim de volta (None = não está
            # descansando). A tela usa para a contagem regressiva.
            "descanso_faltam_s": (max(0, int(j["descanso_ate"] - time.time()))
                                  if j.get("descanso_ate") else None),
            "log": j.get("log", [])[-300:]}


@app.get("/follow/config")
def follow_config_get():
    cfg = _follow_config()
    cfg["presets"] = follower.PRESETS
    cfg["needs_enrichment"] = follower.needs_enrichment(cfg["filters"])
    cfg["active_filters"] = follower.describe_filters(cfg["filters"])
    cfg["history_count"] = db.follow_history_count()
    cfg["blacklist"] = db.list_follow_blacklist()
    return cfg


@app.post("/follow/config")
def follow_config_set(body: FollowConfigRequest):
    if body.settings is not None:
        db.set_setting("follow_settings", json.dumps(body.settings))
    if body.filters is not None:
        db.set_setting("follow_filters", json.dumps(body.filters))
    return follow_config_get()


@app.get("/follow/data-usage")
def follow_data_usage():
    """Consumo MEDIDO de dados no ciclo de cobrança atual. Substitui a estimativa da
    tela — que já errou duas vezes, por 50x e depois por 5x."""
    d = _follow_dados_mes()
    ciclo = _ciclo_dados()
    bytes_ciclo = int(d.get(ciclo) or 0)
    try:
        dia = max(1, min(28, int(db.get_setting("data_plan_dia", "1") or 1)))
    except Exception:
        dia = 1
    # Quantos dias faltam para o plano renovar — é o que decide se dá para gastar.
    ini = time.mktime(time.strptime(ciclo, "%Y-%m-%d"))
    passados = max(0, int((time.time() - ini) / 86400))
    return {
        "ciclo": ciclo,
        "mes": ciclo,                    # compat com quem já lia `mes`
        "dia_renovacao": dia,
        "dias_no_ciclo": passados,
        "bytes": bytes_ciclo,
        "gb": round(bytes_ciclo / (1024 ** 3), 2),
        "historico": {k: round(v / (1024 ** 3), 2) for k, v in sorted(d.items())},
    }


@app.post("/follow/data-usage/reset")
def follow_data_usage_reset():
    """Zera o contador e marca HOJE como o dia de renovação do plano.

    As duas coisas juntas de propósito: quem clica aqui acabou de renovar, e sem
    fixar o dia o medidor voltaria a zerar sozinho na virada do mês — dando leitura
    otimista justo perto do teto."""
    hoje = time.localtime()
    dia = max(1, min(28, hoje.tm_mday))
    db.set_setting("data_plan_dia", str(dia))
    d = _follow_dados_mes()
    ciclo = _ciclo_dados()
    antes = round(int(d.get(ciclo) or 0) / (1024 ** 3), 2)
    d[ciclo] = 0
    db.set_setting("follow_data_usage", json.dumps(d))
    logger.info("dados: contador zerado (era %.2f GB) · renovacao dia %s", antes, dia)
    return {"ok": True, "zerado_gb": antes, "ciclo": ciclo, "dia_renovacao": dia}


@app.get("/follow/history")
def follow_history(limit: int = 50):
    """Histórico persistente das execuções de follow."""
    return {"items": db.list_follow_runs(limit)}


@app.delete("/follow/history/{run_id}")
def follow_history_delete(run_id: str):
    db.delete_follow_run(run_id)
    _follow_jobs.pop(run_id, None)
    return {"ok": True}


def _unfollow_candidates(profile: dict, settings: dict, limit: int = 200) -> list:
    """Fila do unfollow: quem ESTA conta seguiu há ≥ N dias e ainda não foi desfeito.
    A whitelist já é aplicada no SQL; "só o que o app seguiu" é grátis (a fonte é o
    nosso próprio histórico); "quem retribuiu" é checado no motor (custa 1 req)."""
    dias = max(0, int(settings.get("unfollowAfterDays") or 0))
    corte = int((time.time() - dias * 86400) * 1000)
    key = _follow_account_key(profile)
    return [{"username": r["username"], "user_id": r.get("user_id"),
             "ts_ms": r.get("ts_ms"), "followed_at": r.get("followed_at"),
             "status": "pending", "note": "", "actedAt": None}
            for r in db.follow_unfollow_candidates(key, corte, limit)]


@app.get("/follow/unfollow/candidates")
def unfollow_candidates(profile_id: str, limit: int = 200):
    """Prévia: quantos dá para desfazer agora, e como está o teto de 7.500 do IG."""
    prof = get_profile(profile_id)
    if not prof:
        raise HTTPException(404, "Perfil não encontrado")
    cfg = _follow_config()
    key = _follow_account_key(prof)
    itens = _unfollow_candidates(prof, cfg["settings"], limit)
    stats = db.follow_following_stats(key)
    return {"account_key": key, "candidatos": len(itens),
            "amostra": itens[:50],
            "limite_ig": follower.IG_FOLLOWING_LIMIT,
            "unfollow_after_days": cfg["settings"].get("unfollowAfterDays"),
            "protect_mutuals": cfg["settings"].get("protectMutuals"),
            **stats}


@app.post("/follow/unfollow/start")
async def unfollow_start(body: UnfollowStartRequest):
    """Desfaz follows antigos — o que mantém espaço abaixo do teto de 7.500 do IG."""
    prof = get_profile(body.profile_id)
    if not prof:
        raise HTTPException(404, "Perfil não encontrado")
    busy = [j for j in _follow_jobs.values() if j.get("status") in ("running", "harvesting")]
    if busy:
        raise HTTPException(409, "Já existe uma operação em andamento. Pare a atual antes.")

    cfg = _follow_config()
    settings = {**cfg["settings"], **(body.settings or {})}
    itens = _unfollow_candidates(prof, settings, body.limit)
    if not itens:
        dias = settings.get("unfollowAfterDays")
        raise HTTPException(400, f"Nenhum candidato: ninguém que esta conta seguiu pelo app "
                                 f"completou {dias} dia(s) ainda (ou já foi desfeito).")

    job = _follow_new_job(prof, source_type="unfollow", source_target="desfazer follows antigos",
                          use_local=body.use_local, queue=itens)
    job.update({"status": "running", "stop": False, "mode": "unfollow",
                "dry_run": settings.get("dryRun") is True, "settings": settings})
    job["stats"] = {"unfollowed": 0, "skipped": 0, "failed": 0, "simulated": 0}
    log = _follow_log(job)

    key = job["account_key"]
    actions = db.list_follow_actions(key, int((time.time() - 86400) * 1000))
    job["counts"] = follower.limiter_counts(actions)
    viewer = key if not key.startswith("profile:") else ""

    def _on_action(item, kind, ts):
        job["stats"][kind] = job["stats"].get(kind, 0) + 1
        if kind == "unfollowed":
            try:
                db.mark_follow_unfollowed(item["username"], ts)
                db.add_follow_action(key, ts)     # gasta o mesmo orçamento do follow
            except Exception:
                logger.exception("unfollow: falha ao gravar histórico/ação")
        _follow_save(job)

    async def _worker():
        try:
            res = await follower.run_unfollow(
                prof, job["queue"], settings, viewer=viewer, use_local=body.use_local,
                actions=actions, should_stop=lambda: job.get("stop", False), log=log,
                on_action=_on_action, on_counts=lambda c: job.update({"counts": c}))
            job["error"] = res.get("error")
            job["blocked"] = bool(res.get("blocked"))
            job["seguindo_antes"] = res.get("seguindo_antes")
            job["seguindo_depois"] = res.get("seguindo_depois")
            job["status"] = "stopped" if job.get("stop") else (
                "error" if res.get("error") else "done")
            if job["blocked"]:
                u = _spider_mark_blocked(profile=prof,
                                         reason="InstaFollow: bloqueio de ação no unfollow")
                if u:
                    log(f"⚠ conta @{u} marcada como 'Em suspensão'")
        except Exception as e:
            logger.exception("unfollow run")
            job["error"] = str(e)
            job["status"] = "error"
            log(f"✖ erro: {e}")
        finally:
            job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _follow_save(job)

    _follow_save(job)
    asyncio.create_task(_worker())
    return {"job_id": job["id"], "candidatos": len(itens), "dry_run": job["dry_run"]}


class FollowDiscardRequest(BaseModel):
    run_id: str = ""


class FollowCaptureRequest(BaseModel):
    profile_id: str
    timeout: int = 300


@app.post("/follow/capture")
async def follow_capture(body: FollowCaptureRequest):
    """DIAGNÓSTICO: captura a requisição de follow que a INTERFACE do Instagram manda.

    Existe porque o nosso POST devolve 404 mesmo com o perfil logado, a família /api/v1/
    respondendo 200 para leitura e headers idênticos à chamada que funcionou em
    2026-06-21. Cinco hipóteses (body, id, sessão, conta, rota) morreram no teste. A
    interface consegue seguir — então medir o que ELA manda é mais barato e mais honesto
    que adivinhar o próximo header.

    Abre o perfil VISÍVEL, instala um espião no fetch/XHR e espera o usuário seguir
    alguém na mão. Devolve método/URL/headers/corpo e salva um relatório em disco."""
    p = db.get_profile(body.profile_id)
    if not p:
        raise HTTPException(404, "perfil não encontrado")
    # Mesma guarda do launch: não roubar o perfil de uma extração/campanha em andamento.
    for j in list(_spider_jobs.values()):
        if j.get("status") == "running" and body.profile_id in (j.get("profile_ids") or []):
            raise HTTPException(409, "há uma extração rodando com este perfil — pare antes")
    for j in list(_follow_jobs.values()):
        if j.get("status") in ("running", "harvesting") and body.profile_id in (j.get("profile_ids") or []):
            raise HTTPException(409, "há uma campanha rodando com este perfil — pare antes")

    linhas = []
    def _log(m):
        linhas.append(m)
        print(f"[capture] {m}", flush=True)

    res = await follower.capture_follow(p, log=_log,
                                        timeout=max(30, min(900, int(body.timeout or 300))))
    relatorio = {"perfil": p.get("name"), "ok": res.get("ok"),
                 "erro": res.get("erro"), "capturas": res.get("capturas"),
                 "headers_ui": res.get("headers_ui"), "log": linhas}
    try:
        d = SPIDER_DATA_DIR / "_analise"
        d.mkdir(parents=True, exist_ok=True)
        alvo = d / "follow_capture.json"
        alvo.write_text(json.dumps(relatorio, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        relatorio["salvo_em"] = str(alvo)
    except Exception as e:
        relatorio["salvo_em"] = f"(não consegui salvar: {e})"
    return relatorio


@app.post("/follow/queue/discard")
def follow_queue_discard(body: FollowDiscardRequest):
    """ZERA a fila atual SEM SALVAR: para o job, apaga a execução do banco (ela não
    fica no histórico) e limpa a memória. Serve para descartar uma coleta que não
    presta e começar do zero — diferente de 'Começar fila nova', que só solta a tela
    e deixa o registro no histórico."""
    rid = (body.run_id or "").strip()
    j = _follow_jobs.get(rid)
    if j:
        j["stop"] = True                  # se estiver rodando, encerra
        j["queue"] = []
    apagado = False
    if rid:
        try:
            apagado = bool(db.get_follow_run(rid))
            db.delete_follow_run(rid)     # não deixa rastro no histórico
        except Exception:
            logger.exception("falha ao apagar a execução descartada")
    _follow_jobs.pop(rid, None)
    return {"ok": True, "apagado": apagado}


class FollowBackCheckRequest(BaseModel):
    profile_id: str = ""
    # Cobre TODOS por padrao: o custo agora e a lista de seguidores da conta,
    # que nao muda com o numero de pessoas conferidas.
    limit: int = 100000


@app.post("/follow/check-followback")
async def follow_check_followback(body: FollowBackCheckRequest):
    """Confere quem retribuiu o follow e grava o resultado nas metricas.

    So LEITURA: usa `friendships/show_many` -- a mesma rota que a interface chama
    ao abrir uma lista de seguidores (medido na captura de 2026-08-18), o que
    permite conferir ~50 pessoas por requisicao em vez de uma a uma.

    Roda POR PERFIL: cada perfil so pode perguntar sobre as pessoas que ELE
    seguiu (quem segue quem e uma relacao entre aquela conta e a pessoa)."""
    perfis = []
    if body.profile_id:
        p = db.get_profile(body.profile_id)
        if not p:
            raise HTTPException(404, "perfil nao encontrado")
        perfis = [p]
    else:
        # Sem perfil escolhido, confere todos os que tem conta logada.
        for p in db.list_profiles():
            a = db.get_account_by_profile(p["id"])
            if a and (a.get("status") == "active"):
                perfis.append(p)
    if not perfis:
        raise HTTPException(400, "nenhum perfil com conta logada")

    # Nao roubar o perfil de uma campanha/extracao em andamento.
    ocupados = set()
    for j in list(_spider_jobs.values()):
        if j.get("status") == "running":
            ocupados.update(j.get("profile_ids") or [])
    for j in list(_follow_jobs.values()):
        if j.get("status") in ("running", "harvesting"):
            ocupados.update(j.get("profile_ids") or [])

    agora = int(time.time() * 1000)
    linhas, total_ver, total_volta = [], 0, 0
    for p in perfis:
        if p["id"] in ocupados:
            linhas.append({"perfil": p.get("name"), "erro": "em uso por outra operacao"})
            continue
        conta = db.get_account_by_profile(p["id"])
        chave = (conta or {}).get("username") or ""
        cands = db.follow_followback_candidates(by_account=chave,
                                                limit=max(1, min(200000, int(body.limit or 100000))))
        if not cands:
            linhas.append({"perfil": p.get("name"), "conta": chave, "verificados": 0,
                           "seguiram_de_volta": 0, "nota": "ninguem novo para conferir"})
            continue
        r = await follower.checar_seguiram_de_volta(p, cands, log=None)
        if r.get("resultados"):
            db.mark_followback(r["resultados"], agora)
        total_ver += r.get("verificados") or 0
        total_volta += r.get("retribuiram") or 0
        linhas.append({"perfil": p.get("name"), "conta": chave,
                       "verificados": r.get("verificados") or 0,
                       "seguiram_de_volta": r.get("retribuiram") or 0,
                       "erro": r.get("erro")})
    return {"ok": True, "verificados": total_ver, "seguiram_de_volta": total_volta,
            "por_perfil": linhas, **db.follow_followback_totals()}


@app.get("/follow/metrics")
def follow_metrics():
    """Métricas do InstaFollow: total seguido, quanto cada CONTA está segurando do
    teto de 7.500, quanto já foi desfeito, e quais alvos renderam mais.

    'ainda_seguindo' é o número que importa para o teto — seguidos menos desfeitos."""
    agora = int(time.time() * 1000)
    contas = []
    for row in db.follow_metrics_by_account():
        key = row.get("by_account") or "(sem conta)"
        seg = int(row.get("ainda_seguindo") or 0)
        contas.append({
            "conta": key,
            "seguidos": int(row.get("seguidos") or 0),
            "ainda_seguindo": seg,
            "desfeitos": int(row.get("desfeitos") or 0),
            "vagas_ate_o_teto": max(0, follower.IG_FOLLOWING_LIMIT - seg),
            "pct_do_teto": round(seg * 100.0 / follower.IG_FOLLOWING_LIMIT, 1),
            # Quantos deste perfil retribuiram. A taxa usa VERIFICADOS como
            # denominador: quem ainda nao foi conferido nao e "nao retribuiu".
            "seguiram_de_volta": int(row.get("seguiram_de_volta") or 0),
            "verificados": int(row.get("verificados") or 0),
            "taxa_retorno_pct": (
                round(int(row.get("seguiram_de_volta") or 0) * 100.0
                      / int(row.get("verificados") or 0), 1)
                if int(row.get("verificados") or 0) else None),
            "primeiro_ms": row.get("primeiro_ms"), "ultimo_ms": row.get("ultimo_ms"),
            **db.follow_actions_counts(key, agora),
        })
    total_seg = sum(c["seguidos"] for c in contas)
    total_ativo = sum(c["ainda_seguindo"] for c in contas)
    return {
        "limite_ig": follower.IG_FOLLOWING_LIMIT,
        "total": {
            "pessoas_seguidas": total_seg,          # histórico global (nunca repete)
            "ainda_seguindo": total_ativo,
            "desfeitos": sum(c["desfeitos"] for c in contas),
            "contas": len(contas),
            "capacidade_total": len(contas) * follower.IG_FOLLOWING_LIMIT,
            **db.follow_followback_totals(),
        },
        "por_conta": contas,
        "por_alvo": db.follow_metrics_by_source(20),
        "blacklist": len(db.list_follow_blacklist()),
        "whitelist": len(db.list_follow_whitelist()),
    }


@app.get("/follow/export-csv")
def follow_export_csv():
    """CSV do histórico completo — 1 linha por pessoa já seguida (nunca repetida).
    Salva em data/_instafollow/ e devolve o arquivo (mesmo padrão do Spider)."""
    from fastapi.responses import Response
    linhas = db.follow_history_export()

    def _dt(ms):
        if not ms:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ms) / 1000))

    headers = ["Username", "User Id", "Perfil URL", "Seguida por (conta)", "Origem",
               "Seguida em", "Desfeita em", "Ainda seguindo"]
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(headers)
    for r in linhas:
        u = r.get("username") or ""
        w.writerow([u, r.get("user_id") or "",
                    f"https://www.instagram.com/{u}/" if u else "",
                    r.get("by_account") or "", r.get("source") or "",
                    _dt(r.get("ts_ms")), _dt(r.get("unfollowed_ts_ms")),
                    "não" if r.get("unfollowed_ts_ms") else "sim"])
    conteudo = buf.getvalue()

    nome = f"instafollow_historico_{time.strftime('%Y-%m-%d_%H%M')}.csv"
    salvo = ""
    try:
        pasta = SPIDER_DATA_DIR / "_instafollow"
        pasta.mkdir(parents=True, exist_ok=True)
        destino = pasta / nome
        # newline="" para o Windows não traduzir \r\n em \r\r\n (o mesmo bug que
        # dobrava as linhas do CSV do Spider).
        destino.write_text(conteudo, encoding="utf-8-sig", newline="")
        salvo = str(destino)
    except Exception:
        logger.exception("falha ao salvar CSV do InstaFollow")

    return Response(content=conteudo.encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"',
                             "X-Saved-Path": salvo})


@app.get("/follow/whitelist")
def follow_whitelist_get():
    return {"items": db.list_follow_whitelist()}


@app.post("/follow/whitelist")
def follow_whitelist_add(body: FollowBlacklistRequest):
    """Nunca deixar de seguir estas contas (o oposto da blacklist)."""
    u = spider.clean_username(body.username)
    if not u:
        raise HTTPException(400, "Informe um @usuário")
    db.add_follow_whitelist(u)
    return {"items": db.list_follow_whitelist()}


@app.delete("/follow/whitelist/{username}")
def follow_whitelist_del(username: str):
    db.delete_follow_whitelist(spider.clean_username(username))
    return {"items": db.list_follow_whitelist()}


@app.post("/follow/blacklist")
def follow_blacklist_add(body: FollowBlacklistRequest):
    u = spider.clean_username(body.username)
    if not u:
        raise HTTPException(400, "Informe um @usuário")
    db.add_follow_blacklist(u)
    return {"items": db.list_follow_blacklist()}


@app.delete("/follow/blacklist/{username}")
def follow_blacklist_del(username: str):
    db.delete_follow_blacklist(spider.clean_username(username))
    return {"items": db.list_follow_blacklist()}


# ─── Modem 4G: rotação do IP de saída ─────────────────────────────────────────
# O modem é a conexão local do PC. Rotacionar troca o IP de saída de TODOS os
# perfis que usam essa conexão — por isso é serializado (modem._lock) e usado nas
# TROCAS DE CONTA (Spider e Seguir), para que cada conta atue num IP próprio.

class ModemConfigRequest(BaseModel):
    url: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    enabled: Optional[bool] = None
    method: Optional[str] = None          # 'mode' (troca de rede, ~5s) | 'reboot' (~60s)
    wait_seconds: Optional[int] = None


@app.get("/modem/status")
async def modem_status():
    """Estado do modem + IPs de saída atuais. Só leitura (não rotaciona)."""
    st = await modem.status()
    st["rotating"] = modem.is_rotating()
    st["last_rotation"] = modem.last_rotation()
    return st


@app.get("/modem/config")
def modem_config_get():
    cfg = modem.config()
    cfg.pop("password", None)             # não devolve a senha
    return cfg


@app.post("/modem/config")
def modem_config_set(body: ModemConfigRequest):
    if body.url is not None:
        db.set_setting("modem_url", body.url.strip().rstrip("/"))
    if body.user is not None:
        db.set_setting("modem_user", body.user.strip())
    if body.password:
        db.set_setting("modem_pass", body.password)
    if body.enabled is not None:
        db.set_setting("modem_rotate_enabled", "1" if body.enabled else "0")
    if body.method is not None:
        # só os dois métodos medidos; qualquer outra coisa vira 'mode'
        db.set_setting("modem_rotate_method", "reboot" if body.method == "reboot" else "mode")
    if body.wait_seconds is not None:
        db.set_setting("modem_rotate_wait", str(max(10, int(body.wait_seconds))))
    return modem_config_get()


@app.post("/modem/rotate")
async def modem_rotate_now():
    """Rotaciona AGORA (botão de teste). Roda mesmo com a rotação automática
    desligada — é o jeito de conferir se o IP realmente muda nesta operadora."""
    if modem.is_rotating():
        raise HTTPException(409, "Já existe uma rotação em andamento")
    return await modem.rotate(force=True)


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
