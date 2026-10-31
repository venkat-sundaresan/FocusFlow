"""Wires everything together and owns the system-tray icon."""
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__, profiles, winapi
from .config import APP_DIR, Settings
from .engine import Engine
from .export import export_all
from .server import Server
from .store import Store
from .switchnav import SwitchNav
from .ui import UI
from .undo import Undo

SETTING_COALESCE_S = 8.0
EXPORT_DIR = Path.home() / "Documents" / "FocusFlow" / "exports"


class App:
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._redirect_output()
        self.cfg = Settings()
        self.store = Store(APP_DIR / "activity.db")
        self.ui = UI(self)
        self.engine = Engine(self.cfg, self.store, self.ui.local)
        self.switch = SwitchNav(self.cfg, self.engine, self.ui.local, on_change=lambda t: None)
        self.undoer = Undo(self.cfg, self.store, self)
        self.server = Server(self)
        self.hook = winapi.MouseHook(self.engine.on_mouse)
        self.hotkeys = None
        self.tray = None
        self.voice = None
        self.suggestion_pending = False
        self._setting_events = {}
        self.engine.on_fault = self._on_fault
        self.engine.on_suggest = self._on_suggest
        self.engine.on_adapted = lambda label: self.ui.toast(f"“{label}” now has a larger hit area", 2600)
        self.cfg.on_change(self._on_setting)

    @staticmethod
    def _redirect_output():
        # pythonw has no console; keep tracebacks in a log file instead of losing them
        try:
            log = open(APP_DIR / "focusflow.log", "a", buffering=1, encoding="utf-8")
            sys.stderr = log
            if sys.stdout is None:
                sys.stdout = log
        except OSError:
            pass

    # -- status ------------------------------------------------------------------------------------------------
    def status(self):
        e = self.engine
        return {"enabled": self.cfg["enabled"], "version": __version__, "uia": e.uia.ok,
                "tremor": e.status["tremor"], "speed": e.status["speed"], "target": e.status["target"],
                "profile": e.current_profile(), "jitter": e.jitter_on(), "pipeline_fault": e.pipe_faulted,
                "extension_connected": e.ext.rect is not None and time.time() - e.ext.ts < e.ext.TTL_S}

    # -- on/off, emergency, profiles -----------------------------------------------------------------------------
    def _ensure_hook(self):
        if not self.hook.is_alive():
            self.hook = winapi.MouseHook(self.engine.on_mouse)
            self.hook.start()

    def set_enabled(self, value, log=False):
        prev = self.cfg["enabled"]
        if value == prev:
            return
        if value:
            self._ensure_hook()
        self.engine.set_enabled(value)
        self.ui.toast("FocusFlow is ON" if value else "FocusFlow is paused")
        self._refresh_tray()
        if log:
            self.store.add_event("toggle", "desktop", "ON" if value else "OFF", {},
                                 undo={"type": "enabled", "previous": prev})

    def toggle_enabled(self):
        self.set_enabled(not self.cfg["enabled"], log=True)

    def emergency_stop(self):
        """Turn every pointer intervention off *and* remove the mouse hook, so the pointer is plain Windows."""
        self.engine.set_enabled(False)
        try:
            self.hook.stop()
        except Exception:
            pass
        self.switch.stop_auto()
        self.store.add_event("emergency_stop", "desktop", "Emergency stop", {})
        self.ui.toast("Assistance stopped — pointer is back to normal. Ctrl+Alt+F resumes.", 3500)
        self._refresh_tray()

    def activate_profile(self, name):
        if profiles.apply_profile(self.cfg, name):
            self.engine._app_override = None
            self.store.add_event("profile_change", "desktop", name, {"profile": name})
            self.ui.toast(f"Profile: {name}")
            self._refresh_tray()

    def cycle_profile(self):
        self.activate_profile(profiles.next_profile(self.cfg))

    # suggestion from the erratic-movement heuristic (never a diagnosis) ---------------------------------------------
    def _on_suggest(self):
        self.suggestion_pending = True
        self.ui.confirm("Steadier pointer?",
                        "FocusFlow noticed repeated short direction changes in your pointer movement. This is a simple "
                        "heuristic, not a medical assessment. Stronger smoothing may help. Switch to Maximum Assistance?",
                        "Yes, try it", "Not now",
                        lambda ok: self.accept_suggestion() if ok else self.dismiss_suggestion())

    def accept_suggestion(self):
        self.suggestion_pending = False
        self.activate_profile("Maximum Assistance")

    def dismiss_suggestion(self):
        self.suggestion_pending = False

    def _on_fault(self, where):
        self.ui.toast("Pointer stabilising stopped after an error — see focusflow.log", 4000)

    # -- other actions -------------------------------------------------------------------------------------------
    def open_dashboard(self):
        webbrowser.open(f"http://127.0.0.1:{self.server.port}/")

    def session_saved(self):
        self.ui.call(lambda: self.ui.main.pages.get("analytics") and setattr(self.ui.main.pages["analytics"], "_sig", None))

    def export(self, fmt, demo=False):
        path = export_all(self.store, EXPORT_DIR, fmt, demo)
        self.store.add_event("setting_change", "desktop", "export", {"setting": "export", "value": fmt})
        return path

    def undo(self, event_id):
        return self.undoer.undo(event_id)

    def undo_last(self):
        ev = self.store.last_undoable()
        if ev is None:
            return self.ui.toast("Nothing FocusFlow can safely undo")
        what = f"{ev['kind'].replace('_', ' ')}: {ev['target'] or ''}"

        def done(ok):
            if ok:
                _, msg = self.undo(ev["id"])
                self.ui.toast(msg, 3500)

        self.ui.confirm("Undo this?", what, "Undo", "Keep", done)

    def change_setting(self, key, value):
        if key == "autostart" and value:
            def done(ok):
                if ok:
                    winapi.set_autostart(True)
                    self.cfg.set("autostart", True)
            return self.ui.confirm("Start with Windows?", "FocusFlow will start quietly in the system tray "
                                   "each time you sign in. You can turn this off at any time.",
                                   "Yes, start it", "No", done)
        prev = self.cfg[key]
        if prev == value:
            return
        if key == "autostart":
            winapi.set_autostart(False)
        self.cfg.set(key, value)
        # fold rapid +/- presses on one setting into a single log entry, keeping the original value for undo
        old = self._setting_events.get(key)
        now = time.time()
        if old and now - old[1] < SETTING_COALESCE_S:
            self.store.update_detail(old[0], {"setting": key, "value": value})
            self._setting_events[key] = (old[0], now)
        else:
            eid = self.store.add_event("setting_change", "desktop", key, {"setting": key, "value": value},
                                       undo={"type": "settings", "previous": {key: prev}})
            self._setting_events[key] = (eid, now)

    def _on_setting(self, key):
        if key == "osk_enabled":
            self.ui.show_keyboard(bool(self.cfg["osk_enabled"]))
        elif key == "voice_enabled":
            self._sync_voice()
        elif key in ("switch_enabled", "switch_auto_scan"):
            if self.cfg["switch_enabled"] and self.cfg["switch_auto_scan"]:
                self.switch.start_auto()
            else:
                self.switch.stop_auto()

    def extension_config(self):
        c, e = self.cfg, self.engine
        return {"enabled": bool(c["enabled"] and c["extension_enabled"] and e.dwell_on()),
                "dwell_ms": c["dwell_ms"], "cooldown_ms": c["dwell_cooldown_ms"],
                "assist_factor": c["assist_dwell_factor"], "hover_speed": c["hover_speed_px_s"],
                "tolerance_px": c["tolerance_px"], "sensitivity": c["sensitivity"],
                "snap_enabled": e.snap_on(), "snap_radius": c["snap_radius_px"],
                "near_miss_correct": e.correct_on(), "miss_radius": c["miss_radius_px"],
                "log_names": c["log_target_names"]}

    # -- voice ---------------------------------------------------------------------------------------------------
    def _sync_voice(self):
        want = bool(self.cfg["voice_enabled"])
        if want and (self.voice is None or not self.voice.is_alive()):
            from .voice import VoiceListener
            self.voice = VoiceListener(self.voice_action)
            self.voice.start()
        elif not want and self.voice is not None:
            self.voice.stop()
            self.voice = None

    def voice_status_text(self):
        if not self.cfg["voice_enabled"]:
            return "Off. Needs a Windows speech-recognition language and a microphone."
        if self.voice is None:
            return "Starting…"
        heard = f"   Last heard: “{self.voice.heard[-1]}”" if self.voice.heard else ""
        return f"Status: {self.voice.status}.{heard}"

    def voice_action(self, action, phrase):
        """Called on the voice thread when a command is recognised."""
        if action == "pause":
            self.set_enabled(False, log=True)
        elif action == "resume":
            self.set_enabled(True, log=True)
        elif action == "emergency":
            self.emergency_stop()
        elif action == "click":
            winapi.send_click(None, None)
        elif action == "double_click":
            winapi.send_click(None, None, double=True)
        elif action == "right_click":
            winapi.send_click(None, None, button="right")
        elif action == "scroll_up":
            winapi.send_wheel(3)
        elif action == "scroll_down":
            winapi.send_wheel(-3)
        elif action == "next_profile":
            self.cycle_profile()
        elif action.startswith("profile:"):
            self.activate_profile(action.split(":", 1)[1])
        elif action == "keyboard_show":
            self.change_setting("osk_enabled", True)
        elif action == "keyboard_hide":
            self.change_setting("osk_enabled", False)
        elif action == "open_lab":
            self.ui.show_window("lab")
        elif action == "open_window":
            self.ui.show_window()
        self.ui.toast(f"“{phrase}”", 1200)

    # -- tray ----------------------------------------------------------------------------------------------------
    def _tray_icon(self):
        from PIL import Image, ImageDraw
        on = self.cfg["enabled"]
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=(45, 212, 167, 255) if on else (122, 133, 148, 255))
        if on:
            d.ellipse((20, 20, 44, 44), outline=(255, 255, 255, 255), width=5)
        else:
            d.rectangle((21, 19, 29, 45), fill=(255, 255, 255, 255))
            d.rectangle((35, 19, 43, 45), fill=(255, 255, 255, 255))
        return img

    def _refresh_tray(self):
        if self.tray is not None:
            self.tray.icon = self._tray_icon()
            self.tray.title = (f"FocusFlow - {self.engine.current_profile()}" if self.cfg["enabled"]
                               else "FocusFlow - paused")
            self.tray.update_menu()

    def _start_tray(self):
        import pystray
        item = pystray.MenuItem

        def activate_cb(n):                       # pystray inspects callback arity, so no default-arg lambdas
            return lambda icon, entry: self.activate_profile(n)

        def checked_cb(n):
            return lambda entry: self.cfg["active_profile"] == n

        def profile_items():
            for name in profiles.all_profiles(self.cfg):
                yield item(name, activate_cb(name), checked=checked_cb(name), radio=True)

        menu = pystray.Menu(
            item("Open FocusFlow", lambda i, it: self.ui.show_window(), default=True),
            item(lambda i: "Pause assistance" if self.cfg["enabled"] else "Resume assistance",
                 lambda i, it: self.toggle_enabled()),
            item("Profile", pystray.Menu(profile_items)),
            item("Live Lab", lambda i, it: self.ui.show_window("lab")),
            item("On-screen keyboard", lambda i, it: self.change_setting("osk_enabled", not self.cfg["osk_enabled"]),
                 checked=lambda it: bool(self.cfg["osk_enabled"])),
            item("Web dashboard", lambda i, it: self.open_dashboard()),
            item("Undo last action...", lambda i, it: self.undo_last()),
            pystray.Menu.SEPARATOR,
            item("Emergency stop", lambda i, it: self.emergency_stop()),
            item("Quit FocusFlow", lambda i, it: self.quit()),
        )
        self.tray = pystray.Icon("FocusFlow", self._tray_icon(), "FocusFlow", menu)
        self.tray.run_detached()

    # -- lifecycle -----------------------------------------------------------------------------------------------
    def _bind_hotkeys(self):
        hk, ui = self.cfg["hotkeys"], self.ui
        binds = [(hk["toggle"], self.toggle_enabled), (hk["emergency"], self.emergency_stop),
                 (hk["window"], ui.toggle_window), (hk["profile"], self.cycle_profile),
                 (hk["lab"], lambda: ui.show_window("lab")),
                 (hk["keyboard"], lambda: self.change_setting("osk_enabled", not self.cfg["osk_enabled"])),
                 (hk["settings"], lambda: ui.show_window("settings")), (hk["undo"], self.undo_last),
                 (hk["switch_next"], self.switch.next), (hk["switch_select"], self.switch.select)]
        self.hotkeys = winapi.HotkeyThread(binds)
        self.hotkeys.start()

    def quit(self):
        def done(ok):
            if ok:
                self._shutdown()
        self.ui.confirm("Quit FocusFlow?", "Pointer stabilising and dwell clicking will stop.", "Quit", "Keep running", done)

    def _shutdown(self):
        try:
            self.engine.flush_stats_now()
            self.store.flush()
        except Exception:
            pass
        for fn in (self.engine.stop, self.hook.stop, lambda: self.hotkeys and self.hotkeys.stop(),
                   self.switch.stop_auto, lambda: self.voice and self.voice.stop(),
                   self.server.stop, lambda: self.tray and self.tray.stop()):
            try:
                fn()
            except Exception:
                pass
        self.ui.quit()

    def run(self):
        self.server.start()
        (APP_DIR / "port.txt").write_text(str(self.server.port))
        self.ui.start()
        self.engine.start()
        self.hook.start()
        self._bind_hotkeys()
        self._start_tray()
        if self.cfg["osk_enabled"]:
            self.ui.show_keyboard(True)
        if self.cfg["voice_enabled"]:
            self._sync_voice()
        if self.cfg["switch_enabled"] and self.cfg["switch_auto_scan"]:
            self.switch.start_auto()
        if self.cfg["show_window_on_start"]:
            self.ui.show_window()
        if not self.cfg["autostart_asked"]:
            self.cfg.set("autostart_asked", True)
            self.ui.root.after(1500, lambda: self.change_setting("autostart", True))
        self.ui.run()


def main():
    winapi.set_dpi_aware()
    _handle, first = winapi.single_instance()
    if not first:
        return
    if "--selftest" in sys.argv:
        return selftest()
    App().run()
    os._exit(0)


def selftest():
    print("FocusFlow", __version__, "- core modules import fine")
