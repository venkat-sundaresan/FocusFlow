"""The calibration wizard window. All measuring and decision logic lives in `calibration.py` (pure and tested);
this file only draws the steps and feeds that logic real pointer data.

Flow: intro -> hold still on a ring (8 s) -> optional click task (10 targets) -> result you can save.
While measuring, every assistance feature is masked off (the same mask as the "Normal mouse operation" experiment
mode) so the recording is of the unassisted hand; it is restored the moment measuring ends, however the wizard exits.
Nothing here is saved unless you press Save."""
import math
import random
import time
import tkinter as tk

from . import calibration as cal
from .widgets import ACCENT, ACCENT_DK, BG, BORDER, CARD, CARD2, FAINT, FG, MUTED, RED, Btn, Panel, font, pill, px

MEASURE_MODE = "Normal mouse operation"
PROFILE_NAME = "My calibration"


class CalibrationWindow(Panel):
    name = "calibrate"
    W, H = 860, 470
    REACH_TARGETS = 10
    TARGET_PX = 46             # logical px
    MAX_MISSES_PER_TARGET = 4  # then move on, so a hand that cannot hit a target is not trapped
    TICK_MS = 50

    def __init__(self, ui, cursor_pos=None, clock=None):
        super().__init__(ui)
        self._cursor_pos = cursor_pos
        self._clock = clock or (lambda: time.perf_counter() * 1000.0)
        self.measuring = False
        self.phase = None
        self._tick_job = None

    # -- plumbing -------------------------------------------------------------------------------------------
    @property
    def engine(self):
        return self.ui.app.engine

    def cursor_pos(self):
        if self._cursor_pos is None:
            from . import winapi
            self._cursor_pos = winapi.get_cursor_pos
        return self._cursor_pos()

    def _alive(self):
        return self.win is not None and self.win.winfo_exists()

    def _measure_on(self):
        if not self.measuring:
            self.engine.begin_session(MEASURE_MODE)
            self.measuring = True

    def _measure_off(self):
        if self.measuring:
            self.measuring = False
            self.engine.end_session()

    def _cancel_tick(self):
        if self._tick_job is not None and self._alive():
            try:
                self.win.after_cancel(self._tick_job)
            except tk.TclError:
                pass
        self._tick_job = None

    def _schedule(self, fn):
        self._tick_job = self.win.after(self.TICK_MS, fn) if self._alive() else None

    # -- window ---------------------------------------------------------------------------------------------
    def start(self):
        self._measure_off()
        self._cancel_tick()
        self.destroy()
        self.can_click, self.hold, self.reach, self.rec = True, None, None, None
        win = self.win = self._new()
        win.title("FocusFlow · Calibrate to my hand")
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.stop)
        win.bind("<Escape>", lambda e: self.stop())
        top = tk.Frame(win, bg=BG, padx=18, pady=12)
        top.pack(fill="x")
        self.pill = pill(top, "Calibration", ACCENT)
        self.pill.pack(side="left")
        self.title_lbl = tk.Label(top, text="", font=font(14, bold=True), bg=BG, fg=FG)
        self.title_lbl.pack(side="left", padx=18)
        self.canvas = tk.Canvas(win, width=px(self.W), height=px(self.H), bg=CARD, highlightthickness=1,
                                highlightbackground=BORDER)
        self.canvas.pack(padx=18, pady=(0, 6))
        self.bar = tk.Frame(win, bg=BG, padx=18)
        self.bar.pack(fill="x", pady=(0, 12))
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{max(0, (sw - win.winfo_reqwidth()) // 2)}+{max(0, (sh - win.winfo_reqheight()) // 2)}")
        self.show()
        win.focus_force()
        self._phase_intro()

    def stop(self):
        self._cancel_tick()
        self._measure_off()
        if self.win is not None:
            self.destroy()

    def _reset_phase(self, title, buttons):
        """Fresh canvas and button row for the next step. buttons: [(label, command, kind)]."""
        self._cancel_tick()
        self.canvas.unbind("<Button-1>")
        self.canvas.delete("all")
        for child in self.bar.winfo_children():
            child.destroy()
        self._reg = []
        self._pub_sig = None
        self.title_lbl.config(text=title)
        for label, command, kind in buttons:
            b = Btn(self.bar, label, command, kind, 12, 20, 9)
            b.pack(side="left", padx=(0, 12))
            self.reg(b, label, command)

    def _say(self, lines, y0=70, size=13, color=FG, gap=34):
        for i, text in enumerate(lines):
            self.canvas.create_text(px(self.W) / 2, px(y0 + i * gap), text=text, font=font(size), fill=color,
                                    width=px(self.W - 80), justify="center")

    # -- steps ----------------------------------------------------------------------------------------------
    def _phase_intro(self):
        self.phase = "intro"
        self._reset_phase("Calibrate to my hand", [
            ("Start (I can click)", lambda: self._begin(True), "primary"),
            ("Start (clicking is hard)", lambda: self._begin(False), "ghost"),
            ("Cancel", self.stop, "quiet")])
        self._say(["This takes about a minute and has two short steps.",
                   "1.  Rest the pointer on a ring and hold your hand as still as you can for 8 seconds.",
                   "2.  Click ten targets (skipped if clicking is hard for you).",
                   "FocusFlow turns its assistance off while measuring, then suggests settings you can save."],
                  y0=90, gap=48)
        self._say(["Nothing is saved unless you choose Save. This is a starting point, not a medical measurement."],
                  y0=330, size=10, color=MUTED)

    def _begin(self, can_click):
        self.can_click = can_click
        self._measure_on()
        self._phase_hold()

    def _center(self):
        return (self.canvas.winfo_rootx() + px(self.W) // 2, self.canvas.winfo_rooty() + px(self.H) // 2)

    def _phase_hold(self):
        self.phase = "hold"
        self._reset_phase("Step 1 of 2 · Hold still" if self.can_click else "Hold still",
                          [("Cancel", self.stop, "quiet")])
        self.collector = cal.HoldCollector(px(36))
        cx, cy, r = px(self.W) / 2, px(self.H) / 2, px(36)
        for k, w in ((1.0, 3), (2.2, 1)):
            self.canvas.create_oval(cx - r * k, cy - r * k, cx + r * k, cy + r * k, outline=ACCENT if k == 1.0 else BORDER,
                                    width=w, dash=() if k == 1.0 else (4, 4))
        self.arc = self.canvas.create_arc(cx - r - px(10), cy - r - px(10), cx + r + px(10), cy + r + px(10),
                                          style="arc", start=90, extent=0, outline=ACCENT, width=px(6))
        self.status = self.canvas.create_text(px(self.W) / 2, px(60), text="", font=font(14, bold=True), fill=FG,
                                              width=px(self.W - 80), justify="center")
        self._status_text = ""
        self._note = ""
        self._hold_tick()

    def _set_status(self, text):
        if text != self._status_text:
            self._status_text = text
            self.canvas.itemconfigure(self.status, text=text)

    def _hold_tick(self):
        if not self._alive() or self.phase != "hold":
            return
        c = self.collector
        now = self._clock()
        trace = self.engine.pipe.snapshot(since_ms=c.t0)[0] if c.t0 is not None else ()
        before = c.restarts
        state = c.update(now, self.cursor_pos(), self._center(), trace)
        if c.restarts != before:
            self._note = "The pointer left the ring, so the wait started again. "
        if state == c.WAITING:
            self._set_status(self._note + "Move the pointer onto the ring and rest your hand.")
            self.canvas.itemconfigure(self.arc, extent=0)
        elif state == c.RECORDING:
            left = max(0, math.ceil(c.hold_s * (1 - c.progress)))
            self._set_status(f"Hold still…  {left} s")
            self.canvas.itemconfigure(self.arc, extent=-359.9 * c.progress)
        else:
            return self._hold_done()
        self._schedule(self._hold_tick)

    def _hold_done(self):
        try:
            self.hold = self.collector.result(self.engine.pipe.scale)
        except cal.CalibrationError as exc:
            return self._phase_failed(str(exc))
        if self.can_click:
            self._phase_reach()
        else:
            self._phase_result()

    # click task ------------------------------------------------------------------------------------------
    def _phase_reach(self):
        self.phase = "reach"
        self._reset_phase("Step 2 of 2 · Click the targets", [("Cancel", self.stop, "quiet")])
        self.clicks, self.target_no, self.misses_here, self.rect = [], 0, 0, None
        self.size = px(self.TARGET_PX)
        self.prev = (px(self.W) / 2, px(self.H) / 2)
        self.step_lbl = self.canvas.create_text(px(self.W) / 2, px(26), text="", font=font(12), fill=MUTED)
        self.canvas.bind("<Button-1>", self._reach_click)
        self._next_target()

    def _next_target(self):
        c, s = self.canvas, self.size
        if self.target_no >= self.REACH_TARGETS:
            return self._reach_done()
        margin = s + px(40)
        for _ in range(50):                       # a target well away from the last one, inside the canvas
            x = random.uniform(margin, px(self.W) - margin)
            y = random.uniform(margin + px(20), px(self.H) - margin)
            if math.hypot(x - self.prev[0], y - self.prev[1]) >= px(180):
                break
        self.prev = (x, y)
        c.delete("target")
        c.create_rectangle(x - s / 2, y - s / 2, x + s / 2, y + s / 2, fill=ACCENT_DK, outline=ACCENT, width=2,
                           tags="target")
        c.create_text(x, y, text=str(self.target_no + 1), font=font(16, bold=True), fill=FG, tags="target")
        self.rect = (x - s / 2, y - s / 2, x + s / 2, y + s / 2)
        self.misses_here = 0
        c.itemconfigure(self.step_lbl, text=f"Target {self.target_no + 1} of {self.REACH_TARGETS}")

    def _reach_click(self, event):
        if self.phase != "reach" or self.rect is None:
            return
        ox, oy = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        l, t, r, b = self.rect
        screen_rect = (ox + l, oy + t, ox + r, oy + b)
        d = cal.rect_distance((event.x_root, event.y_root), screen_rect)
        hit = d == 0
        self.clicks.append({"t": self._clock() / 1000.0, "hit": hit, "miss_px": d})
        if hit:
            self.target_no += 1
            return self._next_target()
        self.misses_here += 1
        mark = self.canvas.create_text(event.x, event.y, text="×", font=font(18, bold=True), fill=RED)
        self.win.after(450, lambda: self._alive() and self.canvas.delete(mark))
        if self.misses_here >= self.MAX_MISSES_PER_TARGET:
            self.target_no += 1
            self._next_target()

    def _reach_done(self):
        try:
            self.reach = cal.analyze_reach(self.clicks)
        except cal.CalibrationError as exc:
            return self._phase_failed(str(exc))
        self._phase_result()

    # outcome ---------------------------------------------------------------------------------------------
    def _phase_failed(self, message):
        self._measure_off()
        self.phase = "failed"
        self._reset_phase("That didn't work", [("Try again", self.start, "primary"), ("Close", self.stop, "ghost")])
        self._say([message], y0=150, size=14)

    def _phase_result(self):
        self._measure_off()                       # assistance is back on before the person reads or presses anything
        self.phase = "result"
        self.rec = cal.recommend(self.hold, self.reach, self.can_click, self.engine.pipe.scale)
        self._reset_phase("Your suggested settings", [
            (f"Save as “{PROFILE_NAME}”", self.save, "primary"),
            ("Run again", self.start, "ghost"),
            ("Close without saving", self.stop, "quiet")])
        self._say(self.rec.reasons, y0=60, size=12, gap=62)
        m = self.rec.metrics
        bits = [f"wander {m['raw_rms_px']} px → {m['residual_rms_px']} px with the suggested smoothing",
                f"tremor ≈ {m['approx_hz']} Hz"]
        if "miss_rate" in m:
            bits.append(f"{m['miss_rate']:.0%} of clicks missed")
        self.canvas.create_text(px(self.W) / 2, px(self.H - 40), text="   ·   ".join(bits), font=font(10), fill=FAINT)

    def save(self):
        name = cal.apply_recommendation(self.ui.app.cfg, self.rec, PROFILE_NAME)
        self.ui.toast(f"Saved and switched to “{name}”" if name else "Could not save the profile")
        self.stop()
