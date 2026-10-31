"""Reproducible synthetic challenge evaluation, separate from the legacy demo workflow."""
import csv
import importlib.metadata
import json
from pathlib import Path
import shutil

import numpy as np

from .data import LABELS, STRESS_SCENARIOS, read_windows
from .model import MotionClassifier, fit_head, motion_features, probabilities, score, sha256, split_challenge


def _scenarios(path, windows):
    by_session = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            scenario = row.get("scenario")
            if scenario not in STRESS_SCENARIOS or STRESS_SCENARIOS[scenario] != row["label"]:
                raise ValueError("Challenge data requires a known scenario with its documented label")
            session = row["session_id"]
            if by_session.setdefault(session, scenario) != scenario:
                raise ValueError("A session cannot change stress scenario")
    return np.asarray([by_session[s] for s in windows.sessions])


def reference_training_indices(reference, report, reference_path, challenge):
    """Reconstruct the missing baseline from the original training partition only."""
    if sha256(reference_path) != report["data_sha256"]:
        raise ValueError("Reference dataset hash differs from the saved training report")
    train_groups, test_groups = set(report["train_groups"]), set(report["test_groups"])
    if train_groups & test_groups or train_groups | test_groups != set(reference.groups):
        raise ValueError("Invalid original train/test group manifest")
    if set(reference.groups) & set(challenge.groups) or set(reference.sessions) & set(challenge.sessions):
        raise ValueError("Challenge sessions/groups overlap the original dataset")
    indices = np.flatnonzero(np.isin(reference.groups, list(train_groups)))
    if len(indices) != report["train_windows"] or set(reference.labels[indices]) != set(LABELS):
        raise ValueError("Original training partition does not match its report")
    return indices


def evaluate_stress(data_path, reference_path, model_dir, output, seed=42):
    """Compare original and stress-trained classifiers using a single sealed split.

    Fixed C=0.1 for both architectures, no hyperparameter search or refit on validation.
    Only the stress training indices enter new scalers/heads. The original model stays
    frozen; only its missing baseline is reconstructed from the verified old dataset.
    """
    output, model_dir = Path(output), Path(model_dir)
    if output.exists():
        raise ValueError("Stress output already exists; choose a new directory")
    windows = read_windows(data_path)
    if set(windows.sources) != {"synthetic"}:
        raise ValueError("This stress workflow requires explicitly synthetic data")
    scenarios = _scenarios(data_path, windows)
    splits = split_challenge(windows, seed)
    reference = read_windows(reference_path)
    original_report = json.loads((model_dir / "metrics.json").read_text(encoding="utf-8"))
    reference_train = reference_training_indices(reference, original_report, reference_path, windows)
    reference_features = motion_features(reference.velocity)
    original_baseline = fit_head(reference_features[reference_train], reference.labels[reference_train])
    reference_test = np.flatnonzero(np.isin(reference.groups, original_report["test_groups"]))
    reconstructed = score(reference_features[reference_test], reference.labels[reference_test], original_baseline)
    for metric in ("accuracy", "macro_f1"):
        if not np.isclose(reconstructed[metric], original_report["motion_features_baseline"][metric], atol=1e-12, rtol=0):
            raise ValueError("Reconstructed baseline does not reproduce the original report; check dependency versions")
    classifier = MotionClassifier(model_dir)
    features = classifier.encoder.features(windows.velocity)
    basic = motion_features(windows.velocity)
    train_indices = splits["train"]
    adapted_head = fit_head(features[train_indices], windows.labels[train_indices])
    adapted_baseline = fit_head(basic[train_indices], windows.labels[train_indices])
    models = {
        "original_chronos": (features, classifier.artifact["head"]),
        "original_feature_baseline": (basic, original_baseline),
        "stress_trained_chronos": (features, adapted_head),
        "stress_trained_feature_baseline": (basic, adapted_baseline),
    }
    report = {
        "evaluation_schema": 1, "synthetic_only": True, "split_seed": seed,
        "data_sha256": sha256(data_path), "reference_data_sha256": sha256(reference_path),
        "original_classifier_sha256": sha256(model_dir / "classifier.json"),
        "original_metrics_sha256": sha256(model_dir / "metrics.json"),
        "backbone_sha256": classifier.artifact["backbone_sha256"],
        "base_revision": classifier.artifact["base_revision"],
        "protocol": "Fixed C=0.1. Group-disjoint 60/20/20 train/validation/challenge-test. "
                    "New heads/scalers fit stress training windows only; no validation or test refitting. "
                    "Original baseline reconstructed exclusively from original training groups.",
        "feature_schema": {"engineered_features": 7, "chronos_plus_engineered_features": int(features.shape[1]),
                           "hz": 60, "velocity_shape": [2, 120], "unchanged": True},
        "label_policy": "Intentional task motion with superimposed oscillations is intentional. "
                        "Noise without a periodic tremor generator is steady. Tremor-like generators "
                        "have oscillations with at most small drift. These are synthetic task labels.",
        "reconstructed_original_baseline_test": reconstructed,
        "splits": {}, "validation": {}, "challenge_test": {}, "challenge_by_scenario": {},
        "limitations": "Synthetic only; no clinical validation or demonstrated accessibility benefit. "
                       "Challenge sessions are independent draws from the same scenario families, not "
                       "unseen participants or unseen scenario types. Windows within a session are correlated. "
                       "Low-amplitude oscillation and random noise can be ambiguous under these labels. "
                       "Scores are uncalibrated. Feature schema and live cursor behaviour are unchanged.",
        "versions": {p: importlib.metadata.version(p) for p in
                     ("numpy", "scikit-learn", "torch", "transformers", "chronos-forecasting")},
    }
    for name, indices in splits.items():
        report["splits"][name] = {
            "windows": len(indices), "groups": sorted(set(windows.groups[indices])),
            "sessions": sorted(set(np.asarray(windows.sessions)[indices])),
            "class_counts": {label: int(np.sum(windows.labels[indices] == label)) for label in LABELS},
            "scenario_counts": {s: int(np.sum(scenarios[indices] == s)) for s in sorted(set(scenarios))},
        }
    predictions = []
    # Same indices/order for every model, including the frozen original models.
    for partition in ("validation", "challenge_test"):
        indices = splits[partition]
        for name, (x, head) in models.items():
            report[partition][name] = score(x[indices], windows.labels[indices], head)
        all_probs = {name: probabilities(x[indices], head) for name, (x, head) in models.items()}
        for j, i in enumerate(indices):
            predictions.append({
                "partition": partition, "session_id": windows.sessions[i], "group_id": str(windows.groups[i]),
                "start_s": windows.starts[i], "scenario": str(scenarios[i]), "label": str(windows.labels[i]),
                "models": {name: {"label": head["classes"][int(all_probs[name][j].argmax())],
                                  "scores": dict(zip(head["classes"], map(float, all_probs[name][j])))}
                           for name, (_, head) in models.items()},
            })
    test_indices = splits["challenge_test"]
    for scenario in sorted(set(scenarios[test_indices])):
        indices = test_indices[scenarios[test_indices] == scenario]
        report["challenge_by_scenario"][scenario] = {
            name: score(x[indices], windows.labels[indices], head) for name, (x, head) in models.items()}
    # Create a new bundle compatible with the existing predict command; preserve the source bundle.
    output.mkdir(parents=True, exist_ok=False)
    adapted_dir = output / "model"
    shutil.copytree(model_dir / "backbone", adapted_dir / "backbone")
    artifact = dict(classifier.artifact, head=adapted_head, sources=["synthetic"])
    for path, payload in (
        (adapted_dir / "classifier.json", artifact),
        (output / "original-feature-baseline.json", original_baseline),
        (output / "stress-feature-baseline.json", adapted_baseline),
        (output / "metrics.json", report), (output / "predictions.json", predictions),
    ):
        with path.open("x", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=2, allow_nan=False) + "\n")
    return report
