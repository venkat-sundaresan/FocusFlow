"""Local activity log (SQLite). Only FocusFlow's own events are stored: no screenshots, no keystrokes."""
import json
import queue
import random
import sqlite3
import threading
import time

KINDS = (
    "interaction_detected", "target_identified", "dwell_complete", "click_assisted",
    "near_miss", "click_prevented", "manual_click", "setting_change", "toggle",
    "target_adapted", "erratic_movement", "profile_change", "emergency_stop",
)   # "app_launch" rows from earlier versions are kept but no longer produced

SESSION_FIELDS = (
    "mode", "task", "profile", "completed", "seconds", "raw_distance", "stabilised_distance", "raw_changes",
    "stabilised_changes", "raw_corrective", "stabilised_corrective", "successes", "incorrect", "near_misses",
    "accidental", "erratic_flags", "targets",
)
POINTER_FIELDS = (
    "seconds", "raw_distance", "stabilised_distance", "raw_changes", "stabilised_changes", "raw_corrective",
    "stabilised_corrective", "erratic_flags",
)

# Each entry upgrades the database by one version. Never edit an old one; append. Existing rows are never dropped.
MIGRATIONS = (
    # 1: the original events table
    ("CREATE TABLE IF NOT EXISTS events ("
     "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,"
     "source TEXT NOT NULL, target TEXT, detail TEXT, undo TEXT,"
     "undoable INTEGER NOT NULL DEFAULT 0, undone INTEGER NOT NULL DEFAULT 0,"
     "demo INTEGER NOT NULL DEFAULT 0)",
     "CREATE INDEX IF NOT EXISTS ix_events_demo_ts ON events(demo, ts)"),
    # 2: experiment sessions + rolling pointer statistics; retire the launcher-era undo records
    ("CREATE TABLE IF NOT EXISTS sessions ("
     "id INTEGER PRIMARY KEY AUTOINCREMENT, ts_start REAL NOT NULL, ts_end REAL NOT NULL,"
     "mode TEXT NOT NULL, task TEXT, profile TEXT, completed INTEGER NOT NULL DEFAULT 0, seconds REAL,"
     "raw_distance REAL, stabilised_distance REAL, raw_changes INTEGER, stabilised_changes INTEGER,"
     "raw_corrective INTEGER, stabilised_corrective INTEGER, successes INTEGER, incorrect INTEGER,"
     "near_misses INTEGER, accidental INTEGER, erratic_flags INTEGER, targets INTEGER,"
     "demo INTEGER NOT NULL DEFAULT 0)",
     "CREATE INDEX IF NOT EXISTS ix_sessions_demo_ts ON sessions(demo, ts_start)",
     "CREATE TABLE IF NOT EXISTS pointer_stats ("
     "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, profile TEXT, seconds REAL,"
     "raw_distance REAL, stabilised_distance REAL, raw_changes INTEGER, stabilised_changes INTEGER,"
     "raw_corrective INTEGER, stabilised_corrective INTEGER, erratic_flags INTEGER,"
     "demo INTEGER NOT NULL DEFAULT 0)",
     "CREATE INDEX IF NOT EXISTS ix_pointer_stats_ts ON pointer_stats(demo, ts)",
     "UPDATE events SET undoable=0, undo=NULL WHERE undo LIKE '%close_pid%' OR undo LIKE '%close_window%'"),
)
SCHEMA_VERSION = len(MIGRATIONS)


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.migrated_from = self._migrate()
        self._q = queue.Queue()
        threading.Thread(target=self._writer, daemon=True, name="ff-log").start()

    def _migrate(self):
        """Bring the file up to SCHEMA_VERSION, one committed step at a time. Returns the version it started at."""
        with self.lock:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            start = version
            if version == 0 and self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
                version = 1                      # a database from before versioning existed
            while version < SCHEMA_VERSION:
                try:
                    for stmt in MIGRATIONS[version]:
                        self.db.execute(stmt)
                    version += 1
                    self.db.execute(f"PRAGMA user_version = {version}")
                    self.db.commit()
                except sqlite3.Error:
                    self.db.rollback()
                    raise
            return start

    # -- writing ---------------------------------------------------------
    def add_event(self, kind, source="desktop", target=None, detail=None, undo=None, ts=None, demo=False):
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO events(ts,kind,source,target,detail,undo,undoable,demo) VALUES(?,?,?,?,?,?,?,?)",
                (ts or time.time(), kind, source, target,
                 json.dumps(detail or {}), json.dumps(undo) if undo else None,
                 1 if undo else 0, 1 if demo else 0))
            self.db.commit()
            return cur.lastrowid

    def log_async(self, *args, **kwargs):
        """For latency-sensitive threads (the mouse hook)."""
        self._q.put((args, kwargs))

    def _writer(self):
        while True:
            args, kwargs = self._q.get()
            try:
                self.add_event(*args, **kwargs)
            except sqlite3.Error:
                pass
            finally:
                self._q.task_done()

    def flush(self):
        """Wait until every queued asynchronous log entry has been written."""
        self._q.join()

    def close(self):
        self.flush()
        with self.lock:
            self.db.close()

    def update_detail(self, event_id, detail):
        with self.lock:
            self.db.execute("UPDATE events SET detail=?, ts=? WHERE id=?",
                            (json.dumps(detail), time.time(), event_id))
            self.db.commit()

    def withdraw_undo(self, event_id):
        """The action turned out not to be reversible after all; stop offering undo."""
        with self.lock:
            self.db.execute("UPDATE events SET undoable=0, undo=NULL WHERE id=?", (event_id,))
            self.db.commit()

    def mark_undone(self, event_id):
        with self.lock:
            self.db.execute("UPDATE events SET undone=1 WHERE id=?", (event_id,))
            self.db.commit()

    def clear(self, demo):
        with self.lock:
            for table in ("events", "sessions", "pointer_stats"):
                self.db.execute(f"DELETE FROM {table} WHERE demo=?", (1 if demo else 0,))
            self.db.commit()

    # -- sessions & pointer statistics ---------------------------------------
    def add_session(self, rec, ts_start, ts_end, demo=False):
        cols = ", ".join(SESSION_FIELDS)
        marks = ", ".join("?" * len(SESSION_FIELDS))
        with self.lock:
            cur = self.db.execute(
                f"INSERT INTO sessions(ts_start, ts_end, {cols}, demo) VALUES(?,?,{marks},?)",
                (ts_start, ts_end, *[rec.get(f) for f in SESSION_FIELDS], 1 if demo else 0))
            self.db.commit()
            return cur.lastrowid

    def sessions(self, limit=200, demo=False):
        with self.lock:
            rows = self.db.execute("SELECT * FROM sessions WHERE demo=? ORDER BY id DESC LIMIT ?",
                                   (1 if demo else 0, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id):
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE id=?", (int(session_id),))
            self.db.commit()

    def add_pointer_stats(self, rec, profile=None, ts=None, demo=False):
        cols = ", ".join(POINTER_FIELDS)
        marks = ", ".join("?" * len(POINTER_FIELDS))
        with self.lock:
            self.db.execute(f"INSERT INTO pointer_stats(ts, profile, {cols}, demo) VALUES(?,?,{marks},?)",
                            (ts or time.time(), profile, *[rec.get(f, 0) for f in POINTER_FIELDS], 1 if demo else 0))
            self.db.commit()

    def pointer_totals(self, since=0.0, demo=False):
        with self.lock:
            r = self.db.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(seconds),0) seconds, COALESCE(SUM(raw_distance),0) raw_distance,"
                " COALESCE(SUM(stabilised_distance),0) stabilised_distance, COALESCE(SUM(raw_changes),0) raw_changes,"
                " COALESCE(SUM(stabilised_changes),0) stabilised_changes,"
                " COALESCE(SUM(raw_corrective),0) raw_corrective,"
                " COALESCE(SUM(stabilised_corrective),0) stabilised_corrective,"
                " COALESCE(SUM(erratic_flags),0) erratic_flags FROM pointer_stats WHERE demo=? AND ts>=?",
                (1 if demo else 0, since)).fetchone()
        return dict(r)

    def pointer_series(self, limit=60, demo=False):
        with self.lock:
            rows = self.db.execute("SELECT ts, raw_distance, stabilised_distance, raw_changes, stabilised_changes "
                                   "FROM pointer_stats WHERE demo=? ORDER BY id DESC LIMIT ?",
                                   (1 if demo else 0, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def mode_summary(self, demo=False):
        """Average of every completed session per experiment mode."""
        with self.lock:
            rows = self.db.execute(
                "SELECT mode, COUNT(*) n, AVG(seconds) seconds, AVG(raw_distance) raw_distance,"
                " AVG(stabilised_distance) stabilised_distance, AVG(raw_changes) raw_changes,"
                " AVG(stabilised_changes) stabilised_changes, AVG(raw_corrective) raw_corrective,"
                " AVG(stabilised_corrective) stabilised_corrective, AVG(successes) successes,"
                " AVG(incorrect) incorrect, AVG(near_misses) near_misses, AVG(accidental) accidental"
                " FROM sessions WHERE demo=? AND completed=1 GROUP BY mode", (1 if demo else 0,)).fetchall()
        return {r["mode"]: dict(r) for r in rows}

    # -- reading ---------------------------------------------------------
    @staticmethod
    def _row(r):
        d = dict(r)
        d["detail"] = json.loads(d["detail"] or "{}")
        d["undo"] = json.loads(d["undo"]) if d["undo"] else None
        d["undoable"] = bool(d["undoable"]) and not d["undone"]
        d["undone"] = bool(d["undone"])
        d["demo"] = bool(d["demo"])
        return d

    def get(self, event_id):
        with self.lock:
            r = self.db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return self._row(r) if r else None

    def recent(self, limit=200, demo=False):
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE demo=? ORDER BY id DESC LIMIT ?",
                                   (1 if demo else 0, int(limit))).fetchall()
        return [self._row(r) for r in rows]

    def last_undoable(self):
        with self.lock:
            r = self.db.execute("SELECT * FROM events WHERE demo=0 AND undoable=1 AND undone=0 "
                                "ORDER BY id DESC LIMIT 1").fetchone()
        return self._row(r) if r else None

    def has_demo(self):
        with self.lock:
            return self.db.execute("SELECT 1 FROM events WHERE demo=1 LIMIT 1").fetchone() is not None

    def stats(self, demo=False):
        with self.lock:
            rows = self.db.execute(
                "SELECT kind, source, detail, ts FROM events WHERE demo=? AND kind IN "
                "('click_assisted','manual_click','near_miss','click_prevented')",
                (1 if demo else 0,)).fetchall()
        return compute_stats([(r["kind"], r["source"], json.loads(r["detail"] or "{}"), r["ts"]) for r in rows])

    def near_miss_points(self, demo=False, limit=300):
        with self.lock:
            rows = self.db.execute("SELECT detail FROM events WHERE demo=? AND kind='near_miss' "
                                   "ORDER BY id DESC LIMIT ?", (1 if demo else 0, limit)).fetchall()
        pts = []
        for r in rows:
            d = json.loads(r["detail"] or "{}")
            if "nx" in d and "ny" in d:
                pts.append({"nx": d["nx"], "ny": d["ny"], "corrected": bool(d.get("corrected"))})
        return pts

    # -- demo data (flagged demo=1, never mixed into real stats) -------------
    def seed_demo(self):
        rng = random.Random(7)
        now = time.time()
        with self.lock:
            for table in ("events", "sessions", "pointer_stats"):
                self.db.execute(f"DELETE FROM {table} WHERE demo=1")
            from .profiles import MODE_NAMES
            for n, mode in enumerate(MODE_NAMES * 2):
                jit, dw, ad = (n % 5) in (1, 4), (n % 5) in (2, 4), (n % 5) in (3, 4)
                raw = rng.uniform(5200, 6400)
                rec = {"mode": mode, "task": "demo", "profile": "Comfort", "completed": 1,
                       "seconds": rng.uniform(26, 34) * (0.8 if dw or ad else 1.0),
                       "raw_distance": raw, "stabilised_distance": raw * (rng.uniform(.62, .72) if jit else 1.0),
                       "raw_changes": rng.randint(70, 95),
                       "stabilised_changes": rng.randint(30, 45) if jit else rng.randint(70, 95),
                       "raw_corrective": rng.randint(35, 50),
                       "stabilised_corrective": rng.randint(10, 20) if jit else rng.randint(35, 50),
                       "successes": 10, "incorrect": rng.randint(0, 2) if (ad or dw) else rng.randint(2, 6),
                       "near_misses": rng.randint(0, 1) if ad else rng.randint(1, 4),
                       "accidental": rng.randint(0, 1) if dw else rng.randint(0, 3),
                       "erratic_flags": rng.randint(0, 3), "targets": 10}
                self.add_session(rec, now - (10 - n) * 3600, now - (10 - n) * 3600 + rec["seconds"], demo=True)
            for i in range(24):
                raw = rng.uniform(9000, 16000)
                self.add_pointer_stats({"seconds": 60, "raw_distance": raw,
                                        "stabilised_distance": raw * rng.uniform(.6, .75),
                                        "raw_changes": rng.randint(80, 200), "stabilised_changes": rng.randint(30, 80),
                                        "raw_corrective": rng.randint(40, 90), "stabilised_corrective": rng.randint(10, 30),
                                        "erratic_flags": rng.randint(0, 2)}, "Comfort", ts=now - (24 - i) * 300, demo=True)
            for i in range(60):
                ts = now - (60 - i) * 90
                if i % 3 == 0:
                    d = {"mode": "manual", "path_px": rng.randint(900, 2200), "reversals": rng.randint(9, 26),
                         "duration_ms": rng.randint(3200, 7800)}
                    self.add_event("manual_click", "desktop", "demo target", d, ts=ts, demo=True)
                else:
                    d = {"mode": "assisted", "method": "dwell", "path_px": rng.randint(500, 1100),
                         "reversals": rng.randint(1, 8), "duration_ms": rng.randint(1800, 3600)}
                    self.add_event("click_assisted", rng.choice(["desktop", "browser"]), "demo target", d,
                                   ts=ts, demo=True)
                if i % 4 == 0:
                    nx, ny = rng.uniform(-1.6, 1.6), rng.uniform(-1.6, 1.6)
                    if abs(nx) < 0.55 and abs(ny) < 0.55:
                        nx += 0.9
                    self.add_event("near_miss", "desktop", "demo target",
                                   {"nx": nx, "ny": ny, "corrected": i % 8 != 0}, ts=ts, demo=True)
                if i % 9 == 0:
                    self.add_event("click_prevented", "desktop", "demo target",
                                   {"reason": "repeat_dwell_guard"}, ts=ts, demo=True)
            self.db.commit()


def _avg(vals):
    return round(sum(vals) / len(vals), 1) if vals else None


def compute_stats(rows):
    """rows: (kind, source, detail dict, ts). Pure, so it is unit-testable.

    Every assisted click is one `click_assisted` event (dwell, snapped dwell, or a redirected
    near-miss click), so total_assisted is simply their count.
    """
    assisted = [r for r in rows if r[0] == "click_assisted"]
    manual = [r for r in rows if r[0] == "manual_click"]
    misses = [r for r in rows if r[0] == "near_miss"]
    prevented = [r for r in rows if r[0] == "click_prevented"]

    def eff(group):
        def col(name):
            return [r[2][name] for r in group if r[2].get(name) is not None]
        return {"n": len(group), "avg_path_px": _avg(col("path_px")),
                "avg_reversals": _avg(col("reversals")), "avg_duration_ms": _avg(col("duration_ms"))}

    return {
        "total_assisted": len(assisted),
        "dwell_clicks": sum(1 for r in assisted if r[2].get("method") in ("dwell", "snap_dwell")),
        "near_miss_detected": len(misses),
        "near_miss_corrected": sum(1 for r in misses if r[2].get("corrected")),
        "prevented": len(prevented),
        "by_source": {
            "desktop": sum(1 for r in assisted if r[1] == "desktop"),
            "browser": sum(1 for r in assisted if r[1] == "browser"),
        },
        "efficiency": {"manual": eff(manual), "assisted": eff(assisted)},
    }
