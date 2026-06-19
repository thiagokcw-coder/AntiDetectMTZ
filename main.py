import threading
import time
import sys
import os
import socket
import traceback
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).parent

sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)

import asyncio
import urllib.request
import uvicorn

from paths import LOG_FILE


def _log(msg: str):
    """Anexa ao log (write_text truncava o arquivo a cada chamada)."""
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(msg if msg.endswith("\n") else msg + "\n")
    except Exception:
        pass


PORT = 18754
_api_ready = threading.Event()
_loop = None
_server = None

# ── Windows: impedir REINÍCIO/SUSPENSÃO enquanto o app está aberto ─────────────
# Motivação: o Windows Update reiniciou o PC sozinho de madrugada no meio de uma
# extração (perdeu progresso). Aqui o app pede ao Windows pra não dormir e
# registra um "bloqueio de desligamento" na janela (mostrado ao usuário). A prova
# de falha de verdade contra o reinício automático é a política do sistema
# NoAutoRebootWithLoggedOnUsers (aplicada à parte, exige admin).
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_shutdown_block_hwnd = None


def _win_keep_awake_loop():
    """Daemon: re-afirma 'sistema necessário' a cada 60s (impede o PC de dormir
    durante extrações longas). ES_CONTINUOUS mantém o estado até ser limpo."""
    if sys.platform != "win32":
        return
    import ctypes
    while True:
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        except Exception:
            pass
        time.sleep(60)


def _win_block_reboot():
    """Registra na janela do app um motivo de bloqueio de desligamento: enquanto o
    Monitor MTZ está aberto, o Windows adia o reinício e mostra a mensagem ao
    usuário. Tenta achar a janela pelo título (independe do backend do pywebview)."""
    global _shutdown_block_hwnd
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = 0
        for _ in range(20):                       # a janela pode ainda não existir
            hwnd = user32.FindWindowW(None, "Monitor MTZ")
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            return
        reason = "Extração/monitoramento em andamento — feche o Monitor MTZ antes de reiniciar."
        user32.ShutdownBlockReasonCreate(wintypes.HWND(hwnd), ctypes.c_wchar_p(reason))
        _shutdown_block_hwnd = hwnd
    except Exception:
        pass


def _win_unblock_reboot():
    """Remove o bloqueio de desligamento e libera a suspensão (ao sair do app)."""
    global _shutdown_block_hwnd
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        if _shutdown_block_hwnd:
            ctypes.windll.user32.ShutdownBlockReasonDestroy(wintypes.HWND(_shutdown_block_hwnd))
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)   # volta ao normal
    except Exception:
        pass
    _shutdown_block_hwnd = None


def find_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_event_loop():
    global _loop, PORT
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            PORT = find_free_port()

    async def serve():
        global _loop, _server
        _loop = asyncio.get_running_loop()
        try:
            print("DEBUG: run_event_loop thread started, importing api...")
            from api import app
            print(f"DEBUG: api imported successfully, starting uvicorn on {PORT}")
            _log(f"Iniciando API na porta {PORT}")
            config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
            _server = uvicorn.Server(config)
            _api_ready.set()
            print("DEBUG: api ready set, calling serve() - if it hangs here, check DB/init")
            await _server.serve()
        except Exception as e:
            print(f"DEBUG: exception in serve: {e}")
            _log(f"ERRO: {e}\n{traceback.format_exc()}")
            _api_ready.set()

    asyncio.run(serve())


def shutdown():
    """Fecha navegadores abertos e encerra o servidor — chamado ao sair do app."""
    _win_unblock_reboot()   # libera o bloqueio de reinício/suspensão
    if _loop and _loop.is_running():
        try:
            import session_manager as sm
            fut = asyncio.run_coroutine_threadsafe(sm.shutdown_all(), _loop)
            fut.result(timeout=10)
        except Exception:
            pass
    if _server is not None:
        _server.should_exit = True
        # Tenta forçar parada mais agressiva do uvicorn
        try:
            _server.handle_exit()
        except Exception:
            pass


def wait_api_ready(timeout=30):
    # Pinga /health (instantâneo, sem banco). NÃO usar /profiles: com banco remoto
    # ele faz dezenas de queries (segundos) e o ping de timeout curto nunca sucedia
    # → janela presa em "Carregando...".
    for _ in range(int(timeout / 0.5)):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=3)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def _mac_set_app_name(name: str = "Monitor MTZ"):
    """macOS: troca o nome exibido no menu/Dock (senão aparece 'Python').
    Precisa rodar ANTES do pywebview montar o menu (início do main())."""
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
        b = NSBundle.mainBundle()
        info = b.localizedInfoDictionary() or b.infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
    except Exception:
        pass


def _mac_set_dock_icon():
    """macOS: troca o ícone do Dock (senão aparece o foguete padrão do Python)."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage
        icon = BASE_DIR / "MonitorMtzMac.icns"
        if not icon.exists():
            return
        img = NSImage.alloc().initWithContentsOfFile_(str(icon))
        if img:
            NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception:
        pass


def on_loaded(window):
    print("DEBUG: on_loaded called, waiting for API ready (polling /profiles)...")
    _mac_set_dock_icon()  # reaplica após o app subir (mais confiável)
    # Bloqueia reinício do Windows enquanto a janela existe (título ainda "Monitor
    # MTZ" neste ponto; o bloqueio fica preso ao HWND mesmo que o título mude).
    _win_block_reboot()
    if wait_api_ready():
        print(f"DEBUG: API ready, loading URL http://127.0.0.1:{PORT}")
        window.load_url(f"http://127.0.0.1:{PORT}")
    else:
        print(f"DEBUG: wait_api_ready timed out, showing error (check {LOG_FILE})")
        window.load_html(
            "<html style='background:#0f1117;display:flex;align-items:center;justify-content:center;height:100vh'>"
            f"<p style='color:#ef4444;font-family:sans-serif;font-size:16px'>Erro ao iniciar API. Veja {LOG_FILE}</p></html>"
        )


def main():
    print("DEBUG: main() started, launching uvicorn thread...")
    # Nome do app no macOS (antes de qualquer uso de AppKit pelo pywebview).
    _mac_set_app_name()
    # Windows: impede o PC de dormir enquanto o app está aberto (daemon).
    threading.Thread(target=_win_keep_awake_loop, daemon=True).start()
    # API roda em thread dedicada com seu próprio event loop
    t = threading.Thread(target=run_event_loop, daemon=False)
    t.start()
    print("DEBUG: uvicorn thread launched, starting GUI...")

    # Tenta abrir janela nativa (PyWebView). No Windows usa WebView2.
    try:
        import webview
        _mac_set_dock_icon()  # ícone do Dock (foguete → MTZ)
        window = webview.create_window(
            title="Monitor MTZ",
            html="<html style='background:#0f1117;margin:0;display:flex;align-items:center;justify-content:center;height:100vh'>"
                 "<p style='color:#5c6ef8;font-family:-apple-system,Segoe UI,sans-serif;font-size:16px'>Carregando...</p></html>",
            width=1100,
            height=720,
            min_size=(800, 560),
            background_color="#0f1117",
        )
        webview.start(on_loaded, window, debug=False)
        # Janela fechada — encerra navegadores e servidor graciosamente
        shutdown()
        t.join(timeout=10)
        # Força o encerramento do processo mesmo que alguma thread/servidor
        # demore a parar — assim o terminal do run.bat fecha junto com o app.
        os._exit(0)
    except Exception:
        # Fallback: sem backend de GUI — abre no navegador padrão e mantém o servidor vivo
        if wait_api_ready():
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{PORT}")
            print(f"Interface aberta no navegador: http://127.0.0.1:{PORT}")
            print("Feche esta janela do terminal para encerrar o servidor.")
        else:
            print(f"Erro ao iniciar API. Veja {LOG_FILE}")
        try:
            t.join()
        except KeyboardInterrupt:
            shutdown()
            t.join(timeout=20)


if __name__ == "__main__":
    main()
