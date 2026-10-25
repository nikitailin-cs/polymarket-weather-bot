@echo off
rem Stop the windows opened by start_local.bat (daemon + dashboard) and any cycle they spawned.
rem   stop_local.bat          stop both
rem   stop_local.bat daemon   stop only the daemon
rem   stop_local.bat ui       stop only the dashboard
setlocal
set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"

if /i "%MODE%"=="ui" goto ui
echo [stop_local] stopping METAR.ws daemon...
taskkill /FI "WINDOWTITLE eq WeatherBot Daemon*" /T /F >nul 2>&1 && echo [stop_local] daemon stopped || echo [stop_local] daemon was not running
if /i "%MODE%"=="daemon" goto done

:ui
echo [stop_local] stopping dashboard...
taskkill /FI "WINDOWTITLE eq WeatherBot Dashboard*" /T /F >nul 2>&1 && echo [stop_local] dashboard stopped || echo [stop_local] dashboard was not running

:done
endlocal
