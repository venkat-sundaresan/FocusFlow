"""Calibration: turn two short, unassisted recordings into a suggested starting profile.  Pure (no Windows calls).

Step 1  HOLD  - rest the pointer on a marker for several seconds. Gives how much the pointer wanders.
Step 2  REACH - click a short series of targets. Gives how often, and by how far, clicks land outside them.

The hold recording is replayed through the real `AdaptivePointer` filter at each smoothing strength, and the
weakest strength that calms the wander is chosen, so the recommendation reflects the actual filter rather than a
guessed formula. Dwell time, hit-area radius and the other settings follow simple, documented rules below.

This is a heuristic *starting point* to tune in the Live Lab, not a clinical measurement or a diagnosis. All
wander measurements are in logical pixels (physical pixels divided by the display scale); the radius and tolerance
settings it recommends are in physical screen pixels, the unit the engine compares them in.
"""
import math

from . import profiles
from .pointer import AdaptivePointer

STEP_MS = 4.0                  # 250 Hz, the pipeline's own sampling rate
HOLD_MIN_S = 5.0               # shorter recordings are rejected
HOLD_MAX_EXTENT_PX = 80.0      # more than this means the hand moved on purpose, not wandered
DETREND_S = 1.0                # slow drift is removed before judging tremor-band wander
REACH_MIN_CLICKS = 8
DOUBLE_CLICK_S = 0.15
STRENGTHS = tuple(range(3, 10))        # 3 = Precision, 9 = Maximum Assistance; 10 is left to the live ML policy
QUIET_RMS_PX = 0.8                     # below this the pointer is already steady
RESIDUAL_FLOOR_PX = 0.8                # the filter's 0.65 px dead-band makes lower residuals unrealistic
RESIDUAL_FRACTION = 0.35               # aim to leave at most this fraction of the wander


class CalibrationError(ValueError):
    """The recording cannot be used; the message is safe to show to the user."""


# ---- hold ----------------------------------------------------------------------------------------------------
def resample_hold(samples, t_start, t_end, step_ms=STEP_MS):
    """Zero-order-hold resampling of (t_ms, x, y) onto a fixed grid. A still hand produces no mouse events, so the
    last known position is held until the next one arrives."""
    pts = sorted((float(t), float(x), float(y)) for t, x, y in samples
                 if all(math.isfinite(v) for v in (t, x, y)))
    if not pts:
        raise CalibrationError("No pointer movement was recorded. Make sure FocusFlow is running and try again.")
    out, i = [], 0
    cur = pts[0]
    t = float(t_start)
    while t <= t_end:
        while i < len(pts) and pts[i][0] <= t:
            cur = pts[i]
            i += 1
        out.append((t, cur[1], cur[2]))
        t += step_ms
    return out


def _moving_mean(values, window):
    """Centred moving average using cumulative sums (window in samples)."""
    n = len(values)
    half = max(1, window // 2)
    cs = [0.0]
    for v in values:
        cs.append(cs[-1] + v)
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append((cs[hi] - cs[lo]) / (hi - lo))
    return out


def _detrended(xs, ys):
    w = int(DETREND_S * 1000 / STEP_MS)
    mx, my = _moving_mean(xs, w), _moving_mean(ys, w)
    return [x - a for x, a in zip(xs, mx)], [y - b for y, b in zip(ys, my)]


def _percentile(values, p):
    vals = sorted(values)
    if not vals:
        return 0.0
    k = (len(vals) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _wander(grid, scale):
    """Wander statistics of a resampled recording, in logical pixels."""
    xs = [p[1] / scale for p in grid]
    ys = [p[2] / scale for p in grid]
    dx, dy = _detrended(xs, ys)
    radial = [math.hypot(a, b) for a, b in zip(dx, dy)]
    rms = math.sqrt(sum(r * r for r in radial) / len(radial))
    # approximate dominant frequency: count full swings of the stronger axis through a hysteresis band, so
    # integer-pixel rounding around zero is not mistaken for extra cycles
    dur = (len(grid) - 1) * STEP_MS / 1000.0
    series = max((dx, dy), key=lambda s: sum(v * v for v in s))
    band = 0.3 * math.sqrt(sum(v * v for v in series) / len(series))
    state, swings = 0, 0
    for v in series:
        if v > band and state <= 0:
            swings += 1 if state == -1 else 0
            state = 1
        elif v < -band and state >= 0:
            swings += 1 if state == 1 else 0
            state = -1
    cross = swings
    return {"rms_px": rms, "p95_px": _percentile(radial, 0.95),
            "extent_px": math.hypot(max(xs) - min(xs), max(ys) - min(ys)),
            "approx_hz": cross / 2.0 / dur if dur > 0 else 0.0}


def analyze_hold(samples, t_start, t_end, scale=1.0):
    """samples: [(t_ms, x, y)] raw pointer positions (physical px) recorded while the user rests on a marker."""
    if scale <= 0 or not math.isfinite(scale):
        raise CalibrationError("Invalid display scale.")
    if (t_end - t_start) / 1000.0 < HOLD_MIN_S:
        raise CalibrationError(f"Hold the pointer still for at least {HOLD_MIN_S:.0f} seconds.")
    grid = resample_hold(samples, t_start, t_end)
    stats = _wander(grid, scale)
    if stats["extent_px"] > HOLD_MAX_EXTENT_PX:
        raise CalibrationError("The pointer moved too far. Rest your hand and keep the pointer on the marker.")
    stats["seconds"] = round((t_end - t_start) / 1000.0, 1)
    stats["grid"] = grid
    return stats


def residual_after_filter(grid, strength, scale=1.0):
    """Wander left after the real filter at `strength` (same measure as the raw wander)."""
    f = AdaptivePointer(scale=scale, cutoff_scale=profiles.cutoff_scale(strength))
    out = [(t, *f.sample((x, y), t)) for t, x, y in grid]
    return _wander(out, scale)


def choose_strength(grid, raw, scale=1.0):
    """Weakest strength in STRENGTHS whose residual wander is small enough. Returns (strength, residual, note)."""
    if raw["rms_px"] < QUIET_RMS_PX:
        return STRENGTHS[0], residual_after_filter(grid, STRENGTHS[0], scale), "quiet"
    goal = max(RESIDUAL_FLOOR_PX, RESIDUAL_FRACTION * raw["rms_px"])
    best = None
    for s in STRENGTHS:
        res = residual_after_filter(grid, s, scale)
        best = (s, res)
        if res["rms_px"] <= goal:
            return s, res, "met"
    return best[0], best[1], "capped"


# ---- reach ---------------------------------------------------------------------------------------------------
def analyze_reach(clicks):
    """clicks: [{'t': seconds, 'hit': bool, 'miss_px': distance outside the target edge in physical screen px
    (0 when hit)}]."""
    if len(clicks) < REACH_MIN_CLICKS:
        raise CalibrationError(f"Need at least {REACH_MIN_CLICKS} clicks; got {len(clicks)}.")
    misses = [c["miss_px"] for c in clicks if not c["hit"]]
    ts = sorted(c["t"] for c in clicks)
    doubles = sum(1 for a, b in zip(ts, ts[1:]) if b - a < DOUBLE_CLICK_S)
    return {"clicks": len(clicks), "miss_rate": len(misses) / len(clicks),
            "p90_miss_px": _percentile(misses, 0.9) if misses else 0.0, "double_clicks": doubles}


# ---- recommendation ------------------------------------------------------------------------------------------
class Recommendation:
    def __init__(self, values, reasons, metrics):
        self.values, self.reasons, self.metrics = values, reasons, metrics

    def to_dict(self):
        return {"values": dict(self.values), "reasons": list(self.reasons), "metrics": dict(self.metrics)}


def _round_to(v, step):
    return int(round(v / step) * step)


def recommend(hold, reach=None, can_click=True, scale=1.0):
    """hold: result of analyze_hold(); reach: result of analyze_reach() or None; can_click: the user can press
    mouse buttons comfortably. Returns a Recommendation built on the Comfort profile."""
    v = dict(profiles.BUILTIN["Comfort"]["values"])
    reasons = []
    strength, res, why = choose_strength(hold["grid"], hold, scale)
    v["smoothing_strength"] = strength
    v["jitter_enabled"] = True
    raw_rms, res_rms = hold["rms_px"], res["rms_px"]
    if why == "quiet":
        reasons.append(f"Your pointer was already steady ({raw_rms:.1f} px wander), so smoothing is kept light.")
    elif why == "met":
        reasons.append(f"Smoothing {strength}/10 cut your pointer wander from {raw_rms:.1f} px to {res_rms:.1f} px "
                       f"in a replay of your recording.")
    else:
        reasons.append(f"Even smoothing {strength}/10 left {res_rms:.1f} px of wander (from {raw_rms:.1f} px). "
                       f"Dwell clicking and a larger hit area matter more for you.")
    v["sensitivity"] = 3 if raw_rms < QUIET_RMS_PX else 5 if raw_rms < 2.0 else 8

    # dwell: steadier pointer -> shorter wait. Never below 1.5 s, so resting a hand on a control cannot click it.
    v["dwell_ms"] = 1500 if res_rms <= 1.0 else 2500 if res_rms <= 2.0 else 4000
    v["tolerance_px"] = max(6, min(16, math.ceil(2 * res["p95_px"] * scale) + 4))
    quiet_and_clicks = raw_rms < QUIET_RMS_PX and can_click
    v["dwell_enabled"] = not quiet_and_clicks
    if not can_click:
        reasons.append("Dwell clicking is on because pressing the buttons is difficult.")
    elif quiet_and_clicks:
        reasons.append("Dwell clicking is off: your pointer is steady and you can click normally.")
    else:
        reasons.append(f"Dwell time is {v['dwell_ms'] / 1000:.1f} s to match how much the pointer still moves.")

    if reach is not None:
        miss = reach["miss_rate"]
        radius = max(16, min(56, _round_to(reach["p90_miss_px"] * 1.25, 4))) if miss else 16
        v["miss_radius_px"] = v["snap_radius_px"] = v["attraction_radius_px"] = radius
        helps = miss >= 0.10
        v["near_miss_correct"] = v["adaptation_enabled"] = v["snap_enabled"] = helps
        v["attraction_strength"] = 6 if miss >= 0.25 else 4 if helps else 0
        if helps:
            reasons.append(f"{miss:.0%} of target clicks missed, usually by under {reach['p90_miss_px']:.0f} px, so "
                           f"near-miss correction and a {radius} px hit area are on.")
        else:
            reasons.append(f"Only {miss:.0%} of target clicks missed, so near-miss correction stays off.")
        if reach["double_clicks"] >= 2:
            v["bounce_filter"], v["bounce_ms"] = True, 150
            reasons.append("Some clicks repeated within 0.15 s, so the repeat-click guard is on.")
    else:
        reasons.append("No click task was run, so hit-area settings keep their Comfort defaults.")

    keys = set(profiles.PROFILE_KEYS)
    assert set(v) == keys, set(v) ^ keys
    metrics = {"raw_rms_px": round(raw_rms, 2), "residual_rms_px": round(res_rms, 2),
               "approx_hz": round(hold["approx_hz"], 1), "hold_seconds": hold["seconds"]}
    if reach is not None:
        metrics.update({"miss_rate": round(reach["miss_rate"], 3), "p90_miss_px": round(reach["p90_miss_px"], 1),
                        "double_clicks": reach["double_clicks"]})
    return Recommendation(v, reasons, metrics)


def apply_recommendation(cfg, rec, name="My calibration"):
    """Make the recommendation the live setting and save it as a custom profile. Returns the saved name."""
    cfg.set_many(dict(rec.values))
    return profiles.save_custom(cfg, name)


# ---- live collection (pure: the caller supplies time, cursor position and the pipeline trace) -----------------
HOLD_SECONDS = 8.0


def rect_distance(point, rect):
    """Distance from a point to a rectangle (left, top, right, bottom); 0 when inside."""
    dx = max(rect[0] - point[0], 0, point[0] - rect[2])
    dy = max(rect[1] - point[1], 0, point[1] - rect[3])
    return math.hypot(dx, dy)


class HoldCollector:
    """Waits for the pointer to rest on the marker, then records `hold_s` seconds of raw pointer samples.

    Call update() every ~50 ms. `trace` is the pipeline's raw trace, entries (t_ms, raw_x, raw_y, ...); only entries
    newer than what was already taken are used. Leaving the marker for longer than `grace_s` restarts the wait."""
    WAITING, RECORDING, DONE = "waiting", "recording", "done"

    def __init__(self, radius_px, hold_s=HOLD_SECONDS, settle_s=1.0, leave_factor=2.2, grace_s=0.5):
        self.radius, self.hold_s, self.settle_s = radius_px, hold_s, settle_s
        self.leave, self.grace_s = leave_factor, grace_s
        self.restarts = 0
        self._reset()

    def _reset(self):
        self.state = self.WAITING
        self.samples, self.t0, self.t_end = [], None, None
        self._on_since = self._off_since = None
        self._last_t = -math.inf

    @property
    def progress(self):
        return 0.0 if self.t0 is None else min(1.0, (self._now - self.t0) / (self.hold_s * 1000.0))

    def update(self, now_ms, cursor, center, trace=()):
        self._now = now_ms
        if self.state == self.DONE:
            return self.state
        dist = math.hypot(cursor[0] - center[0], cursor[1] - center[1])
        if self.state == self.WAITING:
            if dist <= self.radius:
                self._on_since = now_ms if self._on_since is None else self._on_since
                if now_ms - self._on_since >= self.settle_s * 1000.0:
                    self.state, self.t0, self._last_t = self.RECORDING, now_ms, now_ms
                    self.samples = [(now_ms, float(cursor[0]), float(cursor[1]))]
            else:
                self._on_since = None
            return self.state
        for e in trace:                               # RECORDING
            if e[0] > self._last_t:
                self.samples.append((float(e[0]), float(e[1]), float(e[2])))
                self._last_t = e[0]
        if dist > self.radius * self.leave:
            self._off_since = now_ms if self._off_since is None else self._off_since
            if now_ms - self._off_since > self.grace_s * 1000.0:
                self.restarts += 1
                self._reset()
                return self.state
        else:
            self._off_since = None
        if now_ms - self.t0 >= self.hold_s * 1000.0:
            self.state, self.t_end = self.DONE, now_ms
        return self.state

    def result(self, scale=1.0):
        if self.state != self.DONE:
            raise CalibrationError("The hold recording is not finished.")
        return analyze_hold(self.samples, self.t0, self.t_end, scale)
