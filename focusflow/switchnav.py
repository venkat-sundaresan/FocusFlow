"""Switch-based target navigation: step through the interactive controls of the window in front and activate one
with a single press, for people who can use one or two switches (any device that can press a hotkey).

Two global hotkeys (RegisterHotKey, so no keystroke is ever observed): `switch_next` moves the highlight,
`switch_select` activates it. With auto-scan on, the highlight advances by itself and only `switch_select`
is needed. Candidates come from Windows UI Automation (FocusFlow's own windows publish theirs directly)."""
import threading
import time

from . import winapi

STALE_S = 10.0


class SwitchNav:
    def __init__(self, cfg, engine, local, on_change=None):
        self.cfg, self.engine, self.local = cfg, engine, local
        self.on_change = on_change
        self.targets = []
        self.index = -1
        self._scanned_at = 0.0
        self._hwnd = None
        self._lock = threading.RLock()
        self._auto = None
        self._auto_stop = threading.Event()

    # -- state read by the overlay ----------------------------------------------------------------
    def current(self):
        with self._lock:
            if 0 <= self.index < len(self.targets) and time.time() - self._scanned_at < STALE_S * 3:
                return self.targets[self.index]
        return None

    # -- scanning -----------------------------------------------------------------------------------
    def _own_window_in_front(self):
        return winapi.foreground_exe() in ("python.exe", "pythonw.exe") and bool(self.local.all())

    def rescan(self):
        hwnd = winapi.foreground_hwnd()
        if self._own_window_in_front():
            found = sorted(self.local.all(), key=lambda t: (t.rect[1] // 28, t.rect[0]))
        elif self.engine.uia.ok and hwnd:
            try:
                found = self.engine.uia.submit(lambda s: s.scan_window(hwnd)).result(2.0)
            except Exception:
                found = []
        else:
            found = []
        with self._lock:
            self.targets, self._scanned_at, self._hwnd, self.index = found, time.time(), hwnd, -1
        return len(found)

    def _fresh(self):
        return (self.targets and time.time() - self._scanned_at < STALE_S
                and winapi.foreground_hwnd() == self._hwnd)

    # -- the two switch actions ---------------------------------------------------------------------
    def next(self):
        if not self.cfg["switch_enabled"]:
            return None
        if not self._fresh():
            self.rescan()
        with self._lock:
            if not self.targets:
                return None
            self.index = (self.index + 1) % len(self.targets)
            self._scanned_at = time.time()          # keep the list alive while the user is stepping
            t = self.targets[self.index]
        if self.on_change:
            self.on_change(t)
        return t

    def select(self):
        if not self.cfg["switch_enabled"]:
            return False
        t = self.current()
        if t is None:
            return False
        if t.action is not None:
            self.local.run(t)
        else:
            winapi.send_click(*(t.hit_point or t.center))
        with self._lock:
            self.targets, self.index = [], -1       # the screen has probably changed
        self.engine.store.add_event("click_assisted", "desktop", (t.name or t.ctype or "control")[:40],
                                    {"mode": "assisted", "method": "switch", "path_px": 0, "reversals": 0,
                                     "duration_ms": 0})
        if self.on_change:
            self.on_change(None)
        return True

    # -- auto-scan --------------------------------------------------------------------------------------
    def start_auto(self):
        self.stop_auto()
        self._auto_stop = threading.Event()
        stop = self._auto_stop

        def loop():
            while not stop.wait(max(0.4, self.cfg["switch_scan_ms"] / 1000.0)):
                if self.cfg["switch_enabled"] and self.cfg["switch_auto_scan"] and self.engine.active:
                    try:
                        self.next()
                    except Exception:
                        pass

        self._auto = threading.Thread(target=loop, daemon=True, name="ff-switch-scan")
        self._auto.start()

    def stop_auto(self):
        self._auto_stop.set()
