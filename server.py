#!/usr/bin/env python3
"""
PC stats server for the iPad dashboard.

Serves web/ as static files and live sensor readings at /api/stats.
Standard library only - no pip installs, no build step.

Sensor sources:
  RAM       GlobalMemoryStatusEx (ctypes)          no admin
  CPU load  GetSystemTimes (ctypes)                no admin
  GPU       nvidia-smi                             no admin
  CPU temp  LibreHardwareMonitor's web server      needs LHM running elevated

Everything except CPU temp works without LibreHardwareMonitor; that field
simply reports null until LHM is up.
"""

import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import wallpapers

# 127.0.0.1 rather than localhost on purpose: localhost resolves to ::1 as well,
# and when nothing is listening urllib pays the connect timeout once per family.
LHM_URL = os.environ.get("LHM_URL", "http://127.0.0.1:8085/data.json")
POLL_SECONDS = 1.0
LHM_RETRY_SECONDS = 15.0  # how long to stop asking after LHM looks absent

# When frozen by PyInstaller, __file__ points into a temporary extraction
# directory. Everything the app reads or writes - web/, cache/, state.json -
# must live next to the executable instead, so that editing a web file still
# changes build_stamp() and still reaches the iPad.
if getattr(sys, "frozen", False):
    _HERE = os.path.dirname(os.path.abspath(sys.executable))
else:
    _HERE = os.path.dirname(os.path.abspath(__file__))

# Read-only resources - the dashboard and the icon - may live either beside the
# exe or bundled inside it. A local copy WINS, so editing web/ still changes the
# build stamp and still reaches the iPad on a development machine; a released
# single-file exe, with nothing beside it, falls back to the copy PyInstaller
# unpacked into _MEIPASS. That is what lets CaseGauge.exe ship on its own.
_MEIPASS = getattr(sys, "_MEIPASS", None)


def _resource_dir(name):
    local = os.path.join(_HERE, name)
    if os.path.isdir(local):
        return local
    if _MEIPASS:
        bundled = os.path.join(_MEIPASS, name)
        if os.path.isdir(bundled):
            return bundled
    return local


WEB_ROOT = _resource_dir("web")
ASSETS_DIR = _resource_dir("assets")
# Writable state always lives beside the exe, never in the read-only bundle.
CACHE_DIR = os.environ.get("PCSTATS_CACHE", os.path.join(_HERE, "cache"))


def _load_port():
    """Resolve the listening port.

    Order: ``--port N``, then ``PCSTATS_PORT``, then a ``port`` key in
    ``casegauge.json`` beside the exe, else 8777.

    The config file exists because the exe has no console, so an environment
    variable or a shortcut argument is easy to miss for anyone who just
    double-clicks it. One server serves every tablet on one port - change this
    only to avoid a clash, or to run a second instance (which needs its own
    folder, since ``state.json`` is shared per-folder).
    """
    val = None
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv):
            val = argv[i + 1]
        elif arg.startswith("--port="):
            val = arg.split("=", 1)[1]
    if val is None:
        val = os.environ.get("PCSTATS_PORT")
    if val is None:
        try:
            # utf-8-sig: a hand-edited file (Notepad, PowerShell) often carries
            # a BOM, which plain json.load would reject.
            with open(os.path.join(_HERE, "casegauge.json"),
                      encoding="utf-8-sig") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict) and saved.get("port") is not None:
                val = saved["port"]
        except (OSError, ValueError):
            pass
    try:
        return max(1, min(65535, int(val)))
    except (TypeError, ValueError):
        return 8777


PORT = _load_port()

# Hide the console window nvidia-smi would otherwise flash on every poll.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# RAM
# ---------------------------------------------------------------------------

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def read_ram():
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return None
    gb = 1024 ** 3
    total = stat.ullTotalPhys / gb
    avail = stat.ullAvailPhys / gb
    return {
        "used_gb": round(total - avail, 2),
        "total_gb": round(total, 2),
        "percent": stat.dwMemoryLoad,
    }


DRIVE_FIXED, DRIVE_REMOTE = 3, 4


def list_drives():
    """Every fixed and network drive, so one can be chosen from the tray."""
    try:
        need = ctypes.windll.kernel32.GetLogicalDriveStringsW(0, None)
        buf = ctypes.create_unicode_buffer(need)
        ctypes.windll.kernel32.GetLogicalDriveStringsW(need, buf)
    except OSError:
        return []

    out = []
    for root in buf[:need].split(chr(0)):
        if not root:
            continue
        try:
            kind = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        except OSError:
            continue
        if kind not in (DRIVE_FIXED, DRIVE_REMOTE):
            continue          # skip optical, removable and unmapped
        out.append({"root": root, "drive": root[:2],
                    "network": kind == DRIVE_REMOTE})
    return out


_disk_cache = {"at": 0.0, "value": []}
DISK_CACHE_SECONDS = 15.0


def read_all_disks():
    """Every fixed and network drive, cached.

    Deliberately not read every poll: free space changes slowly, and
    GetDiskFreeSpaceExW on an unreachable network share can block for a long
    time - which would stall the 1 Hz loop for every other sensor too.
    """
    now = time.monotonic()
    if now - _disk_cache["at"] < DISK_CACHE_SECONDS and _disk_cache["value"]:
        return _disk_cache["value"]

    out = []
    for drv in list_drives():
        info = read_disk(drv["root"])
        if info:
            info["network"] = drv["network"]
            out.append(info)
    _disk_cache["at"] = now
    _disk_cache["value"] = out
    return out


def read_disk(root=None):
    """Used/total for one drive. Network drives can be slow or vanish, so a
    failure here returns None rather than stalling the poll loop."""
    if root is None:
        root = get_disk_root()
    free = ctypes.c_ulonglong()
    total = ctypes.c_ulonglong()
    try:
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(root), None,
            ctypes.byref(total), ctypes.byref(free))
    except OSError:
        return None
    if not ok or not total.value:
        return None
    gb = 1024 ** 3
    used = (total.value - free.value) / gb
    return {
        "drive": root[:2],
        "used_gb": round(used, 1),
        "total_gb": round(total.value / gb, 1),
        "percent": round(100.0 * used / (total.value / gb)),
    }


# ---------------------------------------------------------------------------
# CPU load - GetSystemTimes deltas between polls
# ---------------------------------------------------------------------------

_prev_times = None


def read_cpu_load():
    """Percent busy since the previous call. The first call returns None."""
    global _prev_times
    idle = ctypes.c_ulonglong()
    kern = ctypes.c_ulonglong()
    user = ctypes.c_ulonglong()
    ok = ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)
    )
    if not ok:
        return None

    # kern already includes idle time, so total busy+idle is kern + user.
    cur = (idle.value, kern.value + user.value)
    prev, _prev_times = _prev_times, cur
    if prev is None:
        return None

    d_idle = cur[0] - prev[0]
    d_total = cur[1] - prev[1]
    if d_total <= 0:
        return None
    return round(100.0 * (d_total - d_idle) / d_total, 1)


# ---------------------------------------------------------------------------
# Per-core CPU load - NtQuerySystemInformation
# ---------------------------------------------------------------------------

# GetSystemTimes is totals only, so the per-logical-processor figure comes from
# NtQuerySystemInformation with SystemProcessorPerformanceInformation (class 8).
# It returns one record per logical processor (16 on a 5700X3D), and the busy
# percentage is the same delta arithmetic as read_cpu_load(). No admin needed.

_SPPI_CLASS = 8
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


class _PROC_PERF(ctypes.Structure):
    # Five LARGE_INTEGERs (8 bytes each) then a ULONG. sizeof is 48 with
    # alignment, which is what the kernel expects - do not "tidy" this.
    _fields_ = [
        ("IdleTime", ctypes.c_longlong),
        ("KernelTime", ctypes.c_longlong),
        ("UserTime", ctypes.c_longlong),
        ("DpcTime", ctypes.c_longlong),
        ("InterruptTime", ctypes.c_longlong),
        ("InterruptCount", ctypes.c_ulong),
    ]


_ntdll = ctypes.WinDLL("ntdll")
# Explicit argtypes: without them ctypes passes the buffer as a C int, which
# truncates the pointer on 64-bit and faults inside the kernel.
_ntdll.NtQuerySystemInformation.argtypes = [
    ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
    ctypes.POINTER(ctypes.c_ulong),
]
_ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong

_prev_cores = None


def read_cpu_cores():
    """Per-logical-processor busy percent since the previous call.

    Returns a list of ints (one per logical processor), or None on the first
    call, when the structure is unavailable, or when the processor count
    changed (resume from sleep can do that) - in which case the caller should
    simply keep the last good list.
    """
    global _prev_cores
    n = os.cpu_count() or 0
    if n <= 0:
        return None

    size = ctypes.sizeof(_PROC_PERF) * n
    buf = ctypes.create_string_buffer(size)
    returned = ctypes.c_ulong(0)
    status = _ntdll.NtQuerySystemInformation(
        _SPPI_CLASS, buf, size, ctypes.byref(returned))
    if status == _STATUS_INFO_LENGTH_MISMATCH:
        size = max(returned.value, size)
        buf = ctypes.create_string_buffer(size)
        status = _ntdll.NtQuerySystemInformation(
            _SPPI_CLASS, buf, size, ctypes.byref(returned))
    if status != 0:
        return None

    count = returned.value // ctypes.sizeof(_PROC_PERF)
    if count <= 0:
        return None
    arr = (_PROC_PERF * count).from_buffer(buf)

    # KernelTime already includes idle time, so busy+idle is kernel + user.
    cur = [(p.IdleTime, p.KernelTime + p.UserTime) for p in arr]
    prev, _prev_cores = _prev_cores, cur
    if prev is None or len(prev) != len(cur):
        return None

    out = []
    for (p_idle, p_total), (c_idle, c_total) in zip(prev, cur):
        d_idle = c_idle - p_idle
        d_total = c_total - p_total
        out.append(round(100.0 * (d_total - d_idle) / d_total, 1) if d_total > 0
                   else 0.0)
    return out


# ---------------------------------------------------------------------------
# GPU - nvidia-smi
# ---------------------------------------------------------------------------

_GPU_FIELDS = "name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed"


def _num(text):
    """Parse a leading number, tolerating '[N/A]' and comma decimal separators."""
    if text is None:
        return None
    m = re.match(r"\s*([-+]?\d+(?:[.,]\d+)?)", str(text))
    return float(m.group(1).replace(",", ".")) if m else None


def read_gpu():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + _GPU_FIELDS,
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None

    parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
    if len(parts) < 7:
        return None

    used, total = _num(parts[3]), _num(parts[4])
    return {
        "name": parts[0],
        "temp_c": _num(parts[1]),
        "load": _num(parts[2]),
        "vram_used_gb": round(used / 1024, 2) if used is not None else None,
        "vram_total_gb": round(total / 1024, 2) if total is not None else None,
        "power_w": _num(parts[5]),
        "fan": _num(parts[6]),
    }


# ---------------------------------------------------------------------------
# CPU temperature - LibreHardwareMonitor's JSON tree
# ---------------------------------------------------------------------------

# Sensor labels to accept, best first. AMD reports Tctl/Tdie; Intel uses Package.
_TEMP_LABELS = (
    "core (tctl/tdie)",
    "core (tctl)",
    "core (tdie)",
    "cpu package",
    "package",
    "core average",
)


def _flatten(node, path, out):
    text = node.get("Text", "")
    here = path + [text] if text else path
    value = node.get("Value")
    if value not in (None, ""):
        out.append((here, text, value))
    for child in node.get("Children", ()):
        _flatten(child, here, out)


_lhm_quiet_until = 0.0


def _lhm_listening():
    """Cheap port probe. A refused connect returns instantly, where urllib
    would spend its full timeout on every address localhost resolves to."""
    parsed = urllib.parse.urlparse(LHM_URL)
    try:
        with socket.create_connection(
            (parsed.hostname or "127.0.0.1", parsed.port or 8085), 0.3
        ):
            return True
    except OSError:
        return False


# Labels to accept for the GPU memory junction, best first. It runs a lot
# hotter than the GPU core and is the number that actually matters on a modern
# card, but nvidia-smi does not expose it - only LHM does.
_JUNCTION_LABELS = (
    "gpu memory junction",
    "gpu memory",
    "memory junction",
)


def read_lhm():
    """Everything worth having from LibreHardwareMonitor, in one fetch.

    Returns {"cpu_temp": C or None, "gpu_junction": C or None}. When LHM is
    absent this must fail fast, or one dead sensor drags the whole poll loop
    below its 1 Hz budget.
    """
    global _lhm_quiet_until

    empty = {"cpu_temp": None, "gpu_junction": None}

    now = time.monotonic()
    if now < _lhm_quiet_until:
        return empty

    if not _lhm_listening():
        _lhm_quiet_until = now + LHM_RETRY_SECONDS
        return empty

    try:
        with urllib.request.urlopen(LHM_URL, timeout=2) as resp:
            tree = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        _lhm_quiet_until = now + LHM_RETRY_SECONDS
        return empty

    sensors = []
    _flatten(tree, [], sensors)

    # Only consider readings under a Temperatures group that are measured in
    # degrees, so a voltage or clock can never be mistaken for a temperature.
    candidates = {}
    for path, label, value in sensors:
        joined = " / ".join(path).lower()
        if "temperatur" not in joined:
            continue
        if "c" not in str(value).lower():
            continue
        candidates.setdefault(label.strip().lower(), _num(value))

    def first_of(labels):
        for wanted in labels:
            if candidates.get(wanted) is not None:
                return candidates[wanted]
        return None

    return {
        "cpu_temp": first_of(_TEMP_LABELS),
        "gpu_junction": first_of(_JUNCTION_LABELS),
    }


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

_state = {"ok": False, "error": "starting up"}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Wallpaper selection - owned by the PC, not the viewer
# ---------------------------------------------------------------------------

# Kept on the server rather than in each viewer's localStorage so the choice
# can be made from this PC (tray menu) and every viewer follows it. Viewers
# still keep their own zoom locally, which is genuinely per-device.

STATE_PATH = os.path.join(_HERE, "state.json")
ZOOM_MIN, ZOOM_MAX = 0.6, 2.4

# Which cards are shown, in what order, and which rows inside each card.
# Same ownership model as wallpaper and zoom: the PC decides, every viewer
# follows on its next 1 Hz poll. A card that is absent from the list is hidden;
# the list order is the on-screen order.
ROW_IDS = {
    "cpu": ("temp", "load", "cores"),
    "gpu": ("temp", "load", "vram", "chips"),
    "ram": ("temp", "load", "free", "drives"),
}
CARD_ORDER = ("cpu", "gpu", "ram")
CARD_LABELS = {"cpu": "CPU", "gpu": "GPU", "ram": "Memory"}
ROW_LABELS = {
    "cpu": {"temp": "Temperature", "load": "Load meter",
            "cores": "Per-core square"},
    "gpu": {"temp": "Temperature", "load": "Load meter", "vram": "VRAM meter",
            "chips": "Power / Fan / Junction"},
    "ram": {"temp": "Temperature", "load": "In-use meter", "free": "Free GB",
            "drives": "Drive chips"},
}


def layout_schema():
    """Labels and ids for the tray menu, so they live in one place."""
    return [
        {"id": cid, "label": CARD_LABELS[cid],
         "rows": [{"id": r, "label": ROW_LABELS[cid][r]} for r in ROW_IDS[cid]]}
        for cid in CARD_ORDER
    ]


def default_layout():
    return [{"id": cid, "rows": list(ROW_IDS[cid])} for cid in CARD_ORDER]


_ui = {"wallpaper": {"mode": "shader"}, "zoom": 1.0, "disk": "",
       "layout": default_layout()}
_ui_lock = threading.Lock()


def _clean_layout(raw):
    """Accept only known cards/rows, in a canonical row order. None if unusable."""
    if not isinstance(raw, list):
        return None
    seen = set()
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id")
        if cid not in ROW_IDS or cid in seen:
            continue
        seen.add(cid)
        rows = entry.get("rows")
        if not isinstance(rows, list):
            rows = list(ROW_IDS[cid])
        # Canonical order, unknown ids dropped: the page can rely on this.
        out.append({"id": cid, "rows": [r for r in ROW_IDS[cid] if r in rows]})
    return out


def _save_locked():
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_ui, fh)
    except OSError:
        pass              # a read-only disk must not break the switch


def load_state():
    global _ui
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(saved, dict):
        return
    if "mode" in saved:
        # Older file held the wallpaper choice at the top level.
        _ui = {"wallpaper": saved, "zoom": 1.0}
        return
    wp = saved.get("wallpaper")
    if isinstance(wp, dict) and wp.get("mode"):
        _ui["wallpaper"] = wp
    try:
        _ui["zoom"] = max(ZOOM_MIN, min(ZOOM_MAX, float(saved.get("zoom", 1.0))))
    except (TypeError, ValueError):
        pass
    if isinstance(saved.get("disk"), str):
        _ui["disk"] = saved["disk"]
    lay = _clean_layout(saved.get("layout"))
    if lay:                       # never let a saved layout hide every card
        _ui["layout"] = lay


def get_ui():
    with _ui_lock:
        return {"wallpaper": dict(_ui["wallpaper"]), "zoom": _ui["zoom"],
                "layout": [{"id": c["id"], "rows": list(c["rows"])}
                           for c in _ui["layout"]]}


def get_layout():
    with _ui_lock:
        return [{"id": c["id"], "rows": list(c["rows"])} for c in _ui["layout"]]


def set_layout(raw):
    clean = _clean_layout(raw)
    if not clean:                 # refuse an empty or malformed layout
        return get_layout()
    with _ui_lock:
        _ui["layout"] = clean
        _save_locked()
    return get_layout()


def get_wallpaper():
    with _ui_lock:
        return dict(_ui["wallpaper"])


def set_wallpaper(choice):
    clean = {"mode": choice.get("mode") or "shader"}
    if clean["mode"] in ("video", "web"):
        clean["id"] = str(choice.get("id") or "")
        clean["title"] = choice.get("title") or ""
        if not clean["id"]:
            clean = {"mode": "shader"}
    with _ui_lock:
        _ui["wallpaper"] = clean
        _save_locked()
    return dict(clean)


def get_disk_root():
    with _ui_lock:
        chosen = _ui.get("disk") or ""
    if chosen:
        return chosen
    return (os.environ.get("SystemDrive") or "C:") + "\\"


def set_disk_root(root):
    with _ui_lock:
        _ui["disk"] = str(root or "")
        _save_locked()
    return get_disk_root()


def get_zoom():
    with _ui_lock:
        return _ui["zoom"]


def set_zoom(value):
    try:
        z = float(value)
    except (TypeError, ValueError):
        return get_zoom()
    z = round(max(ZOOM_MIN, min(ZOOM_MAX, z)), 2)
    with _ui_lock:
        _ui["zoom"] = z
        _save_locked()
    return z


def detect_cpu_name():
    try:
        out = subprocess.run(
            ["reg", "query",
             r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0",
             "/v", "ProcessorNameString"],
            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW,
        )
        m = re.search(r"ProcessorNameString\s+REG_SZ\s+(.+)", out.stdout)
        if m:
            return m.group(1).strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "CPU"


CPU_NAME = detect_cpu_name()


def poll_once():
    lhm = read_lhm()
    cpu_temp = lhm.get("cpu_temp")
    gpu = read_gpu()
    if gpu is not None:
        gpu["junction_c"] = lhm.get("gpu_junction")
    return {
        "ok": True,
        "time": time.time(),
        "cpu": {
            "name": CPU_NAME,
            "load": read_cpu_load(),
            "cores": read_cpu_cores(),
            "temp_c": cpu_temp,
            "threads": os.cpu_count(),
        },
        "gpu": gpu,
        "ram": read_ram(),
        "disk": read_disk(),
        "disks": read_all_disks(),
    }


def poller():
    global _state
    while True:
        try:
            snapshot = poll_once()
        except Exception as exc:  # one bad sensor must not kill the loop
            snapshot = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
        with _lock:
            _state = snapshot
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def log(message):
    """Append a line to CaseGauge.log beside the app.

    Frozen with --noconsole (and under pythonw) there is no stdout or stderr,
    so a tray failure would otherwise be completely invisible - and the tray
    is the only way to control the app.
    """
    try:
        with open(os.path.join(_HERE, "CaseGauge.log"), "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + chr(10))
    except OSError:
        pass


def build_stamp():
    """Newest mtime across the web assets, as a short cache-busting token."""
    newest = 0
    for name in ("index.html", "style.css", "app.js", "wallpaper.js"):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(WEB_ROOT, name)))
        except OSError:
            pass
    return format(int(newest), "x")


_MEDIA_TYPES = {
    ".mp4": "video/mp4", ".webm": "video/webm", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".html": "text/html", ".js": "text/javascript",
    ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".woff2": "font/woff2",
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_ROOT, **kwargs)

    # -- helpers ----------------------------------------------------------

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self._own_cache_header = True
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_index(self):
        """index.html with a build stamp on each asset URL.

        Safari - especially an Add-to-Home-Screen launch - will happily keep
        serving a cached app.js however many no-cache headers it was sent.
        Changing the URL when the file changes is the only reliable cure.
        """
        path = os.path.join(WEB_ROOT, "index.html")
        try:
            html = open(path, encoding="utf-8").read()
        except OSError:
            self.send_error(404)
            return

        stamp = build_stamp()
        for asset in ("style.css", "app.js", "wallpaper.js", "apple-touch-icon.png"):
            html = html.replace('"' + asset + '"', '"' + asset + "?v=" + stamp + '"')
        # Must land BEFORE the script tags: app.js reads it on load, and if the
        # element does not exist yet the page cannot tell which build it is.
        if "<body>" in html:
            html = html.replace(
                "<body>", '<body><span id="build" hidden>' + stamp + "</span>", 1)
        else:
            html = html.replace(
                "</head>", '</head><span id="build" hidden>' + stamp + "</span>", 1)

        body = html.encode("utf-8", "replace")
        self._own_cache_header = True
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path, cache="public, max-age=86400"):
        """Serve a file, honouring Range.

        iOS Safari will not play a <video> from a server that ignores Range
        requests - it issues one immediately and gives up if it gets a plain
        200 back. SimpleHTTPRequestHandler does not implement this.
        """
        if not path or not os.path.isfile(path):
            self.send_error(404)
            return

        self._own_cache_header = True
        size = os.path.getsize(path)
        ctype = _MEDIA_TYPES.get(os.path.splitext(path)[1].lower(),
                                 "application/octet-stream")
        start, end = 0, size - 1
        partial = False

        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                g1, g2 = m.group(1), m.group(2)
                if g1:
                    start = int(g1)
                    if g2:
                        end = min(int(g2), size - 1)
                else:                       # suffix form: bytes=-N
                    start = max(0, size - int(g2 or 0))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache)
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()

        if self.command == "HEAD":
            return
        remaining = length
        with open(path, "rb") as fh:
            fh.seek(start)
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return          # Safari closes early when it seeks; normal
                remaining -= len(chunk)

    # -- routing ----------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        """Receives layout-shift reports from the page, and layout edits.

        The display is an iPad that cannot be reached and cannot be filmed, and
        the shift does not reproduce in headless Chromium - so the page
        measures itself and posts what it saw. Written to diag.log beside the
        app.
        """
        route = self.path.split("?")[0]

        # Layout is a nested structure, so it arrives as a JSON body rather
        # than a query string. Same ownership as wallpaper/zoom: whoever posts
        # it wins, and every viewer follows on its next poll.
        if route == "/api/layout/select":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(length, 65536)).decode("utf-8", "replace")
                layout = json.loads(body)
            except (ValueError, OSError):
                layout = None
            self._json({"layout": set_layout(layout)})
            return

        if route != "/api/diag":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, 65536)).decode("utf-8", "replace")
        except (ValueError, OSError):
            body = ""
        try:
            with open(os.path.join(_HERE, "diag.log"), "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + body + chr(10))
        except OSError:
            pass
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._own_cache_header = False
        # Decode percent-escapes before routing: web wallpapers ship files with
        # spaces and non-ASCII names, which the browser sends encoded. Traversal
        # is not a concern here because safe_join() realpath-checks the result.
        route = urllib.parse.unquote(self.path.split("?")[0])

        if route in ("/", "/index.html"):
            self._send_index()
            return

        if route == "/api/stats":
            with _lock:
                snapshot = dict(_state)
            # Ride along on the 1 Hz poll the page already makes, so a change
            # made on the PC reaches the iPad within a second without the
            # viewer having to ask a second question.
            ui = get_ui()
            snapshot["wallpaper"] = ui["wallpaper"]
            snapshot["zoom"] = ui["zoom"]
            snapshot["layout"] = ui["layout"]
            # Lets a viewer notice it is running stale code and reload itself.
            # The iPad is mounted inside the PC case and cannot be touched, so
            # updates have to propagate without anyone reaching the device.
            snapshot["build"] = build_stamp()
            self._json(snapshot)
            return

        if route == "/api/wallpaper/select":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            choice = {
                "mode": (q.get("mode") or ["shader"])[0],
                "id": (q.get("id") or [""])[0],
                "title": (q.get("title") or [""])[0],
            }
            self._json(set_wallpaper(choice))
            return

        if route == "/api/disks":
            self._json({"drives": list_drives(), "current": get_disk_root()})
            return

        if route == "/api/disk/select":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json({"disk": set_disk_root((q.get("root") or [""])[0])})
            return

        if route == "/api/zoom/select":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json({"zoom": set_zoom((q.get("value") or ["1"])[0])})
            return

        if route == "/api/wallpapers":
            items = [
                {k: v for k, v in item.items() if k != "folder"}
                for item in wallpapers.scan()
            ]
            self._json({
                "items": items,
                "ffmpeg": bool(wallpapers.FFMPEG),
            })
            return

        parts = [p for p in route.split("/") if p]

        # /api/wallpaper/<id>/prepare
        if len(parts) == 4 and parts[:2] == ["api", "wallpaper"] and parts[3] == "prepare":
            item = wallpapers.by_id(parts[2])
            if not item or item["type"] != "video":
                self._json({"state": "error", "detail": "not a video wallpaper"}, 404)
                return
            _, status = wallpapers.ensure_video(item, CACHE_DIR)
            self._json(status)
            return

        # /media/<id>/video | /media/<id>/preview | /media/<id>/web/<path>
        if len(parts) >= 3 and parts[0] == "media":
            item = wallpapers.by_id(parts[1])
            if not item:
                self.send_error(404)
                return
            what = parts[2]

            if what == "video":
                path = wallpapers.cache_path(CACHE_DIR, item["id"])
                if not os.path.isfile(path):
                    self.send_error(409, "not prepared yet")
                    return
                self._send_file(path)
                return

            if what == "preview":
                if not item["preview"]:
                    self.send_error(404)
                    return
                self._send_file(wallpapers.safe_join(item["folder"], item["preview"]))
                return

            if what == "web":
                rel = "/".join(parts[3:]) or item["entry"]
                target = wallpapers.safe_join(item["folder"], rel)
                if target is None:
                    self.send_error(403)
                    return
                # The entry page gets the Wallpaper Engine API shim spliced in,
                # because these wallpapers call into an API that only exists
                # inside Wallpaper Engine and throw without it.
                if os.path.normcase(rel) == os.path.normcase(item["entry"]):
                    html = wallpapers.entry_html(item, target)
                    if html is not None:
                        body = html.encode("utf-8", "replace")
                        self._own_cache_header = True
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        if self.command != "HEAD":
                            self.wfile.write(body)
                        return
                self._send_file(target, cache="public, max-age=3600")
                return

            self.send_error(404)
            return

        super().do_GET()

    def end_headers(self):
        # Static dashboard files get edited live, so default them to no-cache.
        # Routes that set their own Cache-Control flag it and are left alone -
        # otherwise a 97 MB wallpaper would be re-downloaded on every load.
        if not getattr(self, "_own_cache_header", False):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # a line per second per client is just noise


class Server(ThreadingHTTPServer):
    # HTTPServer defaults allow_reuse_address on, which on Windows lets a
    # second process bind a port that is already being listened on - both
    # instances then accept connections and the dashboard sees alternating
    # stale readings. Refuse the duplicate instead of shadowing.
    allow_reuse_address = False
    daemon_threads = True


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    load_state()
    threading.Thread(target=poller, daemon=True).start()
    time.sleep(POLL_SECONDS * 1.2)  # let the first CPU-load delta establish

    with _lock:
        snap = dict(_state)

    cpu = snap.get("cpu") or {}
    gpu = snap.get("gpu")
    if cpu.get("temp_c") is not None:
        temp_status = "LibreHardwareMonitor OK"
    else:
        temp_status = "no LHM - CPU temp will read as --"

    print()
    print("  CPU   " + CPU_NAME)
    print("  temp  " + temp_status)
    print("  GPU   " + (gpu["name"] if gpu else "nvidia-smi unavailable"))
    print()
    print("  Open on the iPad:  http://" + local_ip() + ":" + str(PORT) + "/")
    print("  Ctrl+C to stop.")
    print()

    try:
        server = Server(("0.0.0.0", PORT), Handler)
    except OSError as exc:
        print("  Could not bind port " + str(PORT) + ": " + str(exc))
        print("  Another copy is probably already running.")
        print("  Set PCSTATS_PORT to use a different port.")
        return 1

    # Under pythonw there is no console and no taskbar button, so without the
    # tray icon the only way to stop this is Task Manager.
    use_tray = os.environ.get("PCSTATS_NO_TRAY", "") == ""
    if use_tray:
        try:
            import tray
        except ImportError as exc:
            log("tray module unavailable: " + str(exc))
            use_tray = False

    if not use_tray:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("stopped")
        return 0

    threading.Thread(target=server.serve_forever, daemon=True).start()
    lan = "http://" + local_ip() + ":" + str(PORT) + "/"
    try:
        # Owns the main thread: a tray icon needs a window message pump.
        def tray_wallpapers():
            items = [{"mode": "shader", "title": "Built-in nebula"}]
            for w in wallpapers.scan():
                if w["supported"]:
                    items.append({"mode": w["type"], "id": w["id"],
                                  "title": w["title"]})
            return items

        log("starting tray")
        tray.run("http://127.0.0.1:" + str(PORT) + "/", lan,
                 tooltip="CaseGauge - " + lan,
                 list_wallpapers=tray_wallpapers,
                 get_current=get_wallpaper,
                 set_wallpaper=set_wallpaper,
                 get_zoom=get_zoom,
                 set_zoom=set_zoom,
                 list_disks=list_drives,
                 get_disk=get_disk_root,
                 set_disk=set_disk_root,
                 get_layout=get_layout,
                 set_layout=set_layout,
                 layout_schema=layout_schema,
                 icon_path=os.path.join(ASSETS_DIR, "icon.ico"),
                 on_ready=lambda hwnd: log("tray icon created, hwnd=" + str(hwnd)))
    except Exception as exc:          # noqa: BLE001 - never lose the server
        log("tray failed: " + type(exc).__name__ + ": " + str(exc))
        print("tray unavailable (" + str(exc) + "); serving without it")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
