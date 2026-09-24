<#
    Opens the dashboard fullscreen on a chosen monitor.

      .\kiosk.ps1 -List            show monitors and which one is pinned
      .\kiosk.ps1 -Monitor 3       pin monitor 3 and open there
      .\kiosk.ps1                  open on the pinned monitor
      .\kiosk.ps1 -Wait            wait for it to appear first (use at startup)

    The pin is stored in kiosk.json beside this script. It records the device
    name AND the resolution, because a spacedesk display only exists while the
    iPad is connected, and the numbering shifts as monitors come and go - so a
    bare index would point somewhere else by morning.
#>
[CmdletBinding()]
param(
    [switch]$List,
    [int]$Monitor = 0,
    [switch]$Wait,
    [int]$WaitSeconds = 300,
    [switch]$ServerOnly,
    [string]$Url = ""
)

# Deliberately NOT calling SetProcessDPIAware. Windows is at 125% here, so a
# DPI-aware process sees DISPLAY5 as 960x1280 at (6400,-134) while this one
# sees 768x1024 at (5120,-107). Chromium places the window in whichever space
# it is handed, so reading and passing coordinates from the same unaware
# process keeps them consistent. Making this script DPI-aware would require
# changing the numbers passed to Edge to match.
Add-Type -AssemblyName System.Windows.Forms

$Here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $Here "kiosk.json"
$ProfileDir = Join-Path $env:TEMP "casegauge-kiosk"

# Follow the same port the server resolves (--port / PCSTATS_PORT /
# casegauge.json), so the kiosk window points at the server the tray reports.
if (-not $Url) {
    $p = $env:PCSTATS_PORT
    $portCfg = Join-Path $Here "casegauge.json"
    if (-not $p -and (Test-Path $portCfg)) {
        try { $p = (Get-Content $portCfg -Raw | ConvertFrom-Json).port } catch { }
    }
    if (-not $p) { $p = 8777 }
    $Url = "http://127.0.0.1:$p/"
}

function Ensure-Server {
    try {
        $r = Invoke-WebRequest -Uri ($Url + "api/stats") -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { return $true }
    } catch { }

    $py = Join-Path $Here "server.py"
    # pythonw rather than python: no console window, nothing on the taskbar.
    $pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($pythonw -and (Test-Path $py)) {
        Write-Host "Starting server.py..."
        Start-Process $pythonw.Source -ArgumentList $py -WorkingDirectory $Here -WindowStyle Hidden
        Start-Sleep -Seconds 4
        return $true
    }
    Write-Warning "server.py is not running and pythonw was not found - start it yourself."
    return $false
}

function Get-Screens { [System.Windows.Forms.Screen]::AllScreens }

function Get-Pin {
    if (Test-Path $ConfigPath) {
        try { return Get-Content $ConfigPath -Raw | ConvertFrom-Json } catch { return $null }
    }
    return $null
}

function Resolve-Screen($pin) {
    if ($null -eq $pin) { return $null }
    $screens = Get-Screens
    # Device name first - exact and stable while the display exists.
    $hit = $screens | Where-Object { $_.DeviceName -eq $pin.DeviceName } | Select-Object -First 1
    if ($hit) { return $hit }
    # Then resolution, which survives spacedesk handing out a new device name.
    $hit = $screens | Where-Object {
        $_.Bounds.Width -eq $pin.Width -and $_.Bounds.Height -eq $pin.Height -and -not $_.Primary
    } | Select-Object -First 1
    return $hit
}

# ---- server only -----------------------------------------------------------

# Used when the dashboard is viewed in Safari on the iPad instead of being
# shown on a spacedesk display: there is no window to place, so start the
# server and get out. pythonw leaves nothing on the taskbar.
if ($ServerOnly) {
    if (Ensure-Server) { Write-Host "Server ready at $Url" }
    return
}

# ---- list ------------------------------------------------------------------

if ($List) {
    $pin = Get-Pin
    $i = 0
    Write-Host ""
    foreach ($s in Get-Screens) {
        $i++
        $b = $s.Bounds
        $tag = ""
        if ($s.Primary) { $tag = "  (primary)" }
        if ($pin -and $s.DeviceName -eq $pin.DeviceName) { $tag = $tag + "  <-- pinned" }
        "{0}  {1,-12} {2,5} x {3,-5} at ({4},{5}){6}" -f `
            $i, $s.DeviceName.Replace('\\.\',''), $b.Width, $b.Height, $b.X, $b.Y, $tag
    }
    Write-Host ""
    if ($pin -and -not (Resolve-Screen $pin)) {
        Write-Host ("Pinned display '{0}' ({1}x{2}) is not attached right now." -f `
            $pin.DeviceName, $pin.Width, $pin.Height) -ForegroundColor Yellow
    }
    "Pin one with:  .\kiosk.ps1 -Monitor <number>"
    return
}

# ---- pick ------------------------------------------------------------------

if ($Monitor -gt 0) {
    $screens = @(Get-Screens)
    if ($Monitor -gt $screens.Count) {
        Write-Error "There is no monitor $Monitor. Run .\kiosk.ps1 -List"
        exit 1
    }
    $s = $screens[$Monitor - 1]
    [pscustomobject]@{
        DeviceName = $s.DeviceName
        Width      = $s.Bounds.Width
        Height     = $s.Bounds.Height
    } | ConvertTo-Json | Set-Content $ConfigPath -Encoding utf8
    Write-Host ("Pinned to {0} ({1}x{2})" -f $s.DeviceName.Replace('\\.\',''), $s.Bounds.Width, $s.Bounds.Height)
}

$pin = Get-Pin
if ($null -eq $pin) {
    # Nothing pinned: a virtual display has no EDID, so it is the one with no
    # physical monitor entry. Falling back to any non-primary display.
    $screen = Get-Screens | Where-Object { -not $_.Primary } | Select-Object -First 1
    if (-not $screen) { $screen = [System.Windows.Forms.Screen]::PrimaryScreen }
    Write-Host "Nothing pinned yet - using $($screen.DeviceName). Pin with -Monitor N." -ForegroundColor Yellow
} else {
    $screen = Resolve-Screen $pin
}

# ---- wait for the display --------------------------------------------------

if (-not $screen -and $Wait) {
    Write-Host ("Waiting up to {0}s for {1} ({2}x{3})..." -f $WaitSeconds, $pin.DeviceName, $pin.Width, $pin.Height)
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline -and -not $screen) {
        Start-Sleep -Seconds 5
        $screen = Resolve-Screen $pin
    }
}

if (-not $screen) {
    Write-Error "Pinned display is not attached. Connect the iPad, or re-pin with -Monitor N."
    exit 1
}

# ---- make sure the server is up --------------------------------------------

Ensure-Server

# ---- replace any previous kiosk window -------------------------------------

Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*casegauge-kiosk*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# ---- launch ----------------------------------------------------------------

$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }
if (-not (Test-Path $edge)) {
    Write-Error "Edge not found. Open $Url in any browser instead."
    exit 1
}

$b = $screen.Bounds
$args = @(
    "--app=$Url",
    "--window-position=$($b.X),$($b.Y)",
    "--window-size=$($b.Width),$($b.Height)",
    "--start-fullscreen",
    "--user-data-dir=$ProfileDir",
    "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--disable-sync", "--disable-background-networking",
    "--disable-breakpad", "--disable-component-update", "--disable-domain-reliability",
    "--renderer-process-limit=1", "--process-per-site",
    "--disable-features=msEdgeCopilot,msImplicitSignin,Translate,MediaRouter,OptimizationHints,msWebOOUI,msPdfOOUI",
    "--js-flags=--max-old-space-size=96"
)

Start-Process $edge -ArgumentList $args
Write-Host ("Opened on {0} ({1}x{2} at {3},{4})" -f `
    $screen.DeviceName.Replace('\\.\',''), $b.Width, $b.Height, $b.X, $b.Y)
