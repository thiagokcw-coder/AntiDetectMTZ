"""
modem.py — rotação de IP do modem 4G (UFI/TIM) usado como saída dos perfis.

Descoberto por engenharia reversa do painel web (SPA "4G UFI", firmware
UFI_HM_SIM1_V020_251125): a UI inteira fala com UM endpoint JSON.

    POST /himiapi/json
    login:        {"cmdid":"login","username":..,"password":..}  -> {"reply":"ok","session":..}
    autenticadas: header Authorization: <session>  +  sessionId no corpo

MEDIDO AO VIVO (2026-08-16, chip TIM) — o que motiva o desenho daqui:

1. O modem NÃO tem "um" IP público. A TIM faz CGNAT e atribui o IP de saída
   POR CONEXÃO: em amostras seguidas o mesmo modem responde de 3-4 IPs
   diferentes ao mesmo tempo. Logo "1 conta = 1 IP fixo" é impossível aqui; o
   que dá pra garantir é "1 conta = 1 ÉPOCA de rotação" (conjunto de IPs
   exclusivo daquele período).
2. Toda rotação renova o conjunto INTEIRO — em 5 épocas medidas, nenhum IP
   exato se repetiu entre elas.
3. Trocar o MODO DE REDE re-registra na torre e renova os IPs em ~1-10s;
   REINICIAR faz o mesmo em ~60s. Por isso o padrão é o modo, e o reboot é
   reserva.
4. O bloco /16 (181.77.x) é da TIM e não muda — a rotação troca o IP exato,
   não a operadora (que é justamente o que dá confiança ao IP móvel).

⚠️ SEGURANÇA: o comando `rebootcmd` aceita params "reboot" E "freset"
(RESET DE FÁBRICA). Aqui a string "reboot" é literal e nunca vem de
configuração/variável, pra que nenhum erro de digitação vire factory reset.
"""
import asyncio
import json
import time
import urllib.request

import db

DEFAULT_URL = "http://192.168.100.1"
DEFAULT_USER = "admin"
DEFAULT_PASS = "admin"

# netmode do firmware: 0 = 4G/3G (auto) · 1 = 4G · 2 = 3G
MODE_AUTO, MODE_4G, MODE_3G = "0", "1", "2"

IP_SERVICES = ["https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"]

# Uma rotação por vez no processo inteiro: ela derruba a saída de TODOS os
# perfis por alguns segundos, então dois jobs não podem rotacionar juntos.
_lock = asyncio.Lock()
_rotating = False
_last = {"at": None, "ips": [], "method": None}


def is_rotating() -> bool:
    """True enquanto a rotação está em curso — os loops de extração/follow usam
    isto para ESPERAR em vez de contar como falha de rede."""
    return _rotating


def last_rotation() -> dict:
    return dict(_last)


def config() -> dict:
    """Configuração do modem (settings do banco, com os defaults descobertos)."""
    return {
        "url": (db.get_setting("modem_url", DEFAULT_URL) or DEFAULT_URL).rstrip("/"),
        "user": db.get_setting("modem_user", DEFAULT_USER) or DEFAULT_USER,
        "password": db.get_setting("modem_pass", DEFAULT_PASS) or DEFAULT_PASS,
        "enabled": str(db.get_setting("modem_rotate_enabled", "0") or "0") == "1",
        "method": db.get_setting("modem_rotate_method", "mode") or "mode",
        "wait_seconds": int(db.get_setting("modem_rotate_wait", "45") or 45),
    }


# ── Camada HTTP (síncrona; chamada via executor) ──────────────────────────────

def _post(cfg: dict, cmdid: str, params=None, session: str = None, timeout: float = 10):
    body = {"cmdid": cmdid}
    if params is not None:
        body["params"] = params
    headers = {"Content-Type": "application/json;charset=UTF-8"}
    if session:
        body["sessionId"] = session
        headers["Authorization"] = session
    req = urllib.request.Request(f"{cfg['url']}/himiapi/json",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _session(cfg: dict, timeout: float = 10) -> str:
    """Autentica e devolve o sessionId. O login leva username/password no corpo
    (não em `params`), por isso não passa pelo _post genérico."""
    body = {"cmdid": "login", "username": cfg["user"], "password": cfg["password"]}
    req = urllib.request.Request(f"{cfg['url']}/himiapi/json", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json;charset=UTF-8"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    if resp.get("reply") != "ok" or not resp.get("session"):
        raise RuntimeError("login no modem falhou (usuário/senha?)")
    return resp["session"]


def _public_ip(url: str, timeout: float = 8):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode().strip()
    except Exception:
        return None


def _sample_ips(n: int = 6, pause: float = 0.3) -> list:
    """Amostra o pool de saída. Cada requisição abre conexão nova, e o CGNAT
    pode responder de um IP diferente — por isso amostra, não mede uma vez."""
    seen = []
    for i in range(n):
        ip = _public_ip(IP_SERVICES[i % len(IP_SERVICES)])
        if ip and ip not in seen:
            seen.append(ip)
        time.sleep(pause)
    return sorted(seen)


def _wait_online(timeout: float = 45) -> float:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _public_ip(IP_SERVICES[0], timeout=5):
            return round(time.time() - t0, 1)
        time.sleep(2)
    return -1.0


def status_sync() -> dict:
    """Estado do modem + IPs de saída atuais (leitura, não mexe em nada)."""
    cfg = config()
    out = {"url": cfg["url"], "enabled": cfg["enabled"], "method": cfg["method"],
           "ok": False, "error": None}
    try:
        sid = _session(cfg)
        ov = (_post(cfg, "getoverview", None, sid).get("params") or {})
        st = (_post(cfg, "getallstatus", None, sid).get("params") or {})
        nm = (_post(cfg, "getnetworkmode", None, sid).get("params") or {})
        out.update({
            "ok": True,
            "wan_ip": ov.get("WANIP"),          # CGNAT interno — NÃO é o IP público
            "imei": ov.get("IMEI"),
            "signal": ov.get("SIGNAL"),
            "carrier": st.get("carrier"),
            "state": st.get("internetState"),
            "sim": st.get("simCardState"),
            "netmode": nm.get("netmode"),
            "netmode_label": {"0": "4G/3G (auto)", "1": "4G", "2": "3G"}.get(nm.get("netmode"), "?"),
            "public_ips": _sample_ips(4),
        })
    except Exception as e:
        out["error"] = str(e)
    return out


def _rotate_sync(method: str, wait_seconds: int, log=None, avoid=None) -> dict:
    """Executa a rotação e espera a internet voltar. Roda em thread (executor).

    `avoid`: IPs que NÃO podem reaparecer (os da época anterior). O resultado traz
    `mudou`/`repetidos` comparando contra a amostra de agora UNIDA a esse conjunto —
    quem decide re-tentar é o rotate()."""
    cfg = config()
    res = {"method": method, "ok": False, "error": None}

    def _log(m):
        if log:
            log(m)

    before = _sample_ips(4)
    proibidos = set(before) | set(avoid or [])
    res["ips_antes"] = before
    _log(f"🔄 rotacionando IP do modem ({method}) — saída atual: {', '.join(before) or '?'}")

    t0 = time.time()
    try:
        sid = _session(cfg)
        if method == "reboot":
            # "reboot" é LITERAL de propósito: o mesmo comando aceita "freset"
            # (reset de fábrica) e isso jamais pode vir de configuração.
            _post(cfg, "rebootcmd", "reboot", sid)
            _log("↻ modem reiniciando (~60s)…")
            time.sleep(20)                       # deixa cair antes de sondar
        else:
            # Troca de modo: re-registra na torre e renova o IP em segundos.
            # Dois passos (auto → 4G) garantem o re-attach e terminam em 4G puro.
            _post(cfg, "setnetworkmode", {"netmode": MODE_AUTO}, sid)
            time.sleep(3)
            try:
                sid = _session(cfg)
            except Exception:
                pass
            _post(cfg, "setnetworkmode", {"netmode": MODE_4G}, sid)
            _log("📶 re-registrando na torre (4G)…")
            time.sleep(2)
    except Exception as e:
        # O corte da conexão pode matar a própria resposta do comando — isso é
        # esperado e não significa que o comando não foi aplicado.
        _log(f"(a conexão caiu ao enviar o comando, o que é esperado: {str(e)[:60]})")

    waited = _wait_online(timeout=wait_seconds)
    if waited < 0:
        res["error"] = f"a internet não voltou em {wait_seconds}s"
        _log(f"✖ {res['error']}")
        return res

    time.sleep(2)
    after = _sample_ips(5)
    res.update({"ok": True, "ips_depois": after,
                "segundos": round(time.time() - t0, 1),
                "mudou": bool(after) and not (set(after) & proibidos),
                "repetidos": sorted(set(after) & proibidos)})
    if res["mudou"]:
        _log(f"✓ IP renovado em {res['segundos']}s — nova saída: {', '.join(after)}")
    else:
        _log(f"⚠ o IP repetiu ({', '.join(res['repetidos'])}) — a operadora devolveu "
             f"a mesma faixa")
    return res


async def rotate(log=None, force: bool = False, attempts: int = 3) -> dict:
    """Rotaciona o IP de saída até ele REALMENTE mudar. Serializada: uma por vez no
    processo inteiro, porque derruba a saída de TODOS os perfis por alguns segundos.

    Garantia: o conjunto de IPs da nova época não pode intersectar o da época
    anterior (nem a amostra imediatamente anterior). Se a operadora devolver a
    mesma faixa, tenta de novo — e na última tentativa escala para REBOOT, que é
    mais forte que a troca de modo (mais lento, mas renova o attach inteiro).
    Retorna {ok, mudou, ips_antes, ips_depois, segundos, tentativas} ou {ok: False}."""
    global _rotating
    cfg = config()
    if not cfg["enabled"] and not force:
        return {"ok": False, "error": "rotação de IP desligada nos ajustes", "skipped": True}

    async with _lock:
        _rotating = True
        try:
            loop = asyncio.get_running_loop()
            # IPs da ÉPOCA ANTERIOR (do perfil que acabou de sair): o novo perfil
            # não pode reaparecer em nenhum deles.
            evitar = set(_last.get("ips") or [])
            n = max(1, int(attempts))
            res = {}
            for i in range(1, n + 1):
                # Última tentativa: escala p/ reboot (renova o attach inteiro).
                metodo = "reboot" if (i == n and n > 1) else cfg["method"]
                if i > 1 and log:
                    log(f"↻ tentativa {i}/{n} de rotação"
                        + (" — escalando para REBOOT (~60s)" if metodo == "reboot" else ""))
                res = await loop.run_in_executor(
                    None, lambda m=metodo: _rotate_sync(m, cfg["wait_seconds"], log, evitar))
                res["tentativas"] = i
                if not res.get("ok"):
                    break                     # falha de rede/modem: não insiste
                evitar |= set(res.get("ips_antes") or [])
                if res.get("mudou"):
                    break
            if res.get("ok"):
                _last.update({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "ips": res.get("ips_depois") or [], "method": res.get("method")})
                if not res.get("mudou") and log:
                    log(f"⚠ após {res.get('tentativas')} tentativa(s) o IP ainda repete "
                        f"({', '.join(res.get('repetidos') or [])}) — seguindo mesmo assim")
            return res
        finally:
            _rotating = False


async def status() -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, status_sync)


async def wait_if_rotating(timeout: float = 90) -> bool:
    """Usado pelos loops: se uma rotação está em curso, espera ela acabar em vez
    de contabilizar a queda como falha de rede/bloqueio."""
    t0 = time.time()
    while _rotating and time.time() - t0 < timeout:
        await asyncio.sleep(1)
    return _rotating
