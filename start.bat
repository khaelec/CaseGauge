@echo off
REM Starts the dashboard and exits immediately. The app itself has no console
REM and no taskbar button - control it from its tray icon, or with stop.bat.
REM
REM Closing this window does NOT stop the dashboard; nothing is left attached.
cd /d "%~dp0"
if exist "%~dp0CaseGauge.exe" (
  start "" "%~dp0CaseGauge.exe"
) else (
  start "" pythonw.exe "%~dp0server.py"
)
