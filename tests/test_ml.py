"""Portable ML tests; heavy model download is covered separately by the CLI smoke run."""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HAS_ML = all(importlib.util.find_spec(p) is not None for p in ("numpy", "sklearn"))
if HAS_ML:
    import numpy as np
    from focusflow.ml.data import generate_demo, read_windows, LABELS, STEPS
    from focusflow.ml.model import fit_head, motion_features, probabilities, split_groups


@unittest.skipUnless(HAS_ML, "optional ML dependencies not installed")
class MLTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "demo.csv"
        generate_demo(self.path, groups=30)

    def edit_rows(self, edit):
        with self.path.open(newline="") as f:
            reader = csv.DictReader(f)
            fields, rows = reader.fieldnames, list(reader)
        edit(rows)
        with self.path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_synthetic_reproducibility_and_no_overwrite(self):
        other = self.path.with_name("same.csv")
        generate_demo(other, groups=30)
        self.assertEqual(self.path.read_bytes(), other.read_bytes())
        with self.assertRaises(FileExistsError):
            generate_demo(other)

    def test_group_split_has_no_leakage(self):
        windows = read_windows(self.path)
        self.assertEqual(windows.velocity.shape, (90, 2, STEPS))
        train, test = split_groups(windows)
        self.assertFalse(set(windows.groups[train]) & set(windows.groups[test]))
        self.assertEqual(set(windows.labels[test]), set(LABELS))
        self.assertEqual(set(windows.sources), {"synthetic"})
        self.assertEqual(len(set(windows.groups)), 30)

    def test_translation_and_dpi_invariance(self):
        before = read_windows(self.path).velocity
        def transform(rows):
            for row in rows:
                row["x"] = str(float(row["x"]) * 2 + 2000)
                row["y"] = str(float(row["y"]) * 2 - 500)
                row["dpi_scale"] = "2"
        self.edit_rows(transform)
        np.testing.assert_allclose(before, read_windows(self.path).velocity, atol=1e-5)

    def test_invalid_timestamp_and_nan_rejected(self):
        self.edit_rows(lambda rows: rows[1].update(timestamp_s=rows[0]["timestamp_s"]))
        with self.assertRaisesRegex(ValueError, "increase strictly"):
            read_windows(self.path)
        self.edit_rows(lambda rows: rows[1].update(timestamp_s="nan"))
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            read_windows(self.path)

    def test_inconsistent_session_and_invalid_label_rejected(self):
        self.edit_rows(lambda rows: rows[1].update(group_id="someone-else"))
        with self.assertRaisesRegex(ValueError, "changes group"):
            read_windows(self.path)
        self.edit_rows(lambda rows: rows[0].update(label="unknown"))
        with self.assertRaisesRegex(ValueError, "Invalid session"):
            read_windows(self.path)

    def test_gap_is_not_interpolated(self):
        def keep(rows):
            rows[:] = [r for r in rows if r["session_id"] == "demo-42-0"
                       and (float(r["timestamp_s"]) < 1 or float(r["timestamp_s"]) > 5)]
        self.edit_rows(keep)
        with self.assertRaisesRegex(ValueError, "No complete"):
            read_windows(self.path)

    def test_missing_classes_fail_before_loading_encoder(self):
        windows = read_windows(self.path)
        windows.labels[:] = "steady"
        with self.assertRaisesRegex(ValueError, "all classes"):
            split_groups(windows)

    def test_head_json_round_trip_and_separable_predictions(self):
        # Independent simple dataset checks the learned head and portable softmax.
        x = np.array([[-4, 0], [-3, 0], [4, 0], [3, 0], [0, 4], [0, 3]])
        y = np.repeat(LABELS, 2)
        head = json.loads(json.dumps(fit_head(x, y)))
        p = probabilities(x, head)
        np.testing.assert_allclose(p.sum(axis=1), 1)
        np.testing.assert_array_equal(np.array(head["classes"])[p.argmax(axis=1)], y)
        head["scale"][0] = 0
        with self.assertRaises(ValueError):
            probabilities(x, head)

    def test_features_handle_stationary_input_and_validate_shape(self):
        self.assertTrue(np.isfinite(motion_features(np.zeros((2, 2, STEPS)))).all())
        with self.assertRaises(ValueError):
            motion_features(np.zeros((1, 2, 30)))

    def test_prediction_does_not_require_labels(self):
        self.edit_rows(lambda rows: [r.update(label="") for r in rows])
        self.assertEqual(len(read_windows(self.path, labeled=False).velocity), 90)


if __name__ == "__main__":
    unittest.main()
