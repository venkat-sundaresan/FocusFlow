"""Experiment sessions: run a task under one of five assistance modes and record what actually happened.

Everything measured comes from the live pointer pipeline (real Windows mouse input) and from the clicks
the task window sees. Nothing is simulated."""
import time

from .pointer import nearby_target
from .profiles import MODE_NAMES


class SessionRecorder:
    TASK = "Target sequence"

    def __init__(self, engine, mode, targets_total=10, task=None):
        if mode not in MODE_NAMES:
            raise ValueError(mode)
        self.engine, self.mode, self.task = engine, mode, task or self.TASK
        self.total = targets_total
        self.successes = self.incorrect = self.near_misses = self.accidental = 0
        self.corrected = 0
        self.profile = engine.current_profile()
        self.finished = False
        engine.session = self
        engine.begin_session(mode)                    # masks features, clears adaptation, resets stats
        self.base = engine.pipe.stats()
        self.t0 = time.time()
        self._t0_mono = time.perf_counter()

    # -- fed by the task window -----------------------------------------------------------------
    def click(self, point, expected_id, hit_id, rects, source="mouse"):
        """One activation attempt. `rects`: [(id, (l, t, r, b))] of everything clickable in the task.
        Returns 'success' | 'accidental' | 'miss'."""
        eng = self.engine
        if hit_id is not None and hit_id == expected_id:
            self.successes += 1
            eng.miss.success(expected_id)
            return "success"
        self.incorrect += 1
        if hit_id is not None:                         # the wrong control was activated
            self.accidental += 1
            return "accidental"
        near = nearby_target(point, rects)             # only an unambiguous, nearby target counts as a near miss
        if near is not None:
            self.near_misses += 1
        if eng.miss.record(near, eng.adapt_on()) and eng.on_adapted:
            eng.on_adapted(str(near))
        return "miss"

    def engine_near_miss(self, corrected):
        """The engine noticed (and, if allowed, fixed) a click that landed just outside a control."""
        self.near_misses += 1
        if corrected:
            self.corrected += 1

    # -- results ---------------------------------------------------------------------------------
    def elapsed(self):
        return time.perf_counter() - self._t0_mono

    def stats_delta(self):
        cur = self.engine.pipe.stats()
        return {k: cur[k] - self.base[k] for k in cur}

    def finish(self, completed):
        """Stop measuring, restore normal assistance, and (for completed runs) persist the result."""
        if self.finished:
            return None
        self.finished = True
        seconds = self.elapsed()
        d = self.stats_delta()
        self.engine.end_session()
        self.engine.session = None
        rec = {"mode": self.mode, "task": self.task, "profile": self.profile, "completed": 1 if completed else 0,
               "seconds": round(seconds, 2), "raw_distance": round(d["raw_distance"], 1),
               "stabilised_distance": round(d["stabilised_distance"], 1), "raw_changes": d["raw_changes"],
               "stabilised_changes": d["stabilised_changes"], "raw_corrective": d["raw_corrective"],
               "stabilised_corrective": d["stabilised_corrective"], "successes": self.successes,
               "incorrect": self.incorrect, "near_misses": self.near_misses, "accidental": self.accidental,
               "erratic_flags": d["erratic_flags"], "targets": self.total}
        if completed:
            rec["id"] = self.engine.store.add_session(rec, self.t0, self.t0 + seconds)
        return rec
