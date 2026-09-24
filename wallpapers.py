#!/usr/bin/env python3
"""
Wallpaper library reader: the Wallpaper Engine workshop, plus a local folder.

Wallpaper Engine itself is never launched. This only reads the files its
workshop downloads already left on disk.

Of the four workshop types, two survive the trip to an iPad:

  video   an .mp4 - transcoded once to an iPad-sized loop and cached
  web     a folder of HTML/JS - served as-is behind a small API shim
  scene   Wallpaper Engine's proprietary .pkg format; only its own renderer
          reads it, so these are listed as unsupported
  application  an .exe; not happening

The second source is the ``wallpapers`` folder beside the exe. It needs no
Steam and no Wallpaper Engine, so whatever is dropped in it travels with the
app, and it adds a type the workshop has no equivalent for:

  image   a .jpg/.png/.gif/.webp - served as-is, no transcode, no ffmpeg

Unusable items are still listed, each carrying a ``reason``, because a
wallpaper that silently never appears reads as a bug in the scan.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
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
# The local folder
# ---------------------------------------------------------------------------

LOCAL_DIR_NAME = "wallpapers"

# Anything ffmpeg can read may be dropped in; it is transcoded to .mp4 on the
# way to the iPad the same as a workshop video is.
_VIDEO_EXTS = (".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi")
# Safari on iPadOS renders all of these, animated .gif included.
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif")


def _app_dir():
    """Beside the exe once frozen, beside this file otherwise."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def local_dir():
    """The folder beside the exe. Always scanned, always exists."""
    return (os.environ.get("CASEGAUGE_WALLPAPERS")
            or os.path.join(_app_dir(), LOCAL_DIR_NAME))


# Folders the user has pointed the app at, on top of the one beside the exe.
# The server owns the list and persists it in state.json; this is only where
# the scan reads it from, which keeps scan() and by_id() callable with no
# arguments from the tray and the request handlers alike.
_extra_sources = []


def set_sources(paths):
    """Replace the user-chosen source folders."""
    global _extra_sources
    _extra_sources = [p for p in (paths or []) if p]


def source_roots():
    """Every folder holding the user's own wallpapers, the built-in one first.

    Duplicates and folders nested inside one another are dropped, or a file
    would be listed twice under two different ids.
    """
    roots = []
    for path in [local_dir()] + list(_extra_sources):
        try:
            real = os.path.realpath(path)
        except OSError:
            continue
        if not os.path.isdir(real):
            continue
        if any(real == r or real.startswith(r + os.sep) or
               r.startswith(real + os.sep) for r in roots):
            continue
        roots.append(real)
    return roots


def _local_id(root, rel):
    """A stable, filesystem- and URL-safe id.

    It is written into state.json as the current wallpaper, so it has to
    survive a restart: moving or renaming the file is the only thing that
    changes it. The digest covers the full path, so the same file name under
    two different source folders still gets two ids.
    """
    full = os.path.join(root, rel).replace("\\", "/")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-",
                  os.path.splitext(os.path.basename(rel))[0]).strip("-")
    digest = hashlib.sha1(full.lower().encode("utf-8")).hexdigest()[:8]
    return "local-%s-%s" % (digest, slug[:40].lower() or "item")


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


def _sibling_preview(folder, filename):
    """clouds.jpg standing in as the thumbnail for clouds.mp4."""
    stem = os.path.splitext(filename)[0]
    for ext in _IMAGE_EXTS:
        if os.path.isfile(os.path.join(folder, stem + ext)):
            return stem + ext
    return None


def _reason_for(project, kind, entry, path):
    """Why this one cannot be shown - worded for the dashboard to print."""
    if kind == "scene":
        return "Scene wallpaper - only Wallpaper Engine can render these"
    if kind == "application":
        return "Runs a program, so it cannot be shown in a browser"
    if not kind:
        if project.get("dependency") or project.get("preset"):
            return "A saved preset for another wallpaper, not a wallpaper itself"
        return "project.json does not say what type this is"
    if not entry:
        return "project.json names no file to play"
    if not os.path.isfile(path):
        return "Its file is missing: " + entry
    return "Unsupported type: " + kind


def _entry(wid, title, kind, entry, folder, preview, source, reason="",
           group=""):
    """One wallpaper.

    'title' is the full name, stored in state.json and shown in the tray.
    'label' is what the tile prints - just the file's own name, because a tile
    is 176px wide and a path prefix pushes the actual name out of sight.
    'group' is the heading it files under.
    """
    path = os.path.join(folder, entry) if entry else ""
    exists = os.path.isfile(path)
    supported = kind in ("video", "web", "image") and exists
    return {
        "id": wid,
        "title": title,
        "label": title.rsplit("/", 1)[-1],
        "group": group,
        "type": kind,
        "entry": entry,
        "folder": folder,
        "preview": preview,
        "source": source,
        "supported": supported,
        "reason": "" if supported else (reason or "Its file is missing"),
        "size_mb": round(os.path.getsize(path) / 1048576, 1) if exists else None,
    }


def _project_item(folder, wid, source, group=""):
    """One wallpaper read from a project.json, or None if there isn't one."""
    try:
        with open(os.path.join(folder, "project.json"),
                  encoding="utf-8-sig", errors="replace") as fh:
            project = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(project, dict):
        return None

    kind = (project.get("type") or "").lower()
    entry = project.get("file") or ""
    path = os.path.join(folder, entry) if entry else ""
    return _entry(wid, project.get("title") or wid, kind, entry, folder,
                  _preview_for(folder, project), source,
                  _reason_for(project, kind, entry, path), group)


# How far down to follow subfolders. Deep enough for the way people actually
# file things, shallow enough that a stray junction cannot run away with it.
_MAX_DEPTH = 4


def _single_wallpaper(root, folder, rel, name, group):
    """A folder that IS one wallpaper rather than a drawer holding several:
    a copied workshop item, or a web wallpaper with an index.html."""
    wid = _local_id(root, rel)
    item = _project_item(folder, wid, "local", group)
    if item:
        return item
    if os.path.isfile(os.path.join(folder, "index.html")):
        return _entry(wid, name, "web", "index.html", folder,
                      _preview_for(folder, {}), "local", "", group)
    return None


def _walk_local(root, folder, rel, depth, items, group):
    """Collect every wallpaper at or below folder.

    A drawer of twenty videos should list twenty wallpapers, not one, so
    ordinary folders are descended into rather than treated as a single item.
    'group' is the heading these file under: the containing folder's name, so
    the picker can show "Frieren" over the six loops that live in it.
    """
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return

    # clouds.jpg beside clouds.mp4 is that video's thumbnail, so it must not
    # also be offered as a wallpaper in its own right.
    thumbs = set()
    for name in names:
        if name.lower().endswith(_VIDEO_EXTS):
            sib = _sibling_preview(folder, name)
            if sib:
                thumbs.add(sib.lower())

    # Names are prefixed by the folder they sit in, so two files called
    # "loop.mp4" in different drawers stay tellable apart in the picker.
    def titled(stem):
        return (rel + "/" + stem) if rel else stem

    found = 0
    for name in names:
        path = os.path.join(folder, name)
        child = (rel + "/" + name) if rel else name
        lower = name.lower()

        if os.path.isdir(path):
            if depth >= _MAX_DEPTH:
                continue
            one = _single_wallpaper(root, path, child, titled(name), group)
            if one:
                items.append(one)
                found += 1
                continue
            before = len(items)
            _walk_local(root, path, child, depth + 1, items, child)
            found += len(items) - before
        elif lower.endswith(_VIDEO_EXTS):
            items.append(_entry(_local_id(root, child),
                                titled(os.path.splitext(name)[0]),
                                "video", name, folder,
                                _sibling_preview(folder, name), "local", "",
                                group))
            found += 1
        elif lower.endswith(_IMAGE_EXTS) and lower not in thumbs:
            # A still needs no preview of its own: it is its own thumbnail.
            items.append(_entry(_local_id(root, child),
                                titled(os.path.splitext(name)[0]),
                                "image", name, folder, name, "local", "",
                                group))
            found += 1

    # A folder with files in it but nothing usable is worth saying out loud;
    # an empty one is not.
    if rel and not found and names:
        items.append(_entry(_local_id(root, rel), rel, "", "", folder,
                            _preview_for(folder, {}), "local",
                            "Nothing usable in this folder - no picture, "
                            "video or index.html", group))


def _local_items():
    items = []
    builtin = os.path.realpath(local_dir())
    for root in source_roots():
        # Files sitting loose at the top of a source folder are grouped under
        # that folder's own name, so two added folders stay separate sections.
        # The built-in one keeps the friendlier "Your folder" heading: its real
        # name is an implementation detail the user never chose.
        group = "" if root == builtin else os.path.basename(root)
        _walk_local(root, root, "", 0, items, group)
    return items


def scan():
    """Every wallpaper on offer, local folder and workshop both.

    Unusable ones are included too, carrying a 'reason', so the dashboard can
    show that they were seen and rejected rather than never found.
    """
    items = _local_items()
    for root in workshop_dirs():
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for wid in names:
            item = _project_item(os.path.join(root, wid), wid, "workshop",
                                 "Wallpaper Engine")
            if item:
                items.append(item)

    # Usable first, then the folder the user controls, then by name.
    items.sort(key=lambda i: (not i["supported"],
                              i["source"] != "local",
                              i["title"].lower()))
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


# A local video usually has no artwork beside it, and a wall of blank tiles is
# no way to pick a wallpaper. One frame out of the file makes the thumbnail.
_poster_lock = threading.Lock()


def poster_path(cache_dir, wid):
    return os.path.join(cache_dir, wid + ".poster.jpg")


def ensure_poster(item, cache_dir):
    """A thumbnail for a video that shipped without one, or None.

    Cheap enough to do inline: ffmpeg seeks to a keyframe and decodes a single
    frame, so even a 4K source costs well under a second, and the result is
    cached for good.
    """
    if item["type"] != "video" or not FFMPEG:
        return None

    out = poster_path(cache_dir, item["id"])
    if os.path.isfile(out):
        return out

    src = os.path.join(item["folder"], item["entry"])
    if not os.path.isfile(src):
        return None

    # One at a time. Opening the picker asks for every missing thumbnail at
    # once, and a dozen parallel 4K decodes would bury the 1 Hz stats loop.
    with _poster_lock:
        if os.path.isfile(out):
            return out
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            return None

        tmp = out + ".part"
        # -ss before -i seeks without decoding what it skips. A second in
        # avoids the fade-from-black a lot of loops open on.
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-ss", "1", "-i", src, "-frames:v", "1",
               "-vf", "scale=-2:360:flags=bicubic",
               "-q:v", "4", "-f", "image2", tmp]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=60, creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return None

        # A clip shorter than the seek yields nothing; take frame one instead.
        if proc.returncode != 0 or not os.path.isfile(tmp):
            cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                   "-i", src, "-frames:v", "1",
                   "-vf", "scale=-2:360:flags=bicubic",
                   "-q:v", "4", "-f", "image2", tmp]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=60, creationflags=_NO_WINDOW)
            except (OSError, subprocess.SubprocessError):
                return None

        if proc.returncode != 0 or not os.path.isfile(tmp):
            if os.path.isfile(tmp):
                os.remove(tmp)
            return None
        try:
            os.replace(tmp, out)
        except OSError:
            return None
        return out


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
    small = height is not None and height <= TARGET_HEIGHT

    # Already small enough, and already the container the cache hands out:
    # copying beats spending GPU time to make it worse.
    if small and src.lower().endswith(".mp4"):
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
    ]
    # A .webm or .mkv that is already small still has to be re-wrapped, but
    # scaling it here would only enlarge it: spend the pixels on nothing and
    # blur it on the way. Leave anything under the target at its own size.
    if not small:
        cmd += ["-vf", "scale=-2:%d:flags=bicubic" % TARGET_HEIGHT]
    cmd += [
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
