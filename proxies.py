"""
proxies.py — pool de proxies COMPARTILHADO (tabela proxy_pool no banco).

O pool é lido do banco (db.proxy_pool), de modo que vários usuários compartilham
os mesmos proxies. Na primeira execução, se o pool estiver vazio, fazemos um
"seed" automático a partir dos arquivos da pasta local ./proxys (migração).

Aceita os formatos por linha:
  host:porta:usuario:senha     (Webshare e similares)
  host:porta
  scheme://usuario:senha@host:porta
  scheme://host:porta

Distribui os proxies entre perfis (1 por perfil) priorizando os menos usados e
respeitando o limite de contas por proxy.
"""
from pathlib import Path
from urllib.parse import urlparse

import db

_POOL_DIR = Path(__file__).parent / "proxys"
_seeded = False

# ── País do proxy (resolução OFFLINE via GeoLite2, p/ coerência geográfica) ──────
# Estratégia "país-alvo": só atribui proxies do país configurado em target_country,
# para a persona (nome BR) casar com o IP. Resolve o país do IP de saída pelo mmdb
# que já vem com o Camoufox — sem rede. Cache em memória (pool é pequeno).
_geo_reader = None          # geoip2 Reader (lazy)
_geo_failed = False         # se não der p/ abrir o mmdb, desliga o filtro silenciosamente
_country_cache: dict = {}   # host -> ISO ("IT", "BR", ...) ou "" se desconhecido


def _country_reader():
    global _geo_reader, _geo_failed
    if _geo_reader is not None or _geo_failed:
        return _geo_reader
    try:
        import os
        import geoip2.database
        import camoufox
        mmdb = os.path.join(os.path.dirname(camoufox.__file__), "GeoLite2-City.mmdb")
        _geo_reader = geoip2.database.Reader(mmdb)
    except Exception:
        _geo_failed = True
        _geo_reader = None
    return _geo_reader


def proxy_country(host: str) -> str:
    """ISO-2 do país do IP do proxy (ex.: 'IT'), ou '' se não resolver. Offline + cache."""
    if not host:
        return ""
    if host in _country_cache:
        return _country_cache[host]
    iso = ""
    r = _country_reader()
    if r is not None:
        try:
            iso = (r.city(host).country.iso_code or "").upper()
        except Exception:
            iso = ""
    _country_cache[host] = iso
    return iso


def target_country() -> str:
    """País-alvo configurado (ISO-2 maiúsculo) ou '' (sem filtro = comportamento antigo)."""
    return (db.get_setting("target_country", "") or "").strip().upper()


def parse_proxy_line(line: str):
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    # formato URL
    if "://" in line:
        try:
            u = urlparse(line)
            if not u.hostname or not u.port:
                return None
            p = {"protocol": u.scheme or "http", "host": u.hostname, "port": int(u.port)}
            if u.username:
                p["username"] = u.username
                p["password"] = u.password or ""
            return p
        except Exception:
            return None
    # formato por dois-pontos
    parts = line.split(":")
    try:
        if len(parts) == 4:
            host, port, user, pw = parts
            return {"protocol": "http", "host": host.strip(), "port": int(port),
                    "username": user.strip(), "password": pw.strip()}
        if len(parts) == 2:
            host, port = parts
            return {"protocol": "http", "host": host.strip(), "port": int(port)}
    except Exception:
        return None
    return None


def proxy_key(p) -> str:
    if not p or not p.get("host"):
        return ""
    return f"{p['host']}:{p.get('port','')}"


def _read_pool_files() -> list:
    """Lê todos os arquivos da pasta ./proxys e devolve proxies únicos (por host:porta)."""
    out, seen = [], set()
    if not _POOL_DIR.exists():
        return out
    for f in sorted(_POOL_DIR.glob("**/*")):
        if not f.is_file() or f.suffix.lower() not in (".txt", ".csv", ""):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            p = parse_proxy_line(line)
            if p:
                k = proxy_key(p)
                if k and k not in seen:
                    seen.add(k)
                    out.append(p)
    return out


def import_from_text(text: str) -> dict:
    """Adiciona proxies (uma linha por proxy) ao pool compartilhado. Idempotente."""
    added, skipped, invalid = 0, 0, 0
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        p = parse_proxy_line(line)
        if not p:
            invalid += 1
            continue
        if db.add_pool_proxy(p):
            added += 1
        else:
            skipped += 1
    return {"added": added, "skipped": skipped, "invalid": invalid}


def _ensure_seed():
    """Na primeira vez (por processo), se o pool estiver vazio, importa os arquivos
    locais da pasta ./proxys para o banco compartilhado. Depois disso, o pool é
    gerenciado pelo banco e os arquivos deixam de ser relidos."""
    global _seeded
    if _seeded:
        return
    _seeded = True
    try:
        if db.count_pool() == 0:
            for p in _read_pool_files():
                db.add_pool_proxy(p)
    except Exception:
        pass


def load_pool() -> list:
    """Devolve os proxies do pool compartilhado (lidos do banco)."""
    _ensure_seed()
    return db.list_pool_proxies()


def usage_counts() -> dict:
    """Quantas CONTAS CONCLUÍDAS usam cada proxy (host:porta).

    Antes contava PERFIS — o que marcava o proxy como usado já na criação do perfil,
    antes de qualquer conta existir. Agora conta só contas realmente criadas (código
    de e-mail usado), então tentativas incompletas não consomem a cota do proxy.
    """
    return db.completed_accounts_by_proxy()


def _active_profile_counts() -> dict:
    """Quantos PERFIS VIVOS seguram cada proxy (host:porta).

    Usado para ESPALHAR a atribuição: evita que vários perfis (ex.: do warm-pool)
    caiam todos no MESMO IP — footprint forte de bot no signup do IG. É diferente
    de usage_counts(): aquela conta a COTA (contas concluídas, que não some ao
    apagar um perfil); esta conta quem está ocupando o IP AGORA.
    """
    counts: dict = {}
    try:
        for p in db.list_profiles():
            k = proxy_key(p.get("proxy") or {})
            if k:
                counts[k] = counts.get(k, 0) + 1
    except Exception:
        pass
    return counts


def assign(n: int, limit: int) -> list:
    """
    Distribui proxies de forma inteligente.

    - Quando limit > 0: respeita o limite global.
    - Quando limit <= 0 (sem limite): usa recomendações baseadas em ban rate real
      para dar o máximo possível sem arriscar proxies ruins demais.
    Prioriza sempre os proxies com melhor histórico (menor ban rate + menos usados).
    """
    pool = load_pool()
    if not pool:
        return [None] * n

    # Marcação MANUAL: remove proxies marcados como 'banned' (queimado pelo usuário) —
    # não cria mais contas neles. Se isso esvaziar o pool, mantém o original (não trava
    # a criação; o usuário decide se libera a marcação).
    try:
        banned = {k for k, v in db.list_proxy_flags().items() if v.get("status") == "banned"}
        if banned:
            kept = [p for p in pool if proxy_key(p) not in banned]
            if kept:
                pool = kept
    except Exception:
        pass

    # Coerência geográfica (estratégia país-alvo): se houver target_country, usa só
    # proxies desse país — a persona BR casa com o IP. Se NENHUM proxy do pool for
    # do país-alvo, faz fallback ao pool inteiro com aviso (não trava a criação;
    # o usuário precisa adicionar proxies do país).
    tgt = target_country()
    if tgt:
        filtered = [p for p in pool if proxy_country(p.get("host", "")) == tgt]
        if filtered:
            pool = filtered
        else:
            try:
                import logging
                logging.getLogger("proxies").warning(
                    "target_country=%s mas nenhum proxy do pool é desse país — "
                    "usando o pool inteiro. Adicione proxies de %s para coerência.", tgt, tgt)
            except Exception:
                pass

    counts = usage_counts()
    active = _active_profile_counts()  # espalha: evita reusar IP já ocupado por outro perfil

    # Tenta usar recomendações baseadas em analytics quando não há limite rígido
    try:
        from accounts import get_proxy_recommendations
        recs = {r["proxy_key"]: r for r in get_proxy_recommendations()}
    except Exception:
        recs = {}

    result = []
    for _ in range(n):
        best = None
        best_score = None

        for p in pool:
            k = proxy_key(p)
            c = counts.get(k, 0)

            # Respeita limite global quando definido
            if limit and limit > 0 and c >= limit:
                continue

            # Score: quanto menor melhor
            # Base: cota usada (contas concluídas) + perfis vivos no mesmo IP
            # (o termo `active` espalha os perfis por IPs distintos).
            score = c + active.get(k, 0)

            # Se temos recomendação, ajusta o score
            rec = recs.get(k)
            if rec:
                ban = rec.get("ban_rate_pct", 0) or 0
                rec_max = rec.get("recommended_max", 999)

                # Penalidade forte para proxies ruins
                if ban >= 35:
                    score += 50
                elif ban >= 20:
                    score += 15
                else:
                    score -= 2   # bônus para bons proxies

                # Respeita o "recommended_max" como soft cap
                if c >= rec_max and limit <= 0:
                    continue

            if best_score is None or score < best_score:
                best = p
                best_score = score

        if best is None:
            result.append(None)
        else:
            k = proxy_key(best)
            counts[k] = counts.get(k, 0) + 1
            active[k] = active.get(k, 0) + 1  # espalha dentro do próprio lote
            result.append(best)

    return result


def summary() -> dict:
    """Resumo do pool para a UI (sem expor senha). Usa recomendações reais quando possível."""
    pool = load_pool()
    counts = usage_counts()
    limit = int(db.get_setting("proxy_limit", "3") or 0)

    try:
        from accounts import get_proxy_recommendations
        recs = {r["proxy_key"]: r for r in get_proxy_recommendations()}
    except Exception:
        recs = {}

    items = []
    total_recommended_remaining = 0

    for p in pool:
        k = proxy_key(p)
        used = counts.get(k, 0)
        rec = recs.get(k)

        if rec:
            remaining = rec.get("remaining_recommended", 0)
            ban_rate = rec.get("ban_rate_pct")
            blocked = remaining <= 0 and limit > 0
        else:
            remaining = max(0, limit - used) if limit > 0 else 999
            ban_rate = None
            blocked = bool(limit and used >= limit)

        total_recommended_remaining += max(0, remaining) if remaining != 999 else 0

        items.append({
            "key": k,
            "protocol": p.get("protocol", "http"),
            "auth": bool(p.get("username")),
            "used": used,
            "blocked": blocked,
            "ban_rate_pct": ban_rate,
            "recommended_remaining": remaining if remaining != 999 else None,
        })

    # free_slots agora reflete o que é realista baseado em dados de ban
    if limit > 0:
        capacity = sum(max(0, (limit - i["used"])) for i in items)
    else:
        capacity = total_recommended_remaining if total_recommended_remaining > 0 else None

    return {
        "count": len(items),
        "limit": limit,
        "free_slots": capacity,
        "items": items
    }
