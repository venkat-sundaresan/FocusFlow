"""Dwell-click state machine (pure). Feed it the current target key every tick."""

GRACE_S = 0.2     # tolerate brief unsettled samples (tremor spikes) before resetting progress
REARM_S = 0.6     # pointer must leave a just-clicked target this long before it can fire again


class Dwell:
    def __init__(self):
        self.key = None
        self.t0 = None
        self.progress = 0.0
        self._unsettled_since = None
        self._blocked_reported = False
        self._fired_key = None
        self._fired_left_at = None
        self._last_fire = -1e9
        self._armed = True

    def reset(self):
        self.key = None
        self.t0 = None
        self.progress = 0.0
        self._unsettled_since = None

    def update(self, now, key, settled, dwell_s, cooldown_s):
        """Returns (progress 0..1, event) with event in None | 'start' | 'cancel' | 'fire' | 'blocked'."""
        event = None
        if key != self.key:
            if self.key is not None:
                if self.progress >= 0.2:
                    event = "cancel"
                if self.key == self._fired_key:
                    self._fired_left_at = now
            self.key = key
            self.t0 = None
            self.progress = 0.0
            self._unsettled_since = None
            self._blocked_reported = False
            if key is not None and key == self._fired_key:
                left = self._fired_left_at
                self._armed = left is not None and now - left >= REARM_S
            else:
                self._armed = True
        if key is None:
            return 0.0, event

        if not settled:
            if self._unsettled_since is None:
                self._unsettled_since = now
            if now - self._unsettled_since > GRACE_S:
                self.t0 = None
                self.progress = 0.0
            return self.progress, event
        self._unsettled_since = None

        if self.t0 is None:
            self.t0 = now
            event = event or "start"
        self.progress = min(1.0, (now - self.t0) / max(dwell_s, 0.05))
        if self.progress >= 1.0:
            if not self._armed or now - self._last_fire < cooldown_s:
                if not self._blocked_reported:
                    self._blocked_reported = True
                    event = "blocked"
                self.t0 = None
                self.progress = 0.0
            else:
                self._fired_key = key
                self._fired_left_at = None
                self._last_fire = now
                self._armed = False
                self.t0 = None
                self.progress = 0.0
                event = "fire"
        return self.progress, event
