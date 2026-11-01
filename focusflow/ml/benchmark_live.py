"""Local worker-only benchmark; never controls the mouse or writes raw trajectories.

python -m focusflow.ml.benchmark_live --output artifacts/live-ml-benchmark.json
"""
import argparse
import json
import math
import os
import platform
import statistics
import time
from pathlib import Path

from .live import DEFAULT_MODEL, INTERVAL_S, LiveML


def benchmark(model=DEFAULT_MODEL, count=30):
    if count < 5:
        raise ValueError("Use at least five measured windows")
    worker = LiveML()
    started = time.perf_counter()
    worker.configure(True, "shadow", str(model))
    try:
        deadline = started + 60
        while worker.view()["state"] == "loading" and time.perf_counter() < deadline:
            time.sleep(.01)
        state = worker.view()
        if state["state"] != "warming":
            raise RuntimeError(f"Model did not load: {state}")
        load_s = time.perf_counter() - started
        latencies, ages, labels = [], [], []
        failures = []
        cpu_start = wall_start = None
        for i in range(count + 3):
            if i == 3:
                cpu_start, wall_start = time.process_time(), time.perf_counter()
            end = time.perf_counter()
            # 125 Hz acquisition, mixed drift/oscillation. Data remains in RAM.
            rows = tuple((end - 2.1 + j / 125, 400 + 2 * math.sin(2 * math.pi * 8 * j / 125),
                          300 + .5 * math.sin(2 * math.pi * 6 * j / 125), 1.) for j in range(264))
            # Last sample is the window end (rounding never creates a future timestamp).
            rows = tuple((t - (rows[-1][0] - end), x, y, s) for t, x, y, s in rows)
            worker.submit(rows)
            deadline = end + INTERVAL_S
            seen = False
            while time.perf_counter() < deadline:
                state = worker.view()
                p = state["prediction"]
                if p and p["window_end"] == end:
                    if i >= 3:
                        latencies.append(p["latency_ms"])
                        ages.append((time.perf_counter() - end) * 1000)
                        labels.append(p["label"])
                    seen = True
                    break
                if state["state"] == "error":
                    raise RuntimeError(state["error"])
                time.sleep(.002)
            if i >= 3 and not seen:
                failures.append({"window": i - 3, "state": state["state"], "latency_ms": state["latency_ms"]})
            time.sleep(max(0., deadline - time.perf_counter()))
        cpu_s, wall_s = time.process_time() - cpu_start, time.perf_counter() - wall_start
        def summary(values):
            if not values:
                return None
            ordered = sorted(values)
            return {"p50_ms": statistics.median(values), "p95_ms": ordered[math.ceil(.95 * len(values)) - 1],
                    "max_ms": max(values)}
        return {"scope": "worker-only on synthetic in-memory trajectories; excludes Windows hook/UI/engine",
                "platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
                "logical_cpus": os.cpu_count(), "torch_threads": 1, "model": str(model), "load_seconds": load_s,
                "warmup_windows": 3, "measured_windows": count, "accepted_windows": len(latencies),
                "cadence_hz": 1 / INTERVAL_S, "inference": summary(latencies), "observed_end_to_end": summary(ages),
                "wall_seconds": wall_s, "process_cpu_seconds": cpu_s,
                "cpu_percent_one_core": 100 * cpu_s / wall_s, "dropped_pending_jobs": worker.dropped,
                "rejected_windows": failures, "labels": {label: labels.count(label) for label in set(labels)}}
    finally:
        worker.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--windows", type=int, default=30)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    path = Path(args.output)
    if path.exists():
        parser.error("Output already exists; choose a new path")
    result = benchmark(args.model, args.windows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2, allow_nan=False)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
