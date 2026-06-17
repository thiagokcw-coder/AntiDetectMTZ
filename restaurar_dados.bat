@echo off
REM Restaura o local data de um ZIP gerado pelo backup_dados.bat.
REM Uso: arraste o .zip para cima deste arquivo, OU rode e informe o caminho.
setlocal
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
  set "PY=venv\Scripts\python.exe"
) else (
  set "PY=python"
)
set "ZIP=%~1"
if "%ZIP%"=="" set /p "ZIP=Caminho do arquivo .zip: "
echo Fechar o app antes de restaurar (os arquivos serao sobrescritos).
"%PY%" backup_data.py restore "%ZIP%"
pause
endlocal
