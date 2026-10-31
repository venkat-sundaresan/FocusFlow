"""LIVE test: the real low-level mouse hook, the real cursor, real relative SendInput moves.

It moves your pointer for a few seconds, so it only runs when asked:   set FOCUSFLOW_LIVE_TESTS=1

What it proves on this machine: the hook delivers absolute positions for relative input, a swallowed move can be
replaced with SetCursorPos without the hook seeing an echo, the filtered cursor really is steadier than the raw
input, intentional movement still arrives, and turning assistance off restores a 1:1 pointer.
What it cannot prove: hardware-specific behaviour (high-polling-rate mice, touchpads, tablets).
"""
import ctypes
from ctypes import wintypes
import math
import os
import sys
import tempfile
import threading
import time
import unittest

LIVE = os.environ.get("FOCUSFLOW_LIVE_TESTS") == "1"
if LIVE:
    os.environ["FOCUSFLOW_TEST_ACCEPT_INJECTED"] = "1"      # must be set before winapi is imported

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@unittest.skipUnless(LIVE and sys.platform == "win32", "set FOCUSFLOW_LIVE_TESTS=1 (moves the real pointer)")
class LiveHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from focusflow import winapi
        from focusflow.config import Settings
        from focusflow.engine import Engine
        from focusflow.store import Store
        from focusflow.targets import LocalTargets
        cls.winapi = winapi
        winapi.set_dpi_aware()
        cls.dir = tempfile.TemporaryDirectory()
        cls.cfg = Settings(os.path.join(cls.dir.name, "s.json"))
        cls.store = Store(os.path.join(cls.dir.name, "t.db"))
        cls.engine = Engine(cls.cfg, cls.store, LocalTargets())
        cls.engine.pointer_thread.start()
        cls.hook = winapi.MouseHook(cls.engine.on_mouse)
        cls.hook.start()
        cls.origin = winapi.get_cursor_pos()
        time.sleep(0.4)

    @classmethod
    def tearDownClass(cls):
        cls.hook.stop()
        cls.engine.stop()
        cls.winapi.set_cursor_pos(*cls.origin)
        time.sleep(0.1)
        cls.store.close()
        cls.dir.cleanup()

    # -- helpers ---------------------------------------------------------------------------------------
    def move_rel(self, dx, dy):
        w = self.winapi
        seq = (w._INPUT * 1)()
        seq[0].type = 0
        seq[0].mi.dx, seq[0].mi.dy, seq[0].mi.dwFlags = int(dx), int(dy), 0x0001
        w.user32.SendInput(1, seq, ctypes.sizeof(w._INPUT))

    def park(self, x=500, y=400):
        self.winapi.set_cursor_pos(x, y)
        time.sleep(0.3)                  # > the 150 ms input-gap reset: the next move re-syncs the pipeline

    def configure(self, strength, jitter=True):
        self.cfg.set_many({"enabled": True, "jitter_enabled": jitter, "smoothing_strength": strength,
                           "attraction_strength": 0})
        self.engine.apply_features()

    def sample_cursor(self, stop):
        out = []
        while not stop.is_set():
            out.append(self.winapi.get_cursor_pos())
            time.sleep(0.001)
        return out

    def drive_tremor(self, seconds=1.0, amp=2.0, hz=8.0):
        """Relative 250 Hz input whose integral is a sine of amplitude `amp` px."""
        stop = threading.Event()
        trace = []
        th = threading.Thread(target=lambda: trace.extend(self.sample_cursor(stop)))
        th.start()
        prev, n = 0, int(seconds * 250)
        t0 = time.perf_counter()
        for i in range(1, n + 1):
            target = int(round(amp * math.sin(2 * math.pi * hz * i * 0.004)))
            if target != prev:
                self.move_rel(target - prev, 0)
                prev = target
            while time.perf_counter() - t0 < i * 0.004:
                pass
        stop.set()
        th.join()
        return trace

    # -- tests -----------------------------------------------------------------------------------------
    def test_assistance_off_is_a_one_to_one_pointer(self):
        self.configure(5, jitter=False)
        self.park()
        self.move_rel(1, 0)
        time.sleep(0.01)
        start = self.winapi.get_cursor_pos()
        self.move_rel(40, 25)
        time.sleep(0.15)
        end = self.winapi.get_cursor_pos()
        self.assertGreater(end[0] - start[0], 20)           # moved by the full (accelerated) delta, unfiltered
        self.assertGreater(end[1] - start[1], 12)

    def test_tremor_is_steadier_on_the_real_cursor_than_in_the_raw_input(self):
        """Compares total path travelled (what the analytics report) rather than peak-to-peak swing, which is
        distorted by integer pixels and slow convergence. Real mouse movement during the run can leak in, so the
        threshold is deliberately loose; a passing run still proves the hook, the swallow and SetCursorPos work."""
        def path(points):
            return sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(points, points[1:]))

        self.configure(9)
        reductions = []
        for _ in range(3):
            self.park()
            self.move_rel(1, 0)                              # first event after a pause: re-sync
            time.sleep(0.01)
            before = self.engine.pipe.stats()
            trace = self.drive_tremor(1.2, amp=3.0)
            after = self.engine.pipe.stats()
            raw_path = after["raw_distance"] - before["raw_distance"]
            self.assertGreater(raw_path, 30, "the injected tremor must actually reach the hook")
            reductions.append(1 - path(trace) / raw_path)
        reductions.sort()
        self.assertGreater(reductions[1], 0.15, reductions)   # the median run: real cursor travels clearly less
        st = self.engine.pipe.stats()
        self.assertGreater(st["raw_distance"], st["stabilised_distance"])

    def test_deliberate_movement_still_arrives_promptly(self):
        self.configure(9)
        self.park()
        self.move_rel(1, 0)
        time.sleep(0.01)
        start = self.winapi.get_cursor_pos()
        raw_before = self.engine.pipe.raw
        t0 = time.perf_counter()
        for i in range(100):                                 # 400 px at ~1000 px/s
            self.move_rel(4, 0)
            while time.perf_counter() - t0 < (i + 1) * 0.004:
                pass
        raw_after = self.engine.pipe.raw
        time.sleep(0.25)                                     # let the pointer settle with no more input
        end = self.winapi.get_cursor_pos()
        self.assertGreater(raw_after[0] - raw_before[0], 300)
        self.assertLessEqual(abs(end[0] - raw_after[0]), 1.5, (end, raw_after))   # caught up, never left behind
        self.assertGreater(end[0] - start[0], 300)

    def test_emergency_disable_restores_the_raw_pointer(self):
        self.configure(9)
        self.cfg.set("enabled", False)
        self.engine.apply_features()
        self.park()
        self.move_rel(1, 0)
        time.sleep(0.02)
        a = self.winapi.get_cursor_pos()
        self.move_rel(30, 0)
        time.sleep(0.1)
        self.assertGreater(self.winapi.get_cursor_pos()[0] - a[0], 15)
        self.cfg.set("enabled", True)


@unittest.skipUnless(LIVE and sys.platform == "win32", "set FOCUSFLOW_LIVE_TESTS=1 (opens a window and clicks it)")
class LiveCaptionButtonTests(unittest.TestCase):
    """Real window, real DWM geometry, real clicks on minimise and close."""

    @classmethod
    def setUpClass(cls):
        from focusflow import caption, winapi
        cls.caption, cls.winapi = caption, winapi
        winapi.set_dpi_aware()
        cls.origin = winapi.get_cursor_pos()

    @classmethod
    def tearDownClass(cls):
        cls.winapi.set_cursor_pos(*cls.origin)

    def spawn(self, title):
        import subprocess
        code = ("import tkinter as t; r=t.Tk(); r.title(%r); r.geometry('700x400+300+200'); "
                "r.after(15000, r.destroy); r.mainloop()" % title)
        child = subprocess.Popen([sys.executable, "-c", code])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        for _ in range(60):
            hwnd = ctypes.windll.user32.FindWindowW(None, title)
            if hwnd and ctypes.windll.user32.IsWindowVisible(hwnd):
                time.sleep(0.3)
                return child, hwnd
            time.sleep(0.1)
        self.fail("test window did not appear")

    def find_buttons(self, hwnd):
        """Sweep the title bar with target_at and collect distinct buttons, as the engine would meet them."""
        wr = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(wr))
        found = {}
        for x in range(wr.right - 260, wr.right, 3):
            t = self.caption.target_at(x, wr.top + 18)
            if t is not None:
                found.setdefault(t.name, t)
        return found

    def test_each_target_is_exactly_what_windows_itself_hit_tests(self):
        child, hwnd = self.spawn("ffcaption1")
        found = self.find_buttons(hwnd)
        self.assertEqual(sorted(found), ["Close button", "Maximize button", "Minimize button"])
        HT = {"Minimize button": 8, "Maximize button": 9, "Close button": 20}
        for name, t in found.items():
            l, tp, r, b = t.rect
            self.assertTrue(25 <= r - l <= 140, f"{name} width {r - l}")
            for px in (l + 3, (l + r) // 2, r - 3):                          # left edge, centre, right edge
                py = (tp + b) // 2
                got = self.caption.hit_code(hwnd, px, py)
                self.assertEqual(got, HT[name], f"{name}: ({px},{py}) hit-tests as {got}")
            self.assertEqual(self.caption.hit_code(hwnd, *t.hit_point), HT[name])
        child.kill()

    def test_the_title_bar_itself_and_the_client_area_are_not_targets(self):
        child, hwnd = self.spawn("ffcaption2")
        wr = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(wr))
        self.assertIsNone(self.caption.target_at(wr.left + 200, wr.top + 18))      # draggable title area
        self.assertIsNone(self.caption.target_at(wr.left + 300, wr.top + 300))     # client area
        child.kill()

    def test_pressing_found_targets_really_minimises_maximises_restores_and_closes_the_window(self):
        child, hwnd = self.spawn("ffcaption3")
        user32 = ctypes.windll.user32
        self.assertTrue(self.caption.press(self.find_buttons(hwnd)["Minimize button"]))
        self._until(lambda: user32.IsIconic(hwnd), "minimise")
        user32.ShowWindow(hwnd, 9)                                          # SW_RESTORE
        self._until(lambda: not user32.IsIconic(hwnd), "un-minimise")
        time.sleep(0.5)
        self.assertTrue(self.caption.press(self.find_buttons(hwnd)["Maximize button"]))
        self._until(lambda: user32.IsZoomed(hwnd), "maximise")
        time.sleep(0.5)
        restore = self.find_buttons(hwnd)
        self.assertIn("Restore button", restore, "a maximised window's middle button must be Restore")
        self.assertTrue(self.caption.press(restore["Restore button"]))
        self._until(lambda: not user32.IsZoomed(hwnd), "restore")
        time.sleep(0.5)
        self.assertTrue(self.caption.press(self.find_buttons(hwnd)["Close button"]))
        self.assertIsNotNone(self._wait(child), "close did not close the window")

    @staticmethod
    def _until(cond, what, seconds=4):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return
            time.sleep(0.1)
        raise AssertionError(f"window did not {what}")

    @staticmethod
    def _wait(child):
        for _ in range(40):
            if child.poll() is not None:
                return child.returncode
            time.sleep(0.1)
        return None


if __name__ == "__main__":
    unittest.main()
