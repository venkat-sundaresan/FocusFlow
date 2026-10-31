"""Pointer motion analysis: tremor detection, settled/intentional classification, approach metrics."""
import math
import threading
from collections import deque

WINDOW_S = 0.8        # analysis window for oscillation
MIN_STEP_PX = 2.0     # ignore sub-2px sensor noise when counting direction reversals


class Motion:
    """Snapshot returned by MotionAnalyzer.analyze()."""
    __slots__ = ("speed", "tremor", "is_tremor", "cycles", "settled", "intentional", "pos")

    def __init__(self, speed, tremor, cycles, settled, intentional, pos):
        self.speed = speed
        self.tremor = tremor            # 0..1
        self.is_tremor = tremor >= 0.5
        self.cycles = cycles            # oscillation cycles per second
        self.settled = settled
        self.intentional = intentional  # deliberate, directed movement
        self.pos = pos                  # smoothed position


class MotionAnalyzer:
    def __init__(self, sensitivity=5, hover_speed=250.0):
        self.sensitivity = sensitivity
        self.hover_speed = hover_speed
        self.rest_reset_s = 2.5
        self._lock = threading.Lock()
        self._samples = deque()   # (t, x, y)
        self._segs = deque()      # (t, reversed?, length)
        self._anchor = None
        self._last_v = None
        self._last = None
        self._still_since = None
        self._reset_approach()

    # -- approach metrics: "how much effort did reaching this click take?" ----
    def _reset_approach(self):
        self._a_start = None
        self._a_path = 0.0
        self._a_rev = 0

    def take_approach(self, now):
        """Return {path_px, reversals, duration_ms} since the pointer last rested, then reset."""
        with self._lock:
            if self._a_start is None:
                out = None
            else:
                out = {"path_px": int(self._a_path), "reversals": self._a_rev,
                       "duration_ms": int((now - self._a_start) * 1000)}
            self._reset_approach()
            return out

    def feed(self, t, x, y):
        with self._lock:
            self._samples.append((t, x, y))
            while self._samples and t - self._samples[0][0] > 1.5:
                self._samples.popleft()
            if self._last is not None:
                d = math.hypot(x - self._last[0], y - self._last[1])
                if d >= 1.0:
                    self._still_since = None
                    if self._a_start is None:
                        self._a_start = t
                    self._a_path += d
                elif self._still_since is None:
                    self._still_since = t
                elif t - self._still_since > self.rest_reset_s:
                    self._reset_approach()
            self._last = (x, y)
            # direction reversals between successive >=2px segments
            if self._anchor is None:
                self._anchor = (x, y)
            else:
                vx, vy = x - self._anchor[0], y - self._anchor[1]
                if math.hypot(vx, vy) >= MIN_STEP_PX:
                    rev = False
                    if self._last_v is not None:
                        rev = (vx * self._last_v[0] + vy * self._last_v[1]) < 0
                    self._last_v = (vx, vy)
                    self._anchor = (x, y)
                    self._segs.append((t, rev, math.hypot(vx, vy)))
                    if rev and self._a_start is not None:
                        self._a_rev += 1
            while self._segs and t - self._segs[0][0] > WINDOW_S:
                self._segs.popleft()

    def analyze(self, now):
        with self._lock:
            s = list(self._samples)
            segs = list(self._segs)
        if len(s) < 10:
            pos = (s[-1][1], s[-1][2]) if s else (0, 0)
            return Motion(0.0, 0.0, 0.0, True, False, pos)
        a, b = s[-10:-5], s[-5:]
        ax, ay = sum(p[1] for p in a) / 5, sum(p[2] for p in a) / 5
        bx, by = sum(p[1] for p in b) / 5, sum(p[2] for p in b) / 5
        dt = (sum(p[0] for p in b) - sum(p[0] for p in a)) / 5 or 1e-3
        speed = math.hypot(bx - ax, by - ay) / dt

        win = [p for p in s if now - p[0] <= WINDOW_S]
        cycles = 0.0
        level = 0.0
        intentional = False
        if len(win) >= 8 and win[-1][0] - win[0][0] >= 0.4:
            span_t = win[-1][0] - win[0][0]
            revs = sum(1 for g in segs if g[1])
            path = sum(g[2] for g in segs)
            net = math.hypot(win[-1][1] - win[0][1], win[-1][2] - win[0][2])
            xs, ys = [p[1] for p in win], [p[2] for p in win]
            extent = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
            cycles = (revs / 2.0) / span_t
            straight = net / path if path > 0 else 1.0
            intentional = net > 80 and straight > 0.6
            if not intentional and extent <= 80 and straight < 0.6:
                thr = 4.0 - 0.3 * self.sensitivity   # cycles/s that count as "definitely tremor"
                level = min(1.0, cycles / (2.0 * thr))
        return Motion(speed, level, cycles, speed < self.hover_speed, intentional, (bx, by))
