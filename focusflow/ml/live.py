"""Optional local inference worker and conservative smoothing policy (no Win32 imports).

Only the worker imports NumPy/Torch. No raw coordinates are persisted or logged.
"""
from bisect import bisect_right
import math
from pathlib import Path
import queue
import threading
import time

DEFAULT_MODEL = Path(__file__).resolve().parents[2] / "artifacts" / "motion-stress-v1" / "model"
INTERVAL_S = 0.5
MAX_INFERENCE_S = 0.250
MAX_AGE_S = 1.0
LABELS = {"steady", "intentional", "tremor_like"}


def load_classifier(path):
    from .model import MotionClassifier
    import torch
    classifier = MotionClassifier(path)  # local_files_only=True; never downloads
    torch.set_num_threads(1)
    return classifier


def prepare_window(samples):
    from .data import resample_velocity
    import numpy as np
    if len(samples) < 2:
        raise ValueError("Warming up")
    end = samples[-1][0]
    start = end - 2.0
    left = max(0, bisect_right([p[0] for p in samples], start) - 1)
    rows = samples[left:]
    scale = rows[0][3]
    if any(p[3] != scale for p in rows):
        raise ValueError("Display scale changed")
    velocity = resample_velocity([p[:3] for p in rows], start, scale)
    return np.asarray([velocity], dtype=np.float32)


class LiveML:
    """One persistent daemon, one pending job; old/in-flight generations cannot publish.

    configure/submit/view are engine/UI operations, never mouse-hook operations.
    An error latches until a settings change or explicit disable/re-enable.
    """
    def __init__(self, factory=load_classifier, clock=time.perf_counter):
        self.factory, self.clock = factory, clock
        self.lock = threading.RLock()
        self.jobs = queue.Queue(maxsize=1)
        self.stop_event = threading.Event()
        self.thread = None
        self.generation = 0
        self.enabled = False
        self.mode = "shadow"
        self.path = str(DEFAULT_MODEL)
        self.status = "off"
        self.error = ""
        self.result = None
        self.dropped = 0
        self.latency_ms = None
        self.inflight = None

    def configure(self, enabled, mode="shadow", path=None):
        with self.lock:
            self.generation += 1
            self.enabled = bool(enabled) and mode in ("shadow", "experimental")
            self.mode = mode
            self.result = None
            self.error = "" if mode in ("shadow", "experimental") else "Invalid ML mode"
            try:
                # No filesystem probing on the engine/UI thread, including for unavailable drives.
                self.path = str(Path(path).expanduser().absolute()) if path else str(DEFAULT_MODEL)
            except (TypeError, ValueError, RuntimeError, OSError):
                self.enabled = False
                self.error = "Invalid ML model folder"
            self.status = ("warming" if self.enabled else "off") if not self.error else "error"
            self.latency_ms = None
            self.inflight = None
            self._clear_jobs()
            if self.enabled and not self.stop_event.is_set():
                self.status = "loading"
                self.jobs.put_nowait((self.generation, self.path, None))
            if self.enabled and not self.stop_event.is_set() and self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="ff-ml")
                try:
                    self.thread.start()
                except RuntimeError as exc:
                    self.thread = None
                    self.fail(f"Cannot start ML worker: {exc}")

    def _clear_jobs(self):
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break

    def submit(self, samples):
        with self.lock:
            if not self.enabled or self.status == "error" or self.stop_event.is_set():
                return
            job = (self.generation, self.path, samples)
            try:
                self.jobs.put_nowait(job)
            except queue.Full:
                self._clear_jobs()
                self.dropped += 1
                self.jobs.put_nowait(job)

    def view(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            state = self.status
            result = self.result
            if result is not None and now - result["window_end"] > MAX_AGE_S:
                state, result = "stale", None
            if self.inflight is not None and now - self.inflight > MAX_INFERENCE_S:
                state, result = "slow", None
            return {"enabled": self.enabled, "mode": self.mode, "state": state, "error": self.error,
                    "model": self.path, "latency_ms": self.latency_ms, "dropped": self.dropped,
                    "prediction": dict(result) if result is not None else None}

    def _publish(self, generation, state, result=None, error="", latency=None):
        with self.lock:
            if generation != self.generation or not self.enabled or self.stop_event.is_set():
                return
            self.status, self.result, self.error = state, result, error
            self.latency_ms = latency
            self.inflight = None

    def _run(self):
        classifier, loaded_path = None, None
        while not self.stop_event.is_set():
            try:
                generation, path, samples = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            with self.lock:
                if generation != self.generation or not self.enabled:
                    continue
            try:
                if classifier is None or path != loaded_path:
                    self._publish(generation, "loading")
                    classifier = self.factory(path)
                    loaded_path = path
                with self.lock:
                    if generation != self.generation or not self.enabled:
                        continue
                if not samples:
                    self._publish(generation, "warming")
                    continue
                if self.clock() - samples[-1][0] > MAX_AGE_S:
                    self._publish(generation, "stale")
                    continue
                started = self.clock()
                with self.lock:
                    if generation != self.generation or not self.enabled:
                        continue
                    self.inflight = started
                try:
                    velocity = prepare_window(samples)
                except ValueError:
                    self._publish(generation, "warming")
                    continue
                prediction = classifier.predict(velocity)[0]
                scores = prediction["scores"]
                if (prediction["label"] not in LABELS or set(scores) != LABELS
                        or any(not math.isfinite(v) or not 0 <= v <= 1 for v in scores.values())
                        or abs(sum(scores.values()) - 1) > 1e-5
                        or scores[prediction["label"]] != max(scores.values())):
                    raise ValueError("Invalid classifier output")
                finished = self.clock()
                latency = (finished - started) * 1000
                if finished - started > MAX_INFERENCE_S:
                    self._publish(generation, "slow", latency=latency)
                elif finished - samples[-1][0] > MAX_AGE_S:
                    self._publish(generation, "stale", latency=latency)
                else:
                    self._publish(generation, "ready", {
                        "window_end": samples[-1][0], "label": prediction["label"],
                        "tremor_score": float(scores["tremor_like"]),
                        "intentional_score": float(scores["intentional"]),
                        "latency_ms": latency}, latency=latency)
            except Exception as exc:
                self._publish(generation, "error", error=f"{type(exc).__name__}: {exc}"[:240])
            finally:
                # Do not retain raw windows while the worker is idle/disabled.
                samples = None

    def stop(self):
        self.configure(False, self.mode, self.path)
        self.stop_event.set()

    def fail(self, reason):
        with self.lock:
            self.generation += 1
            self.result = None
            self.inflight = None
            self.status, self.error = "error", reason
            self._clear_jobs()


class SmoothingPolicy:
    """Three distinct high-score windows spanning >=1s latch a bounded adjustment.

    Confidence thresholds are heuristics over uncalibrated synthetic-model scores.
    No setting, click decision, attraction or dwell behavior is modified here.
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.extra = 0.0
        self.high_count = 0
        self.first_high = None
        self.last_window = None
        self.latched = False
        self.last_tick = None

    def update(self, now, view, eligible, intentional=False):
        p = view.get("prediction")
        if (not eligible or view["mode"] != "experimental" or view["state"] != "ready" or p is None
                or now - p["window_end"] > MAX_AGE_S or p["latency_ms"] > MAX_INFERENCE_S * 1000
                or intentional or p["label"] == "intentional"):
            self.reset()
            return 0.0
        dt = min(0.1, max(0.0, now - self.last_tick)) if self.last_tick is not None else 0.0
        self.last_tick = now
        if self.last_window != p["window_end"]:
            self.last_window = p["window_end"]
            if p["label"] == "tremor_like" and p["tremor_score"] >= 0.85:
                self.high_count += 1
                if self.first_high is None:
                    self.first_high = p["window_end"]
                if self.high_count >= 3 and p["window_end"] - self.first_high >= 1.0:
                    self.latched = True
            elif p["tremor_score"] < 0.55 or p["label"] == "steady":
                self.latched = False
                self.high_count, self.first_high = 0, None
            else:
                self.high_count, self.first_high = 0, None
        self.extra = min(2.0, self.extra + 0.5 * dt) if self.latched else max(0.0, self.extra - dt)
        return self.extra
