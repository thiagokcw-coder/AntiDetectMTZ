"""
logsetup.py — logging persistente em arquivo (com rotação).

Antes, os logger.exception()/logger.warning() dos módulos (instagram, sms_activate,
profile_warmup, batch_import…) não iam para lugar nenhum, pois o logging do Python
nunca era configurado. Aqui ligamos handlers de arquivo para o logger raiz, então
TODO log desses módulos passa a ser gravado — útil para investigar erros depois
(ex.: conta bloqueada pelo IG, falha de proxy, timeout de código por email).

Arquivos (em %LOCALAPPDATA%\ProfileManager\logs):
  - app.log     → tudo (INFO+)
  - errors.log  → só problemas (WARNING+), com traceback
"""
import logging
from logging.handlers import RotatingFileHandler

from paths import LOGS_DIR

APP_LOG = LOGS_DIR / "app.log"
ERROR_LOG = LOGS_DIR / "errors.log"

_configured = False


def setup_logging(level: int = logging.INFO):
    """Configura handlers de arquivo no logger raiz. Idempotente."""
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    app_h = RotatingFileHandler(APP_LOG, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    app_h.setLevel(level)
    app_h.setFormatter(fmt)

    err_h = RotatingFileHandler(ERROR_LOG, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    err_h.setLevel(logging.WARNING)
    err_h.setFormatter(fmt)

    root.addHandler(app_h)
    root.addHandler(err_h)


def tail(path, lines: int = 200) -> str:
    """Devolve as últimas `lines` linhas de um arquivo de log (ou string vazia)."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return "".join(f.readlines()[-lines:])
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"(erro ao ler log: {e})"
