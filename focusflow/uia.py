"""Windows UI Automation probing, run on one dedicated worker thread (COM apartment).

UIA bounding rectangles are *hints*. We never trust them blindly:
  * implausible rectangles (tiny / screen-sized) are rejected,
  * every target remembers `hit_point`, a point that really hit-tests into it,
  * a snap point (e.g. the centre) is hit-tested again before FocusFlow clicks there.
"""
import queue
import threading
import time
from concurrent.futures import Future

from .targets import Target, contains, plausible_rect
from . import winapi

# UIA control type ids that normally respond to a click
CLICKABLE = {
    50000: "Button", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit", 50005: "Hyperlink",
    50007: "ListItem", 50011: "MenuItem", 50013: "RadioButton", 50019: "TabItem",
    50024: "TreeItem", 50031: "SplitButton",
}
# Text-ish surfaces: a click here is probably aiming at text (caret / selection), never "near a button"
TEXTY = {50004, 50020, 50030, 50029}   # Edit, Text, Document, Table
# property ids: Is{Invoke,Toggle,SelectionItem,ExpandCollapse}PatternAvailable
PATTERN_PROPS = (30031, 30041, 30036, 30028)


class UiaWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="ff-uia")
        self._q = queue.Queue()
        self.ok = False
        self._ready = threading.Event()
        self._screen_area = 1920 * 1080

    def submit(self, fn):
        fut = Future()
        self._q.put((fn, fut))
        return fut

    def run(self):
        try:
            import comtypes
            import comtypes.client
            comtypes.CoInitializeEx()
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as UIA
            self._UIA = UIA
            self._auto = comtypes.CoCreateInstance(UIA.CUIAutomation._reg_clsid_,
                                                   interface=UIA.IUIAutomation,
                                                   clsctx=comtypes.CLSCTX_INPROC_SERVER)
            self._walker = self._auto.ControlViewWalker
            self.ok = True
        except Exception:
            self.ok = False
        finally:
            self._ready.set()
        if not self.ok:
            return
        _, _, sw, sh = winapi.virtual_screen()
        self._screen_area = max(sw * sh, 1)
        while True:
            fn, fut = self._q.get()
            try:
                fut.set_result(fn(self))
            except Exception as exc:  # a misbehaving app must never kill the worker
                fut.set_exception(exc)

    def wait_ready(self, timeout=5):
        self._ready.wait(timeout)
        return self.ok

    # -- everything below runs on the worker thread ------------------------
    def _is_interactive(self, el):
        try:
            if el.CurrentIsOffscreen or not el.CurrentIsEnabled:
                return None
            ctype = el.CurrentControlType
            if ctype in CLICKABLE:
                return CLICKABLE[ctype]
            for prop in PATTERN_PROPS:
                if el.GetCurrentPropertyValue(prop):
                    return "Control"
        except Exception:
            return None
        return None

    def _target_for(self, el, ctype, hit):
        r = el.CurrentBoundingRectangle
        rect = (r.left, r.top, r.right, r.bottom)
        if not plausible_rect(rect, self._screen_area):
            return None
        try:
            name = (el.CurrentName or "")[:60]
        except Exception:
            name = ""
        if ctype == "Edit":
            name = name or "text field"   # never read a field's value, only its label
        return Target(key=f"{rect}|{name}|{ctype}", rect=rect, name=name, ctype=ctype, hit_point=hit)

    def interactive_at(self, x, y, el=None):
        """The nearest interactive ancestor-or-self of the element under (x, y), or None."""
        if el is None:
            el = self._auto.ElementFromPoint(self._UIA.tagPOINT(x, y))
        for _ in range(5):
            if el is None:
                return None
            ctype = self._is_interactive(el)
            if ctype:
                return self._target_for(el, ctype, (x, y))
            try:
                el = self._walker.GetParentElement(el)
            except Exception:
                return None
        return None

    def probe(self, x, y, radius, want_nearby):
        """Target under the pointer plus interactive neighbours found by sampling rings around it."""
        raw = self._auto.ElementFromPoint(self._UIA.tagPOINT(x, y))
        try:
            texty = raw.CurrentControlType in TEXTY
        except Exception:
            texty = False
        under = self.interactive_at(x, y, raw)
        found = {}
        if want_nearby and under is None:
            step = max(12, radius // 2)          # two rings are enough to find a clear nearest neighbour
            for r in range(step, radius + 1, step):
                for dx, dy in ((r, 0), (-r, 0), (0, r), (0, -r), (r, r), (-r, r), (r, -r), (-r, -r)):
                    t = self.interactive_at(x + dx, y + dy)
                    if t is None:
                        continue
                    old = found.get(t.key)
                    if old is None:
                        found[t.key] = t
                    else:  # keep the verified hit point nearest the centre
                        cx, cy = t.center
                        if (abs(t.hit_point[0] - cx) + abs(t.hit_point[1] - cy) <
                                abs(old.hit_point[0] - cx) + abs(old.hit_point[1] - cy)):
                            old.hit_point = t.hit_point
        return ProbeResult((x, y), time.perf_counter(), under, list(found.values()), texty)

    def scan_window(self, hwnd, limit=250, budget_s=0.8, max_depth=12):
        """Interactive controls of one top-level window in reading order (for switch scanning).
        Bounded by element count and wall-clock time so a huge UI tree can never stall the worker."""
        root = self._auto.ElementFromHandle(hwnd)
        deadline = time.perf_counter() + budget_s
        queue_, found, seen = [(root, 0)], {}, 0
        while queue_ and len(found) < limit and time.perf_counter() < deadline and seen < 4000:
            el, depth = queue_.pop(0)
            seen += 1
            ctype = self._is_interactive(el)
            if ctype:
                try:
                    r = el.CurrentBoundingRectangle
                    center = ((r.left + r.right) // 2, (r.top + r.bottom) // 2)
                    t = self._target_for(el, ctype, center)
                except Exception:
                    t = None
                if t is not None and t.key not in found:
                    found[t.key] = t
            if depth >= max_depth:
                continue
            try:
                child = self._walker.GetFirstChildElement(el)
                while child is not None and len(queue_) < 6000:
                    queue_.append((child, depth + 1))
                    child = self._walker.GetNextSiblingElement(child)
            except Exception:
                continue
        return sorted(found.values(), key=lambda t: (t.rect[1] // 28, t.rect[0]))

    def verified_point(self, target, point):
        """`point` if it genuinely hit-tests into `target`, otherwise the target's remembered hit point."""
        if contains(target.rect, point):
            try:
                t = self.interactive_at(point[0], point[1])
                if t is not None and t.key == target.key:
                    return point
            except Exception:
                pass
        return target.hit_point or point


class ProbeResult:
    __slots__ = ("pos", "ts", "under", "nearby", "texty")

    def __init__(self, pos, ts, under, nearby, texty=False):
        self.pos, self.ts, self.under, self.nearby, self.texty = pos, ts, under, nearby, texty
