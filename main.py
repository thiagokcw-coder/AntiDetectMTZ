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
    if _loop and _loop.is_running():
        try:
            import session_manager as sm
            fut = asyncio.run_coroutine_threadsafe(sm.shutdown_all(), _loop)
            fut.result(timeout=15)
        except Exception:
            pass
    if _server is not None:
        _server.should_exit = True


def wait_api_ready(timeout=20):
    for _ in range(int(timeout / 0.3)):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/profiles", timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
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
