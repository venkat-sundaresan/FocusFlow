"""Frozen pretrained Chronos encoder + fitted, portable linear classification head."""
import hashlib
import importlib.metadata
import json
from pathlib import Path

import numpy as np

from .data import HZ, LABELS, SECONDS, STEPS

MODEL_ID = "amazon/chronos-t5-tiny"
SCHEMA = 1


def motion_features(velocity):
    """Amplitude and direction features retain information lost by Chronos scaling."""
    v = np.asarray(velocity, dtype=np.float32)
    if v.ndim != 3 or v.shape[1:] != (2, STEPS) or not len(v) or not np.isfinite(v).all():
        raise ValueError(f"Expected finite velocity windows shaped (N, 2, {STEPS})")
    speed = np.linalg.norm(v, axis=1)
    mean_speed = speed.mean(axis=1)
    net_speed = np.linalg.norm(v.mean(axis=2), axis=1)
    centered = v - v.mean(axis=2, keepdims=True)
    power = np.abs(np.fft.rfft(centered, axis=2)) ** 2
    frequencies = np.fft.rfftfreq(STEPS, 1 / HZ)
    band = power[:, :, (frequencies >= 4) & (frequencies <= 12)].sum(axis=(1, 2))
    ratio = band / np.maximum(power.sum(axis=(1, 2)), 1e-8)
    return np.column_stack((np.log1p(mean_speed), np.log1p(speed.std(axis=1)),
                            np.log1p(net_speed), net_speed / np.maximum(mean_speed, 1e-8),
                            np.log1p(v.std(axis=2)), ratio)).astype(np.float64)


class Encoder:
    def __init__(self, source=MODEL_ID, revision="main", offline=False):
        import torch
        from chronos import ChronosPipeline

        # Small CPU batches avoid monopolising a workstation. No CUDA required.
        torch.set_num_threads(min(4, torch.get_num_threads()))
        self.pipeline = ChronosPipeline.from_pretrained(
            str(source), revision=revision, device_map="cpu", torch_dtype=torch.float32,
            local_files_only=offline, trust_remote_code=False, use_safetensors=True)
        self.pipeline.model.eval()
        self.pipeline.model.requires_grad_(False)
        self.revision = getattr(self.pipeline.model.model.config, "_commit_hash", None)

    def features(self, velocity, batch_size=16):
        import torch

        basic = motion_features(velocity)
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        embeddings = []
        with torch.inference_mode():
            for start in range(0, len(velocity), batch_size):
                batch = np.asarray(velocity[start:start + batch_size], dtype=np.float32)
                context = torch.from_numpy(batch.reshape(-1, STEPS).copy())
                hidden, _ = self.pipeline.embed(context)
                # No padding; exclude the appended EOS token from temporal pooling.
                pooled = hidden[:, :STEPS].mean(dim=1).numpy().reshape(len(batch), -1)
                embeddings.append(pooled)
        return np.column_stack((np.concatenate(embeddings), basic))

    def save(self, path):
        self.pipeline.model.model.save_pretrained(path, safe_serialization=True)


def split_groups(windows, seed=42):
    from sklearn.model_selection import GroupShuffleSplit

    if set(windows.labels) != set(LABELS):
        raise ValueError(f"Training requires all classes: {LABELS}")
    for label in LABELS:
        if len(set(windows.groups[windows.labels == label])) < 2:
            raise ValueError(f"Need at least two independent groups for class {label}")
    # Choose by label coverage only, never by model performance.
    splitter = GroupShuffleSplit(n_splits=100, test_size=0.25, random_state=seed)
    for train, test in splitter.split(windows.velocity, windows.labels, windows.groups):
        if set(windows.labels[train]) == set(LABELS) == set(windows.labels[test]):
            return train, test
    raise ValueError("Cannot create a group-disjoint split containing every class; add more groups")


def fit_head(features, labels):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler().fit(features)
    classifier = LogisticRegression(C=0.1, max_iter=3000, class_weight="balanced")
    classifier.fit(scaler.transform(features), labels)
    return {"classes": classifier.classes_.tolist(), "mean": scaler.mean_.tolist(),
            "scale": scaler.scale_.tolist(), "coef": classifier.coef_.tolist(),
            "intercept": classifier.intercept_.tolist()}


def split_challenge(windows, seed=42):
    """Approximately 60/20/20 train/validation/challenge, keeping groups intact.

    Session integrity is checked even for Windows constructed without the CSV loader.
    Fold assignment depends only on labels/groups, never features or model scores.
    """
    from sklearn.model_selection import StratifiedGroupKFold

    if set(windows.labels) != set(LABELS):
        raise ValueError(f"Challenge evaluation requires all classes: {LABELS}")
    owners = {}
    for session, group in zip(windows.sessions, windows.groups):
        if owners.setdefault(session, group) != group:
            raise ValueError("A session cannot belong to multiple groups")
    for label in LABELS:
        if len(set(windows.groups[windows.labels == label])) < 5:
            raise ValueError(f"Need at least five independent groups for class {label}")
    splitter = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=seed)
    folds = [test for _, test in splitter.split(windows.velocity, windows.labels, windows.groups)]
    parts = {"train": np.sort(np.concatenate(folds[2:])), "validation": folds[1], "challenge_test": folds[0]}
    for name, indices in parts.items():
        if set(windows.labels[indices]) != set(LABELS):
            raise ValueError(f"{name} lacks a class; supply more independent groups")
    return parts


def probabilities(features, head):
    x = np.asarray(features, dtype=np.float64)
    mean, scale = np.asarray(head["mean"]), np.asarray(head["scale"])
    coef, intercept = np.asarray(head["coef"]), np.asarray(head["intercept"])
    if (x.ndim != 2 or mean.shape != (x.shape[1],) or scale.shape != mean.shape
            or coef.shape != (len(LABELS), x.shape[1]) or intercept.shape != (len(LABELS),)
            or set(head["classes"]) != set(LABELS) or len(head["classes"]) != len(LABELS)
            or not all(np.isfinite(a).all() for a in (x, mean, scale, coef, intercept))
            or np.any(scale <= 0)):
        raise ValueError("Invalid classification head or incompatible features")
    logits = ((x - mean) / scale) @ coef.T + intercept
    logits -= logits.max(axis=1, keepdims=True)
    exp = np.exp(logits)
    return exp / exp.sum(axis=1, keepdims=True)


def score(features, labels, head):
    from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score

    predicted = np.asarray(head["classes"])[probabilities(features, head).argmax(axis=1)]
    negative = np.asarray(labels) != "tremor_like"
    false_positive = negative & (predicted == "tremor_like")
    return {"accuracy": float(accuracy_score(labels, predicted)),
            "macro_f1": float(f1_score(labels, predicted, labels=LABELS, average="macro", zero_division=0)),
            "confusion_matrix_labels": list(LABELS),
            "confusion_matrix": confusion_matrix(labels, predicted, labels=LABELS).tolist(),
            "per_class": classification_report(labels, predicted, labels=LABELS, output_dict=True, zero_division=0),
            "tremor_like_false_positives": {
                "count": int(false_positive.sum()), "non_tremor_windows": int(negative.sum()),
                "rate": float(false_positive.sum() / negative.sum()) if negative.any() else None,
                "by_true_class": {label: int(np.sum(false_positive & (np.asarray(labels) == label)))
                                  for label in LABELS if label != "tremor_like"}}}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def train(windows, data_path, output, seed=42, source=MODEL_ID, revision="main", offline=False):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Output directory is not empty; choose a new directory")
    train_idx, test_idx = split_groups(windows, seed)
    encoder = Encoder(source, revision, offline)
    features = encoder.features(windows.velocity)
    head = fit_head(features[train_idx], windows.labels[train_idx])
    basic = motion_features(windows.velocity)
    baseline = fit_head(basic[train_idx], windows.labels[train_idx])
    labels, counts = np.unique(windows.labels[train_idx], return_counts=True)
    majority = labels[counts.argmax()]
    report = {
        "schema": SCHEMA, "seed": seed, "data_sha256": sha256(data_path),
        "sources": sorted(set(windows.sources)),
        "contains_synthetic_data": "synthetic" in windows.sources,
        "base_model": str(source), "base_revision": encoder.revision or revision,
        "method": "frozen Chronos encoder + standardized logistic regression; encoder not fine-tuned",
        "train_windows": len(train_idx), "test_windows": len(test_idx),
        "train_groups": sorted(set(windows.groups[train_idx])),
        "test_groups": sorted(set(windows.groups[test_idx])),
        "transfer_model": score(features[test_idx], windows.labels[test_idx], head),
        "motion_features_baseline": score(basic[test_idx], windows.labels[test_idx], baseline),
        "majority_baseline_accuracy": float(np.mean(windows.labels[test_idx] == majority)),
        "limitations": "Offline motion classification only. Synthetic performance does not establish "
                       "benefit for people, clinical validity, or safe automatic cursor control. "
                       "Softmax scores are not calibrated confidence or out-of-distribution detection.",
        "versions": {p: importlib.metadata.version(p) for p in
                     ("numpy", "scikit-learn", "torch", "transformers", "chronos-forecasting")},
    }
    output.mkdir(parents=True, exist_ok=True)
    encoder.save(output / "backbone")
    artifact = {"schema": SCHEMA, "hz": HZ, "seconds": SECONDS, "head": head,
                "sources": report["sources"], "base_model": report["base_model"],
                "base_revision": report["base_revision"],
                "backbone_sha256": {p.name: sha256(p) for p in (output / "backbone").iterdir()
                                    if p.is_file()}}
    (output / "classifier.json").write_text(json.dumps(artifact, indent=2, allow_nan=False), encoding="utf-8")
    (output / "metrics.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    return report


class MotionClassifier:
    """Load once and reuse off the mouse-hook thread. Always loads local weights only."""
    def __init__(self, model_dir):
        path = Path(model_dir)
        self.artifact = json.loads((path / "classifier.json").read_text(encoding="utf-8"))
        a = self.artifact
        if a["schema"] != SCHEMA or a["hz"] != HZ or a["seconds"] != SECONDS:
            raise ValueError("Unsupported model schema or preprocessing")
        for name, digest in a["backbone_sha256"].items():
            if Path(name).name != name or sha256(path / "backbone" / name) != digest:
                raise ValueError("Backbone integrity check failed")
        self.encoder = Encoder(path / "backbone", offline=True)

    def predict(self, velocity):
        head = self.artifact["head"]
        probs = probabilities(self.encoder.features(velocity), head)
        return [{"label": head["classes"][int(p.argmax())],
                 "scores": dict(zip(head["classes"], map(float, p))),
                 "trained_on_synthetic": "synthetic" in self.artifact["sources"]} for p in probs]
