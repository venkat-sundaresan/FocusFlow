"""Offline ML commands, including separate synthetic stress generation/evaluation."""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import sys
import time


def record(args):
    if sys.platform != "win32":
        raise ValueError("Live recording requires Windows; CSV training works on macOS/Linux too")
    if not math.isfinite(args.seconds) or args.seconds < 2 or not args.session.strip() or not args.group.strip():
        raise ValueError("Use >=2 seconds and non-empty session/group identifiers")
    from focusflow import winapi

    winapi.set_dpi_aware()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    print("Recording pointer coordinates. Pause FocusFlow assistance before recording raw movement.", file=sys.stderr)
    with path.open("x", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(("session_id", "group_id", "timestamp_s", "x", "y", "dpi_scale", "label", "source"))
        start = time.perf_counter()
        while True:
            x, y = winapi.get_cursor_pos()
            elapsed = time.perf_counter() - start
            writer.writerow((args.session, args.group, elapsed, x, y, winapi.dpi_scale_at(x, y), args.label, "recorded"))
            if elapsed >= args.seconds:
                break
            time.sleep(1 / 60)


def main(argv=None):
    parser = argparse.ArgumentParser(description="FocusFlow offline motion transfer learning")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("generate-demo", help="Generate clearly labeled synthetic CSV")
    demo.add_argument("--output", default="data/ml/demo.csv")
    demo.add_argument("--groups", type=int, default=90)
    demo.add_argument("--seed", type=int, default=42)
    stress = sub.add_parser("generate-stress", help="Generate a separate synthetic mixed-signal challenge CSV")
    stress.add_argument("--output", default="data/ml/stress-v1.csv")
    stress.add_argument("--groups", type=int, default=240)
    stress.add_argument("--seed", type=int, default=2026)
    evaluate = sub.add_parser("evaluate-stress", help="Compare original and stress-trained heads offline")
    evaluate.add_argument("--data", required=True)
    evaluate.add_argument("--reference-data", required=True, help="Exact original training CSV for baseline reconstruction")
    evaluate.add_argument("--model", default="artifacts/motion-demo")
    evaluate.add_argument("--output", default="artifacts/motion-stress-v1")
    evaluate.add_argument("--seed", type=int, default=42, help="Group split seed")
    train_parser = sub.add_parser("train", help="Train a head on frozen pretrained Chronos embeddings")
    train_parser.add_argument("--data", required=True)
    train_parser.add_argument("--output", default="artifacts/motion-demo")
    train_parser.add_argument("--seed", type=int, default=42)
    train_parser.add_argument("--base-model", default="amazon/chronos-t5-tiny")
    train_parser.add_argument("--revision", default="main", help="Optional Hugging Face commit SHA")
    train_parser.add_argument("--offline", action="store_true")
    pred = sub.add_parser("predict", help="Classify CSV windows with local saved weights")
    pred.add_argument("--data", required=True)
    pred.add_argument("--model", default="artifacts/motion-demo")
    pred.add_argument("--output", help="Write JSON to a new file instead of stdout")
    rec = sub.add_parser("record", help="Explicitly capture a labeled pointer session on Windows")
    rec.add_argument("--output", required=True)
    rec.add_argument("--session", required=True)
    rec.add_argument("--group", required=True, help="Stable anonymous participant ID for splitting")
    rec.add_argument("--label", required=True, choices=("steady", "intentional", "tremor_like"))
    rec.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args(argv)
    # Downloads are weights only. Pointer recordings never leave the machine.
    os.environ.setdefault("HF_HOME", str(Path.cwd() / ".cache" / "huggingface"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    try:
        if args.command == "record":
            record(args)
            return 0
        from .data import generate_demo, generate_stress, read_windows
        if args.command == "generate-demo":
            generate_demo(args.output, args.groups, args.seed)
            print(f"Synthetic data saved to {args.output}")
        elif args.command == "generate-stress":
            generate_stress(args.output, args.groups, args.seed)
            print(f"Synthetic stress data saved to {args.output}")
        elif args.command == "evaluate-stress":
            from .stress import evaluate_stress
            result = evaluate_stress(args.data, args.reference_data, args.model, args.output, args.seed)
            print(json.dumps({"output": args.output, "synthetic_only": True,
                              "validation": result["validation"], "challenge_test": result["challenge_test"]}, indent=2))
        elif args.command == "train":
            from .model import train
            result = train(read_windows(args.data), args.data, args.output, args.seed,
                           args.base_model, args.revision, args.offline)
            print(json.dumps(result, indent=2))
        else:
            from .model import MotionClassifier
            windows = read_windows(args.data, labeled=False)
            predictions = MotionClassifier(args.model).predict(windows.velocity)
            result = [dict(p, session_id=s, start_s=t) for p, s, t in
                      zip(predictions, windows.sessions, windows.starts)]
            payload = json.dumps(result, indent=2, allow_nan=False)
            if args.output:
                path = Path(args.output)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("x", encoding="utf-8") as f:
                    f.write(payload + "\n")
            else:
                print(payload)
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(2, f"Error: {exc}\nInstall optional dependencies with pip install -r requirements-ml.txt.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
