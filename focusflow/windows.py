"""Auxiliary windows: click-through overlay, confirm prompt, toast, on-screen keyboard and the practice task."""
import math
import random
import time
import tkinter as tk
from collections import deque

from . import winapi
from .experiment import SessionRecorder
from .pointer import hit_target
from .profiles import MODE_NAMES
from .targets import Target
from .widgets import (px, ACCENT, ACCENT_DK, AMBER, BG, BLUE, BORDER, CARD, CARD2, FAINT, FG, KEY, MUTED, RAW, RED, STAB,
                      Btn, Panel, font, pill)

RING, RING_ASSIST = "#FFE14D", "#FF4FD8"      # dwell ring: high contrast on both light and dark pages
CURSOR_COLORS = {"yellow": "#FFE14D", "white": "#FFFFFF", "cyan": "#38E1FF", "black": "#101010"}
RAW_COLOR, STEADY_COLOR = "#FF4040", "#3DFF7A"   # raw (shaky) hand vs steadied pointer
STAB_TRAIL_S = 1.6
ZOOM_GROWN = 1.55                                  # visible magnification of a repeatedly-missed target
ARROW = ((0, 0), (0, 21), (5, 16.5), (9, 25), (13, 23), (9, 15), (16, 15))   # arrow tip at the hotspot


class Overlay:
    """One transparent, click-through window over the whole desktop. Layers (back to front): target highlight,
    adapted-target outline, switch-scan focus, dwell ring, pointer trail, large cursor."""

    def __init__(self, ui):
        self.ui = ui
        self.win = tk.Toplevel(ui.root, bg=KEY)
        self.win.overrideredirect(True)
        self.win.attributes("-transparentcolor", KEY, "-topmost", True)
        self.x0, self.y0, self.w, self.h = winapi.virtual_screen()
        self.win.geometry(f"{self.w}x{self.h}{self.x0:+d}{self.y0:+d}")
        c = self.canvas = tk.Canvas(self.win, width=self.w, height=self.h, bg=KEY, highlightthickness=0)
        c.pack()
        self.zoom = c.create_rectangle(0, 0, 0, 0, width=px(3), outline=RING, fill=RING, stipple="gray25", state="hidden")
        self.rect = c.create_rectangle(0, 0, 0, 0, width=px(2), outline=ACCENT)
        self.raw_path = c.create_line(0, 0, 0, 0, 0, 0, width=2, fill=RAW_COLOR, state="hidden")
        self.steady_path = c.create_line(0, 0, 0, 0, 0, 0, width=4, fill=STEADY_COLOR, capstyle="round", joinstyle="round",
                                         state="hidden")
        self.link = c.create_line(0, 0, 0, 0, width=1, fill=RAW_COLOR, dash=(3, 3), state="hidden")
        self.raw_dot = c.create_oval(0, 0, 0, 0, width=3, outline=RAW_COLOR, state="hidden")
        self.raw_tag = c.create_text(0, 0, text="your hand", fill=RAW_COLOR, font=("Segoe UI", 9, "bold"), anchor="nw",
                                     state="hidden")
        self.zoom_tag = c.create_text(0, 0, text="", fill="#FFFFFF", font=("Segoe UI", 9, "bold"), anchor="s", state="hidden")
        self.zoom_label = c.create_text(0, 0, text="", fill=FG, font=font(13, bold=True),
                                        anchor="center", state="hidden")
        self.raw_trail, self.steady_trail = deque(), deque()
        self.scan = c.create_rectangle(0, 0, 0, 0, width=px(4), outline=AMBER)
        # dwell gauge: a thin coloured arc over a slim dark underlay (contrast on light and dark pages), nothing else
        self.track = c.create_oval(0, 0, 0, 0, width=px(4), outline="#4A5362")
        self.ring = c.create_oval(0, 0, 0, 0, width=1, outline="#12161C", state="hidden")      # unused, kept hidden
        self.arc = c.create_arc(0, 0, 0, 0, style="arc", width=px(3), start=90, extent=0, outline=RING)
        self.trail_items = [c.create_line(0, 0, 0, 0, width=3, capstyle="round", state="hidden") for _ in range(48)]
        self.halo = c.create_oval(0, 0, 0, 0, width=3, outline="#000000", state="hidden")
        self.cursor = c.create_polygon(0, 0, 0, 0, 0, 0, fill="#FFE14D", outline="#000000", width=2, state="hidden",
                                       joinstyle="round")
        self.trail = deque(maxlen=48)
        self.shown = False
        self._last_frame = 0.0
        self.win.withdraw()

    def _wanted(self, state):
        cfg = self.ui.app.cfg
        return (state is not None or cfg["large_cursor"] or cfg["pointer_trails"]
                or self.ui.app.switch.current() is not None or self._stabilization_on())

    def _stabilization_on(self):
        app = self.ui.app
        return bool(app.cfg["show_stabilization"] and app.engine.jitter_on())

    def _restyle(self):
        if self.shown:
            winapi.make_overlay_window(self.win, self.x0, self.y0, self.w, self.h)

    def update(self, state):
        app = self.ui.app
        cfg = app.cfg
        if not app.engine.active or not self._wanted(state):
            if self.shown:
                self.win.withdraw()
                self.shown = False
                self.trail.clear()
                self.raw_trail.clear()
                self.steady_trail.clear()
            return
        if not self.shown:
            self.win.deiconify()
            self.win.update_idletasks()          # let Tk create its native wrapper window before we style it
            winapi.make_overlay_window(self.win, self.x0, self.y0, self.w, self.h)
            self.shown = True
            self.win.after(120, self._restyle)   # Tk may rebuild the wrapper once more; assert the style again
        c = self.canvas
        cur = winapi.get_cursor_pos()
        cx, cy = cur[0] - self.x0, cur[1] - self.y0

        # target highlight / dwell ring
        if state is not None:
            color = RING_ASSIST if state.assisted else RING
            l, t, r, b = state.rect
            if cfg["visual_enlarge"] and state.grown:
                k = ZOOM_GROWN
                w, h = (r - l) * (k - 1) / 2, (b - t) * (k - 1) / 2
                # A labelled proxy of the native control, not just an outline. Keep
                # every corner inside the existing doubled near-miss click radius.
                cap = cfg["miss_radius_px"] * 2 / math.sqrt(2)
                w, h = min(max(w, px(6)), cap), min(max(h, px(6)), cap)
                c.coords(self.zoom, l - self.x0 - w, t - self.y0 - h, r - self.x0 + w, b - self.y0 + h)
                c.itemconfigure(self.zoom, outline=color, fill=CARD, stipple="", state="normal")
                c.coords(self.zoom_label, (l + r) / 2 - self.x0, (t + b) / 2 - self.y0)
                c.itemconfigure(self.zoom_label, text=state.label[:60], width=max(1, r - l + 2 * w - px(8)),
                                state="normal")
                c.coords(self.zoom_tag, (l + r) / 2 - self.x0, t - self.y0 - h - px(3))
                c.itemconfigure(self.zoom_tag, text="enlarged target",
                                fill=color, state="normal")
            else:
                c.itemconfigure(self.zoom, state="hidden")
                c.itemconfigure(self.zoom_tag, state="hidden")
                c.itemconfigure(self.zoom_label, state="hidden")
            if cfg["highlight_targets"]:                    # the box is opt-in; dwell shows only the small gauge
                m = px(2)
                c.coords(self.rect, l - self.x0 - m, t - self.y0 - m, r - self.x0 + m, b - self.y0 + m)
                c.itemconfigure(self.rect, outline=color, dash=(6, 4) if state.assisted else (),
                                width=px(3) if state.grown else px(2), state="normal")
            else:
                c.itemconfigure(self.rect, state="hidden")
            if state.progress > 0.01:                       # the gauge appears only once dwelling has really begun
                cx_, cy_ = state.pos[0] - self.x0, state.pos[1] - self.y0
                rad = px(13)
                box = (cx_ - rad, cy_ - rad, cx_ + rad, cy_ + rad)
                for item in (self.track, self.arc):
                    c.coords(item, *box)
                    c.itemconfigure(item, state="normal")
                c.itemconfigure(self.arc, extent=-359.9 * state.progress, outline=color)
            else:
                for item in (self.track, self.arc):
                    c.itemconfigure(item, state="hidden")
        else:
            for item in (self.zoom, self.zoom_tag, self.zoom_label, self.rect, self.track, self.arc):
                c.itemconfigure(item, state="hidden")

        self._draw_stabilization(c, cx, cy)

        # switch-scanning focus
        nav = app.switch.current()
        if nav is not None:
            l, t, r, b = nav.rect
            c.coords(self.scan, l - self.x0 - 5, t - self.y0 - 5, r - self.x0 + 5, b - self.y0 + 5)
            c.itemconfigure(self.scan, state="normal")
        else:
            c.itemconfigure(self.scan, state="hidden")

        # pointer trail (the stabilised path the user actually got)
        if cfg["pointer_trails"]:
            if not self.trail or self.trail[-1] != (cx, cy):
                self.trail.append((cx, cy))
            n = min(len(self.trail), int(cfg["trail_length"]))
            pts = list(self.trail)[-n:]
            for i, item in enumerate(self.trail_items):
                if i < len(pts) - 1:
                    f = (i + 1) / max(1, len(pts) - 1)
                    shade = int(40 + 215 * f)
                    c.coords(item, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                    c.itemconfigure(item, state="normal", fill=f"#{int(shade * 0.4):02x}{shade:02x}{shade:02x}",
                                    width=2 + 5 * f)
                else:
                    c.itemconfigure(item, state="hidden")
        else:
            self.trail.clear()
            for item in self.trail_items:
                c.itemconfigure(item, state="hidden")

        # large high-contrast cursor
        if cfg["large_cursor"]:
            s = 1.0 + 0.55 * (int(cfg["cursor_size"]) - 1)
            flat = [v for pt in ARROW for v in (cx + pt[0] * s, cy + pt[1] * s)]
            c.coords(self.cursor, *flat)
            fill = CURSOR_COLORS.get(cfg["cursor_color"], "#FFE14D")
            outline = "#FFFFFF" if cfg["cursor_color"] == "black" else "#000000"
            c.itemconfigure(self.cursor, state="normal", fill=fill, outline=outline, width=2 + int(s / 1.5))
            r = 12 * s
            c.coords(self.halo, cx - r, cy - r, cx + r, cy + r)
            c.itemconfigure(self.halo, state="normal", outline=outline)
            c.tag_raise(self.cursor)
        else:
            c.itemconfigure(self.cursor, state="hidden")
            c.itemconfigure(self.halo, state="hidden")

    def _draw_stabilization(self, c, cx, cy):
        """Make the smoothing visible: the jagged raw hand path (red) next to the steadied path (green)."""
        items = (self.raw_path, self.steady_path, self.link, self.raw_dot, self.raw_tag)
        raw = getattr(self.ui.app.engine.pipe, "raw", None)
        if not self._stabilization_on() or raw is None:
            self.raw_trail.clear()
            self.steady_trail.clear()
            for item in items:
                c.itemconfigure(item, state="hidden")
            return
        now = time.perf_counter()
        rx, ry = raw[0] - self.x0, raw[1] - self.y0
        for trail, pt in ((self.raw_trail, (rx, ry)), (self.steady_trail, (cx, cy))):
            if not trail or trail[-1][1:] != pt:
                trail.append((now,) + pt)
            while trail and now - trail[0][0] > STAB_TRAIL_S:
                trail.popleft()
        for item, trail in ((self.raw_path, self.raw_trail), (self.steady_path, self.steady_trail)):
            if len(trail) >= 2:
                c.coords(item, *[v for p in trail for v in p[1:]])
                c.itemconfigure(item, state="normal")
            else:
                c.itemconfigure(item, state="hidden")
        gap = math.hypot(rx - cx, ry - cy)
        if gap >= 1.5:                                  # the pointer you got differs from the hand you moved
            r = px(6)
            c.coords(self.raw_dot, rx - r, ry - r, rx + r, ry + r)
            c.coords(self.link, rx, ry, cx, cy)
            c.coords(self.raw_tag, rx + r + 3, ry + r + 1)
            c.itemconfigure(self.raw_tag, text=f"your hand  ({gap:.0f}px steadied)")
            for item in (self.raw_dot, self.link, self.raw_tag):
                c.itemconfigure(item, state="normal")
        else:
            for item in (self.raw_dot, self.link, self.raw_tag):
                c.itemconfigure(item, state="hidden")
        for item in (self.raw_path, self.steady_path, self.link, self.raw_dot, self.raw_tag, self.zoom_tag):
            c.tag_raise(item)
        c.tag_raise(self.cursor)

    def pump(self):
        try:
            self.update(self.ui.app.engine.overlay)
        except tk.TclError:
            pass
        self.win.after(33 if self.shown else 70, self.pump)       # idle: poll gently


# ---- confirm prompt ----------------------------------------------------------------------------------------
class Prompt(Panel):
    name = "prompt"

    def ask(self, title, message, yes, no, on_result, default=None):
        self.destroy()
        win = self.win = self._new()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        box = tk.Frame(win, bg=CARD, padx=34, pady=28, highlightbackground=AMBER, highlightthickness=3)
        box.pack()
        tk.Label(box, text=title, font=font(22, bold=True), bg=CARD, fg=FG).pack(anchor="w")
        tk.Label(box, text=message, font=font(13), bg=CARD, fg=MUTED, justify="left", wraplength=640) \
            .pack(anchor="w", pady=(8, 22))
        rowf = tk.Frame(box, bg=CARD)
        rowf.pack(fill="x")
        done = []

        def finish(result):
            if done:
                return
            done.append(1)
            self.destroy()
            on_result(result)

        b1 = Btn(rowf, f"{yes}   (Y)", lambda: finish(True), "primary", 17, 40, 22)
        b1.pack(side="left", expand=True, fill="x", padx=(0, 12))
        b2 = Btn(rowf, f"{no}   (N / Esc)", lambda: finish(False), "ghost", 17, 40, 22)
        b2.pack(side="left", expand=True, fill="x")
        self.reg(b1, yes, lambda: finish(True), "yes")
        self.reg(b2, no, lambda: finish(False), "no")
        for k in ("y", "Y"):
            win.bind(k, lambda e: finish(True))
        for k in ("n", "N", "<Escape>"):
            win.bind(k, lambda e: finish(False))
        win.bind("<Return>", lambda e: finish(default if default is not None else False))
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{(sw - win.winfo_reqwidth()) // 2}+{(sh - win.winfo_reqheight()) // 3}")
        self.show()
        win.focus_force()


def toast(ui, text, ms=1800):
    win = tk.Toplevel(ui.root, bg=CARD)
    win.overrideredirect(True)
    win.attributes("-topmost", True, "-alpha", 0.96)
    f = tk.Frame(win, bg=CARD, highlightbackground=ACCENT, highlightthickness=2)
    f.pack()
    tk.Label(f, text=text, font=font(16, bold=True), bg=CARD, fg=FG, padx=28, pady=14).pack()
    win.update_idletasks()
    win.geometry(f"+{(win.winfo_screenwidth() - win.winfo_reqwidth()) // 2}+48")
    win.after(ms, win.destroy)


# ---- on-screen keyboard -----------------------------------------------------------------------------------
class KeyboardWindow(Panel):
    """Dwell-activated on-screen keyboard. It never takes keyboard focus (WS_EX_NOACTIVATE), so characters go
    to the application you were using. FocusFlow only *sends* keys; it never reads any."""
    name = "keyboard"
    ROWS = ("1234567890", "qwertyuiop", "asdfghjkl'", "zxcvbnm,.?")
    VK = {"Back": 8, "Tab": 9, "Enter": 13, "Esc": 27}

    def open(self):
        if self.visible():
            return
        self.destroy()
        cfg = self.ui.app.cfg
        win = self.win = self._new()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        self.shift = False
        self.keys = []
        outer = tk.Frame(win, bg=CARD, padx=10, pady=10, highlightbackground=ACCENT, highlightthickness=2)
        outer.pack()
        dwell = int(cfg["osk_dwell_ms"])

        def add(parent, text, cmd, kind="ghost", width=None, size=15):
            b = Btn(parent, text, cmd, kind, size, 6, 11)
            if width:
                b.config(width=width)
            self.reg(b, f"key {text}", cmd, f"k{len(self._reg)}", dwell)
            return b

        for ri, chars in enumerate(self.ROWS):
            rf = tk.Frame(outer, bg=CARD)
            rf.pack(pady=2)
            if ri == 3:
                self.shift_btn = add(rf, "⇧", self._toggle_shift, "ghost", 4)
                self.shift_btn.pack(side="left", padx=2)
            for ch in chars:
                b = add(rf, ch, lambda c=ch: self._type(c), "ghost", 3)
                b.pack(side="left", padx=2)
                self.keys.append((ch, b))
            if ri == 0:
                add(rf, "⌫", lambda: self._vk("Back"), "ghost", 5).pack(side="left", padx=(8, 2))
        bottom = tk.Frame(outer, bg=CARD)
        bottom.pack(pady=(4, 0))
        add(bottom, "Tab", lambda: self._vk("Tab"), "ghost", 5, 12).pack(side="left", padx=2)
        add(bottom, "Space", lambda: self._type(" "), "ghost", 22, 13).pack(side="left", padx=2)
        add(bottom, "Enter", lambda: self._vk("Enter"), "primary", 7, 12).pack(side="left", padx=2)
        add(bottom, "Close", self.close, "danger", 6, 12).pack(side="left", padx=(12, 2))
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{(sw - win.winfo_reqwidth()) // 2}+{sh - win.winfo_reqheight() - 70}")
        win.deiconify()
        win.update_idletasks()
        winapi.make_noactivate(win)
        self.show()

    def close(self):
        """The keyboard's own Close key: also switches the setting off."""
        self.close_quiet()
        if self.ui.app.cfg["osk_enabled"]:
            self.ui.app.change_setting("osk_enabled", False)

    def close_quiet(self):
        self.destroy()

    def _toggle_shift(self):
        self.shift = not self.shift
        self.shift_btn.restyle("primary" if self.shift else "ghost")
        for ch, b in self.keys:
            if ch.isalpha():
                b.config(text=ch.upper() if self.shift else ch)

    def _type(self, ch):
        winapi.send_text(ch.upper() if (self.shift and ch.isalpha()) else ch)
        if self.shift:
            self._toggle_shift()                        # one-shot shift, like a phone keyboard

    def _vk(self, name):
        winapi.send_vk(self.VK[name])


# ---- practice task ------------------------------------------------------------------------------------------
class TaskWindow(Panel):
    """A measured navigation task: select the highlighted numbered target, ten times, in a fixed route.
    Real clicks and real dwell activations are recorded by SessionRecorder; nothing is simulated."""
    name = "task"
    LAYOUT = {1: (120, 120, 52), 2: (270, 105, 46), 3: (330, 235, 46), 4: (540, 130, 56), 5: (640, 300, 44)}
    ROUTE = (1, 2, 3, 4, 5, 4, 3, 2, 1, 5)
    W, H = 860, 430

    def start(self, mode):
        app = self.ui.app
        self.destroy()
        self.rec = SessionRecorder(app.engine, mode, len(self.ROUTE))
        self.step = 0
        self.mode = mode
        self.done = False
        win = self.win = self._new()
        win.title("FocusFlow · Pointer task")
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.stop)
        top = tk.Frame(win, bg=BG, padx=18, pady=12)
        top.pack(fill="x")
        pill(top, mode, ACCENT).pack(side="left")
        self.step_lbl = tk.Label(top, text="", font=font(14, bold=True), bg=BG, fg=FG)
        self.step_lbl.pack(side="left", padx=18)
        self.time_lbl = tk.Label(top, text="0.0 s", font=font(14, mono=True), bg=BG, fg=MUTED)
        self.time_lbl.pack(side="right")
        stop = Btn(top, "Stop", self.stop, "danger", 10, 14, 5)
        stop.pack(side="right", padx=14)
        self.reg(stop, "Stop task", self.stop, "stop")
        self.canvas = tk.Canvas(win, width=self.W, height=self.H, bg=CARD, highlightthickness=1,
                                highlightbackground=BORDER)
        self.canvas.pack(padx=18, pady=(0, 6))
        self.hint = tk.Label(win, text="Click or hover on the highlighted target. Wrong targets and empty space count "
                             "as errors. Esc stops.", font=font(9), bg=BG, fg=FAINT)
        self.hint.pack(pady=(0, 12))
        self.items = {}
        for tid, (x, y, s) in self.LAYOUT.items():
            r = self.canvas.create_rectangle(x - s / 2, y - s / 2, x + s / 2, y + s / 2, fill=CARD2, outline=BORDER,
                                             width=2)
            t = self.canvas.create_text(x, y, text=str(tid), font=font(16, bold=True), fill=FG)
            self.items[tid] = (r, t)
        self.ring = self.canvas.create_rectangle(0, 0, 0, 0, outline=ACCENT, width=4)
        self.canvas.bind("<Button-1>", self._on_click)
        win.bind("<Escape>", lambda e: self.stop())
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"+{(sw - win.winfo_reqwidth()) // 2}+{(sh - win.winfo_reqheight()) // 2}")
        self._refresh_expected()
        self.show()
        win.focus_force()
        self._tick()

    # geometry helpers -------------------------------------------------------------------------------------
    def _screen_rect(self, tid):
        x, y, s = self.LAYOUT[tid]
        ox, oy = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        return (ox + int(x - s / 2), oy + int(y - s / 2), ox + int(x + s / 2), oy + int(y + s / 2))

    def _rects(self):
        return [(tid, self._screen_rect(tid)) for tid in self.LAYOUT]

    def extra_targets(self):
        out = []
        if self.done:
            return out
        for tid in self.LAYOUT:
            r = self._screen_rect(tid)
            t = Target(f"task:{tid}", r, f"target {tid}", "Task target", "local",
                       hit_point=((r[0] + r[2]) // 2, (r[1] + r[3]) // 2),
                       action=lambda i=tid: self._activate(i, "dwell"))
            out.append(t)
        return out

    # task flow --------------------------------------------------------------------------------------------
    def _expected(self):
        return self.ROUTE[self.step]

    def _refresh_expected(self):
        exp = self._expected()
        x, y, s = self.LAYOUT[exp]
        self.canvas.coords(self.ring, x - s / 2 - 7, y - s / 2 - 7, x + s / 2 + 7, y + s / 2 + 7)
        for tid, (r, _) in self.items.items():
            self.canvas.itemconfigure(r, fill=ACCENT_DK if tid == exp else CARD2,
                                      outline=ACCENT if tid == exp else BORDER)
        self.step_lbl.config(text=f"Select {exp}   ·   step {self.step + 1} of {len(self.ROUTE)}")

    def _flash(self, tid, color):
        r = self.items[tid][0]
        self.canvas.itemconfigure(r, fill=color)
        self.win.after(180, lambda: self.win and self.win.winfo_exists() and not self.done and self._refresh_expected())

    def _on_click(self, event):
        if self.done:
            return
        point = (event.x_root, event.y_root)
        rects = self._rects()
        self._record(point, hit_target(point, rects), rects, "mouse")

    def _activate(self, tid, source):
        if self.done:
            return
        r = self._screen_rect(tid)
        self._record(((r[0] + r[2]) // 2, (r[1] + r[3]) // 2), tid, self._rects(), source)

    def _record(self, point, hit, rects, source):
        outcome = self.rec.click(point, self._expected(), hit, rects, source)
        if outcome == "success":
            self._flash(hit, ACCENT)
            self.step += 1
            if self.step >= len(self.ROUTE):
                return self._complete()
            self.win.after(200, self._refresh_expected)
        elif outcome == "accidental":
            self._flash(hit, "#5A262D")
        else:
            ox, oy = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
            x, y = point[0] - ox, point[1] - oy
            mark = self.canvas.create_text(x, y, text="×", font=font(18, bold=True), fill=RED)
            self.win.after(450, lambda: self.canvas.delete(mark))

    def _tick(self):
        if self.win is None or not self.win.winfo_exists() or self.done:
            return
        self.time_lbl.config(text=f"{self.rec.elapsed():.1f} s")
        self.win.after(100, self._tick)

    def _complete(self):
        self.done = True
        rec = self.rec.finish(True)
        self.ui.local.clear(self.name)
        self._reg = [r for r in self._reg if r[0] == "stop"]
        self.canvas.delete("all")
        raw, stab = rec["raw_distance"], rec["stabilised_distance"]
        less = (1 - stab / raw) * 100 if raw else 0
        c = self.canvas
        c.create_text(self.W / 2, 56, text="Task complete", font=font(24, bold=True), fill=ACCENT)
        lines = [f"Time {rec['seconds']:.1f} s     ·     {rec['successes']} of {rec['targets']} selected",
                 f"Wrong clicks {rec['incorrect']}     ·     near-misses {rec['near_misses']}     ·     accidental "
                 f"activations {rec['accidental']}",
                 f"Pointer distance: raw {raw:.0f} px  →  stabilised {stab:.0f} px  ({less:.0f}% less)",
                 f"Direction changes: raw {rec['raw_changes']}  →  stabilised {rec['stabilised_changes']}     ·     "
                 f"corrective movements {rec['raw_corrective']} → {rec['stabilised_corrective']}"]
        for i, text in enumerate(lines):
            c.create_text(self.W / 2, 120 + i * 34, text=text, font=font(12), fill=FG)
        c.create_text(self.W / 2, 290, text=f"Saved to your history as “{self.mode}”. Run the other modes to compare.",
                      font=font(10), fill=MUTED)
        nxt = MODE_NAMES[(MODE_NAMES.index(self.mode) + 1) % len(MODE_NAMES)]
        self._again = nxt
        again = Btn(self.win, f"Next mode: {nxt}", lambda: self.start(nxt), "primary", 12, 22, 10)
        close = Btn(self.win, "Close", self.stop, "ghost", 12, 22, 10)
        c.create_window(self.W / 2 - 110, 350, window=again)
        c.create_window(self.W / 2 + 170, 350, window=close)
        self.reg(again, "Next mode", lambda: self.start(nxt), "again")
        self.reg(close, "Close", self.stop, "close")
        self.hint.config(text="")
        self.ui.app.session_saved()

    def stop(self):
        if self.win is None:
            return
        if not self.done:
            self.rec.finish(False)                       # incomplete trials are not saved
        self.destroy()
