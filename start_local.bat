@echo off
rem ---------------------------------------------------------------------------
rem Start the local paper-trading stack in separate console windows:
rem   - METAR.ws daemon (push mode: every live report triggers one trading cycle)
rem   - ops dashboard   http://127.0.0.1:8765/
rem
rem   start_local.bat            start daemon + dashboard
rem   start_local.bat daemon     start only the daemon
rem   start_local.bat ui         start only the dashboard
rem   start_local.bat refresh    refresh data\icao_mapping.json from Polymarket, then start all
rem
rem Settings come from .env (METAR_WS_API_KEY, METAR_SOURCE=metar_ws, DRY_RUN=1 ...).
rem Close a window or run stop_local.bat to stop. Each window keeps its log on screen.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist ".env" (
    echo [start_local] .env not found. Copy .env.example to .env and set METAR_WS_API_KEY.
    exit /b 1
)

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" -c "import httpx, websockets, dotenv" 2>nul || (
    echo [start_local] Python dependencies missing for "%PY%". Run: "%PY%" -m pip install -r requirements.txt
    exit /b 1
)

set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"

if /i "%MODE%"=="refresh" (
    echo [start_local] refreshing data\icao_mapping.json from Polymarket...
    "%PY%" bots\metar_ws\refresh_mapping.py || exit /b 1
    set "MODE=all"
)

if /i "%MODE%"=="daemon" goto daemon
if /i "%MODE%"=="ui" goto ui
if /i "%MODE%"=="all" goto daemon
echo [start_local] unknown mode "%MODE%" (use: all, daemon, ui, refresh)
exit /b 1

:daemon
rem one trading cycle per live observation, coalesced over 10 s; paper settlement runs inside run_cycle.py
set "METAR_SOURCE=metar_ws"
if "%METAR_WS_TRIGGER_CMD%"=="" set "METAR_WS_TRIGGER_CMD=%PY% bots\metar_ws\run_cycle.py"
if "%METAR_WS_TRIGGER_DEBOUNCE_SEC%"=="" set "METAR_WS_TRIGGER_DEBOUNCE_SEC=10"
echo [start_local] starting METAR.ws daemon (push mode, trigger: %METAR_WS_TRIGGER_CMD%)
start "WeatherBot Daemon" cmd /k ""%PY%" bots\metar_ws\metar_ws_daemon.py"
if /i "%MODE%"=="daemon" goto done

:ui
set "METAR_SOURCE=metar_ws"
if "%UI_PORT%"=="" set "UI_PORT=8765"
echo [start_local] starting dashboard on http://127.0.0.1:%UI_PORT%/
start "WeatherBot Dashboard" cmd /k ""%PY%" -m ui.server"
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:%UI_PORT%/"

:done
echo [start_local] running. Stop with stop_local.bat or by closing the windows.
endlocal
