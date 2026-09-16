@echo off
setlocal EnableExtensions
rem ═══════════════════════════════════════════════════════════════════
rem  run_crmlite.bat — automação do worker TSE contra o CRM Lite.
rem
rem  Passos:
rem   (a) garante o worker AutoHotkey (tse_clicker.ahk) rodando;
rem   (b) garante o Chrome aberto na página de consulta do TSE;
rem   (c) roda  python crmlite_main.py --producao  (loop até zerar a fila);
rem   (d) sai com o código de saída do Python.
rem
rem  Pensado para o Agendador de Tarefas (a cada 15 min) — veja
rem  AUTOMACAO-CRMLITE.md. Se já houver uma execução em andamento,
rem  o próprio Python para rápido (fila zerada ou sem progresso).
rem ═══════════════════════════════════════════════════════════════════
cd /d "%~dp0"

rem ── (a) Worker AutoHotkey ────────────────────────────────────────────
rem Procura um processo AutoHotkey cuja linha de comando mencione tse_clicker.
set "AHK_COUNT=0"
for /f %%i in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'AutoHotkey*' -and $_.CommandLine -like '*tse_clicker*' }).Count"') do set "AHK_COUNT=%%i"

if "%AHK_COUNT%"=="0" (
    echo [run_crmlite] Worker AHK nao encontrado — iniciando tse_clicker.ahk...
    rem Localiza o AutoHotkey64.exe (instalacao padrao v2; depois por usuario).
    set "AHK_EXE=C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"
    if not exist "%AHK_EXE%" set "AHK_EXE=C:\Program Files\AutoHotkey\AutoHotkey64.exe"
    if not exist "%AHK_EXE%" set "AHK_EXE=%LOCALAPPDATA%\Programs\AutoHotkey\v2\AutoHotkey64.exe"
    if not exist "%AHK_EXE%" set "AHK_EXE=%LOCALAPPDATA%\Programs\AutoHotkey\AutoHotkey64.exe"
    if not exist "%AHK_EXE%" (
        echo [run_crmlite] ERRO: AutoHotkey64.exe nao encontrado. Instale o AHK v2 ^(veja LEIA-ME.md^).
        exit /b 2
    )
    start "" "%AHK_EXE%" "%~dp0tse_clicker.ahk"
    rem Da um tempo pro worker subir e mostrar o "pronto".
    timeout /t 3 /nobreak >nul
) else (
    echo [run_crmlite] Worker AHK ja esta rodando.
)

rem ── (b) Chrome na página de consulta do TSE ──────────────────────────
rem Se nao houver nenhum chrome.exe, abre na pagina de NUMERO DO TITULO
rem (a fase B tambem usa a pagina de situacao via fallback, mas o worker
rem navega/parti da principal — veja LEIA-ME.md, secao 4).
tasklist /FI "IMAGENAME eq chrome.exe" /NH 2>nul | find /I "chrome.exe" >nul
if errorlevel 1 (
    echo [run_crmlite] Chrome fechado — abrindo na pagina de consulta do TSE...
    start "" "chrome" "https://www.tse.jus.br/servicos-eleitorais/autoatendimento-eleitoral#/atendimento-eleitor/consultar-numero-titulo-eleitor"
    timeout /t 5 /nobreak >nul
) else (
    echo [run_crmlite] Chrome ja esta aberto.
)

rem ── (c) Python do venv local ─────────────────────────────────────────
rem Prioridade: .venv → env_bg → python do PATH.
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0env_bg\Scripts\python.exe" set "PY=%~dp0env_bg\Scripts\python.exe"
if not defined PY set "PY=python"
echo [run_crmlite] Python: %PY%

rem ── (d) Roda o orquestrador em modo producao ─────────────────────────
rem --producao: fase A (enriquecimento) + fase B (coleta TSE) em loop
rem ate a fila de pendentes zerar (ou travar sem progresso).
"%PY%" "%~dp0crmlite_main.py" --producao
set "RC=%errorlevel%"

echo [run_crmlite] Fim (exit code %RC%).
endlocal & exit /b %RC%
