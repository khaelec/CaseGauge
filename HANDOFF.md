# Handoff

*Written for: an agent picking this project up cold.*

Read `README.md` for how the thing works and how to run it. This file covers
what is **unresolved**, what has already been **ruled out**, and the
environment facts that are expensive to rediscover.

---

## Project identity

The app is called **CaseGauge** (renamed from "PC Monitor" on 2026-09-23). The
exe is `CaseGauge.exe`, the log `CaseGauge.log`, the tray window class
`CaseGaugeTray`. Licensing artifacts now exist: `LICENSE` (Apache-2.0),
`THIRD-PARTY-NOTICES.md`, `REQUIREMENTS.md` (the public-release checklist), and
`assets/` (multi-size icon + Home Screen artwork). The tray loads
`assets/icon.ico` via `LoadImageW`; the old `shell32.dll` icon extraction is
gone. `assets/icon.ico` resolves relative to the exe, so `assets/` must ship
beside it like `web/`.

**Port.** `_load_port()` resolves `--port` → `PCSTATS_PORT` → `casegauge.json`
`{"port": N}` → 8777. `casegauge.json` is read with `utf-8-sig`, because a
hand-edited file (Notepad/PowerShell) usually carries a BOM that plain
`json.load` rejects. `kiosk.ps1` resolves the port the same way. One server
serves every client, so multiple devices do **not** need multiple ports.

**GPU sources.** `poll_once()` reads **both** sources every poll and
`merge_gpus()` folds them into one list: `read_gpus()` returns every line
`nvidia-smi` prints (NVIDIA, no admin), `_lhm_gpus_from_tree()` returns every
GPU device in LHM's tree (the only source for AMD and Intel, and for the memory
junction on any card). Lookups are keyed on **(group, label)** because "GPU
Core" appears under both Temperatures and Load. AMD and Intel are supported this
way but are **untested** - the author has no such hardware. The first poll of
each GPU writes its LHM sensor labels to `CaseGauge.log`, so a mismatch is
reportable. Do not rewrite it as "works on AMD/Intel"; it is "should work,
unproven".

The merge matches a card by name (`_gpu_key()`), and each LHM reading claims **at
most one** nvidia-smi entry - that is what keeps two identical cards from
collapsing into one while still merging each of them. Validated on two RTX 5070
Ti: two cards, distinct readings, no duplicate. The snapshot carries `gpus`
(the list) and `gpu` (the leading card, kept only so a page cached from an older
build still paints something).

**Client platforms.** The server is always the PC; there is no Android build.
The page is deliberately platform-agnostic: `apple-touch-icon` for iOS, a
manifest would be added for Android, the wake lock is **feature-detected**, and
fonts fall back in CSS. Do not add UA sniffing to branch behaviour. Note the
Wake Lock API needs a secure context (HTTPS), so on Android over plain LAN HTTP
the screen will sleep and users need a kiosk browser (documented in the README).
That also implies the iPad's wake lock may not actually be engaging over HTTP —
check whether Auto-Lock is simply off.

## What this is

A PC stats dashboard (CPU/GPU/RAM) with a live wallpaper, served from a
Windows PC and viewed in **Safari on an iPad 6**, added to the Home Screen.

It replaced spacedesk, which is now **uninstalled** â€” do not reintroduce a
dependency on it. The iPad is a browser client on the LAN, not a display.

Everything is Python **standard library only**. No pip packages. Keep it that
way unless the user asks otherwise; it is the reason the server is ~31 MB.

---

## Environment (verified, not assumed)

| | |
|---|---|
| Project | `E:\Ipad` |
| Python | 3.12.10 at `%LOCALAPPDATA%\Programs\Python\Python312` |
| CPU / GPU / RAM | Ryzen 7 5700X3D Â· RTX 5070 Ti Â· 32 GB |
| Displays | DISPLAY1 5120Ã—1440 primary, DISPLAY2 1350Ã—2400 portrait. **Windows is at 125% scale** |
| iPad | 6th gen, Lightning, iPadOS 17.7.x (caps there), panel 2048Ã—1536, `devicePixelRatio` 2 â†’ **CSS viewport 1024Ã—768 / 768Ã—1024** |
| LAN | PC wired, `192.168.2.20`. Dashboard at `http://192.168.2.20:8777/` |
| Firewall | **No rule needed.** Windows already has inbound Allow rules for `python.exe`/`pythonw.exe` on the Private profile. Verified `Inbound/Allow/Enabled/Private`. |

**LibreHardwareMonitor** â€” CPU temperature only. Installed by winget as a
*portable* package (no Start Menu entry by default; a shortcut was added):

```
%LOCALAPPDATA%\Microsoft\WinGet\Packages\LibreHardwareMonitor.LibreHardwareMonitor_Microsoft.Winget.Source_8wekyb3d8bbwe\
```

It self-elevates, writes its config on exit, and is already configured to start
minimised with its web server on `127.0.0.1:8085`. It does **not** auto-start at
logon â€” the scheduled-task command is in `README.md` and the user has to run it
elevated themselves (agents are blocked from registering it).

**ffmpeg** â€” winget `Gyan.FFmpeg`, found via `wallpapers.find_ffmpeg()` because
the winget shim is not on PATH until a new shell. NVENC confirmed working.

**Wallpaper Engine library** â€” `D:\SteamLibrary\steamapps\workshop\content\431960`,
70 items: 16 video, 4 web, 49 scene, 1 unknown. Scene and application types are
**not usable** â€” proprietary format, only WE's own renderer reads them.

---

## Open issues

### 1. Layout jitter â€” SOLVED, confirmed on the device

The long-running "numbers make everything jump" bug. It had nothing to do with
the numbers.

`web/app.js` contains a **layout-shift watchdog** that samples key rows every
animation frame and POSTs anything that moves to `/api/diag`, which the server
appends to `diag.log`. That exists because the display is an iPad mounted
inside the PC case: it cannot be reached, cannot be filmed, and the shift never
reproduced in headless Chromium. The page had to measure itself.

It caught the cause in a single correlated frame:

```
cards     826.5 -> 826.5    +0.0     <- container fixed
cpuCard   195.2 -> 220.1   +24.9
gpuCard   289.1 -> 314.0   +24.9
ramCard   314.6 -> 264.8   -49.8
...every child:            +0.0
```

The three cards were **trading height with each other** inside a fixed
container while nothing inside them changed size. `.cards` used
`grid-auto-rows: auto`, so rows sized themselves from content and shared the
leftover space; any nudge re-ran that distribution across all three.

Fixed with explicit `grid-template-rows` fractions (portrait:
`0.78fr 1.15fr 1.07fr` - the GPU card carries more content so it gets more
room). A row's height can no longer depend on anything inside it or inside its
siblings. `diag.log` has been empty since.

**Keep the watchdog.** It costs four rects per frame, reports at most once per
20s and only when something moved, and it is the only diagnostic channel that
exists for this display. If layout regresses, it will say so without anyone
having to look at the iPad. It now **re-baselines on a viewport change**, so a
rotation - or a headless test resizing the window - no longer posts a
false-positive full-screen report that looks like the bug returning.

Dead ends, for the record - none of these were the cause:
`font-variant-numeric: tabular-nums` (a verified **no-op** in this font stack;
`"44"` is 161.2px and `"45"` 158.2px, identical with and without
`font-feature-settings: "tnum"`), `-webkit-text-size-adjust`, caching, and
zoom. Fixed-width digit boxes and in-place text-node updates went in along the
way and are worth keeping, but they were not the bug.

### 1c. Built: per-core square and user-editable layout

Both landed. Summary of what exists, and the traps worth keeping.

**Per-core load.** `server.py:read_cpu_cores()` calls `NtQuerySystemInformation`
with `SystemProcessorPerformanceInformation` (class 8) - `GetSystemTimes` is
totals only - and returns one busy percent per **logical processor** (16 here).
Same delta arithmetic as `read_cpu_load()`. No admin. It rides on
`/api/stats` as `cpu.cores`. Note `ntdll.NtQuerySystemInformation` **must** have
explicit `argtypes`: without them ctypes passes the buffer as a C int, truncates
the pointer on 64-bit and faults inside the kernel.

Drawn as a **square** the same height as the headline number, sitting to the
**right** of the CPU temperature (`web/index.html`, `.cores` in `style.css`,
`setCores` in `app.js`). It is 2 columns x 8 rows of fixed tracks; only the
fill width changes, so it cannot resize the card. Two reasons it is on the
right, not the left gutter originally sketched: on the left it pushed the CPU
number off the shared left edge, so the three headline numbers no longer
matched. `.cores:empty` hides it when there is no data, and the layout owns
`hidden` - `setCores` never touches it.

**Layout.** A `layout` key in `state.json` - an ordered list of
`{id, rows[]}`. Cards absent from the list are hidden; list order is screen
order. `get_ui()` rides it on `/api/stats`; the page adopts it in `paint()` and
writes edits to `POST /api/layout/select` (JSON body, because it is nested).
The tray has **Layout â–¸** (per card: show, row ticks, move up/down), and the
page has a **Layout** overlay for when the dashboard is open on the PC.

**The trap, and why it is minmax(0, fr).** `web/style.css` `.cards` and
`fitGrid()` in `app.js` use `minmax(0, 1fr)` / `minmax(0, Xfr)` everywhere -
**never a bare `fr`**. A bare `fr` is `minmax(auto, fr)`, and the CPU card's
min-content width (number + per-core square) is wider than an equal share, so
with `repeat(3, 1fr)` the landscape columns came out 425/305/237 instead of
equal - the exact "mismatched cards" symptom. `minmax(0, ...)` pins the minimum
and restores equal columns. The grid template is regenerated in JS from the
**visible** cards (`fitGrid()`), never left to the media query alone, because a
hidden card is `display:none` and not a grid item at all. It is still always
explicit fractions - never `auto` - so the layout-jitter fix (issue 1) holds.
`diag.log` stayed empty through the change.

`web/` needs no rebuild; `server.py`/`tray.py` do. Both were rebuilt and the
exe restarted on 2026-09-23.

Still open from the original sketch: the tray menu's **text-invisible** bug
(issue 2) also affects the new Layout submenu, which is why the on-page overlay
exists as the reliable control surface.


### 1d. Old issue 1 (historical)

The user reported repeatedly that the layout shifts when values update. Three
rounds of fixes went in. **The user's last "it's still happening" arrived before
the third fix landed, so the current build has not been confirmed on device.**

**First thing to do: ask for the build stamp.** It is shown in the wallpaper
picker next to the wallpaper count, and in `<span id="build">`. If it does not
match `build_stamp()` on the server, they are on stale code and nothing else
matters.

What went in, in order:

1. **Fixed-width digit boxes** (`setNum` in `app.js`, `.dig`/`.pt` in
   `style.css`) â€” each digit gets its own box so which digits are shown cannot
   change an element's width.
2. **`-webkit-text-size-adjust: 100%`** â€” iOS Safari inflates text on pages it
   judges non-responsive and re-runs that heuristic as content changes. This is
   Safari-only and *cannot be reproduced in headless Chromium*, which is why it
   survived several rounds of clean measurements. This is the most likely true
   cause; the user's wording was "font sizes causing things to move".
3. **Structural rigidity** â€” the real miss. `.hint` shares the flex column with
   `.cards`, so a wallpaper-progress message appearing resized every card.
   `.chips` could wrap to a second line. `.readout` used auto margins, which
   are computed from sibling heights, so anything changing below moved the big
   number above. All now fixed-size or `flex: 0 0 auto`.

**Do not** try to fix this with `font-variant-numeric: tabular-nums`. It is
already set and it is a **verified no-op** in this font stack: measured at
140px, `"44"` is 161.2px and `"45"` is 158.2px, *identical* with and without
`font-feature-settings: "tnum" 1`. The font has no tabular figures.

Also ruled out: caching (now impossible, see below), and zoom (the user
suspected it; it is not the cause).

If it persists on a confirmed-current build, next suspects:
- `vw`-based `clamp()` font sizes reacting to something viewport-related in
  standalone mode
- the `applyZoom` fit-loop running more than once (it mutates `--s` in a
  `while` loop and each iteration reflows)
- font swap after load (`ui-rounded` resolving late)

Ask the user **which** element moves â€” clock, a percentage, the big number, or
the whole block. That narrows it enormously and has not been established.

### 1b. PC-side zoom not reaching the iPad â€” unresolved

The user reports that changing zoom from the tray, or from the dashboard open
on the PC, has no effect on the iPad.

Verified working on the current build in a real browser, both directions:
server -> page adoption (`/api/stats` carries `zoom`, `paint()` applies it) and
page -> server writes (`/api/zoom/select`). So the mechanism is sound and the
prime suspect is the same stale-cache problem as issue 1.

A **real race was found and fixed** while testing this: clicking `+` three
times left the page showing 140% while the server held 2.0. The 1 Hz poll can
return the previous server value after a click has already moved on, which
dragged `zoomWanted` backwards and made the next click compute from a stale
base. `paint()` now ignores the server's zoom for `ZOOM_SETTLE_MS` (2.5s) after
a local write. Retested: 3 clicks -> page 130%, server 1.3.

Note `applyZoom` has a fit-clamp loop that reduces zoom until the layout fits.
The clamped value is deliberately **not** written back to the server. If the
iPad ever appears to ignore zoom *downward*, check whether `overflows()` is
always true there (safe-area insets could do it) - that would clamp every
request back down and look like "zoom does nothing".

### 2. Tray icon: menu text invisible, and sometimes no icon at all

Two related faults in `tray.py`, which is hand-rolled Win32 via `ctypes`.

**Menu text invisible.** The user right-clicked the tray icon and got a menu
with no visible text. The menu *data* is correct â€” verified by building the
same menu and reading it back with `GetMenuStringW`:

```
[0] 'http://192.168.2.20:8777/'   [2] 'Open dashboard'
[3] 'Copy iPad URL'               [4] 'Wallpaper'       [5] 'Quit'
```

So this is a **rendering** problem, not a data one. Prime suspect: the owner
window is created with style `0` (not `WS_POPUP`, not visible) at 0Ã—0, and
Windows may refuse to give it foreground, which breaks menu painting.
`SetForegroundWindow` is already called before `TrackPopupMenu`. Try giving the
window `WS_POPUP`, or use a real (offscreen) window.

**Icon sometimes absent.** Under `pythonw`, `FindWindowW("CaseGaugeTray")`
sometimes returns nothing while the server still serves â€” meaning `tray.run()`
raised and `server.py` fell through to `serve_forever()` without a tray. The
fallback is silent under `pythonw` (no stderr). Make the failure loud: write it
to a log file rather than `print`.

A genuine bug **was** found and fixed here: `DefWindowProcW` had no `argtypes`,
so ctypes defaulted to C `int` and every pointer-sized `wparam`/`lparam` raised
`OverflowError: int too long to convert` from inside the window procedure. That
flooded the console under `start.bat` and was invisible under `pythonw`. All the
Win32 functions now have explicit `argtypes`/`restype` â€” **keep it that way**;
it is the single easiest way to reintroduce this class of bug.

### 3. Waking from sleep â€” FIXED, needs confirming on device

The user reported "the app doesn't come back when I wake from sleep". A real
bug was found and fixed, in two parts.

**WebGL context loss.** iOS discards the WebGL context while the device sleeps.
There was no `webglcontextlost` / `webglcontextrestored` handling at all, so the
render loop kept calling `drawArrays` into a dead context forever and the
wallpaper stayed frozen on its last frame.

**The subtler half**, and the reason the first attempt looked like it worked:
`resize()` only did its setup when the canvas *dimensions changed*. After a
context restore the canvas keeps its old width and height, so `resize()` did
nothing, and `gl.viewport()` and the `u_res` uniform were never set on the new
context. `u_res` stayed (0,0), the shader divided by zero, and the result was a
renderer reporting "running" while displaying a static frame. The fix tracks
`curW`/`curH` in module variables and resets them to -1 on context loss, rather
than reading back from the canvas.

Also added: `pageshow` / `focus` / `online` handlers (Safari can restore from
bfcache without firing `visibilitychange`), video `error` recovery, and a
`location.reload()` when the page has been visible and online with no
successful poll for two minutes. A dashboard has no state worth preserving, so
a clean reload beats trying to repair every subsystem.

`canvas.dataset.gl` now carries the renderer state (`running` / `lost` /
`restored` / `stalled` / `init-failed`). That is deliberate: page globals are
unreachable from patchright's isolated world, so DOM is the only channel for
observing internals â€” and it is useful when debugging on the iPad too.

### 4. Orphan process

The user's concern, and legitimate: under `pythonw` with no tray there is no
window, no console and no taskbar button, so no way to stop the server.
`stop.bat` now exists and is tested. If the tray is reworked, keep `stop.bat`
as the guaranteed escape hatch.

---

## v1.2: the local wallpaper folder, stills, and honest listings

Three things, all in `wallpapers.py`, `server.py` and the picker.

**`wallpapers\` beside the exe is a second library.** No Steam, no Wallpaper
Engine; copy the folder and the wallpapers travel with it. `local_dir()`
resolves it, `CASEGAUGE_WALLPAPERS` overrides it, and `server.main()` creates
it empty on first run so there is somewhere obvious to drop files. Ids are
`local-<sha1[:8]>-<slug>` from the path relative to the root — they end up in
`state.json`, so they must survive a restart, and only renaming the file
changes one.

**A folder is a drawer, not a wallpaper.** `_walk_local()` descends four deep
and lists every picture and video it finds separately, because someone who
drops twenty loops in a folder means twenty wallpapers. The two exceptions
are folders that *are* one wallpaper: a `project.json` (a workshop copy) or an
`index.html` (a web wallpaper). Titles are prefixed with the containing
folder, so two files called `loop.mp4` stay tellable apart.

**Stills are a new type.** `image` alongside `video`/`web`, rendered by a new
`#wallpaper-image` layer. No transcode, no ffmpeg, no prepare/poll round trip
— the picture is already something Safari can read. Animated GIFs animate.
This is the only wallpaper type that works with nothing else installed.

**Unusable wallpapers are listed, not hidden.** Every item carries a `reason`
when `supported` is false, and the picker draws it greyed out and captioned.
The old behaviour — filter to `supported` — is what sent the user hunting:
a scene wallpaper they had just downloaded looked identical to one the scan
had never seen. Diagnosing "it is not in the list" is much harder than reading
"Scene wallpaper - only Wallpaper Engine can render these" on the tile itself.

Two bugs fell out of building it, both in code that predates it:

- **The picker grid collapsed once it scrolled.** `.tile` sizes itself with
  `aspect-ratio: 16/10`, but grid rows never derived height from it — the
  `1fr` columns are not definite when rows are sized, so rows fell back to the
  tile's *text* height, about 20px. With 21 tiles the leftover space stretched
  them and it looked fine; at 81 tiles there was none and every tile overlapped
  the next. Fixed with `grid-auto-rows: max-content` (`max-content` does
  consult the aspect ratio) plus `align-content: start`. Showing every
  wallpaper is what first made the list long enough to expose it.
- **The transcode shortcut mislabelled containers.** Anything at or under
  `TARGET_HEIGHT` was `shutil.copyfile`d to `<id>.mp4`. Fine while every source
  was a workshop .mp4; a local `.webm` became an mp4-named webm that Safari
  refuses. Now the copy needs a `.mp4` source, and anything else is encoded.
  The same fix stopped small sources being *upscaled* to 768p: the scale filter
  is dropped entirely when the source is already under the target.

---

## v1.3: making the picker usable

v1.2 listed everything it found, which was honest and unreadable: 81 tiles, 54
of them greyed-out scene wallpapers repeating the same caption, and every local
video a blank gradient because none had artwork beside it. The verdict was
"it's just messy and no previews". Three fixes.

**Thumbnails are generated.** `ensure_poster()` pulls one frame out of a video
that shipped without artwork, scales it to 360p and caches it in `cache/` as
`<id>.poster.jpg`. `-ss 1` before `-i` seeks without decoding what it skips, so
a 4K source costs ~0.25s; a second in also skips the fade-from-black a lot of
loops open on, and there is a fallback to frame zero for clips shorter than the
seek. It is done inline in the preview request under a lock, because opening
the picker asks for every missing thumbnail at once and a dozen parallel 4K
decodes would bury the 1 Hz stats loop. The picker now requests a preview for
any video, not just one with a `preview` field, and a tile whose image 404s
falls back to the lettered gradient instead of a broken-image icon.

**Unusable wallpapers are hidden by default.** The v1.2 decision to show them
was right in principle and wrong in practice. They are one unticked box away,
the count line always reports how many are hidden, and the setting lives in
that device's `localStorage` — it changes what you look at, not what the PC
shows, so it does not belong in `state.json` with the wallpaper choice.

**The grid is sectioned.** Items carry `group` (the folder they came from, or
"Wallpaper Engine") and `label` (the file's own name). Headings are full-width
grid items (`grid-column: 1 / -1`). The label matters: a tile is 176px wide, so
the old path-prefixed `title` pushed the actual name out of sight. `title` is
still the full name — it is what `state.json` stores and what the tray shows —
and it survives as the tile's tooltip.

**Several source folders.** `set_sources()` / `source_roots()` in
`wallpapers.py`, a list in `state.json`, and add/remove endpoints. Kept as
module state rather than a `scan()` argument so the tray and the request
handlers can still call `scan()` and `by_id()` with no arguments. Roots that
are duplicates or nested inside one another are dropped, or a file would be
listed twice under two ids. `_local_id()` now digests the **full** path, so the
same filename under two source folders gets two ids.

A folder dialog from the tray would be nicer than typing a path, but the tray
is raw ctypes Win32 with no dialog helper, so the picker takes a typed path
instead. That also means it works from the iPad.

### A trap this turn set

While testing, `sources` kept vanishing from `state.json`. It was not a
persistence bug: the **running `CaseGauge.exe` shares `E:\Ipad\state.json`
with any `server.py` started for testing**, and a v1.2 exe has no `sources` key,
so every save from it wiped the key a v1.3 server had just written. Two servers,
one state file. `PCSTATS_CACHE` redirects the cache but there is no override for
`STATE_PATH` — it is always `_HERE/state.json`. To test state honestly, copy
`server.py`, `tray.py`, `wallpapers.py`, `web/` and `assets/` to a scratch
directory and run from there with `CASEGAUGE_WALLPAPERS` pointed at the real
folder.

---

## v1.4: a card per GPU

Two GPUs in the machine, one on the dashboard. Both sources were written to
return the single best card - `read_gpu()` parsed `splitlines()[0]` and threw
the rest away, `_lhm_gpu_from_tree()` kept whichever device had the most VRAM -
so the second card was not lost, it was never read.

Both now return lists, and `merge_gpus()` folds them together. The interesting
case is two **identical** cards: matching LHM readings to nvidia-smi entries by
name alone would put both onto the first entry and leave the second untouched,
so each LHM reading claims at most one unclaimed entry. Ordering is by VRAM,
descending and stable, which keeps a discrete card ahead of an onboard one and
leaves two identical cards in bus order.

**The extra cards are opt-in.** This was the user's call and it is the right
one: a second GPU is most often integrated graphics nobody wants a card for.
`sync_gpu_cards()` therefore only makes the card *available* - it raises
`_gpu_cards`, which is what `card_ids()` and so the Layout menus are built from
- and never inserts it into the layout. `default_layout()` stops at the first
GPU, so **Reset** does not drag the extras back in.

It does the reverse, though: a GPU that goes away has its card dropped from the
layout, after `GPU_DROP_GRACE` (90s). Without the delay, LHM starting a minute
after CaseGauge - the normal case for an AMD or Intel GPU - would offer a card
and then remove it moments later. `load_state()` also raises `_gpu_cards` to
cover whatever GPU ids the saved layout mentions, or `_clean_layout()` would
drop them as unknown before the first reading has come in.

Ids are `gpu`, `gpu2`, `gpu3`: the first keeps the bare id, so a `state.json`
written by v1.3 still means what it meant. On the page, card 1 keeps the element
ids it always had and the others are clones with every inner id suffixed
(`gpu-temp-gpu2`), so `gel(cardId, base)` finds either.

**Two labels, on purpose.** The Layout editor and the tray say "GPU 1" / "GPU 2"
as soon as two GPUs exist, because you have to tell apart what you are ticking.
The heading on the card only numbers itself once two cards are actually on
screen - with one card there is nothing to distinguish it from, and "GPU 1"
beside no GPU 2 just raises a question.

**A bug this uncovered.** `server.main()` passes `layout_schema` (a function) to
`tray.run()`, which did `for card in layout_schema:`. That raises `TypeError`,
and the whole Layout submenu is built inside one `try/except Exception`, so the
tray has silently had **no Layout menu at all** since the feature was added.
Now called through `_schema()`, which also re-reads it on every open - necessary
here, since the card list changes when a GPU appears.

Verified on the real hardware: two RTX 5070 Ti, distinct temperatures (33 vs
37 C) and loads, no duplicate element ids, four equal columns in landscape and
four weighted rows in portrait with no overflow.

### The old-tablet bug found in the same turn

A second viewer arrived: an Android 5.0.2 tablet on **Chrome 85**. The dashboard
rendered; "wallpapers do not work". The cause was not the wallpapers.

`inset: 0` is **Chrome 87**. Chrome 85 drops it, and a `position: fixed` box with
auto offsets falls back to its *static* position. `#wallpaper` survived because
it also carries `width/height: 100%` - but `.picker`, which has neither, landed
at **y = 720 in a 720px viewport** with `scrollHeight` equal to `innerHeight`.
The wallpaper picker, and the Layout panel that shares the class, were one full
screen below the fold and unreachable by scrolling. `aspect-ratio` is **Chrome
88**, so the picker tiles and the per-core square, which get their whole height
from it, collapsed as well.

Both now have fallbacks: longhand offsets everywhere, and the heights under
`@supports not (aspect-ratio: 1 / 1)`.

**Testing this honestly is the interesting part.** Stripping `inset` from a copy
of `style.css` and serving it is a faithful simulation - an unknown property is
exactly what Chrome 85 sees - and that is how the picker's position was
measured. It does **not** work for the `@supports` fallback: this Chromium
supports `aspect-ratio`, so the guard is false however the declarations are
stripped. Verify that block by swapping the guard for one that is true here
(`@supports (display: grid)`) and measuring what the declarations produce: 95x95
cores, 176x94 tiles. Then check the real file still gives 176x110.

Not fixed, and not worth fixing blind: whether WebGL, H.264 High profile or an
iframe web wallpaper actually run on that SoC. `initGL()` now says "no WebGL on
this browser" on the hint line instead of silently painting a gradient, which
turns the remaining unknown into something the user can read off the screen.

---

## v1.5: fans, board temperatures, drives and the network

Asked for fan speed; the honest answer was that the app read exactly one fan -
the GPU's - and nothing else the board exposes. What the machine actually offers
was found by dumping LHM's whole tree rather than guessing, and that is the
first thing to do again before adding a sensor.

**The tree is not a fixed depth.** A GPU sits at Computer / GPU / Temperatures,
but the super-IO chip that owns the fan headers is one deeper: Computer /
Motherboard / Nuvoton NCT6687D / Fans. The old code walked exactly two levels,
which is why no board sensor had ever been reachable. `_lhm_devices()` now
recurses and yields any node whose children are **sensor groups**, where the
group names are a closed set (`_LHM_GROUPS`) - and that closed set is what keeps
the motherboard node itself from being mistaken for a device. One walk per poll,
shared by every reader below it.

**Drive letters to physical disks.** LHM names drives by model, Windows by
letter, so a temperature could not be attached to a drive chip without asking
the volume what it sits on. `IOCTL_STORAGE_QUERY_PROPERTY` answers that with no
admin and `dwDesiredAccess` 0. Two traps, both already paid for:

- `ctypes.WinDLL` defaults `CreateFileW`'s restype to a 32-bit int, which
  **truncates the HANDLE** and turns every call into ERROR_INVALID_NAME (123) on
  a path that is perfectly valid. The prototypes are declared explicitly now.
- In STORAGE_DEVICE_DESCRIPTOR the id offsets are at **12 and 16**, not 8 and
  12. Getting that wrong returns an empty string rather than an error, which
  reads like "this drive does not report a model".

Verified: C: to SPCC M.2 PCIe SSD, D: to Fanxiang S501 1TB, E: to SPCC (a second
partition on the same disk, correctly sharing its temperature), and Z: (a
network share) to nothing. `merge_drive_health()` copies rather than annotating
in place, because `read_all_disks()` hands out its cache and a stale temperature
would otherwise outlive the drive dropping out of LHM by a minute.

**Fans at 0 RPM are hidden.** The user's call, and it is a real trade: this board
reports eight headers and five read zero, so showing them all is noise - but it
means the row says which fans are turning, not which exist, and a fan that FAILS
disappears instead of showing a zero. If that ever needs reversing it is one
condition in `_lhm_fans()` and the note in the README.

**DEFAULT_ROWS and DEFAULT_CARDS.** `ROW_IDS` is now everything *available* and
`DEFAULT_ROWS` everything *on* - the same split `card_ids()` and
`default_card_ids()` already had for the second GPU card. Every reading added
this turn is off by default. `layout_schema()` carries `defaults` so the tray,
the page editor and the server agree on what a card looks like when it is ticked
back on; before this the tray would have turned a card on with every row it has.

**The grid wraps past four cards.** Six cards in one row is 170px each on the
iPad, narrower than the headline number, and six stacked in portrait clipped the
meters. Both orientations now go to two rows (landscape) or two columns
(portrait) above four cards. Measured at 1024x768: 3x2 at 327x367, nothing
clipped, no overflow; portrait 768x1024 gives 2x3 at 330px.

Per-card GPU fan counts fell out of the same work: these two cards report 2 and
3 fans and the chip only ever showed the first. It now shows the hardest-working
fan with the count beside it ("0 % fan x3"), because what matters at a glance is
whether the card is spinning up, not which individual fan is doing it.

---

## v1.6: the tablet could not start a video, and said so to nobody

Reported as "the wallpapers no longer load" plus "it keeps dropping the
connection and reconnecting". The tablet was in fact fine, and its own words
gave it away: the hint line read **"tap to start"**.

That string had exactly one source - `playVideo()`'s rejection handler - and a
grep for tap handlers found that **nothing anywhere listened for the tap**. The
one `touchend` listener in `app.js` re-arms the wake lock. So on any browser that
refuses muted autoplay the message was a promise nothing kept, and the wallpaper
never appeared. Safari on the iPad allows the autoplay, which is why this
survived until a second device arrived.

`tryPlay()` now owns every attempt, sets `video.muted` as a **property** (the
autoplay policy tests the property, and `load()` has been called on the element
since the markup was parsed), and on rejection sets `awaitingTap`. A one-shot
gesture handler on touchend/pointerdown/click/keydown retries inside the gesture,
and a `playing` listener clears the prompt and the failure count.

**Testing it needed care.** Stubbing `HTMLMediaElement.prototype.play` to reject
is not enough: the element carries `autoplay` in the markup, so headless Chromium
starts the video by itself regardless of the stubbed method, fires `playing`, and
clears the very prompt under test - which reads exactly like the fix not working.
Remove the attribute in the test and the device's behaviour is reproduced
faithfully. Proof, in order: `preparing...`, ``, `tap the screen to start ...`,
then a second `play()` tagged `gesture`, then the prompt cleared.

**The reconnecting was real too, and separate.** Two causes:

- The video error handler retried every two seconds for ever, and each retry
  re-downloads the file. A client that cannot decode it spends all its time
  fetching a video it will never play, and its 1 Hz poll goes late - which the
  page reports as losing the server. Now four tries with a widening gap, then a
  message.
- `socketserver`'s default accept backlog is **5**, and this server never raised
  it. Every request is its own connection (HTTP/1.0, `Connection: close`), so
  three viewers at 1 Hz plus one page load - index, css, two scripts, poster
  images, a video with Range requests - bursts well past five. An overflowed
  backlog drops the SYN and the client waits out a retransmit timeout. It was
  visible as SYN_RECEIVED piling up against the port from the third device
  specifically. `request_queue_size = 128` now.

**HTTP/1.1 keep-alive was considered and declined.** It would cut connections by
roughly the poll rate, but two paths in `_serve_file` are not keep-alive safe: the
416 branch sent no `Content-Length` (fixed here anyway), and the body loop
`return`s on BrokenPipeError having written fewer bytes than it promised, which
desyncs a reused connection. Closing per request is what makes that abort
harmless, and Safari aborting a seek is normal traffic here. Raising the backlog
buys most of the benefit for none of the risk.

---

## v1.7: the tap was causing the abort that stopped the video

v1.6 made "tap to start" real, and on the tablet the tap then produced
**AbortError**. That is not a refusal and not a codec problem: `play()` rejects
with AbortError when something interrupts it - a `pause()`, or a reload of the
element - while it is in flight.

`resume()` was the interrupter:

```js
if (video.error || video.readyState === 0) apply(current);   // "came back broken"
```

`readyState === 0` is HAVE_NOTHING, which is also **exactly what a video that is
still loading looks like**. `resume()` runs on `visibilitychange`, `pageshow` and
**`focus`** - and a tap raises focus. So the tap called resume(), resume() decided
a still-loading video was broken, `apply()` tore it down through `clearVideo()`
(`pause()` + `removeAttribute('src')` + `load()`), and the play() the tap had just
started rejected with AbortError. The user's own words were the clue: "it tried
to".

Fixed three ways:

- `resume()` only rebuilds on a real `video.error`, or on readyState 0 with
  `networkState !== NETWORK_LOADING`. Still loading is not broken.
- A rebuild happens at most once every three seconds however many focus and
  visibility events arrive. Each rebuild re-downloads the file, and on a weak
  client that alone starves the 1 Hz poll.
- AbortError no longer asks for a tap. It retries quietly up to five times and
  only then says it keeps being interrupted.

Measured: eight `focus` events fired during the load produce **one** `load()` and
one `pause()`, no AbortError, and the video reaches readyState 4 playing. Before,
each of those eight tore the element down.

**The layout-shift watchdog is now behind `?debug=1`.** It called
`getBoundingClientRect()` on twenty elements every animation frame, for ever -
roughly 1200 forced layouts a second. It found the jitter it was written for, but
on the tablet it starves the main thread enough that the 1 Hz poll lands late and
the status flips live/reconnecting as the numbers change, which is precisely what
the user reported. Keep the tool, stop running it for every viewer.

**Wallpaper failures now report themselves.** `reportVideo()` POSTs the play
rejection name, `video.error.code`, readyState/networkState, the decoded size and
`canPlayType()` for baseline/main/high to `/api/diag`, once per wallpaper per
stage. A tablet has no console anyone can reach and the server sees only a
successful file transfer, so without this the next failure is another round of
guessing.

**Still unproven, and the next thing to look at if video fails again.** The
transcode produces widths that are not multiples of 16 - measured 1366x768 for a
16:9 source and 1922x768 for an ultrawide one - in High profile with 2 B-frames.
Old Android hardware decoders commonly require macroblock-aligned width, and 1366
is the classic case. If diag.log comes back with `high40: ""` or a media error 3
or 4, the fix is `scale=-16:768` plus `-profile:v main -bf 0` in `_transcode()`,
and a cache key change so existing files are re-encoded. Not done on a hypothesis:
it re-transcodes everything and the AbortError above explained the symptom.

---

## Not started, but discussed

- **Video wallpaper sharpness.** `TARGET_HEIGHT = 768` in `wallpapers.py` was
  chosen when spacedesk fed the iPad 960Ã—1280. Viewed directly the panel is
  2048Ã—1536, so loops are ~2Ã— upscaled. Raising it to 1080 was offered and not
  yet accepted: roughly double the file size (the 97 MB astronaut becomes
  ~180 MB) and everything in `cache/` must be re-transcoded (just delete it).
- **Sparklines.** A 60-second rolling history per card was offered twice and
  never taken up.
- **Extra sensors.** LHM exposes GPU Memory Junction (runs much hotter than GPU
  core and is the number that matters on a 5070 Ti), CCD1, VRM MOS, SSD and
  per-DIMM temperatures. Offered, not built.

---

## It is an exe now

`CaseGauge.exe`, built with PyInstaller (see `README.md` for the command). It
now bundles `web/` and `assets/` **inside** the onefile exe, so it ships as a
single download. The resolution rule is in `server._resource_dir()`: a **local
`web/` or `assets/` folder beside the exe wins**, otherwise it falls back to
`sys._MEIPASS`. That keeps the live-edit workflow intact on a development
machine while letting the released exe run alone. Writable state
(`cache/`, `state.json`, `CaseGauge.log`) is always beside the exe. Verified by
running the exe in an empty folder: index served, 16 cores, no errors.

Two consequences that will catch you out:

- **Changing `server.py`, `tray.py` or `wallpapers.py` requires a rebuild.**
  Restarting the exe runs the *old* bundled code. This is the easiest way to
  spend an hour debugging a fix that was never running.
- **Changing anything in `web/` requires nothing.** Those files sit next to the
  exe, are read per request, and bump the build stamp - which makes the iPad
  reload itself within a second.

`_HERE` is derived from `sys.executable` when `sys.frozen` is set, so `web/`,
`cache/`, `state.json` and `CaseGauge.log` resolve beside the exe rather than
in PyInstaller's temp extraction directory.

**`CaseGauge.log`** is the only diagnostic channel: `--noconsole` means no
stdout or stderr at all. It records tray startup and failures. Use `log()` in
`server.py` for anything else you need to see.

A warning about probes: `FindWindowW("CaseGaugeTray")` proved **unreliable** -
it repeatedly reported the tray missing while it was demonstrably working. Do
not conclude the tray is broken from that alone; check `CaseGauge.log` for
`tray icon created`.

## How to test

Do **not** ask the user to eyeball things you can measure. There is a headless
browser harness:

```bash
node "C:/Users/Ulrick/.claude/skills/browser-automation/browser.mjs" \
     http://127.0.0.1:8777/ --script <script.mjs> --screenshot <out.png>
```

Reusable scripts live in the session scratchpad (regenerate if gone):
`jitter2.mjs` samples leaf-element rects over time; `jitter3.mjs` forces
worst-case digit counts; `reflow.mjs` exercises hint/chips/status changes.

**Three traps that cost real time here:**

0. `gl.readPixels` returns black after compositing unless the context was made
   with `preserveDrawingBuffer: true`. To prove the shader is animating,
   screenshot a wallpaper-only region twice and compare hashes â€” and leave a
   generous gap, because the nebula drifts at `u_time * 0.055` and barely moves
   in under a second.


1. `page.evaluate` runs in an **isolated world** in patchright â€” page globals
   like `window.Wallpaper` are *not* reachable. Assert on the DOM instead.
   Doing so is better practice anyway: it tests what rendered.
2. Measuring a **container** proves nothing about its children. The first
   jitter fix looked complete because the test watched `.chips`, which is full
   width and never moves, instead of the chips inside it.

Also: `page.setViewportSize` needs a moment before rects settle. An early read
made the canvas look like it had a stale aspect ratio when it did not.

---

## Things that will bite you

- **Caching.** `index.html` is generated per request by `Handler._send_index`,
  which stamps every asset URL with `build_stamp()` (newest mtime of the web
  files) and sends `no-store`. Safari in standalone mode ignores `no-cache`
  headers, so the URL itself has to change. Do not "simplify" this away.
- **Web files vs Python files.** Editing `web/*` needs only a page reload.
  Editing `server.py` / `tray.py` / `wallpapers.py` needs a server restart
  (tray â†’ Quit, or `stop.bat`, then the Startup shortcut or `start.bat`).
- **`allow_reuse_address`.** Deliberately `False` on the `Server` class. On
  Windows `SO_REUSEADDR` lets two processes bind the *same* listening port, and
  the dashboard then sees alternating stale readings. This actually happened.
- **LHM fast-fail.** `read_cpu_temp()` does a 0.3s socket probe and backs off
  15s when LHM is absent. Without it, a refused connect to `localhost` costs a
  full timeout *per address family* and drags the 1 Hz loop to 4 seconds.
- **`localhost` vs `127.0.0.1`.** Always the latter for LHM, same reason.
- **Range requests.** iOS Safari will not play a `<video>` from a server that
  ignores `Range`. `_send_file` implements it; `SimpleHTTPRequestHandler` does
  not.
- **`wallpapers/` the folder vs `wallpapers.py` the module.** They sit side by
  side. Python resolves the module file ahead of a directory with no
  `__init__.py`, so the import is safe — but do not put an `__init__.py` in
  that folder, and do not rename the module to match it.
- **NVENC is assumed.** The transcode hardcodes `-hwaccel cuda` and
  `h264_nvenc`. The local folder makes the app portable to machines without an
  NVIDIA GPU, where video wallpapers will now fail at the encode step; stills
  still work. A `libx264` fallback has not been written.
- **DPI.** `kiosk.ps1` deliberately does **not** call `SetProcessDPIAware`. At
  125% scale a DPI-aware process sees different coordinates than an unaware
  one, and Chromium places windows in whatever space it is handed. Reading and
  passing coordinates from the same unaware process keeps them consistent.
- **Agent permission blocks.** Registering scheduled tasks and adding firewall
  rules are refused as persistence/system changes. Hand the user the command.

---

## Working style the user expects

- Measure, don't speculate. They responded well to being shown numbers
  (`"44" 161.2px vs "45" 158.2px`) rather than told a theory.
- Say plainly when something was wrong or incomplete. Two fixes here were
  announced as complete and were not; admitting the test was flawed landed
  better than re-asserting.
- They are comfortable with admin steps and kernel drivers when the reason is
  explained, and they care a lot about **RAM footprint** â€” the whole reason
  spacedesk (~490 MB with Edge) was dropped for this (~31 MB).
