"""Standard-library-only entry point: diagnostics must precede Win32/Tk/app imports."""
import argparse
import ctypes
import faulthandler
import importlib
import importlib.metadata
import logging
import os
from pathlib import Path
import platform
import struct
import sys
import tempfile
import threading
import traceback
import uuid

LOG = logging.getLogger("focusflow")


class Tee:
    """Keep PowerShell errors visible while also preserving pythonw tracebacks."""
    def __init__(self, console, log):
        self.console, self.log = console, log

    def write(self, text):
        self.log.write(text)
        self.log.flush()
        if self.console is not None:
            try:
                self.console.write(text)
                self.console.flush()
            except (OSError, ValueError):
                pass  # A detached console must not lose the persistent traceback.
        return len(text)

    def flush(self):
        self.log.flush()
        if self.console is not None:
            try:
                self.console.flush()
            except (OSError, ValueError):
                pass

    @property
    def encoding(self):
        return getattr(self.console, "encoding", None) or self.log.encoding

    def isatty(self):
        return bool(self.console is not None and getattr(self.console, "isatty", lambda: False)())

    def fileno(self):
        return self.log.fileno()


def open_log():
    preferred = Path(os.environ["FOCUSFLOW_HOME"]) if os.environ.get("FOCUSFLOW_HOME") else (
        Path(os.environ.get("APPDATA") or Path.home()) / "FocusFlow")
    errors = []
    for directory in (preferred, Path(tempfile.gettempdir()) / "FocusFlow"):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "focusflow.log"
            stream = path.open("a", buffering=1, encoding="utf-8", errors="backslashreplace")
            return path, stream, errors
        except OSError as exc:
            errors.append(f"Cannot write startup log in {directory}: {exc}")
    raise OSError("; ".join(errors))


def check_dependencies():
    """Import the actual native modules, not just their distribution metadata."""
    for distribution, module in (("comtypes", "comtypes"), ("pystray", "pystray"), ("Pillow", "PIL.Image")):
        LOG.info("Checking dependency %s", distribution)
        try:
            version = importlib.metadata.version(distribution)
            importlib.import_module(module)
        except Exception as exc:
            raise RuntimeError(f"Cannot load {distribution} using {sys.executable}. "
                               "Run this interpreter with -m pip install --upgrade -r requirements.txt") from exc
        LOG.info("Dependency %s=%s loaded", distribution, version)
    import tkinter
    LOG.info("Tkinter import OK; Tcl=%s Tk=%s", tkinter.TclVersion, tkinter.TkVersion)


def diagnose():
    """Real Windows probes without mouse injection, hooks, settings or DB changes."""
    from . import winapi
    from .uia import UiaWorker
    import tkinter

    name = "Local\\FocusFlowDiagnostic-" + uuid.uuid4().hex
    handles = []
    try:
        # Exercise a stale incoming error as well as an actual duplicate handle.
        ctypes.set_last_error(183)
        first, is_first = winapi.single_instance(name)
        handles.append(first)
        second, is_second_first = winapi.single_instance(name)
        handles.append(second)
        if not is_first or is_second_first:
            raise RuntimeError("Unique/duplicate mutex diagnostic failed")
        LOG.info("Mutex diagnostic passed: fresh=True, duplicate=False")
    finally:
        for handle in handles:
            winapi.kernel32.CloseHandle(handle)
    handle, first = winapi.single_instance()
    winapi.kernel32.CloseHandle(handle)
    LOG.info("Application mutex available=%s (an existing instance is allowed during diagnostics)", first)
    winapi.set_dpi_aware()
    LOG.info("Creating diagnostic Tk root")
    root = tkinter.Tk()
    try:
        root.withdraw()
        root.update_idletasks()
        LOG.info("Tk initialized; patchlevel=%s", root.tk.call("info", "patchlevel"))
    finally:
        root.destroy()
    LOG.info("Win32 cursor read=%s; desktop bounds=%s", winapi.get_cursor_pos(), winapi.virtual_bounds())
    worker = UiaWorker()
    worker.start()
    if not worker.wait_ready(15):
        raise RuntimeError("UI Automation initialization failed or timed out; see its traceback in the log")
    print("Diagnostics passed: imports, Win32 mutex, Tk and UI Automation. "
          "Tray visibility and pointer assistance still require a normal interactive launch.")
    LOG.info("Diagnostics completed; no input hooks or synthetic mouse events were used")
    return 0


def _launch(args):
    if sys.platform != "win32":
        raise RuntimeError("The FocusFlow desktop app requires Windows 10/11. The ML commands are cross-platform.")
    check_dependencies()
    LOG.info("Importing application")
    from .app import main as app_main
    if args.diagnose:
        return diagnose()
    if args.selftest:
        from .app import selftest
        selftest()
        return 0
    return app_main(show=args.show)


def main(argv=None):
    parser = argparse.ArgumentParser(description="FocusFlow Windows pointer assistant")
    parser.add_argument("--diagnose", action="store_true", help="Check native startup components without pointer assistance")
    parser.add_argument("--selftest", action="store_true", help="Check dependencies and application imports")
    parser.add_argument("--show", action="store_true", help="Open the main window even if configured to start hidden")
    args = parser.parse_args(argv)
    old_stdout, old_stderr = sys.stdout, sys.stderr
    old_thread_hook = threading.excepthook
    try:
        path, stream, warnings = open_log()
    except OSError:
        if sys.stderr is not None:
            traceback.print_exc()
        return 1
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    old_level, old_propagate = LOG.level, LOG.propagate
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    LOG.addHandler(handler)
    sys.stderr = Tee(old_stderr, stream)
    if sys.stdout is None:
        sys.stdout = stream
    fault_logging = False
    try:
        faulthandler.enable(file=stream, all_threads=True)
        fault_logging = True
        def thread_exception(args):
            LOG.error("Unhandled worker exception: %s", args.thread.name if args.thread else "unknown",
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
            if old_stderr is not None:
                print("FocusFlow worker failed; see " + str(path), file=old_stderr)
        threading.excepthook = thread_exception
        print(f"FocusFlow startup log: {path}", file=sys.stderr)
        LOG.info("START pid=%s executable=%s python=%s platform=%s bits=%s cwd=%s source=%s",
                 os.getpid(), sys.executable, sys.version.replace("\n", " "), platform.platform(),
                 struct.calcsize("P") * 8, Path.cwd(), Path(__file__).resolve())
        for warning in warnings:
            LOG.warning(warning)
        code = _launch(args) or 0
        LOG.info("EXIT code=%s", code)
        return code
    except KeyboardInterrupt:
        LOG.info("Interrupted by user")
        return 130
    except Exception:
        LOG.error("STARTUP/RUNTIME FAILURE")
        traceback.print_exc(file=sys.stderr)
        print(f"FocusFlow failed. Full details: {path}", file=sys.stderr)
        if old_stderr is None and sys.platform == "win32":
            try:
                ctypes.windll.user32.MessageBoxW(None, f"FocusFlow could not start.\nSee {path}", "FocusFlow", 0x10)
            except Exception:
                pass
        return 1
    finally:
        # All application cleanup runs before restoring streams/closing the fault log.
        if fault_logging:
            faulthandler.disable()
        threading.excepthook = old_thread_hook
        sys.stdout, sys.stderr = old_stdout, old_stderr
        LOG.removeHandler(handler)
        LOG.setLevel(old_level)
        LOG.propagate = old_propagate
        handler.close()
        stream.close()
