"""
Caminhos persistentes do app, resolvidos por sistema operacional.
Windows  : %LOCALAPPDATA%\\ProfileManager
macOS    : ~/Library/Application Support/ProfileManager
Linux    : ~/.local/share/ProfileManager
"""
import os
import platform
from pathlib import Path


def _app_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ProfileManager"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "ProfileManager"
    return Path.home() / ".local" / "share" / "ProfileManager"


APP_DIR = _app_dir()
APP_DIR.mkdir(parents=True, exist_ok=True)

PROFILES_DIR = APP_DIR / "user_data"
PROFILES_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = APP_DIR / "profiles.db"

LOGS_DIR = APP_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "ProfileManager.log"
