@echo off
REM Forces the iPad to reload the dashboard, without touching the iPad.
REM
REM The iPad is mounted inside the PC case, so it cannot be reloaded by hand.
REM The page reloads itself in two situations, both detected on a SUCCESSFUL
REM poll (never during an outage, which would strand it on an error page):
REM
REM   1. the server reports a newer build than the page loaded
REM   2. the server was unreachable for more than two minutes and came back
REM
REM This script triggers case 2: stop the server, wait past the threshold,
REM start it again. The iPad reloads a second or two after it returns.
REM
REM NOTE: case 2 only works if the page is already running a build that
REM contains this logic. If the iPad is on older code than that, it will sit
REM on "no signal" and then simply reconnect without reloading - in which case
REM the iPad has to be reached once by hand. After that it self-updates.

setlocal
cd /d "%~dp0"

echo Stopping the dashboard server...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-Process CaseGauge -EA SilentlyContinue | Stop-Process -Force -EA SilentlyContinue;" ^
  "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" -EA SilentlyContinue | Where-Object { $_.CommandLine -like '*server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }"

echo Waiting 150 seconds so the iPad registers a long outage...
powershell -NoProfile -Command "Start-Sleep -Seconds 150"

echo Starting the server again...
if exist "%~dp0CaseGauge.exe" ( start "" "%~dp0CaseGauge.exe" ) else ( start "" pythonw.exe "%~dp0server.py" )

echo.
echo Done. The iPad should reload within a few seconds of reconnecting.
pause >nul
