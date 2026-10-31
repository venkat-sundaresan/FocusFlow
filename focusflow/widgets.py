"""FocusFlow's design system: dark theme tokens, dwell-aware controls and small canvas charts.

Every interactive control registers itself with its window (`Panel.reg`), which publishes the control's screen
rectangle so the dwell engine can click it. FocusFlow is therefore fully usable with no clicking at all."""
import threading
import tkinter as tk

from .targets import Target

# ---- tokens -------------------------------------------------------------------------------------------------
BG, SIDEBAR, CARD, CARD2, BORDER = "#0D1117", "#11161D", "#161B24", "#1C2330", "#252D3B"
FG, MUTED, FAINT = "#E8EDF4", "#8D99AB", "#5E6A7C"
ACCENT, ACCENT_DK, ACCENT_HOVER = "#2DD4A7", "#0F3B31", "#4BE8BC"
BLUE, AMBER, RED, VIOLET = "#5AA9FF", "#FFC857", "#FF6B6B", "#B79CFF"
RAW, STAB = "#FB923C", "#67E8F9"          # raw input / stabilised pointer, shared by every chart
KEY = "#010101"                            # transparent colour key for the overlay window
FONT, FONT_B, MONO = "Segoe UI", "Segoe UI Semibold", "Consolas"

SCALE = 1.0          # display scale (1.5 at 150 %); Tk scales fonts by itself but not pixel sizes


def set_scale(value):
    global SCALE
    SCALE = max(1.0, float(value))


def px(n):
    return max(1, int(round(n * SCALE)))


def font(size, bold=False, mono=False):
    return (MONO if mono else (FONT_B if bold else FONT), size) if not (bold and mono) else (MONO, size, "bold")


class Panel:
    """A Toplevel whose controls are published as dwell targets while it is visible."""
    name = "panel"

    def __init__(self, ui):
        self.ui = ui
        self.win = None
        self._reg = []                         # (key, label, widget, command, dwell_ms)
        self._job = None
        self._pub_sig = None

    def _new(self, **kw):
        win = tk.Toplevel(self.ui.root, bg=BG, **kw)
        win.withdraw()
        return win

    def reg(self, widget, label, command, key=None, dwell_ms=None):
        self._reg.append((key or f"{len(self._reg)}", label, widget, command, dwell_ms))
        return widget

    def extra_targets(self):
        """Targets that are not widgets (e.g. canvas items). Override."""
        return []

    def prune(self):
        """Forget controls that were destroyed (after a page rebuilds part of itself)."""
        alive = []
        for entry in self._reg:
            try:
                if entry[2].winfo_exists():
                    alive.append(entry)
            except tk.TclError:
                pass
        self._reg = alive

    def visible(self):
        return self.win is not None and self.win.winfo_exists() and self.win.state() != "withdrawn"

    def _signature(self):
        """Cheap fingerprint of "where everything is"; subclasses add scroll position / current page."""
        return (self.win.winfo_geometry(), len(self._reg), self.win.winfo_viewable())   # unmapped -> mapped changes it

    def _publish(self, force=False):
        if self.win is None or not self.win.winfo_exists():
            self.ui.local.clear(self.name)
            return
        if self.visible():
            sig = self._signature()
            if not force and sig == getattr(self, "_pub_sig", None):
                self._job = self.win.after(250, self._publish)      # nothing moved: keep the published rectangles
                return
            self._pub_sig = sig
            targets = []
            for key, label, widget, command, dwell_ms in self._reg:
                try:
                    if not widget.winfo_exists() or not widget.winfo_viewable():
                        continue
                    x, y = widget.winfo_rootx(), widget.winfo_rooty()
                    w, h = widget.winfo_width(), widget.winfo_height()
                except tk.TclError:
                    continue
                if w < 4 or h < 4:
                    continue
                t = Target(f"{self.name}:{key}", (x, y, x + w, y + h), label, "FocusFlow control", "local",
                           hit_point=(x + w // 2, y + h // 2), action=command)
                t.dwell_ms = dwell_ms
                targets.append(t)
            targets += self.extra_targets()
            self.ui.local.set(self.name, targets)
        else:
            self.ui.local.clear(self.name)
            self._pub_sig = None
        self._job = self.win.after(250, self._publish)

    def _dirty(self, event=None):
        self._pub_sig = None                       # something was mapped or resized: republish on the next cycle

    def show(self):
        self.win.deiconify()
        self.win.lift()
        if not getattr(self.win, "_ff_bound", False):          # layout events invalidate the published rectangles
            self.win.bind("<Map>", self._dirty, add="+")
            self.win.bind("<Configure>", self._dirty, add="+")
            self.win._ff_bound = True
        self.win.update_idletasks()
        if self._job is not None:
            try:
                self.win.after_cancel(self._job)
            except tk.TclError:
                pass
        self._publish()

    def hide(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.withdraw()
        self.ui.local.clear(self.name)

    def destroy(self):
        self.ui.local.clear(self.name)
        if self.win is not None and self.win.winfo_exists():
            self.win.destroy()
        self.win = None
        self._reg = []


# ---- controls -----------------------------------------------------------------------------------------------
class Btn(tk.Label):
    """Flat button. kind: primary | ghost | danger | nav | quiet."""
    KINDS = {
        "primary": (ACCENT, "#04271E", ACCENT_HOVER),
        "ghost": (CARD2, FG, BORDER),
        "danger": ("#3A1D22", "#FFB4B4", "#5A262D"),
        "quiet": (CARD, MUTED, CARD2),
        "nav": (SIDEBAR, MUTED, CARD2),
        "navon": (CARD2, FG, CARD2),
    }

    def __init__(self, parent, text, command, kind="ghost", size=11, padx=16, pady=8, anchor="center", **kw):
        bg, fg, hover = self.KINDS[kind]
        super().__init__(parent, text=text, font=font(size, bold=True), bg=bg, fg=fg, padx=px(padx), pady=px(pady),
                         cursor="hand2", anchor=anchor, **kw)
        self.command, self.kind = command, kind
        self._bg, self._fg, self._hover = bg, fg, hover
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self.config(bg=self._hover))
        self.bind("<Leave>", lambda e: self.config(bg=self._bg))

    def restyle(self, kind=None, text=None):
        if kind and kind != self.kind:
            self.kind = kind
            self._bg, self._fg, self._hover = self.KINDS[kind]
            self.config(bg=self._bg, fg=self._fg)
        if text is not None:
            self.config(text=text)


class Switch(tk.Canvas):
    """A pill toggle. `get()` reads the current value, `set(v)` writes it."""
    def __init__(self, parent, get, set_, bg=CARD):
        self.W, self.H = px(46), px(26)
        super().__init__(parent, width=self.W, height=self.H, bg=bg, highlightthickness=0, cursor="hand2")
        self.get, self.set_ = get, set_
        self._drawn = None
        self.bind("<Button-1>", lambda e: self.toggle())
        self.refresh()

    def toggle(self):
        self.set_(not self.get())
        self.refresh()

    def refresh(self):
        on = bool(self.get())
        if on == self._drawn:
            return
        self._drawn = on
        self.delete("all")
        track = ACCENT if on else "#303A4A"
        r = self.H // 2
        self.create_oval(0, 0, self.H, self.H, fill=track, outline=track)
        self.create_oval(self.W - self.H, 0, self.W, self.H, fill=track, outline=track)
        self.create_rectangle(r, 0, self.W - r, self.H, fill=track, outline=track)
        x = self.W - r - 1 if on else r + 1
        m = px(4)
        self.create_oval(x - r + m, m, x + r - m, self.H - m, fill="#04271E" if on else "#AEB8C7", outline="")


class Stepper(tk.Frame):
    """[-] value [+] for dwell-friendly numeric settings."""

    def __init__(self, parent, panel, label, get, set_, step, lo, hi, fmt=str, bg=CARD):
        super().__init__(parent, bg=bg)
        self.get, self.set_, self.step, self.lo, self.hi, self.fmt = get, set_, step, lo, hi, fmt
        tk.Label(self, text=label, font=font(10), bg=bg, fg=MUTED, anchor="w").pack(side="left", padx=(0, 10))
        self.minus = Btn(self, "−", lambda: self.bump(-1), "ghost", 13, 12, 2)
        self.value = tk.Label(self, text="", font=font(12, bold=True), bg=bg, fg=FG, width=8)
        self.plus = Btn(self, "+", lambda: self.bump(1), "ghost", 13, 12, 2)
        self.plus.pack(side="right")
        self.value.pack(side="right", padx=4)
        self.minus.pack(side="right")
        panel.reg(self.minus, f"{label} minus", lambda: self.bump(-1))
        panel.reg(self.plus, f"{label} plus", lambda: self.bump(1))
        self.refresh()

    def bump(self, direction):
        cur = self.get()
        new = max(self.lo, min(self.hi, cur + direction * self.step))
        if isinstance(self.step, float):
            new = round(new, 3)
        if new != cur:
            self.set_(new)
        self.refresh()

    def refresh(self):
        text = self.fmt(self.get())
        if text != self.value.cget("text"):
            self.value.config(text=text)


class Segmented(tk.Frame):
    """A row of mutually exclusive choices."""

    def __init__(self, parent, panel, options, get, set_, bg=CARD, size=10):
        super().__init__(parent, bg=bg)
        self.get, self.set_, self.buttons = get, set_, {}
        for value, text in options:
            b = Btn(self, text, lambda v=value: self.choose(v), "ghost", size, 12, 6)
            b.pack(side="left", padx=(0, 4))
            panel.reg(b, text, lambda v=value: self.choose(v))
            self.buttons[value] = b
        self.refresh()

    def choose(self, value):
        self.set_(value)
        self.refresh()

    def refresh(self):
        cur = self.get()
        for v, b in self.buttons.items():
            b.restyle("primary" if v == cur else "ghost")


def card(parent, title=None, subtitle=None, pad=16, bg=CARD, wrap=340):
    f = tk.Frame(parent, bg=bg, highlightbackground=BORDER, highlightthickness=1)
    inner = tk.Frame(f, bg=bg, padx=px(pad), pady=px(pad - 4))
    inner.pack(fill="both", expand=True)
    if title:
        tk.Label(inner, text=title, font=font(12, bold=True), bg=bg, fg=FG, anchor="w").pack(fill="x")
    if subtitle:
        tk.Label(inner, text=subtitle, font=font(9), bg=bg, fg=MUTED, anchor="w", justify="left",
                 wraplength=px(wrap)).pack(fill="x", pady=(2, 0))
    return f, inner


def row(parent, label, hint=None, bg=CARD):
    """A label (+ optional hint) on the left, space for a control on the right. Returns the right-hand frame."""
    r = tk.Frame(parent, bg=bg)
    r.pack(fill="x", pady=(px(8), 0))
    right = tk.Frame(r, bg=bg)
    right.pack(side="right")                 # packed first: the control is never squeezed by a long hint
    left = tk.Frame(r, bg=bg)
    left.pack(side="left", fill="x", expand=True)
    tk.Label(left, text=label, font=font(10), bg=bg, fg=FG, anchor="w").pack(fill="x")
    if hint:
        tk.Label(left, text=hint, font=font(8), bg=bg, fg=FAINT, anchor="w", justify="left",
                 wraplength=px(190)).pack(fill="x")
    return right


def tile(parent, label, value="-", color=FG, bg=CARD2, width=None):
    f = tk.Frame(parent, bg=bg, padx=px(14), pady=px(8), width=width)
    tk.Label(f, text=label, font=font(8), bg=bg, fg=MUTED, anchor="w").pack(fill="x")
    v = tk.Label(f, text=value, font=font(17, bold=True), bg=bg, fg=color, anchor="w")
    v.pack(fill="x")
    f.value = v
    return f


def pill(parent, text, color=ACCENT, bg=None):
    return tk.Label(parent, text=f"  {text}  ", font=font(9, bold=True), bg=bg or ACCENT_DK, fg=color, pady=px(3))


# ---- charts -------------------------------------------------------------------------------------------------
def draw_bars(canvas, groups, width, height, labels, colors, ymax=None, pad=(46, 14, 12, 34)):
    """Grouped vertical bars. groups: list of tuples (one value per series) aligned with labels."""
    canvas.delete("all")
    l, t, r, b = pad
    if not groups:
        canvas.create_text(width / 2, height / 2, text="No completed sessions yet", fill=FAINT, font=font(10))
        return
    top = ymax or max((max(g) for g in groups if g), default=0) or 1
    top *= 1.12
    for i in range(5):
        y = t + (height - t - b) * i / 4
        canvas.create_line(l, y, width - r, y, fill=BORDER)
        canvas.create_text(l - 6, y, text=f"{top * (1 - i / 4):.0f}", anchor="e", fill=FAINT, font=font(8))
    slot = (width - l - r) / len(groups)
    bw = min(26, slot / (len(colors) + 1))
    for gi, (g, lab) in enumerate(zip(groups, labels)):
        x0 = l + slot * gi + (slot - bw * len(colors)) / 2
        for si, v in enumerate(g):
            h = (height - t - b) * (v / top)
            canvas.create_rectangle(x0 + si * bw, height - b - h, x0 + (si + 1) * bw - 2, height - b,
                                    fill=colors[si], outline="")
        canvas.create_text(l + slot * gi + slot / 2, height - b + 14, text=lab, fill=MUTED, font=font(8))


def draw_line(canvas, series, width, height, colors, pad=(10, 8, 10, 10)):
    canvas.delete("all")
    l, t, r, b = pad
    top = max((max(s) for s in series if s), default=0) or 1
    for s, c in zip(series, colors):
        if len(s) < 2:
            continue
        pts = []
        for i, v in enumerate(s):
            pts += [l + (width - l - r) * i / (len(s) - 1), height - b - (height - t - b) * v / top]
        canvas.create_line(*pts, fill=c, width=2, smooth=True)
    if not any(len(s) > 1 for s in series):
        canvas.create_text(width / 2, height / 2, text="Collecting data...", fill=FAINT, font=font(9))
