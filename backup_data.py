"""
backup_data.py — reúne TODO o "local data" num único ZIP, fácil de copiar e
restaurar em outra máquina.

O que entra no ZIP:
  app/profiles.db    -> %LOCALAPPDATA%\\ProfileManager\\profiles.db   (banco SQLite local)
  app/user_data/     -> ...\\ProfileManager\\user_data\\               (perfis Camoufox: cookies/sessões)
  app/ig_sessions/   -> ...\\ProfileManager\\ig_sessions\\             (sessões instagrapi)
  repo/proxys/       -> <repo>\\proxys\\                               (listas de proxy)
  repo/database_url.txt -> <repo>\\database_url.txt                   (Postgres compartilhado, se existir)
NÃO inclui logs (não são necessários para migrar).

Uso:
  python backup_data.py backup  [arquivo.zip]     # cria o ZIP (padrão: com data/hora)
  python backup_data.py restore <arquivo.zip>     # restaura na máquina atual

Restaurar SOBRESCREVE os arquivos existentes nos destinos. Feche o app antes.
"""
import sys
import time
import shutil
import zipfile
from pathlib import Path

from paths import APP_DIR   # %LOCALAPPDATA%\ProfileManager (ou equivalente por SO)

REPO = Path(__file__).resolve().parent

# Subpastas de CACHE (lixo regenerável) dos perfis Camoufox/Firefox — não migram,
# só incham o ZIP. Cookies/sessões/storage (o que importa) ficam de fora destas.
_SKIP_DIRS = {"cache2", "startupcache", "shader-cache", "shadercache", "offlinecache",
              "thumbnails", "crashes", "minidumps", "datareporting", "saved-telemetry-pings"}


def _is_junk(rel: Path) -> bool:
    return any(part.lower() in _SKIP_DIRS for part in rel.parts)


def _items():
    """(origem_absoluta, prefixo_dentro_do_zip)."""
    return [
        (APP_DIR / "profiles.db",   "app/profiles.db"),
        (APP_DIR / "user_data",     "app/user_data"),
        (APP_DIR / "ig_sessions",   "app/ig_sessions"),
        (REPO / "proxys",           "repo/proxys"),
        (REPO / "database_url.txt", "repo/database_url.txt"),
    ]


def backup(dest: str):
    dest = Path(dest)
    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for src, prefix in _items():
            if not src.exists():
                print(f"  (pulado, nao existe) {src}")
                continue
            if src.is_file():
                z.write(src, prefix)
                n += 1
            else:
                skipped = 0
                for f in src.rglob("*"):
                    if not f.is_file():
                        continue
                    relp = f.relative_to(src)
                    if _is_junk(relp):          # pula cache/lixo regenerável
                        skipped += 1
                        continue
                    z.write(f, f"{prefix}/{str(relp).replace(chr(92), '/')}")
                    n += 1
                print(f"  + {prefix}" + (f"  (cache ignorado: {skipped} arq.)" if skipped else ""))
                continue
            print(f"  + {prefix}")
    size_mb = dest.stat().st_size / (1024 * 1024)
    print(f"\nOK: {n} arquivo(s), {size_mb:.1f} MB -> {dest}")
    print("Copie esse ZIP para a outra maquina e rode: python backup_data.py restore <arquivo.zip>")


def restore(zip_path: str):
    zip_path = Path(zip_path)
    if not zip_path.exists():
        print(f"ERRO: ZIP nao encontrado: {zip_path}")
        sys.exit(1)
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            if name.startswith("app/"):
                target = APP_DIR / name[len("app/"):]
            elif name.startswith("repo/"):
                target = REPO / name[len("repo/"):]
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
    print(f"OK: {n} arquivo(s) restaurado(s).")
    print(f"  app  -> {APP_DIR}")
    print(f"  repo -> {REPO}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "backup":
        if len(sys.argv) > 2:
            dest = sys.argv[2]
        else:
            stamp = time.strftime("%Y%m%d-%H%M")
            dest = str(REPO / f"profilemanager-backup-{stamp}.zip")
        backup(dest)
    elif mode == "restore":
        if len(sys.argv) < 3:
            print("uso: python backup_data.py restore <arquivo.zip>")
            sys.exit(1)
        restore(sys.argv[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
