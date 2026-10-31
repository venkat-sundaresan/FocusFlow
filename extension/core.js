// Pure logic shared by the content script: a port of the desktop agent's motion analyser,
// dwell state machine and nearest-target selection, so both behave the same way.
// All coordinates handed to Motion are PHYSICAL pixels (CSS px * devicePixelRatio).
(function (root) {
  'use strict';

  function distToRect(x, y, r) {
    const dx = Math.max(r.left - x, 0, x - r.right);
    const dy = Math.max(r.top - y, 0, y - r.bottom);
    return Math.hypot(dx, dy);
  }

  // candidates: [{rect:{left,top,right,bottom}, ...}]. Closest within radius that is clearly closer than
  // the runner-up, otherwise null (never guess between two buttons).
  function nearestUnambiguous(x, y, candidates, radius, margin) {
    margin = margin == null ? 8 : margin;
    const ranked = candidates
      .map(c => ({ c, d: distToRect(x, y, c.rect) }))
      .filter(p => p.d <= radius)
      .sort((a, b) => a.d - b.d);
    if (!ranked.length) return { target: null, dist: 0 };
    if (ranked.length > 1 && ranked[1].d - ranked[0].d < margin) return { target: null, dist: ranked[0].d };
    return { target: ranked[0].c, dist: ranked[0].d };
  }

  const WINDOW_S = 0.8;
  const MIN_STEP = 2.0;

  class Motion {
    constructor(sensitivity, hoverSpeed) {
      this.sensitivity = sensitivity == null ? 5 : sensitivity;
      this.hoverSpeed = hoverSpeed == null ? 250 : hoverSpeed;
      this.restResetS = 2.5;
      this.samples = [];
      this.segs = [];
      this.anchor = null;
      this.lastV = null;
      this.last = null;
      this.stillSince = null;
      this._resetApproach();
    }
    _resetApproach() { this.aStart = null; this.aPath = 0; this.aRev = 0; }
    takeApproach(now) {
      if (this.aStart == null) { this._resetApproach(); return null; }
      const out = { path_px: Math.floor(this.aPath), reversals: this.aRev, duration_ms: Math.floor((now - this.aStart) * 1000) };
      this._resetApproach();
      return out;
    }
    feed(t, x, y) {
      this.samples.push([t, x, y]);
      while (this.samples.length && t - this.samples[0][0] > 1.5) this.samples.shift();
      if (this.last) {
        const d = Math.hypot(x - this.last[0], y - this.last[1]);
        if (d >= 1) {
          this.stillSince = null;
          if (this.aStart == null) this.aStart = t;
          this.aPath += d;
        } else if (this.stillSince == null) {
          this.stillSince = t;
        } else if (t - this.stillSince > this.restResetS) {
          this._resetApproach();
        }
      }
      this.last = [x, y];
      if (!this.anchor) {
        this.anchor = [x, y];
      } else {
        const vx = x - this.anchor[0], vy = y - this.anchor[1];
        const len = Math.hypot(vx, vy);
        if (len >= MIN_STEP) {
          let rev = false;
          if (this.lastV) rev = (vx * this.lastV[0] + vy * this.lastV[1]) < 0;
          this.lastV = [vx, vy];
          this.anchor = [x, y];
          this.segs.push([t, rev, len]);
          if (rev && this.aStart != null) this.aRev++;
        }
      }
      while (this.segs.length && t - this.segs[0][0] > WINDOW_S) this.segs.shift();
    }
    analyze(now) {
      const s = this.samples;
      if (s.length < 10) {
        const p = s.length ? s[s.length - 1] : [0, 0, 0];
        return { speed: 0, tremor: 0, isTremor: false, cycles: 0, settled: true, intentional: false, pos: [p[1], p[2]] };
      }
      const a = s.slice(-10, -5), b = s.slice(-5);
      const mean = (arr, i) => arr.reduce((acc, p) => acc + p[i], 0) / arr.length;
      const dt = (mean(b, 0) - mean(a, 0)) || 1e-3;
      const speed = Math.hypot(mean(b, 1) - mean(a, 1), mean(b, 2) - mean(a, 2)) / dt;
      const win = s.filter(p => now - p[0] <= WINDOW_S);
      let cycles = 0, level = 0, intentional = false;
      if (win.length >= 8 && win[win.length - 1][0] - win[0][0] >= 0.4) {
        const spanT = win[win.length - 1][0] - win[0][0];
        const revs = this.segs.filter(g => g[1]).length;
        const path = this.segs.reduce((acc, g) => acc + g[2], 0);
        const net = Math.hypot(win[win.length - 1][1] - win[0][1], win[win.length - 1][2] - win[0][2]);
        const xs = win.map(p => p[1]), ys = win.map(p => p[2]);
        const extent = Math.hypot(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
        cycles = (revs / 2) / spanT;
        const straight = path > 0 ? net / path : 1;
        intentional = net > 80 && straight > 0.6;
        if (!intentional && extent <= 80 && straight < 0.6) {
          const thr = 4.0 - 0.3 * this.sensitivity;
          level = Math.min(1, cycles / (2 * thr));
        }
      }
      return { speed, tremor: level, isTremor: level >= 0.5, cycles, settled: speed < this.hoverSpeed,
               intentional, pos: [mean(b, 1), mean(b, 2)] };
    }
  }

  const GRACE_S = 0.2, REARM_S = 0.6;

  class Dwell {
    constructor() {
      this.key = null; this.t0 = null; this.progress = 0; this.unsettledSince = null;
      this.blockedReported = false; this.firedKey = null; this.firedLeftAt = null;
      this.lastFire = -1e9; this.armed = true;
    }
    reset() { this.key = null; this.t0 = null; this.progress = 0; this.unsettledSince = null; }
    // returns {progress, event} with event in null|'start'|'cancel'|'fire'|'blocked'
    update(now, key, settled, dwellS, cooldownS) {
      let event = null;
      if (key !== this.key) {
        if (this.key != null) {
          if (this.progress >= 0.2) event = 'cancel';
          if (this.key === this.firedKey) this.firedLeftAt = now;
        }
        this.key = key; this.t0 = null; this.progress = 0; this.unsettledSince = null; this.blockedReported = false;
        if (key != null && key === this.firedKey) {
          this.armed = this.firedLeftAt != null && now - this.firedLeftAt >= REARM_S;
        } else {
          this.armed = true;
        }
      }
      if (key == null) return { progress: 0, event };
      if (!settled) {
        if (this.unsettledSince == null) this.unsettledSince = now;
        if (now - this.unsettledSince > GRACE_S) { this.t0 = null; this.progress = 0; }
        return { progress: this.progress, event };
      }
      this.unsettledSince = null;
      if (this.t0 == null) { this.t0 = now; event = event || 'start'; }
      this.progress = Math.min(1, (now - this.t0) / Math.max(dwellS, 0.05));
      if (this.progress >= 1) {
        if (!this.armed || now - this.lastFire < cooldownS) {
          if (!this.blockedReported) { this.blockedReported = true; event = 'blocked'; }
          this.t0 = null; this.progress = 0;
        } else {
          this.firedKey = key; this.firedLeftAt = null; this.lastFire = now; this.armed = false;
          this.t0 = null; this.progress = 0; event = 'fire';
        }
      }
      return { progress: this.progress, event };
    }
  }

  const api = { distToRect, nearestUnambiguous, Motion, Dwell };
  root.FocusFlowCore = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
