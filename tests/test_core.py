import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from focusflow.dwell import Dwell
from focusflow.motion import MotionAnalyzer
from focusflow.store import Store, compute_stats
from focusflow.targets import (HitCache, Target, decide_physical_click, dist_to_rect,
                               nearest_unambiguous, normalized_offset, plausible_rect, snap_point)


def run_motion(path, hz=60, sens=5):
    m = MotionAnalyzer(sensitivity=sens)
    t = 0.0
    result = None
    for (x, y) in path:
        m.feed(t, x, y)
        result = m.analyze(t)
        t += 1.0 / hz
    return m, result


class MotionTests(unittest.TestCase):
    def test_tremor_is_detected(self):
        # 8 Hz, 5 px wobble around a resting point
        path = [(500 + 5 * math.sin(2 * math.pi * 8 * i / 60), 400) for i in range(90)]
        _, r = run_motion(path)
        self.assertTrue(r.is_tremor, r.tremor)

    def test_straight_movement_is_not_tremor(self):
        path = [(100 + 12 * i, 300 + 4 * i) for i in range(60)]
        _, r = run_motion(path)
        self.assertFalse(r.is_tremor)
        self.assertTrue(r.intentional)
        self.assertFalse(r.settled)

    def test_tremor_riding_on_a_deliberate_move_is_not_flagged(self):
        path = [(100 + 10 * i + 4 * math.sin(i * 1.3), 300) for i in range(60)]
        _, r = run_motion(path)
        self.assertFalse(r.is_tremor)

    def test_still_pointer_is_settled(self):
        _, r = run_motion([(200, 200)] * 60)
        self.assertTrue(r.settled)
        self.assertEqual(r.tremor, 0.0)

    def test_sub_pixel_sensor_noise_ignored(self):
        path = [(200 + (i % 2), 200) for i in range(60)]
        _, r = run_motion(path)
        self.assertFalse(r.is_tremor)

    def test_approach_metrics_count_reversals(self):
        path = [(500 + 6 * math.sin(2 * math.pi * 6 * i / 60), 400) for i in range(60)]
        m, _ = run_motion(path)
        a = m.take_approach(1.0)
        self.assertGreater(a["reversals"], 5)
        self.assertGreater(a["path_px"], 50)
        self.assertIsNone(m.take_approach(1.0))


class DwellTests(unittest.TestCase):
    def drive(self, d, seconds, key="a", settled=True, start=0.0, dwell=1.0, cooldown=1.5, step=0.05):
        events = []
        t = start
        while t <= start + seconds:
            prog, ev = d.update(t, key, settled, dwell, cooldown)
            if ev:
                events.append((round(t, 2), ev))
            t += step
        return events

    def test_fires_once_after_dwell_time(self):
        d = Dwell()
        ev = self.drive(d, 3.0)
        self.assertEqual([e for _, e in ev if e == "fire"], ["fire"])
        fire_t = [t for t, e in ev if e == "fire"][0]
        self.assertAlmostEqual(fire_t, 1.0, delta=0.1)

    def test_no_double_click_while_resting_on_same_target(self):
        d = Dwell()
        ev = self.drive(d, 6.0)
        self.assertEqual(sum(1 for _, e in ev if e == "fire"), 1)
        self.assertEqual(sum(1 for _, e in ev if e == "blocked"), 1)

    def test_leaving_resets_progress(self):
        d = Dwell()
        self.drive(d, 0.7)
        prog, ev = d.update(0.8, None, True, 1.0, 1.5)
        self.assertEqual(prog, 0.0)
        self.assertEqual(ev, "cancel")
        ev = self.drive(d, 0.7, start=0.9)
        self.assertNotIn("fire", [e for _, e in ev])

    def test_rearms_after_leaving_and_returning(self):
        d = Dwell()
        self.drive(d, 1.2)
        self.drive(d, 1.0, key=None, start=1.3)
        ev = self.drive(d, 3.0, start=2.4)
        self.assertEqual(sum(1 for _, e in ev if e == "fire"), 1)

    def test_brief_jitter_does_not_reset(self):
        d = Dwell()
        self.drive(d, 0.6)
        d.update(0.65, "a", False, 1.0, 1.5)  # one unsettled sample
        prog, _ = d.update(0.7, "a", True, 1.0, 1.5)
        self.assertGreater(prog, 0.6)

    def test_moving_pointer_never_fires(self):
        d = Dwell()
        ev = self.drive(d, 5.0, settled=False)
        self.assertEqual(ev, [])


class TargetTests(unittest.TestCase):
    def t(self, key, rect):
        return Target(key, rect)

    def test_distance(self):
        self.assertEqual(dist_to_rect((50, 50), (0, 0, 100, 100)), 0)
        self.assertAlmostEqual(dist_to_rect((110, 50), (0, 0, 100, 100)), 11, delta=1)

    def test_nearest_requires_clear_winner(self):
        a, b = self.t("a", (100, 0, 140, 40)), self.t("b", (100, 70, 140, 110))
        near, _ = nearest_unambiguous((120, 55), [a, b], 30)
        self.assertIsNone(near)  # equidistant -> do not guess
        near, d = nearest_unambiguous((120, 43), [a, b], 30)
        self.assertEqual(near.key, "a")

    def test_radius_respected(self):
        a = self.t("a", (100, 0, 140, 40))
        near, _ = nearest_unambiguous((300, 20), [a], 30)
        self.assertIsNone(near)

    def test_plausible_rect(self):
        area = 1920 * 1080
        self.assertFalse(plausible_rect((0, 0, 3, 3), area))
        self.assertFalse(plausible_rect((0, 0, 1900, 1000), area))
        self.assertFalse(plausible_rect((100, 100, 1255, 724), area))  # big content pane
        self.assertTrue(plausible_rect((100, 100, 1100, 140), area))   # wide, short text field
        self.assertTrue(plausible_rect((10, 10, 90, 40), area))

    def test_snap_point(self):
        small = self.t("s", (100, 100, 140, 130))
        self.assertEqual(snap_point(small, (90, 90)), (120, 115))
        big = self.t("b", (0, 0, 800, 400))
        self.assertEqual(snap_point(big, (900, 200)), (795, 200))

    def test_normalized_offset(self):
        self.assertEqual(normalized_offset((120, 115), (100, 100, 140, 130)), (0.0, 0.0))
        nx, ny = normalized_offset((150, 115), (100, 100, 140, 130))
        self.assertGreater(nx, 1.0)

    def test_click_policy(self):
        near = self.t("n", (100, 100, 140, 130))
        cache = HitCache((90, 115), 10.0, None, near, 10.0)
        self.assertEqual(decide_physical_click(cache, (90, 115), 10.1, 24, True)[0], "redirect")
        self.assertEqual(decide_physical_click(cache, (90, 115), 10.1, 24, False)[0], "miss")
        self.assertEqual(decide_physical_click(cache, (90, 115), 11.0, 24, True)[0], "pass")  # stale
        self.assertEqual(decide_physical_click(cache, (140, 115), 10.1, 24, True)[0], "pass")  # moved
        text = HitCache((90, 115), 10.0, None, near, 10.0, texty=True)
        self.assertEqual(decide_physical_click(text, (90, 115), 10.1, 24, True)[0], "miss")  # text: never hijack
        far = HitCache((90, 115), 10.0, None, near, 40.0)
        self.assertEqual(decide_physical_click(far, (90, 115), 10.1, 24, True)[0], "pass")
        hit = HitCache((110, 115), 10.0, near, None, 0.0)
        self.assertEqual(decide_physical_click(hit, (110, 115), 10.1, 24, True)[0], "manual_hit")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.dir.name, "t.db"))

    def tearDown(self):
        self.store.db.close()
        self.dir.cleanup()

    def test_empty_stats_have_no_fabricated_numbers(self):
        s = self.store.stats()
        self.assertEqual(s["total_assisted"], 0)
        self.assertIsNone(s["efficiency"]["manual"]["avg_path_px"])

    def test_stats_from_real_events(self):
        a = {"method": "dwell", "path_px": 400, "reversals": 2, "duration_ms": 2000}
        self.store.add_event("click_assisted", "desktop", "OK", a)
        self.store.add_event("click_assisted", "browser", "Link", dict(a, path_px=600))
        self.store.add_event("manual_click", "desktop", "OK", {"path_px": 1000, "reversals": 12, "duration_ms": 5000})
        self.store.add_event("near_miss", "desktop", "OK", {"corrected": True, "nx": 1.4, "ny": 0.2})
        self.store.add_event("click_prevented", "desktop", "OK", {"reason": "repeat_dwell_guard"})
        s = self.store.stats()
        self.assertEqual(s["total_assisted"], 2)
        self.assertEqual(s["dwell_clicks"], 2)
        self.assertEqual(s["near_miss_corrected"], 1)
        self.assertEqual(s["prevented"], 1)
        self.assertEqual(s["efficiency"]["assisted"]["avg_path_px"], 500.0)
        self.assertEqual(s["efficiency"]["manual"]["avg_reversals"], 12.0)

    def test_demo_data_is_separate(self):
        self.store.seed_demo()
        self.assertEqual(self.store.stats(demo=False)["total_assisted"], 0)
        self.assertGreater(self.store.stats(demo=True)["total_assisted"], 0)
        self.assertEqual(self.store.recent(10, demo=False), [])
        self.store.clear(demo=True)
        self.assertFalse(self.store.has_demo())

    def test_undo_flags(self):
        i = self.store.add_event("app_launch", "desktop", "Notes", {}, undo={"type": "close_pid", "pid": 1})
        j = self.store.add_event("click_assisted", "desktop", "OK", {})
        self.assertTrue(self.store.get(i)["undoable"])
        self.assertFalse(self.store.get(j)["undoable"])
        self.assertEqual(self.store.last_undoable()["id"], i)
        self.store.mark_undone(i)
        self.assertIsNone(self.store.last_undoable())


if __name__ == "__main__":
    unittest.main()
