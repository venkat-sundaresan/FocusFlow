"""Live ML contracts: no native hooks, no network, and deterministic policy clocks."""
import ast
import importlib.util
import math
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from focusflow import profiles
from focusflow.config import Settings
from focusflow.dwell import Dwell
from focusflow.motion import MotionAnalyzer
from focusflow.pointer import PointerPipeline, MissTracker
from focusflow.ml.live import LiveML, SmoothingPolicy, prepare_window

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
if HAS_NUMPY:
    import numpy as np


def samples(end=10, scale=1, hz=125):
    return tuple((end - 2.1 + i / hz, scale * (20 + i / hz), scale * 40, scale)
                 for i in range(int(2.1 * hz) + 1))


def prediction(label="tremor_like"):
    return {"label": label, "scores": {c: .9 if c == label else .05
                                       for c in ("steady", "intentional", "tremor_like")}}


def view(end, label="tremor_like", score=.9, mode="experimental"):
    return {"mode": mode, "state": "ready", "prediction": {
        "window_end": end, "label": label, "tremor_score": score, "latency_ms": 10}}


class CapturePolicyTests(unittest.TestCase):
    def test_disabled_worker_does_not_load_dependencies_or_start_thread(self):
        factory = mock.Mock()
        worker = LiveML(factory=factory)
        worker.configure(False)
        worker.submit(samples())
        factory.assert_not_called()
        self.assertIsNone(worker.thread)
        self.assertEqual(worker.jobs.qsize(), 0)
        worker.stop()

    def test_invalid_path_or_thread_failure_does_not_raise_into_application(self):
        worker = LiveML(factory=mock.Mock())
        worker.configure(True, path=object())
        self.assertEqual(worker.view()["state"], "error")
        self.assertFalse(worker.enabled)
        with mock.patch("threading.Thread.start", side_effect=RuntimeError("thread unavailable")):
            worker.configure(True)
        self.assertEqual(worker.view()["state"], "error")
        self.assertIsNone(worker.thread)
        worker.stop()

    def test_capture_is_opt_in_bounded_and_does_not_change_pointer_output(self):
        a, b = PointerPipeline(), PointerPipeline()
        a.configure(True, 1)
        b.configure(True, 1)
        b.enable_ml_capture(True)
        for i in range(10000):
            t = 1000 + i
            x, y = (a.cursor[0] if a.cursor else 100) + .2 * math.sin(i / 10), 100
            self.assertEqual(a.on_move(x, y, t), b.on_move(x, y, t))
            self.assertEqual(a.cursor, b.cursor)
        self.assertEqual(len(a.ml_raw), 0)
        self.assertLessEqual(len(b.ml_raw), 1024)
        self.assertGreater(len(b.ml_raw), 100)
        b.tick(11004)
        self.assertEqual(b.ml_raw[-1][1:3], b.raw)

    def test_idle_heartbeat_and_dpi_change(self):
        pipe = PointerPipeline()
        pipe.enable_ml_capture(True)
        pipe.on_move(30, 40, 1000)
        for i in range(1, 270):
            pipe.tick(1000 + i * 8)
        epoch, points = pipe.ml_snapshot()
        self.assertGreater(points[-1][0] - points[0][0], 2)
        self.assertEqual(points[-1][1:3], (30., 40.))
        pipe.set_scale(1.5)
        self.assertGreater(pipe.ml_epoch, epoch)
        self.assertFalse(pipe.ml_raw)

    def test_lease_expiry_fast_motion_and_disabled_smoothing(self):
        pipe = PointerPipeline()
        pipe.configure(True, 1)
        pipe.set_ml_adjustment(100, 100)
        self.assertEqual(pipe.ml_extra, 2)
        pipe.tick(101)
        self.assertEqual(pipe.filter.cutoff_scale, 1)
        pipe.set_ml_adjustment(2, 10000)
        pipe.speed = 300
        pipe.tick(102)
        self.assertEqual(pipe.ml_extra, 0)
        pipe.configure(False, 1)
        pipe.set_ml_adjustment(2, 10000)
        self.assertEqual(pipe.ml_extra, 0)

    def test_distinct_sustained_predictions_and_hysteresis(self):
        policy = SmoothingPolicy()
        for i in range(50):
            self.assertEqual(policy.update(10 + i / 100, view(10), True), 0)
        self.assertEqual(policy.high_count, 1)
        policy.update(10.5, view(10.5), True)
        self.assertFalse(policy.latched)
        policy.update(11, view(11), True)
        self.assertTrue(policy.latched)
        for i in range(1000):
            t = 11 + i / 60
            policy.update(t, view(t), True)
        self.assertEqual(policy.extra, 2)
        policy.update(t + .01, view(t + .01, score=.7), True)
        self.assertTrue(policy.latched)
        policy.update(t + .02, view(t + .02, score=.4), True)
        self.assertFalse(policy.latched)
        self.assertLess(policy.extra, 2)

    def test_shadow_intentional_stale_failure_and_priority_reset(self):
        for state in (view(10, mode="shadow"), view(10, "intentional"), view(8),
                      {"mode": "experimental", "state": "error", "prediction": None}):
            policy = SmoothingPolicy()
            policy.extra = 2
            self.assertEqual(policy.update(10, state, True), 0)
        policy = SmoothingPolicy()
        policy.extra = 2
        self.assertEqual(policy.update(10, view(10), False), 0)
        policy.extra = 2
        self.assertEqual(policy.update(10, view(10), True, intentional=True), 0)


@unittest.skipUnless(HAS_NUMPY, "requires optional NumPy")
class WindowWorkerTests(unittest.TestCase):
    def worker(self, classifier=None, clock=lambda: 10):
        classifier = classifier or SimpleNamespace(predict=lambda x: [prediction()])
        worker = LiveML(factory=lambda path: classifier, clock=clock)
        self.addCleanup(worker.stop)
        worker.configure(True)
        self.wait(worker, "warming")
        return worker

    def wait(self, worker, state):
        deadline = time.perf_counter() + 3
        while time.perf_counter() < deadline:
            if worker.view()["state"] == state:
                return
            time.sleep(.005)
        self.fail(f"Expected {state}, got {worker.view()}")

    def test_window_matches_offline_preprocessing_and_seven_features(self):
        from focusflow.ml.data import resample_velocity, read_windows, generate_demo
        from focusflow.ml.model import motion_features
        rows = samples(10, scale=1.5)
        v = prepare_window(rows)
        self.assertEqual(v.shape, (1, 2, 120))
        np.testing.assert_allclose(v[0, 0], 1, atol=1e-5)
        np.testing.assert_allclose(v[0, 1], 0, atol=1e-5)
        expected = resample_velocity([p[:3] for p in rows], rows[-1][0] - 2, 1.5)
        np.testing.assert_array_equal(v[0], expected.astype(np.float32))
        self.assertEqual(motion_features(v).shape, (1, 7))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.csv"
            generate_demo(path, groups=12)
            self.assertEqual(read_windows(path).velocity.shape, (36, 2, 120))

    def test_incomplete_gaps_and_scale_mixing_rejected(self):
        for rows in (samples()[:40], samples()[:30] + samples()[70:],
                     samples()[:-1] + ((10, 30, 40, 2),)):
            with self.assertRaises(ValueError):
                prepare_window(rows)

    def test_inference_runs_only_in_background_and_results_expire(self):
        threads = []
        def predict(v):
            threads.append(threading.current_thread().name)
            return [prediction()]
        worker = self.worker(SimpleNamespace(predict=predict))
        worker.submit(samples())
        self.wait(worker, "ready")
        self.assertEqual(threads, ["ff-ml"])
        self.assertEqual(worker.view()["prediction"]["label"], "tremor_like")
        self.assertEqual(worker.view(12)["state"], "stale")
        self.assertIsNone(worker.view(12)["prediction"])

    def test_latency_budget_rejects_result(self):
        now = [10.]
        def predict(v):
            now[0] += .3
            return [prediction()]
        worker = self.worker(SimpleNamespace(predict=predict), clock=lambda: now[0])
        worker.submit(samples())
        self.wait(worker, "slow")
        self.assertIsNone(worker.view()["prediction"])
        self.assertGreater(worker.view()["latency_ms"], 250)

    def test_hung_inference_is_unavailable_before_it_returns(self):
        entered, release = threading.Event(), threading.Event()
        now = [10.]
        def predict(v):
            entered.set()
            release.wait(2)
            return [prediction()]
        worker = self.worker(SimpleNamespace(predict=predict), clock=lambda: now[0])
        self.addCleanup(release.set)
        worker.submit(samples())
        self.assertTrue(entered.wait(2))
        now[0] = 10.3
        self.assertEqual(worker.view()["state"], "slow")
        self.assertIsNone(worker.view()["prediction"])
        release.set()

    def test_disable_rejects_inflight_result_and_queue_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        def predict(v):
            entered.set()
            release.wait(2)
            return [prediction()]
        worker = self.worker(SimpleNamespace(predict=predict))
        self.addCleanup(release.set)
        worker.submit(samples())
        self.assertTrue(entered.wait(2))
        for _ in range(40):
            worker.submit(samples())
        self.assertEqual(worker.jobs.qsize(), 1)
        self.assertGreater(worker.dropped, 0)
        worker.configure(False)
        release.set()
        time.sleep(.02)
        self.assertEqual(worker.view()["state"], "off")
        self.assertIsNone(worker.view()["prediction"])
        self.assertEqual(worker.jobs.qsize(), 0)

    def test_missing_model_and_invalid_prediction_fail_closed(self):
        worker = LiveML(factory=mock.Mock(side_effect=FileNotFoundError("model unavailable")), clock=lambda: 10)
        self.addCleanup(worker.stop)
        worker.configure(True)
        self.wait(worker, "error")
        self.assertIn("FileNotFoundError", worker.view()["error"])
        bad = self.worker(SimpleNamespace(predict=lambda v: [{"label": "unknown", "scores": {}}]))
        bad.submit(samples())
        self.wait(bad, "error")
        self.assertIsNone(bad.view()["prediction"])

    @unittest.skipUnless(importlib.util.find_spec("torch") is not None, "requires optional Torch")
    def test_real_stress_model_matches_offline_prediction(self):
        from focusflow.ml.live import DEFAULT_MODEL, load_classifier
        if not (DEFAULT_MODEL / "classifier.json").exists():
            self.skipTest("stress-trained artifact not present")
        classifier = load_classifier(DEFAULT_MODEL)
        rows = samples()
        expected = classifier.predict(prepare_window(rows))[0]
        worker = self.worker(classifier)
        worker.submit(rows)
        self.wait(worker, "ready")
        actual = worker.view()["prediction"]
        self.assertEqual(actual["label"], expected["label"])
        self.assertAlmostEqual(actual["tremor_score"], expected["scores"]["tremor_like"])


class EngineMLTests(unittest.TestCase):
    """Actual Engine class isolated from Win32 imports; native input is never simulated as verified."""
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "focusflow/engine.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Engine")
        self.ml = mock.Mock()
        self.ml.view.return_value = view(time.perf_counter())
        namespace = dict(threading=threading, math=math, time=time, profiles=profiles, winapi=mock.Mock(),
                         MotionAnalyzer=MotionAnalyzer, Dwell=Dwell, UiaWorker=mock.Mock(),
                         PointerPipeline=PointerPipeline, PointerThread=mock.Mock(), ExtState=mock.Mock(),
                         MissTracker=MissTracker, LiveML=lambda: self.ml, SmoothingPolicy=SmoothingPolicy,
                         FLUSH_S=60., INTERVAL_S=.5)
        namespace["winapi"].virtual_bounds.return_value = (0, 0, 2000, 1000)
        namespace["winapi"].get_cursor_pos.return_value = (100, 100)
        exec(compile(ast.Module(body=[node], type_ignores=[]), "focusflow/engine.py", "exec"), namespace)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Settings(Path(self.tmp.name) / "settings.json")
        self.engine = namespace["Engine"](self.cfg, mock.Mock(), mock.Mock())
        self.addCleanup(self.engine.stop)
        self.motion = SimpleNamespace(intentional=False)

    def advance(self, experimental=True):
        self.cfg.set("ml_enabled", True)
        self.cfg.set("ml_mode", "experimental" if experimental else "shadow")
        for i in range(180):
            now = 10 + i / 60
            self.ml.view.return_value = view(10 + int(i / 30) * .5,
                                             mode="experimental" if experimental else "shadow")
            self.engine._ml_tick(now, self.motion)

    def test_default_off_and_shadow_preserves_cutoff(self):
        self.assertFalse(self.cfg["ml_enabled"])
        self.assertEqual(self.cfg["ml_mode"], "shadow")
        cutoff = self.engine.pipe.filter.cutoff_scale
        self.advance(False)
        self.assertEqual(self.engine.pipe.filter.cutoff_scale, cutoff)
        self.assertEqual(self.engine.pipe.ml_extra, 0)

    def test_experimental_and_manual_override(self):
        self.advance()
        self.assertGreater(self.engine.pipe.ml_extra, 0)
        self.assertEqual(self.cfg["smoothing_strength"], 5)
        self.cfg.set("smoothing_strength", 3)
        self.assertTrue(self.engine._ml_manual)
        self.engine._ml_tick(13, self.motion)
        self.assertEqual(self.engine.pipe.ml_extra, 0)
        self.assertEqual(self.engine.pipe.filter.cutoff_scale, profiles.cutoff_scale(3))

    def test_emergency_stop_wins_over_result_and_capture(self):
        self.advance()
        self.engine.set_enabled(False)
        self.engine._ml_tick(13, self.motion)
        self.assertFalse(self.engine.pipe.owns_cursor)
        self.assertFalse(self.engine.pipe.ml_capture)
        self.assertEqual(self.engine.pipe.ml_extra, 0)
        self.assertFalse(self.engine.active)
        self.engine.set_enabled(True)
        self.engine._ml_tick(13.1, self.motion)
        self.assertTrue(self.engine._ml_manual)
        self.assertEqual(self.engine.pipe.ml_extra, 0)

    def test_failure_dragging_and_experiments_restore_baseline(self):
        for reason in ("error", "stale", "slow"):
            self.advance()
            self.ml.view.return_value = {"mode": "experimental", "state": reason, "prediction": None}
            self.engine._ml_tick(13, self.motion)
            self.assertEqual(self.engine.pipe.ml_extra, 0)
        self.advance()
        self.engine._ml_tick(13, self.motion, dragging=True)
        self.assertEqual(self.engine.pipe.ml_extra, 0)
        self.advance()
        self.engine.session = object()
        self.engine._ml_tick(13, self.motion)
        self.assertEqual(self.engine.pipe.ml_extra, 0)

    def test_maximum_strength_and_suspension_do_not_allow_extra(self):
        self.cfg.set("smoothing_strength", 10)
        self.advance()
        self.assertEqual(self.engine.pipe.ml_extra, 0)
        self.engine.set_suspended(True)
        self.engine._ml_tick(13, self.motion)
        self.assertFalse(self.engine.pipe.ml_capture)
        self.assertFalse(self.engine.pipe.owns_cursor)


if __name__ == "__main__":
    unittest.main()
