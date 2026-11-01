"""Exercise the real engine's adaptation path with native services replaced by mocks."""
import ast
import math
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from focusflow import profiles, targets
from focusflow.config import Settings
from focusflow.dwell import Dwell
from focusflow.motion import MotionAnalyzer
from focusflow.pointer import MissTracker, PointerPipeline
from focusflow.ml.live import LiveML, SmoothingPolicy


class AdaptationTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "focusflow/engine.py").read_text())
        nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.Assign))]
        self.native = mock.Mock(WM_MOUSEMOVE=1, WM_LBUTTONDOWN=2, WM_LBUTTONUP=3)
        self.native.virtual_bounds.return_value = (0, 0, 2000, 1000)
        self.native.get_cursor_pos.return_value = (95, 115)
        self.native.left_button_down.return_value = False
        ns = dict(vars(targets), threading=threading, time=time, math=math, profiles=profiles,
                  winapi=self.native, caption=mock.Mock(), MotionAnalyzer=MotionAnalyzer, Dwell=Dwell,
                  UiaWorker=mock.Mock(), PointerPipeline=PointerPipeline, MissTracker=MissTracker,
                  LiveML=LiveML, SmoothingPolicy=SmoothingPolicy, INTERVAL_S=.5)
        ns["caption"].target_at.return_value = None
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "focusflow/engine.py", "exec"), ns)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Settings(Path(tmp.name) / "settings.json")
        self.cfg.set_many(dict(extension_enabled=False, dwell_enabled=False, snap_enabled=False,
                               near_miss_correct=False, attraction_strength=0), persist=False)
        self.local = targets.LocalTargets()
        self.e = ns["Engine"](self.cfg, mock.Mock(), self.local)
        self.e.uia.submit.return_value.done.return_value = False
        self.addCleanup(self.e.stop)
        self.e._refresh_environment = mock.Mock()
        self.e._foreground = mock.Mock()
        self.e._ml_tick = mock.Mock()
        self.e.motion = mock.Mock()
        self.e.motion.analyze.return_value = SimpleNamespace(pos=(95, 115), settled=True, speed=0, tremor=0)
        self.e.motion.take_approach.return_value = None
        self.target = targets.Target("button", (100, 100, 150, 130), "Save", "Button", "local")
        self.local.set("test", [self.target])

    def tick(self, now):
        self.e._tick(now)

    def test_misses_after_leaving_button_grow_visible_proxy_without_dwell_or_snap(self):
        self.e._cur = self.target  # dwell hysteresis still considers this the current target
        with mock.patch.object(time, "perf_counter", side_effect=[10., 11., 12.]):
            for now in (10., 11., 12.):
                self.tick(now)
                self.assertEqual(self.e.hit_cache.nearest, self.target)
                self.assertFalse(self.e.on_mouse(2, 95, 115, False))
        self.tick(12.1)
        self.assertIn(self.target.key, self.e.miss.grown)
        self.assertTrue(self.e.overlay.grown)
        self.assertEqual(self.e.overlay.label, "Save")
        self.assertEqual(self.e.overlay.progress, 0)
        self.e._cur = None
        self.tick(12.2)
        self.assertTrue(self.e.overlay.grown)  # still visible outside, without hysteresis
        self.e.undo_adaptation()
        self.tick(12.3)
        self.assertIsNone(self.e.overlay)

    def test_native_probes_refresh_before_click_cache_expiry(self):
        self.e._probe_pos = (95, 115)
        self.e._probe_ts = 10.
        self.e._request_probe(10.21, 95, 115, True)
        self.e.uia.submit.assert_called_once()

    def test_local_geometry_is_fresh_even_with_old_native_probe(self):
        self.e._probe = SimpleNamespace(pos=(95, 115), ts=8., under=None, nearby=[], texty=False)
        self.tick(10.)
        self.assertEqual(self.e.hit_cache.ts, 10.)
        self.assertEqual(targets.decide_physical_click(self.e.hit_cache, (95, 115), 10.01, 24, False)[0], "miss")

    def test_native_under_rectangle_does_not_reset_miss_streak_outside_button(self):
        self.local.clear("test")
        self.target.source = "uia"
        self.e._probe = SimpleNamespace(pos=(101, 115), ts=10., under=self.target, nearby=[], texty=False)
        self.tick(10.1)
        self.assertIsNone(self.e.hit_cache.under)
        self.assertEqual(self.e.hit_cache.nearest, self.target)
        self.assertEqual(targets.decide_physical_click(self.e.hit_cache, (95, 115), 10.11, 24, False)[0], "miss")

    def test_duplicate_targets_do_not_create_false_ambiguity(self):
        self.assertEqual(targets.nearest_unambiguous((95, 115), [self.target, self.target], 24)[0], self.target)
        other = targets.Target("other", (90, 130, 140, 160))
        self.assertIsNone(targets.nearest_unambiguous((95, 130), [self.target, other], 24)[0])

    def test_stale_cache_and_disabled_assistance_never_redirect(self):
        self.tick(10.)
        self.e.miss.grown.add(self.target.key)
        self.assertEqual(targets.decide_physical_click(self.e.hit_cache, (95, 115), 11., 24, True,
                                                      grown=self.e.miss.grown)[0], "pass")
        self.e.set_enabled(False)
        self.assertFalse(self.e.on_mouse(2, 95, 115, False))


if __name__ == "__main__":
    unittest.main()
