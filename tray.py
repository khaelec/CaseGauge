#!/usr/bin/env python3
"""
A system tray icon for the dashboard server - ctypes only, no pip packages.

The server runs under pythonw so it has no console and no taskbar button,
which otherwise leaves no way to see that it is running or to stop it short
of Task Manager. This puts it in the notification area instead.

  double-click   open the dashboard
  right-click    Open dashboard / Copy iPad URL / Quit

run() owns the thread it is called on, because a tray icon needs a window
message pump. Call it from the main thread and run the HTTP server in a
background thread.
"""

import ctypes
import os
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_TRAY = 0x0400 + 1          # WM_APP + 1

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0000, 0x0800, 0x0001
MF_POPUP, MF_CHECKED = 0x0010, 0x0008
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100

ID_WALLPAPER_BASE = 2000      # one id per wallpaper, assigned when shown
ID_ZOOM_BASE = 3000           # one id per zoom step
ID_DISK_BASE = 4000           # one id per drive
ID_LAYOUT_BASE = 5000         # layout toggles and moves
ZOOM_STEPS = (0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

ID_OPEN, ID_COPY, ID_QUIT = 1001, 1002, 1003

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# Every one of these needs explicit argtypes. Without them ctypes guesses C
# int for Python ints, and Windows routinely passes pointer-sized wparam and
# lparam values - which then raise "OverflowError: int too long to convert"
# from inside the window procedure, where the exception is swallowed.
user32.CreateWindowExW.restype = wintypes.HWND
user32.LoadIconW.restype = wintypes.HICON
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                              wintypes.UINT, ctypes.c_int, ctypes.c_int,
                              wintypes.UINT]
user32.CreatePopupMenu.restype = wintypes.HMENU

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT

user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = LRESULT

user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL

user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int

user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                               ctypes.c_void_p, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL

user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int

user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


def _base_dir():
    """The exe's folder. Frozen, __file__ points into PyInstaller's temp
    extraction dir, where assets/ is not."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _icon(icon_path=None):
    """CaseGauge's own icon, falling back to the generic Windows one.

    This used to extract icon 15 from shell32.dll - Microsoft's asset, which is
    not licensed for redistribution. The icon is either beside the exe in
    assets/ (shipped or development) or inside the single-file exe, and the
    server passes whichever resolved.
    """
    try:
        path = icon_path or os.path.join(_base_dir(), "assets", "icon.ico")
        if os.path.isfile(path):
            h = user32.LoadImageW(None, path, IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if h:
                return h
    except Exception:
        pass
    return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))


def _copy(text):
    """Put text on the clipboard. Failure here must never kill the tray."""
    try:
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            buf = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buf)
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            lock = kernel32.GlobalLock(handle)
            ctypes.memmove(lock, buf, size)
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(CF_UNICODETEXT, handle)
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


def run(url, lan_url, on_quit=None, tooltip="CaseGauge",
        list_wallpapers=None, get_current=None, set_wallpaper=None,
        get_zoom=None, set_zoom=None, on_ready=None,
        list_disks=None, get_disk=None, set_disk=None,
        get_layout=None, set_layout=None, layout_schema=None,
        icon_path=None):
    """Show the tray icon and pump messages until Quit. Blocks.

    The wallpaper callbacks are what let this PC decide what the iPad shows,
    without anyone touching the iPad. Layout follows the same pattern: the PC
    is the only place the layout can be edited, because the iPad is mounted
    inside the case and cannot be touched.
    """
    import webbrowser

    hinst = kernel32.GetModuleHandleW(None)
    class_name = "CaseGaugeTray"
    wp_by_id = {}
    zoom_by_id = {}
    disk_by_id = {}
    layout_by_id = {}

    def _edit_layout(action):
        """Apply one menu action to the current layout and save it."""
        layout = [{"id": c.get("id"), "rows": list(c.get("rows") or [])}
                  for c in (get_layout() or [])]
        kind = action[0]
        if kind == "toggle_card":
            cid = action[1]
            ids = [c["id"] for c in layout]
            if cid in ids:
                if len(layout) <= 1:
                    return          # never hide every card
                layout = [c for c in layout if c["id"] != cid]
            else:
                rows = []
                for card in (layout_schema or []):
                    if card.get("id") == cid:
                        rows = [r["id"] for r in card.get("rows") or []]
                layout.append({"id": cid, "rows": rows})
        elif kind == "toggle_row":
            cid, row = action[1], action[2]
            for c in layout:
                if c["id"] == cid:
                    if row in c["rows"]:
                        c["rows"].remove(row)
                    else:
                        c["rows"].append(row)
        elif kind == "move":
            cid, delta = action[1], action[2]
            ids = [c["id"] for c in layout]
            if cid in ids:
                i = ids.index(cid)
                j = i + delta
                if 0 <= j < len(layout):
                    layout[i], layout[j] = layout[j], layout[i]
        set_layout(layout)

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            low = lparam & 0xFFFF
            if low == WM_LBUTTONDBLCLK:
                webbrowser.open(url)
            elif low == WM_RBUTTONUP:
                _menu(hwnd)
            return 0
        if msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == ID_OPEN:
                webbrowser.open(url)
            elif cmd == ID_COPY:
                _copy(lan_url)
            elif cmd == ID_QUIT:
                user32.DestroyWindow(hwnd)
            elif cmd in wp_by_id and set_wallpaper:
                try:
                    set_wallpaper(wp_by_id[cmd])
                except Exception:
                    pass      # a bad pick must never take the tray down
            elif cmd in zoom_by_id and set_zoom:
                try:
                    set_zoom(zoom_by_id[cmd])
                except Exception:
                    pass
            elif cmd in disk_by_id and set_disk:
                try:
                    set_disk(disk_by_id[cmd])
                except Exception:
                    pass
            elif cmd in layout_by_id and set_layout:
                try:
                    _edit_layout(layout_by_id[cmd])
                except Exception:
                    pass
            return 0
        if msg in (WM_CLOSE, WM_DESTROY):
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(hwnd):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, lan_url)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "Open dashboard")
        user32.AppendMenuW(menu, MF_STRING, ID_COPY, "Copy iPad URL")

        submenu = None
        wp_by_id.clear()
        if list_wallpapers:
            try:
                items = list_wallpapers()
                current = get_current() if get_current else {}
            except Exception:
                items, current = [], {}

            if items:
                submenu = user32.CreatePopupMenu()
                for offset, item in enumerate(items):
                    ident = ID_WALLPAPER_BASE + offset
                    wp_by_id[ident] = item
                    flags = MF_STRING
                    same = (item.get("mode") == current.get("mode") and
                            (item.get("mode") == "shader" or
                             str(item.get("id")) == str(current.get("id"))))
                    if same:
                        flags |= MF_CHECKED
                    label = item.get("title") or item.get("id") or "?"
                    user32.AppendMenuW(submenu, flags, ident, label[:60])
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                user32.AppendMenuW(menu, MF_STRING | MF_POPUP, submenu, "Wallpaper")

        disk_by_id.clear()
        if list_disks and set_disk:
            try:
                drives = list_disks()
                current = get_disk() if get_disk else ""
            except Exception:
                drives, current = [], ""
            if drives:
                dmenu = user32.CreatePopupMenu()
                for offset, drv in enumerate(drives):
                    ident = ID_DISK_BASE + offset
                    disk_by_id[ident] = drv.get("root")
                    flags = MF_STRING
                    if drv.get("root") == current:
                        flags |= MF_CHECKED
                    label = drv.get("drive", "?")
                    if drv.get("network"):
                        label += "  (network)"
                    user32.AppendMenuW(dmenu, flags, ident, label)
                user32.AppendMenuW(menu, MF_STRING | MF_POPUP, dmenu, "Disk")

        zoom_by_id.clear()
        if set_zoom:
            try:
                now = get_zoom() if get_zoom else 1.0
            except Exception:
                now = 1.0
            zmenu = user32.CreatePopupMenu()
            for offset, step in enumerate(ZOOM_STEPS):
                ident = ID_ZOOM_BASE + offset
                zoom_by_id[ident] = step
                flags = MF_STRING
                if abs(step - now) < 0.005:
                    flags |= MF_CHECKED
                user32.AppendMenuW(zmenu, flags, ident, "%d%%" % round(step * 100))
            user32.AppendMenuW(menu, MF_STRING | MF_POPUP, zmenu, "Zoom")

        layout_by_id.clear()
        if layout_schema and get_layout and set_layout:
            try:
                current = get_layout() or []
            except Exception:
                current = []
            present = {}
            for c in current:
                if isinstance(c, dict):
                    present[c.get("id")] = c

            # Nested popups: a failure here must not take the whole menu (and
            # therefore the tray) down, so it is guarded as a unit.
            try:
                lmenu = user32.CreatePopupMenu()
                ident = ID_LAYOUT_BASE
                for card in layout_schema:
                    cid = card.get("id")
                    cmenu = user32.CreatePopupMenu()

                    shown = cid in present
                    ident += 1
                    layout_by_id[ident] = ("toggle_card", cid)
                    user32.AppendMenuW(
                        cmenu, MF_STRING | (MF_CHECKED if shown else 0), ident,
                        "Show this card")

                    user32.AppendMenuW(cmenu, MF_SEPARATOR, 0, None)
                    rows_on = set((present.get(cid) or {}).get("rows") or [])
                    for row in card.get("rows") or []:
                        ident += 1
                        layout_by_id[ident] = ("toggle_row", cid, row.get("id"))
                        flags = MF_STRING
                        if row.get("id") in rows_on:
                            flags |= MF_CHECKED
                        user32.AppendMenuW(cmenu, flags, ident,
                                           row.get("label") or "?")

                    user32.AppendMenuW(cmenu, MF_SEPARATOR, 0, None)
                    ident += 1
                    layout_by_id[ident] = ("move", cid, -1)
                    user32.AppendMenuW(cmenu, MF_STRING, ident, "Move up")
                    ident += 1
                    layout_by_id[ident] = ("move", cid, 1)
                    user32.AppendMenuW(cmenu, MF_STRING, ident, "Move down")

                    user32.AppendMenuW(lmenu, MF_STRING | MF_POPUP, cmenu,
                                       card.get("label") or "?")
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                user32.AppendMenuW(menu, MF_STRING | MF_POPUP, lmenu, "Layout")
            except Exception:
                layout_by_id.clear()

        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit")

        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # Required, or the menu refuses to close when you click elsewhere.
        user32.SetForegroundWindow(hwnd)
        chosen = user32.TrackPopupMenu(
            menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)
        if chosen:
            user32.SendMessageW(hwnd, WM_COMMAND, chosen, 0)

    proc = WNDPROC(wndproc)          # must outlive the window
    cls = WNDCLASS()
    cls.lpfnWndProc = proc
    cls.hInstance = hinst
    cls.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(cls)):
        if ctypes.get_last_error() not in (1410,):   # already registered
            raise ctypes.WinError(ctypes.get_last_error())

    hwnd = user32.CreateWindowExW(0, class_name, class_name, 0,
                                  0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())

    nid = NOTIFYICONDATA()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = _icon(icon_path)
    nid.szTip = tooltip[:127]

    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        raise ctypes.WinError(ctypes.get_last_error())

    if on_ready:
        try:
            on_ready(hwnd)
        except Exception:
            pass

    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))

    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    if on_quit:
        on_quit()


if __name__ == "__main__":
    run("http://127.0.0.1:8777/", "http://127.0.0.1:8777/",
        on_quit=lambda: sys.stdout.write("quit\n"))
