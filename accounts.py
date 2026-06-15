"""
accounts.py — monitoração de contas, proxies e números de WhatsApp.

- Registra cada conta criada (email, senha, username, whatsapp, proxy usado).
- Calcula "vida útil" (dias desde a criação, ou até o banimento).
- Agrega uso de proxies e números de WhatsApp e aplica limites.
- Dispara verificação de status (abre o perfil no Camoufox e detecta o estado).
"""
import uuid
import time
import datetime as _dt
from typing import Optional

import db
import session_manager as sm

# Aquecimentos de conta em andamento (para contagem regressiva): account_id -> info
_warming: dict[str, dict] = {}


# ── Helpers de tempo / proxy ────────────────────────────────────────────────

def _now() -> _dt.datetime:
    return _dt.datetime.utcnow()


def _parse(ts: Optional[str]) -> Optional[_dt.datetime]:
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return _dt.datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def proxy_key(proxy: Optional[dict]) -> str:
    if not proxy or not proxy.get("host"):
        return ""
    return f"{proxy['host']}:{proxy.get('port', '')}"


def _lifespan(acc: dict) -> dict:
    created = _parse(acc.get("created_at"))
    if not created:
        return {"days": None, "label": "—"}
    end = _parse(acc.get("banned_at")) if acc.get("status") in ("banned", "suspended") else _now()
    delta = (end or _now()) - created
    days = delta.days
    hours = delta.seconds // 3600
    if days <= 0:
        label = f"{hours}h"
    else:
        label = f"{days}d {hours}h"
    return {"days": days, "label": label, "ongoing": acc.get("status") not in ("banned", "suspended")}


# ── Limites ─────────────────────────────────────────────────────────────────

def get_limits() -> dict:
    return {
        "proxy_limit":    int(db.get_setting("proxy_limit", "3")),
        "whatsapp_limit": int(db.get_setting("whatsapp_limit", "1")),
        # País-alvo (ISO-2) p/ coerência geográfica: só usa proxies desse país.
        # "" = sem filtro (qualquer país).
        "target_country": (db.get_setting("target_country", "") or "").strip().upper(),
    }


def set_limits(proxy_limit: Optional[int] = None, whatsapp_limit: Optional[int] = None,
               target_country: Optional[str] = None) -> dict:
    if proxy_limit is not None:
        db.set_setting("proxy_limit", int(proxy_limit))
    if whatsapp_limit is not None:
        db.set_setting("whatsapp_limit", int(whatsapp_limit))
    if target_country is not None:
        db.set_setting("target_country", (target_country or "").strip().upper())
    return get_limits()


def proxy_limit_status(proxy: Optional[dict]) -> dict:
    """Status de uso do proxy.
    Quando não há limite global rígido, usa recomendações baseadas em ban rate real.
    """
    key = proxy_key(proxy)
    used = db.count_accounts_by_proxy(key) if key else 0
    limits = get_limits()
    global_limit = limits["proxy_limit"]

    try:
        recs = get_proxy_recommendations()
        rec = next((r for r in recs if r["proxy_key"] == key), None)
    except Exception:
        rec = None

    if rec:
        recommended_max = rec.get("recommended_max", 999)
        blocked = used >= recommended_max
        effective_limit = recommended_max
    else:
        blocked = bool(key) and global_limit > 0 and used >= global_limit
        effective_limit = global_limit

    return {
        "proxy_key": key or None,
        "used": used,
        "limit": effective_limit,
        "blocked": blocked,
        "is_recommendation_based": bool(rec),
    }


def profile_account_status(profile_id: str) -> dict:
    """Se o perfil já tem conta (regra: 1 conta por perfil)."""
    existing = db.get_account_by_profile(profile_id) if profile_id else None
    return {
        "has_account": bool(existing),
        "account_id":  existing["id"] if existing else None,
        "username":    existing.get("username") if existing else None,
        "status":      existing.get("status") if existing else None,
    }


def whatsapp_limit_status(number: Optional[str]) -> dict:
    used = db.count_accounts_by_whatsapp(number) if number else 0
    limit = get_limits()["whatsapp_limit"]
    return {
        "number": number,
        "used": used,
        "limit": limit,
        "blocked": bool(number) and limit > 0 and used >= limit,
    }


# ── Registro de contas (chamado pelo fluxo de criação) ──────────────────────

def record_new_account(profile_id: str, data: dict) -> str:
    """Cria a linha da conta no início do fluxo. Snapshota o proxy do perfil."""
    profile = db.get_profile(profile_id)
    proxy = profile.get("proxy") if profile else None
    account_id = str(uuid.uuid4())
    db.save_account({
        "id":            account_id,
        "profile_id":    profile_id,
        "platform":      "instagram",
        "username":      data.get("username"),
        "email":         data.get("email"),
        "password":      data.get("password"),
        "whatsapp_number": None,
        "proxy":         proxy,
        "proxy_key":     proxy_key(proxy),
        "status":        "creating",
        "status_detail": "Cadastro iniciado",
    })
    try:
        db.record_account_history(db.get_account(account_id))
    except Exception:
        pass
    return account_id


def add_manual(data: dict) -> dict:
    """
    Cadastra uma conta manualmente na lista. Se um profile_id for informado,
    herda o proxy do perfil (mantém o vínculo conta↔perfil). Não impõe a regra
    de 1-conta-por-perfil (é entrada manual, decisão do usuário) — a UI avisa.
    """
    profile_id = data.get("profile_id") or None
    proxy = None
    if profile_id:
        profile = db.get_profile(profile_id)
        if not profile:
            raise ValueError("Perfil informado não existe")
        proxy = profile.get("proxy")
    account_id = str(uuid.uuid4())
    db.save_account({
        "id":              account_id,
        "profile_id":      profile_id,
        "platform":        data.get("platform") or "instagram",
        "username":        data.get("username"),
        "email":           data.get("email"),
        "password":        data.get("password"),
        "whatsapp_number": data.get("whatsapp_number") or None,
        "proxy":           proxy,
        "proxy_key":       proxy_key(proxy),
        "status":          data.get("status") or "created",
        "status_detail":   "Adicionada manualmente",
    })
    return get_account(account_id)


_EDITABLE = {"username", "email", "password", "whatsapp_number", "status", "platform"}


def edit(account_id: str, data: dict) -> dict:
    """
    Atualiza os campos editáveis de uma conta. Se o profile_id mudar, re-snapshota
    o proxy do novo perfil. Campos com valor None são ignorados (não apagam).
    """
    import json
    acc = db.get_account(account_id)
    if not acc:
        raise ValueError("Conta não encontrada")

    fields = {k: v for k, v in data.items() if k in _EDITABLE and v is not None}

    # Troca de perfil → re-snapshota proxy/proxy_key
    if "profile_id" in data:
        pid = data["profile_id"] or None
        fields["profile_id"] = pid
        prof = db.get_profile(pid) if pid else None
        proxy = prof.get("proxy") if prof else None
        fields["proxy"] = json.dumps(proxy) if proxy else None
        fields["proxy_key"] = proxy_key(proxy)

    # Se virou banida/suspensa e ainda não tinha data de banimento, registra
    if fields.get("status") in ("banned", "suspended") and not acc.get("banned_at"):
        fields["banned_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")

    if fields:
        db.update_account(account_id, **fields)
    return get_account(account_id)


def set_status(account_id: str, status: str, detail: str = ""):
    fields = {"status": status, "status_detail": detail}
    if status in ("banned", "suspended"):
        fields["banned_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
    db.update_account(account_id, **fields)
    # Espelha no histórico append-only (métricas persistem mesmo se a conta/perfil for apagado).
    try:
        db.record_account_history(db.get_account(account_id))
    except Exception:
        pass


def set_whatsapp(account_id: str, number: str):
    db.update_account(account_id, whatsapp_number=number)


def warmed_by_profile(profile_id: str):
    """Marca a conta vinculada a este perfil como aquecida no IG (atividade)."""
    acc = db.get_account_by_profile(profile_id)
    if acc:
        db.mark_account_warmed(acc["id"])


def warming_status(account_id: str) -> Optional[dict]:
    """Estado do aquecimento em andamento (para a contagem regressiva)."""
    w = _warming.get(account_id)
    if not w:
        return None
    remaining = max(0, int(w["ends_at"] - time.time()))
    return {
        "active": w["status"] == "warming" and remaining > 0,
        "status": w["status"],
        "minutes": w["minutes"],
        "remaining_seconds": remaining,
    }


async def run_account_warmup(account_id: str, minutes: float = 8, headless: bool = True):
    """
    Aquece a conta a partir da aba Contas: abre o perfil (em segundo plano por
    padrão), navega o IG por `minutes`, re-verifica o status e atualiza o registro.
    Mantém uma contagem regressiva.

    Agora passa a idade da conta para o warmup ter mais variedade (especialmente importante 7-15+ dias).
    """
    import instagram as ig
    acc = db.get_account(account_id)
    if not acc or not acc.get("profile_id"):
        _warming[account_id] = {"status": "error", "minutes": minutes, "ends_at": time.time()}
        return
    profile = db.get_profile(acc["profile_id"])
    if not profile:
        _warming[account_id] = {"status": "error", "minutes": minutes, "ends_at": time.time()}
        return

    # Calcula idade da conta em dias para variar o aquecimento
    created = _parse(acc.get("created_at"))
    age_days = 0
    if created:
        age_days = (_now() - created).days

    _warming[account_id] = {"status": "warming", "minutes": minutes,
                            "ends_at": time.time() + minutes * 60}

    async def _fn(page):
        await ig._warmup_browse(page, minutes, account_age_days=age_days)
        # Após aquecer, re-verifica o status real na mesma página
        try:
            return await ig.detect_status(page)
        except Exception:
            return None

    try:
        st = await sm.run_profile_task(profile, _fn, headless=headless, load_extension=False)
        db.mark_account_warmed(account_id)
        # Atualiza o status real se a verificação foi conclusiva
        fields = {"last_checked_at": _now().strftime("%Y-%m-%d %H:%M:%S")}
        if isinstance(st, dict) and st.get("status") and st["status"] != "unknown":
            fields["status"] = st["status"]
            fields["status_detail"] = st.get("detail", "")
            if st["status"] in ("banned", "suspended") and not acc.get("banned_at"):
                fields["banned_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
            fields["last_check_result"] = st.get("detail", "")
        db.update_account(account_id, **fields)
        _warming[account_id] = {"status": "done", "minutes": minutes, "ends_at": time.time()}
    except Exception as e:
        _warming[account_id] = {"status": "error", "minutes": minutes,
                                "ends_at": time.time(), "error": str(e)}


def set_username(account_id: str, username: str):
    db.update_account(account_id, username=username)


# ── Leitura para a UI ────────────────────────────────────────────────────────

_OS_LABEL = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}


def _device_label(platform: str) -> str:
    pl = (platform or "").lower()
    if "win" in pl:  return "Windows"
    if "mac" in pl:  return "macOS"
    if "linux" in pl: return "Linux"
    return "Desktop"


def _public(acc: dict, reveal: bool = False) -> dict:
    out = dict(acc)
    out["lifespan"] = _lifespan(acc)
    out["proxy_usage"] = proxy_limit_status(acc.get("proxy"))
    out["whatsapp_usage"] = whatsapp_limit_status(acc.get("whatsapp_number"))
    # Aquecimento da CONTA (atividade no IG) — distinto do aquecimento do perfil
    out["account_warmed"] = bool(acc.get("warmup_count"))
    out["account_warmup_count"] = acc.get("warmup_count") or 0
    out["warming"] = warming_status(acc["id"])
    # Vínculo com o perfil (nome + dispositivo/SO)
    prof = db.get_profile(acc.get("profile_id")) if acc.get("profile_id") else None
    if prof:
        out["profile_name"] = prof.get("name")
        out["profile_device"] = _device_label(prof.get("fingerprint", {}).get("platform", ""))
        out["profile_exists"] = True
        out["profile_warmup_count"] = prof.get("warmup_count") or 0
        out["profile_last_warmup"] = prof.get("last_warmup_at")
        out["profile_warmed"] = bool(prof.get("warmup_count"))
    else:
        out["profile_name"] = None
        out["profile_device"] = None
        out["profile_exists"] = False
        out["profile_warmup_count"] = 0
        out["profile_last_warmup"] = None
        out["profile_warmed"] = False
    # Na LISTA a senha do proxy é mascarada; no DETALHE (reveal) é exposta para uso.
    if not reveal and out.get("proxy") and out["proxy"].get("password"):
        out["proxy"] = dict(out["proxy"])
        out["proxy"]["password"] = "••••••"
    return out


def list_accounts() -> list:
    return [_public(a) for a in db.list_accounts()]


def get_account(account_id: str) -> Optional[dict]:
    acc = db.get_account(account_id)
    return _public(acc, reveal=True) if acc else None


def delete_account(account_id: str):
    # Antes de apagar, congela o snapshot no histórico para a métrica não se perder.
    try:
        db.record_account_history(db.get_account(account_id))
    except Exception:
        pass
    db.delete_account(account_id)


def export_accounts_to_csv() -> str:
    """Exporta todas as contas para CSV (com proxy/perfil/aquecimento/vida útil)."""
    import csv
    import io

    fieldnames = [
        "username", "email", "password", "whatsapp_number", "status",
        "platform", "profile_name", "profile_device", "proxy",
        "navegador_aquecido", "conta_aquecida", "vida_util",
        "created_at", "banned_at", "last_checked_at",
    ]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    for a in list_accounts():  # já vem com perfil, vida útil e flags de aquecimento
        proxy = a.get("proxy") or {}
        proxy_str = f"{proxy.get('host','')}:{proxy.get('port','')}" if proxy.get("host") else ""
        writer.writerow({
            "username":           a.get("username", ""),
            "email":              a.get("email", ""),
            "password":           a.get("password", ""),
            "whatsapp_number":    a.get("whatsapp_number", ""),
            "status":             a.get("status", ""),
            "platform":           a.get("platform", ""),
            "profile_name":       a.get("profile_name") or "",
            "profile_device":     a.get("profile_device") or "",
            "proxy":              proxy_str,
            "navegador_aquecido": "sim" if a.get("profile_warmed") else "não",
            "conta_aquecida":     "sim" if a.get("account_warmed") else "não",
            "vida_util":          (a.get("lifespan") or {}).get("label", ""),
            "created_at":         a.get("created_at", ""),
            "banned_at":          a.get("banned_at") or "",
            "last_checked_at":    a.get("last_checked_at") or "",
        })
    return out.getvalue()


# ── Resumos de uso ────────────────────────────────────────────────────────────

def proxy_usage_summary() -> list:
    """Agrupa contas por proxy e devolve uso + limite + bloqueio."""
    limits = get_limits()
    grouped: dict[str, dict] = {}
    for a in db.list_accounts():
        key = a.get("proxy_key") or "(sem proxy)"
        g = grouped.setdefault(key, {
            "proxy_key": a.get("proxy_key"),
            "proxy": a.get("proxy"),
            "count": 0,
            "accounts": [],
            "statuses": {},
        })
        g["count"] += 1
        g["accounts"].append({"id": a["id"], "username": a.get("username"), "status": a.get("status")})
        st = a.get("status") or "unknown"
        g["statuses"][st] = g["statuses"].get(st, 0) + 1
    out = []
    for key, g in grouped.items():
        limit = limits["proxy_limit"]
        if g.get("proxy") and g["proxy"].get("password"):
            g["proxy"] = dict(g["proxy"]); g["proxy"]["password"] = "••••••"
        g["limit"] = limit
        g["blocked"] = bool(g["proxy_key"]) and limit > 0 and g["count"] >= limit
        out.append(g)
    return out


# ── Métricas / Ban Analytics (implementado conforme pedido) ────────────────────

def get_ban_analytics() -> dict:
    """
    Retorna estatísticas agregadas úteis para análise de bans e qualidade do pool.
    Usa contas vivas ∪ histórico — as métricas persistem mesmo após apagar perfil/conta.
    """
    all_accounts = _merged_accounts()
    total = len(all_accounts)
    if total == 0:
        return {"total": 0, "active": 0, "banned": 0, "suspended": 0, "ban_rate": 0.0}

    active = sum(1 for a in all_accounts if a.get("status") == "active")
    banned = sum(1 for a in all_accounts if a.get("status") == "banned")
    suspended = sum(1 for a in all_accounts if a.get("status") == "suspended")
    other = total - active - banned - suspended

    ban_rate = round((banned + suspended) / total * 100, 1) if total > 0 else 0.0

    # Por proxy (ban rate por proxy)
    by_proxy = {}
    for a in all_accounts:
        key = a.get("proxy_key") or "(sem proxy)"
        if key not in by_proxy:
            by_proxy[key] = {"total": 0, "banned": 0, "suspended": 0}
        by_proxy[key]["total"] += 1
        if a.get("status") == "banned":
            by_proxy[key]["banned"] += 1
        elif a.get("status") == "suspended":
            by_proxy[key]["suspended"] += 1

    proxy_stats = []
    for key, data in by_proxy.items():
        rate = round((data["banned"] + data["suspended"]) / data["total"] * 100, 1) if data["total"] > 0 else 0
        proxy_stats.append({
            "proxy_key": key,
            "total_accounts": data["total"],
            "banned": data["banned"],
            "suspended": data["suspended"],
            "ban_rate_pct": rate
        })

    # Ordena por ban rate (pior primeiro)
    proxy_stats.sort(key=lambda x: x["ban_rate_pct"], reverse=True)

    # Sobrevivência média (média de dias das contas que ainda estão vivas ou que foram banidas)
    lifespans = []
    for a in all_accounts:
        ls = _lifespan(a)
        if ls.get("days") is not None:
            lifespans.append(ls["days"])

    avg_lifespan = round(sum(lifespans) / len(lifespans), 1) if lifespans else 0

    return {
        "total_accounts": total,
        "active": active,
        "banned": banned,
        "suspended": suspended,
        "other": other,
        "ban_rate_pct": ban_rate,
        "average_lifespan_days": avg_lifespan,
        "by_proxy": proxy_stats[:20]   # limita para não ficar gigante
    }


def _merged_accounts() -> list:
    """Contas VIVAS ∪ HISTÓRICO (contas já apagadas). Dedup por id — a viva prevalece
    (status atual); a do histórico entra quando a conta/perfil foi removido. É a base das
    métricas para que NADA se perca ao apagar perfil ou conta."""
    by_id = {}
    try:
        for h in db.list_account_history():
            if h.get("account_id"):
                by_id[h["account_id"]] = {**h, "id": h["account_id"]}
    except Exception:
        pass
    for a in db.list_accounts():
        by_id[a["id"]] = a
    return list(by_id.values())


def proxy_outcomes() -> dict:
    """Desfechos das contas por proxy_key (para avaliar o risco REAL de um proxy).
    Retorna {proxy_key: {total, banned, suspended, checkpoint, discarded, bad, bad_rate}}."""
    out = {}
    for a in _merged_accounts():
        key = a.get("proxy_key") or "(sem proxy)"
        d = out.setdefault(key, {"total": 0, "banned": 0, "suspended": 0,
                                 "checkpoint": 0, "discarded": 0})
        d["total"] += 1
        st = (a.get("status") or "").lower()
        if st in ("banned", "suspended", "checkpoint", "discarded"):
            d[st] += 1
    for d in out.values():
        d["bad"] = d["banned"] + d["suspended"]
        d["bad_rate"] = round(d["bad"] / d["total"] * 100, 1) if d["total"] else 0.0
    return out


def get_proxy_recommendations() -> list:
    """
    Retorna recomendações inteligentes por proxy baseadas em dados reais de ban.
    Usado para distribuição 'máximo possível' sem limite rígido global.
    """
    analytics = get_ban_analytics()
    limits = get_limits()
    global_limit = limits["proxy_limit"]

    recommendations = []
    for p in analytics.get("by_proxy", []):
        key = p["proxy_key"]
        used = p["total_accounts"]
        ban_rate = p.get("ban_rate_pct", 0) or 0

        # Lógica de recomendação:
        # - Proxies com ban_rate alto (>=25%) → limite conservador
        # - Proxies bons (<15%) → deixar usar bem mais
        # - Se global_limit > 0, respeitar como teto, mas permitir um pouco mais em bons proxies
        if ban_rate >= 40:
            recommended_max = max(2, int(used * 0.6))   # bem conservador
        elif ban_rate >= 25:
            recommended_max = max(4, int(used * 0.8) + 2)
        elif ban_rate >= 15:
            recommended_max = used + 5
        else:
            # Proxy muito bom → vai o mais longe possível
            recommended_max = used + 12 if global_limit <= 0 else max(used + 8, global_limit + 4)

        remaining = max(0, recommended_max - used)
        safe = ban_rate < 20

        recommendations.append({
            "proxy_key": key,
            "used": used,
            "ban_rate_pct": ban_rate,
            "recommended_max": recommended_max,
            "remaining_recommended": remaining,
            "safe": safe,
            "global_limit": global_limit
        })

    # Ordena pelos melhores (menor ban rate primeiro, depois mais remaining)
    recommendations.sort(key=lambda x: (x["ban_rate_pct"], -x["remaining_recommended"]))
    return recommendations


def whatsapp_usage_summary() -> list:
    limits = get_limits()
    grouped: dict[str, dict] = {}
    for a in db.list_accounts():
        num = a.get("whatsapp_number")
        if not num:
            continue
        g = grouped.setdefault(num, {"number": num, "count": 0, "accounts": []})
        g["count"] += 1
        g["accounts"].append({"id": a["id"], "username": a.get("username"), "status": a.get("status")})
    out = []
    for num, g in grouped.items():
        limit = limits["whatsapp_limit"]
        g["limit"] = limit
        g["blocked"] = limit > 0 and g["count"] >= limit
        out.append(g)
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


# ── Verificação de status (automática) ──────────────────────────────────────

async def check_status(account_id: str, headless: bool = True) -> dict:
    """Abre o perfil da conta, detecta o status no Instagram e atualiza o banco."""
    import instagram as ig
    acc = db.get_account(account_id)
    if not acc:
        return {"error": "conta não encontrada"}
    profile = db.get_profile(acc.get("profile_id")) if acc.get("profile_id") else None
    if not profile:
        db.update_account(account_id,
                          last_checked_at=_now().strftime("%Y-%m-%d %H:%M:%S"),
                          last_check_result="perfil ausente")
        return {"status": acc.get("status"), "detail": "perfil ausente — não foi possível verificar"}

    result = await sm.check_account_status(profile, ig.detect_status, headless=headless)
    status = result.get("status", "unknown")
    detail = result.get("detail", "")
    fields = {
        "last_checked_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_check_result": detail,
    }
    # Só sobrescreve status com resultados conclusivos
    if status != "unknown":
        fields["status"] = status
        fields["status_detail"] = detail
        if status in ("banned", "suspended") and not acc.get("banned_at"):
            fields["banned_at"] = _now().strftime("%Y-%m-%d %H:%M:%S")
    db.update_account(account_id, **fields)
    return {"status": status, "detail": detail}
