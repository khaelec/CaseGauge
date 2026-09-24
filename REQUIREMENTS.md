# CaseGauge — v1.0 public release requirements

The working checklist for publishing CaseGauge for free, with an optional
donation link. Nothing here is about features; it is what has to be true before
the repo and the release are public.

## Decisions

| | |
|---|---|
| **Name** | CaseGauge |
| **License** | Apache-2.0 |
| **Copyright holder** | khaelec |
| **Repo** | `github.com/khaelec/CaseGauge` |
| **Distribution** | Free, source on GitHub, exe on Releases, optional donation |
| **Donation** | Buy Me a Coffee — link, voluntary, no feature gating |

## R1 · Licensing artifacts

- [x] `LICENSE` — Apache-2.0, copyright filled in
- [x] `THIRD-PARTY-NOTICES.md` — Python/PSF, PyInstaller, LHM, ffmpeg, NVIDIA, Wallpaper Engine, trademarks
- [x] README **Credits & legal** section — disclaimer + non-affiliation + "must own Wallpaper Engine"

## R2 · Asset provenance

- [x] App icon — `assets/icon.ico` (multi-size) plus Home Screen artwork
- [x] Tray loads `assets/icon.ico`; `shell32.dll` extraction removed
- [ ] Confirm the nebula shader in `web/wallpaper.js` is original (or replace it) — an unattributed Shadertoy shader is CC BY-NC-SA

## R3 · Repository

- [ ] `git init`, first commit
- [x] `.gitignore` covers the exe, machine state, logs and caches
- [ ] README rewritten for a public audience (name, screenshots, quick start)
- [ ] User docs: install, iPad setup, troubleshooting

## R4 · Distribution

- [x] Single-file exe — `web/` and `assets/` bundled in, local folder overrides
- [ ] Versioned GitHub Release — `CaseGauge.exe` + SHA-256
- [x] Build command documented (reproducible)
- [ ] Prerequisites listed: Windows, NVIDIA GPU, LibreHardwareMonitor (elevated), ffmpeg, iPad

## R5 · Credits

- [x] Wallpaper Engine credit + "you must own it, bought via Steam"
- [x] LibreHardwareMonitor, ffmpeg, NVIDIA, Python/PyInstaller

## R6 · Donation

- [ ] Buy Me a Coffee link in README, worded as a voluntary tip

## R7 · Hardening (optional)

- [ ] Version string visible in-app
- [ ] LAN-only security warning made prominent

## Deliberately deferred

- Bundling ffmpeg or LibreHardwareMonitor. Keeping both **user-installed** is
  what avoids the GPL and driver-redistribution questions. Revisit only with
  legal input.
- Code signing. Nice to have; not required for a free release.
- Authentication/TLS. Only needed if CaseGauge ever leaves a trusted LAN.
