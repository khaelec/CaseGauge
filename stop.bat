@echo off
REM Stops the dashboard, however it was started (CaseGauge.exe or server.py).
REM
REM Normally you would use the tray icon's Quit. This is the guaranteed escape
REM hatch: the app has no console and no taskbar button, so if the tray ever
REM fails to appear there would otherwise be no way to stop it.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$killed = 0;" ^
  "Get-Process CaseGauge -EA SilentlyContinue | ForEach-Object { Write-Host ('stopping CaseGauge.exe pid ' + $_.Id); Stop-Process -Id $_.Id -Force -EA SilentlyContinue; $killed++ };" ^
  "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" -EA SilentlyContinue | Where-Object { $_.CommandLine -like '*server.py*' } | ForEach-Object { Write-Host ('stopping server.py pid ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue; $killed++ };" ^
  "if ($killed -eq 0) { Write-Host 'dashboard was not running' };" ^
  "Start-Sleep -Seconds 1;" ^
  "if (Get-NetTCPConnection -LocalPort 8777 -State Listen -EA SilentlyContinue) { Write-Host 'WARNING: port 8777 still held' } else { Write-Host 'stopped; port 8777 free' }"

echo.
echo Press any key to close.
pause >nul
