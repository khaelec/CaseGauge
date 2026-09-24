# Third-party notices

CaseGauge itself is licensed under the Apache License 2.0 (see `LICENSE`).
It bundles no third-party libraries in the sense of shipping their code inside
this repository. What follows is what it **uses**, what it **runs**, and under
what terms, so the obligations are clear.

---

## Bundled with the released executable

CaseGauge's release is a single executable produced by PyInstaller, which
embeds a Python interpreter and the standard library.

### Python
© Python Software Foundation. Licensed under the **PSF License Agreement**,
a permissive, OSI-approved license.
<https://docs.python.org/3/license.html>

### PyInstaller
© the PyInstaller developers. Licensed under **GPL-2.0-or-later with the
Bootloader Exception**, which explicitly permits bundling and distributing
commercial and proprietary applications with no attribution requirement.
<https://pyinstaller.org/en/stable/license.html>

No source changes to PyInstaller or Python are distributed here.

---

## Used at runtime, but **not** bundled or distributed

These are external programs the user installs separately. CaseGauge only talks
to them if they are present; none of their code ships with CaseGauge, and
CaseGauge is not a derivative of them.

### LibreHardwareMonitor
© the LibreHardwareMonitor contributors. **MPL-2.0**.
Used for CPU temperature only. Not bundled — the user installs it. If you
redistribute CaseGauge together with LibreHardwareMonitor, you are responsible
for the MPL-2.0 obligations (ship the license; publish any changes you make to
LibreHardwareMonitor itself). Untouched binaries may be bundled.
<https://github.com/LibreHardwareMonitor/LibreHardwareMonitor>

### ffmpeg
© the FFmpeg developers. **LGPL-2.1-or-later** in its default build; builds
compiled with `--enable-gpl` (for example the "full" builds from gyan.dev,
which include libx264/x265) are **GPL-2.0-or-later**. Not bundled — the user
installs it (e.g. `winget install Gyan.FFmpeg`). CaseGauge invokes it as a
separate process. If you ever bundle an ffmpeg build, choose an LGPL build or
make your distribution GPL-compatible, and satisfy the LGPL source-offer
requirements.
<https://ffmpeg.org/legal.html>

### NVIDIA drivers / `nvidia-smi`
© NVIDIA Corporation. GPU readings come from `nvidia-smi`, which ships with the
NVIDIA driver on the user's machine. Not bundled.

---

## Wallpaper Engine

**Wallpaper Engine** is a Steam application developed and published by the
**Wallpaper Engine Team** (© Skutta Software GmbH, Steam app ID 431960).

- Wallpaper Engine is **not** bundled with, distributed with, or included in
  CaseGauge.
- To use the wallpaper feature you must **purchase and install Wallpaper Engine
  yourself, through Steam**, and download wallpapers through Steam Workshop.
- CaseGauge only reads files from **your own local library** and does not
  redistribute any wallpaper content.
- CaseGauge is **not affiliated with, sponsored by, or endorsed by** Skutta
  Software GmbH, Valve Corporation, or Steam.
- "Wallpaper Engine" and "Steam" are trademarks of their respective owners and
  are used here only to describe interoperability.

Only portable wallpaper types (video, web) can be used. Scene and application
wallpapers are proprietary and are rendered only by Wallpaper Engine itself.

---

## Trademarks

"Windows" is a trademark of Microsoft Corporation. "NVIDIA" and "GeForce" are
trademarks of NVIDIA Corporation. All other trademarks are the property of their
respective owners. Their use here is descriptive and does not imply any
affiliation or endorsement.

CaseGauge's own icon (`assets/`) is the project's own artwork, used for the
executable and the iPad Home Screen. No Windows shell icon assets are
redistributed. Note that a purely AI-generated image may not attract copyright
in some jurisdictions; it is used here as the project's own branding.
