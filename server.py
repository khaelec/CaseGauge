#!/usr/bin/env python3
"""
PC stats server for the iPad dashboard.

Serves web/ as static files and live sensor readings at /api/stats.
Standard library only - no pip installs, no build step.

Sensor sources:
  RAM       GlobalMemoryStatusEx (ctypes)          no admin
  CPU load  GetSystemTimes (ctypes)                no admin
  GPU       nvidia-smi and LibreHardwareMonitor    every card, NVIDIA first
  CPU temp  LibreHardwareMonitor's web server      needs LHM running elevated
  Fans      LibreHardwareMonitor                   board headers and GPU fans
  Board     LibreHardwareMonitor                   System / VRM / PCH / Socket
  Drives    GetDiskFreeSpaceEx + IOCTL + LHM       free space, temp, life
  Network   LibreHardwareMonitor                   the busiest adapter

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


# ---- which physical drive is behind a letter --------------------------------
#
# LHM names drives by model ("SPCC M.2 PCIe SSD") where Windows names them by
# letter, so its temperature and life readings cannot be attached to a drive
# chip without asking the volume what it sits on. IOCTL_STORAGE_QUERY_PROPERTY
# answers that, needs no admin, and opens the volume with dwDesiredAccess 0 -
# a query, not a read of the disk.

_IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
_VOLUME_PATH = "\\\\.\\%s:"


class _STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [("PropertyId", wintypes.DWORD),
                ("QueryType", wintypes.DWORD),
                ("AdditionalParameters", ctypes.c_byte * 1)]


# Default restypes would truncate a 64-bit HANDLE to int, which turns every
# call into ERROR_INVALID_NAME on a path that is perfectly valid.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                             wintypes.HANDLE]
_k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]

_INVALID_HANDLE = wintypes.HANDLE(-1).value


def drive_model(root):
    """The model string of the disk behind a drive letter, or None.

    Two letters on one physical disk return the same model, which is correct -
    they share its temperature.
    """
    letter = (root or "")[:1]
    if not letter.isalpha():
        return None                    # a network share has no volume to ask
    handle = _k32.CreateFileW(_VOLUME_PATH % letter, 0, 3, None, 3, 0, None)
    if not handle or handle == _INVALID_HANDLE:
        return None
    try:
        query = _STORAGE_PROPERTY_QUERY(0, 0, (ctypes.c_byte * 1)())
        buf = ctypes.create_string_buffer(1024)
        written = wintypes.DWORD()
        ok = _k32.DeviceIoControl(
            handle, _IOCTL_STORAGE_QUERY_PROPERTY,
            ctypes.byref(query), ctypes.sizeof(query),
            buf, ctypes.sizeof(buf), ctypes.byref(written), None)
        if not ok or written.value < 32:
            return None
        raw = buf.raw

        # STORAGE_DEVICE_DESCRIPTOR: the id fields are byte offsets into this
        # same buffer, pointing at NUL-terminated ASCII. 0 means "not reported".
        def text_at(offset):
            if not offset or offset >= len(raw):
                return ""
            end = raw.find(bytes(1), offset)      # the NUL that ends the string
            return raw[offset:end if end >= 0 else None].decode(
                "latin-1", "replace").strip()

        def u32(offset):
            return int.from_bytes(raw[offset:offset + 4], "little")

        name = (text_at(u32(12)) + " " + text_at(u32(16))).strip()
        return name or None
    except OSError:
        return None
    finally:
        _k32.CloseHandle(handle)


def _model_key(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def merge_drive_health(disks, lhm_drives):
    """Attach each drive's temperature and remaining life to its chip.

    Copies rather than annotating in place: read_all_disks() hands out its
    cache, and a drive that drops out of LHM must lose its temperature rather
    than keep showing the last one for a minute.
    """
    by_model = {}
    for drive in lhm_drives or ():
        by_model.setdefault(_model_key(drive.get("model")), drive)

    out = []
    for disk in disks or ():
        copy = dict(disk)
        health = by_model.get(_model_key(disk.get("model")))
        copy["temp_c"] = health.get("temp_c") if health else None
        copy["life"] = health.get("life") if health else None
        out.append(copy)
    return out


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
            info["model"] = None if drv["network"] else drive_model(drv["root"])
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


def read_gpus():
    """Every NVIDIA card nvidia-smi can see, in its bus order.

    One line of CSV per GPU, so an SLI pair or a second card for encoding both
    turn up here. Empty list when nvidia-smi is absent - that is the AMD and
    Intel case, and LibreHardwareMonitor covers it.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + _GPU_FIELDS,
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0 or not out.stdout.strip():
        return []

    gpus = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        used, total = _num(parts[3]), _num(parts[4])
        gpus.append({
            "name": parts[0],
            "temp_c": _num(parts[1]),
            "load": _num(parts[2]),
            "vram_used_gb": round(used / 1024, 2) if used is not None else None,
            "vram_total_gb": round(total / 1024, 2) if total is not None else None,
            "power_w": _num(parts[5]),
            "fan": _num(parts[6]),
            "fan_unit": "%",
            "fan_count": 1,
            "junction_c": None,
            "hotspot_c": None,
            "core_mhz": None,
            "mem_mhz": None,
            "source": "nvidia-smi",
        })
    return gpus


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

# ---- GPU from LibreHardwareMonitor: the AMD and Intel path ------------------
#
# Windows has no CLI for AMD or Intel GPU sensors (rocm-smi and amd-smi are
# Linux; Intel has none) and the vendor SDKs are C DLLs, which would break the
# standard-library rule. LHM covers all three vendors, so it is the source for
# anything nvidia-smi cannot report.
#
# LHM groups sensors by kind (Temperatures, Load, Powers, Fans, Controls, Data)
# and the label "GPU Core" appears under BOTH Temperatures and Load, so every
# lookup below is keyed on (group, label) - never on the label alone.


def _value_unit(text):
    """Split LHM's '2150.0 MB' into (2150.0, 'mb')."""
    if text is None:
        return None, ""
    m = re.match(r"\s*([-+]?\d+(?:[.,]\d+)?)\s*(\S*)\s*$", str(text))
    if not m:
        return None, ""
    return float(m.group(1).replace(",", ".")), m.group(2).lower()


def _lhm_num(text):
    return _value_unit(text)[0]


def _lhm_gb(text):
    val, unit = _value_unit(text)
    if val is None:
        return None
    if unit.startswith("kb"):
        return round(val / 1024 / 1024, 2)
    if unit.startswith("gb"):
        return round(val, 2)
    return round(val / 1024, 2)          # LHM reports GPU memory in MB


def _first(items, labels):
    for label in labels:
        if label in items:
            return items[label]
    return None


# The group names LHM files sensors under. Anything else is not a sensor group,
# which is how a device node is told apart from the plumbing above it.
_LHM_GROUPS = frozenset((
    "voltages", "temperatures", "fans", "controls", "load", "powers",
    "clocks", "data", "throughput", "levels", "factors", "timings",
))


def _device_groups(device):
    """{group_name: {label: raw_value}} for one LHM device node."""
    groups = {}
    for group in device.get("Children") or ():
        gname = (group.get("Text") or "").strip().lower()
        if gname not in _LHM_GROUPS:
            continue
        items = {}
        for sensor in group.get("Children") or ():
            label = (sensor.get("Text") or "").strip().lower()
            value = sensor.get("Value")
            if label and value not in (None, ""):
                items.setdefault(label, value)
        if items:
            groups[gname] = items
    return groups


def _lhm_devices(node, out=None):
    """Every (name, groups) pair in the tree, at any depth.

    A GPU sits two levels down - Computer / GPU / Temperatures - but the
    super-IO chip that owns the fan headers is three: Computer / Motherboard /
    Nuvoton NCT6687D / Fans. So this cannot be a fixed-depth walk. A node whose
    children are sensor groups is a device; anything else is passed through.
    """
    if out is None:
        out = []
    for child in node.get("Children") or ():
        groups = _device_groups(child)
        if groups:
            out.append(((child.get("Text") or "").strip(), groups))
        else:
            _lhm_devices(child, out)
    return out


def _looks_like_gpu(groups):
    return (("gpu core" in groups.get("temperatures", {})) or
            ("gpu memory total" in groups.get("data", {})))


# Names already dumped to the log. Per name, not a single flag: on a dual-GPU
# box the second card is the one we have not seen before.
_lhm_gpu_logged = set()


def _lhm_gpu_dict(name, groups):
    """Map LHM's sensor labels onto the shape nvidia-smi produces."""
    temps = groups.get("temperatures", {})
    loads = groups.get("load", {})
    powers = groups.get("powers", {})
    controls = groups.get("controls", {})
    fans = groups.get("fans", {})
    clocks = groups.get("clocks", {})
    data = groups.get("data", {})

    # A card can carry two or three fans - these two report 2 and 3 - and the
    # chip only ever showed the first. The summary is the hardest-working fan,
    # with the count beside it, because what matters at a glance is whether the
    # card is spinning up, not which individual fan is doing it.
    fan_pcts = [_lhm_num(v) for k, v in controls.items() if k.startswith("gpu fan")]
    fan_rpms = [_lhm_num(v) for k, v in fans.items() if k.startswith("gpu fan")]
    fan_pcts = [v for v in fan_pcts if v is not None]
    fan_rpms = [v for v in fan_rpms if v is not None]

    fan_count = len(fan_pcts) or len(fan_rpms)
    if fan_pcts:
        fan, fan_unit = max(fan_pcts), "%"
    elif fan_rpms:
        fan, fan_unit = max(fan_rpms), "RPM"
    else:
        fan, fan_unit = _first(controls, ("gpu fan", "fan")), "%"
        if fan is None:
            fan, fan_unit = _first(fans, ("gpu fan", "fan")), "RPM"
        fan = _lhm_num(fan)
        fan_count = 1 if fan is not None else 0

    if name not in _lhm_gpu_logged:
        # One-time dump so a user on a card we could not test can report the
        # exact labels LHM produced - vendor and driver naming varies.
        _lhm_gpu_logged.add(name)
        flat = []
        for gname in sorted(groups):
            for label in sorted(groups[gname]):
                flat.append(gname + "/" + label + "=" + str(groups[gname][label]))
        log("LHM GPU '" + name + "': " + "; ".join(flat))

    return {
        "name": name,
        "temp_c": _lhm_num(_first(temps, ("gpu core",))),
        "junction_c": _lhm_num(_first(temps, ("gpu memory junction",))),
        "hotspot_c": _lhm_num(_first(temps, ("gpu hot spot",))),
        "load": _lhm_num(_first(loads, ("gpu core",))),
        "vram_used_gb": _lhm_gb(_first(data, ("gpu memory used",))),
        "vram_total_gb": _lhm_gb(_first(data, ("gpu memory total",))),
        "power_w": _lhm_num(_first(powers, ("gpu package", "gpu power", "gpu core"))),
        "fan": fan,
        "fan_unit": fan_unit,
        "fan_count": fan_count,
        "core_mhz": _lhm_num(_first(clocks, ("gpu core",))),
        "mem_mhz": _lhm_num(_first(clocks, ("gpu memory",))),
        "source": "lhm",
    }


def _lhm_gpus_from_devices(devices):
    """Every GPU LHM can see, most VRAM first.

    Ordering by VRAM rather than by tree position keeps the discrete card ahead
    of a laptop's integrated one, which is what the old single-GPU code did and
    still the right default when there are several: the big card is the one the
    dashboard should lead with.
    """
    found = []
    for name, groups in devices:
        if _looks_like_gpu(groups):
            total = _lhm_gb(_first(groups.get("data", {}), ("gpu memory total",)))
            found.append((total if total is not None else 0.0,
                          name or "GPU", groups))
    # Stable, so two identical cards keep the order LHM listed them in.
    found.sort(key=lambda item: -item[0])
    return [_lhm_gpu_dict(name, groups) for _, name, groups in found]


# ---- fans, board temps, drives and the network ------------------------------
#
# All four come from devices nvidia-smi knows nothing about, so LHM is the only
# source. They are read from the same fetch as everything else: a second HTTP
# round trip per poll would cost more than every sensor here put together.


def _lhm_bps(text):
    """LHM's "5.8 KB/s" or "1158.1 MB/s" as bytes per second."""
    val, unit = _value_unit(text)
    if val is None:
        return None
    mult = {"b/s": 1, "kb/s": 1024, "mb/s": 1024 ** 2, "gb/s": 1024 ** 3}
    return val * mult.get(unit, 1024)


def _is_gpu_device(groups):
    return _looks_like_gpu(groups)


def _is_drive_device(groups):
    return ("life" in groups.get("levels", {}) or
            "composite temperature" in groups.get("temperatures", {}))


def _is_super_io(groups):
    """The chip that owns the case fan headers - a board's fans and its own
    temperatures, with none of the GPU's sensors."""
    return (("fans" in groups or "controls" in groups)
            and not _looks_like_gpu(groups))


def _is_cpu_device(groups):
    return ("cpu total" in groups.get("load", {}) or
            any(k.startswith("core (t") for k in groups.get("temperatures", {})))


# A fan header's label, shortened to fit a chip.
_FAN_LABELS = {"cpu fan": "CPU", "pump fan": "Pump", "fan": "Fan",
               "cpu optional fan": "CPU opt", "chipset fan": "Chipset"}


def _fan_label(label):
    if label in _FAN_LABELS:
        return _FAN_LABELS[label]
    if label.startswith("system fan"):
        return "Sys" + label[len("system fan"):].replace(" ", " ")
    return label.title()


def _lhm_fans(devices):
    """Case and cooler fans, the ones actually turning.

    A board exposes every header it has whether anything is plugged into it or
    not - this one reports eight and five of them read 0 - so a list of all of
    them is mostly noise on a card meant to be read from across a room. A fan
    at 0 RPM is dropped. The cost of that choice: a fan that FAILS disappears
    rather than showing 0, so this row says which fans are turning, not which
    fans exist.
    """
    out = []
    for name, groups in devices:
        if not _is_super_io(groups):
            continue
        fans = groups.get("fans", {})
        controls = groups.get("controls", {})
        for label in fans:
            rpm = _lhm_num(fans[label])
            if not rpm:                    # 0 RPM or unreadable: not a fan
                continue
            out.append({"name": _fan_label(label), "rpm": rpm,
                        "percent": _lhm_num(controls.get(label))})
    return out


# Board temperatures worth a chip, and what to call them. Curated because a
# super-IO also reports thresholds and dead headers, and "M2 #1 = 0.0" is not a
# temperature.
_BOARD_LABELS = {
    "system": "System", "vrm mos": "VRM", "vrm": "VRM", "pch": "PCH",
    "chipset": "Chipset", "cpu socket": "Socket", "motherboard": "Board",
}
_BOARD_SKIP = ("limit", "resolution", "critical", "warning", "average")


def _lhm_board(devices):
    """The board's own temperatures. Not the CPU's - that has a better source."""
    out = []
    for name, groups in devices:
        if not _is_super_io(groups):
            continue
        for label, raw in groups.get("temperatures", {}).items():
            if label == "cpu" or any(w in label for w in _BOARD_SKIP):
                continue
            temp = _lhm_num(raw)
            if not temp:                   # a header with nothing on it
                continue
            out.append({"name": _BOARD_LABELS.get(label, label.title()),
                        "temp_c": temp})
    # Known names first and in a stable order, so the chips do not reshuffle.
    order = list(_BOARD_LABELS.values())
    out.sort(key=lambda b: (order.index(b["name"]) if b["name"] in order
                            else len(order), b["name"]))
    return out[:4]


def _lhm_drives(devices):
    """Temperature and remaining life per physical drive, keyed by its model.

    LHM names drives by model where Windows names them by letter, so matching
    the two is left to read_all_disks(), which asks the volume itself.
    """
    out = []
    for name, groups in devices:
        if not _is_drive_device(groups):
            continue
        temps = groups.get("temperatures", {})
        out.append({
            "model": name,
            "temp_c": _lhm_num(_first(temps, ("composite temperature",
                                              "temperature"))),
            "life": _lhm_num(groups.get("levels", {}).get("life")),
        })
    return out


def _lhm_net(devices):
    """The busiest network adapter. One card, not a list: a machine has several
    adapters and only one of them is usually carrying anything."""
    best = None
    best_total = -1.0
    for name, groups in devices:
        through = groups.get("throughput", {})
        if "download speed" not in through:
            continue
        data = groups.get("data", {})
        down_gb = _lhm_gb(data.get("data downloaded"))
        up_gb = _lhm_gb(data.get("data uploaded"))
        total = (down_gb or 0.0) + (up_gb or 0.0)
        if total <= best_total:
            continue
        best_total = total
        best = {
            "name": name,
            "down_bps": _lhm_bps(through.get("download speed")),
            "up_bps": _lhm_bps(through.get("upload speed")),
            "down_gb": down_gb,
            "up_gb": up_gb,
            "util": _lhm_num(groups.get("load", {}).get("network utilization")),
        }
    return best


def _lhm_cpu_extras(devices):
    """The CPU's average clock and package power. Its temperature is read from
    the tree-wide scan instead, which already handles AMD and Intel naming."""
    for name, groups in devices:
        if not _is_cpu_device(groups):
            continue
        return {
            "clock_mhz": _lhm_num(_first(groups.get("clocks", {}),
                                         ("cores (average)", "core #1",
                                          "bus speed"))),
            "power_w": _lhm_num(_first(groups.get("powers", {}),
                                       ("package", "cpu package"))),
        }
    return {"clock_mhz": None, "power_w": None}


def read_lhm():
    """Everything worth having from LibreHardwareMonitor, in one fetch.

    Returns the CPU temperature, clock and package power, the GPU list, the
    fans that are turning, the board temperatures, per-drive health and the
    busiest network adapter. When LHM is absent this must fail fast, or one dead
    sensor drags the whole poll loop below its 1 Hz budget.
    """
    global _lhm_quiet_until

    empty = {"cpu_temp": None, "cpu_clock_mhz": None, "cpu_power_w": None,
             "gpu_junction": None, "gpus": [], "fans": [], "board": [],
             "drives": [], "net": None}

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

    # One walk of the tree, shared by every reader below it.
    devices = _lhm_devices(tree)
    cpu = _lhm_cpu_extras(devices)

    return {
        "cpu_temp": first_of(_TEMP_LABELS),
        "cpu_clock_mhz": cpu["clock_mhz"],
        "cpu_power_w": cpu["power_w"],
        "gpu_junction": first_of(_JUNCTION_LABELS),
        "gpus": _lhm_gpus_from_devices(devices),
        "fans": _lhm_fans(devices),
        "board": _lhm_board(devices),
        "drives": _lhm_drives(devices),
        "net": _lhm_net(devices),
    }


# ---- one list, one entry per physical card ----------------------------------


def _gpu_key(name):
    """Loose identity for a card, so nvidia-smi and LHM naming can be matched."""
    text = (name or "").lower()
    for noise in ("nvidia", "(r)", "(tm)"):
        text = text.replace(noise, " ")
    return re.sub(r"[^a-z0-9]+", "", text)


def merge_gpus(nvidia, lhm_gpus, junction_fallback=None):
    """Every GPU in the machine, biggest card first, nvidia-smi preferred.

    nvidia-smi needs no admin and no LHM, so it stays the source for NVIDIA
    cards; LHM is the only source for AMD and Intel, and the only source of the
    memory junction on any card. A card BOTH can see has to produce one entry,
    not two, so each LHM reading claims at most one nvidia-smi card of the same
    name - per entry, which is what stops two identical cards collapsing into
    one while still merging each of them.
    """
    merged = [dict(gpu) for gpu in nvidia]
    claimed = set()

    for extra in lhm_gpus:
        key = _gpu_key(extra.get("name"))
        match = None
        for i, gpu in enumerate(merged):
            if i not in claimed and _gpu_key(gpu.get("name")) == key:
                match = i
                break
        if match is None:
            merged.append(dict(extra))
            continue
        claimed.add(match)
        target = merged[match]
        for field in ("temp_c", "junction_c", "hotspot_c", "load",
                      "vram_used_gb", "vram_total_gb", "power_w",
                      "core_mhz", "mem_mhz"):
            if target.get(field) is None and extra.get(field) is not None:
                target[field] = extra[field]
        if extra.get("fan") is not None and (
                target.get("fan") is None
                or (extra.get("fan_count") or 1) > (target.get("fan_count") or 1)):
            # The unit and the count travel with the reading: LHM may only have
            # RPM where nvidia-smi reports a percentage, and nvidia-smi reports
            # one fan where LHM can see all three.
            target["fan"] = extra["fan"]
            target["fan_unit"] = extra.get("fan_unit") or "%"
            target["fan_count"] = extra.get("fan_count") or 1

    # Discrete ahead of integrated, so card 1 is the one worth looking at.
    # Stable, so two identical cards keep their bus order.
    merged.sort(key=lambda gpu: -(gpu.get("vram_total_gb") or 0.0))

    # The tree-wide junction scan cannot tell which card it read, so it may
    # only be trusted for the leading one - and only when nothing better came
    # from that card's own sensors.
    if merged and merged[0].get("junction_c") is None:
        merged[0]["junction_c"] = junction_fallback

    for i, gpu in enumerate(merged):
        gpu["slot"] = i
    return merged


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
    "cpu": ("temp", "load", "cores", "clocks"),
    "gpu": ("temp", "load", "vram", "chips", "clocks"),
    "ram": ("temp", "load", "free", "drives"),
    "cool": ("temp", "fans", "board"),
    "net": ("rate", "load", "totals"),
}
CARD_ORDER = ("cpu", "gpu", "ram", "cool", "net")
CARD_LABELS = {"cpu": "CPU", "gpu": "GPU", "ram": "Memory",
               "cool": "Cooling", "net": "Network"}
ROW_LABELS = {
    "cpu": {"temp": "Temperature", "load": "Load meter",
            "cores": "Per-core square", "clocks": "Clock / Package power"},
    "gpu": {"temp": "Temperature", "load": "Load meter", "vram": "VRAM meter",
            "chips": "Power / Fan / Junction",
            "clocks": "Core / Memory clock"},
    "ram": {"temp": "Temperature", "load": "In-use meter", "free": "Free GB",
            "drives": "Drive chips"},
    "cool": {"temp": "System temperature", "fans": "Fan speeds",
             "board": "Board temperature chips"},
    "net": {"rate": "Download speed", "load": "Utilisation meter",
            "totals": "Upload and session totals"},
}

# What a fresh install shows, card by card and row by row. Everything added
# after v1.3 is offered rather than imposed - the same call as the second GPU
# card, for the same reason: a dashboard read from across a room earns its
# space, and nobody asked for eight more numbers by default. ROW_IDS stays the
# full set, so the Layout menus list what is available and a tick brings it in.
DEFAULT_CARDS = ("cpu", "gpu", "ram")
DEFAULT_ROWS = {
    "cpu": ("temp", "load", "cores"),
    "gpu": ("temp", "load", "vram", "chips"),
    "ram": ("temp", "load", "free", "drives"),
    "cool": ("temp", "fans", "board"),
    "net": ("rate", "load", "totals"),
}


# A machine can hold more than one GPU - a discrete card plus the CPU's
# integrated one is the common case, two discrete cards the interesting one.
# Each gets a card of its own with an id of its own, so the layout can hide and
# reorder them independently. The first keeps the bare "gpu" id, so a state.json
# written by an older version still means exactly what it meant.
MAX_GPU_CARDS = 4
GPU_DROP_GRACE = 90.0    # seconds before believing a GPU has gone away
_GPU_ID = re.compile(r"^gpu([2-9])?$")
_gpu_cards = 1           # how many GPU cards exist; set from what we detect


def card_kind(cid):
    """Which rows and labels a card id uses. Every GPU card shares the GPU's."""
    return "gpu" if _GPU_ID.match(cid or "") else cid


def gpu_slot(cid):
    """1 for "gpu", 2 for "gpu2"... 0 for anything that is not a GPU card."""
    m = _GPU_ID.match(cid or "")
    return int(m.group(1) or 1) if m else 0


def gpu_card_ids():
    return ["gpu"] + ["gpu" + str(n) for n in range(2, _gpu_cards + 1)]


def card_ids():
    """Every card id that currently exists, in default order."""
    out = []
    for cid in CARD_ORDER:
        out.extend(gpu_card_ids() if cid == "gpu" else [cid])
    return out


def default_card_ids():
    """The cards a fresh install shows: CPU, one GPU, Memory.

    A second GPU is usually an onboard chip nobody wants a card for, and
    Cooling and Network are extra by definition, so all of them are offered
    rather than imposed - they exist in card_ids(), so the Layout menus list
    them and a tick brings them in.
    """
    return [cid for cid in card_ids()
            if gpu_slot(cid) < 2 and card_kind(cid) in DEFAULT_CARDS]


def card_label(cid):
    """Just "GPU" on its own, but "GPU 1" and "GPU 2" once there are two.

    This is the menu label: the Layout menus have to tell the cards apart even
    when only one of them is shown. The heading ON the card is the page's call,
    and it only numbers itself once two cards are actually on screen.
    """
    kind = card_kind(cid)
    if kind == "gpu" and _gpu_cards > 1:
        return "GPU " + str(gpu_slot(cid))
    return CARD_LABELS[kind]


def layout_schema():
    """Labels and ids for the tray menu, so they live in one place."""
    return [
        {"id": cid, "label": card_label(cid),
         "rows": [{"id": r, "label": ROW_LABELS[card_kind(cid)][r]}
                  for r in ROW_IDS[card_kind(cid)]],
         "defaults": list(DEFAULT_ROWS[card_kind(cid)])}
        for cid in card_ids()
    ]


def default_layout():
    return [{"id": cid, "rows": list(DEFAULT_ROWS[card_kind(cid)])}
            for cid in default_card_ids()]


_ui = {"wallpaper": {"mode": "shader"}, "zoom": 1.0, "disk": "", "sources": [],
       "layout": default_layout()}
_ui_lock = threading.Lock()


def _clean_layout(raw):
    """Accept only known cards/rows, in a canonical row order. None if unusable."""
    if not isinstance(raw, list):
        return None
    known = card_ids()
    seen = set()
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id")
        if cid not in known or cid in seen:
            continue
        seen.add(cid)
        all_rows = ROW_IDS[card_kind(cid)]
        rows = entry.get("rows")
        if not isinstance(rows, list):
            rows = list(all_rows)
        # Canonical order, unknown ids dropped: the page can rely on this.
        out.append({"id": cid, "rows": [r for r in all_rows if r in rows]})
    return out


def _save_locked():
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_ui, fh)
    except OSError:
        pass              # a read-only disk must not break the switch


def load_state():
    global _ui, _gpu_cards
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
    _ui["sources"] = [p for p in (saved.get("sources") or []) if isinstance(p, str)]
    wallpapers.set_sources(_ui["sources"])

    wp = saved.get("wallpaper")
    if isinstance(wp, dict) and wp.get("mode"):
        _ui["wallpaper"] = wp
    try:
        _ui["zoom"] = max(ZOOM_MIN, min(ZOOM_MAX, float(saved.get("zoom", 1.0))))
    except (TypeError, ValueError):
        pass
    if isinstance(saved.get("disk"), str):
        _ui["disk"] = saved["disk"]

    # Take the saved layout's word for how many GPU cards there are, or its
    # entries for the second card would be dropped as unknown before the first
    # reading has come in. If that GPU is genuinely gone, sync_gpu_cards()
    # removes the card once it is sure.
    for entry in saved.get("layout") or []:
        if isinstance(entry, dict):
            _gpu_cards = max(_gpu_cards, min(gpu_slot(entry.get("id")),
                                             MAX_GPU_CARDS))
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


_gpu_gone_since = 0.0


def sync_gpu_cards(count):
    """Match the GPU cards on OFFER to the GPUs actually found.

    Called from the poll loop, so a GPU that turns up - or a driver that finally
    reports one - can be shown without a restart. It only makes the card
    available: nothing is added to the layout, because a second GPU is often an
    onboard chip nobody wants a card for. Ticking "GPU 2" in the Layout menu is
    what puts it on screen.

    A GPU that DISAPPEARS is only believed after a grace period, and then its
    card is dropped from the layout: LibreHardwareMonitor is often still
    starting when we take our first reading, and a GPU only it can see would
    otherwise flicker in and out in the first few seconds.
    """
    global _gpu_cards, _gpu_gone_since

    count = max(1, min(int(count or 1), MAX_GPU_CARDS))
    now = time.monotonic()

    if count < _gpu_cards:
        if _gpu_gone_since == 0.0:
            _gpu_gone_since = now
        if now - _gpu_gone_since < GPU_DROP_GRACE:
            return
    _gpu_gone_since = 0.0
    if count == _gpu_cards:
        return

    with _ui_lock:
        _gpu_cards = count
        wanted = gpu_card_ids()
        kept = [c for c in _ui["layout"]
                if card_kind(c["id"]) != "gpu" or c["id"] in wanted]
        if len(kept) == len(_ui["layout"]):
            return                # cards gained, nothing to drop: offer only
        _ui["layout"] = kept
        _save_locked()


def get_wallpaper():
    with _ui_lock:
        return dict(_ui["wallpaper"])


def set_wallpaper(choice):
    clean = {"mode": choice.get("mode") or "shader"}
    if clean["mode"] in ("video", "web", "image"):
        clean["id"] = str(choice.get("id") or "")
        clean["title"] = choice.get("title") or ""
        if not clean["id"]:
            clean = {"mode": "shader"}
    with _ui_lock:
        _ui["wallpaper"] = clean
        _save_locked()
    return dict(clean)


def get_sources():
    with _ui_lock:
        return list(_ui.get("sources") or [])


def add_source(path):
    """Point the app at another folder of wallpapers.

    Rejected rather than stored if it is not a readable directory: a typo
    saved into state.json would otherwise be a silent no-op every scan.
    """
    path = (path or "").strip().strip('"')
    if not path:
        return {"ok": False, "detail": "no folder given", "sources": get_sources()}
    path = os.path.normpath(os.path.expandvars(os.path.expanduser(path)))
    if not os.path.isdir(path):
        return {"ok": False, "detail": "not a folder: " + path,
                "sources": get_sources()}
    try:
        os.listdir(path)
    except OSError as exc:
        return {"ok": False, "detail": "cannot read it: %s" % exc,
                "sources": get_sources()}

    with _ui_lock:
        current = list(_ui.get("sources") or [])
        if not any(os.path.normcase(p) == os.path.normcase(path) for p in current):
            current.append(path)
            _ui["sources"] = current
            _save_locked()
        wallpapers.set_sources(current)
        out = list(current)
    return {"ok": True, "detail": "", "sources": out}


def remove_source(path):
    with _ui_lock:
        current = [p for p in (_ui.get("sources") or [])
                   if os.path.normcase(p) != os.path.normcase(path or "")]
        _ui["sources"] = current
        _save_locked()
        wallpapers.set_sources(current)
        out = list(current)
    return {"ok": True, "detail": "", "sources": out}


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
    gpus = merge_gpus(read_gpus(), lhm.get("gpus") or [], lhm.get("gpu_junction"))
    sync_gpu_cards(len(gpus))
    return {
        "ok": True,
        "time": time.time(),
        "cpu": {
            "name": CPU_NAME,
            "load": read_cpu_load(),
            "cores": read_cpu_cores(),
            "temp_c": cpu_temp,
            "clock_mhz": lhm.get("cpu_clock_mhz"),
            "power_w": lhm.get("cpu_power_w"),
            "threads": os.cpu_count(),
        },
        # "gpu" is the leading card, kept so an older cached page still paints
        # something; "gpus" is the real answer and what the dashboard reads.
        "gpu": gpus[0] if gpus else None,
        "gpus": gpus,
        "ram": read_ram(),
        "disk": read_disk(),
        "disks": merge_drive_health(read_all_disks(), lhm.get("drives")),
        # Fans, board temperatures and the network only exist while LHM is up.
        # Absent it they are empty, and their cards say so rather than lying.
        "fans": lhm.get("fans") or [],
        "board": lhm.get("board") or [],
        "net": lhm.get("net"),
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
                    self.send_header("Content-Length", "0")
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
                # So the picker can tell the user where to drop their own.
                "local_dir": wallpapers.local_dir(),
                "sources": get_sources(),
            })
            return

        if route == "/api/wallpaper/source/add":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(add_source((q.get("path") or [""])[0]))
            return

        if route == "/api/wallpaper/source/remove":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(remove_source((q.get("path") or [""])[0]))
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

            if what == "image":
                if item["type"] != "image":
                    self.send_error(404)
                    return
                target = wallpapers.safe_join(item["folder"], item["entry"])
                if target is None:
                    self.send_error(403)
                    return
                self._send_file(target)
                return

            if what == "preview":
                if item["preview"]:
                    self._send_file(
                        wallpapers.safe_join(item["folder"], item["preview"]))
                    return
                # No artwork beside it: pull a frame out of the video itself.
                poster = wallpapers.ensure_poster(item, CACHE_DIR)
                if not poster:
                    self.send_error(404)
                    return
                self._send_file(poster)
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
    # socketserver's default accept backlog is 5, which is too small here. Every
    # request is its own connection (HTTP/1.0, Connection: close), so three
    # viewers polling at 1 Hz plus a page load - index, css, two scripts, the
    # poster images, a video with Range requests - arrives in bursts well past
    # five. An overflowed backlog DROPS the SYN, the client waits out a retransmit
    # timeout, and the dashboard reports it as losing the server. Seen as
    # SYN_RECEIVED piling up against this port with a third device connected.
    request_queue_size = 128

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
    gpus = snap.get("gpus") or []
    if cpu.get("temp_c") is not None:
        temp_status = "LibreHardwareMonitor OK"
    else:
        temp_status = "no LHM - CPU temp will read as --"

    print()
    print("  CPU   " + CPU_NAME)
    print("  temp  " + temp_status)
    if gpus:
        for i, gpu in enumerate(gpus):
            label = "GPU " + str(i + 1) if len(gpus) > 1 else "GPU  "
            print("  " + label + " " + gpu["name"] +
                  "  (" + (gpu.get("source") or "lhm") + ")")
    else:
        print("  GPU   no GPU found "
              "(needs nvidia-smi or LibreHardwareMonitor)")
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

    # Created empty on first run. A folder that is not there is one the user
    # cannot find, and the picker prints this path as the place to drop files.
    try:
        os.makedirs(wallpapers.local_dir(), exist_ok=True)
    except OSError:
        pass

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
