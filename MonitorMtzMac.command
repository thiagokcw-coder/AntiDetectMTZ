#!/usr/bin/env bash
# Antidetect Camoufox — inicializador para macOS / Linux.
# Equivalente ao run.bat (Windows): cria venv, instala deps, baixa Firefox/Camoufox
# e inicia o main.py. Pode ser dado duplo-clique no Finder (.command).
set -u

# ── Vai para a pasta do script (mesmo se aberto pelo Finder) ───────────────────
cd "$(dirname "$0")" || exit 1

VENV_DIR="venv"

# ── Detecta Python 3.10+ ───────────────────────────────────────────────────────
# Tenta versões específicas antes do generico (o 'python3' do PATH pode ser antigo,
# ex.: 3.7 do python.org, enquanto o Homebrew tem python3.11/3.12 ao lado).
PY_CMD=""
# Ordem de preferencia: versoes maduras (wheels prontos p/ psycopg/camoufox) primeiro.
for candidate in python3.12 python3.11 python3.13 python3.10 python3.14 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
            PY_CMD="$candidate"
            break
        fi
    fi
done

if [ -z "$PY_CMD" ]; then
    echo
    echo " ERRO: Python 3.10+ nao encontrado."
    echo " Instale via 'brew install python' ou em https://www.python.org/downloads/"
    echo
    read -r -p "Pressione Enter para fechar..." _
    exit 1
fi

echo " Python: $($PY_CMD --version 2>&1)"

# ── Cria venv ────────────────────────────────────────────────────────────────
if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo
    echo "[1/4] Criando ambiente virtual..."
    "$PY_CMD" -m venv "$VENV_DIR" || { echo " ERRO ao criar ambiente virtual."; read -r -p "Enter para fechar..." _; exit 1; }
fi

VPY="$VENV_DIR/bin/python"

# ── Instala dependencias (apenas na primeira vez) ────────────────────────────
if [ ! -f "$VENV_DIR/.installed" ]; then
    echo
    echo "[2/4] Instalando dependencias Python..."
    "$VPY" -m pip install --upgrade pip -q
    "$VPY" -m pip install -r requirements.txt || { echo " ERRO ao instalar dependencias. Verifique sua conexao."; read -r -p "Enter para fechar..." _; exit 1; }

    echo
    echo "[3/4] Instalando Firefox via Playwright..."
    "$VPY" -m playwright install firefox || { echo " ERRO ao instalar Firefox/Playwright."; read -r -p "Enter para fechar..." _; exit 1; }

    echo
    echo "[4/4] Baixando Camoufox (Firefox antidetect, ~150MB)..."
    "$VPY" -m camoufox fetch || { echo " ERRO ao baixar Camoufox."; read -r -p "Enter para fechar..." _; exit 1; }

    echo "ok" > "$VENV_DIR/.installed"
    echo
    echo " Instalacao concluida!"
fi

# ── Banco compartilhado (opcional) ───────────────────────────────────────────
# Se existir database_url.txt ao lado deste script, usa PostgreSQL compartilhado.
# A 1a linha do arquivo deve ser a connection string (use sslmode=require).
# Sem o arquivo (e sem DATABASE_URL definida), o app usa o SQLite local.
if [ -f "database_url.txt" ]; then
    DATABASE_URL="$(head -n 1 database_url.txt)"
    export DATABASE_URL
    echo " Banco: PostgreSQL compartilhado (database_url.txt)"
elif [ -n "${DATABASE_URL:-}" ]; then
    echo " Banco: PostgreSQL compartilhado (DATABASE_URL do ambiente)"
else
    echo " Banco: SQLite local (modo single-user)"
fi

# ── Inicia o app ─────────────────────────────────────────────────────────────
echo
echo " Iniciando Antidetect Camoufox..."
echo
"$VPY" main.py
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
    echo
    echo " App encerrou com erro (codigo $STATUS). Veja o log acima."
    echo
    read -r -p "Pressione Enter para fechar..." _
fi

# ========================================================
# Fecha automaticamente a janela do Terminal no macOS
# quando o app (GUI) é fechado. Assim o terminal some junto.
# ========================================================
if [ "$(uname)" = "Darwin" ]; then
    sleep 0.7
    osascript -e '
        tell application "Terminal"
            try
                -- Tenta fechar janelas que parecem ser deste launcher
                set scriptName to "MonitorMtzMac"
                set theWindows to (every window whose name contains scriptName or name ends with ".command")
                if (count of theWindows) > 0 then
                    repeat with w in theWindows
                        close w saving no
                    end repeat
                else
                    -- fallback: fecha a janela da frente (comum ao dar duplo-clique)
                    close front window saving no
                end if
            end try
        end tell
    ' >/dev/null 2>&1 || true
fi


