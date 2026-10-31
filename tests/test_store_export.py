import csv
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from focusflow import export
from focusflow.store import SCHEMA_VERSION, Store


def make_v1_database(path):
    """A database exactly as the original FocusFlow wrote it (no user_version, launcher-era undo records)."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, "
               "source TEXT NOT NULL, target TEXT, detail TEXT, undo TEXT, undoable INTEGER NOT NULL DEFAULT 0, "
               "undone INTEGER NOT NULL DEFAULT 0, demo INTEGER NOT NULL DEFAULT 0)")
    db.execute("INSERT INTO events(ts,kind,source,target,detail,undo,undoable) VALUES(1,'app_launch','desktop','Notes','{}',"
               "'{\"type\":\"close_pid\",\"pid\":4,\"exe\":\"notepad.exe\"}',1)")
    db.execute("INSERT INTO events(ts,kind,source,target,detail,undo,undoable) VALUES(2,'setting_change','desktop','dwell_ms',"
               "'{}','{\"type\":\"settings\",\"previous\":{\"dwell_ms\":1000}}',1)")
    db.execute("INSERT INTO events(ts,kind,source,target,detail) VALUES(3,'click_assisted','desktop','OK',"
               "'{\"method\":\"dwell\",\"path_px\":400,\"reversals\":2,\"duration_ms\":2000}')")
    db.execute("INSERT INTO events(ts,kind,source,target,detail,demo) VALUES(4,'click_assisted','desktop','demo','{}',1)")
    db.commit()
    db.close()


def session(mode="Full FocusFlow assistance", **kw):
    rec = {"mode": mode, "task": "Target sequence", "profile": "Comfort", "completed": 1, "seconds": 30.0,
           "raw_distance": 6000.0, "stabilised_distance": 4000.0, "raw_changes": 80, "stabilised_changes": 40,
           "raw_corrective": 40, "stabilised_corrective": 15, "successes": 10, "incorrect": 1, "near_misses": 1,
           "accidental": 0, "erratic_flags": 1, "targets": 10}
    rec.update(kw)
    return rec


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "a.db")

    def tearDown(self):
        self.dir.cleanup()

    def test_a_v1_database_is_upgraded_without_losing_data(self):
        make_v1_database(self.path)
        s = Store(self.path)
        self.assertEqual(s.migrated_from, 0)
        self.assertEqual(s.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(len(s.recent(10)), 3)                       # every real row preserved
        self.assertEqual(len(s.recent(10, demo=True)), 1)            # demo rows still separate
        self.assertEqual(s.stats()["total_assisted"], 1)
        names = {r[0] for r in s.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"events", "sessions", "pointer_stats"} <= names)
        s.close()

    def test_launcher_era_undo_records_are_disarmed_but_settings_undo_survives(self):
        make_v1_database(self.path)
        s = Store(self.path)
        self.assertEqual(s.last_undoable()["target"], "dwell_ms")    # the close_pid record can no longer be offered
        self.assertIsNone(s.get(1)["undo"])
        self.assertEqual(s.get(1)["kind"], "app_launch")             # but the history row remains
        s.close()

    def test_reopening_is_a_no_op_and_a_fresh_database_starts_current(self):
        make_v1_database(self.path)
        Store(self.path).close()
        s = Store(self.path)
        self.assertEqual(s.migrated_from, SCHEMA_VERSION)
        s.close()
        fresh = Store(os.path.join(self.dir.name, "fresh.db"))
        self.assertEqual(fresh.migrated_from, 0)
        self.assertEqual(fresh.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        fresh.close()


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.s = Store(os.path.join(self.dir.name, "t.db"))

    def tearDown(self):
        self.s.close()
        self.dir.cleanup()

    def test_sessions_round_trip_and_newest_comes_first(self):
        a = self.s.add_session(session(seconds=20.0), 100, 120)
        b = self.s.add_session(session(seconds=25.0), 200, 225)
        rows = self.s.sessions()
        self.assertEqual([r["id"] for r in rows], [b, a])
        self.assertEqual(rows[0]["raw_distance"], 6000.0)
        self.s.delete_session(a)
        self.assertEqual(len(self.s.sessions()), 1)

    def test_mode_summary_averages_only_completed_real_sessions(self):
        self.s.add_session(session("Normal mouse operation", seconds=40), 1, 2)
        self.s.add_session(session("Normal mouse operation", seconds=20), 3, 4)
        self.s.add_session(session("Normal mouse operation", seconds=999, completed=0), 5, 6)
        self.s.add_session(session("Jitter reduction only", seconds=10), 7, 8, demo=True)
        m = self.s.mode_summary()
        self.assertEqual(set(m), {"Normal mouse operation"})
        self.assertEqual(m["Normal mouse operation"]["n"], 2)
        self.assertEqual(m["Normal mouse operation"]["seconds"], 30.0)

    def test_pointer_totals_sum_and_filter_by_time_and_demo(self):
        self.s.add_pointer_stats({"seconds": 60, "raw_distance": 1000, "stabilised_distance": 700, "raw_changes": 20,
                                  "stabilised_changes": 8}, "Comfort", ts=1000)
        self.s.add_pointer_stats({"seconds": 60, "raw_distance": 500, "stabilised_distance": 400, "raw_changes": 10,
                                  "stabilised_changes": 4}, "Comfort", ts=2000)
        t = self.s.pointer_totals()
        self.assertEqual((t["n"], t["raw_distance"], t["stabilised_changes"]), (2, 1500, 12))
        self.assertEqual(self.s.pointer_totals(since=1500)["n"], 1)
        self.assertEqual(self.s.pointer_totals(demo=True)["n"], 0)
        self.assertEqual(len(self.s.pointer_series()), 2)

    def test_demo_data_is_flagged_and_never_mixed_into_real_numbers(self):
        self.s.seed_demo()
        self.assertEqual(self.s.sessions(), [])
        self.assertEqual(self.s.pointer_totals()["n"], 0)
        self.assertGreater(len(self.s.sessions(demo=True)), 0)
        self.s.clear(demo=True)
        self.assertEqual(self.s.sessions(demo=True), [])
        self.assertFalse(self.s.has_demo())

    def test_flush_waits_for_async_writes(self):
        for i in range(50):
            self.s.log_async("click_prevented", "desktop", None, {"i": i})
        self.s.flush()
        self.assertEqual(len(self.s.recent(100)), 50)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.s = Store(os.path.join(self.dir.name, "t.db"))
        for mode in ("Normal mouse operation", "Jitter reduction only", "Full FocusFlow assistance"):
            self.s.add_session(session(mode), 1700000000, 1700000030)
        self.s.add_pointer_stats({"seconds": 60, "raw_distance": 1000, "stabilised_distance": 700, "raw_changes": 20,
                                  "stabilised_changes": 8, "raw_corrective": 9, "stabilised_corrective": 3})
        self.s.add_event("click_assisted", "desktop", "OK", {"method": "dwell"})

    def tearDown(self):
        self.s.close()
        self.dir.cleanup()

    def test_csv_has_one_row_per_session_with_every_required_measure(self):
        rows = list(csv.DictReader(io.StringIO(export.sessions_csv(self.s.sessions()))))
        self.assertEqual(len(rows), 3)
        for col in ("mode", "seconds", "raw_distance", "stabilised_distance", "raw_changes", "stabilised_changes",
                    "raw_corrective", "successes", "incorrect", "near_misses", "accidental", "started"):
            self.assertIn(col, rows[0])
        self.assertEqual(rows[0]["raw_distance"], "6000.0")

    def test_json_bundle_contains_sessions_averages_and_totals(self):
        data = json.loads(export.bundle_json(self.s))
        self.assertEqual(len(data["sessions"]), 3)
        self.assertEqual(len(data["mode_averages"]), 3)
        self.assertEqual(data["pointer_totals"]["raw_distance"], 1000)
        self.assertEqual(data["event_totals"]["total_assisted"], 1)
        self.assertFalse(data["demo_data"])

    def test_demo_exports_are_labelled_as_demo(self):
        self.s.seed_demo()
        data = export.bundle(self.s, demo=True)
        self.assertTrue(data["demo_data"])
        self.assertIn("DEMO", data["note"])
        self.assertIn("-DEMO", export.export_all(self.s, self.dir.name, "csv", demo=True).name)

    def test_export_all_writes_each_format_to_disk(self):
        for fmt in ("csv", "json", "pdf"):
            p = export.export_all(self.s, self.dir.name, fmt)
            self.assertTrue(p.exists() and p.stat().st_size > 100, fmt)
            self.assertEqual(p.suffix, "." + fmt)
        with self.assertRaises(ValueError):
            export.export_all(self.s, self.dir.name, "docx")

    def test_pdf_is_structurally_valid(self):
        pdf = export.report_pdf(export.bundle(self.s))
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        # every xref entry must point at "<n> 0 obj"
        start = int(re.search(rb"startxref\n(\d+)", pdf).group(1))
        self.assertTrue(pdf[start:].startswith(b"xref"))
        count = int(re.search(rb"xref\n0 (\d+)", pdf).group(1))
        entries = re.findall(rb"(\d{10}) 00000 n ", pdf[start:])
        self.assertEqual(len(entries), count - 1)
        for n, off in enumerate(entries, start=1):
            self.assertTrue(pdf[int(off):].startswith(b"%d 0 obj" % n), f"object {n}")
        pages = re.search(rb"/Type /Pages /Kids \[([^\]]*)\] /Count (\d+)", pdf)
        self.assertEqual(len(pages.group(1).split(b" R")) - 1, int(pages.group(2)))
        self.assertIn(b"Experiment comparison", pdf)
        self.assertIn(b"Normal mouse operation", pdf)

    def test_pdf_tolerates_stored_sessions_with_missing_numbers(self):
        self.s.add_session({"mode": "Normal mouse operation", "completed": 1, "targets": 10}, 1700000000, 1700000030)
        pdf = export.report_pdf(export.bundle(self.s))                # NULL columns must print as "-", not crash
        self.assertTrue(pdf.startswith(b"%PDF"))

    def test_pdf_survives_awkward_text_and_paginates_long_histories(self):
        for i in range(120):
            self.s.add_session(session("Mode (with) \\ parens and Ünicode ✓"), 1700000000 + i, 1700000030 + i)
        pdf = export.report_pdf(export.bundle(self.s))
        pages = int(re.search(rb"/Count (\d+)", pdf).group(1))
        self.assertGreaterEqual(pages, 1)
        for m in re.finditer(rb"<< /Length (\d+) >>\nstream\n", pdf):
            n = int(m.group(1))
            self.assertEqual(pdf[m.end() + n:m.end() + n + 10], b"\nendstream")


if __name__ == "__main__":
    unittest.main()
