"""Portable startup failure injection plus explicitly Windows-only native mutex probes."""
import ast
import ctypes
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from focusflow import startup

ROOT = Path(__file__).resolve().parents[1]


def source_definition(path, name, namespace):
    """Execute an actual definition in isolation; never emulate the Win32 platform globally."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


class StartupLoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"FOCUSFLOW_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def log(self):
        return (self.home / "focusflow.log").read_text(encoding="utf-8")

    def test_import_failure_is_logged_and_visible_with_nonzero_status(self):
        console = io.StringIO()
        with mock.patch.object(startup, "_launch", side_effect=ModuleNotFoundError("missing pystray")), \
                mock.patch.object(sys, "stderr", console):
            self.assertEqual(startup.main([]), 1)
            self.assertIs(sys.stderr, console)
        for output in (console.getvalue(), self.log()):
            self.assertIn("missing pystray", output)
            self.assertIn("Traceback", output)
        self.assertIn("executable=", self.log())

    def test_pythonw_without_console_still_logs_failure(self):
        with mock.patch.object(startup, "_launch", side_effect=RuntimeError("Tk creation failed")), \
                mock.patch.object(sys, "stderr", None), mock.patch.object(sys, "stdout", None), \
                mock.patch.object(sys, "platform", "test"):
            self.assertEqual(startup.main([]), 1)
            self.assertIsNone(sys.stderr)
            self.assertIsNone(sys.stdout)
        self.assertIn("Tk creation failed", self.log())

    def test_worker_traceback_is_logged(self):
        def launch(args):
            def fail():
                raise RuntimeError("worker initialization failed")
            thread = threading.Thread(target=fail, name="test-worker")
            thread.start()
            thread.join()
            return 0
        original = threading.excepthook
        with mock.patch.object(startup, "_launch", side_effect=launch), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(startup.main([]), 0)
        self.assertIs(threading.excepthook, original)
        self.assertIn("test-worker", self.log())
        self.assertIn("worker initialization failed", self.log())

    def test_existing_log_is_appended_and_show_is_forwarded(self):
        (self.home / "focusflow.log").write_text("previous session\n")
        with mock.patch.object(startup, "_launch", return_value=2) as launch, \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(startup.main(["--show"]), 2)
        self.assertTrue(launch.call_args.args[0].show)
        self.assertTrue(self.log().startswith("previous session\n"))
        self.assertIn("EXIT code=2", self.log())

    def test_unwritable_preferred_location_uses_temp_fallback(self):
        blocked = self.home / "file-not-directory"
        blocked.write_text("keep")
        with mock.patch.dict(os.environ, {"FOCUSFLOW_HOME": str(blocked)}), \
                mock.patch.object(startup.tempfile, "gettempdir", return_value=str(self.home)):
            path, stream, warnings = startup.open_log()
        stream.close()
        self.assertEqual(path, self.home / "FocusFlow" / "focusflow.log")
        self.assertTrue(warnings)
        self.assertEqual(blocked.read_text(), "keep")

    def test_dependency_import_error_keeps_original_exception(self):
        with mock.patch.object(startup.importlib.metadata, "version", return_value="test"), \
                mock.patch.object(startup.importlib, "import_module", side_effect=ImportError("native DLL missing")):
            with self.assertRaisesRegex(RuntimeError, "comtypes") as caught:
                startup.check_dependencies()
        self.assertIn("native DLL missing", str(caught.exception.__cause__))

    @unittest.skipIf(sys.platform == "win32", "non-Windows entrypoint check")
    def test_real_entrypoints_reject_non_windows_with_persistent_diagnostic(self):
        for args in (["-m", "focusflow"], [str(ROOT / "focusflow.pyw")]):
            result = subprocess.run([sys.executable, *args], cwd=ROOT, env=os.environ.copy(),
                                    text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 1)
            self.assertIn("requires Windows", result.stderr)
        self.assertIn("requires Windows", self.log())


class MutexContractTests(unittest.TestCase):
    def setUp(self):
        self.error = 183
        def set_error(value):
            self.error = value
        self.ctypes = mock.Mock()
        self.ctypes.set_last_error.side_effect = set_error
        self.ctypes.get_last_error.side_effect = lambda: self.error
        self.ctypes.WinError.side_effect = lambda code: OSError(code, "simulated Win32 error")
        self.kernel = mock.Mock()
        self.single_instance = source_definition("focusflow/winapi.py", "single_instance",
                                                 {"ctypes": self.ctypes, "kernel32": self.kernel})

    def test_new_handle_clears_stale_error(self):
        self.kernel.CreateMutexW.return_value = 0x100000001
        self.assertEqual(self.single_instance(), (0x100000001, True))

    def test_existing_mutex_is_not_mistaken_for_a_new_instance(self):
        def existing(*args):
            self.error = 183
            return 123
        self.kernel.CreateMutexW.side_effect = existing
        self.assertEqual(self.single_instance(), (123, False))

    def test_null_handle_raises_access_denied(self):
        def denied(*args):
            self.error = 5
            return None
        self.kernel.CreateMutexW.side_effect = denied
        with self.assertRaises(OSError) as caught:
            self.single_instance()
        self.assertEqual(caught.exception.errno, 5)


class ApplicationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.winapi = mock.Mock()
        self.namespace = {"LOG": logging.getLogger("focusflow"), "APP_DIR": Path(self.tmp.name),
                          "sys": sys, "winapi": self.winapi, "threading": threading}
        self.App = source_definition("focusflow/app.py", "App", self.namespace)
        self.app = self.App.__new__(self.App)
        self.app._shutting_down = False
        for name in ("engine", "store", "ui", "hook", "hotkeys", "switch", "server", "tray", "voice"):
            setattr(self.app, name, mock.Mock())
        self.app.server.port = 47321
        self.app.cfg = {"show_window_on_start": True, "osk_enabled": False, "voice_enabled": False,
                        "switch_enabled": False, "autostart_asked": True}
        self.app._start_tray = mock.Mock()
        self.app._bind_hotkeys = mock.Mock()

    def test_tray_failure_never_enables_assistance_and_cleans_up(self):
        self.app._start_tray.side_effect = RuntimeError("tray failed")
        with self.assertRaisesRegex(RuntimeError, "tray failed"):
            self.app.run()
        self.app.engine.start.assert_not_called()
        self.app.hook.start.assert_not_called()
        self.assertNotIn(mock.call(False), self.app.engine.set_suspended.call_args_list)
        self.app.server.stop.assert_called_once()
        self.app.hook.stop.assert_called_once()
        self.app.ui.root.destroy.assert_called_once()

    def test_hook_failure_never_starts_engine(self):
        self.app.hook.wait_ready.side_effect = RuntimeError("hook failed")
        with self.assertRaisesRegex(RuntimeError, "hook failed"):
            self.app.run()
        self.app.engine.start.assert_not_called()
        self.app.hook.stop.assert_called_once()

    def test_main_window_failure_is_not_swallowed_by_ui_queue(self):
        self.app.ui.main.open.side_effect = RuntimeError("bad Tk widget")
        with self.assertRaisesRegex(RuntimeError, "bad Tk widget"):
            self.app.run()
        self.app._start_tray.assert_not_called()
        self.app.hook.start.assert_not_called()

    def test_normal_lifecycle_and_show_override(self):
        self.app.cfg["show_window_on_start"] = False
        self.app.run(show=True)
        self.app.ui.main.open.assert_called_once()
        self.app.engine.start.assert_called_once()
        self.assertEqual(self.app.engine.set_suspended.call_args_list,
                         [mock.call(True), mock.call(False), mock.call(True)])
        self.app.ui.run.assert_called_once()
        self.app.ui.root.destroy.assert_called_once()
        self.app._shutdown()
        self.app.server.stop.assert_called_once()

    def test_duplicate_exit_and_exception_close_mutex(self):
        factory = mock.Mock()
        self.namespace["App"] = factory
        main = source_definition("focusflow/app.py", "main", self.namespace)
        with mock.patch.object(sys, "argv", ["focusflow"]), mock.patch.object(sys, "stderr", io.StringIO()):
            self.winapi.single_instance.return_value = (123, False)
            self.assertEqual(main(), 2)
            factory.assert_not_called()
            self.winapi.kernel32.CloseHandle.assert_called_with(123)
            self.winapi.single_instance.return_value = (456, True)
            factory.side_effect = RuntimeError("Tk initialization failed")
            with self.assertRaisesRegex(RuntimeError, "Tk initialization failed"):
                main()
            self.winapi.kernel32.CloseHandle.assert_called_with(456)


class NativeWorkerFailureTests(unittest.TestCase):
    """Exercise real worker control flow with native calls mocked, never install a hook."""
    def setUp(self):
        self.user, self.kernel, self.ffi = mock.Mock(), mock.Mock(), mock.Mock()
        self.ffi.WinError.side_effect = lambda code: OSError(code, "native failure")
        self.ffi.get_last_error.return_value = 5
        self.ns = {"user32": self.user, "kernel32": self.kernel, "ctypes": self.ffi,
                   "threading": threading, "logging": logging, "w": mock.Mock(),
                   "WH_MOUSE_LL": 14, "WM_HOTKEY": 0x312, "WM_QUIT": 0x12,
                   "_HOOKPROC": lambda fn: fn, "parse_hotkey": lambda spec: (3, 70)}

    def test_hook_install_failure_is_reported_to_launcher(self):
        Hook = source_definition("focusflow/winapi.py", "MouseHook", self.ns)
        hook = Hook(lambda *args: False)
        self.user.SetWindowsHookExW.return_value = None
        with self.assertLogs("focusflow", level="ERROR"):
            hook.run()
        with self.assertRaisesRegex(RuntimeError, "installation failed"):
            hook.wait_ready(0)
        self.user.GetMessageW.assert_not_called()

    def test_hook_is_uninstalled_when_message_loop_fails(self):
        Hook = source_definition("focusflow/winapi.py", "MouseHook", self.ns)
        hook = Hook(lambda *args: False)
        self.user.SetWindowsHookExW.return_value = 123
        self.user.GetMessageW.return_value = -1
        with self.assertLogs("focusflow", level="ERROR"):
            hook.run()
        self.user.UnhookWindowsHookEx.assert_called_once_with(123)
        self.assertIsNone(hook._hook)
        with self.assertRaises(RuntimeError):
            hook.wait_ready(0)

    def test_hook_initialization_timeout_is_explicit(self):
        Hook = source_definition("focusflow/winapi.py", "MouseHook", self.ns)
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            Hook(lambda *args: False).wait_ready(0)

    def test_hotkey_registration_failure_is_exposed(self):
        Hotkeys = source_definition("focusflow/winapi.py", "HotkeyThread", self.ns)
        self.user.RegisterHotKey.return_value = 0
        self.user.GetMessageW.return_value = 0
        worker = Hotkeys([("ctrl+alt+shift+f", lambda: None)])
        worker.run()
        worker.wait_ready(0)
        self.assertEqual(worker.failed, ["ctrl+alt+shift+f"])

    def test_hotkeys_are_unregistered_on_message_loop_failure(self):
        Hotkeys = source_definition("focusflow/winapi.py", "HotkeyThread", self.ns)
        self.user.RegisterHotKey.return_value = 1
        self.user.GetMessageW.return_value = -1
        worker = Hotkeys([("ctrl+alt+shift+f", lambda: None)])
        with self.assertLogs("focusflow", level="ERROR"):
            worker.run()
        self.user.UnregisterHotKey.assert_called_once_with(None, 1)
        with self.assertRaisesRegex(RuntimeError, "initialization failed"):
            worker.wait_ready(0)


@unittest.skipUnless(sys.platform == "win32", "requires real Windows kernel32")
class WindowsMutexTests(unittest.TestCase):
    def test_real_unique_duplicate_and_released_mutex(self):
        from focusflow import winapi
        name = "Local\\FocusFlowTest-" + uuid.uuid4().hex
        handles = []
        try:
            ctypes.set_last_error(183)
            first, fresh = winapi.single_instance(name)
            handles.append(first)
            self.assertTrue(fresh)
            second, fresh = winapi.single_instance(name)
            handles.append(second)
            self.assertFalse(fresh)
        finally:
            for handle in handles:
                winapi.kernel32.CloseHandle(handle)
        handle, fresh = winapi.single_instance(name)
        try:
            self.assertTrue(fresh)
        finally:
            winapi.kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
