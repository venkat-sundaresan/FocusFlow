"""Engine.on_mouse decision glue and feature policy, with Windows calls mocked out."""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

if sys.platform != "win32":
    raise unittest.SkipTest("engine click integration requires Windows APIs")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from focusflow import profiles, winapi
from focusflow.config import Settings
from focusflow.engine import Engine
from focusflow.store import Store
from focusflow.targets import HitCache, LocalTargets, Target


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = Settings(os.path.join(self.dir.name, "s.json"))
        self.store = Store(os.path.join(self.dir.name, "t.db"))
        with mock.patch.object(winapi, "get_cursor_pos", return_value=(0, 0)):
            self.engine = Engine(self.cfg, self.store, LocalTargets())
        self.engine.pipe._apply = lambda x, y: (x, y)
        self.button = Target("k", (100, 100, 140, 130), "OK", "Button", hit_point=(118, 114))

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def cache(self, under=None, nearest=None, dist=0.0, pos=(90, 115), texty=False):
        self.engine.hit_cache = HitCache(pos, time.perf_counter(), under, nearest, dist, texty)

    def kinds(self):
        time.sleep(0.2)   # async log writer
        return [e["kind"] for e in reversed(self.store.recent(50))]


class EngineClickTests(EngineTestBase):
    def test_near_miss_click_is_redirected_and_button_up_swallowed(self):
        self.cache(nearest=self.button, dist=10)
        clicked = []
        with mock.patch.object(winapi, "send_click", side_effect=lambda x, y: clicked.append((x, y))):
            self.assertTrue(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 90, 115, False))
            self.assertTrue(self.engine.on_mouse(winapi.WM_LBUTTONUP, 90, 115, False))
            time.sleep(0.3)
        self.assertEqual(clicked, [(118, 114)])        # a point that hit-tested into the button
        self.assertIn("near_miss", self.kinds())
        self.assertIn("click_assisted", self.kinds())
        self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONUP, 90, 115, False))  # only one 'up' swallowed

    def test_correction_disabled_only_logs(self):
        self.cfg.set("near_miss_correct", False)
        self.cache(nearest=self.button, dist=10)
        with mock.patch.object(winapi, "send_click") as sc:
            self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 90, 115, False))
            sc.assert_not_called()
        self.assertIn("near_miss", self.kinds())

    def test_click_on_a_target_passes_through_and_is_recorded_as_manual(self):
        self.engine.motion.feed(0.0, 0, 0)
        self.engine.motion.feed(0.1, 50, 0)
        self.cache(under=self.button, pos=(110, 115))
        self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 110, 115, False))
        self.assertIn("manual_click", self.kinds())

    def test_stale_or_unknown_context_never_touches_the_click(self):
        self.engine.hit_cache = HitCache((90, 115), time.perf_counter() - 5, None, self.button, 5)
        self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 90, 115, False))
        self.engine.hit_cache = None
        self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 90, 115, False))

    def test_injected_input_is_ignored(self):
        self.cache(nearest=self.button, dist=10)
        self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 90, 115, True))
        self.assertFalse(self.engine.on_mouse(winapi.WM_MOUSEMOVE, 90, 115, True))

    def test_paused_means_hands_off_including_pointer_movement(self):
        self.cache(nearest=self.button, dist=10)
        self.cfg.set("enabled", False)
        self.engine.apply_features()
        self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 90, 115, False))
        self.assertFalse(self.engine.on_mouse(winapi.WM_MOUSEMOVE, 90, 115, False))

    def test_bounce_filter_swallows_fast_repeat_only_when_enabled(self):
        self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 50, 50, False))
        self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 50, 50, False))  # filter off
        self.cfg.set("bounce_filter", True)
        self.engine._last_down = 0
        self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 50, 50, False))
        self.assertTrue(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 50, 50, False))   # 0 ms later
        self.assertTrue(self.engine.on_mouse(winapi.WM_LBUTTONUP, 50, 50, False))
        self.assertIn("click_prevented", self.kinds())


class HitAreaAdaptationTests(EngineTestBase):
    def miss(self, dist=10, texty=False):
        self.cache(nearest=self.button, dist=dist, texty=texty)
        with mock.patch.object(winapi, "send_click"):
            self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 90, 115, False)
            self.engine._swallow_up = False
            self.engine._last_down = 0
            time.sleep(0.05)

    def test_three_nearby_misses_enlarge_the_hit_area_and_it_is_logged_once(self):
        self.cfg.set("near_miss_correct", False)
        adapted = []
        self.engine.on_adapted = adapted.append
        for _ in range(3):
            self.miss()
        self.assertIn("k", self.engine.miss.grown)
        self.assertEqual(adapted, ["OK"])
        self.assertEqual(self.kinds().count("target_adapted"), 1)

    def test_enlarged_target_is_corrected_out_to_double_the_radius_even_without_immediate_correction(self):
        self.cfg.set_many({"near_miss_correct": False, "miss_radius_px": 24})
        self.cache(nearest=self.button, dist=40)                       # beyond 24 px, inside 2 x 24
        with mock.patch.object(winapi, "send_click") as sc:
            self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 90, 115, False))   # not grown yet
            self.engine.miss.grown.add("k")
            self.engine._last_down = 0
            self.cache(nearest=self.button, dist=40)
            self.assertTrue(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 90, 115, False))
            time.sleep(0.2)
            sc.assert_called_once()

    def test_text_surfaces_are_never_redirected_or_counted_even_when_grown(self):
        self.engine.miss.grown.add("k")
        self.cache(nearest=self.button, dist=10, texty=True)
        with mock.patch.object(winapi, "send_click") as sc:
            self.assertFalse(self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 90, 115, False))
            sc.assert_not_called()
        self.assertEqual(self.engine.miss.counts.get("k", 0), 0)

    def test_a_successful_hit_clears_the_streak(self):
        self.cfg.set("near_miss_correct", False)
        self.miss()
        self.miss()
        self.cache(under=self.button, pos=(110, 115))
        self.engine.motion.feed(0.0, 0, 0)
        self.engine.motion.feed(0.1, 50, 0)
        self.engine._last_down = 0
        self.engine.on_mouse(winapi.WM_LBUTTONDOWN, 110, 115, False)
        self.assertEqual(self.engine.miss.counts["k"], 0)
        self.miss()
        self.assertNotIn("k", self.engine.miss.grown)

    def test_adaptation_off_never_enlarges(self):
        self.cfg.set_many({"near_miss_correct": False, "adaptation_enabled": False})
        for _ in range(4):
            self.miss()
        self.assertFalse(self.engine.miss.grown)

    def test_undo_adaptation_restores_every_target(self):
        self.engine.miss.grown.update({"a", "b"})
        self.assertEqual(self.engine.undo_adaptation(), 2)
        self.assertFalse(self.engine.miss.grown)


class PointerPipelineGlueTests(EngineTestBase):
    def test_moves_are_swallowed_only_while_jitter_reduction_is_active(self):
        self.engine.on_mouse(winapi.WM_MOUSEMOVE, 500, 500, False)      # first event: re-sync, passes
        self.assertTrue(self.engine.on_mouse(winapi.WM_MOUSEMOVE, 502, 500, False))
        self.cfg.set_many({"jitter_enabled": False, "attraction_strength": 0})   # attraction alone also owns the cursor
        self.assertEqual(self.engine.pipe.smoothing, False)
        self.engine.on_mouse(winapi.WM_MOUSEMOVE, 600, 600, False)
        self.assertFalse(self.engine.on_mouse(winapi.WM_MOUSEMOVE, 603, 600, False))

    def test_smoothing_strength_changes_the_filter_cutoff(self):
        self.cfg.set("smoothing_strength", 9)
        a = self.engine.pipe.filter.cutoff_scale
        self.cfg.set("smoothing_strength", 2)
        self.assertGreater(self.engine.pipe.filter.cutoff_scale, a)

    def test_an_exception_in_the_pointer_path_stops_stabilising_and_passes_the_event(self):
        faults = []
        self.engine.on_fault = faults.append
        with mock.patch.object(self.engine.pipe, "on_move", side_effect=RuntimeError("boom")):
            self.assertFalse(self.engine.on_mouse(winapi.WM_MOUSEMOVE, 10, 10, False))
        self.assertTrue(self.engine.pipe_faulted)
        self.assertEqual(len(faults), 1)
        self.assertFalse(self.engine.pipe.smoothing)                    # pipeline switched off
        self.assertFalse(self.engine.on_mouse(winapi.WM_MOUSEMOVE, 12, 10, False))
        self.cfg.set("enabled", False)
        self.engine.set_enabled(True)                                    # turning assistance on again clears the fault
        self.assertFalse(self.engine.pipe_faulted)

    def test_attraction_is_gated_by_the_adaptation_group(self):
        self.cfg.set("attraction_strength", 6)
        self.assertEqual(self.engine.attraction(), 6)
        self.engine.begin_session("Jitter reduction only")
        self.assertEqual(self.engine.attraction(), 0)
        self.engine.end_session()
        self.assertEqual(self.engine.attraction(), 6)


class FeatureMaskTests(EngineTestBase):
    def test_each_experiment_mode_enables_exactly_its_feature_group(self):
        expect = {
            "Normal mouse operation": (False, False, False),
            "Jitter reduction only": (True, False, False),
            "Dwell clicking only": (False, True, False),
            "Target adaptation only": (False, False, True),
            "Full FocusFlow assistance": (True, True, True),
        }
        self.cfg.set_many(profiles.all_profiles(self.cfg)["Maximum Assistance"])      # everything enabled in the profile
        for mode, (jit, dwell, adapt) in expect.items():
            self.engine.begin_session(mode)
            got = (self.engine.jitter_on(), self.engine.dwell_on(), self.engine.adapt_on())
            self.assertEqual(got, (jit, dwell, adapt), mode)
            self.assertEqual(self.engine.correct_on(), adapt, mode)
            self.assertEqual(self.engine.snap_on(), dwell, mode)
            self.assertEqual(self.engine.bounce_on(), dwell, mode)
            self.assertEqual(self.engine.pipe.smoothing, jit, mode)
        self.engine.end_session()
        self.assertTrue(self.engine.jitter_on() and self.engine.dwell_on() and self.engine.adapt_on())

    def test_a_mode_starts_without_adaptation_carried_over(self):
        self.engine.miss.grown.add("x")
        self.engine.begin_session("Target adaptation only")
        self.assertFalse(self.engine.miss.grown)

    def test_the_profile_setting_still_wins_inside_a_mode(self):
        self.cfg.set_many(profiles.all_profiles(self.cfg)["Precision"])               # dwell off in the profile
        self.engine.begin_session("Dwell clicking only")
        self.assertFalse(self.engine.dwell_on())


class AppProfileSwitchTests(EngineTestBase):
    def test_a_per_application_profile_applies_while_that_app_is_in_front_and_reverts_after(self):
        self.cfg.set("app_profiles", {"game.exe": "Precision"})
        self.engine._check_app_profile("game.exe")
        self.assertEqual(self.engine.current_profile(), "Precision")
        self.assertFalse(self.cfg["dwell_enabled"])
        self.assertEqual(self.cfg["active_profile"], "Comfort")                        # the user's choice is untouched
        self.engine._check_app_profile("notepad.exe")
        self.assertEqual(self.engine.current_profile(), "Comfort")
        self.assertTrue(self.cfg["dwell_enabled"])

    def test_focusflows_own_windows_do_not_flip_profiles(self):
        self.cfg.set("app_profiles", {"python.exe": "Precision"})
        self.engine._check_app_profile("python.exe")
        self.assertEqual(self.engine.current_profile(), "Comfort")

    def test_no_switching_during_an_experiment_session(self):
        self.cfg.set("app_profiles", {"game.exe": "Precision"})
        self.engine.session = object()
        self.engine._check_app_profile("game.exe")
        self.assertEqual(self.engine.current_profile(), "Comfort")


class SuggestionTests(EngineTestBase):
    def test_repeated_reversals_offer_stronger_smoothing_at_most_once_per_cooldown(self):
        offers = []
        self.engine.on_suggest = lambda: offers.append(1)
        self.engine._on_erratic()
        self.engine._on_erratic()
        self.assertEqual(len(offers), 1)

    def test_no_offer_when_smoothing_is_already_strong_or_suggestions_are_off(self):
        offers = []
        self.engine.on_suggest = lambda: offers.append(1)
        self.cfg.set("smoothing_strength", 9)
        self.engine._on_erratic()
        self.cfg.set_many({"smoothing_strength": 5, "suggest_stronger": False})
        self.engine._on_erratic()
        self.assertEqual(offers, [])

    def test_the_event_is_logged_as_a_heuristic_not_a_diagnosis(self):
        self.engine._on_erratic()
        time.sleep(0.2)
        ev = [e for e in self.store.recent(10) if e["kind"] == "erratic_movement"]
        self.assertEqual(len(ev), 1)
        self.assertIn("heuristic", ev[0]["detail"]["note"])


if __name__ == "__main__":
    unittest.main()


class CaptionButtonTests(EngineTestBase):
    def test_extent_scan_finds_the_run_of_the_same_hit_code_around_the_pointer(self):
        from focusflow import caption

        def hit(x, y):                      # Windows' hit-test along a caption row
            if 854 <= x <= 904:
                return caption.HTMINBUTTON
            if 907 <= x <= 957:
                return caption.HTMAXBUTTON
            if 958 <= x <= 1010:
                return caption.HTCLOSE
            return 2
        l, r = caption.scan_extent(hit, 980, 20, caption.HTCLOSE)
        self.assertLessEqual(abs(l - 958), 2)
        self.assertLessEqual(abs(r - 1011), 2)
        l, r = caption.scan_extent(hit, 880, 20, caption.HTMINBUTTON)
        self.assertLessEqual(abs(l - 854), 2)
        self.assertLessEqual(abs(r - 905), 2)

    def test_only_the_three_system_buttons_become_targets_and_names_follow_the_window_state(self):
        from focusflow import caption
        self.assertEqual(caption.name_for(caption.HTCLOSE, False), "Close")
        self.assertEqual(caption.name_for(caption.HTMAXBUTTON, False), "Maximize")
        self.assertEqual(caption.name_for(caption.HTMAXBUTTON, True), "Restore")
        self.assertIsNone(caption.name_for(2, False))                  # HTCAPTION: a title-bar drag, not a button
        self.assertIsNone(caption.name_for(None, False))               # the window did not answer

    def test_dwell_on_a_caption_button_sends_the_system_command_not_a_mouse_click(self):
        from focusflow import caption
        btn = Target("caption:77:Close", (1000, 0, 1073, 45), "Close button", "Button", "caption",
                     hit_point=(1036, 22), data=(77, caption.HTCLOSE))
        with mock.patch.object(winapi, "send_click") as sc, \
                mock.patch.object(caption.user32, "IsWindow", return_value=1), \
                mock.patch.object(caption.user32, "IsZoomed", return_value=0), \
                mock.patch.object(caption.user32, "PostMessageW", return_value=1) as post:
            self.engine._fire(btn, False, (1070, 40), mock.Mock(tremor=0.0), time.perf_counter())
        sc.assert_not_called()
        post.assert_called_once_with(77, caption.WM_SYSCOMMAND, caption.SC_CLOSE, 0)

    def test_each_button_maps_to_its_system_command_and_maximise_becomes_restore(self):
        from focusflow import caption
        self.assertEqual(caption.command_for(caption.HTMINBUTTON, False), caption.SC_MINIMIZE)
        self.assertEqual(caption.command_for(caption.HTMAXBUTTON, False), caption.SC_MAXIMIZE)
        self.assertEqual(caption.command_for(caption.HTMAXBUTTON, True), caption.SC_RESTORE)
        self.assertEqual(caption.command_for(caption.HTCLOSE, True), caption.SC_CLOSE)
        self.assertIsNone(caption.command_for(2, False))

    def test_press_refuses_a_window_that_has_gone_away_or_a_target_without_a_window(self):
        from focusflow import caption
        gone = Target("caption:5:Close", (0, 0, 1, 1), "Close button", "Button", "caption", data=(5, caption.HTCLOSE))
        with mock.patch.object(caption.user32, "IsWindow", return_value=0), \
                mock.patch.object(caption.user32, "PostMessageW") as post:
            self.assertFalse(caption.press(gone))
            post.assert_not_called()
        self.assertFalse(caption.press(Target("x", (0, 0, 1, 1))))

    def _patched(self, **extra):
        from focusflow import caption
        base = dict(root_window_at=1, _strip=(900, 0, 1100, 45))
        base.update(extra)
        cms = [mock.patch.object(caption, k, return_value=v) for k, v in base.items()]
        cms += [mock.patch.object(caption.user32, "IsWindowVisible", return_value=1),
                mock.patch.object(caption.user32, "IsIconic", return_value=0),
                mock.patch.object(caption.user32, "IsZoomed", return_value=0),
                mock.patch.object(caption.user32, "GetWindowLongW", return_value=0xCF0000)]
        return cms

    def test_a_window_that_does_not_answer_is_ignored(self):
        from focusflow import caption
        cms = self._patched(hit_code=None)
        for c in cms:
            c.start()
            self.addCleanup(c.stop)
        self.assertIsNone(caption.target_at(1000, 20))

    def test_outside_the_caption_strip_costs_nothing_and_finds_nothing(self):
        from focusflow import caption
        cms = self._patched()
        for c in cms:
            c.start()
            self.addCleanup(c.stop)
        with mock.patch.object(caption, "hit_code") as hc:
            self.assertIsNone(caption.target_at(300, 400))
            hc.assert_not_called()                                     # pre-filter: no cross-process message sent

    def test_a_hit_on_a_button_becomes_a_target_with_its_scanned_extent_and_a_centre_click_point(self):
        from focusflow import caption

        def hit(hwnd, x, y):
            return caption.HTCLOSE if 1040 <= x <= 1093 else 2
        cms = self._patched()
        for c in cms:
            c.start()
            self.addCleanup(c.stop)
        with mock.patch.object(caption, "hit_code", side_effect=hit):
            t = caption.target_at(1060, 20)
        self.assertEqual((t.name, t.source, t.ctype), ("Close button", "caption", "Button"))
        self.assertLessEqual(abs(t.rect[0] - 1040), 2)
        self.assertLessEqual(abs(t.rect[2] - 1094), 2)
        self.assertEqual(t.hit_point, ((t.rect[0] + t.rect[2]) // 2, (t.rect[1] + t.rect[3]) // 2))


if __name__ == "__main__":
    unittest.main()
