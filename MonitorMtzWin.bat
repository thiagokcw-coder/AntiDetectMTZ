@echo off
setlocal
cd /d "%~dp0"
title Monitor MTZ

set "VENV_DIR=venv"
set "PY_CMD="

:: ── Detecta Python ───────────────────────────────────────────────────────────
:: Nao basta "where python": o Windows deixa um ATALHO da Microsoft Store em
:: %LocalAppData%\Microsoft\WindowsApps\python.exe que aparece no PATH mas NAO e
:: Python (so abre a Store e retorna erro). Por isso cada candidato e testado
:: rodando de verdade + checando a versao (3.10+).
call :try_py python
call :try_py "%LocalAppData%\Programs\Python\Python313\python.exe"
call :try_py "%LocalAppData%\Programs\Python\Python312\python.exe"
call :try_py "%LocalAppData%\Programs\Python\Python311\python.exe"
call :try_py "%ProgramFiles%\Python313\python.exe"
call :try_py "%ProgramFiles%\Python312\python.exe"
call :try_py "%ProgramFiles%\Python311\python.exe"
call :try_py py
if defined PY_CMD goto :found_python

:: ── Sem Python: instala sozinho via winget (vem no Windows 10/11) ────────────
winget --version >nul 2>nul
if errorlevel 1 goto :no_python

echo.
echo [0/4] Python nao encontrado. Instalando Python 3.12 automaticamente...
echo       (leva alguns minutos; pode aparecer um pedido de permissao do Windows)
echo.
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements

echo.
echo  Verificando a instalacao...
call :try_py "%LocalAppData%\Programs\Python\Python312\python.exe"
call :try_py "%ProgramFiles%\Python312\python.exe"
call :try_py python
call :try_py py
if defined PY_CMD goto :found_python

echo.
echo  Python instalado, mas esta janela ainda nao o enxerga (o PATH so atualiza
echo  em terminais novos). FECHE esta janela e rode o MonitorMtzWin.bat de novo.
echo.
goto :fim

:no_python
echo.
echo  ERRO: Python 3.10+ nao encontrado e o winget nao esta disponivel.
echo.
echo  Baixe em https://www.python.org/downloads/windows/ e no instalador
echo  MARQUE a caixa "Add python.exe to PATH".
echo.
echo  OBS: o atalho "python" da Microsoft Store NAO serve (ele so abre a Store).
echo  Se o instalador nao resolver, desligue o alias em:
echo  Configuracoes ^> Aplicativos ^> Configuracoes avancadas ^> Aliases de execucao.
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

:: ── Sub-rotina: aceita o candidato so se ele EXECUTAR e for 3.10+ ────────────
:try_py
if defined PY_CMD goto :eof
%1 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PY_CMD=%~1"
goto :eof
