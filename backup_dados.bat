@echo off
REM Reune todo o local data (banco + perfis/cookies + sessoes + proxys) num ZIP.
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
  set "PY=venv\Scripts\python.exe"
) else (
  set "PY=python"
)
"%PY%" backup_data.py backup
echo.
echo Pronto. Copie o .zip gerado nesta pasta para a outra maquina.
pause
