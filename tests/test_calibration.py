"""Calibration: synthetic hold/reach recordings -> recommended profile. Pure, runs on any platform."""
import math
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from focusflow import calibration as cal
from focusflow import profiles
from focusflow.config import Settings

STEP = 4.0


def hold(amp=0.0, freq=8.0, seconds=8.0, noise=0.0, drift=0.0, seed=1, scale=1.0, event_ms=8.0):
    """Mouse events (t_ms, x, y) in physical px: oscillation + noise + slow drift around (800, 500)."""
    rng = random.Random(seed)
    out, t = [], 1000.0
    while t <= 1000.0 + seconds * 1000:
        s = (t - 1000.0) / 1000.0
        x = 800 + scale * (amp * math.sin(2 * math.pi * freq * s) + rng.gauss(0, noise) + drift * s)
        y = 500 + scale * (0.5 * amp * math.cos(2 * math.pi * freq * s) + rng.gauss(0, noise))
        out.append((t, round(x), round(y)))          # a real mouse reports integer pixels
        t += event_ms
    return out, 1000.0, 1000.0 + seconds * 1000


def analyzed(**kw):
    scale = kw.get("scale", 1.0)
    samples, t0, t1 = hold(**kw)
    return cal.analyze_hold(samples, t0, t1, scale)


def reach(n=12, miss_rate=0.0, miss_px=10.0, double=0):
    clicks, t = [], 0.0
    for i in range(n):
        missed = i < round(n * miss_rate)
        clicks.append({"t": t, "hit": not missed, "miss_px": miss_px if missed else 0.0})
        t += 1.5
    for k in range(double):
        clicks.append({"t": clicks[k]["t"] + 0.08, "hit": True, "miss_px": 0.0})
    return cal.analyze_reach(clicks)


class HoldAnalysisTests(unittest.TestCase):
    def test_steady_hand_has_low_wander(self):
        h = analyzed(amp=0.0, noise=0.2)
        self.assertLess(h["rms_px"], cal.QUIET_RMS_PX)

    def test_tremor_is_measured_and_its_frequency_estimated(self):
        h = analyzed(amp=3.0, freq=8.0)
        self.assertGreater(h["rms_px"], 1.5)
        self.assertAlmostEqual(h["approx_hz"], 8.0, delta=1.5)

    def test_slow_drift_is_not_counted_as_tremor(self):
        drifting = analyzed(amp=0.0, drift=3.0, seconds=8.0)       # 24 px of slow travel
        self.assertLess(drifting["rms_px"], cal.QUIET_RMS_PX)

    def test_a_still_hand_with_no_events_after_the_first_is_handled(self):
        h = cal.analyze_hold([(1000.0, 800, 500)], 1000.0, 7000.0)
        self.assertEqual(h["rms_px"], 0.0)

    def test_display_scale_is_divided_out(self):
        a = analyzed(amp=3.0, scale=1.0)
        b = analyzed(amp=3.0, scale=1.5)
        self.assertAlmostEqual(a["rms_px"], b["rms_px"], delta=0.35)

    def test_rejects_short_empty_and_moving_recordings(self):
        s, t0, t1 = hold(amp=2.0, seconds=2.0)
        with self.assertRaises(cal.CalibrationError):
            cal.analyze_hold(s, t0, t1)
        with self.assertRaises(cal.CalibrationError):
            cal.analyze_hold([], 0.0, 8000.0)
        s, t0, t1 = hold(amp=0.0, drift=40.0, seconds=8.0)           # walked 320 px: not a hold
        with self.assertRaises(cal.CalibrationError):
            cal.analyze_hold(s, t0, t1)


class StrengthTests(unittest.TestCase):
    def test_steady_hand_gets_the_lightest_smoothing(self):
        rec = cal.recommend(analyzed(amp=0.0, noise=0.2))
        self.assertEqual(rec.values["smoothing_strength"], cal.STRENGTHS[0])

    def test_stronger_tremor_never_gets_weaker_smoothing(self):
        picks = [cal.recommend(analyzed(amp=a)).values["smoothing_strength"] for a in (0.0, 1.5, 3.0, 6.0)]
        self.assertEqual(picks, sorted(picks))
        self.assertGreater(picks[-1], picks[0])

    def test_chosen_strength_really_reduces_wander_in_replay(self):
        h = analyzed(amp=3.0)
        rec = cal.recommend(h)
        res = cal.residual_after_filter(h["grid"], rec.values["smoothing_strength"])
        self.assertLess(res["rms_px"], h["rms_px"] * 0.6)

    def test_strength_is_bounded_and_leaves_room_for_live_ml(self):
        rec = cal.recommend(analyzed(amp=12.0, freq=6.0))
        self.assertLessEqual(rec.values["smoothing_strength"], 9)
        self.assertGreaterEqual(rec.values["smoothing_strength"], 3)


class ReachTests(unittest.TestCase):
    def test_reach_metrics(self):
        r = reach(n=10, miss_rate=0.3, miss_px=20.0)
        self.assertAlmostEqual(r["miss_rate"], 0.3)
        self.assertAlmostEqual(r["p90_miss_px"], 20.0)

    def test_double_clicks_are_counted(self):
        self.assertEqual(reach(double=3)["double_clicks"], 3)

    def test_too_few_clicks_rejected(self):
        with self.assertRaises(cal.CalibrationError):
            cal.analyze_reach([{"t": 0, "hit": True, "miss_px": 0}] * 3)

    def test_misses_turn_on_correction_and_size_the_hit_area(self):
        rec = cal.recommend(analyzed(amp=3.0), reach(miss_rate=0.4, miss_px=20.0))
        v = rec.values
        self.assertTrue(v["near_miss_correct"] and v["adaptation_enabled"])
        self.assertEqual(v["miss_radius_px"], 24)                     # 20 px * 1.25, rounded to 4
        self.assertGreater(v["attraction_strength"], 0)

    def test_hit_area_is_clamped(self):
        big = cal.recommend(analyzed(amp=3.0), reach(miss_rate=0.5, miss_px=200.0)).values
        self.assertEqual(big["miss_radius_px"], 56)

    def test_accurate_clicks_keep_correction_off(self):
        v = cal.recommend(analyzed(amp=0.0, noise=0.2), reach(miss_rate=0.0)).values
        self.assertFalse(v["near_miss_correct"])
        self.assertEqual(v["attraction_strength"], 0)

    def test_repeated_clicks_enable_the_repeat_click_guard(self):
        self.assertTrue(cal.recommend(analyzed(amp=3.0), reach(double=3)).values["bounce_filter"])
        self.assertFalse(cal.recommend(analyzed(amp=3.0), reach()).values["bounce_filter"])


class DwellTests(unittest.TestCase):
    def test_dwell_is_never_shorter_than_one_and_a_half_seconds(self):
        for amp in (0.0, 1.5, 3.0, 8.0):
            v = cal.recommend(analyzed(amp=amp), can_click=False).values
            self.assertTrue(v["dwell_enabled"])
            self.assertGreaterEqual(v["dwell_ms"], 1500)

    def test_steady_user_who_can_click_does_not_get_dwell(self):
        v = cal.recommend(analyzed(amp=0.0, noise=0.2), can_click=True).values
        self.assertFalse(v["dwell_enabled"])

    def test_user_who_cannot_click_always_gets_dwell(self):
        self.assertTrue(cal.recommend(analyzed(amp=0.0, noise=0.2), can_click=False).values["dwell_enabled"])

    def test_tremor_user_gets_a_longer_dwell_than_a_steady_one(self):
        steady = cal.recommend(analyzed(amp=0.0, noise=0.2), can_click=False).values["dwell_ms"]
        shaky = cal.recommend(analyzed(amp=6.0), can_click=False).values["dwell_ms"]
        self.assertGreater(shaky, steady)


class ProfileTests(unittest.TestCase):
    def test_values_are_a_complete_profile_with_sane_ranges(self):
        rec = cal.recommend(analyzed(amp=3.0), reach(miss_rate=0.3, miss_px=15.0, double=2))
        self.assertEqual(set(rec.values), set(profiles.PROFILE_KEYS))
        v = rec.values
        self.assertTrue(0 <= v["smoothing_strength"] <= 10 and 1 <= v["sensitivity"] <= 10)
        self.assertTrue(0 <= v["attraction_strength"] <= 10 and 6 <= v["tolerance_px"] <= 16)
        self.assertTrue(rec.reasons and all(isinstance(r, str) for r in rec.reasons))

    def test_result_does_not_mutate_the_builtin_profiles(self):
        before = {k: dict(p["values"]) for k, p in profiles.BUILTIN.items()}
        cal.recommend(analyzed(amp=5.0), reach(miss_rate=0.5, miss_px=30.0, double=3))
        self.assertEqual(before, {k: dict(p["values"]) for k, p in profiles.BUILTIN.items()})

    def test_apply_saves_a_custom_profile_and_activates_it(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Settings(os.path.join(d, "settings.json"))
            rec = cal.recommend(analyzed(amp=3.0), reach(miss_rate=0.3, miss_px=15.0))
            name = cal.apply_recommendation(cfg, rec)
            self.assertEqual(name, "My calibration")
            self.assertEqual(cfg["active_profile"], "My calibration")
            self.assertIn("My calibration", profiles.all_profiles(cfg))
            self.assertEqual(cfg["smoothing_strength"], rec.values["smoothing_strength"])
            self.assertTrue(profiles.matches(cfg, "My calibration"))

    def test_cannot_overwrite_a_builtin_profile_name(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Settings(os.path.join(d, "settings.json"))
            self.assertIsNone(cal.apply_recommendation(cfg, cal.recommend(analyzed(amp=3.0)), name="Comfort"))


if __name__ == "__main__":
    unittest.main()


class CollectorTests(unittest.TestCase):
    CENTER = (800, 500)

    def drive(self, c, seconds, cursor=None, trace_fn=None, start=0.0, step=50.0):
        t = start
        while t <= start + seconds * 1000:
            tr = trace_fn(t - step, t) if trace_fn else ()
            c.update(t, cursor or self.CENTER, self.CENTER, tr)
            t += step
        return t

    def test_waits_for_the_pointer_to_rest_on_the_marker_before_recording(self):
        c = cal.HoldCollector(radius_px=30)
        self.drive(c, 3.0, cursor=(1000, 800))                       # nowhere near the marker
        self.assertEqual(c.state, c.WAITING)
        self.drive(c, 0.6, start=3000.0)                             # on it, but not for a full second yet
        self.assertEqual(c.state, c.WAITING)
        self.drive(c, 0.6, start=3600.0)
        self.assertEqual(c.state, c.RECORDING)

    def test_records_for_the_hold_duration_then_analyses_the_trace(self):
        c = cal.HoldCollector(radius_px=30, hold_s=8.0)
        events, t0, _ = hold(amp=3.0, seconds=12.0, event_ms=8.0)
        trace = lambda a, b: [(t, x, y, x, y, False) for t, x, y in events if a < t <= b]
        self.drive(c, 11.0, trace_fn=trace, start=1000.0)
        self.assertEqual(c.state, c.DONE)
        res = c.result()
        self.assertGreater(res["rms_px"], 1.5)
        self.assertAlmostEqual(res["seconds"], 8.0, delta=0.2)

    def test_samples_are_not_duplicated_across_updates(self):
        c = cal.HoldCollector(radius_px=30)
        events, _, _ = hold(amp=3.0, seconds=12.0)
        all_trace = [(t, x, y, x, y, False) for t, x, y in events]
        self.drive(c, 11.0, trace_fn=lambda a, b: all_trace, start=1000.0)   # same full trace every call
        times = [s[0] for s in c.samples]
        self.assertEqual(times, sorted(set(times)))

    def test_leaving_the_marker_restarts_the_wait(self):
        c = cal.HoldCollector(radius_px=30)
        t = self.drive(c, 2.0)
        self.assertEqual(c.state, c.RECORDING)
        self.drive(c, 1.0, cursor=(900, 600), start=t)
        self.assertEqual(c.state, c.WAITING)
        self.assertEqual(c.restarts, 1)

    def test_a_brief_wobble_off_the_marker_is_forgiven(self):
        c = cal.HoldCollector(radius_px=30, grace_s=0.5)
        t = self.drive(c, 2.0)
        c.update(t, (900, 600), self.CENTER)
        c.update(t + 200, self.CENTER, self.CENTER)
        self.assertEqual(c.state, c.RECORDING)

    def test_result_before_done_is_an_error(self):
        with self.assertRaises(cal.CalibrationError):
            cal.HoldCollector(radius_px=30).result()


class RectDistanceTests(unittest.TestCase):
    def test_inside_edge_side_and_corner(self):
        r = (100, 100, 150, 150)
        self.assertEqual(cal.rect_distance((120, 120), r), 0)
        self.assertEqual(cal.rect_distance((160, 120), r), 10)
        self.assertAlmostEqual(cal.rect_distance((160, 170), r), math.hypot(10, 20))
