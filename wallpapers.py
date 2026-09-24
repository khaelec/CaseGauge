#!/usr/bin/env python3
"""
Wallpaper Engine library reader.

Wallpaper Engine itself is never launched. This only reads the files its
workshop downloads already left on disk.

Of the four wallpaper types, two survive the trip to an iPad:

  video   an .mp4 - transcoded once to an iPad-sized loop and cached
  web     a folder of HTML/JS - served as-is behind a small API shim
  scene   Wallpaper Engine's proprietary .pkg format; only its own renderer
          reads it, so these are listed as unsupported
  application  an .exe; not happening
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time

APP_ID = "431960"  # Wallpaper Engine

# Target height for transcoded loops. Aspect is preserved and the browser
# crops with object-fit: cover, so this stays correct if the iPad rotates.
TARGET_HEIGHT = 768
MAX_BITRATE = "3M"

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# Locating things
# ---------------------------------------------------------------------------

def _steam_libraries():
    """Every Steam library path, from the default install plus libraryfolders.vdf."""
    roots = []
    for base in (
        os.path.expandvars(r"%ProgramFiles(x86)%\Steam"),
        os.path.expandvars(r"%ProgramFiles%\Steam"),
    ):
        if os.path.isdir(base):
            roots.append(base)

    vdfs = [os.path.join(r, "steamapps", "libraryfolders.vdf") for r in list(roots)]
    for vdf in vdfs:
        if not os.path.isfile(vdf):
            continue
        try:
            text = open(vdf, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        # "path"    "D:\\SteamLibrary"
        for m in re.finditer(r'"path"\s*"([^"]+)"', text):
            p = m.group(1).replace("\\\\", "\\")
            if os.path.isdir(p) and p not in roots:
                roots.append(p)
    return roots


def workshop_dirs():
    """Directories holding downloaded Wallpaper Engine items."""
    override = os.environ.get("WALLPAPER_DIR")
    if override and os.path.isdir(override):
        return [override]

    found = []
    for root in _steam_libraries():
        d = os.path.join(root, "steamapps", "workshop", "content", APP_ID)
        if os.path.isdir(d):
            found.append(d)
    return found


def find_ffmpeg():
    """ffmpeg on PATH, or where winget's shim puts it (PATH needs a new shell)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    pkgs = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(pkgs):
        for dirpath, _, files in os.walk(pkgs):
            if "ffmpeg.exe" in files:
                return os.path.join(dirpath, "ffmpeg.exe")
    return None


FFMPEG = find_ffmpeg()


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

_PREVIEW_NAMES = ("preview.jpg", "preview.png", "preview.gif", "preview.webp")


def _preview_for(folder, project):
    name = project.get("preview")
    if name and os.path.isfile(os.path.join(folder, name)):
        return name
    for cand in _PREVIEW_NAMES:
        if os.path.isfile(os.path.join(folder, cand)):
            return cand
    return None


def scan():
    """Every downloaded wallpaper, newest-looking first, with a 'supported' flag."""
    items = []
    for root in workshop_dirs():
        for wid in os.listdir(root):
            folder = os.path.join(root, wid)
            pj = os.path.join(folder, "project.json")
            if not os.path.isfile(pj):
                continue
            try:
                with open(pj, encoding="utf-8-sig", errors="replace") as fh:
                    project = json.load(fh)
            except (OSError, ValueError):
                continue

            kind = (project.get("type") or "").lower()
            entry = project.get("file") or ""
            path = os.path.join(folder, entry) if entry else ""

            supported = (
                (kind == "video" and os.path.isfile(path))
                or (kind == "web" and os.path.isfile(path))
            )

            items.append({
                "id": wid,
                "title": project.get("title") or wid,
                "type": kind,
                "entry": entry,
                "folder": folder,
                "preview": _preview_for(folder, project),
                "supported": supported,
                "size_mb": round(os.path.getsize(path) / 1048576, 1)
                           if os.path.isfile(path) else None,
            })

    items.sort(key=lambda i: (not i["supported"], i["title"].lower()))
    return items


def by_id(wid):
    for item in scan():
        if item["id"] == wid:
            return item
    return None


def default_properties(folder):
    """The user-property defaults Wallpaper Engine would hand the wallpaper."""
    try:
        with open(os.path.join(folder, "project.json"),
                  encoding="utf-8-sig", errors="replace") as fh:
            project = json.load(fh)
    except (OSError, ValueError):
        return {}

    props = (project.get("general") or {}).get("properties") or {}
    out = {}
    for key, spec in props.items():
        if isinstance(spec, dict) and "value" in spec:
            out[key] = {"value": spec["value"]}
    return out


# Stands in for the API Wallpaper Engine injects. Without it these wallpapers
# throw on load: they assume wallpaperRegisterAudioListener and friends exist,
# and they wait to be handed their user properties before drawing anything.
_SHIM = """<script>
(function () {
  var noop = function () {};
  window.wallpaperRegisterAudioListener = window.wallpaperRegisterAudioListener || noop;
  window.wallpaperRequestRandomFileForProperty = window.wallpaperRequestRandomFileForProperty || noop;
  window.wallpaperRegisterMediaStatusListener = window.wallpaperRegisterMediaStatusListener || noop;
  window.wallpaperRegisterMediaPropertiesListener = window.wallpaperRegisterMediaPropertiesListener || noop;
  window.wallpaperRegisterMediaThumbnailListener = window.wallpaperRegisterMediaThumbnailListener || noop;
  window.wallpaperRegisterMediaTimelineListener = window.wallpaperRegisterMediaTimelineListener || noop;
  window.wallpaperPluginListener = window.wallpaperPluginListener || { onPluginLoaded: noop };

  var PROPS = __PROPS__;

  // The wallpaper installs its listener as the page parses, so deliver the
  // defaults once everything has loaded rather than racing it.
  function deliver() {
    var l = window.wallpaperPropertyListener;
    if (!l) return;
    try { if (l.applyGeneralProperties) l.applyGeneralProperties({ fps: 30 }); } catch (e) {}
    try { if (l.applyUserProperties) l.applyUserProperties(PROPS); } catch (e) {}
    try { if (l.setPaused) l.setPaused(false); } catch (e) {}
  }
  if (document.readyState === 'complete') setTimeout(deliver, 0);
  else window.addEventListener('load', function () { setTimeout(deliver, 0); });
})();
</script>"""


def entry_html(item, path):
    """The wallpaper's entry page with the shim inserted, or None to serve raw."""
    try:
        html = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None

    shim = _SHIM.replace("__PROPS__", json.dumps(default_properties(item["folder"])))

    m = re.search(r"<head[^>]*>", html, re.IGNORECASE)
    if m:
        return html[:m.end()] + shim + html[m.end():]
    return shim + html


def safe_join(folder, relative):
    """Resolve relative inside folder, refusing anything that escapes it."""
    folder = os.path.realpath(folder)
    target = os.path.realpath(os.path.join(folder, relative.replace("/", os.sep)))
    if target != folder and not target.startswith(folder + os.sep):
        return None
    return target


# ---------------------------------------------------------------------------
# Transcoding
# ---------------------------------------------------------------------------

# id -> {"state": pending|running|done|error, "detail": str, "path": str}
_jobs = {}
_jobs_lock = threading.Lock()


def cache_path(cache_dir, wid):
    return os.path.join(cache_dir, wid + ".mp4")


def job_status(wid):
    with _jobs_lock:
        job = _jobs.get(wid)
        return dict(job) if job else None


def _probe_height(path):
    ffprobe = os.path.join(os.path.dirname(FFMPEG), "ffprobe.exe") if FFMPEG else None
    if not ffprobe or not os.path.isfile(ffprobe):
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW,
        )
        return int(out.stdout.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _set(wid, state, detail="", path=""):
    with _jobs_lock:
        _jobs[wid] = {"state": state, "detail": detail, "path": path}


def _transcode(item, out_path):
    wid = item["id"]
    src = os.path.join(item["folder"], item["entry"])

    height = _probe_height(src)
    # Already small enough: copying beats spending GPU time to make it worse.
    if height is not None and height <= TARGET_HEIGHT:
        try:
            shutil.copyfile(src, out_path)
            _set(wid, "done", "copied (already %dp)" % height, out_path)
            return
        except OSError as exc:
            _set(wid, "error", str(exc))
            return

    tmp = out_path + ".part"
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-hwaccel", "cuda",
        "-i", src,
        "-an",                                    # a wallpaper has no sound
        "-vf", "scale=-2:%d:flags=bicubic" % TARGET_HEIGHT,
        "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "30",
        "-maxrate", MAX_BITRATE, "-bufsize", "6M",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",                # lets Safari start early
        "-f", "mp4",                              # .part hides the container
        tmp,
    ]

    _set(wid, "running", "encoding")
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=1800, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        _set(wid, "error", str(exc))
        return

    if proc.returncode != 0 or not os.path.isfile(tmp):
        detail = (proc.stderr or "ffmpeg failed").strip().splitlines()[-1:] or ["failed"]
        _set(wid, "error", detail[0][:200])
        if os.path.isfile(tmp):
            os.remove(tmp)
        return

    os.replace(tmp, out_path)
    mb = os.path.getsize(out_path) / 1048576
    _set(wid, "done", "%.0fs, %.1f MB" % (time.time() - started, mb), out_path)


def ensure_video(item, cache_dir):
    """Return (path, status). Kicks off a background transcode when needed."""
    os.makedirs(cache_dir, exist_ok=True)
    out = cache_path(cache_dir, item["id"])

    if os.path.isfile(out):
        return out, {"state": "done", "detail": "cached", "path": out}

    if not FFMPEG:
        return None, {"state": "error", "detail": "ffmpeg not found"}

    with _jobs_lock:
        job = _jobs.get(item["id"])
        if job and job["state"] in ("pending", "running"):
            return None, dict(job)
        _jobs[item["id"]] = {"state": "pending", "detail": "queued", "path": ""}

    threading.Thread(target=_transcode, args=(item, out), daemon=True).start()
    return None, {"state": "pending", "detail": "queued", "path": ""}
