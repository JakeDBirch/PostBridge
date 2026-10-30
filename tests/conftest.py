"""Windows that tests and check scripts open go on the ASUS monitor.

The app is developed on the machine it is being used on, often with Pro Tools
open, and a test's window would otherwise land on top of that work.  So every
Tk window opens on the ASUS (found by its monitor ID, whatever its position),
and a position given to it is moved there too, so a zoomed window fills that
monitor.  On a machine without one, such as CI, nothing changes.

A check script outside pytest gets the same by calling send_windows_to_asus()
before it builds a window.  It does not stop a test taking keyboard focus: the
few that call focus_force need it to deliver key events.
"""
import ctypes
import gc
import re
import sys

import pytest

# PnP IDs: ASUS sells under both.
_ASUS_IDS = ("MONITOR\\ACI", "MONITOR\\AUS")
_POS_RE = re.compile(r"^(\d+x\d+)?[+-]-?\d+[+-]-?\d+$")
_INSET = 40                 # clear of the monitor's corner


def asus_work_area():
    """(left, top, right, bottom) of the ASUS's usable area, or None.  In this
    process's own coordinates, so ask after any DPI setting has been made."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes as W

    class _Info(ctypes.Structure):
        _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT),
                    ("dwFlags", W.DWORD), ("szDevice", W.WCHAR * 32)]

    class _Device(ctypes.Structure):
        _fields_ = [("cb", W.DWORD), ("DeviceName", W.WCHAR * 32),
                    ("DeviceString", W.WCHAR * 128), ("StateFlags", W.DWORD),
                    ("DeviceID", W.WCHAR * 128), ("DeviceKey", W.WCHAR * 128)]

    user32 = ctypes.windll.user32
    found = []

    def _each(handle, _dc, _rect, _data):
        info = _Info()
        info.cbSize = ctypes.sizeof(_Info)
        user32.GetMonitorInfoW(handle, ctypes.byref(info))
        dev = _Device()
        dev.cb = ctypes.sizeof(_Device)
        i = 0
        while user32.EnumDisplayDevicesW(info.szDevice, i, ctypes.byref(dev), 0):
            if dev.DeviceID.upper().startswith(_ASUS_IDS):
                r = info.rcWork
                found.append((r.left, r.top, r.right, r.bottom))
                break
            i += 1
        return True

    callback = ctypes.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC,
                                  ctypes.POINTER(W.RECT), W.LPARAM)
    user32.EnumDisplayMonitors(None, None, callback(_each), 0)
    return found[0] if found else None


def send_windows_to_asus():
    """From here on, every Tk window in this process opens on the ASUS."""
    import tkinter as tk
    if getattr(tk, "_pb_on_asus", False):
        return
    tk._pb_on_asus = True
    area = [False, None]      # asked once, on the first window

    def _origin():
        if not area[0]:
            area[0], area[1] = True, asus_work_area()
        return area[1]

    place = tk.Wm.wm_geometry

    def wm_geometry(self, newGeometry=None):
        a = _origin()
        if a and newGeometry and _POS_RE.match(newGeometry):
            size = _POS_RE.match(newGeometry).group(1) or ""
            newGeometry = "{}+{}+{}".format(size, a[0] + _INSET, a[1] + _INSET)
        return place(self, newGeometry)

    tk.Wm.wm_geometry = tk.Wm.geometry = wm_geometry

    def _opens_there(cls):
        init = cls.__init__

        def __init__(self, *args, **kw):
            init(self, *args, **kw)
            a = _origin()
            if a:
                place(self, "+{}+{}".format(a[0] + _INSET, a[1] + _INSET))
        cls.__init__ = __init__

    _opens_there(tk.Tk)
    _opens_there(tk.Toplevel)


def pytest_configure(config):
    # A test's dead Tk interpreter must be freed on the thread that made it, or
    # Tcl aborts the whole run.  Python's automatic collection runs on whichever
    # thread happens to allocate, often a worker the app started, so which test
    # dies shifts with any change to the suite.  Collect between tests instead
    # (below).  The app has one interpreter, alive until exit, so it can't hit this.
    gc.disable()
    try:
        send_windows_to_asus()
    except Exception:
        pass                  # no tkinter: nothing to place


@pytest.fixture(autouse=True)
def _collect_on_the_main_thread():
    yield
    gc.collect()
