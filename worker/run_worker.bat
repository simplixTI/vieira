@echo off
rem ============================================================
rem run_worker.bat — worker do portal em modo producao (loop).
rem Para dry-run sem rede: troque --producao por --stub --limite 5
rem ============================================================
setlocal
cd /d "%~dp0"

rem O .venv original do export_vps quebra nesta maquina (aponta pro Python
rem de outro usuario). Preferimos o .venv_new, que tem supabase/dotenv/requests.
set "PY=..\export_vps\.venv_new\Scripts\python.exe"
if not exist "%PY%" set "PY=..\export_vps\.venv\Scripts\python.exe"

"%PY%" main.py --producao %*
set "EXITCODE=%ERRORLEVEL%"

echo.
echo [run_worker] encerrado com codigo %EXITCODE%. Log em logs\worker_%DATE:~6,4%%DATE:~3,2%%DATE:~0,2%.log
exit /b %EXITCODE%
