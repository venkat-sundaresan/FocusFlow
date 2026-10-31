"""Portable ML tests; heavy model download is covered separately by the CLI smoke run."""
import csv
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HAS_ML = all(importlib.util.find_spec(p) is not None for p in ("numpy", "sklearn"))
if HAS_ML:
    import numpy as np
    from focusflow.ml.data import (generate_demo, generate_stress, read_windows, LABELS, STEPS,
                                  HZ, STRESS_SCENARIOS, _stress_path)
    from focusflow.ml.model import (fit_head, motion_features, probabilities, split_groups,
                                   split_challenge, score, sha256, MotionClassifier)
    from focusflow.ml.stress import evaluate_stress, reference_training_indices


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


@unittest.skipUnless(HAS_ML, "optional ML dependencies not installed")
class StressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "stress.csv"
        generate_stress(self.path, groups=60, seed=7)

    def test_deterministic_independent_sessions_and_scenarios(self):
        same, other = self.root / "same.csv", self.root / "other.csv"
        generate_stress(same, groups=60, seed=7)
        generate_stress(other, groups=60, seed=8)
        self.assertEqual(self.path.read_bytes(), same.read_bytes())
        a, b = read_windows(self.path), read_windows(other)
        self.assertEqual(a.velocity.shape, (180, 2, STEPS))
        self.assertEqual(len(set(a.sessions)), 60)
        self.assertFalse(set(a.sessions) & set(b.sessions))
        self.assertTrue(np.isfinite(a.velocity).all())
        with self.path.open() as f:
            self.assertEqual({r["scenario"] for r in csv.DictReader(f)}, set(STRESS_SCENARIOS))
        with self.assertRaises(FileExistsError):
            generate_stress(self.path)
        before = sha256(self.path)
        with self.assertRaises(ValueError):
            generate_stress(self.root / "invalid.csv", groups=1)
        self.assertEqual(sha256(self.path), before)

    def test_growing_dataset_does_not_change_existing_sessions(self):
        larger = self.root / "larger.csv"
        generate_stress(larger, groups=72, seed=7)
        a, b = read_windows(self.path), read_windows(larger)
        np.testing.assert_array_equal(a.velocity, b.velocity[:len(a.velocity)])

    def test_mixed_signals_contain_both_components(self):
        t = np.arange(6 * HZ + 1) / HZ
        intentional = _stress_path("intentional_oscillation", t, np.random.default_rng(10))
        velocity = np.diff(intentional) * HZ
        self.assertGreater(np.linalg.norm(velocity.mean(axis=1)), 10)
        residual = velocity - velocity.mean(axis=1, keepdims=True)
        power = (np.abs(np.fft.rfft(residual)) ** 2).sum(axis=0)
        freq = np.fft.rfftfreq(len(t) - 1, 1 / HZ)
        self.assertGreater(power[(freq >= 4) & (freq <= 12)].sum() / power.sum(), 0.1)
        self.assertEqual(STRESS_SCENARIOS["intentional_oscillation"], "intentional")
        mixed = _stress_path("mixed_frequencies", t, np.random.default_rng(10))
        power = (np.abs(np.fft.rfft(np.diff(mixed))) ** 2).sum(axis=0)
        self.assertGreater(power[(freq >= 2) & (freq < 4)].sum() / power.sum(), 0.01)
        self.assertGreater(power[(freq >= 7) & (freq <= 13)].sum() / power.sum(), 0.1)

    def test_chirp_frequency_increases(self):
        t = np.arange(6 * HZ + 1) / HZ
        path = _stress_path("variable_frequency", t, np.random.default_rng(3))
        v = np.diff(path)
        frequency = np.fft.rfftfreq(STEPS, 1 / HZ)
        def peak(segment):
            power = (np.abs(np.fft.rfft(segment - segment.mean(axis=1, keepdims=True))) ** 2).sum(axis=0)
            return frequency[power.argmax()]
        self.assertGreater(peak(v[:, -STEPS:]) - peak(v[:, :STEPS]), 2)

    def test_complete_groups_and_sessions_in_three_disjoint_sets(self):
        windows = read_windows(self.path)
        # Multiple sessions from one participant must still travel together.
        mapping = {s: f"participant-{i // 2}" for i, s in enumerate(dict.fromkeys(windows.sessions))}
        windows.groups = np.asarray([mapping[s] for s in windows.sessions])
        splits = split_challenge(windows, seed=17)
        again = split_challenge(windows, seed=17)
        groups_seen, sessions_seen, indices_seen = set(), set(), set()
        for name, indices in splits.items():
            np.testing.assert_array_equal(indices, again[name])
            groups = set(windows.groups[indices])
            sessions = set(np.asarray(windows.sessions)[indices])
            self.assertFalse(groups & groups_seen)
            self.assertFalse(sessions & sessions_seen)
            self.assertFalse(set(indices) & indices_seen)
            self.assertEqual(set(windows.labels[indices]), set(LABELS))
            groups_seen.update(groups)
            sessions_seen.update(sessions)
            indices_seen.update(indices)
        self.assertEqual(indices_seen, set(range(len(windows.labels))))
        windows.sessions[1] = windows.sessions[9]
        with self.assertRaisesRegex(ValueError, "multiple groups"):
            split_challenge(windows)

    def test_small_group_count_rejected(self):
        path = self.root / "small.csv"
        generate_stress(path, groups=12)
        with self.assertRaisesRegex(ValueError, "five independent"):
            split_challenge(read_windows(path))

    def test_seven_features_keep_their_existing_order_and_units(self):
        velocity = np.zeros((1, 2, STEPS), dtype=np.float32)
        velocity[:, 0] = 3
        velocity[:, 1] = 4
        expected = [[np.log(6), 0, np.log(6), 1, 0, 0, 0]]
        np.testing.assert_allclose(motion_features(velocity), expected, atol=1e-6)
        windows = read_windows(self.path)
        self.assertEqual(motion_features(windows.velocity).shape, (180, 7))

    def test_false_positive_denominator_and_confusion_order(self):
        head = {"classes": list(LABELS), "mean": [0], "scale": [1],
                "coef": [[0], [0], [0]], "intercept": [0, 0, 5]}
        result = score(np.zeros((4, 1)), np.array(["steady", "intentional", "tremor_like", "tremor_like"]), head)
        self.assertEqual(result["confusion_matrix"], [[0, 0, 1], [0, 0, 1], [0, 0, 2]])
        self.assertEqual(result["tremor_like_false_positives"], {
            "count": 2, "non_tremor_windows": 2, "rate": 1,
            "by_true_class": {"steady": 1, "intentional": 1}})
        single = score(np.zeros((1, 1)), np.array(["tremor_like"]), head)
        self.assertIsNone(single["tremor_like_false_positives"]["rate"])

    def test_evaluation_fits_only_training_rows_and_preserves_inputs(self):
        reference_path = self.root / "demo.csv"
        generate_demo(reference_path, groups=30)
        reference, windows = read_windows(reference_path), read_windows(self.path)
        train, test = split_groups(reference)
        basic = motion_features(reference.velocity)
        head = fit_head(basic[train], reference.labels[train])
        report = {"data_sha256": sha256(reference_path), "train_windows": len(train),
                  "train_groups": sorted(set(reference.groups[train])),
                  "test_groups": sorted(set(reference.groups[test])),
                  "motion_features_baseline": score(basic[test], reference.labels[test], head)}
        model = self.root / "original"
        (model / "backbone").mkdir(parents=True)
        (model / "backbone" / "config.json").write_text("{}")
        (model / "metrics.json").write_text(json.dumps(report))
        artifact = {"head": head, "backbone_sha256": {}, "base_revision": "test", "sources": ["synthetic"]}
        (model / "classifier.json").write_text(json.dumps(artifact))
        fake = SimpleNamespace(artifact=artifact, encoder=SimpleNamespace(features=motion_features))
        output = self.root / "result"
        original_hashes = {p: sha256(p) for p in (reference_path, self.path, model / "classifier.json")}
        with mock.patch("focusflow.ml.stress.MotionClassifier", return_value=fake), \
                mock.patch("focusflow.ml.stress.fit_head", wraps=fit_head) as fit:
            result = evaluate_stress(self.path, reference_path, model, output)
        self.assertEqual(fit.call_count, 3)
        np.testing.assert_array_equal(fit.call_args_list[0].args[0], basic[train])
        indices = split_challenge(windows)["train"]
        for call in fit.call_args_list[1:]:
            np.testing.assert_array_equal(call.args[0], motion_features(windows.velocity)[indices])
            np.testing.assert_array_equal(call.args[1], windows.labels[indices])
        predictions = json.loads((output / "predictions.json").read_text())
        for row in predictions:
            self.assertEqual(len(row["models"]), 4)
            self.assertIn(row["partition"], ("validation", "challenge_test"))
        for p, digest in original_hashes.items():
            self.assertEqual(sha256(p), digest)
        self.assertTrue(result["synthetic_only"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            evaluate_stress(self.path, reference_path, model, output)
        with self.assertRaisesRegex(ValueError, "overlap"):
            reference_training_indices(reference, report, reference_path, reference)
        report["data_sha256"] = "incorrect"
        with self.assertRaisesRegex(ValueError, "hash differs"):
            reference_training_indices(reference, report, reference_path, windows)

    @unittest.skipUnless(importlib.util.find_spec("torch") is not None, "requires optional torch")
    def test_original_saved_head_still_loads_offline_with_stress_features(self):
        model = Path(__file__).resolve().parents[1] / "artifacts" / "motion-demo"
        if not (model / "backbone" / "model.safetensors").exists():
            self.skipTest("bundled pretrained weights unavailable")
        classifier = MotionClassifier(model)
        velocity = read_windows(self.path).velocity[:3]
        features = classifier.encoder.features(velocity)
        self.assertEqual(features.shape, (3, 519))
        np.testing.assert_array_equal(features[:, -7:], motion_features(velocity))
        np.testing.assert_allclose(probabilities(features, classifier.artifact["head"]).sum(axis=1), 1)


if __name__ == "__main__":
    unittest.main()
