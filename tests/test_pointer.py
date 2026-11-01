"""The PointerLab port: filter behaviour on synthetic-but-physical input (the real hook is tested in test_hook.py)."""
import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from focusflow.pointer import (AdaptivePointer, Attractor, ErraticDetector, MissTracker, MotionTracker,
                               PointerPipeline, hit_target, nearby_target)

STEP_MS = 4.0   # 250 Hz, the pipeline's sampling rate


def run(filter_, raw_path, near=False, t0=1000.0):
    """raw_path: list of (x, y) at 250 Hz. Returns list of (t, raw, out)."""
    out = []
    for i, p in enumerate(raw_path):
        t = t0 + i * STEP_MS
        out.append((t, p, filter_.sample(p, t, near)))
    return out


def lag(trace):
    return [math.hypot(r[0] - o[0], r[1] - o[1]) for _, r, o in trace]


def path_len(points):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


class AdaptivePointerTests(unittest.TestCase):
    @staticmethod
    def p2p_ratio(freq, amp, cutoff_scale=1.0, n=500):
        f = AdaptivePointer(cutoff_scale=cutoff_scale)
        raws, outs = [], []
        for i in range(n):
            t = i * STEP_MS
            x = 500 + amp * math.sin(2 * math.pi * freq * t / 1000)
            outs.append(f.sample((x, 400), 1000 + t, False)[0])
            raws.append(x)
        r, o = raws[100:], outs[100:]
        return (max(o) - min(o)) / (max(r) - min(r))

    def test_subpixel_tremor_is_removed_entirely(self):
        self.assertLess(self.p2p_ratio(8, 0.6), 0.05)

    def test_small_tremor_is_reduced_at_original_strength_and_more_at_higher_strength(self):
        base = self.p2p_ratio(8, 1.5, 1.0)            # the original filter, unchanged
        strong = self.p2p_ratio(8, 1.5, 0.4)          # Maximum-assistance cut-off scale
        self.assertLess(base, 0.75)
        self.assertLess(strong, 0.45)
        self.assertLess(strong, base)

    def test_large_deliberate_swings_are_mostly_passed(self):
        self.assertGreater(self.p2p_ratio(2, 40, 1.0), 0.9)    # a 2 Hz, 40 px sweep is movement, not tremor

    def test_oscillation_does_not_open_speed_adaptive_filter(self):
        # Signed velocity estimation suppresses rectified tremor speed while the
        # deliberate sweep/lag tests above continue to enforce responsiveness.
        self.assertLess(self.p2p_ratio(8, 3), 0.68)
        self.assertLess(self.p2p_ratio(12, 3), 0.55)

    def test_stronger_strength_filters_more(self):
        rng = random.Random(5)
        path = [(300 + 4 * math.sin(i * 0.5) + rng.uniform(-1, 1), 300) for i in range(300)]
        light = path_len([o for _, _, o in run(AdaptivePointer(cutoff_scale=2.0), path)])
        strong = path_len([o for _, _, o in run(AdaptivePointer(cutoff_scale=0.5), path)])
        self.assertLess(strong, light)

    def test_rapid_movement_stays_responsive_and_lag_is_bounded(self):
        path = [(100 + 6 * i, 300) for i in range(200)]       # 1500 px/s for 0.8 s
        tr = run(AdaptivePointer(), path)
        self.assertLessEqual(max(lag(tr)), 8.0 + 1e-6)
        self.assertGreater(tr[-1][2][0], tr[-1][1][0] - 8.01)  # never further behind than the cap
        # progress is made throughout: no stalls
        xs = [o[0] for _, _, o in tr]
        self.assertTrue(all(b >= a for a, b in zip(xs, xs[1:])))

    def test_not_excessively_slow_on_a_normal_aimed_move(self):
        # 300 px glide at ~500 px/s: the displayed pointer must reach within 2 px of the target soon after the hand
        path = [(100 + 2 * i, 200) for i in range(150)]
        f = AdaptivePointer()
        tr = run(f, path)
        end_t = tr[-1][0]
        for k in range(1, 60):                                 # keep the filter settling with no new input
            f.settle(end_t + k * 8)
        arrive = None
        for k in range(1, 200):
            out = f.settle(end_t + 480 + k * 8)
        self.assertLess(math.hypot(out[0] - path[-1][0], out[1] - path[-1][1]), 1.0)
        self.assertLess(max(lag(tr)), 8.01)

    def test_intentional_reversal_is_followed(self):
        path = [(100 + 4 * i, 300) for i in range(100)] + [(500 - 4 * i, 300) for i in range(100)]
        tr = run(AdaptivePointer(), path)
        self.assertLessEqual(max(lag(tr)), 8.0 + 1e-6)
        # the output actually turned around and travelled back
        xs = [o[0] for _, _, o in tr]
        peak = max(xs)
        self.assertGreater(peak, 480)
        self.assertLess(xs[-1], peak - 150)

    def test_sudden_stop_settles_without_new_input(self):
        path = [(100 + 5 * i, 300) for i in range(120)]
        f = AdaptivePointer()
        tr = run(f, path)
        end_t, last_raw = tr[-1][0], tr[-1][1]
        t = end_t
        for _ in range(30):                                    # 240 ms at 125 Hz
            t += 8
            f.settle(t)
        self.assertLess(math.hypot(f.output[0] - last_raw[0], f.output[1] - last_raw[1]), 1.0)
        self.assertFalse(f.is_settling() and math.hypot(f.output[0] - last_raw[0], f.output[1] - last_raw[1]) > 1)

    def test_subpixel_noise_does_not_animate_forever(self):
        f = AdaptivePointer()
        f.sample((200, 200), 0)
        f.sample((200.4, 200), 4)
        self.assertFalse(f.is_settling())

    def test_long_input_gap_resets_to_the_raw_point(self):
        f = AdaptivePointer()
        for i in range(20):
            f.sample((100 + i, 100), i * 4)
        out = f.sample((900, 700), 20 * 4 + 400)
        self.assertEqual(out, (900.0, 700.0))

    def test_near_button_reversal_is_resisted(self):
        def displacement_at_reversal(near):
            f = AdaptivePointer()
            t = 1000.0
            for i in range(30):                                # steady 2 px steps right
                f.sample((300 + 2 * i, 300), t, near)
                t += STEP_MS
            before = f.output
            out = f.sample((300 + 2 * 29 - 2, 300), t, near)   # small back-step: a brief reversal near a button
            return math.hypot(out[0] - before[0], out[1] - before[1])
        self.assertLess(displacement_at_reversal(True), displacement_at_reversal(False))

    def test_display_scale_scales_the_distance_constants(self):
        path = [(100 + 12 * i, 300) for i in range(120)]        # fast, with a very low cut-off: the lag cap binds
        base = max(lag(run(AdaptivePointer(scale=1.0, cutoff_scale=0.05), path)))
        hi = max(lag(run(AdaptivePointer(scale=1.5, cutoff_scale=0.05), path)))
        self.assertAlmostEqual(base, 8.0, delta=0.01)           # the original 8 px bound
        self.assertAlmostEqual(hi, 12.0, delta=0.01)            # the same bound on a 150 % display


class DetectorTests(unittest.TestCase):
    def test_motion_tracker_counts_reversals_over_three_pixel_segments(self):
        m = MotionTracker()
        for x in [0, 1, 2, 3, 4, 5, 6, 3, 0, -3, -6]:
            m.add((x, 0))
        self.assertEqual(m.direction_changes, 1)
        self.assertEqual(m.corrective, 1)
        self.assertAlmostEqual(m.total_distance, 6 + 12, delta=1e-9)

    def test_sub_threshold_wobble_is_not_a_direction_change(self):
        m = MotionTracker()
        for i in range(200):
            m.add((100 + (1 if i % 2 else 0), 100))
        self.assertEqual(m.direction_changes, 0)

    def test_erratic_detector_needs_repeated_short_reversals_in_the_window(self):
        d = ErraticDetector()
        flags = [d.observe(t, True, 8) for t in range(0, 1000, 150)]
        self.assertEqual(sum(flags), 1)                          # fired on the 6th, then cooled down
        self.assertFalse(any(ErraticDetector().observe(t, True, 8) for t in (0, 600, 1200, 1800, 2400, 3000)))
        self.assertFalse(any(ErraticDetector().observe(t, True, 80) for t in range(0, 900, 100)))  # long = deliberate

    def test_erratic_detector_cooldown(self):
        d = ErraticDetector()
        n = sum(d.observe(t, True, 8) for t in range(0, 2400, 100))
        self.assertEqual(n, 1)


class TargetTests(unittest.TestCase):
    A = ("a", (100, 100, 140, 130))
    B = ("b", (100, 160, 140, 190))

    def test_hit_target(self):
        self.assertEqual(hit_target((120, 110), [self.A, self.B]), "a")
        self.assertIsNone(hit_target((300, 300), [self.A, self.B]))

    def test_nearby_is_unambiguous_and_close(self):
        self.assertEqual(nearby_target((120, 140), [self.A], 36), "a")
        self.assertIsNone(nearby_target((120, 145), [self.A, self.B], 36))   # 15 vs 15: do not guess
        self.assertEqual(nearby_target((120, 135), [self.A, self.B], 36), "a")
        self.assertIsNone(nearby_target((400, 400), [self.A], 36))           # distant whitespace

    def test_three_nearby_misses_enlarge_once_and_success_clears_the_streak(self):
        m = MissTracker()
        self.assertFalse(m.record("a"))
        self.assertFalse(m.record("a"))
        m.success("a")
        self.assertFalse(m.record("a"))
        self.assertFalse(m.record("a"))
        self.assertTrue(m.record("a"))
        self.assertFalse(m.record("a"))                           # already enlarged
        self.assertIn("a", m.grown)
        m.clear()
        self.assertFalse(m.grown)

    def test_threshold_is_configurable(self):
        m = MissTracker(lambda: 2)
        self.assertFalse(m.record("a"))
        self.assertTrue(m.record("a"))

    def test_disabled_adaptation_never_enlarges(self):
        m = MissTracker()
        self.assertFalse(any(m.record("a", enabled=False) for _ in range(5)))
        self.assertFalse(m.record(None))


class AttractorTests(unittest.TestCase):
    RECT = ("t", (200, 100, 260, 140))

    def drive(self, a, base, speed, seconds, strength=6, radius=40, target=RECT):
        dt = 0.008
        for _ in range(int(seconds / dt)):
            a.update(base, speed, target, strength, radius, dt)

    def test_slow_pointer_is_pulled_into_a_nearby_target_and_held(self):
        a = Attractor()
        self.drive(a, (180, 120), 20, 1.0)
        self.assertGreaterEqual(180 + a.ox, 200)
        self.assertLessEqual(180 + a.ox, 260)
        held = (a.ox, a.oy)
        self.drive(a, (180, 120), 20, 0.5)
        self.assertEqual((a.ox, a.oy), held)                      # inside: no further pushing

    def test_pull_never_exceeds_the_radius(self):
        a = Attractor()
        self.drive(a, (120, 120), 20, 1.0, radius=40)             # target is 80 px away
        self.assertEqual((a.ox, a.oy), (0.0, 0.0))

    def test_deliberate_movement_drains_the_pull_quickly(self):
        a = Attractor()
        self.drive(a, (180, 120), 20, 1.0)
        self.assertTrue(a.active)
        self.drive(a, (180, 120), 1500, 0.4)
        self.assertFalse(a.active)

    def test_no_target_no_pull_and_zero_strength_off(self):
        a = Attractor()
        self.drive(a, (180, 120), 20, 0.5, target=None)
        self.assertFalse(a.active)
        self.drive(a, (180, 120), 20, 0.5, strength=0)
        self.assertFalse(a.active)


class PipelineTests(unittest.TestCase):
    def make(self, smoothing=True, scale=1.0, **kw):
        placed = []
        pipe = PointerPipeline(scale, bounds=(0, 0, 1920, 1080), apply=lambda x, y: (placed.append((x, y)) or (x, y)))
        pipe.configure(smoothing, 1.0, **kw)
        return pipe, placed

    def feed_os(self, pipe, deltas, t0=1000.0, start=(500, 500), dt=4.0):
        """Simulate the OS: it adds each hardware delta to the *current* cursor and reports that to the hook."""
        cursor = list(start)
        t = t0
        swallowed = 0
        for dx, dy in deltas:
            x, y = cursor[0] + dx, cursor[1] + dy
            swallowed += pipe.on_move(x, y, t)
            if not (pipe.owns_cursor and pipe.cursor is not None and pipe.cursor != (x, y)):
                pass
            cursor = list(pipe.cursor if pipe.cursor else (x, y))
            if not pipe.owns_cursor:
                cursor = [x, y]
            t += dt
        return cursor, swallowed, t

    def test_first_event_and_gaps_pass_through_and_resync(self):
        pipe, placed = self.make()
        self.assertFalse(pipe.on_move(500, 500, 1000.0))          # nothing to filter yet
        self.assertFalse(pipe.on_move(700, 100, 1500.0))          # 500 ms later: resync, not a delta
        self.assertEqual(pipe.cursor, (700, 100))
        self.assertEqual(placed, [])

    def test_raw_position_integrates_deltas_even_though_events_are_swallowed(self):
        pipe, placed = self.make()
        pipe.on_move(500, 500, 1000.0)
        cursor, swallowed, _ = self.feed_os(pipe, [(3, 0)] * 100, t0=1004.0)
        self.assertGreater(swallowed, 50)
        self.assertAlmostEqual(pipe.raw[0], 800.0, delta=1e-6)    # 100 events x 3 px
        self.assertLessEqual(math.hypot(pipe.raw[0] - pipe.out[0], pipe.raw[1] - pipe.out[1]), 8.0 + 1e-6)

    def test_jitter_is_not_passed_to_the_os_cursor(self):
        pipe, placed = self.make()
        pipe.configure(True, 0.4)                                # Maximum-assistance strength
        pipe.on_move(500, 500, 1000.0)
        # 8 Hz, +-2 px hand tremor delivered as 250 Hz mouse deltas
        pos = [2 * math.sin(2 * math.pi * 8 * i * 0.004) for i in range(501)]
        deltas = [(round(b - a, 3), 0) for a, b in zip(pos, pos[1:])]
        deltas = [(int(round(b)) - int(round(a)), 0) for a, b in zip(pos, pos[1:])]
        self.feed_os(pipe, deltas, t0=1004.0)
        xs = [500] + [p[0] for p in placed]   # complete suppression is valid: no SetCursorPos calls
        self.assertLessEqual(max(xs) - min(xs), 2)               # raw swings 4 px peak-to-peak
        st = pipe.stats()
        self.assertGreater(st["raw_distance"], 2 * st["stabilised_distance"])

    def test_passthrough_mode_never_swallows_and_still_measures(self):
        pipe, placed = self.make(smoothing=False)
        pipe.on_move(500, 500, 1000.0)
        _, swallowed, _ = self.feed_os(pipe, [(4, 0)] * 50, t0=1004.0)
        self.assertEqual(swallowed, 0)
        self.assertEqual(placed, [])
        self.assertGreater(pipe.stats()["raw_distance"], 100)
        self.assertEqual(pipe.stats()["raw_distance"], pipe.stats()["stabilised_distance"])

    def test_tick_finishes_the_move_after_input_stops(self):
        pipe, placed = self.make()
        pipe.on_move(500, 500, 1000.0)
        _, _, t = self.feed_os(pipe, [(5, 0)] * 80, t0=1004.0)
        for k in range(60):
            pipe.tick(t + 30 + k * 8)
        self.assertLessEqual(abs(pipe.cursor[0] - pipe.raw[0]), 1)

    def test_cursor_is_clamped_to_the_virtual_desktop(self):
        pipe, placed = self.make()
        pipe.on_move(5, 500, 1000.0)
        self.feed_os(pipe, [(-6, 0)] * 80, t0=1004.0, start=(5, 500))
        self.assertGreaterEqual(pipe.raw[0], 0)
        self.assertTrue(all(p[0] >= 0 for p in placed))

    def test_erratic_callback_fires_on_repeated_short_reversals(self):
        pipe, _ = self.make()
        hits = []
        pipe.on_erratic = lambda: hits.append(1)
        pipe.on_move(500, 500, 1000.0)
        deltas = ([(5, 0)] * 2 + [(-5, 0)] * 2) * 60
        self.feed_os(pipe, deltas, t0=1004.0, dt=10.0)
        self.assertGreaterEqual(len(hits), 1)

    def test_trace_and_events_are_recorded_for_the_live_lab(self):
        pipe, _ = self.make()
        pipe.on_move(500, 500, 1000.0)
        self.feed_os(pipe, [(2, 1)] * 60, t0=1004.0)
        trace, events, speed, smoothing = pipe.snapshot()
        self.assertGreater(len(trace), 20)
        self.assertTrue(smoothing)
        self.assertEqual(len(trace[0]), 6)


if __name__ == "__main__":
    unittest.main()
