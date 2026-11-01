"""UI smoke test: builds the real App objects (no mouse hook, no tray, no server) and drives every page, the overlay,
the on-screen keyboard and the practice task, failing on any exception. Needs a desktop session."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
HOME = tempfile.mkdtemp(prefix="ffui")
os.environ["FOCUSFLOW_HOME"] = HOME          # before focusflow.config is imported

HAVE_DISPLAY = False
if sys.platform == "win32":
    try:
        import tkinter
        tkinter.Tk().destroy()
        HAVE_DISPLAY = True
    except Exception:
        pass


@unittest.skipUnless(HAVE_DISPLAY, "needs a Windows desktop session")
class UiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from focusflow import winapi
        from focusflow.app import App
        # Discovery may already have imported config; never use the user's actual settings/database.
        for target in ("focusflow.app.APP_DIR", "focusflow.config.APP_DIR"):
            patcher = mock.patch(target, Path(HOME))
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        winapi.set_dpi_aware()
        cls.app = App()
        cls.ui = cls.app.ui
        cls.ui.start()
        cls.pump()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.engine.stop()
            cls.ui.root.destroy()
        except Exception:
            pass

    @classmethod
    def pump(cls, n=6):
        for _ in range(n):
            cls.ui.root.update()

    def test_every_page_builds_refreshes_and_publishes_targets(self):
        from focusflow.ui import NAV
        self.ui.main.open()
        self.pump()
        for key in [k for k, *_ in NAV] + ["settings"]:
            self.ui.main.go(key)
            self.pump()
            self.ui.main.pages[key].refresh()
            if key == "lab":
                self.ui.main.pages["lab"].frame()
        self.ui.main._publish()
        self.assertGreater(len(self.ui.local.all()), 10, "controls must be published as dwell targets")
        self.ui.main.hide()

    def test_system_tray_starts_and_stops(self):
        try:
            self.app._start_tray()
            self.assertTrue(self.app.tray.visible)
        finally:
            if self.app.tray is not None:
                self.app.tray.stop()
                self.app.tray = None

    def test_live_ml_controls_start_disabled_in_shadow_mode(self):
        self.ui.main.open("assist")
        self.pump()
        page = self.ui.main.pages["assist"]
        page.refresh()
        self.assertEqual(set(page.ml_mode.buttons), {"shadow", "experimental"})
        self.assertFalse(self.app.cfg["ml_enabled"])
        self.assertEqual(self.app.cfg["ml_mode"], "shadow")
        self.assertIsNone(self.app.engine.ml.thread)
        self.assertIn("off", page.ml_label.cget("text"))

    def test_profile_cards_rebuild_when_a_custom_profile_is_saved(self):
        from focusflow import profiles
        self.ui.main.open("profiles")
        self.pump()
        profiles.save_custom(self.app.cfg, "Evening")
        self.ui.main.pages["profiles"].refresh()
        self.pump()
        names = [w for w in self.ui.main.pages["profiles"].cards.winfo_children()]
        self.assertEqual(len(names), 4)
        profiles.delete_custom(self.app.cfg, "Evening")
        self.ui.main.pages["profiles"].refresh()
        self.ui.main.hide()

    def test_overlay_draws_large_cursor_trail_and_highlight(self):
        from focusflow.engine import OverlayState
        self.app.cfg.set_many({"large_cursor": True, "pointer_trails": True, "highlight_targets": True})
        ov = self.ui.overlay
        for i in range(5):
            ov.update(OverlayState((200 + i * 9, 300), 0.4, (180, 280, 260, 320), False, 0.1, False))
            self.pump(2)
        self.assertTrue(ov.shown)
        state = ov.canvas.itemcget(ov.cursor, "state")
        self.assertEqual(state, "normal")
        ov.update(OverlayState((200, 300), 0.4, (180, 280, 260, 320), True, 0.1, True))   # assisted + adapted target
        self.app.cfg.set_many({"large_cursor": False, "pointer_trails": False, "highlight_targets": False})
        ov.update(None)
        self.assertFalse(ov.shown)

    def test_no_box_is_drawn_around_a_hovered_target_unless_highlighting_is_switched_on(self):
        from focusflow.engine import OverlayState
        self.app.cfg.set_many({"highlight_targets": False, "large_cursor": False, "pointer_trails": False})
        ov = self.ui.overlay
        state = OverlayState((200, 300), 0.5, (180, 280, 260, 320), False, 0.1, False)
        ov.update(state)
        self.pump(2)
        self.assertEqual(ov.canvas.itemcget(ov.rect, "state"), "hidden")      # only the small dwell gauge shows
        self.assertEqual(ov.canvas.itemcget(ov.arc, "state"), "normal")
        self.app.cfg.set("highlight_targets", True)                           # the opt-in accessibility aid
        ov.update(state)
        self.assertEqual(ov.canvas.itemcget(ov.rect, "state"), "normal")
        self.app.cfg.set("highlight_targets", False)
        ov.update(None)

    def test_on_screen_keyboard_builds_and_publishes_dwell_keys(self):
        self.ui.keyboard.open()
        self.pump()
        self.ui.keyboard._publish()
        keys = [t for t in self.ui.local.all() if t.key.startswith("keyboard:")]
        self.assertGreater(len(keys), 30)
        self.assertTrue(all(t.dwell_ms == self.app.cfg["osk_dwell_ms"] for t in keys))
        self.ui.keyboard.close_quiet()

    def test_practice_task_runs_to_completion_and_saves_a_session(self):
        from focusflow.profiles import MODE_NAMES
        task = self.ui.task
        before = len(self.app.store.sessions())
        task.start(MODE_NAMES[1])
        self.pump()
        self.assertEqual(self.app.engine.mask, {"jitter": True, "dwell": False, "adapt": False})
        for _ in range(len(task.ROUTE)):
            tid = task._expected()
            task._activate(tid, "mouse")
            self.pump(2)
        self.assertTrue(task.done)
        self.assertEqual(len(self.app.store.sessions()), before + 1)
        rec = self.app.store.sessions(1)[0]
        self.assertEqual(rec["mode"], MODE_NAMES[1])
        self.assertEqual(rec["successes"], len(task.ROUTE))
        self.assertEqual(self.app.engine.mask, {"jitter": True, "dwell": True, "adapt": True})   # restored
        task.stop()

    def test_stopping_a_task_early_saves_nothing_and_restores_assistance(self):
        from focusflow.profiles import MODE_NAMES
        task = self.ui.task
        before = len(self.app.store.sessions())
        task.start(MODE_NAMES[0])
        self.pump()
        task._activate(task._expected(), "mouse")
        task.stop()
        self.assertEqual(len(self.app.store.sessions()), before)
        self.assertEqual(self.app.engine.mask, {"jitter": True, "dwell": True, "adapt": True})
        self.assertIsNone(self.app.engine.session)


if __name__ == "__main__":
    unittest.main()
