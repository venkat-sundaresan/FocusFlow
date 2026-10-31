"""Validated pointer recordings and deterministic synthetic demonstration data."""
import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np

LABELS = ("steady", "intentional", "tremor_like")
HZ = 60
SECONDS = 2
STEPS = HZ * SECONDS


@dataclass
class Windows:
    velocity: np.ndarray  # [window, axis, time], logical pixels/second
    labels: np.ndarray
    groups: np.ndarray
    sessions: list
    starts: list
    sources: list


def read_windows(path, labeled=True):
    """CSV timestamps are seconds, positions physical pixels, dpi_scale physical/logical.

    group_id identifies a participant (or session if no participant ID exists).
    Never interpolate across a pause longer than 100 ms. Trailing partial windows
    are discarded; malformed recordings fail explicitly rather than being repaired.
    """
    recordings = {}
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"session_id", "group_id", "timestamp_s", "x", "y", "dpi_scale"}
        if labeled:
            required.add("label")
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"CSV requires columns: {', '.join(sorted(required))}")
        for line, row in enumerate(reader, 2):
            if any(row.get(k) is None for k in required):
                raise ValueError(f"Missing CSV value at line {line}")
            session, group = row["session_id"].strip(), row["group_id"].strip()
            label = row.get("label", "").strip() if labeled else ""
            if not session or not group or (labeled and label not in LABELS):
                raise ValueError(f"Invalid session, group or label at line {line}")
            values = np.array([float(row[k]) for k in ("timestamp_s", "x", "y", "dpi_scale")])
            if not np.isfinite(values).all() or values[3] <= 0:
                raise ValueError(f"Non-finite coordinate or invalid DPI at line {line}")
            source = row.get("source", "recorded")
            info = (group, label, source, values[3])
            previous, samples = recordings.setdefault(session, (info, []))
            if previous != info:
                raise ValueError(f"Session {session} changes group, label, source or DPI; split it first")
            if samples and values[0] <= samples[-1][0]:
                raise ValueError(f"Timestamps must increase strictly within session {session}")
            samples.append((values[0], values[1] / values[3], values[2] / values[3]))
    velocities, labels, groups, sessions, starts, sources = [], [], [], [], [], []
    for session, ((group, label, source, _), samples) in recordings.items():
        a = np.asarray(samples)
        boundaries = np.r_[0, np.flatnonzero(np.diff(a[:, 0]) > 0.1) + 1, len(a)]
        for left, right in zip(boundaries[:-1], boundaries[1:]):
            segment = a[left:right]
            count = int((segment[-1, 0] - segment[0, 0] + 1e-8) // SECONDS)
            for i in range(count):
                start = segment[0, 0] + i * SECONDS
                grid = start + np.arange(STEPS + 1) / HZ
                xy = np.array([np.interp(grid, segment[:, 0], segment[:, axis]) for axis in (1, 2)])
                velocities.append(np.diff(xy, axis=1) * HZ)
                labels.append(label)
                groups.append(group)
                sessions.append(session)
                starts.append(float(start))
                sources.append(source)
    if not velocities:
        raise ValueError("No complete 2-second windows with sample gaps <=100 ms")
    return Windows(np.asarray(velocities, dtype=np.float32), np.asarray(labels),
                   np.asarray(groups), sessions, starts, sources)


def generate_demo(path, groups=90, seed=42):
    """Simulated signal classes only; these are not recordings of people or diagnoses."""
    if groups < 12:
        raise ValueError("Use at least 12 independent synthetic groups")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    with path.open("x", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(("session_id", "group_id", "timestamp_s", "x", "y", "dpi_scale", "label", "source"))
        t = np.arange(6 * HZ + 1) / HZ
        for i in range(groups):
            label = LABELS[i % len(LABELS)]
            xy = rng.normal(0, rng.uniform(0.04, 0.3), (2, len(t)))
            angle = rng.uniform(0, 2 * np.pi)
            direction = np.array([np.cos(angle), np.sin(angle)])[:, None]
            if label == "intentional":
                # Sweeps, curves and deliberate low-frequency reversals.
                speed = rng.uniform(25, 300)
                progress = t if i % 2 else np.sin(2 * np.pi * rng.uniform(0.2, 1.5) * t)
                xy += direction * speed * progress
                xy[1] += 12 * np.sin(t * rng.uniform(0.5, 3))
            elif label == "tremor_like":
                frequency, amplitude = rng.uniform(4, 12), rng.uniform(1.5, 9)
                phases = rng.uniform(0, 2 * np.pi, (2, 1))
                xy += amplitude * np.sin(2 * np.pi * frequency * t + phases)
                xy += direction * rng.uniform(0, 12) * t
            else:
                xy += direction * rng.uniform(0, 0.7) * t
            xy += rng.uniform(100, 800, (2, 1))
            for j, timestamp in enumerate(t):
                writer.writerow((f"demo-{seed}-{i}", f"demo-{seed}-{i}", f"{timestamp:.8f}",
                                 f"{xy[0, j]:.6f}", f"{xy[1, j]:.6f}", 1, label, "synthetic"))


# Synthetic labels describe the generating task, not a person's medical condition.
# Intentional movement stays intentional when oscillations are superimposed.
STRESS_SCENARIOS = {
    "sensor_noise": "steady",
    "slow_drift": "steady",
    "correlated_noise": "steady",
    "noise_bursts": "steady",
    "intentional_oscillation": "intentional",
    "curved_path": "intentional",
    "deliberate_corrections": "intentional",
    "stop_start": "intentional",
    "low_amplitude": "tremor_like",
    "variable_frequency": "tremor_like",
    "mixed_frequencies": "tremor_like",
    "amplitude_modulated": "tremor_like",
}


def _stress_path(scenario, t, rng):
    """Two-axis logical-pixel trajectory with independent session parameters."""
    noise = rng.normal(0, rng.uniform(0.05, 0.45), (2, len(t)))
    phase = rng.uniform(0, 2 * np.pi, (2, 1))
    frequency = rng.uniform(4, 12)
    oscillation = np.sin(2 * np.pi * frequency * t + phase)
    speed = rng.uniform(15, 120)
    if scenario == "sensor_noise":
        xy = rng.normal(0, rng.uniform(0.15, 1.1), (2, len(t)))
    elif scenario == "slow_drift":
        xy = noise + np.array([rng.uniform(0.1, 2), rng.uniform(-1, 1)])[:, None] * t
    elif scenario == "correlated_noise":
        # Bounded, low-pass noise; no periodic oscillator.
        xy = np.zeros_like(noise)
        for j in range(1, len(t)):
            xy[:, j] = 0.85 * xy[:, j - 1] + noise[:, j]
    elif scenario == "noise_bursts":
        envelope = 0.2 + 1.8 * (np.sin(2 * np.pi * rng.uniform(0.5, 1.5) * t) > 0.8)
        xy = noise * envelope
    elif scenario == "intentional_oscillation":
        xy = np.array([speed * t, 4 * np.sin(2 * np.pi * 0.4 * t)])
        xy += rng.uniform(0.5, 6) * oscillation + noise
    elif scenario == "curved_path":
        angle = 2 * np.pi * rng.uniform(0.15, 0.7) * t
        radius = rng.uniform(12, 65)
        xy = radius * np.array([np.sin(angle), 0.65 * np.cos(angle)])
        xy += rng.uniform(0.2, 2) * oscillation + noise
    elif scenario == "deliberate_corrections":
        # Repeated target approaches with overshoot and deliberate backtracking.
        period = rng.uniform(0.6, 1.1)
        u = (t % period) / period
        correction = np.interp(u, [0, 0.45, 0.65, 0.85, 1], [0, 1.2, 0.8, 1, 1])
        progress = np.floor(t / period) + correction
        xy = np.array([speed * period * progress, 4 * np.sin(2 * np.pi * t / period)]) + noise
    elif scenario == "stop_start":
        period = rng.uniform(0.65, 1.2)
        u = (t % period) / period
        # Each 2-second window contains both motion and a pause.
        progress = np.floor(t / period) + np.minimum(u / 0.6, 1)
        xy = np.array([speed * period * progress, 3 * np.sin(progress)])
        xy += rng.uniform(0.1, 1) * oscillation + noise
    elif scenario == "low_amplitude":
        xy = rng.uniform(0.15, 1.2) * oscillation + noise
    elif scenario == "variable_frequency":
        f0, f1 = rng.uniform(3, 6), rng.uniform(10, 14)
        # Integrate instantaneous frequency, rather than multiplying f(t) by t.
        chirp_phase = 2 * np.pi * (f0 * t + (f1 - f0) * t ** 2 / (2 * t[-1]))
        xy = rng.uniform(0.5, 3) * np.sin(chirp_phase + phase) + noise
    elif scenario == "mixed_frequencies":
        low, high = rng.uniform(2, 3.5), rng.uniform(7, 13)
        xy = (rng.uniform(0.5, 2) * np.sin(2 * np.pi * low * t + phase)
              + rng.uniform(0.5, 2) * np.sin(2 * np.pi * high * t - phase) + noise)
    elif scenario == "amplitude_modulated":
        envelope = 0.2 + 1.8 * (0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(0.3, 1) * t))
        xy = envelope * oscillation + noise
    else:
        raise ValueError(f"Unknown stress scenario: {scenario}")
    if STRESS_SCENARIOS[scenario] == "tremor_like":
        xy += rng.uniform(-2, 2, (2, 1)) * t  # Small concurrent drift.
    angle = rng.uniform(0, 2 * np.pi)
    rotation = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    return rotation @ xy + rng.uniform(100, 800, (2, 1))


def generate_stress(path, groups=240, seed=2026):
    """New CSV, never overwrite; per-session RNG is stable when dataset size grows.

    scenario is audit metadata only: read_windows does not expose it as a feature.
    Same seed/index intentionally reproduces the same ID and path; different seeds
    and the original demo have disjoint namespaces.
    """
    if groups < len(STRESS_SCENARIOS) or seed < 0:
        raise ValueError("Use at least 12 stress groups and a non-negative seed")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(6 * HZ + 1) / HZ
    scenarios = list(STRESS_SCENARIOS)
    with path.open("x", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(("session_id", "group_id", "timestamp_s", "x", "y", "dpi_scale",
                         "label", "source", "scenario"))
        for i in range(groups):
            rng = np.random.default_rng(np.random.SeedSequence([seed, i]))
            scenario = scenarios[i % len(scenarios)]
            xy = _stress_path(scenario, t, rng)
            scale = float(rng.choice([1, 1.25, 1.5, 2]))
            session = f"stress-v1-{seed}-{i}"
            for j, timestamp in enumerate(t):
                writer.writerow((session, session, f"{timestamp:.8f}", f"{xy[0, j] * scale:.6f}",
                                 f"{xy[1, j] * scale:.6f}", scale, STRESS_SCENARIOS[scenario],
                                 "synthetic", scenario))
