"""Drives the calibration wizard window with a fake engine and fake clock. Needs Tk and a display
(skipped otherwise; on Linux run it with `xvfb-run -a python -m unittest tests.test_calibration_ui`)."""
import math
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gc

try:
    import tkinter as tk
except Exception:                                   # no tkinter at all
    tk = None


def _have_display():
    """Probe once, then drop the probe interpreter on this (main) thread: a Tcl interpreter that is garbage-collected
    later on another thread (e.g. a server thread started by a different test) aborts the whole process."""
    if tk is None:
        return False
    try:
        probe = tk.Tk()
        probe.withdraw()
        probe.destroy()
        del probe
        gc.collect()
        return True
    except Exception:
        return False


HAVE_TK = _have_display()

if HAVE_TK:
    from focusflow import calibration as cal
    from focusflow.calibration_ui import CalibrationWindow, PROFILE_NAME
    from focusflow.config import Settings


class FakePipe:
    scale = 1.0

    def __init__(self, events):
        self.events = events

    def snapshot(self, since_ms=None):
        tr = [(t, x, y, x, y, False) for t, x, y in self.events if since_ms is None or t >= since_ms]
        return tr, [], 0.0, False


class FakeEngine:
    def __init__(self, events):
        self.pipe = FakePipe(events)
        self.began = self.ended = 0
        self.mode = None

    def begin_session(self, mode):
        self.began += 1
        self.mode = mode

    def end_session(self):
        self.ended += 1


class FakeLocal:
    def set(self, name, targets):
        pass

    def clear(self, name):
        pass


class FakeApp:
    def __init__(self, cfg, engine):
        self.cfg, self.engine = cfg, engine


class FakeUI:
    def __init__(self, root, app):
        self.root, self.app, self.local = root, app, FakeLocal()
        self.toasts = []

    def toast(self, text, ms=0):
        self.toasts.append(text)


class Event:
    def __init__(self, x_root, y_root, ox, oy):
        self.x_root, self.y_root, self.x, self.y = x_root, y_root, x_root - ox, y_root - oy


@unittest.skipUnless(HAVE_TK, "needs Tk and a display")
class WizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.geometry("+0+0")
        self.clock = [0.0]
        self.cursor = [0, 0]
        events = []
        t = 0.0
        while t < 40000:                             # 8 Hz tremor around the screen position set in drive_hold
            events.append((t, 400 + 3 * math.sin(2 * math.pi * 8 * t / 1000), 300))
            t += 8.0
        self.engine = FakeEngine(events)
        self.cfg = Settings(os.path.join(self.tmp.name, "settings.json"))
        self.ui = FakeUI(self.root, FakeApp(self.cfg, self.engine))
        self.w = CalibrationWindow(self.ui, cursor_pos=lambda: tuple(self.cursor), clock=lambda: self.clock[0])
        random.seed(3)

    def tearDown(self):
        self.w.stop()
        self.root.destroy()
        self.w = self.ui = self.root = self.engine = None
        gc.collect()                                 # release Tk objects here, on the main thread
        self.tmp.cleanup()

    def drive_hold(self, seconds=12.0):
        self.root.update()
        cx, cy = self.w._center()
        self.cursor[:] = [cx, cy]
        # re-centre the synthetic trace on the marker so the collector sees a hand resting on the ring
        self.engine.pipe.events = [(t, cx + (x - 400), cy + (y - 300)) for t, x, y in self.engine.pipe.events]
        steps = int(seconds * 1000 / 50)
        for _ in range(steps):
            self.clock[0] += 50.0
            self.w._cancel_tick()                    # one pending timer at a time, as in the running app
            self.w._hold_tick()
            if self.w.phase != "hold":
                break

    def click_all_targets(self, miss_every=3, miss_by=20):
        n = 0
        while self.w.phase == "reach":
            ox, oy = self.w.canvas.winfo_rootx(), self.w.canvas.winfo_rooty()
            l, t, r, b = self.w.rect
            cx, cy = ox + (l + r) / 2, oy + (t + b) / 2
            if n % miss_every == 0:
                self.w._reach_click(Event(cx + (r - l) / 2 + miss_by, cy, ox, oy))   # lands 20 px outside
            self.clock[0] += 700.0
            self.w._reach_click(Event(cx, cy, ox, oy))
            n += 1
            self.assertLess(n, 40)

    def test_full_run_ends_with_a_recommendation_and_saves_a_profile(self):
        self.w.start()
        self.assertEqual(self.w.phase, "intro")
        self.assertEqual(self.engine.began, 0)                       # nothing is masked while reading the intro
        self.w._begin(True)
        self.assertEqual(self.engine.mode, "Normal mouse operation")
        self.drive_hold()
        self.assertEqual(self.w.phase, "reach")
        self.click_all_targets()
        self.assertEqual(self.w.phase, "result")
        self.assertEqual((self.engine.began, self.engine.ended), (1, 1))   # assistance restored before the result
        self.assertGreater(self.w.rec.values["smoothing_strength"], 3)     # the tremor was noticed
        self.assertTrue(self.w.rec.values["near_miss_correct"])            # a third of the clicks missed
        self.w.save()
        self.assertEqual(self.cfg["active_profile"], PROFILE_NAME)
        self.assertEqual(self.cfg["smoothing_strength"], self.w.rec.values["smoothing_strength"])
        self.assertIsNone(self.w.win)
        self.assertIn(PROFILE_NAME, self.ui.toasts[-1])

    def test_clicking_is_hard_skips_the_click_task(self):
        self.w.start()
        self.w._begin(False)
        self.drive_hold()
        self.assertEqual(self.w.phase, "result")
        self.assertTrue(self.w.rec.values["dwell_enabled"])
        self.assertNotIn("miss_rate", self.w.rec.metrics)

    def test_closing_mid_measurement_restores_assistance_and_saves_nothing(self):
        before = dict(self.cfg.snapshot())
        self.w.start()
        self.w._begin(True)
        self.assertTrue(self.w.measuring)
        self.w.stop()
        self.assertEqual((self.engine.began, self.engine.ended), (1, 1))
        self.assertFalse(self.w.measuring)
        self.assertEqual(before, dict(self.cfg.snapshot()))

    def test_restarting_never_leaves_two_sessions_open(self):
        self.w.start()
        self.w._begin(True)
        self.w.start()
        self.assertEqual(self.engine.began - self.engine.ended, 0)

    def test_a_moving_hand_fails_cleanly_and_restores_assistance(self):
        self.w.start()
        self.w._begin(True)
        self.root.update()
        cx, cy = self.w._center()
        self.cursor[:] = [cx, cy]
        self.engine.pipe.events = [(t, cx + t / 40.0, cy) for t in range(0, 40000, 8)]   # walks 200+ px
        for _ in range(240):
            self.clock[0] += 50.0
            self.w._cancel_tick()                    # one pending timer at a time, as in the running app
            self.w._hold_tick()
            if self.w.phase != "hold":
                break
        self.assertEqual(self.w.phase, "failed")
        self.assertEqual(self.engine.began, self.engine.ended)


if __name__ == "__main__":
    unittest.main()
