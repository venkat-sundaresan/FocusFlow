"""The five sections of the FocusFlow window (+ settings). Every control is dwell-targetable via `win.reg`."""
import math
import time
import tkinter as tk

from . import profiles, winapi
from .profiles import MODE_NAMES
from .widgets import (px, ACCENT, ACCENT_DK, AMBER, BG, BLUE, BORDER, CARD, CARD2, FAINT, FG, MUTED, RAW, RED, STAB,
                      VIOLET, Btn, Segmented, Stepper, Switch, card, draw_bars, draw_line, font, pill, row, tile)

OWN_EXES = {"python.exe", "pythonw.exe", "focusflow.exe"}


class Page(tk.Frame):
    title, subtitle = "", ""

    def __init__(self, parent, win):
        super().__init__(parent, bg=BG)
        self.win, self.app = win, win.ui.app
        self.cfg = self.app.cfg
        self.toggles, self.steppers = [], []
        self.build()

    def build(self):
        raise NotImplementedError

    def refresh(self):
        for w in self.toggles + self.steppers:
            w.refresh()

    # helpers -------------------------------------------------------------------------------------------
    def setting_switch(self, parent, key, label, hint=None):
        right = row(parent, label, hint)
        sw = Switch(right, lambda: self.cfg[key], lambda v: self.app.change_setting(key, v))
        sw.pack()
        self.win.reg(sw, label, sw.toggle)
        self.toggles.append(sw)
        return sw

    def setting_step(self, parent, key, label, step, lo, hi, fmt=str, hint=None):
        right = row(parent, label, hint)
        st = Stepper(right, self.win, "", lambda: self.cfg[key], lambda v: self.app.change_setting(key, v),
                     step, lo, hi, fmt)
        st.pack()
        self.steppers.append(st)
        return st

    def grid_cards(self, specs, cols=2, minh=0):
        for i in range(cols):
            self.grid_columnconfigure(i, weight=1, uniform="c")
        for i, spec in enumerate(specs):
            r, c = divmod(i, cols)
            span = 1
            if isinstance(spec, tuple):
                spec, span = spec
            spec.grid(row=r + 1, column=c, columnspan=span, sticky="nsew", padx=(0 if c == 0 else 8, 0 if c == cols - 1 else 8),
                      pady=(0, 14))


# ============================================================================================================
class PointerAssistPage(Page):
    title = "Pointer Assist"
    subtitle = "Steady the real Windows pointer, help you reach targets, and protect against accidental clicks."

    def build(self):
        self.banner = tk.Frame(self, bg="#2A2412", highlightbackground="#6B5A1E", highlightthickness=1)
        self.banner_text = tk.Label(self.banner, text="", font=font(10), bg="#2A2412", fg=AMBER, anchor="w",
                                    justify="left", wraplength=px(640))
        self.banner_text.pack(side="left", padx=14, pady=10, fill="x", expand=True)
        b1 = Btn(self.banner, "Try Maximum Assistance", self.app.accept_suggestion, "primary", 10, 14, 6)
        b1.pack(side="right", padx=(0, 6), pady=8)
        b2 = Btn(self.banner, "Dismiss", self.app.dismiss_suggestion, "ghost", 10, 14, 6)
        b2.pack(side="right", padx=6, pady=8)
        self.win.reg(b1, "Try Maximum Assistance", self.app.accept_suggestion)
        self.win.reg(b2, "Dismiss", self.app.dismiss_suggestion)
        self.banner.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self.banner.grid_remove()

        c1, i1 = card(self, "Jitter reduction",
                      "The PointerLab speed-adaptive filter, on your real cursor. Tiny involuntary movements are "
                      "smoothed; quick deliberate movement stays responsive (lag never exceeds 8 px).")
        self.setting_switch(i1, "jitter_enabled", "Stabilise the pointer")
        self.setting_step(i1, "smoothing_strength", "Smoothing strength", 1, 0, 10, lambda v: f"{v} / 10",
                          "5 is balanced smoothing. Higher = steadier, slightly softer.")
        self.setting_switch(i1, "show_stabilization", "Show raw hand vs steadied pointer",
                            "Draws your shaky hand path in red and the steadied pointer path in green, with a link line.")
        c2, i2 = card(self, "Target attraction",
                      "A gentle magnetic pull toward one clearly-nearest button when you slow down near it. "
                      "Never over text or documents, never when two targets are close.")
        self.setting_step(i2, "attraction_strength", "Pull strength", 1, 0, 10, lambda v: "off" if v == 0 else f"{v} / 10")
        self.setting_step(i2, "attraction_radius_px", "Attraction radius", 4, 8, 100, lambda v: f"{v} px")
        c3, i3 = card(self, "Dwell clicking", "Hover on a button and it clicks itself, with a ring showing progress.")
        self.setting_switch(i3, "dwell_enabled", "Dwell click")
        self.setting_step(i3, "dwell_ms", "Dwell time", 100, 400, 5000, lambda v: f"{v / 1000:.1f} s")
        self.setting_step(i3, "dwell_cooldown_ms", "Repeat-click guard", 250, 500, 5000, lambda v: f"{v / 1000:.2f} s",
                          "A just-clicked target must be left before it can fire again.")
        c4, i4 = card(self, "Target adaptation",
                      "A target you narrowly miss several times in a row is enlarged (button or text link) and gets a larger hit area. Safe by design: only "
                      "one unambiguous nearby target, never text. Undo any time.")
        self.setting_switch(i4, "adaptation_enabled", "Learn from nearby misses")
        self.setting_switch(i4, "near_miss_correct", "Fix near-miss clicks")
        self.setting_switch(i4, "visual_enlarge", "Visibly enlarge repeatedly missed targets",
                            "Only targets you have just missed repeatedly are enlarged; nothing grows on a plain hover.")
        self.setting_step(i4, "adapt_misses", "Misses before enlarging", 1, 2, 6, lambda v: f"{v} in a row",
                          "Nearby misses on the same target, with no successful click between.")
        self.setting_step(i4, "miss_radius_px", "Hit-area radius", 4, 8, 80, lambda v: f"{v} px")
        r = row(i4, "Enlarged targets", None)
        self.adapt_lbl = tk.Label(r, text="0", font=font(11, bold=True), bg=CARD, fg=FG)
        self.adapt_lbl.pack(side="left", padx=10)
        ub = Btn(r, "Undo adaptation", self.undo_adaptation, "ghost", 10, 12, 5)
        ub.pack(side="left")
        self.win.reg(ub, "Undo adaptation", self.undo_adaptation)
        c5, i5 = card(self, "Snap & click protection", "Help dwell find a button you are close to, and absorb double-taps.")
        self.setting_switch(i5, "snap_enabled", "Snap dwell to nearby target")
        self.setting_step(i5, "snap_radius_px", "Snap radius", 4, 8, 100, lambda v: f"{v} px")
        self.setting_switch(i5, "bounce_filter", "Ignore bounce re-clicks")
        self.setting_step(i5, "bounce_ms", "Bounce window", 25, 25, 500, lambda v: f"{v} ms")
        c6, i6 = card(self, "Sensitivity", "How readily a hovering hand counts as 'still' for dwell.")
        self.setting_step(i6, "sensitivity", "Tremor sensitivity", 1, 1, 10, lambda v: f"{v} / 10")
        self.setting_step(i6, "tolerance_px", "Hover wander allowed", 2, 2, 40, lambda v: f"{v} px",
                          "Extra drift tolerated while dwelling; grows automatically with tremor.")
        self.grid_cards([c1, c2, c3, c4, c5, c6])
        ml_card, ml_inner = card(self, "Local ML (experimental)",
                                "Synthetic-trained motion classification. Shadow mode observes only. "
                                "Experimental mode may add up to two smoothing levels; it never changes clicks.", wrap=700)
        ml_card.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        self.setting_switch(ml_inner, "ml_enabled", "Enable local ML")
        self.ml_mode = Segmented(ml_inner, self.win, [("shadow", "Shadow"), ("experimental", "Experimental")],
                                 lambda: self.cfg["ml_mode"], lambda v: self.app.change_setting("ml_mode", v))
        self.ml_mode.pack(anchor="w", pady=8)
        choose = Btn(ml_inner, "Choose model folder", self.choose_ml_model, "ghost", 10, 12, 5)
        choose.pack(anchor="w")
        self.win.reg(choose, "Choose model folder", self.choose_ml_model)
        self.ml_label = tk.Label(ml_inner, text="ML off", font=font(10), bg=CARD, fg=MUTED,
                                 justify="left", anchor="w", wraplength=px(700))
        self.ml_label.pack(fill="x", pady=8)

    def choose_ml_model(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(parent=self.winfo_toplevel(), title="Select a classifier.json + backbone folder")
        if folder:
            self.app.change_setting("ml_model_path", folder)

    def undo_adaptation(self):
        n = self.app.engine.undo_adaptation()
        self.app.ui.toast(f"{n} enlarged target{'s' if n != 1 else ''} restored" if n else "Nothing was enlarged")

    def refresh(self):
        super().refresh()
        self.ml_mode.refresh()
        status = self.app.engine.ml_status()
        prediction = status["prediction"]
        label = prediction["label"] if prediction else "waiting"
        latency = f"{status['latency_ms']:.1f} ms" if status["latency_ms"] is not None else "—"
        text = (f"{status['state']} | {label} | inference: {latency} | added smoothing: {status['extra_smoothing']:.2f}\n"
                f"Model: {status['model']}")
        if status["manual_override"]:
            text += "\nManual override: toggle ML off/on to re-arm experimental adjustment."
        if status["error"]:
            text += "\n" + status["error"]
        self.ml_label.config(text=text)
        n = str(len(self.app.engine.miss.grown))
        if self.adapt_lbl.cget("text") != n:
            self.adapt_lbl.config(text=n)
        if self.app.suggestion_pending:
            self.banner_text.config(text="Repeated short direction changes were detected in your pointer movement. "
                                         "This is a simple heuristic, not a medical assessment — but stronger "
                                         "smoothing may make the pointer steadier.")
            self.banner.grid()
        else:
            self.banner.grid_remove()


# ============================================================================================================
class ProfilesPage(Page):
    title = "Adaptive Profiles"
    subtitle = "One tap to change how much help you get. Make your own, or set a profile per application."

    def build(self):
        self.grid_columnconfigure(0, weight=1)
        self.cards = tk.Frame(self, bg=BG)
        self.cards.grid(row=1, column=0, sticky="ew")
        for i in range(3):
            self.cards.grid_columnconfigure(i, weight=1, uniform="p")
        cc, ci = card(self, "Calibrate to my hand",
                      "A one-minute check: rest on a ring, then click ten targets. FocusFlow replays your own movement "
                      "through its filter and suggests smoothing, dwell time and hit-area size. Nothing is saved "
                      "until you say so.", wrap=520)
        cc.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        cb = Btn(ci, "Start calibration", self.start_calibration, "primary", 11, 18, 7)
        cb.pack(anchor="w", pady=(10, 0))
        self.win.reg(cb, "Start calibration", self.start_calibration)
        sc, si = card(self, "Save current settings as a custom profile",
                      "Tune Pointer Assist the way you like, name it, and it appears above. (Typing a name needs the "
                      "keyboard — or use the on-screen keyboard.)")
        sc.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        line = tk.Frame(si, bg=CARD)
        line.pack(fill="x", pady=(10, 0))
        self.name_var = tk.StringVar()
        self.entry = tk.Entry(line, textvariable=self.name_var, font=font(12), bg=CARD2, fg=FG, insertbackground=FG,
                              relief="flat", highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT, width=26)
        self.entry.pack(side="left", ipady=7, padx=(0, 10))
        sb = Btn(line, "Save profile", self.save, "primary", 11, 18, 7)
        sb.pack(side="left")
        self.win.reg(sb, "Save profile", self.save)
        rc, ri = card(self, "Application-specific profiles",
                      "When one of these applications is in front, its profile applies automatically; your chosen "
                      "profile returns when you leave it.")
        rc.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        self.rules = tk.Frame(ri, bg=CARD)
        self.rules.pack(fill="x", pady=(8, 0))
        arow = tk.Frame(ri, bg=CARD)
        arow.pack(fill="x", pady=(10, 0))
        self.add_btn = Btn(arow, "Add rule: switch to the app within 5 s", self.begin_capture, "ghost", 10, 14, 6)
        self.add_btn.pack(side="left")
        self.win.reg(self.add_btn, "Add application rule", self.begin_capture)
        self._sig = None
        self._capturing = False

    def start_calibration(self):
        self.app.ui.calibration.start()

    def save(self):
        name = profiles.save_custom(self.cfg, self.name_var.get())
        self.app.ui.toast(f"Saved profile “{name}”" if name else "Pick a name that isn't a built-in profile")
        self.name_var.set("")
        self.refresh()

    def begin_capture(self):
        if self._capturing:
            return
        self._capturing = True
        self._count(5)

    def _count(self, n):
        if n > 0:
            self.add_btn.config(text=f"Switch to the application now…  {n}")
            self.after(1000, lambda: self._count(n - 1))
            return
        exe = winapi.foreground_exe()
        self._capturing = False
        self.add_btn.config(text="Add rule: switch to the app within 5 s")
        if not exe or exe in OWN_EXES:
            return self.app.ui.toast("That was FocusFlow itself — try again and switch to another app")
        rules = dict(self.cfg["app_profiles"])
        rules[exe] = self.cfg["active_profile"]
        self.cfg.set("app_profiles", rules)
        self.app.ui.toast(f"{exe}: {rules[exe]}")
        self.refresh()

    def _rebuild(self):
        for w in self.cards.winfo_children():
            w.destroy()
        allp = profiles.all_profiles(self.cfg)
        active = self.cfg["active_profile"]
        for i, (name, vals) in enumerate(allp.items()):
            builtin = name in profiles.BUILTIN
            on = name == active
            f, inner = card(self.cards, None, None, bg=CARD2 if on else CARD)
            f.config(highlightbackground=ACCENT if on else BORDER, highlightthickness=2 if on else 1)
            f.grid(row=i // 3, column=i % 3, sticky="nsew", padx=(0 if i % 3 == 0 else 7, 0 if i % 3 == 2 else 7), pady=(0, 14))
            bg = CARD2 if on else CARD
            head = tk.Frame(inner, bg=bg)
            head.pack(fill="x")
            tk.Label(head, text=name, font=font(14, bold=True), bg=bg, fg=FG).pack(side="left")
            if on:
                modified = not profiles.matches(self.cfg, name)
                pill(head, "ACTIVE · modified" if modified else "ACTIVE", AMBER if modified else ACCENT).pack(side="right")
            blurb = profiles.BUILTIN[name]["blurb"] if builtin else "Your custom profile."
            tk.Label(inner, text=blurb, font=font(9), bg=bg, fg=MUTED, justify="left", wraplength=px(250), anchor="w") \
                .pack(fill="x", pady=(6, 4))
            tk.Label(inner, text=profiles.summary(vals), font=font(9), bg=bg, fg=STAB, justify="left", wraplength=px(250),
                     anchor="w").pack(fill="x", pady=(0, 10))
            btns = tk.Frame(inner, bg=bg)
            btns.pack(fill="x")
            ab = Btn(btns, "Use this profile" if not on else "Reapply", lambda n=name: self.activate(n),
                     "primary" if not on else "ghost", 10, 14, 6)
            ab.pack(side="left")
            self.win.reg(ab, f"Use {name}", lambda n=name: self.activate(n))
            if not builtin:
                db = Btn(btns, "Delete", lambda n=name: self.delete(n), "danger", 10, 12, 6)
                db.pack(side="left", padx=8)
                self.win.reg(db, f"Delete {name}", lambda n=name: self.delete(n))
        for w in self.rules.winfo_children():
            w.destroy()
        rules = self.cfg["app_profiles"]
        if not rules:
            tk.Label(self.rules, text="No application rules yet.", font=font(9), bg=CARD, fg=FAINT).pack(anchor="w")
        for exe, prof in rules.items():
            r = tk.Frame(self.rules, bg=CARD)
            r.pack(fill="x", pady=3)
            tk.Label(r, text=exe, font=font(11, mono=True), bg=CARD, fg=FG, width=22, anchor="w").pack(side="left")
            cb = Btn(r, f"{prof}  ▸", lambda e=exe: self.cycle_rule(e), "ghost", 10, 12, 5)
            cb.pack(side="left", padx=8)
            xb = Btn(r, "Remove", lambda e=exe: self.remove_rule(e), "quiet", 10, 12, 5)
            xb.pack(side="left")
            self.win.reg(cb, f"Change profile for {exe}", lambda e=exe: self.cycle_rule(e))
            self.win.reg(xb, f"Remove rule {exe}", lambda e=exe: self.remove_rule(e))
        self.win.prune()

    def activate(self, name):
        self.app.activate_profile(name)
        self.refresh()

    def delete(self, name):
        profiles.delete_custom(self.cfg, name)
        self.refresh()

    def cycle_rule(self, exe):
        names = list(profiles.all_profiles(self.cfg))
        rules = dict(self.cfg["app_profiles"])
        cur = rules.get(exe)
        rules[exe] = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
        self.cfg.set("app_profiles", rules)
        self.refresh()

    def remove_rule(self, exe):
        rules = dict(self.cfg["app_profiles"])
        rules.pop(exe, None)
        self.cfg.set("app_profiles", rules)
        self.refresh()

    def refresh(self):
        sig = (self.cfg["active_profile"], repr(self.cfg["profiles"]), repr(self.cfg["app_profiles"]),
               tuple(self.cfg[k] for k in profiles.PROFILE_KEYS))
        if sig != self._sig:
            self._sig = sig
            self._rebuild()


# ============================================================================================================
class AccessibilityPage(Page):
    title = "Accessibility Tools"
    subtitle = "Independent aids — turn on only what helps."

    def build(self):
        c1, i1 = card(self, "Large high-contrast cursor",
                      "A big arrow drawn exactly at your pointer. Your system cursor settings are not changed.")
        self.setting_switch(i1, "large_cursor", "Show large cursor")
        self.setting_step(i1, "cursor_size", "Size", 1, 1, 6, lambda v: f"{v}×")
        r = row(i1, "Colour")
        seg = Segmented(r, self.win, [("yellow", "Yellow"), ("white", "White"), ("cyan", "Cyan"), ("black", "Black")],
                        lambda: self.cfg["cursor_color"], lambda v: self.app.change_setting("cursor_color", v))
        seg.pack()
        self.steppers.append(seg)
        c2, i2 = card(self, "Pointer trails & target highlighting",
                      "A fading trail shows where the pointer just was; highlighting outlines the control under it.")
        self.setting_switch(i2, "pointer_trails", "Pointer trail")
        self.setting_step(i2, "trail_length", "Trail length", 4, 8, 48, lambda v: f"{v}")
        self.setting_switch(i2, "highlight_targets", "Highlight target under pointer")
        c3, i3 = card(self, "On-screen keyboard",
                      "Dwell-activated keys that type into the app you were using. It sends keys only — "
                      "FocusFlow never reads what you type.")
        self.setting_switch(i3, "osk_enabled", "Show keyboard")
        self.setting_step(i3, "osk_dwell_ms", "Key dwell time", 100, 300, 3000, lambda v: f"{v / 1000:.1f} s")
        tk.Label(i3, text=f"Shortcut: {self.cfg['hotkeys']['keyboard'].title()}", font=font(9), bg=CARD, fg=FAINT) \
            .pack(anchor="w", pady=(10, 0))
        c4, i4 = card(self, "Voice commands",
                      "Short spoken commands via Windows speech recognition, processed on this PC. Nothing is recorded.")
        self.setting_switch(i4, "voice_enabled", "Listen for commands")
        self.voice_lbl = tk.Label(i4, text="", font=font(10), bg=CARD, fg=MUTED, anchor="w", justify="left", wraplength=px(360))
        self.voice_lbl.pack(fill="x", pady=(10, 0))
        tk.Label(i4, text="Say: “pause focusflow”, “resume focusflow”, “click”, “double click”, “right click”, "
                          "“scroll up/down”, “next profile”, “comfort mode”, “maximum assistance”, “show keyboard”, "
                          "“stop assistance”…", font=font(8), bg=CARD, fg=FAINT, justify="left", wraplength=px(360),
                 anchor="w").pack(fill="x", pady=(6, 0))
        c5, i5 = card(self, "Switch navigation",
                      "Step through the controls of the window in front with one switch, and activate with another. "
                      "Any device that presses a hotkey works.")
        self.setting_switch(i5, "switch_enabled", "Enable switch navigation")
        self.setting_switch(i5, "switch_auto_scan", "Auto-scan (advance by itself)")
        self.setting_step(i5, "switch_scan_ms", "Scan speed", 200, 400, 4000, lambda v: f"{v / 1000:.1f} s")
        hk = self.cfg["hotkeys"]
        tk.Label(i5, text=f"Next: {hk['switch_next'].title()}     Select: {hk['switch_select'].title()}",
                 font=font(9), bg=CARD, fg=FAINT).pack(anchor="w", pady=(10, 0))
        self.grid_cards([c1, c2, c3, c4, (c5, 2)])

    def refresh(self):
        super().refresh()
        self.voice_lbl.config(text=self.app.voice_status_text())


# ============================================================================================================
class LabPage(Page):
    title = "Live Lab"
    subtitle = "Your real mouse input, live: raw movement against the stabilised pointer you actually get."
    CW, CH = 600, 360

    def build(self):
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        left = tk.Frame(self, bg=BG)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        self.canvas = tk.Canvas(left, width=px(self.CW), height=px(self.CH), bg="#0A0E14", highlightthickness=1,
                                highlightbackground=BORDER)
        self.canvas.pack()
        legend = tk.Frame(left, bg=BG)
        legend.pack(fill="x", pady=(8, 0))
        for n, (color, text) in enumerate(((RAW, "raw input"), (STAB, "stabilised"), (VIOLET, "difference"),
                                           (AMBER, "reversal"), (RED, "jitter event"), (BLUE, "target"))):
            tk.Label(legend, text="●", fg=color, bg=BG, font=font(9)).grid(row=0, column=n * 2, sticky="w")
            tk.Label(legend, text=text, fg=MUTED, bg=BG, font=font(8)).grid(row=0, column=n * 2 + 1, sticky="w", padx=(2, 10))
        self.gap_canvas = tk.Canvas(left, width=px(self.CW), height=px(60), bg=CARD, highlightthickness=1,
                                    highlightbackground=BORDER)
        self.gap_canvas.pack(pady=(8, 0))
        tk.Label(left, text="Correction over the last 3 seconds (distance between raw and stabilised)", font=font(8),
                 bg=BG, fg=FAINT).pack(anchor="w")
        zr = tk.Frame(left, bg=BG)
        zr.pack(fill="x", pady=(8, 0))
        self.zoom = 4
        tk.Label(zr, text="Zoom", font=font(9), bg=BG, fg=MUTED).pack(side="left", padx=(0, 8))
        self.zoom_seg = Segmented(zr, self.win, [(1, "1×"), (2, "2×"), (4, "4×"), (8, "8×"), (16, "16×")],
                                  lambda: self.zoom, self._set_zoom, bg=BG)
        self.zoom_seg.pack(side="left")

        right = tk.Frame(self, bg=BG)
        right.grid(row=1, column=1, sticky="nsew")
        grid = tk.Frame(right, bg=BG)
        grid.pack(fill="x")
        self.t = {}
        specs = [("speed", "Speed", RAW), ("gap", "Raw − stabilised", STAB), ("dist", "Distance raw / stab", FG),
                 ("chg", "Direction changes raw / stab", FG), ("corr", "Corrective moves raw / stab", FG),
                 ("jit", "Jitter events", RED)]
        for i, (k, label, color) in enumerate(specs):
            tl = tile(grid, label, "-", color)
            tl.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0 if i % 2 == 0 else 6, 0), pady=(0, 6))
            grid.grid_columnconfigure(i % 2, weight=1, uniform="t")
            self.t[k] = tl.value
        self.profile_lbl = tk.Label(right, text="", font=font(10), bg=BG, fg=MUTED, anchor="w", justify="left")
        self.profile_lbl.pack(fill="x", pady=(4, 2))
        tk.Label(right, text="Dwell progress", font=font(9), bg=BG, fg=MUTED, anchor="w").pack(fill="x")
        self.dwell_bar = tk.Canvas(right, height=14, bg=CARD2, highlightthickness=0)
        self.dwell_bar.pack(fill="x", pady=(2, 10))

        ec, ei = card(right, "Experiment",
                      "Run the same 10-target task under each mode and compare. Saved to Performance Analytics.",
                      wrap=290)
        ec.pack(fill="x")
        self.mode = MODE_NAMES[-1]
        self.mode_btns = {}
        short = ("Normal", "Jitter only", "Dwell only", "Adaptation only", "Full assistance")
        grid_ = tk.Frame(ei, bg=CARD)
        grid_.pack(fill="x", pady=(8, 0))
        for i, (m, label) in enumerate(zip(MODE_NAMES, short)):
            b = Btn(grid_, label, lambda mm=m: self._pick(mm), "ghost", 10, 8, 6)
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
            grid_.grid_columnconfigure(i % 2, weight=1, uniform="m")
            self.win.reg(b, m, lambda mm=m: self._pick(mm))
            self.mode_btns[m] = b
        self.mode_lbl = tk.Label(ei, text="", font=font(9), bg=CARD, fg=ACCENT, anchor="w")
        self.mode_lbl.pack(fill="x", pady=(6, 0))
        brow = tk.Frame(ei, bg=CARD)
        brow.pack(fill="x", pady=(8, 0))
        sb = Btn(brow, "Start task  ▶", self.start_task, "primary", 11, 16, 8)
        sb.pack(side="left", fill="x", expand=True)
        self.win.reg(sb, "Start task", self.start_task)
        rb = Btn(brow, "Reset counters", self.app.engine.pipe.reset_stats, "quiet", 9, 10, 8)
        rb.pack(side="left", padx=(6, 0))
        self.win.reg(rb, "Reset counters", self.app.engine.pipe.reset_stats)
        self._pick(self.mode)
        # persistent canvas items (updated each frame rather than recreated)
        c = self.canvas
        self.it_grid = [c.create_line(0, 0, 0, 0, fill="#141B26") for _ in range(24)]
        self.it_targets = [c.create_rectangle(0, 0, 0, 0, outline=BLUE, width=2, state="hidden") for _ in range(14)]
        self.it_diff = [c.create_line(0, 0, 0, 0, fill=VIOLET, width=1, state="hidden") for _ in range(32)]
        self.it_raw = c.create_line(0, 0, 0, 0, fill=RAW, width=px(3))
        self.it_stab = c.create_line(0, 0, 0, 0, fill=STAB, width=px(2), capstyle="round", joinstyle="round")
        self.it_marks = [c.create_oval(0, 0, 0, 0, outline=AMBER, width=2, state="hidden") for _ in range(40)]
        self.it_raw_dot = c.create_oval(0, 0, 0, 0, outline=RAW, width=3, fill="#0A0E14")
        self.it_stab_dot = c.create_polygon(0, 0, 0, 0, 0, 0, 0, 0, fill=STAB, outline="#04161A")
        self.it_dwell = c.create_arc(0, 0, 0, 0, style="arc", start=90, extent=0, outline=ACCENT, width=4)
        self.it_text = c.create_text(12, 12, anchor="nw", fill=MUTED, font=font(9), text="")
        self.it_hint = c.create_text(px(self.CW) / 2, px(self.CH) / 2, fill=FAINT, font=font(11),
                                     text="Move the mouse to draw")

    def _set_zoom(self, z):
        self.zoom = z

    def _pick(self, m):
        self.mode = m
        for name, b in self.mode_btns.items():
            b.restyle("primary" if name == m else "ghost")
        self.mode_lbl.config(text=m)

    def start_task(self):
        self.app.ui.task.start(self.mode)

    # -- live drawing, ~30 fps while this page is visible ---------------------------------------------------
    def _set(self, key, text, font_=None):
        """Update a tile only when its text changed (Tk redraws on every config call)."""
        if self._tile_cache.get(key) != text:
            self._tile_cache[key] = text
            self.t[key].config(text=text, **({"font": font_} if font_ else {}))

    def frame(self):
        eng = self.app.engine
        self._frame_n = getattr(self, "_frame_n", 0) + 1
        now = time.perf_counter() * 1000.0
        trace, events, speed, smoothing = eng.pipe.snapshot(now - 3000)
        c, z = self.canvas, self.zoom
        if not hasattr(self, "_tile_cache"):
            self._tile_cache = {}
        if self._frame_n % 2 == 0:                                  # numbers do not need 20 updates a second
            st = eng.pipe.stats()
            self._set("speed", f"{speed:,.0f} px/s")
            self._set("dist", f"{st['raw_distance']:.0f} / {st['stabilised_distance']:.0f}", font(14, bold=True))
            self._set("chg", f"{st['raw_changes']} / {st['stabilised_changes']}", font(14, bold=True))
            self._set("corr", f"{st['raw_corrective']} / {st['stabilised_corrective']}", font(14, bold=True))
            self._set("jit", str(st["erratic_flags"]))
            prof = eng.current_profile()
            text = (f"Profile: {prof}   ·   jitter reduction {'ON' if eng.jitter_on() else 'OFF'}"
                    f"   ·   strength {self.cfg['smoothing_strength']}/10" + ("" if eng.active else "   ·   PAUSED"))
            if text != self._tile_cache.get("profile"):
                self._tile_cache["profile"] = text
                self.profile_lbl.config(text=text)
        prog = eng.dwell.progress if (eng.dwell_on() and eng.overlay) else 0
        if prog != self._tile_cache.get("prog"):
            self._tile_cache["prog"] = prog
            self.dwell_bar.delete("all")
            self.dwell_bar.create_rectangle(0, 0, self.dwell_bar.winfo_width() * prog, 14, fill=ACCENT, outline="")
        if len(trace) < 2:
            c.itemconfigure(self.it_hint, state="normal")
            self._set("gap", "-")
            return
        newest = trace[-1][0]
        if newest == self._tile_cache.get("newest") and self._frame_n % 12:
            return                                                  # no new input since the last frame: nothing to redraw
        self._tile_cache["newest"] = newest
        if len(trace) > 130:                                        # a 3 s trace at 250 Hz is 750 points: draw ~130
            k = len(trace) / 130.0
            trace = [trace[int(i * k)] for i in range(130)] + [trace[-1]]
        c.itemconfigure(self.it_hint, state="hidden")
        last = trace[-1]
        cx, cy = (last[1] + last[3]) / 2, (last[2] + last[4]) / 2
        W, H = px(self.CW), px(self.CH)

        def P(x, y):
            return W / 2 + (x - cx) * z, H / 2 + (y - cy) * z

        step = max(1, 25 if z <= 2 else 12)
        for i, item in enumerate(self.it_grid):                 # reference grid: 1 cell = `step` real pixels
            k = i // 2 - 6
            if i % 2 == 0:
                x = W / 2 + (round(cx / step) * step + k * step - cx) * z
                c.coords(item, x, 0, x, H)
            else:
                y = H / 2 + (round(cy / step) * step + k * step - cy) * z
                c.coords(item, 0, y, W, y)
        for i, item in enumerate(self.it_targets):
            rects = eng.lab_targets
            if i < len(rects):
                l, t, r, b = rects[i]
                x1, y1 = P(l, t)
                x2, y2 = P(r, b)
                c.coords(item, x1, y1, x2, y2)
                c.itemconfigure(item, state="normal")
            else:
                c.itemconfigure(item, state="hidden")
        raw_pts, stab_pts = [], []
        for s in trace:
            raw_pts += P(s[1], s[2])
            stab_pts += P(s[3], s[4])
        c.coords(self.it_raw, *raw_pts)
        c.coords(self.it_stab, *stab_pts)
        pick = trace[:: max(1, len(trace) // 32)][-32:]
        for i, item in enumerate(self.it_diff):
            if i < len(pick):
                s = pick[i]
                ax, ay = P(s[1], s[2])
                bx, by = P(s[3], s[4])
                c.coords(item, ax, ay, bx, by)
                c.itemconfigure(item, state="normal")
            else:
                c.itemconfigure(item, state="hidden")
        recent = [e for e in events if e[0] >= now - 3000][-40:]
        for i, item in enumerate(self.it_marks):
            if i < len(recent):
                t_, kind, ex, ey = recent[i]
                mx, my = P(ex, ey)
                rr = 9 if kind == "erratic" else 4
                c.coords(item, mx - rr, my - rr, mx + rr, my + rr)
                c.itemconfigure(item, state="normal", outline=RED if kind == "erratic" else AMBER)
            else:
                c.itemconfigure(item, state="hidden")
        rx, ry = P(last[1], last[2])
        sx, sy = P(last[3], last[4])
        c.coords(self.it_raw_dot, rx - 7, ry - 7, rx + 7, ry + 7)
        c.coords(self.it_stab_dot, sx, sy - 6, sx + 6, sy, sx, sy + 6, sx - 6, sy)
        c.coords(self.it_dwell, sx - 16, sy - 16, sx + 16, sy + 16)
        c.itemconfigure(self.it_dwell, extent=-359.9 * prog)
        gap = math.hypot(last[1] - last[3], last[2] - last[4])
        self._set("gap", f"{gap:.2f} px")
        if self._frame_n % 4 == 0:
            c.itemconfigure(self.it_text, text=f"{z}× · 1 grid cell = {step} real px · last 3 s")
            gaps = [math.hypot(s[1] - s[3], s[2] - s[4]) for s in trace[:: max(1, len(trace) // 90)]]
            draw_line(self.gap_canvas, [gaps], px(self.CW), px(60), [STAB])


# ============================================================================================================
class AnalyticsPage(Page):
    title = "Performance Analytics"
    subtitle = "What actually happened: measured pointer movement and click outcomes, from real use and your experiments."

    def build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.demo = False
        kp = tk.Frame(self, bg=BG)
        kp.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        self.k = {}
        for i, (key, label, color) in enumerate((("less", "Pointer travel saved", ACCENT), ("chg", "Direction changes cut", ACCENT),
                                                 ("clicks", "Assisted clicks", FG), ("near", "Near-misses fixed", FG),
                                                 ("prev", "Accidental clicks prevented", FG))):
            t = tile(kp, label, "-", color, CARD)
            t.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0))
            kp.grid_columnconfigure(i, weight=1, uniform="k")
            self.k[key] = t.value
        c1, i1 = card(self, "Experiment comparison",
                      "Average distance per completed run: grey = raw input, green = the pointer you used.")
        c1.grid(row=2, column=0, sticky="nsew", padx=(0, 7), pady=(0, 14))
        self.bars = tk.Canvas(i1, width=px(430), height=px(190), bg=CARD, highlightthickness=0)
        self.bars.pack(pady=(8, 0))
        c2, i2 = card(self, "Everyday use", "Raw vs stabilised pointer distance per minute of real use.")
        c2.grid(row=2, column=1, sticky="nsew", padx=(7, 0), pady=(0, 14))
        self.line = tk.Canvas(i2, width=px(430), height=px(190), bg=CARD, highlightthickness=0)
        self.line.pack(pady=(8, 0))
        c3, i3 = card(self, "Sessions")
        c3.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(0, 14))
        cols = ("When", "Mode", "Time", "Raw px", "Stab px", "Dir chg", "Corrective", "Near", "Wrong", "Accidental")
        self.table = tk.Frame(i3, bg=CARD)
        self.table.pack(fill="x", pady=(8, 0))
        self.cols = cols
        bar = tk.Frame(i3, bg=CARD)
        bar.pack(fill="x", pady=(12, 0))
        for text, fmt in (("Export CSV", "csv"), ("Export JSON", "json"), ("Export PDF report", "pdf")):
            b = Btn(bar, text, lambda f=fmt: self.export(f), "primary" if fmt == "pdf" else "ghost", 10, 14, 6)
            b.pack(side="left", padx=(0, 8))
            self.win.reg(b, text, lambda f=fmt: self.export(f))
        self.demo_btn = Btn(bar, "Show sample data", self.toggle_demo, "quiet", 10, 12, 6)
        self.demo_btn.pack(side="right")
        self.win.reg(self.demo_btn, "Toggle sample data", self.toggle_demo)
        db = Btn(bar, "Open web dashboard", self.app.open_dashboard, "quiet", 10, 12, 6)
        db.pack(side="right", padx=8)
        self.win.reg(db, "Open web dashboard", self.app.open_dashboard)
        self.demo_note = tk.Label(i3, text="", font=font(9, bold=True), bg=CARD, fg=AMBER)
        self.demo_note.pack(anchor="w", pady=(8, 0))
        self._sig = None

    def toggle_demo(self):
        if not self.demo:
            if not self.app.store.has_demo():
                self.app.store.seed_demo()
        self.demo = not self.demo
        self.demo_btn.config(text="Show my real data" if self.demo else "Show sample data")
        self._sig = None
        self.refresh()

    def export(self, fmt):
        path = self.app.export(fmt, self.demo)
        self.app.ui.toast(f"Saved {path.name}", 3200)

    def refresh(self):
        store, demo = self.app.store, self.demo
        sessions = store.sessions(8, demo)
        pt = store.pointer_totals(0.0, demo)
        ev = store.stats(demo)
        sig = (len(sessions), sessions[0]["id"] if sessions else 0, pt["n"], ev["total_assisted"], demo, ev["prevented"])
        if sig == self._sig:
            return
        self._sig = sig
        less = (1 - pt["stabilised_distance"] / pt["raw_distance"]) * 100 if pt["raw_distance"] > 0 else None
        chg = (1 - pt["stabilised_changes"] / pt["raw_changes"]) * 100 if pt["raw_changes"] > 0 else None
        self.k["less"].config(text="-" if less is None else f"{less:.0f}%")
        self.k["chg"].config(text="-" if chg is None else f"{chg:.0f}%")
        self.k["clicks"].config(text=str(ev["total_assisted"]))
        self.k["near"].config(text=f"{ev['near_miss_corrected']} / {ev['near_miss_detected']}")
        self.k["prev"].config(text=str(ev["prevented"]))
        modes = store.mode_summary(demo)
        names = [m for m in MODE_NAMES if m in modes]
        short = {"Normal mouse operation": "Normal", "Jitter reduction only": "Jitter", "Dwell clicking only": "Dwell",
                 "Target adaptation only": "Adapt", "Full FocusFlow assistance": "Full"}
        draw_bars(self.bars, [(modes[m]["raw_distance"], modes[m]["stabilised_distance"]) for m in names], px(430), px(190),
                  [short[m] for m in names], ["#4A5568", ACCENT])
        series = store.pointer_series(60, demo)
        draw_line(self.line, [[s["raw_distance"] for s in series], [s["stabilised_distance"] for s in series]],
                  px(430), px(190), ["#6B7686", ACCENT])
        for w in self.table.winfo_children():
            w.destroy()
        for ci, name in enumerate(self.cols):
            tk.Label(self.table, text=name, font=font(9, bold=True), bg=CARD, fg=MUTED, anchor="w") \
                .grid(row=0, column=ci, sticky="w", padx=(0, 18))
        if not sessions:
            tk.Label(self.table, text="No sessions yet — open Live Lab and press “Start task”.", font=font(10),
                     bg=CARD, fg=FAINT).grid(row=1, column=0, columnspan=len(self.cols), sticky="w", pady=6)
        for ri, s in enumerate(sessions, start=1):
            vals = [time.strftime("%d %b %H:%M", time.localtime(s["ts_start"])), s["mode"], f"{s['seconds']:.1f}s",
                    f"{s['raw_distance']:.0f}", f"{s['stabilised_distance']:.0f}",
                    f"{s['raw_changes']}→{s['stabilised_changes']}", f"{s['raw_corrective']}→{s['stabilised_corrective']}",
                    s["near_misses"], s["incorrect"], s["accidental"]]
            for ci, v in enumerate(vals):
                tk.Label(self.table, text=str(v), font=font(9), bg=CARD, fg=FG, anchor="w") \
                    .grid(row=ri, column=ci, sticky="w", padx=(0, 18), pady=1)
        self.demo_note.config(text="SAMPLE DATA — generated, not measured. Switch back to see your real numbers."
                              if demo else "")


# ============================================================================================================
class SettingsPage(Page):
    title = "Settings"
    subtitle = "Shortcuts, startup, privacy and safety."

    def build(self):
        c1, i1 = card(self, "Safety", "Pointer assistance can always be stopped.")
        for text in (f"Pause / resume:  {self.cfg['hotkeys']['toggle'].title()}",
                     f"Emergency stop (assistance off, pointer fully raw):  {self.cfg['hotkeys']['emergency'].title()}"):
            tk.Label(i1, text=text, font=font(10), bg=CARD, fg=FG, anchor="w").pack(fill="x", pady=(8, 0))
        eb = Btn(i1, "Emergency stop now", self.app.emergency_stop, "danger", 11, 18, 8)
        eb.pack(anchor="w", pady=(12, 0))
        self.win.reg(eb, "Emergency stop", self.app.emergency_stop)
        c2, i2 = card(self, "Startup & window")
        self.setting_switch(i2, "autostart", "Start with Windows", "Asks for confirmation. Off unless you say yes.")
        self.setting_switch(i2, "show_window_on_start", "Open this window at start")
        self.setting_switch(i2, "suggest_stronger", "Suggest stronger smoothing",
                            "When repeated short reversals are detected (a heuristic, not a diagnosis).")
        c3, i3 = card(self, "Privacy", "Everything stays on this PC. No screenshots, no keystroke capture.")
        self.setting_switch(i3, "log_target_names", "Log control names (e.g. “OK”)")
        self.setting_switch(i3, "extension_enabled", "Browser extension integration")
        tk.Label(i3, text=f"Extension pairing code (auto-paired normally):\n{self.cfg['token']}", font=font(8, mono=True),
                 bg=CARD, fg=FAINT, justify="left").pack(anchor="w", pady=(12, 0))
        c4, i4 = card(self, "Shortcuts")
        for k, v in self.cfg["hotkeys"].items():
            line = tk.Frame(i4, bg=CARD)
            line.pack(fill="x", pady=1)
            tk.Label(line, text=k.replace("_", " ").title(), font=font(9), bg=CARD, fg=MUTED, width=16, anchor="w").pack(side="left")
            tk.Label(line, text=v.title(), font=font(9, mono=True), bg=CARD, fg=FG).pack(side="left")
        self.grid_cards([c1, c2, c3, c4])
