"""The always-on loop: stabilise the pointer, find targets, drive dwell, adapt hit areas, log it all.

Threads:
  hook thread     (winapi.MouseHook)   on_mouse(): jitter pipeline + click policy. Must be fast and must never raise.
  pointer thread  (PointerThread)      125 Hz: lets the stabilised pointer settle, runs the attractor.
  engine thread   (Engine.run)         60 Hz: UI Automation probes, dwell, overlay state, per-app profiles.
"""
import math
import threading
import time

from . import caption, profiles, winapi
from .dwell import Dwell
from .motion import MotionAnalyzer
from .ml.live import LiveML, SmoothingPolicy, INTERVAL_S
from .pointer import MissTracker, PointerPipeline
from .targets import (HitCache, contains, decide_physical_click, nearest_unambiguous, normalized_offset,
                      snap_point)
from .uia import UiaWorker

TICK_S = 1 / 60
PROBE_MIN_GAP_S = 0.12
LONG_NAME = 40
GATE_PX = 30
ASSIST_HOLD_S = 0.35        # hover this long beside a target before snapping to it (aiming is not a miss)
NEAR_BUTTON_PX = 48         # the original lab's "near a button" radius for brief-reversal resistance
FLUSH_S = 60.0              # pointer statistics are stored once a minute
SUGGEST_COOLDOWN_S = 900.0
CLOSE_DWELL_FACTOR = 1.5
OWN_EXES = {"python.exe", "pythonw.exe", "focusflow.exe"}


class ExtState:
    """Where the browser extension says its page viewport is (screen px), so the agent can defer to it."""
    TTL_S = 30.0

    def __init__(self):
        self.rect = None
        self.ts = 0.0
        self.lock = threading.Lock()

    def update(self, rect):
        with self.lock:
            self.rect, self.ts = rect, time.time()

    def leave(self):
        with self.lock:
            self.rect = None

    def covers(self, pt):
        with self.lock:
            return (self.rect is not None and time.time() - self.ts < self.TTL_S
                    and contains(self.rect, pt))


class OverlayState:
    """What the overlay should draw this frame (immutable snapshot, swapped atomically)."""
    __slots__ = ("pos", "progress", "rect", "assisted", "tremor", "grown")

    def __init__(self, pos, progress, rect, assisted, tremor, grown=False):
        self.pos, self.progress, self.rect, self.assisted, self.tremor = pos, progress, rect, assisted, tremor
        self.grown = grown


class PointerThread(threading.Thread):
    """125 Hz: places the stabilised pointer between hardware events (settling) and drives the attractor."""

    def __init__(self, engine):
        super().__init__(daemon=True, name="ff-pointer")
        self.engine = engine
        self.running = True

    def run(self):
        winapi.time_begin_period(1)
        try:
            while self.running:
                try:
                    self.engine.pipe.tick(time.perf_counter() * 1000.0)
                except Exception:
                    self.engine.pipe_fault("pointer thread")
                time.sleep(0.008)
        finally:
            winapi.time_end_period(1)


class Engine(threading.Thread):
    def __init__(self, cfg, store, local_targets):
        super().__init__(daemon=True, name="ff-engine")
        self.cfg, self.store, self.local = cfg, store, local_targets
        self.motion = MotionAnalyzer(cfg["sensitivity"], cfg["hover_speed_px_s"])
        self.dwell = Dwell()
        self.uia = UiaWorker()
        self.ext = ExtState()
        self.pipe = PointerPipeline(1.0, bounds=winapi.virtual_bounds(), apply=winapi.place_cursor)
        self.pipe.on_erratic = self._on_erratic
        self.pointer_thread = PointerThread(self)
        self.miss = MissTracker()
        self.mask = dict(profiles.FULL_MASK)      # experiment modes switch feature groups off
        self.overlay = None            # OverlayState | None, read by the UI thread
        self.lab_targets = []          # rects the Live Lab draws (target boundaries around the pointer)
        self.hit_cache = None          # read by the mouse-hook thread
        self.running = True
        self.suspended = False         # set while FocusFlow's own confirm prompts need plain pointing
        self.pipe_faulted = False
        self.faults = 0
        self.session = None
        self.on_fault = None           # callbacks wired by the app
        self.on_suggest = None
        self.on_adapted = None
        self._probe_future = None
        self._probe_pos = None
        self._probe_ts = 0.0
        self._probe = None             # last ProbeResult
        self._cur = None               # Target we are currently engaged with (hysteresis)
        self._cur_assisted = False
        self._episode = None           # key of the target we already logged "interaction_detected" for
        self._miss_logged = None
        self._fg = (0.0, None)
        self._app_override = None      # profile applied only while a particular application is in front
        self._swallow_up = False
        self._gate_pos = winapi.get_cursor_pos()   # dwell needs fresh movement away from here
        self._gate_open = False
        self._settle_anchor = None
        self._settled_since = 0.0
        self._last_down = 0.0
        self._last_down_pos = (0, 0)
        self._last_erratic_log = 0.0
        self._last_suggest = 0.0
        self._last_env = 0.0
        self._flush_at = time.time() + FLUSH_S
        self._flushed = self.pipe.stats()
        self._flush_t0 = time.time()
        self.status = {"tremor": 0.0, "speed": 0, "target": None}
        self._ml_lock = threading.RLock()
        self.ml = LiveML()
        self._ml_policy = SmoothingPolicy()
        self._ml_signature = None
        self._ml_epoch = None
        self._ml_submit_at = -math.inf
        self._ml_manual = False
        cfg.on_change(self._on_setting)
        self.apply_features()

    # -- configuration --------------------------------------------------------
    FEATURE_KEYS = {"sensitivity", "hover_speed_px_s", "jitter_enabled", "smoothing_strength", "attraction_strength",
                    "attraction_radius_px", "enabled"}

    def _on_setting(self, key):
        if key in ("sensitivity", "hover_speed_px_s"):
            self.motion.sensitivity = self.cfg["sensitivity"]
            self.motion.hover_speed = self.cfg["hover_speed_px_s"]
        if key in self.FEATURE_KEYS:
            self.apply_features()
        if key == "adaptation_enabled" and not self.cfg["adaptation_enabled"]:
            self.miss.clear()
        if key in self.FEATURE_KEYS or key in ("active_profile", "ml_enabled", "ml_mode", "ml_model_path"):
            with self._ml_lock:
                if key.startswith("ml_"):
                    self._ml_manual = False
                elif self.cfg["ml_enabled"]:
                    self._ml_manual = True
                self._reset_ml_locked()

    def _reset_ml_locked(self):
        self._ml_policy.reset()
        self.pipe.set_ml_adjustment(0, 0)
        self.pipe.enable_ml_capture(False)
        self._ml_signature = None
        self._sync_ml_locked()

    def _sync_ml_locked(self):
        signature = (bool(self.cfg["ml_enabled"] and self.active and not self.pipe_faulted
                          and self.running and self.session is None), self.cfg["ml_mode"], self.cfg["ml_model_path"])
        if signature != self._ml_signature:
            self._ml_signature = signature
            self._ml_policy.reset()
            self.pipe.set_ml_adjustment(0, 0)
            self.pipe.enable_ml_capture(signature[0])
            self._ml_epoch = self.pipe.ml_epoch
            self.ml.configure(*signature)

    def ml_status(self):
        with self._ml_lock:
            status = self.ml.view()
            status["manual_override"] = self._ml_manual
            status["extra_smoothing"] = self.pipe.ml_extra
            return status

    def _ml_tick(self, now, motion, dragging=False):
        with self._ml_lock:
            self._sync_ml_locked()
            if self.pipe.ml_epoch != self._ml_epoch:
                self._ml_policy.reset()
                self.pipe.set_ml_adjustment(0, 0)
                self.ml.configure(*self._ml_signature)
                self._ml_epoch = self.pipe.ml_epoch
            if self._ml_signature[0] and now - self._ml_submit_at >= INTERVAL_S:
                self._ml_submit_at = now
                _, samples = self.pipe.ml_snapshot()
                if samples and samples[-1][0] - samples[0][0] >= 2:
                    self.ml.submit(samples)
            view = self.ml.view(now)
            eligible = (self.active and self.jitter_on() and not self.pipe_faulted and not self._ml_manual
                        and self.session is None and not dragging)
            extra = self._ml_policy.update(now, view, eligible,
                                           intentional=motion.intentional or self.pipe.speed >= 250)
            extra = min(extra, max(0, 10 - self.cfg["smoothing_strength"]))
            # If the engine stalls, the hook/125 Hz pointer tick independently expires this lease.
            self.pipe.set_ml_adjustment(extra, (now + 0.1) * 1000)

    # feature groups = profile setting AND experiment mask
    def jitter_on(self):
        return bool(self.cfg["jitter_enabled"] and self.mask["jitter"])

    def dwell_on(self):
        return bool(self.cfg["dwell_enabled"] and self.mask["dwell"])

    def snap_on(self):
        return bool(self.cfg["snap_enabled"] and self.mask["dwell"])

    def adapt_on(self):
        return bool(self.cfg["adaptation_enabled"] and self.mask["adapt"])

    def correct_on(self):
        return bool(self.cfg["near_miss_correct"] and self.mask["adapt"])

    def bounce_on(self):
        return bool(self.cfg["bounce_filter"] and self.mask["dwell"])

    def attraction(self):
        return int(self.cfg["attraction_strength"]) if self.mask["adapt"] else 0

    def apply_features(self):
        """Push the effective settings into the pointer pipeline."""
        with self._ml_lock:
            if not self.active or self.pipe_faulted:
                self.pipe.configure(False, 1.0, 0)
                return
            self.pipe.configure(self.jitter_on(), profiles.cutoff_scale(self.cfg["smoothing_strength"]),
                                self.attraction())
            self.pipe.attract_radius = self.cfg["attraction_radius_px"]

    @property
    def active(self):
        return self.cfg["enabled"] and not self.suspended

    def current_profile(self):
        return self._app_override or self.cfg["active_profile"]

    def set_enabled(self, value):
        self.cfg.set("enabled", bool(value))
        self._close_gate()
        if not value:
            self.dwell.reset()
            self.overlay = None
            self.hit_cache = None
        else:
            self.pipe_faulted = False
        self.apply_features()

    def set_suspended(self, value):
        self.suspended = bool(value)
        self.apply_features()
        with self._ml_lock:
            self._reset_ml_locked()

    def _close_gate(self):
        """Require deliberate pointer movement before the next dwell (a resting pointer is not intent)."""
        self._gate_pos = winapi.get_cursor_pos()
        self._gate_open = False

    def pipe_fault(self, where):
        """Anything unexpected in the pointer path: log it, stop touching the cursor, tell the user."""
        import traceback
        traceback.print_exc()
        self.faults += 1
        self.pipe_faulted = True
        with self._ml_lock:
            self._reset_ml_locked()
        try:
            self.pipe.configure(False, 1.0, 0)
        except Exception:
            pass
        if self.on_fault:
            try:
                self.on_fault(where)
            except Exception:
                pass

    # -- experiment sessions -------------------------------------------------------
    def begin_session(self, mode):
        self.mask = profiles.mode_mask(mode)
        self.miss.clear()                  # each mode starts without adaptation carried over from another
        self.apply_features()
        self.pipe.reset_stats()

    def end_session(self):
        self.mask = dict(profiles.FULL_MASK)
        self.apply_features()

    # -- main loop ------------------------------------------------------------
    def run(self):
        self.pointer_thread.start()
        self.uia.start()
        self.uia.wait_ready(15)
        while self.running:
            t0 = time.perf_counter()
            try:
                self._tick(t0)
            except Exception:
                import traceback
                traceback.print_exc()
            time.sleep(max(0.0, TICK_S - (time.perf_counter() - t0)))

    def stop(self):
        self.running = False
        self.pointer_thread.running = False
        with self._ml_lock:
            self.pipe.enable_ml_capture(False)
            self.pipe.set_ml_adjustment(0, 0)
            self.ml.stop()

    def _foreground(self, now):
        if now - self._fg[0] > 0.25:
            exe = winapi.foreground_exe()
            self._fg = (now, exe)
            self._check_app_profile(exe)
        return self._fg[1]

    def _foreground_is_browser(self, now):
        return self._foreground(now) in winapi.CHROMIUM_EXES

    def _check_app_profile(self, exe):
        if exe is None or exe in OWN_EXES or self.session is not None:
            return
        want = self.cfg["app_profiles"].get(exe)
        if want == self._app_override:
            return
        if want and want in profiles.all_profiles(self.cfg):
            if profiles.apply_profile(self.cfg, want, persist=False, remember=False):
                self._app_override = want
        elif self._app_override:
            profiles.apply_profile(self.cfg, self.cfg["active_profile"], persist=False, remember=False)
            self._app_override = None

    def _idle(self):
        self.dwell.reset()
        self.overlay = None
        self._cur = None

    def _refresh_environment(self, now, x, y):
        if now - self._last_env < 0.5:
            return
        self._last_env = now
        scale = winapi.dpi_scale_at(x, y)
        with self.pipe.lock:
            if abs(scale - self.pipe.scale) > 0.01:
                self.pipe.set_scale(scale)
            self.pipe.bounds = winapi.virtual_bounds()

    def _tick(self, now):
        x, y = winapi.get_cursor_pos()
        self._refresh_environment(now, x, y)
        self.motion.feed(now, x, y)
        m = self.motion.analyze(now)
        try:
            self._ml_tick(now, m, dragging=winapi.left_button_down())
        except Exception as exc:
            # Optional ML failure must never disable the ordinary assistance pipeline.
            with self._ml_lock:
                self.pipe.set_ml_adjustment(0, 0)
                self.pipe.enable_ml_capture(False)
                self._ml_policy.reset()
                self.ml.fail(f"ML controller unavailable: {type(exc).__name__}")
                self._ml_epoch = self.pipe.ml_epoch
                self._ml_manual = True
        self.status["tremor"], self.status["speed"] = round(m.tremor, 2), int(m.speed)
        self._flush_stats()
        cfg = self.cfg
        if not self.active:
            return self._idle()
        if winapi.left_button_down():
            return self._idle()          # user is dragging or clicking by hand
        if cfg["extension_enabled"] and self._foreground_is_browser(now) and self.ext.covers((x, y)):
            self.hit_cache = None        # the page extension owns this region
            return self._idle()
        self._foreground(now)

        pos = (int(m.pos[0]), int(m.pos[1]))
        if not self._gate_open and math.hypot(x - self._gate_pos[0], y - self._gate_pos[1]) >= GATE_PX:
            self._gate_open = True
        tol = int(cfg["tolerance_px"] + 20 * m.tremor)   # more wander allowed while shaking
        self._collect_probe(now)
        attract = self.attraction()
        want_assist = self.snap_on() or self.correct_on() or self.adapt_on() or attract > 0 or self.jitter_on() \
            or cfg["highlight_targets"]
        if m.settled:
            if self._settle_anchor is None or math.hypot(pos[0] - self._settle_anchor[0],
                                                         pos[1] - self._settle_anchor[1]) > max(6, tol):
                self._settle_anchor, self._settled_since = pos, now
        else:
            self._settle_anchor = None
        held = self._settle_anchor is not None and now - self._settled_since >= ASSIST_HOLD_S
        engaged = self.dwell.progress > 0
        probe = self._probe if self._probe and math.hypot(self._probe.pos[0] - x, self._probe.pos[1] - y) <= 8 \
            and now - self._probe.ts < (1.2 if engaged else 3.0) else None

        radius = self._search_radius()
        nearby = (list(probe.nearby) if probe else []) + self.local.near(x, y, radius)
        target, assisted, nearest, nearest_d = None, False, None, 0.0
        local = self.local.hit(pos[0], pos[1])
        if local is not None:
            target = local
        elif probe is not None:
            target = probe.under
        if local is None and m.settled:
            # Minimise / maximise / close. UI Automation does list these buttons, but (a) a synthesised mouse click
            # does not press a non-client caption button, and (b) for DPI-unaware windows UIA reports the wrong
            # button for the pointer. So when Windows' own hit-test says the pointer is on a caption button, that
            # wins over UIA and the button's system command is sent instead of a click. Only when the pointer is
            # *inside* the button: never snapped to, attracted to, or redirected onto.
            inside = caption.target_at(x, y)
            if inside is not None:
                target = inside
        if target is None and self._cur is not None and not self._cur_assisted \
                and contains(self._cur.rect, (x, y), margin=tol):
            target = self._cur             # hysteresis: tremor nudged us just over the edge
        texty = bool(probe and probe.texty)
        if target is None and want_assist and not texty:
            nearest, nearest_d = nearest_unambiguous(pos, nearby, radius)
            if self.snap_on() and held and nearest is not None and nearest_d <= cfg["snap_radius_px"]:
                target, assisted = nearest, True
        if probe is not None or local is not None or nearby:
            self.hit_cache = HitCache((x, y), probe.ts if probe else now, (probe.under if probe else None) or local,
                                      nearest if ((probe.under if probe else None) or local) is None else None,
                                      nearest_d, texty)
        self._steer_pipeline(x, y, probe, local, nearest, nearest_d, texty, attract)
        if m.settled and local is None and (target is None or not (probe and probe.under)):
            self._request_probe(now, x, y, want_assist)            # our own controls are already known: no UIA call

        # dwell
        self._cur, self._cur_assisted = target, assisted
        self.status["target"] = target.name if target else None
        own = target.dwell_ms if target else None       # on-screen keyboard keys dwell independently of the profile
        key = ("assist:" if assisted else "") + target.key \
            if target and (self.dwell_on() or own) and self._gate_open else None
        dwell_s = (own or cfg["dwell_ms"]) / 1000.0 * (cfg["assist_dwell_factor"] if assisted else 1.0)
        if target is not None and target.source == "caption" and target.name.startswith("Close"):
            dwell_s *= CLOSE_DWELL_FACTOR                  # closing a window is the costliest accidental click
        progress, ev = self.dwell.update(now, key, m.settled, dwell_s, cfg["dwell_cooldown_ms"] / 1000.0)

        if ev == "start" and target is not None:
            self._on_engaged(target, assisted, (x, y), m, nearest_d)
        elif ev == "blocked" and target is not None:
            self.store.add_event("click_prevented", "desktop", self._label(target),
                                 {"reason": "repeat_dwell_guard",
                                  "note": "dwell completed again on a target that was just clicked"})
        elif ev == "fire" and target is not None:
            self._fire(target, assisted, (x, y), m, now)

        if target is not None and ((self.dwell_on() or own) and self._gate_open or cfg["highlight_targets"]):
            self.overlay = OverlayState((x, y), progress if (self.dwell_on() or own) else 0.0, target.rect, assisted,
                                        m.tremor, target.key in self.miss.grown)
        else:
            self.overlay = None
        self.lab_targets = [t.rect for t in nearby[:12]] + ([target.rect] if target else [])

    def _search_radius(self):
        cfg = self.cfg
        miss = cfg["miss_radius_px"] * (2 if self.adapt_on() else 1)
        return max(cfg["snap_radius_px"], miss, cfg["attraction_radius_px"], NEAR_BUTTON_PX)

    def _steer_pipeline(self, x, y, probe, local, nearest, nearest_d, texty, attract):
        """Tell the pointer pipeline whether we are near a button and what (if anything) may attract it."""
        under = (probe.under if probe else None) or local
        pipe = self.pipe
        pipe.near_button = bool(under is not None or (nearest is not None and nearest_d <= NEAR_BUTTON_PX))
        tgt = None
        if attract > 0 and not texty:
            if under is not None:
                tgt = (under.key, under.rect)
            elif nearest is not None and nearest_d <= self.cfg["attraction_radius_px"]:
                tgt = (nearest.key, nearest.rect)
        pipe.attract_target = tgt

    # -- statistics ----------------------------------------------------------------
    def _flush_stats(self):
        if time.time() < self._flush_at:
            return
        self._flush_at = time.time() + FLUSH_S
        cur = self.pipe.stats()
        delta = {k: cur[k] - self._flushed[k] for k in cur}
        if any(v < 0 for v in delta.values()):         # the counters were reset (new experiment / Reset button)
            delta = dict(cur)
        delta["seconds"] = time.time() - self._flush_t0
        self._flushed, self._flush_t0 = cur, time.time()
        if delta["raw_distance"] >= 100 and self.active:
            self.store.add_pointer_stats(delta, self.current_profile())

    def flush_stats_now(self):
        self._flush_at = 0.0
        self._flush_stats()

    def _on_erratic(self):
        now = time.time()
        if now - self._last_erratic_log > 20:
            self._last_erratic_log = now
            self.store.log_async("erratic_movement", "desktop", None,
                                 {"reason": "repeated_short_reversals",
                                  "note": "heuristic only; not a medical assessment"})
        if (self.cfg["suggest_stronger"] and self.session is None and self.cfg["smoothing_strength"] < 8
                and now - self._last_suggest > SUGGEST_COOLDOWN_S and self.on_suggest):
            self._last_suggest = now
            self.on_suggest()

    # -- probing ----------------------------------------------------------------
    def _request_probe(self, now, x, y, nearby):
        if not self.uia.ok or self._probe_future is not None or now - self._probe_ts < PROBE_MIN_GAP_S:
            return
        moved = math.hypot(x - self._probe_pos[0], y - self._probe_pos[1]) if self._probe_pos else 1e9
        if moved < 3 and now - self._probe_ts < (1.0 if self.dwell.progress > 0 else 2.5):
            return                                                 # nothing moved: the answer has not changed
        if moved < 6 and now - self._probe_ts < 0.4:
            return                                                 # a few pixels of drift: the target is the same
        self._probe_pos, self._probe_ts = (x, y), now
        radius = self._search_radius()
        self._probe_future = self.uia.submit(lambda s: s.probe(x, y, radius, nearby))

    def _collect_probe(self, now):
        f = self._probe_future
        if f is not None and f.done():
            self._probe_future = None
            try:
                self._probe = f.result()
            except Exception:
                self._probe = None

    # -- events ------------------------------------------------------------------
    def _label(self, target):
        if not self.cfg["log_target_names"]:
            return target.ctype or "control"
        return (target.name or target.ctype or "control")[:LONG_NAME]

    def _on_engaged(self, target, assisted, pos, m, near_d):
        name = self._label(target)
        if self._episode != target.key:
            self._episode = target.key
            self.store.add_event("interaction_detected", "desktop", name,
                                 {"tremor": round(m.tremor, 2), "assisted": assisted})
            self.store.add_event("target_identified", "desktop", name,
                                 {"ctype": target.ctype, "size": [target.rect[2] - target.rect[0],
                                                                  target.rect[3] - target.rect[1]],
                                  "assisted": assisted})
        if assisted and self._miss_logged != target.key:
            self._miss_logged = target.key
            nx, ny = normalized_offset(pos, target.rect)
            self.store.add_event("near_miss", "desktop", name,
                                 {"kind": "hover", "distance_px": int(near_d), "nx": round(nx, 2),
                                  "ny": round(ny, 2), "corrected": True})

    def _fire(self, target, assisted, pos, m, now):
        name = self._label(target)
        approach = self.motion.take_approach(now) or {"path_px": 0, "reversals": 0, "duration_ms": 0}
        self.store.add_event("dwell_complete", "desktop", name,
                             {"dwell_ms": int(self.cfg["dwell_ms"] * (self.cfg["assist_dwell_factor"] if assisted else 1))})
        click_at = pos
        if target.action is not None:
            self.local.run(target)
        else:
            if target.source == "caption":
                caption.press(target)                            # the same system command the button would send
            elif assisted:
                try:
                    want = snap_point(target, pos)
                    click_at = self.uia.submit(lambda s: s.verified_point(target, want)).result(0.5)
                except Exception:
                    click_at = target.hit_point or pos
            if target.source != "caption":
                winapi.send_click(*click_at)
        self._close_gate()
        detail = dict(approach, mode="assisted", method="snap_dwell" if assisted else "dwell",
                      tremor=round(m.tremor, 2))
        if assisted:
            detail["snapped_px"] = int(math.hypot(click_at[0] - pos[0], click_at[1] - pos[1]))
        self.store.add_event("click_assisted", "desktop", name, detail)

    # -- real mouse events, called on the hook thread --------------------------------
    def on_mouse(self, msg, x, y, injected):
        """Return True to swallow the event. Must be fast and must never raise."""
        if injected or not self.active:
            return False
        if msg == winapi.WM_MOUSEMOVE:
            if self.pipe_faulted:
                return False
            try:
                return self.pipe.on_move(x, y, time.perf_counter() * 1000.0)
            except Exception:
                self.pipe_fault("mouse hook")
                return False
        if msg == winapi.WM_LBUTTONUP:
            if self._swallow_up:
                self._swallow_up = False
                return True
            return False
        if msg != winapi.WM_LBUTTONDOWN:
            return False
        cfg, now = self.cfg, time.perf_counter()

        # 1. bounce filter: tremor often produces a second click a few ms after the first
        if self.bounce_on() and now - self._last_down < cfg["bounce_ms"] / 1000.0 \
                and math.hypot(x - self._last_down_pos[0], y - self._last_down_pos[1]) < 8:
            self._swallow_up = True
            self.store.log_async("click_prevented", "desktop", None,
                                 {"reason": "bounce_click", "gap_ms": int((now - self._last_down) * 1000)})
            return True
        self._last_down, self._last_down_pos = now, (x, y)

        if cfg["extension_enabled"] and self.ext.covers((x, y)) and self._fg[1] in winapi.CHROMIUM_EXES:
            return False                 # the extension handles clicks inside web pages

        # 2. near-miss correction / hit-area adaptation / manual-click accounting
        cache = self.hit_cache
        action, target = decide_physical_click(cache, (x, y), now, cfg["miss_radius_px"], self.correct_on(),
                                               grown={k for k in self.miss.grown})
        if action == "manual_hit":
            self.miss.success(target.key)
            approach = self.motion.take_approach(now)
            if approach:
                self.store.log_async("manual_click", "desktop", self._label(target), dict(approach, mode="manual"))
        elif action in ("redirect", "miss"):
            nx, ny = normalized_offset((x, y), target.rect)
            approach = self.motion.take_approach(now) or {"path_px": 0, "reversals": 0, "duration_ms": 0}
            self.store.log_async("near_miss", "desktop", self._label(target),
                                 {"kind": "click", "nx": round(nx, 2), "ny": round(ny, 2),
                                  "corrected": action == "redirect"})
            if cache is not None and not cache.texty and self.miss.record(target.key, self.adapt_on()):
                self.store.log_async("target_adapted", "desktop", self._label(target),
                                     {"reason": "three_nearby_misses", "note": "hit area enlarged; reversible"})
                if self.on_adapted:
                    self.on_adapted(self._label(target))
            if action == "redirect":
                if self.session is not None:      # uncorrected misses are counted by the task window itself
                    self.session.engine_near_miss(True)
                self._swallow_up = True
                self.hit_cache = None
                threading.Thread(target=self._redirect, args=(target, approach), daemon=True).start()
                return True
        return False

    def _redirect(self, target, approach):
        point = target.hit_point or target.center      # a point that really hit-tests into the target
        winapi.send_click(*point)
        self.store.add_event("click_assisted", "desktop", self._label(target),
                             dict(approach, mode="assisted", method="near_miss_redirect"))

    def undo_adaptation(self):
        n = len(self.miss.grown)
        self.miss.clear()
        return n
