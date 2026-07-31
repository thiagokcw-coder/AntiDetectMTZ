"""
session_manager.py — Camoufox (Firefox antidetect)

Camoufox mascara TLS, HTTP/2, Canvas, WebGL e Audio no nível do motor.
Não usa subprocess — o browser é gerenciado pelo próprio Camoufox via Playwright.
A extensão Firefox (firefox-extension/) é carregada automaticamente em cada sessão.
"""
import asyncio
import getpass
import hashlib
import random
import socket
import time
import traceback
import uuid
from pathlib import Path

import db
from paths import LOG_FILE

_sessions: dict[str, dict] = {}
LOCAL_DEBUG_LOG = Path(__file__).parent / "debug.log"

# Extensão Firefox ao lado deste arquivo
_EXT_PATH = Path(__file__).parent.parent / "firefox-extension"

# Extensão IG Filler (agora compativel com Camoufox via MV3 + gecko + MAIN world)
_IG_FILLER_PATH = Path(__file__).parent / "ig-filler-extension"

# ── Lock compartilhado de perfil em uso ────────────────────────────────────────
# Identifica QUEM detém o perfil (exibido na UI) e um token por processo para que
# só o detentor consiga renovar/liberar o lock.
try:
    HOLDER = f"{getpass.getuser()}@{socket.gethostname()}"
except Exception:
    HOLDER = socket.gethostname()
TOKEN = uuid.uuid4().hex
LOCK_TTL = 90          # segundos sem heartbeat → lock considerado expirado
HEARTBEAT_EVERY = 30   # intervalo de renovação do heartbeat


async def _heartbeat_loop(pid: str):
    """Renova o heartbeat do lock enquanto a sessão estiver viva."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_EVERY)
            try:
                db.refresh_profile_lock(pid, TOKEN)
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


def _acquire_lock_or_raise(pid: str):
    """Adquire o lock compartilhado ou levanta RuntimeError (vira HTTP 409 na API)."""
    try:
        lock = db.acquire_profile_lock(pid, HOLDER, TOKEN, LOCK_TTL)
    except Exception as e:
        # Falha de banco não deve impedir o uso local — apenas registra.
        _log(f"acquire_profile_lock falhou (seguindo sem lock): {e}")
        return
    if not lock.get("ok"):
        raise RuntimeError(
            f"Perfil em uso por {lock.get('holder') or 'outro usuário'} "
            f"desde {lock.get('since') or '?'} (UTC). Aguarde liberar ou use outra máquina."
        )


def _log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} session_manager: {msg}\n"
    for p in (LOG_FILE, LOCAL_DEBUG_LOG):
        try:
            with p.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


def _patch_playwright_pageerror():
    """
    Corrige um bug do driver Playwright/Firefox: quando uma página dispara um erro
    JS sem `location` (comum em sites com ads pesados, ex.: ESPN), o driver tentava
    ler `pageError.location.url` e CRASHAVA o processo inteiro do driver — derrubando
    a sessão. Aplica optional chaining (`?.`). Idempotente; sobrevive a reinstalações.
    """
    try:
        import playwright
        core = Path(playwright.__file__).parent / "driver" / "package" / "lib" / "coreBundle.js"
        if not core.exists():
            return
        txt = core.read_text(encoding="utf-8")
        if 'pageError.location?.url || ""' in txt:
            return  # já corrigido (com fallback)
        # 1) garante optional chaining; 2) dá fallback de tipo válido (string/number),
        #    pois o validador do protocolo exige url:string, line/column:number.
        for a, b in (("pageError.location.url", "pageError.location?.url"),
                     ("pageError.location.lineNumber", "pageError.location?.lineNumber"),
                     ("pageError.location.columnNumber", "pageError.location?.columnNumber")):
            txt = txt.replace(a, b)
        txt = (txt.replace("pageError.location?.url,", 'pageError.location?.url || "",')
                  .replace("pageError.location?.lineNumber,", "pageError.location?.lineNumber || 0,")
                  .replace("pageError.location?.columnNumber\n", "pageError.location?.columnNumber || 0\n"))
        core.write_text(txt, encoding="utf-8")
        _log("playwright coreBundle: patch pageError aplicado")
    except Exception as e:
        _log(f"patch playwright falhou: {e}")


_patch_playwright_pageerror()


def browser_info() -> dict:
    try:
        import camoufox
        try:
            from importlib.metadata import version as _pkg_version
            ver = _pkg_version("camoufox")
        except Exception:
            # __version__ pode ser um SUBMÓDULO (camoufox/__version__.py), não uma string
            v = getattr(camoufox, "__version__", "desconhecida")
            ver = getattr(v, "__version__", None) or (v if isinstance(v, str) else "desconhecida")
        return {
            "available": True,
            "name": "Camoufox (Firefox)",
            "is_mimic": False,
            "path": str(Path(camoufox.__file__).parent),
            "version": str(ver),
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


def get_status(profile_id: str):
    s = _sessions.get(profile_id)
    if not s:
        return None
    return {
        "profile_id": profile_id,
        "status":     s["status"],
        "url":        s.get("url"),
        "error":      s.get("error"),
        "browser":    "Camoufox (Firefox)",
    }


def list_active() -> list:
    return [
        {
            "profile_id": pid,
            "status":     s["status"],
            "url":        s.get("url"),
            "browser":    "Camoufox (Firefox)",
            "mode":       s.get("mode"),
        }
        for pid, s in _sessions.items()
        if s["status"] in ("starting", "running")
    ]


def _build_proxy(proxy: dict | None) -> dict | None:
    if not proxy or not proxy.get("host"):
        return None
    scheme = proxy.get("protocol", "http")
    server = f"{scheme}://{proxy['host']}:{proxy['port']}"
    p = {"server": server}
    if proxy.get("username"):
        p["username"] = proxy["username"]
        p["password"] = proxy.get("password", "")
    return p


def _map_os(platform: str) -> str:
    pl = platform.lower()
    if "win" in pl:   return "windows"
    if "mac" in pl:   return "macos"
    return "linux"


# Mapeia o OS do perfil para a sigla usada pelo banco WebGL do Camoufox.
_WEBGL_OS = {"windows": "win", "macos": "mac", "linux": "lin"}


def _profile_seed(profile: dict) -> int:
    """
    Semente ESTÁVEL por perfil (entre sessões). Usada para fixar fingerprint/WebGL
    de forma determinística, evitando que o MESMO perfil "mude de aparelho" a cada
    reabertura (sinal de bot). Origem:
      1) fp_seed gravado no JSON do fingerprint (perfis novos);
      2) fallback: derivado do id do perfil (perfis antigos, sem fp_seed).
    """
    fp = profile.get("fingerprint") or {}
    seed = fp.get("fp_seed")
    if isinstance(seed, int):
        return seed
    pid = str(profile.get("id") or fp.get("userAgent") or "camoufox")
    return int.from_bytes(hashlib.sha256(pid.encode("utf-8")).digest()[:8], "big")


def _stable_webgl(profile: dict) -> tuple[str, str] | None:
    """
    Escolhe um par (vendor, renderer) WebGL FIXO para o perfil, derivado da
    semente. Sem isto, o Camoufox sorteia uma GPU nova a cada launch
    (sample_webgl via np.random) — a conta parece trocar de placa de vídeo
    entre sessões. Pega da lista de pares VÁLIDOS do OS (get_possible_pairs,
    ordenada por probabilidade desc) e enviesa para os mais comuns.
    """
    try:
        from camoufox.webgl.sample import get_possible_pairs
    except Exception:
        return None
    fp = profile.get("fingerprint") or {}
    os_key = _WEBGL_OS.get(_map_os(fp.get("platform", "windows")))
    pairs = (get_possible_pairs() or {}).get(os_key) if os_key else None
    if not pairs:
        return None
    rnd = random.Random(_profile_seed(profile) ^ 0x5745_4247)  # 'WEBG'
    # rnd.random()**2 enviesa o índice para o início (GPUs mais comuns).
    idx = min(len(pairs) - 1, int(rnd.random() ** 2 * len(pairs)))
    vendor, renderer = pairs[idx]
    return (vendor, renderer)


def _stable_fingerprint(profile: dict, os_name: str, screen, window):
    """
    Gera um Fingerprint Firefox DETERMINÍSTICO para o perfil (mesma semente →
    mesmo navigator/UA/hardwareConcurrency/audio entre sessões). Sem isto o
    Camoufox sorteia esses campos a cada launch.

    Usa o MESMO gerador interno do Camoufox (camoufox.fingerprints) com a tela
    da tentativa atual, semeando o random global com save/restore (síncrono e
    concorrência-safe). Em QUALQUER falha retorna None → o Camoufox gera o seu
    (comportamento atual), nunca derrubando o launch. Mantém a cascata de
    fallback de tela: cada tentativa reconstrói os kwargs e rechama este helper.
    """
    try:
        from camoufox.fingerprints import generate_fingerprint
        st = random.getstate()
        try:
            random.seed(_profile_seed(profile))
            cfg = {"os": os_name}
            if screen is not None:
                cfg["screen"] = screen
            return generate_fingerprint(window=window, **cfg)
        finally:
            random.setstate(st)
    except Exception as e:
        _log(f"profile={profile.get('id','')} fingerprint estavel falhou: {e}")
        return None


# ── Destravar um perfil ("Firefox is already running, but is not responding") ──
# O lock do APP (banco, TTL 90s) se recupera sozinho, mas o lock do FIREFOX
# (parent.lock dentro do user_data_dir) só sai quando o PROCESSO morre. Tarefas
# headless (Spider/aquecimento) rodam por run_profile_task e NÃO entram em
# _sessions — se o processo morre sem fechar o browser, sobra um Camoufox órfão
# segurando o perfil e o "Abrir" quebra com aquele diálogo do Firefox. Ao mandar
# ABRIR, liberamos o perfil primeiro.
_LOCK_FILES = ("parent.lock", ".parentlock", "lock")


def _pids_holding(user_data, name_re: str = "camoufox|firefox") -> list[int]:
    """PIDs cujo command line referencia ESTE user_data_dir. Alvo CIRÚRGICO: filtra
    também pelo NOME do processo (só o navegador), então não toca em navegadores de
    outros perfis, nem em extrações de outros perfis, nem em processos alheios.

    O filtro por nome não é cosmético: sem ele o próprio PowerShell que faz a busca
    aparecia no resultado (o caminho procurado está no command line DELE)."""
    import os as _os
    import re
    import subprocess
    import sys
    path = str(user_data)
    pids: list[int] = []
    try:
        if sys.platform.startswith("win"):
            esc = path.replace("'", "''")
            ps = ("Get-CimInstance Win32_Process | Where-Object { "
                  f"$_.Name -match '{name_re}' -and $_.CommandLine -like '*{esc}*'"
                  " } | Select-Object -ExpandProperty ProcessId")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, text=True, timeout=30)
            out = r.stdout or ""
        else:
            rx = re.compile(name_re, re.I)
            r = subprocess.run(["ps", "-eo", "pid=,command="],
                               capture_output=True, text=True, timeout=30)
            out = "\n".join(ln.split(None, 1)[0]
                            for ln in (r.stdout or "").splitlines()
                            if path in ln and rx.search(ln))
        for tok in out.split():
            tok = tok.strip()
            if tok.isdigit():
                pids.append(int(tok))
    except Exception as e:
        _log(f"nao consegui listar processos do perfil: {e}")
    me = _os.getpid()
    return [p for p in pids if p != me]


def _kill_pids(pids) -> int:
    import subprocess
    import sys
    killed = 0
    for p in pids:
        try:
            if sys.platform.startswith("win"):
                # /T mata a árvore (o Camoufox costuma ter processos filhos)
                r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(p)],
                                   capture_output=True, text=True, timeout=25)
                if r.returncode == 0:
                    killed += 1
            else:
                import os as _os
                import signal
                _os.kill(p, signal.SIGKILL)
                killed += 1
        except Exception:
            pass
    return killed


def _clear_locks(user_data) -> int:
    """Apaga os arquivos de lock do Firefox que sobraram (só faz sentido DEPOIS de
    garantir que nenhum processo os detém)."""
    n = 0
    try:
        d = Path(user_data)
        for name in _LOCK_FILES:
            f = d / name
            try:
                if f.exists() or f.is_symlink():
                    f.unlink()
                    n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


async def free_profile(profile: dict, close_others: bool = True,
                       reason: str = "relaunch") -> dict:
    """Garante que o perfil PODE abrir agora. Fecha a janela anterior deste perfil,
    encerra processos órfãos presos no seu user_data_dir, apaga locks de arquivo
    remanescentes e libera o lock do app.

    close_others=True fecha também as OUTRAS janelas rastreadas (pedido do usuário:
    abrir um navegador fecha o anterior). Só afeta o que foi aberto por launch() —
    tarefas headless de OUTROS perfis (Spider/aquecimento) não entram em _sessions
    e têm outro user_data_dir, então não são tocadas."""
    pid = profile["id"]
    out = {"closed": [], "killed": 0, "locks": 0}

    # 1) Outras janelas abertas pelo app → fecha (mantém uma só de cada vez).
    if close_others:
        for other in [k for k, s in _sessions.items()
                      if k != pid and s.get("status") in ("starting", "running")]:
            try:
                await stop(other, reason=f"{reason}: abrindo {pid}")
                out["closed"].append(other)
            except Exception as e:
                _log(f"nao consegui fechar a sessao anterior {other}: {e}")

    # 2) Sessão anterior DESTE perfil (fecha limpo: o finally de _run faz __aexit__).
    s = _sessions.get(pid)
    if s and s.get("status") in ("starting", "running"):
        try:
            await stop(pid, reason=reason)
            out["closed"].append(pid)
        except Exception as e:
            _log(f"nao consegui fechar a sessao anterior {pid}: {e}")

    # 3) Processos órfãos presos NESTE perfil + locks de arquivo remanescentes.
    user_data = profile.get("user_data")
    if user_data:
        pids = _pids_holding(user_data)
        if pids:
            _log(f"profile={pid} encerrando {len(pids)} processo(s) preso(s) no perfil: {pids}")
            out["killed"] = _kill_pids(pids)
            await asyncio.sleep(1.2)      # dá tempo do SO liberar o arquivo de lock
        out["locks"] = _clear_locks(user_data)

    # 4) Libera o lock do app (senão o launch a seguir bateria no próprio lock).
    try:
        db.release_profile_lock(pid, TOKEN)
    except Exception:
        pass
    return out


async def launch(profile: dict, url: str = "about:blank", headless: bool = False,
                 actions=None, mode: str = "manual", load_extension: bool = True,
                 load_ig_filler: bool = False,
                 free_first: bool = True, close_others: bool = False):
    """free_first: destrava ESTE perfil antes de abrir (fecha a sessão anterior dele,
    mata processo órfão preso no seu user_data_dir, limpa parent.lock). Antes isto
    levantava "Profile already running" e não resolvia o lock do Firefox.
    close_others: fecha também as janelas dos OUTROS perfis — só o botão "Abrir"
    manda True (o bulk-launch abre vários de propósito e não deve fechá-los)."""
    pid = profile["id"]
    if free_first:
        await free_profile(profile, close_others=close_others, reason=f"abrir ({mode})")
    elif pid in _sessions and _sessions[pid]["status"] in ("running", "starting"):
        raise RuntimeError(f"Profile {pid} already running")
    # Lock compartilhado: impede outra máquina de abrir o mesmo perfil ao mesmo tempo.
    _acquire_lock_or_raise(pid)
    _sessions[pid] = {"status": "starting", "url": url, "mode": mode}
    task = asyncio.create_task(_run(profile, url, headless, actions, pid, mode, load_extension, load_ig_filler=load_ig_filler))
    _sessions[pid]["task"] = task
    return {"profile_id": pid, "status": "starting"}


def _caller_hint() -> str:
    try:
        frames = traceback.extract_stack(limit=5)
        useful = [f for f in frames if not f.filename.endswith("session_manager.py")]
        f = useful[-1] if useful else frames[-2]
        return f"{Path(f.filename).name}:{f.lineno}:{f.name}"
    except Exception:
        return "unknown"


async def stop(profile_id: str, reason: str = "manual"):
    s = _sessions.get(profile_id)
    if not s:
        raise KeyError(f"Session not found: {profile_id}")
    _log(f"stop requested profile={profile_id} reason={reason} caller={_caller_hint()}")
    task = s.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _sessions[profile_id]["status"] = "stopped"


async def shutdown_all():
    """Fecha todos os navegadores abertos — evita processos Camoufox órfãos
    travando o user_data_dir após o app encerrar."""
    _log(f"shutdown_all requested caller={_caller_hint()} sessions={list(_sessions.keys())}")
    for pid, s in list(_sessions.items()):
        task = s.get("task")
        if task and not task.done():
            task.cancel()
        cm = s.get("cm")
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
        try:
            db.release_profile_lock(pid, TOKEN)
        except Exception:
            pass
        s["status"] = "stopped"
    _log("shutdown_all: navegadores encerrados")


# Força a localização (timezone/idioma/geolocation/WebRTC) para o BRASIL —
# Porto Alegre/RS — em vez de derivar do IP de saída (que variava conforme o proxy).
# É uma busca OFFLINE na GeoLite2 (o IP nunca é contatado). 189.6.244.1 resolve p/
# America/Sao_Paulo · -30.12,-51.17 · pt-BR. Trocar a cidade = outro IP BR;
# None = volta a derivar do IP real (geoip=True).
# ⚠ Só é COERENTE se o IP de saída também for BR (modem 4G BR / proxy BR). Com IP
# de saída estrangeiro, isso vira BR-fingerprint-em-IP-estrangeiro = incoerente.
_FORCE_GEOIP_IP = "189.6.244.1"


def _camoufox_kwargs(profile: dict, headless: bool, load_extension: bool = False,
                     pin_screen: bool = True, geoip: bool = True,
                     constrain_screen: bool = True,
                     load_ig_filler: bool = False) -> dict:
    """
    Monta os argumentos de launch do Camoufox a partir do perfil.

    - load_extension: só carrega a extensão mtzSpider quando o objetivo é scraping.
      Criação/aquecimento/verificação rodam SEM addon (extensão é sinal detectável).
    - load_ig_filler: carrega o preenchedor de cadastro (ig-filler-extension).
      Por padrao False; o fluxo automatico usa filler interno via page.evaluate,
      sem carregar a extensao.
    - pin_screen: fixa a resolução exata do perfil (estável entre sessões).
    - geoip: deriva fuso/locale/lat-long do IP do proxy (coerência anti-ban).
    - constrain_screen: quando False, NÃO passa o objeto Screen para o Camoufox.
      Isso evita falhas internas do browserforge (AttributeError 'dict' has no 'is_set'
      e "No headers based on this input") que acontecem com certas combinações de
      resolução exata + versão do browserforge/camoufox instalada.
    """
    fp        = profile["fingerprint"]
    proxy     = _build_proxy(profile.get("proxy"))
    user_data = Path(profile["user_data"])
    user_data.mkdir(parents=True, exist_ok=True)

    addons = []
    if load_extension and _EXT_PATH.exists():
        addons.append(str(_EXT_PATH))
    if load_ig_filler and _IG_FILLER_PATH.exists():
        addons.append(str(_IG_FILLER_PATH))

    w = fp["screen"]["width"]
    h = fp["screen"]["height"]

    kw = dict(
        headless=headless,
        os=_map_os(fp["platform"]),
        window=(max(800, int(w * 0.85)), max(600, int(h * 0.85))),
        proxy=proxy,
        addons=addons,
        persistent_context=True,
        user_data_dir=str(user_data),
        humanize=True,
        i_know_what_im_doing=True,
    )
    if geoip:
        # Antes: True (derivava do IP de saída → timezone variava com o proxy).
        # Agora: IP fixo BR (Porto Alegre) → timezone/idioma/geolocation sempre Brasil.
        kw["geoip"] = _FORCE_GEOIP_IP or True
    elif proxy:
        # Anti-leak: SEM geoip (fallback da cascata) e COM proxy, o Camoufox não
        # spoofa o WebRTC → o STUN (UDP, ignora o proxy HTTP) vazaria o IP REAL
        # residencial. Melhor bloquear o WebRTC do que vazar. Com geoip on isso
        # não roda: o Camoufox já spoofa webrtc:ipv4 p/ o IP do proxy (testado:
        # só o IP do proxy aparece, sem IP local/IPv6).
        kw["block_webrtc"] = True

    # GPU FIXA por perfil (estável entre sessões). Sem isto o Camoufox sorteia
    # vendor/renderer a cada launch — sinal de bot. Ver _stable_webgl.
    webgl = _stable_webgl(profile)
    if webgl:
        kw["webgl_config"] = webgl

    if constrain_screen:
        from browserforge.fingerprints import Screen
        if pin_screen:
            screen = Screen(min_width=w, max_width=w, min_height=h, max_height=h)
        else:
            screen = Screen(max_width=w, max_height=h)
        kw["screen"] = screen

    # Fingerprint base FIXO por perfil (navigator/UA/hardwareConcurrency/audio).
    # Gerado com a tela desta tentativa; se falhar, deixa o Camoufox gerar o seu.
    # Ver _stable_fingerprint. (Quando passamos fingerprint=, o Camoufox pula a
    # geração e ignora screen=/window= — por isso a tela já vai embutida nele.)
    fp_obj = _stable_fingerprint(profile, kw["os"], kw.get("screen"), kw["window"])
    if fp_obj is not None:
        kw["fingerprint"] = fp_obj

    return kw


async def _open_camoufox(profile: dict, headless: bool, load_extension: bool = False, load_ig_filler: bool = False):
    """
    Abre o Camoufox com fallback em cascata. Retorna (context_manager, context).
      1) tela fixa + geoip   (ideal — estável e coerente)
      2) tela por limite     (se a resolução exata quebrar a geração)
      3) sem geoip           (se o banco geoip faltar)
    """
    from camoufox.async_api import AsyncCamoufox
    pid = profile.get("id", "")
    attempts = [
        dict(pin_screen=True,  geoip=True,  constrain_screen=True),
        dict(pin_screen=False, geoip=True,  constrain_screen=True),
        dict(pin_screen=False, geoip=False, constrain_screen=True),
        dict(pin_screen=False, geoip=True,  constrain_screen=False),  # relax: evita falha interna de Screen/header no browserforge
        dict(pin_screen=False, geoip=False, constrain_screen=False),
    ]
    last = None
    for i, opt in enumerate(attempts):
        kw = _camoufox_kwargs(profile, headless, load_extension, load_ig_filler=load_ig_filler, **opt)
        try:
            cm = AsyncCamoufox(**kw)
            ctx = await cm.__aenter__()
            if i > 0:
                _log(f"profile={pid} aberto no fallback #{i} ({opt})")
            return cm, ctx
        except Exception as e:
            last = e
            _log(f"profile={pid} tentativa #{i} falhou: {e}")
    raise last


async def _run(profile, url, headless, actions, pid, mode, load_extension=True, load_ig_filler=False):
    proxy = _build_proxy(profile.get("proxy"))
    _log(f"launching profile={pid} headless={headless} proxy={bool(proxy)} ext={load_extension} ig_filler={load_ig_filler}")

    hb = asyncio.create_task(_heartbeat_loop(pid))
    try:
        # persistent_context=True faz o Camoufox usar launch_persistent_context,
        # mantendo cookies/localStorage no diretório do perfil. Nesse modo o objeto
        # retornado JÁ É um BrowserContext (não um Browser).
        # NÃO injetamos JS de fingerprint: o Camoufox já mascara tudo no motor de
        # forma coerente; sobrescrever via JS criava inconsistências (ex.: adicionar
        # deviceMemory/getBattery que o Firefox não tem) e era detectável.
        cm, ctx = await _open_camoufox(profile, headless, load_extension, load_ig_filler=load_ig_filler)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            _sessions[pid]["status"]  = "running"
            _sessions[pid]["page"]    = page
            _sessions[pid]["browser"] = "Camoufox (Firefox)"
            _sessions[pid]["cm"]      = cm

            if url and url != "about:blank":
                await page.goto(url, wait_until="domcontentloaded")
            _sessions[pid]["url"] = page.url

            if actions:
                await _run_actions(page, actions)
            else:
                while True:
                    live_pages = [p for p in getattr(ctx, "pages", []) if not p.is_closed()]
                    if not live_pages:
                        break
                    current = _sessions.get(pid, {}).get("page")
                    if current is None or current.is_closed():
                        _sessions[pid]["page"] = live_pages[-1]
                    await asyncio.sleep(1)
                _log(f"profile={pid} browser closed")
        finally:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    except asyncio.CancelledError:
        _log(f"profile={pid} task cancelled")
    except Exception as e:
        _sessions[pid]["status"]   = "error"
        _sessions[pid]["error"]    = str(e)
        _sessions[pid]["traceback"] = traceback.format_exc()
        _log(f"profile={pid} error={e}\n{_sessions[pid]['traceback']}")
    finally:
        hb.cancel()
        try:
            db.release_profile_lock(pid, TOKEN)
        except Exception:
            pass
        if _sessions.get(pid, {}).get("status") == "running":
            _sessions[pid]["status"] = "stopped"
        # Auto-verificação: se você ABRIU manualmente o perfil de uma conta (ex.: pra
        # logar no IG) e fechou a janela, checa o status sozinho (reabre headless,
        # detecta e atualiza) — sem precisar clicar em "Verificar".
        if mode == "manual":
            try:
                a = db.get_account_by_profile(pid)
                if a:
                    import accounts as _acc
                    asyncio.create_task(_acc.check_status(a["id"], headless=True))
                    _log(f"profile={pid} fechado — auto-verificando status da conta {a.get('id')}")
            except Exception:
                pass


async def run_profile_task(profile: dict, fn, headless: bool = False,
                           load_extension: bool = False, load_ig_filler: bool = False):
    """
    Abre o perfil e executa fn(page), devolvendo o resultado de fn.

    - Se o perfil já tem sessão ativa, REUSA a página aberta (Firefox trava o
      diretório de perfil; não dá para abrir 2 contextos sobre o mesmo user_data).
    - Senão, abre um contexto, roda fn e fecha (cookies/histórico ficam salvos
      no user_data_dir do perfil). Por padrão SEM extensão (aquecimento/verificação).
      Mantenha load_ig_filler=False salvo teste manual específico.
    """
    pid = profile["id"]
    s = _sessions.get(pid)
    if s and s.get("status") == "running" and s.get("page"):
        # Sessão já aberta neste processo (já detemos o lock) — reusa a página.
        return await fn(s["page"])

    # Abrindo um contexto novo: adquire o lock compartilhado e mantém heartbeat.
    _acquire_lock_or_raise(pid)
    hb = asyncio.create_task(_heartbeat_loop(pid))
    cm = None
    try:
        cm, ctx = await _open_camoufox(profile, headless, load_extension, load_ig_filler=load_ig_filler)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return await fn(page)
    finally:
        hb.cancel()
        try:
            db.release_profile_lock(pid, TOKEN)
        except Exception:
            pass
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass


async def check_account_status(profile: dict, detect_fn, headless: bool = True) -> dict:
    """Verifica o status de uma conta abrindo o perfil e chamando detect_fn(page)."""
    try:
        return await run_profile_task(profile, detect_fn, headless=headless)
    except Exception as e:
        _log(f"check_account_status profile={profile.get('id')} error={e}")
        return {"status": "unknown", "detail": f"erro ao verificar: {e}"}


async def _run_actions(page, actions):
    for action in (actions or []):
        t = action.get("type")
        if   t == "goto":       await page.goto(action["url"])
        elif t == "click":      await page.click(action["selector"])
        elif t == "fill":       await page.fill(action["selector"], action["value"])
        elif t == "wait":       await asyncio.sleep(action["ms"] / 1000)
        elif t == "screenshot": await page.screenshot(path=action.get("path", "screenshot.png"))
        elif t == "evaluate":   await page.evaluate(action["script"])
