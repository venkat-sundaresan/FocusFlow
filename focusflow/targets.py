"""Target geometry and the click-assist policy (pure functions, no Windows calls)."""
import math
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

Rect = Tuple[int, int, int, int]  # left, top, right, bottom


@dataclass
class Target:
    key: str
    rect: Rect
    name: str = ""
    ctype: str = ""
    source: str = "uia"                          # uia | local
    hit_point: Optional[Tuple[int, int]] = None  # a point known to hit-test into this target
    action: Optional[Callable] = field(default=None, repr=False, compare=False)
    dwell_ms: Optional[int] = None               # own dwell time (on-screen keyboard keys); None = the profile's
    data: object = field(default=None, repr=False, compare=False)   # source-specific payload (caption: the window)

    @property
    def center(self):
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)


def contains(rect, pt, margin=0):
    l, t, r, b = rect
    return l - margin <= pt[0] < r + margin and t - margin <= pt[1] < b + margin


def dist_to_rect(pt, rect):
    """0 when inside, otherwise the distance to the nearest edge."""
    l, t, r, b = rect
    dx = max(l - pt[0], 0, pt[0] - (r - 1))
    dy = max(t - pt[1], 0, pt[1] - (b - 1))
    return math.hypot(dx, dy)


MAX_TARGET_AREA_FRACTION = 0.12   # bigger than ~500x500 on a 1080p screen is a container, not a button


def plausible_rect(rect, screen_area):
    """UIA rectangles are hints, not truth: drop empty, tiny or container-sized ones."""
    l, t, r, b = rect
    w, h = r - l, b - t
    return w >= 6 and h >= 6 and w * h < MAX_TARGET_AREA_FRACTION * screen_area


def nearest_unambiguous(pt, targets, radius, margin=8.0):
    """Closest target within `radius` that is clearly closer than the runner-up, else (None, dist)."""
    ranked = sorted(((dist_to_rect(pt, t.rect), t) for t in targets), key=lambda p: p[0])
    ranked = [p for p in ranked if p[0] <= radius]
    if not ranked:
        return None, 0.0
    if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < margin:
        return None, ranked[0][0]
    return ranked[0][1], ranked[0][0]


def snap_point(target, pt):
    """Where to click. Small targets: centre. Big ones (text areas, list rows): nearest interior point."""
    l, t, r, b = target.rect
    if r - l <= 220 and b - t <= 90:
        return target.center
    inset = 4
    return (min(max(pt[0], l + inset), r - 1 - inset), min(max(pt[1], t + inset), b - 1 - inset))


def normalized_offset(pt, rect):
    """Landing point relative to the target centre in units of its half-size (|n| <= 1 is inside)."""
    l, t, r, b = rect
    hw, hh = max((r - l) / 2.0, 1.0), max((b - t) / 2.0, 1.0)
    return ((pt[0] - (l + r) / 2.0) / hw, (pt[1] - (t + b) / 2.0) / hh)


@dataclass
class HitCache:
    pos: Tuple[int, int]
    ts: float
    under: Optional[Target]
    nearest: Optional[Target]
    nearest_dist: float
    texty: bool = False   # pointer is over text / a document: never redirect clicks away from it


def decide_physical_click(cache, click, now, miss_radius, correct_enabled, max_age=0.4, max_drift=6,
                          grown=frozenset(), grow_factor=2.0):
    """What to do with a real mouse click, given what we knew about the pointer's surroundings.

    Returns (action, target) with action in:
      'pass'       let it through untouched (unknown / not our business)
      'manual_hit' it landed on an interactive target (record it, let it through)
      'redirect'   swallow it and click `target` instead (a near-miss we can fix)
      'miss'       a near-miss we noticed but must not alter

    `grown` holds the keys of targets whose hit area was enlarged after repeated nearby misses: those are
    corrected out to `grow_factor` x the normal radius even when immediate correction is off. Text and
    document surfaces are never redirected, grown or not.
    """
    if cache is None or now - cache.ts > max_age:
        return "pass", None
    if math.hypot(click[0] - cache.pos[0], click[1] - cache.pos[1]) > max_drift:
        return "pass", None
    if cache.under is not None:
        return "manual_hit", cache.under
    near = cache.nearest
    if near is not None:
        is_grown = near.key in grown
        radius = miss_radius * grow_factor if is_grown else miss_radius
        if cache.nearest_dist <= radius:
            fix = (correct_enabled or is_grown) and not cache.texty
            return ("redirect" if fix else "miss"), near
    return "pass", None


class LocalTargets:
    """Rectangles of FocusFlow's own controls (published by the Tk thread) for the engine to dwell on."""

    def __init__(self):
        import threading
        self._lock = threading.Lock()
        self._by_owner = {}
        self.dispatch = None   # set by the UI: runs a callable on the Tk thread

    def set(self, owner, targets):
        with self._lock:
            self._by_owner[owner] = targets

    def clear(self, owner):
        with self._lock:
            self._by_owner.pop(owner, None)

    def all(self):
        with self._lock:
            return [t for group in self._by_owner.values() for t in group]

    def hit(self, x, y):
        with self._lock:
            groups = list(self._by_owner.values())
        for group in reversed(groups):          # most recently shown window wins
            for t in group:
                if contains(t.rect, (x, y)):
                    return t
        return None

    def near(self, x, y, radius):
        """Own controls within `radius` of the point but not under it."""
        out = []
        with self._lock:
            groups = list(self._by_owner.values())
        for group in groups:
            for t in group:
                if 0 < dist_to_rect((x, y), t.rect) <= radius:
                    if t.hit_point is None:
                        t.hit_point = t.center
                    out.append(t)
        return out

    def run(self, target):
        if self.dispatch and target.action:
            self.dispatch(target.action)
