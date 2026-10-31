"""The FocusFlow window (five sections), plus the Tk thread plumbing every other part of the app talks to."""
import queue
import tkinter as tk

from . import widgets
from .pages import (AccessibilityPage, AnalyticsPage, LabPage, PointerAssistPage, ProfilesPage, SettingsPage)
from .targets import LocalTargets
from .widgets import (ACCENT, ACCENT_DK, AMBER, BG, BORDER, CARD, CARD2, FAINT, FG, MUTED, SIDEBAR, Btn, Panel, font,
                      pill, px)
from .windows import KeyboardWindow, Overlay, Prompt, TaskWindow, toast

NAV = (("assist", "Pointer Assist", "◎", PointerAssistPage),
       ("profiles", "Adaptive Profiles", "◧", ProfilesPage),
       ("access", "Accessibility Tools", "♿", AccessibilityPage),
       ("lab", "Live Lab", "◈", LabPage),
       ("analytics", "Performance Analytics", "▥", AnalyticsPage))


class MainWindow(Panel):
    name = "main"

    def __init__(self, ui):
        super().__init__(ui)
        self.pages = {}
        self.nav_btns = {}
        self.current = None
        self._job2 = None

    # -- construction -------------------------------------------------------------------------------------
    def open(self, page=None):
        if self.win is None or not self.win.winfo_exists():
            self._build()
        self.show()
        self.win.focus_force()
        self.go(page or self.current or "assist")

    def _build(self):
        app = self.ui.app
        win = self.win = self._new()
        win.title("FocusFlow")
        win.protocol("WM_DELETE_WINDOW", self.hide)          # closing hides to the tray; Quit is in the tray menu
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w, h = min(px(1180), sw - px(40)), min(px(760), sh - px(110))
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        win.minsize(min(px(980), w), min(px(600), h))
        win.configure(bg=BG)

        # sidebar -----------------------------------------------------------------------------------------
        side = tk.Frame(win, bg=SIDEBAR, width=px(236))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        logo = tk.Frame(side, bg=SIDEBAR)
        logo.pack(fill="x", padx=px(18), pady=(px(20), px(18)))
        tk.Label(logo, text="◉", font=font(22), bg=SIDEBAR, fg=ACCENT).pack(side="left")
        col = tk.Frame(logo, bg=SIDEBAR)
        col.pack(side="left", padx=px(10))
        tk.Label(col, text="FocusFlow", font=font(15, bold=True), bg=SIDEBAR, fg=FG, anchor="w").pack(anchor="w")
        tk.Label(col, text="Pointer assistant", font=font(8), bg=SIDEBAR, fg=FAINT).pack(anchor="w")
        for key, label, icon, _ in NAV:
            b = Btn(side, f"{icon}   {label}", lambda k=key: self.go(k), "nav", 11, 18, 12, anchor="w")
            b.pack(fill="x", padx=px(10), pady=px(2))
            self.reg(b, label, lambda k=key: self.go(k), f"nav-{key}")
            self.nav_btns[key] = b
        foot = tk.Frame(side, bg=SIDEBAR)
        foot.pack(side="bottom", fill="x", padx=px(14), pady=px(16))
        self.side_status = tk.Label(foot, text="", font=font(9), bg=SIDEBAR, fg=MUTED, justify="left", anchor="w",
                                    wraplength=px(200))
        self.side_status.pack(fill="x", pady=(0, px(10)))
        e = Btn(foot, "■  Emergency stop", app.emergency_stop, "danger", 10, 12, 9)
        e.pack(fill="x")
        self.reg(e, "Emergency stop", app.emergency_stop, "emergency")
        sb = Btn(foot, "⚙   Settings", lambda: self.go("settings"), "nav", 10, 12, 9, anchor="w")
        sb.pack(fill="x", pady=(px(6), 0))
        self.reg(sb, "Settings", lambda: self.go("settings"), "settings")
        self.nav_btns["settings"] = sb

        # header ------------------------------------------------------------------------------------------
        main = tk.Frame(win, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        head = tk.Frame(main, bg=BG)
        head.pack(fill="x", padx=px(26), pady=(px(20), px(10)))
        tl = tk.Frame(head, bg=BG)
        tl.pack(side="left", fill="x", expand=True)
        self.title_lbl = tk.Label(tl, text="", font=font(20, bold=True), bg=BG, fg=FG, anchor="w")
        self.title_lbl.pack(fill="x")
        self.sub_lbl = tk.Label(tl, text="", font=font(10), bg=BG, fg=MUTED, anchor="w", wraplength=px(480), justify="left")
        self.sub_lbl.pack(fill="x")
        tr = tk.Frame(head, bg=BG)
        tr.pack(side="right")
        self.pause_btn = Btn(tr, "", app.toggle_enabled, "primary", 11, 16, 8)
        self.pause_btn.pack(side="right", padx=(px(8), 0))
        self.reg(self.pause_btn, "Pause or resume", app.toggle_enabled, "pause")
        self.profile_btn = Btn(tr, "", app.cycle_profile, "ghost", 11, 14, 8)
        self.profile_btn.pack(side="right", padx=(px(8), 0))
        self.reg(self.profile_btn, "Change profile", app.cycle_profile, "profile")
        self.status_pill = tk.Label(tr, text="", font=font(10, bold=True), bg=ACCENT_DK, fg=ACCENT, padx=px(12), pady=px(8))
        self.status_pill.pack(side="right")

        # scrolling content ----------------------------------------------------------------------------------
        body = tk.Frame(main, bg=BG)
        body.pack(fill="both", expand=True, padx=(px(26), px(10)), pady=(0, px(10)))
        self.view = tk.Canvas(body, bg=BG, highlightthickness=0)
        self.view.pack(side="left", fill="both", expand=True)
        rail = tk.Frame(body, bg=BG, width=px(46))
        rail.pack(side="right", fill="y", padx=(px(6), 0))
        up = Btn(rail, "▲", lambda: self.scroll(-1), "ghost", 12, 8, 10)
        up.pack(pady=(0, px(6)))
        dn = Btn(rail, "▼", lambda: self.scroll(1), "ghost", 12, 8, 10)
        dn.pack()
        self.reg(up, "Scroll up", lambda: self.scroll(-1), "up")
        self.reg(dn, "Scroll down", lambda: self.scroll(1), "down")
        self.holder = tk.Frame(self.view, bg=BG)
        self.holder_id = self.view.create_window(0, 0, window=self.holder, anchor="nw")
        self.holder.bind("<Configure>", lambda e: self.view.configure(scrollregion=self.view.bbox("all")))
        self.view.bind("<Configure>", lambda e: self.view.itemconfigure(self.holder_id, width=e.width))
        win.bind("<MouseWheel>", lambda e: self.scroll(-1 if e.delta > 0 else 1, 0.4))
        for key, label, icon, cls in NAV:
            self.pages[key] = cls(self.holder, self)
        self.pages["settings"] = SettingsPage(self.holder, self)
        win.bind("<Escape>", lambda e: self.hide())
        self.prune()
        self._tick()
        self._lab_loop()

    # -- behaviour ------------------------------------------------------------------------------------------
    def go(self, key):
        if key not in self.pages:
            return
        for k, p in self.pages.items():
            p.pack_forget()
        page = self.pages[key]
        page.pack(fill="both", expand=True)
        self.current = key
        self.title_lbl.config(text=page.title)
        self.sub_lbl.config(text=page.subtitle)
        for k, b in self.nav_btns.items():
            b.restyle("navon" if k == key else "nav")
        self.view.yview_moveto(0)
        page.refresh()
        self.win.update_idletasks()
        self.view.configure(scrollregion=self.view.bbox("all"))
        self._publish_now()

    def scroll(self, direction, fraction=0.7):
        bbox = self.view.bbox("all")
        if not bbox:
            return
        total, shown = bbox[3] - bbox[1], self.view.winfo_height()
        if total <= shown:
            return
        top = self.view.canvasy(0) - bbox[1]
        new = max(0.0, min(1.0, (top + direction * shown * fraction) / total))
        self.view.yview_moveto(new)

    def _signature(self):
        return (self.win.winfo_geometry(), len(self._reg), self.win.winfo_viewable(), self.current,
                round(self.view.canvasy(0)))

    def _publish_now(self):
        if self.visible():
            self._publish(force=True)

    def _tick(self):
        if self.win is None or not self.win.winfo_exists():
            return
        app = self.ui.app
        on, eng = app.cfg["enabled"], app.engine
        prof = eng.current_profile()
        self.status_pill.config(text="●  Assisting" if on else "●  Paused",
                                bg=ACCENT_DK if on else "#2A2F3A", fg=ACCENT if on else MUTED)
        self.pause_btn.restyle("ghost" if on else "primary", "⏸  Pause" if on else "▶  Resume")
        self.profile_btn.config(text=f"Profile: {prof}   ▸")
        feats = [n for n, f in (("Smoothing", eng.jitter_on()), ("Dwell", eng.dwell_on()),
                                ("Attraction", eng.attraction() > 0), ("Hit-area", eng.adapt_on())) if f]
        self.side_status.config(text=("Active: " + ", ".join(feats)) if on and feats else
                                ("Assistance is off" if not on else "No assistance enabled"))
        page = self.pages.get(self.current)
        if page is not None and self.visible() and self.current != "lab":
            page.refresh()
        self._job2 = self.win.after(400, self._tick)

    def _lab_loop(self):
        if self.win is None or not self.win.winfo_exists():
            return
        if self.current == "lab" and self.visible():
            try:
                self.pages["lab"].frame()
            except tk.TclError:
                pass
        self.win.after(66, self._lab_loop)


class UI:
    def __init__(self, app):
        self.app = app
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("FocusFlow")
        widgets.set_scale(self.root.winfo_fpixels("1i") / 96.0)
        self._q = queue.Queue()
        self.local = LocalTargets()
        self.local.dispatch = self.call
        self.main = MainWindow(self)
        self.prompt, self.keyboard, self.task = Prompt(self), KeyboardWindow(self), TaskWindow(self)
        self.overlay = None
        self.root.after(40, self._pump)

    def start(self):
        self.overlay = Overlay(self)
        self.overlay.pump()

    def call(self, fn, *args):
        """Run fn on the Tk thread (safe from any thread)."""
        self._q.put((fn, args))

    def _pump(self):
        try:
            while True:
                fn, args = self._q.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    import traceback
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(40, self._pump)

    # -- API used by the rest of the app (all thread-safe) ---------------------------------------------------
    def show_window(self, page=None):
        self.call(self.main.open, page)

    def toggle_window(self):
        def fn():
            if self.main.visible():
                self.main.hide()
            else:
                self.main.open()
        self.call(fn)

    def show_keyboard(self, show):
        self.call(self.keyboard.open if show else self.keyboard.close_quiet)

    def confirm(self, title, message, yes="Yes", no="No", on_result=lambda ok: None, default=None):
        self.call(self.prompt.ask, title, message, yes, no, on_result, default)

    def toast(self, text, ms=1800):
        self.call(toast, self, text, ms)

    def run(self):
        self.root.mainloop()

    def quit(self):
        self.root.after(0, self.root.quit)
