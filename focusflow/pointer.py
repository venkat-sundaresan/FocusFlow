"""Pointer-stabilisation core: a port of PointerLab's `pointer.ts` to native Windows mouse input.

AdaptivePointer, ErraticDetector, MotionTracker, nearby_target and the miss-streak rule are the original
algorithms with the original constants (times in milliseconds, distances in "reference" pixels).

Differences from the TypeScript original, all forced by moving from browser events to a native hook:
  * `scale`        - the original constants are CSS pixels; Windows reports physical pixels, so every distance
                     constant is multiplied by the display scale (1.5 at 150 %). scale=1 is the original.
  * `cutoff_scale` - the user's "smoothing strength"; 1.0 reproduces the original cut-off curve exactly.
  * PointerPipeline - the browser gave absolute positions; a swallowed hook event does not move the cursor, so the
                     pipeline integrates the OS-computed deltas into a raw position of its own, and feeds the filter
                     at <= 250 Hz (a browser delivers ~60-120 pointermove/s; a gaming mouse reports 1000+).
  * Attractor      - new, not in the original: bounded magnetic pull toward one unambiguous target.
The pure classes below have no Windows imports, so they are unit-testable anywhere.
"""
import math
import threading
from collections import deque

# ---- constants lifted unchanged from pointer.ts -------------------------------------------------------------
GAP_RESET_MS = 150          # input gap after which the filter restarts from the raw point
MAX_DT_MS = 50
MIN_DT_MS = 1
SPEED_TAU = 20              # speed low-pass, rad/s
DEADBAND_PX = 0.65
BASE_CUTOFF_HZ = 3.0
CUTOFF_PER_SPEED = 0.09
NEAR_BUTTON_CUTOFF_HZ = 2.5
NEAR_REVERSAL_STEP_PX = 7
NEAR_REVERSAL_GAP_PX = 8
MAX_LAG_PX = 8              # bound on how far the displayed pointer may trail the raw one
SETTLE_AFTER_MS = 24
SETTLE_CUTOFF_HZ = 12
SEGMENT_PX = 3              # MotionTracker: accumulate this much before judging a direction change
ERRATIC_WINDOW_MS = 1200
ERRATIC_MAX_SEGMENT_PX = 24
ERRATIC_COUNT = 6
ERRATIC_COOLDOWN_MS = 2500
NEARBY_RADIUS_PX = 36
NEARBY_AMBIGUITY_PX = 8
MISSES_TO_ADAPT = 3


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def hit_target(point, targets):
    """targets: iterable of (id, (left, top, right, bottom)). First containing id, else None."""
    for tid, (l, t, r, b) in targets:
        if l <= point[0] <= r and t <= point[1] <= b:
            return tid
    return None


def nearby_target(point, targets, radius=NEARBY_RADIUS_PX, ambiguity=NEARBY_AMBIGUITY_PX):
    """Only attribute a miss to an unambiguous nearby target, never to distant whitespace."""
    cands = []
    for tid, (l, t, r, b) in targets:
        d = math.hypot(max(l - point[0], 0, point[0] - r), max(t - point[1], 0, point[1] - b))
        if d <= radius:
            cands.append((d, tid))
    cands.sort(key=lambda c: c[0])
    if not cands or (len(cands) > 1 and cands[1][0] - cands[0][0] < ambiguity):
        return None
    return cands[0][1]


class MotionTracker:
    """Retains the original >90-degree direction-change rule over 3 px segments so that high-frequency
    devices with sub-pixel events are measured consistently. `corrective` counts the short reversals
    (<= 24 px) that make up fine-correction movements."""

    def __init__(self):
        self.total_distance = 0.0
        self.direction_changes = 0
        self.corrective = 0
        self._previous = self._anchor = self._vector = None

    def reset_segment(self):
        self._previous = self._anchor = self._vector = None

    def add(self, point):
        if self._previous is not None:
            self.total_distance += distance(point, self._previous)
        self._previous = point
        if self._anchor is None:
            self._anchor = point
        vx, vy = point[0] - self._anchor[0], point[1] - self._anchor[1]
        length = math.hypot(vx, vy)
        reversed_ = False
        if length >= SEGMENT_PX:
            reversed_ = self._vector is not None and vx * self._vector[0] + vy * self._vector[1] < 0
            if reversed_:
                self.direction_changes += 1
                if length <= ERRATIC_MAX_SEGMENT_PX:
                    self.corrective += 1
            self._vector = (vx, vy)
            self._anchor = point
        return reversed_, length


class AdaptivePointer:
    """Speed-adaptive low-pass filter. No target coordinates enter the filter: proximity to a button only
    adds resistance to brief, small reversals."""

    def __init__(self, scale=1.0, cutoff_scale=1.0):
        self.scale = float(scale)
        self.cutoff_scale = float(cutoff_scale)
        self.reset()

    def reset(self):
        self.raw = self.output = self.vector = None
        self.speed = 0.0                      # reference px / s
        self.last_time = self.last_input_time = 0.0

    def sample(self, point, time, near_button=False):
        s = self.scale
        point = (float(point[0]), float(point[1]))
        if self.raw is None or self.output is None or time - self.last_input_time > GAP_RESET_MS:
            self.raw = self.output = point
            self.last_time = self.last_input_time = time
            self.speed, self.vector = 0.0, None
            return self.output
        dt = max(MIN_DT_MS, min(MAX_DT_MS, time - self.last_input_time)) / 1000.0
        vx, vy = point[0] - self.raw[0], point[1] - self.raw[1]
        step = math.hypot(vx, vy) / s
        reversal = self.vector is not None and vx * self.vector[0] + vy * self.vector[1] < 0
        inst = step / dt
        self.speed += (1 - math.exp(-dt * SPEED_TAU)) * (inst - self.speed)
        gap = distance(point, self.output) / s
        # tiny motion stays stable; sustained motion breaks out of the sub-pixel deadband
        if near_button and reversal and step < NEAR_REVERSAL_STEP_PX and gap < NEAR_REVERSAL_GAP_PX:
            cutoff = NEAR_BUTTON_CUTOFF_HZ
        else:
            cutoff = BASE_CUTOFF_HZ + max(self.speed, inst * 0.65) * CUTOFF_PER_SPEED
        cutoff *= self.cutoff_scale
        if gap > DEADBAND_PX:
            alpha = 1 - math.exp(-2 * math.pi * cutoff * dt)
            ox = self.output[0] + alpha * (point[0] - self.output[0])
            oy = self.output[1] + alpha * (point[1] - self.output[1])
            lag = math.hypot(point[0] - ox, point[1] - oy) / s
            if lag > MAX_LAG_PX:                  # bound spatial lag even during fast travel
                k = MAX_LAG_PX / lag
                ox, oy = point[0] + (ox - point[0]) * k, point[1] + (oy - point[1]) * k
            self.output = (ox, oy)
        if step > 0.2:
            self.vector = (vx, vy)
        self.raw = point
        self.last_time = self.last_input_time = time
        return self.output

    def settle(self, time):
        """Finish a deliberate movement without needing another hardware event. Sub-pixel noise stays
        inside the deadband instead of being animated indefinitely."""
        if self.raw is None or self.output is None:
            return None
        if time - self.last_input_time > SETTLE_AFTER_MS and distance(self.raw, self.output) / self.scale > DEADBAND_PX:
            dt = min(32.0, time - self.last_time) / 1000.0
            alpha = 1 - math.exp(-2 * math.pi * SETTLE_CUTOFF_HZ * dt)
            self.output = (self.output[0] + alpha * (self.raw[0] - self.output[0]),
                           self.output[1] + alpha * (self.raw[1] - self.output[1]))
        self.last_time = time
        return self.output

    def is_settling(self):
        return (self.raw is not None and self.output is not None
                and distance(self.raw, self.output) / self.scale > DEADBAND_PX)


class ErraticDetector:
    """Heuristic only: repeated short reversals in a rolling window. Not a diagnosis."""

    def __init__(self):
        self._reversals = []
        self._last_flag = -math.inf

    def observe(self, time, reversed_, length):
        self._reversals = [t for t in self._reversals if time - t <= ERRATIC_WINDOW_MS]
        if reversed_ and length <= ERRATIC_MAX_SEGMENT_PX:
            self._reversals.append(time)
        if len(self._reversals) >= ERRATIC_COUNT and time - self._last_flag >= ERRATIC_COOLDOWN_MS:
            self._last_flag = time
            self._reversals = []
            return True
        return False


class MissTracker:
    """The original recordMiss rule: a nearby, unambiguous target missed three times in a row (with no
    successful hit between) gets a larger hit area. A success clears the streak. Reversible."""

    def __init__(self):
        self.counts = {}
        self.grown = set()

    def record(self, target_id, enabled=True):
        """Returns True exactly when this miss newly enlarges the target."""
        if target_id is None:
            return False
        self.counts[target_id] = self.counts.get(target_id, 0) + 1
        if enabled and self.counts[target_id] >= MISSES_TO_ADAPT and target_id not in self.grown:
            self.grown.add(target_id)
            return True
        return False

    def success(self, target_id):
        self.counts[target_id] = 0

    def clear(self):
        self.counts.clear()
        self.grown.clear()


class Attractor:
    """Bounded magnetic pull toward one unambiguous target (native-only addition, not in PointerLab).

    The pull is an *offset* added to the stabilised pointer, so the filter's lag bound is untouched.
    It only grows while the pointer is slow, never exceeds `radius`, stops once the pointer is inside the
    target, and drains quickly the moment the user moves deliberately."""
    FAST_PX_S = 600.0
    MAX_RATE_PX_S = 420.0

    def __init__(self, scale=1.0):
        self.scale = scale
        self.ox = self.oy = 0.0
        self.key = None

    def reset(self):
        self.ox = self.oy = 0.0
        self.key = None

    @property
    def active(self):
        return abs(self.ox) > 0.05 or abs(self.oy) > 0.05

    def _decay(self, rate, dt):
        k = min(1.0, rate * dt)
        self.ox -= self.ox * k
        self.oy -= self.oy * k
        if abs(self.ox) < 0.2 and abs(self.oy) < 0.2:
            self.ox = self.oy = 0.0

    def update(self, base, speed_px_s, target, strength, radius, dt):
        """target: (key, (l, t, r, b)) or None. strength 0..10. Returns the (ox, oy) offset."""
        s = self.scale
        if target is None or strength <= 0 or speed_px_s > self.FAST_PX_S * s:
            self._decay(14.0 if speed_px_s > self.FAST_PX_S * s else 5.0, dt)
            if not self.active:
                self.key = None
            return self.ox, self.oy
        key, (l, t, r, b) = target
        if key != self.key:
            if self.active:                      # finish draining the previous pull first
                self._decay(8.0, dt)
                return self.ox, self.oy
            self.key = key
        cx, cy = base[0] + self.ox, base[1] + self.oy
        if l <= cx < r and t <= cy < b:          # already inside: hold, do not push further
            return self.ox, self.oy
        inset = min(4 * s, (r - l) / 4.0, (b - t) / 4.0)
        gx = min(max(base[0], l + inset), r - 1 - inset)
        gy = min(max(base[1], t + inset), b - 1 - inset)
        dx, dy = gx - base[0], gy - base[1]
        need = math.hypot(dx, dy)
        cap = radius * s
        if need > cap:
            return self.ox, self.oy              # target is further than the allowed pull
        step = (strength / 10.0) * self.MAX_RATE_PX_S * s * dt
        ex, ey = dx - self.ox, dy - self.oy
        rem = math.hypot(ex, ey)
        if rem > 1e-6:
            f = min(1.0, step / rem)
            self.ox += ex * f
            self.oy += ey * f
        return self.ox, self.oy


class PointerPipeline:
    """Turns raw hook events into a stabilised cursor, and records what it did.

    `apply(x, y)` places the OS cursor and returns where it actually ended up (monitors clamp it).
    It is only ever called with the pipeline lock held, from the hook thread or the 125 Hz tick thread.
    """
    SAMPLE_MS = 4.0
    JUMP_PX = 500

    def __init__(self, scale=1.0, bounds=None, apply=None, trace_len=900):
        self.lock = threading.RLock()
        self.scale = scale
        self.bounds = bounds                      # (left, top, right, bottom) virtual desktop, or None
        self._apply = apply or (lambda x, y: (x, y))
        self.filter = AdaptivePointer(scale)
        self.attractor = Attractor(scale)
        self.smoothing = False                    # jitter reduction on/off
        self.owns_cursor = False                  # smoothing or attraction: events are swallowed + re-placed
        self.near_button = False                  # set by the engine from target detection
        self.attract_target = None                # (key, rect) set by the engine
        self.attract_strength = 0
        self.attract_radius = 36
        self.on_erratic = None
        self.trace = deque(maxlen=trace_len)      # (t_ms, rx, ry, ox, oy, jitter_flag)
        self.events = deque(maxlen=64)            # (t_ms, kind, x, y)
        self.reset_stats()
        self.cursor = self.raw = self.out = None
        self._last_event = self._last_sample = self._last_tick = -1e9
        self._dirty = False
        self.speed = 0.0
        # Optional, bounded acquisition only. No ML imports, queues or inference in the hook.
        self.ml_capture = False
        self.ml_raw = deque(maxlen=1024)
        self.ml_epoch = 0
        self._ml_sample_ms = -math.inf
        self.ml_extra = 0.0
        self._ml_expires_ms = 0.0
        self._base_cutoff_scale = 1.0

    # -- configuration -----------------------------------------------------------------------------
    def configure(self, smoothing, cutoff_scale, attraction=0):
        with self.lock:
            owns = bool(smoothing) or attraction > 0
            if smoothing != self.smoothing or owns != self.owns_cursor:
                self.filter.reset()
                self.raw = self.out = None
                self.cursor = None
                self.attractor.reset()
                self._reset_ml_samples()
            self.smoothing = bool(smoothing)
            self.owns_cursor = owns
            self.attract_strength = attraction if owns else 0
            self._base_cutoff_scale = self.filter.cutoff_scale = cutoff_scale
            self.ml_extra = 0.0

    def set_scale(self, scale):
        """Display scale changed (pointer moved to a monitor with a different DPI). Caller holds the lock."""
        self.scale = self.filter.scale = self.attractor.scale = float(scale)
        self._reset_ml_samples()
        self.set_ml_adjustment(0, 0)

    def _reset_ml_samples(self):
        self.ml_raw.clear()
        self.ml_epoch += 1
        self._ml_sample_ms = -math.inf
        self.ml_extra = 0.0
        self.filter.cutoff_scale = self._base_cutoff_scale

    def enable_ml_capture(self, enabled):
        with self.lock:
            if bool(enabled) != self.ml_capture:
                self.ml_capture = bool(enabled)
                self._reset_ml_samples()

    def _capture_ml_raw(self, t):
        if self.ml_capture and self.raw is not None and t - self._ml_sample_ms >= self.SAMPLE_MS:
            self.ml_raw.append((t / 1000.0, self.raw[0], self.raw[1], self.scale))
            self._ml_sample_ms = t

    def ml_snapshot(self):
        """Called only by the engine, at most twice per second; timestamps are monotonic seconds."""
        with self.lock:
            return self.ml_epoch, tuple(self.ml_raw)

    def set_ml_adjustment(self, extra, expires_ms):
        """Temporary smoothing-only lease; does not enable assistance or change settings."""
        with self.lock:
            self.ml_extra = max(0.0, min(2.0, float(extra))) if self.smoothing else 0.0
            self._ml_expires_ms = expires_ms
            self.filter.cutoff_scale = self._base_cutoff_scale * 2 ** (-self.ml_extra / 3)

    def _expire_ml(self, t):
        if self.ml_extra and (t >= self._ml_expires_ms or self.speed >= 250):
            self.ml_extra = 0.0
            self.filter.cutoff_scale = self._base_cutoff_scale

    def reset_stats(self):
        self.raw_motion, self.out_motion = MotionTracker(), MotionTracker()
        self.erratic = ErraticDetector()
        self.erratic_flags = 0

    def stats(self):
        with self.lock:
            return {"raw_distance": self.raw_motion.total_distance,
                    "stabilised_distance": self.out_motion.total_distance,
                    "raw_changes": self.raw_motion.direction_changes,
                    "stabilised_changes": self.out_motion.direction_changes,
                    "raw_corrective": self.raw_motion.corrective,
                    "stabilised_corrective": self.out_motion.corrective,
                    "erratic_flags": self.erratic_flags}

    def snapshot(self, since_ms=None):
        with self.lock:
            tr = [e for e in self.trace if since_ms is None or e[0] >= since_ms]
            return tr, list(self.events), self.speed, self.smoothing

    # -- internals ----------------------------------------------------------------------------------
    def _clamp(self, p):
        if self.bounds is None:
            return p
        l, t, r, b = self.bounds
        return (min(max(p[0], l), r - 1), min(max(p[1], t), b - 1))

    def _final(self):
        o = self.attractor
        return self._clamp((self.out[0] + o.ox, self.out[1] + o.oy))

    def _record(self, t):
        reversed_, length = self.raw_motion.add(self.raw)
        jitter = False
        if self.erratic.observe(t, reversed_, length):
            self.erratic_flags += 1
            jitter = True
            self.events.append((t, "erratic", self.raw[0], self.raw[1]))
            if self.on_erratic:
                try:
                    self.on_erratic()
                except Exception:
                    pass
        elif reversed_ and length <= ERRATIC_MAX_SEGMENT_PX:
            self.events.append((t, "reversal", self.raw[0], self.raw[1]))
        self.out_motion.add(self.out)
        self.trace.append((t, self.raw[0], self.raw[1], self.out[0], self.out[1], jitter))

    def _sample(self, t):
        self._expire_ml(t)
        self.out = self.filter.sample(self.raw, t, self.near_button) if self.smoothing else self.raw
        self._last_sample = t
        self._dirty = False
        self._record(t)

    def _commit(self, t):
        """Move the OS cursor if the integer target changed. Returns True when it moved."""
        fx, fy = self._final()
        target = (int(round(fx)), int(round(fy)))
        if target == self.cursor:
            return False
        self.cursor = target                     # before the call: a re-entrant event must see it
        actual = self._apply(*target)
        if actual is not None:
            self.cursor = (int(actual[0]), int(actual[1]))
        return True

    # -- entry points -------------------------------------------------------------------------------
    def on_move(self, x, y, t):
        """Hook thread. Returns True when the event must be swallowed."""
        with self.lock:
            if self.raw is None or (self.cursor is not None and
                    math.hypot(x - self.cursor[0], y - self.cursor[1]) > self.JUMP_PX * self.scale):
                self._reset_ml_samples()
            if self.cursor is None or t - self._last_event > GAP_RESET_MS \
                    or math.hypot(x - self.cursor[0], y - self.cursor[1]) > self.JUMP_PX * self.scale:
                # first event, long pause or teleport (tablet, remote desktop, another app moved the
                # cursor): the delta is meaningless, restart from where the OS says we are.
                self.filter.reset()
                self.attractor.reset()
                self.cursor = (x, y)
                self.raw = self.out = (float(x), float(y))
                self._last_event = self._last_sample = t
                self._dirty = False
                self.trace.append((t, x, y, x, y, False))
                self._capture_ml_raw(t)
                return False
            if (x, y) == self.cursor:
                return False
            self.raw = self._clamp((self.raw[0] + (x - self.cursor[0]), self.raw[1] + (y - self.cursor[1])))
            self._capture_ml_raw(t)
            gap = t - self._last_event
            self._last_event = t
            if gap > 0:
                inst = distance((x, y), self.cursor) / gap * 1000.0 / self.scale
                self.speed += min(1.0, gap / 50.0) * (inst - self.speed)
            if not self.owns_cursor:
                self.out = self.raw
                self.cursor = (x, y)
                if t - self._last_sample >= self.SAMPLE_MS:
                    self._last_sample = t
                    self._record(t)
                return False
            self._dirty = True
            if t - self._last_sample >= self.SAMPLE_MS:
                self._sample(t)
                self._commit(t)
            return True

    def tick(self, t):
        """~125 Hz. Feeds pending raw input, lets the pointer settle, runs the attractor."""
        with self.lock:
            self._capture_ml_raw(t)  # Preserve real idle intervals without polling the smoothed cursor.
            self._expire_ml(t)
            if not self.owns_cursor or self.raw is None or self.out is None:
                self._last_tick = t
                return False
            if t - self._last_event > 250:
                self.speed *= 0.8
            if self._dirty and t - self._last_sample >= self.SAMPLE_MS:
                self._sample(t)
            elif self.smoothing and self.filter.is_settling():
                nxt = self.filter.settle(t)
                if nxt is not None:
                    self.out = nxt
            dt = max(0.001, min(0.05, (t - self._last_tick) / 1000.0)) if self._last_tick > 0 else 0.008
            self._last_tick = t
            self.attractor.update(self.out, self.speed, self.attract_target, self.attract_strength,
                                  self.attract_radius, dt)
            return self._commit(t)
