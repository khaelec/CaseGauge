# CaseGauge — a live PC monitor for a spare screen

CaseGauge turns a spare display into an at-a-glance PC stats panel with a live
wallpaper — a **side monitor**, or a **tablet or phone you already have**, over
your local WiFi. Open it in any modern browser (Safari on iOS, Chrome on
Android) or add it to the Home Screen. No App Store app, no sideloading, no
cloud, no account.

The wallpaper is a WebGL shader rendered **on the device**, so the PC never
streams video — it sends about 200 bytes of JSON per second instead of
~20 Mbit/s of H.264.

## Project status

CaseGauge is a small, **low-resource** utility and is **not actively developed**.
Expect updates only for **major bugs**, not new features — issues and pull
requests may go unanswered. It is provided as-is; see `LICENSE`.

None of the third-party applications it relies on — **LibreHardwareMonitor,
ffmpeg, Wallpaper Engine, the NVIDIA drivers, or Windows** — are owned, made, or
licensed by me. They are separate products from their respective authors; you
obtain and license them yourself, under their own terms. See
`THIRD-PARTY-NOTICES.md`.

## Requirements

- Python 3 (already installed — no pip packages needed)
- An NVIDIA GPU for the GPU card (`nvidia-smi`, ships with the driver)
- **LibreHardwareMonitor, running as administrator**, for CPU temperature only

## Run it

Double-click **`CaseGauge.exe`**, or `start.bat` (which launches it and exits
immediately - closing that window does not stop anything).

The app has **no console and no taskbar button**. Control it from its tray
icon: double-click to open the dashboard, right-click for Open dashboard /
Copy iPad URL / Wallpaper / Zoom / Quit. `stop.bat` is the guaranteed escape
hatch if the tray ever fails to appear.

It starts with Windows via a Startup shortcut pointing at the exe.

### About the exe

Built with PyInstaller, which is a **build-time** dependency only - the app
itself is still standard library Python, and the exe bundles its own
interpreter, so Python does not have to be installed to run it.

`web/` and `assets/` are bundled **into** the exe, so a released
`CaseGauge.exe` runs on its own. When a local `web/` or `assets/` folder sits
beside the exe it **takes precedence** - which is what makes editing a web file
change the build stamp and reach the iPad without a rebuild. Writable state
(`cache/`, `state.json`, `CaseGauge.log`) is always created next to the exe,
never inside it.

Rebuild after changing `server.py`, `tray.py` or `wallpapers.py`:

```powershell
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name CaseGauge ^
  --icon "%CD%\assets\icon.ico" ^
  --add-data "%CD%\web;web" --add-data "%CD%\assets\icon.ico;assets" ^
  --hidden-import tray --hidden-import wallpapers ^
  --distpath . --workpath build --specpath build server.py
```

Editing anything in `web/` needs **no rebuild and no restart** - just save it
(while a local `web/` folder is present).

`CaseGauge.log` records tray startup and any failure. With `--noconsole` there
is no stderr, so without that file a tray failure would be completely silent,
and the tray is the only way to control the app.

To run from source instead (useful when debugging, since you get output):

```powershell
python -u E:\Ipad\server.py
```

It prints the URL to open on the iPad, something like:

```
Open on the iPad:  http://192.168.2.20:8777/
```

On the iPad: open that URL in Safari, then **Share → Add to Home Screen**. The
saved icon launches full screen with no browser chrome, and the page requests a
screen wake lock so it will not dim.

## CPU temperature

Windows exposes no public API for CPU core temperature, and this board has no
usable ACPI thermal zone (`MSAcpi_ThermalZoneTemperature` returns *Not
supported*). Reading Zen 3 Tctl/Tdie means going through a kernel driver, which
is what every temperature tool does — HWiNFO, HWMonitor and MSI Afterburner all
load one.

```powershell
winget install LibreHardwareMonitor.LibreHardwareMonitor
```

winget installs this one as a **portable** package, so it lands in
`%LOCALAPPDATA%\Microsoft\WinGet\Packages\LibreHardwareMonitor.*\` with no
Start Menu entry — which makes it easy to lose. A shortcut has been added to
the Start Menu; search for *LibreHardwareMonitor*.

It is configured to start minimised to the tray, to hide rather than exit when
its window is closed, and to bring its web server up automatically. So once it
is running you can forget about it — but it does need to be running, and it
does not start itself at logon unless you add the task below.

LibreHardwareMonitor self-elevates on launch, so it already has the access it
needs. The one thing it does *not* do by default is expose its readings:

- **Options → Remote Web Server → Run** (leave the port at 8085)

That setting persists, so it is a one-time step. The config keys behind it are
`runWebServerMenuItem` and `listenerPort` in `LibreHardwareMonitor.config`,
which LHM writes on exit — useful if you would rather script it than click.

Note that 0.9.6 installs **PawnIO** rather than the old WinRing0 driver. PawnIO
is signed and sandboxed, which is a real improvement over the blanket ring0
access these tools used to require.

`server.py` reads `http://127.0.0.1:8085/data.json` and picks out the CPU
package sensor. Until LHM is running, the CPU card shows `--` and every other
reading works normally.

If LHM is not listening, the server stops asking for 15 seconds at a time. That
backoff matters: a refused connection to `localhost` costs a full timeout per
address family, which would otherwise drag the 1 Hz poll loop down to 4 seconds.

## Let the iPad reach it

Windows Firewall blocks the port inbound by default. Once, from an elevated
PowerShell:

```powershell
New-NetFirewallRule -DisplayName "CaseGauge dashboard" -Direction Inbound `
  -LocalPort 8777 -Protocol TCP -Action Allow -Profile Private
```

Keep this on `Private` only. The dashboard has no authentication, so anyone on
the LAN who finds the port can read your machine's stats.

## Fullscreen, zoom and keys

| key | does |
|---|---|
| **F** | fullscreen |
| **W** | wallpaper picker |
| **+** / **−** | zoom in / out |
| **0** | reset zoom to 100% |

The ⛶ and zoom buttons sit in the top right. After five seconds without input
they all fade out, and any touch, mouse move or keypress brings them back — so
a permanent display shows nothing but the stats and the wallpaper.

### Zoom

The iPad's panel is small, so zoom drives a single `--s` multiplier that every
size in `style.css` is derived from. That means the layout genuinely reflows
and the text is re-rasterised at the new size — it is not a magnified bitmap,
and nothing goes blurry. Browser zoom or a CSS transform would both have cost
sharpness.

**The ceiling adapts to the display.** The dashboard never scrolls, so a zoom
that did not fit would quietly clip the bottom card. After each change the code
measures the layout and steps back down until it fits, then shows the figure it
actually applied. On the 768×1024 spacedesk display that lands at about 170%;
rotate to landscape and the same button reaches 200%. Rotating re-fits
automatically.

## Choosing which monitor it opens on

`kiosk.bat` pins the dashboard to one display:

```
kiosk.bat -List        show monitors, and which is pinned
kiosk.bat -Monitor 3   pin monitor 3 and open there
kiosk.bat              open on the pinned monitor
kiosk.bat -Wait        wait for that display to appear first
```

The pin is saved in `kiosk.json` as a **device name plus resolution**, not an
index. Display numbering shifts as monitors come and go, and a spacedesk
display only exists while the iPad is connected — so an index saved today would
point at the wrong screen tomorrow.

`-Wait` exists for exactly that reason: at logon the iPad is usually not
connected yet, so it polls for up to five minutes for the pinned display before
opening. It also starts `server.py` if it is not already running.

### Starting with Windows

A shortcut in your Startup folder (`shell:startup`, *CaseGauge dashboard*)
runs `kiosk.ps1 -ServerOnly` — it starts the server and nothing else, because
the iPad views the dashboard in Safari rather than through a desktop window.
Delete the shortcut to stop it.

Swap the argument to `-Wait` if you ever want the kiosk window back at logon.

It does **not** start LibreHardwareMonitor, which needs elevation — see the
scheduled task above.

## The PC decides what the iPad shows

The wallpaper choice lives on the **server**, in `state.json` — not in each
viewer's browser storage. So it is set from this PC and every viewer follows,
without anyone touching the iPad.

Set it from the **tray icon → Wallpaper**, which lists the built-in shader plus
every usable Wallpaper Engine wallpaper, with a tick beside the current one.
The choice rides along on the 1 Hz `/api/stats` poll the page already makes, so
the iPad switches within about a second. It survives a server restart.

The picker inside the page still works from any device, but it now writes to
the server too, so there is one source of truth rather than a per-device
setting.

**Zoom works the same way** — tray icon → **Zoom**, from 80% to 200%. The
iPad follows within a second, and the setting survives a restart.

One subtlety: the page still clamps zoom to what actually fits, so asking for
200% on the 768×1024 display lands at 170%. The clamped figure is deliberately
*not* written back to the server — if it were, every poll would ratchet the
value down a little further. The server keeps what you asked for, and each
viewer fits it to its own screen. That also means a larger display connected
later gets the full 200% without you having to set it again.

### Layout

Which cards and rows are shown, and in what order, also lives on the **server**
(`layout` in `state.json`) and is pushed on the same 1 Hz poll. Set it two ways:

- **Layout** in the top-right of the page — a panel with the cards and their
  rows; untick to hide, arrows to reorder. Useful when the dashboard is open on
  the PC.
- **tray icon → Layout ▸** — per card: show/hide, each row's tick, and move
  up/down. This is the one that works without touching the iPad, which is
  mounted inside the case.

A card hidden from the list is `display:none` and the grid is rebuilt from the
visible cards as explicit fractions, so the equal-width columns and the
"numbers don't jump" guarantee both survive. At least one card always remains.

### Per-core load

The CPU card shows a small square beside the temperature: one bar per **logical
processor** (16 on the 5700X3D), 2 columns × 8 rows, coloured green → amber →
red by load. It reads `NtQuerySystemInformation`
(`SystemProcessorPerformanceInformation`) at 1 Hz — no admin needed. Turn it
off in **Layout** like any other row.

### Nothing is streamed

A video wallpaper is transcoded once on the PC, cached, and then **downloaded
by the iPad over HTTP** like any other file — served with Range support and
`Cache-Control: public, max-age=86400`, so Safari keeps it for a day rather
than re-fetching it. It plays on the iPad's own hardware decoder.

The PC does no live encoding and holds no stream open. That is the whole reason
this costs ~31 MB where spacedesk cost ~490 MB: after the first download, the
PC is only sending about 200 bytes of JSON per second.

Expect a short "transcoding…" wait the first time a large wallpaper is picked,
and a few seconds of download before it starts. After that it is instant.

## The tray icon

The server runs under `pythonw`, so it has no console and no taskbar button.
That would leave no way to see it running or to stop it, so it puts an icon in
the notification area instead:

- **double-click** — open the dashboard
- **right-click** — Open dashboard / Copy iPad URL / **Wallpaper ▸** / **Zoom ▸** / **Layout ▸** / Quit

The menu's top line shows the LAN URL to type on the iPad, and *Copy iPad URL*
puts it on the clipboard.

It is written with `ctypes` against the Win32 API in `tray.py`, so it stays
stdlib-only — no pystray, no Pillow. It costs about 4 MB: the server is ~27 MB
without it and ~31 MB with it. Set `PCSTATS_NO_TRAY=1` to run without it, and
if the tray cannot start for any reason the server keeps serving regardless.

Windows 11 hides new tray icons in the **^** overflow by default. Drag it onto
the taskbar to keep it visible.

## Not using spacedesk

Viewing the dashboard in Safari on the iPad means spacedesk is not involved at
all. If its tray helper and "free version" nag are a nuisance, stop the service
from auto-starting — from an **elevated** PowerShell:

```powershell
Set-Service spacedeskService -StartupType Manual
```

**This does not touch the driver.** `spacedesk Graphics Adapter` is a PnP
display device and stays installed and working, and the kernel drivers
(`spacedeskDriverBus`, `spacedeskKtmInputMouse`, `spacedeskDriverAndroidControl`)
are already `StartMode: Manual` and load on demand. Only the user-mode service
changes, so spacedesk simply is not listening at boot — no tray helper, no nag.

To use spacedesk again: `Start-Service spacedeskService`, or set it back to
`Automatic`. Nothing is uninstalled and nothing is lost.

### A note on DPI

`kiosk.ps1` deliberately does not make itself DPI-aware. Windows is at 125%
here, so a DPI-aware process sees the spacedesk display as 960×1280 at
(6400,−134) while an unaware one sees 768×1024 at (5120,−107). Chromium places
the window in whichever coordinate space it is handed, so reading and passing
coordinates from the same unaware process keeps them consistent.

That 960×1280 is also the real framebuffer spacedesk is sending, against an
iPad panel of 1536×2048 — so the iPad is upscaling. Raise the resolution in
spacedesk if you want a native, sharper image.

## Where to display it, and what it costs

Measured on this machine:

| approach | RAM on this PC |
|---|---|
| Safari on the iPad, over WiFi | **~30 MB** (just `server.py`) |
| `kiosk.bat` — Edge, fullscreen, lean flags | ~450 MB, 8 processes |
| Edge app mode, default flags | ~560 MB, 10 processes |
| A tab in an already-running Brave | adds to its existing ~3 GB |

Running it in a desktop browser and pushing that window to the iPad through
spacedesk costs roughly **fifteen times** more memory than pointing Safari at
it — and that is before spacedesk re-encodes an animated wallpaper as video,
every frame, for as long as it is on screen.

About 450 MB is the floor for anything Chromium-based; the lean flags in
`kiosk.bat` only save about 100 MB over the defaults, because the cost is
Chromium's process model rather than this page. WebView2 would shave a little
more and still land in the same range.

So `kiosk.bat` is there for when you want the dashboard on a desktop monitor.
For the iPad, Safari is both lighter and sharper — the wallpaper renders on the
iPad's own GPU instead of being streamed to it.

## Start it automatically

Two pieces want to come up at logon: the dashboard server, and
LibreHardwareMonitor.

LHM self-elevates, so launching it from a plain Startup shortcut means a UAC
prompt every boot. A scheduled task running with highest privileges avoids
that. From an **elevated** PowerShell, once:

```powershell
$exe = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\LibreHardwareMonitor.LibreHardwareMonitor_Microsoft.Winget.Source_8wekyb3d8bbwe\LibreHardwareMonitor.exe"
Register-ScheduledTask -TaskName "LibreHardwareMonitor (CaseGauge)" -Force `
  -Action   (New-ScheduledTaskAction -Execute $exe -WorkingDirectory (Split-Path $exe)) `
  -Trigger  (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) `
  -Principal(New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -RunLevel Highest -LogonType Interactive) `
  -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero))
```

Undo it with
`Unregister-ScheduledTask -TaskName "LibreHardwareMonitor (CaseGauge)"`.

The dashboard server needs no elevation of its own, so a Startup shortcut to
`pythonw.exe E:\Ipad\server.py` is enough — `pythonw` keeps the console hidden.

## Wallpaper Engine wallpapers

Tap **Wallpaper** in the top right to choose from the ones you have already
downloaded. Wallpaper Engine itself is never launched — `wallpapers.py` only
reads the files its workshop downloads left on disk, under
`steamapps/workshop/content/431960`. Set `WALLPAPER_DIR` to override the
location.

Only two of the four Wallpaper Engine types can travel to an iPad:

| type | works | why |
|---|---|---|
| **video** | yes | an .mp4, transcoded to an iPad-sized loop |
| **web** | usually | already HTML/JS, served behind an API shim |
| **scene** | no | proprietary `.pkg`; only WE's own renderer reads it |
| **application** | no | it's an .exe |

Your library is **20 usable of 70** — the other 50 are scene wallpapers.

### Transcoding

Video wallpapers are built for desktop monitors and are far too heavy to hand
an iPad 6 directly: the largest here is 1069 MB of 3840×2160 at 29.9 Mbit/s,
against an iPad with 2 GB of RAM on a WiFi 5 radio.

So the first time you pick one, ffmpeg re-encodes it with **NVENC** to 768p at
about 2.7 Mbit/s and caches the result in `cache/`. That 1069 MB file becomes
**96 MB in 24 seconds**, and afterwards loads straight from cache. The
dashboard shows progress on the hint line while it works.

Aspect ratio is preserved and the browser crops with `object-fit: cover`, so a
16:9 loop fills the 4:3 panel and still looks right if the iPad rotates.
`+faststart` is set, so Safari begins playing before the download finishes.

Tuning lives at the top of `wallpapers.py`: `TARGET_HEIGHT` and `MAX_BITRATE`.
Delete anything in `cache/` to force a re-encode.

### Web wallpapers

These call an API that only exists inside Wallpaper Engine, so the server
splices a shim into the entry page as it is served. The shim stubs out
`wallpaperRegisterAudioListener` and friends, and hands the wallpaper its
default user properties from `project.json` on load. Most will run; some
assume a desktop GPU and will be slow on an A10.

The iframe uses `sandbox="allow-scripts allow-same-origin"`. The browser warns
that this combination is weak sandboxing, which is true — it is kept because
wallpapers that use `localStorage` or load their own textures break without it.
These are files you downloaded, served from your own machine, so that trade is
reasonable; drop `allow-same-origin` if you would rather be strict.

## Files

| file | what it does |
|---|---|
| `server.py` | polls sensors at 1 Hz, serves `web/`, `/api/stats` and wallpaper media |
| `wallpapers.py` | reads the Wallpaper Engine library, transcodes video loops |
| `tray.py` | the tray icon and its menu, hand-rolled Win32 via `ctypes` |
| `web/index.html` | dashboard markup and the Add-to-Home-Screen metadata |
| `web/style.css` | layout and the temperature colour thresholds |
| `web/app.js` | polling, painting, clock, wake lock |
| `web/wallpaper.js` | the WebGL nebula — replace `FRAGMENT_SRC` to change it |
| `assets/` | the app icon (`icon.ico`) and the Home Screen artwork |
| `LICENSE` | Apache-2.0 |
| `THIRD-PARTY-NOTICES.md` | what is used, bundled, or merely talked to, and under what terms |
| `REQUIREMENTS.md` | the v1.0 public-release checklist |

## Tuning

**Colour thresholds** — `CPU_HEAT` and `GPU_HEAT` at the top of `web/app.js`,
as `[warm, hot]` in °C. CPU defaults to `[65, 82]` because the 5700X3D runs
warm by design and only throttles in the 80s.

**Steady numbers** — every digit that updates is rendered in its own
fixed-width box (`.dig` / `.pt` in `style.css`, built by `setNum` in `app.js`).

`font-variant-numeric: tabular-nums` is not enough. The rounded system font
has no tabular figures, so the property is silently a no-op: measured at
140px, "44" is 161.2px against "45" at 158.2px — *identical* with and without
`font-feature-settings: "tnum"`. Every tick nudged whatever sat beside it.

Two separate causes, both fixed:

- **which** digits are shown — solved by the fixed boxes
- **how many** digits are shown (`7%` → `26%`, memory crossing 10 GB) — solved
  by reserving the widest form with `min-width` and letting the value grow
  leftwards into it

This applies to the clock, the load and VRAM readouts and the chips, not just
the three headline figures. Getting only the headline numbers was the first
attempt and it was not enough — the measurement that missed it watched the
`.chips` container, which is full width and never moves, rather than the chips
inside it.

**Readability over the wallpaper** — the stat cards have no panel, border or
blur, so the wallpaper is fully visible behind them. Legibility comes from two
places instead: the `--lift` and `--lift-big` text shadows in `web/style.css`,
and the full-screen `.vignette`, which darkens further when a video or web
wallpaper is active.

Small text is the first thing to go over a busy wallpaper, which is why the
chips keep a dark pill and the meter troughs keep a dark hairline. If you land
on a very pale wallpaper and the numbers start to struggle, deepen the
`body[data-wp="video"] .vignette` gradient rather than putting the panels back.

**Wallpaper cost** — `MAX_EDGE` and `TARGET_FPS` at the top of
`web/wallpaper.js`. The iPad 6 is 2018 silicon driving a 2048×1536 panel, so
the shader renders at 640 px on the long edge at 30 fps and CSS scales it up; a
soft nebula hides the upscale and the iPad stays cool. Raise `MAX_EDGE` for a
crisper wallpaper at the cost of heat and battery.

**A different wallpaper** — replace `FRAGMENT_SRC` in `web/wallpaper.js`. It
receives `u_res` (resolution) and `u_time` (seconds), which is the same contract
as a Shadertoy fragment shader.

## The port

The server listens on **8777** by default. One server serves **every** tablet at
once — a second device does not need a second port, it just opens the same URL.

Change it only to avoid a clash, or to run two instances. In priority order:

1. **`casegauge.json`** beside the exe (easiest when you just double-click it):

   ```json
   { "port": 9000 }
   ```

2. **`PCSTATS_PORT`** environment variable.
3. **`CaseGauge.exe --port 9000`**.

Precedence is `--port` → `PCSTATS_PORT` → `casegauge.json` → 8777. Whatever you
pick, add a matching firewall rule, and use the new URL; `kiosk.ps1` follows the
setting automatically. Two instances must run from **separate folders**, because
`state.json` lives beside the exe.

The server refuses to start if the port is already taken, rather than binding
alongside it. On Windows `SO_REUSEADDR` lets two processes share a listening
port, and the dashboard would then see alternating stale readings from whichever
instance won each connection.

## Using an Android tablet

The dashboard is plain web, so an Android tablet works as a second viewer with
no extra build — open the same URL in Chrome. Two things differ from the iPad:

- **The screen will sleep.** The Wake Lock API requires a secure context
  (HTTPS), so Chrome refuses it over `http://192.168.x.x`. Android expects a
  kiosk browser for a permanently-on panel: **Fully Kiosk Browser** (or
  WallPanel) has a "keep screen on" option and can start on boot. Android's
  developer option *Stay awake while charging* also works. The iPad sidesteps
  this by being added to the Home Screen.
- **Fonts and shape differ.** Android falls back from the rounded system font to
  Roboto, and its panels are typically 16:10 rather than 4:3, so the layout is
  close but not pixel-identical.

Android's Home Screen install would want a `manifest.json`, which is not shipped
(the page uses the iOS `apple-touch-icon`). The dashboard still runs fine
without one.

## Credits and legal

CaseGauge is licensed under the **Apache License 2.0** (see `LICENSE`). It is
free software. Third-party components, and the terms of everything it talks to,
are detailed in `THIRD-PARTY-NOTICES.md`.

### Wallpaper Engine

CaseGauge can display wallpapers you already own from **Wallpaper Engine**, a
Steam application by the Wallpaper Engine Team (© Skutta Software GmbH, Steam
app ID 431960).

- Wallpaper Engine is **not** bundled with or included in CaseGauge.
- To use the wallpaper feature you must **purchase and install Wallpaper Engine
  yourself, through Steam**, and download wallpapers through Steam Workshop.
- CaseGauge reads files from **your own local library** and redistributes
  nothing.
- CaseGauge is **not affiliated with, sponsored by, or endorsed by** Skutta
  Software GmbH, Valve Corporation, or Steam. All names and trademarks belong to
  their respective owners and are used only to describe interoperability.

Only portable wallpaper types (video, web) can be used. Scene and application
wallpapers are proprietary and are rendered only by Wallpaper Engine itself.

### Other dependencies

**None of these are owned, made, or licensed by me.** They are separate
products from their respective authors, and you obtain them yourself under
their own terms.

- **LibreHardwareMonitor** (MPL-2.0) — CPU temperature only. Install it
  yourself; it is not bundled.
- **ffmpeg** (LGPL-2.1+, or GPL for `--enable-gpl` builds) — video transcoding.
  Install it yourself (e.g. `winget install Gyan.FFmpeg`); it is not bundled.
- **NVIDIA drivers / `nvidia-smi`** — GPU readings. Not bundled.
- **Python** (PSF License) and **PyInstaller** (GPL-2.0 with Bootloader
  Exception) — the interpreter and bundler inside the release exe.

### Disclaimer

CaseGauge is provided **"AS IS", without warranty of any kind**. Hardware sensor
readings depend on third-party tools and drivers. Use it at your own risk. It
has **no authentication and no TLS** — keep it on a trusted, private LAN.

### Support the project

CaseGauge is free, and always will be. If it is useful to you, you can buy me a
coffee — it is a voluntary tip, not a purchase, and it unlocks nothing:

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-khaelec-FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/khaelec)

## Known limits

- No authentication or TLS. LAN only, `Private` firewall profile only.
- GPU card requires NVIDIA. An AMD or Intel GPU reports nothing, since
  `nvidia-smi` is the only source wired up.
- Stats poll at 1 Hz over plain HTTP. Fine for a dashboard; if you ever want
  sub-second updates, move `/api/stats` to Server-Sent Events.
