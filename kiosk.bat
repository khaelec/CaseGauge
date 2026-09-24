@echo off
REM Opens the dashboard fullscreen on the pinned monitor.
REM
REM   kiosk.bat            open on the pinned monitor
REM   kiosk.bat -List      show monitors and which one is pinned
REM   kiosk.bat -Monitor 3 pin monitor 3 and open there
REM   kiosk.bat -Wait      wait for the display to appear first (startup)
REM
REM Alt+F4 closes the window.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kiosk.ps1" %*
if errorlevel 1 pause
