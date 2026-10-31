"""Experiment recorder, voice grammar, switch navigation and the local HTTP API."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from focusflow import voice, winapi
from focusflow.config import Settings
from focusflow.engine import Engine
from focusflow.experiment import SessionRecorder
from focusflow.profiles import MODE_NAMES
from focusflow.server import Server
from focusflow.store import Store
from focusflow.switchnav import SwitchNav
from focusflow.targets import LocalTargets, Target


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = Settings(os.path.join(self.dir.name, "s.json"))
        self.store = Store(os.path.join(self.dir.name, "t.db"))
        with mock.patch.object(winapi, "get_cursor_pos", return_value=(0, 0)):
            self.engine = Engine(self.cfg, self.store, LocalTargets())
        self.engine.pipe._apply = lambda x, y: (x, y)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()


class SessionRecorderTests(Base):
    RECTS = [(1, (100, 100, 150, 150)), (2, (180, 100, 230, 150)), (3, (400, 400, 450, 450))]

    def test_click_classification(self):
        rec = SessionRecorder(self.engine, MODE_NAMES[-1], 3)
        self.assertEqual(rec.click((120, 120), 1, 1, self.RECTS), "success")
        self.assertEqual(rec.click((200, 120), 1, 2, self.RECTS), "accidental")     # wrong control activated
        self.assertEqual(rec.click((160, 175), 1, None, self.RECTS), "miss")        # empty space is always a miss
        self.assertEqual(rec.click((700, 700), 1, None, self.RECTS), "miss")        # far whitespace
        self.assertEqual((rec.successes, rec.incorrect, rec.accidental), (1, 3, 1))
        rec.finish(False)

    def test_only_unambiguous_nearby_targets_count_as_near_misses(self):
        rec = SessionRecorder(self.engine, MODE_NAMES[-1], 3)
        rec.click((125, 175), 1, None, self.RECTS)       # 25 px below target 1 only -> near miss
        rec.click((165, 125), 1, None, self.RECTS)       # equidistant between 1 and 2 -> ambiguous, not attributed
        rec.click((700, 700), 1, None, self.RECTS)       # far from everything
        self.assertEqual(rec.near_misses, 1)
        self.assertEqual(rec.incorrect, 3)
        rec.finish(False)

    def test_three_nearby_misses_on_one_target_enlarge_it_when_adaptation_is_active(self):
        rec = SessionRecorder(self.engine, "Target adaptation only", 3)
        adapted = []
        self.engine.on_adapted = adapted.append
        for _ in range(3):
            rec.click((125, 175), 1, None, self.RECTS)
        self.assertIn(1, self.engine.miss.grown)
        self.assertEqual(adapted, ["1"])
        rec.finish(False)

    def test_adaptation_is_not_applied_in_modes_that_exclude_it(self):
        rec = SessionRecorder(self.engine, "Jitter reduction only", 3)
        for _ in range(4):
            rec.click((125, 175), 1, None, self.RECTS)
        self.assertFalse(self.engine.miss.grown)
        rec.finish(False)

    def test_engine_corrected_near_misses_are_counted_but_are_not_errors(self):
        rec = SessionRecorder(self.engine, MODE_NAMES[-1], 3)
        rec.engine_near_miss(True)
        self.assertEqual((rec.near_misses, rec.incorrect, rec.corrected), (1, 0, 1))
        rec.finish(False)

    def test_finishing_restores_assistance_and_saves_only_completed_runs(self):
        rec = SessionRecorder(self.engine, "Normal mouse operation", 1)
        self.assertFalse(self.engine.jitter_on())
        self.assertIs(self.engine.session, rec)
        rec.click((120, 120), 1, 1, self.RECTS)
        out = rec.finish(True)
        self.assertTrue(self.engine.jitter_on())
        self.assertIsNone(self.engine.session)
        self.assertEqual(self.store.sessions()[0]["id"], out["id"])
        self.assertIsNone(rec.finish(True))                            # idempotent
        rec2 = SessionRecorder(self.engine, MODE_NAMES[0], 1)
        rec2.finish(False)
        self.assertEqual(len(self.store.sessions()), 1)

    def test_pointer_metrics_are_measured_from_the_moment_the_session_starts(self):
        pipe = self.engine.pipe
        pipe.configure(True, 1.0, 0)
        t = 1000.0
        pipe.on_move(500, 500, t)
        for i in range(120):                                           # movement BEFORE the session
            t += 4
            pipe.on_move(500 + (i % 3) * 4 + 4, 500, t)
        self.assertGreater(pipe.stats()["raw_distance"], 0)
        rec = SessionRecorder(self.engine, "Full FocusFlow assistance", 1)
        self.assertEqual(pipe.stats()["raw_distance"], 0)              # a session starts from clean counters
        pipe.configure(True, 1.0, 0)
        t += 400
        pipe.on_move(900, 700, t)
        for i in range(100):
            t += 4
            pipe.on_move(900 + (i % 2) * 6, 700, t)
        stats_at_end = pipe.stats()
        out = rec.finish(True)
        self.assertGreater(out["raw_distance"], 0)
        self.assertEqual(out["raw_distance"], round(stats_at_end["raw_distance"], 1))
        self.assertLessEqual(out["stabilised_distance"], out["raw_distance"])
        self.assertEqual(out["raw_changes"], stats_at_end["raw_changes"])

    def test_unknown_modes_are_rejected(self):
        with self.assertRaises(ValueError):
            SessionRecorder(self.engine, "Turbo")


class VoiceTests(unittest.TestCase):
    def test_grammar_is_well_formed_and_covers_every_command(self):
        root = ET.fromstring(voice.grammar_xml(langid="809"))
        self.assertEqual(root.get("LANGID"), "809")
        phrases = [p.text for p in root.iter("P")]
        self.assertEqual(sorted(phrases), sorted(voice.COMMANDS))
        self.assertEqual(len(set(phrases)), len(phrases))

    def test_phrases_are_escaped(self):
        xml = voice.grammar_xml({"fish & chips": "x", "a<b": "y"})
        ET.fromstring(xml)                                             # would raise on bad escaping

    def test_commands_map_to_actions_the_app_understands(self):
        actions = set(voice.COMMANDS.values())
        for needed in ("pause", "resume", "emergency", "click", "double_click", "right_click", "scroll_up",
                       "scroll_down", "next_profile", "keyboard_show", "keyboard_hide"):
            self.assertIn(needed, actions)
        self.assertTrue(any(a.startswith("profile:") for a in actions))

    def test_low_confidence_and_unknown_results_never_trigger_actions(self):
        got = []
        lst = voice.VoiceListener(lambda a, t: got.append(a))

        def result(text, conf):
            el = mock.Mock(Count=1)
            el.Item.return_value = mock.Mock(ActualConfidence=conf)
            return mock.Mock(PhraseInfo=mock.Mock(GetText=lambda: text, Elements=el))

        lst._on_result(result("click", -1))                            # low confidence: dropped
        lst._on_result(result("launch the missiles", 1))               # not in the grammar: dropped
        lst._on_result(result("Click", 0))                             # normal confidence, case-insensitive
        self.assertEqual(got, ["click"])

    def test_availability_probe_never_raises(self):
        ok, msg = voice.available()
        self.assertIsInstance(ok, bool)
        self.assertTrue(msg)


class SwitchNavTests(Base):
    def setUp(self):
        super().setUp()
        self.local = LocalTargets()
        self.nav = SwitchNav(self.cfg, self.engine, self.local)
        self.found = [Target(f"t{i}", (10 * i, 10, 10 * i + 8, 30), f"Button {i}", "Button", hit_point=(10 * i + 4, 20))
                      for i in range(3)]
        self.patches = [
            mock.patch.object(SwitchNav, "rescan", self._rescan),
            mock.patch.object(SwitchNav, "_fresh", lambda s: bool(s.targets)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def _rescan(self):
        import time
        self.nav.targets, self.nav.index, self.nav._scanned_at = list(self.found), -1, time.time()
        return len(self.found)

    def test_nothing_happens_while_switch_navigation_is_off(self):
        self.assertIsNone(self.nav.next())
        self.assertFalse(self.nav.select())

    def test_next_steps_through_targets_in_order_and_wraps(self):
        self.cfg.set("switch_enabled", True)
        seen = [self.nav.next().name for _ in range(4)]
        self.assertEqual(seen, ["Button 0", "Button 1", "Button 2", "Button 0"])
        self.assertEqual(self.nav.current().name, "Button 0")

    def test_select_clicks_the_highlighted_target_at_its_verified_point(self):
        self.cfg.set("switch_enabled", True)
        self.nav.next()
        self.nav.next()
        with mock.patch.object(winapi, "send_click") as sc:
            self.assertTrue(self.nav.select())
            sc.assert_called_once_with(14, 20)
        self.assertIsNone(self.nav.current())                          # the screen probably changed: highlight cleared
        self.assertFalse(self.nav.select())                            # nothing selected any more

    def test_select_runs_the_action_for_focusflows_own_controls(self):
        self.cfg.set("switch_enabled", True)
        hits = []
        self.found = [Target("own", (0, 0, 10, 10), "Own", "FocusFlow control", "local", action=lambda: hits.append(1))]
        self.local.dispatch = lambda fn: fn()
        self.nav.next()
        self.nav.select()
        self.assertEqual(hits, [1])


class ApiTests(Base):
    def setUp(self):
        super().setUp()
        outer = self

        class StubApp:
            cfg, store, engine = outer.cfg, outer.store, outer.engine

            def status(self):
                return {"enabled": True, "profile": "Comfort"}

            def extension_config(self):
                return {"enabled": True}

            def undo(self, i):
                return True, "ok"

            def set_enabled(self, v, log=False):
                pass
        self.server = Server(StubApp())
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.token = self.cfg["token"]

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def get(self, path, token=True, host=None):
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header("X-FocusFlow-Token", self.token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_ping_is_public_but_every_data_route_needs_the_token(self):
        self.assertEqual(self.get("/api/ping", token=False)[0], 200)
        for path in ("/api/stats", "/api/sessions", "/api/export?format=csv", "/api/events"):
            self.assertEqual(self.get(path, token=False)[0], 401, path)
            self.assertEqual(self.get(path)[0], 200, path)

    def test_dns_rebinding_hosts_are_refused(self):
        self.assertEqual(self.get("/api/ping", host="evil.example.com")[0], 403)

    def test_sessions_endpoint_reports_the_stored_runs(self):
        self.store.add_session({"mode": MODE_NAMES[0], "completed": 1, "seconds": 20, "raw_distance": 5000,
                                "stabilised_distance": 5000, "targets": 10}, 1, 21)
        data = json.loads(self.get("/api/sessions")[1])
        self.assertEqual(len(data["sessions"]), 1)
        self.assertIn(MODE_NAMES[0], data["modes"])

    def test_export_endpoint_serves_downloads_in_all_three_formats(self):
        self.store.add_session({"mode": MODE_NAMES[0], "completed": 1, "seconds": 20, "raw_distance": 5000,
                                "stabilised_distance": 5000, "targets": 10}, 1, 21)
        code, body, h = self.get("/api/export?format=csv")
        self.assertTrue(body.startswith(b"id,started,mode"))
        self.assertIn("attachment", h["Content-Disposition"])
        self.assertIn("focusflow-", h["Content-Disposition"])
        self.assertEqual(self.get("/api/export?format=pdf")[1][:5], b"%PDF-")
        self.assertIn("sessions", json.loads(self.get("/api/export?format=json")[1]))
        self.assertEqual(self.get("/api/export?format=docx")[0], 400)

    def test_dashboard_page_embeds_the_token_and_has_no_cors_headers(self):
        code, body, h = self.get("/", token=False)
        self.assertEqual(code, 200)
        self.assertIn(self.token.encode(), body)
        self.assertIsNone(h["Access-Control-Allow-Origin"])


if __name__ == "__main__":
    unittest.main()
