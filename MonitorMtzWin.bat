@echo off
setlocal
cd /d "%~dp0"
title Monitor MTZ

set "VENV_DIR=venv"
set "PY_CMD=python"

:: ── Detecta Python ───────────────────────────────────────────────────────────
where python >nul 2>nul
if not errorlevel 1 goto :found_python

if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
    set "PY_CMD=%LocalAppData%\Programs\Python\Python311\python.exe"
    goto :found_python
)
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    set "PY_CMD=%LocalAppData%\Programs\Python\Python312\python.exe"
    goto :found_python
)
where py >nul 2>nul
if not errorlevel 1 ( set "PY_CMD=py" & goto :found_python )

echo.
echo  ERRO: Python nao encontrado.
echo  Instale Python 3.10+ em python.org e marque "Add to PATH".
echo.
goto :fim

:found_python
echo  Python: %PY_CMD%

:: ── Cria venv ────────────────────────────────────────────────────────────────
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo.
    echo [1/4] Criando ambiente virtual...
    "%PY_CMD%" -m venv %VENV_DIR%
    if errorlevel 1 (
        echo  ERRO ao criar ambiente virtual.
        goto :fim
    )
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
    echo  ERRO ao ativar ambiente virtual.
    goto :fim
)

:: ── Instala dependencias (apenas na primeira vez) ────────────────────────────
if not exist "%VENV_DIR%\.installed" (
    echo.
    echo [2/4] Instalando dependencias Python...
    "%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip -q
    "%VENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo  ERRO ao instalar dependencias. Verifique sua conexao.
        goto :fim
    )

    echo.
    echo [3/4] Instalando Firefox via Playwright...
    "%VENV_DIR%\Scripts\python.exe" -m playwright install firefox
    if errorlevel 1 (
        echo  ERRO ao instalar Firefox/Playwright.
        goto :fim
    )

    echo.
    echo [4/4] Baixando Camoufox ^(Firefox antidetect, ~150MB^)...
    "%VENV_DIR%\Scripts\python.exe" -m camoufox fetch
    if errorlevel 1 (
        echo  ERRO ao baixar Camoufox.
        goto :fim
    )

    echo ok> "%VENV_DIR%\.installed"
    echo.
    echo  Instalacao concluida!
)

:: ── Banco compartilhado (opcional) ───────────────────────────────────────────
:: Se existir database_url.txt ao lado deste .bat, usa PostgreSQL compartilhado.
:: A 1a linha do arquivo deve ser a connection string (use sslmode=require).
:: Sem o arquivo (e sem DATABASE_URL definida), o app usa o SQLite local.
if exist "database_url.txt" (
    set /p DATABASE_URL=<database_url.txt
    echo  Banco: PostgreSQL compartilhado ^(database_url.txt^)
) else (
    if defined DATABASE_URL (
        echo  Banco: PostgreSQL compartilhado ^(DATABASE_URL do ambiente^)
    ) else (
        echo  Banco: SQLite local ^(modo single-user^)
    )
)

:: ── Inicia o app ─────────────────────────────────────────────────────────────
echo.
echo  Iniciando Antidetect Camoufox...
echo.
"%VENV_DIR%\Scripts\python.exe" main.py
if errorlevel 1 (
    echo.
    echo  App encerrou com erro. Veja o log acima.
    echo.
    pause
)

:: Fechamento NORMAL do app: fecha o terminal junto (sem precisar apertar tecla).
:: Em caso de erro, o "pause" acima segura a janela para voce ler antes de fechar.
endlocal
exit

:fim
:: Erros de instalacao/preparacao: mantem a janela aberta para leitura.
echo.
pause
endlocal
exit
