"""End-to-end (manual, moves your pointer ~10 s): the real engine dwell-clicks a window's caption buttons.

    python tests/e2e_caption_dwell.py

Opens a plain test window, walks the pointer onto its Minimize button and rests; then Close. Prints what happened.
Keep your hand off the mouse."""
import ctypes
import os
import subprocess
import sys
import tempfile
import time
import warnings
from ctypes import wintypes as w

warnings.simplefilter("ignore")
os.environ["FOCUSFLOW_HOME"] = tempfile.mkdtemp(prefix="ffe2e")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from focusflow import caption, winapi
from focusflow.config import Settings
from focusflow.engine import Engine
from focusflow.store import Store
from focusflow.targets import LocalTargets

winapi.set_dpi_aware()
u = ctypes.windll.user32
cfg = Settings(os.path.join(os.environ["FOCUSFLOW_HOME"], "s.json"))
cfg.set_many({"dwell_enabled": True, "dwell_ms": 800, "jitter_enabled": False, "attraction_strength": 0})
store = Store(os.path.join(os.environ["FOCUSFLOW_HOME"], "t.db"))
engine = Engine(cfg, store, LocalTargets())
engine.start()
engine.uia.wait_ready(15)
origin = winapi.get_cursor_pos()


def window():
    code = "import tkinter as t; r=t.Tk(); r.title('e2ecaption'); r.geometry('700x400+300+200'); r.after(40000, r.destroy); r.mainloop()"
    child = subprocess.Popen([sys.executable, "-c", code])
    for _ in range(60):
        hwnd = u.FindWindowW(None, "e2ecaption")
        if hwnd and u.IsWindowVisible(hwnd):
            u.SetWindowPos(hwnd, w.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # topmost: nothing covers the test
            u.SetForegroundWindow(hwnd)
            time.sleep(0.8)
            return child, hwnd
        time.sleep(0.1)
    raise SystemExit("window did not appear")


def button(hwnd, name):
    wr = w.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(wr))
    for x in range(wr.right - 260, wr.right, 3):
        t = caption.target_at(x, wr.top + 18)
        if t and t.name == name:
            return t
    raise SystemExit(f"{name} not found (window {tuple((wr.left, wr.top, wr.right, wr.bottom))}, "
                     f"window at its close position: {hex(caption.root_window_at(wr.right - 30, wr.top + 18) or 0)} vs {hex(hwnd)})")


def walk_to(x, y):
    sx, sy = winapi.get_cursor_pos()
    start = (x - 220, y + 160)
    winapi.set_cursor_pos(*start)
    time.sleep(0.3)
    for i in range(1, 21):                       # a deliberate approach, so the engine's movement gate opens
        winapi.set_cursor_pos(int(start[0] + (x - start[0]) * i / 20), int(start[1] + (y - start[1]) * i / 20))
        time.sleep(0.025)


def wait(cond, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if cond():
            return time.time() - t0
        time.sleep(0.05)
    return None


ok = True
child, hwnd = window()
t = button(hwnd, "Minimize button")
walk_to(*t.hit_point)
def trace():
    print("   engine view: target=%s progress=%.2f gate_open=%s settled_speed=%s cursor=%s over_button=%s" % (
        engine.status["target"], engine.dwell.progress, engine._gate_open, engine.status["speed"],
        winapi.get_cursor_pos(), t.rect))
for _ in range(3):
    time.sleep(0.5)
    trace()
took = wait(lambda: u.IsIconic(hwnd), 6)
print(f"dwell on Minimize  -> {'minimised after %.1f s' % took if took else 'NOTHING HAPPENED'} (dwell setting 0.8 s)")
ok &= took is not None
if took:
    u.ShowWindow(hwnd, 9)
    time.sleep(1.0)
t = button(hwnd, "Close button")
walk_to(*t.hit_point)
t0 = time.time()
took = wait(lambda: child.poll() is not None, 8)
print(f"dwell on Close     -> {'window closed after %.1f s' % took if took else 'NOTHING HAPPENED'} "
      f"(expected ~{0.8 * 1.5:.1f} s: Close waits 1.5x longer)")
ok &= took is not None and took >= 1.0
if child.poll() is None:
    child.kill()
engine.stop()
winapi.set_cursor_pos(*origin)
store.close()
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
