"""Thin ctypes layer over the Win32 calls FocusFlow needs. Mouse and registered hotkeys only:
there is no keyboard hook, no screen capture."""
import ctypes
import logging
import os
import sys
import threading
from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WM_QUIT, WM_HOTKEY = 0x0012, 0x0312
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WH_MOUSE_LL = 14
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02
FF_TAG = 0x46464C57          # dwExtraInfo on every event FocusFlow injects, so its own hook can tell them apart
# Test-only: treat input injected by *other* processes (e.g. SendInput in the test-suite) as physical mouse
# input. Real use never sets this, so every other program's injected input stays untouched.
ACCEPT_FOREIGN_INJECTED = os.environ.get("FOCUSFLOW_TEST_ACCEPT_INJECTED") == "1"

user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetForegroundWindow.restype = w.HWND
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAncestor.argtypes = [w.HWND, w.UINT]
user32.GetAncestor.restype = w.HWND
user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
user32.GetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, w.WPARAM, w.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, w.DWORD]
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.CloseHandle.restype = w.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
kernel32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.CreateMutexW.restype = w.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]


def set_dpi_aware():
    """Per-monitor DPI awareness so cursor, UIA and Tk all speak physical pixels."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def time_begin_period(ms):
    """Ask for ~1 ms sleep granularity (the default is ~15 ms) so the 125 Hz pointer thread keeps time."""
    try:
        ctypes.windll.winmm.timeBeginPeriod(int(ms))
    except Exception:
        pass


def time_end_period(ms):
    try:
        ctypes.windll.winmm.timeEndPeriod(int(ms))
    except Exception:
        pass


def single_instance(name="Local\\FocusFlowAgent"):
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    return handle, error != 183  # ERROR_ALREADY_EXISTS; caller owns and must close the handle


# -- pointer ---------------------------------------------------------------
def get_cursor_pos():
    pt = w.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def set_cursor_pos(x, y):
    user32.SetCursorPos(int(x), int(y))


def place_cursor(x, y):
    """Move the cursor and report where it really ended up (monitor edges / gaps clamp it)."""
    user32.SetCursorPos(int(x), int(y))
    return get_cursor_pos()


def dpi_scale_at(x, y):
    """Display scale (1.0 = 96 dpi) of the monitor under (x, y). 1.0 if it cannot be determined."""
    try:
        mon = user32.MonitorFromPoint(w.POINT(int(x), int(y)), 2)    # MONITOR_DEFAULTTONEAREST
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        if ctypes.windll.shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
            return dx.value / 96.0
    except Exception:
        pass
    return 1.0


def virtual_bounds():
    x, y, wd, ht = virtual_screen()
    return (x, y, x + wd, y + ht)


def left_button_down():
    return bool(user32.GetAsyncKeyState(0x01) & 0x8000)


def virtual_screen():
    """left, top, width, height of the whole desktop (all monitors)."""
    gm = user32.GetSystemMetrics
    return gm(76), gm(77), gm(78), gm(79)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _U)]


user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
user32.MonitorFromPoint.argtypes = [w.POINT, w.DWORD]
user32.MonitorFromPoint.restype = ctypes.c_void_p


def _mouse_events(flags, data=0):
    seq = (_INPUT * len(flags))()
    for i, flag in enumerate(flags):
        seq[i].type = 0
        seq[i].mi.dwFlags = flag
        seq[i].mi.mouseData = data & 0xFFFFFFFF
        seq[i].mi.dwExtraInfo = FF_TAG
    user32.SendInput(len(flags), seq, ctypes.sizeof(_INPUT))


def send_click(x, y, button="left", double=False):
    """Move to (x, y) and click. Tagged with FF_TAG so FocusFlow's own hook ignores it."""
    if x is not None:
        user32.SetCursorPos(int(x), int(y))
    down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
    _mouse_events([down, up] * (2 if double else 1))


def send_wheel(clicks):
    """Scroll the wheel under the pointer; positive = up. One 'click' is 120 units."""
    _mouse_events([0x0800], int(clicks * 120))


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _KINPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]    # mi keeps the union at its real size
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _U)]


_send_keys = ctypes.WinDLL("user32", use_last_error=True).SendInput    # its own function object / argtypes
_send_keys.argtypes = [w.UINT, ctypes.POINTER(_KINPUT), ctypes.c_int]


def _key_events(items):
    seq = (_KINPUT * len(items))()
    for i, (vk, scan, flags) in enumerate(items):
        seq[i].type = 1
        seq[i].ki.wVk, seq[i].ki.wScan, seq[i].ki.dwFlags = vk, scan, flags
        seq[i].ki.dwExtraInfo = FF_TAG
    _send_keys(len(items), seq, ctypes.sizeof(_KINPUT))


def send_text(text):
    """Type characters into the focused window (output only; FocusFlow never reads keystrokes)."""
    items = []
    for ch in text:
        code = ord(ch)
        items += [(0, code, 0x0004), (0, code, 0x0004 | 0x0002)]   # KEYEVENTF_UNICODE down / up
    if items:
        _key_events(items)


def send_vk(vk, with_ctrl=False):
    """Press and release a virtual key (Backspace=8, Tab=9, Enter=13, Esc=27, arrows 37-40...)."""
    items = []
    if with_ctrl:
        items.append((0x11, 0, 0))
    items += [(vk, 0, 0), (vk, 0, 0x0002)]
    if with_ctrl:
        items.append((0x11, 0, 0x0002))
    _key_events(items)


def foreground_hwnd():
    return user32.GetForegroundWindow()


def window_rect(hwnd):
    r = w.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


# -- processes / windows -----------------------------------------------------
def _image_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = w.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)
    return None


def foreground_exe():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return _image_name(pid.value)


def process_alive(pid, expect_exe=None):
    h = kernel32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = w.DWORD()
        if not kernel32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:
            return False
    finally:
        kernel32.CloseHandle(h)
    return expect_exe is None or _image_name(pid) == expect_exe.lower()


CHROMIUM_EXES = {"chrome.exe", "msedge.exe", "brave.exe", "opera.exe", "vivaldi.exe", "chromium.exe"}


def make_noactivate(tk_window):
    """A clickable, topmost window that never takes keyboard focus (for the on-screen keyboard)."""
    hwnd = user32.GetAncestor(tk_window.winfo_id(), 2) or tk_window.winfo_id()
    style = user32.GetWindowLongPtrW(hwnd, -20)
    user32.SetWindowLongPtrW(hwnd, -20, style | 0x08000000 | 0x80)   # NOACTIVATE | TOOLWINDOW
    user32.SetWindowPos(hwnd, w.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0020)  # topmost, no move/size
    return hwnd


def make_overlay_window(tk_window, x, y, w_, h_):
    """Make a Tk toplevel click-through, topmost, non-activating; place it in physical pixels."""
    hwnd = user32.GetAncestor(tk_window.winfo_id(), 2) or tk_window.winfo_id()
    GWL_EXSTYLE = -20
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    style |= 0x00080000 | 0x20 | 0x80 | 0x08000000  # LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
    # HWND_TOPMOST = -1; SWP_NOACTIVATE | SWP_SHOWWINDOW
    user32.SetWindowPos(hwnd, w.HWND(-1), x, y, w_, h_, 0x0010 | 0x0040)
    return hwnd


# -- autostart (HKCU Run: per-user, no admin) ---------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command():
    pyw = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "focusflow.pyw")
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"
    return f'"{exe}" "{pyw}"'


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "FocusFlow", 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, "FocusFlow")
            except FileNotFoundError:
                pass


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, "FocusFlow")
            return True
    except OSError:
        return False


# -- global hotkeys (RegisterHotKey: only the combos we register, nothing else is observed) ---------
_MODS = {"ctrl": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}


def parse_hotkey(spec):
    mods, vk = 0, None
    for part in spec.lower().replace(" ", "").split("+"):
        if part in _MODS:
            mods |= _MODS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x6F + int(part[1:])
        else:
            raise ValueError(f"bad hotkey: {spec}")
    if vk is None or (not mods and not 0x70 <= vk <= 0x87):     # bare keys only for F1-F24 (switch devices)
        raise ValueError(f"bad hotkey: {spec}")
    return mods, vk


class HotkeyThread(threading.Thread):
    def __init__(self, bindings):
        """bindings: list of (spec, callback)."""
        super().__init__(daemon=True, name="ff-hotkeys")
        self.bindings = bindings
        self.tid = None
        self.failed = []
        self._ready = threading.Event()
        self.error = None

    def run(self):
        callbacks = {}
        try:
            self.tid = kernel32.GetCurrentThreadId()
            msg = w.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            for i, (spec, cb) in enumerate(self.bindings, start=1):
                try:
                    mods, vk = parse_hotkey(spec)
                except ValueError:
                    self.failed.append(spec)
                    continue
                if user32.RegisterHotKey(None, i, mods | 0x4000, vk):  # MOD_NOREPEAT
                    callbacks[i] = cb
                else:
                    self.failed.append(spec)
            self._ready.set()
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
                if msg.message == WM_HOTKEY and msg.wParam in callbacks:
                    try:
                        callbacks[msg.wParam]()
                    except Exception:
                        logging.getLogger("focusflow").exception("Hotkey callback failed")
        except Exception as exc:
            self.error = exc
            logging.getLogger("focusflow").exception("Hotkey worker failed")
        finally:
            self._ready.set()
            for i in callbacks:
                user32.UnregisterHotKey(None, i)

    def wait_ready(self, timeout=5):
        if not self._ready.wait(timeout):
            raise RuntimeError("Hotkey initialization timed out")
        if self.error is not None:
            raise RuntimeError("Hotkey initialization failed") from self.error

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)


# -- low-level mouse hook -------------------------------------------------------
class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", w.POINT), ("mouseData", w.DWORD), ("flags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


_HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, w.WPARAM, w.LPARAM)


class MouseHook(threading.Thread):
    """Calls handler(msg, x, y, injected) for each mouse event; a truthy return swallows the event.
    Any exception passes the event through untouched, so a bug can never trap the mouse."""

    def __init__(self, handler):
        super().__init__(daemon=True, name="ff-mousehook")
        self.handler = handler
        self.tid = None
        self._hook = None
        self._proc = _HOOKPROC(self._callback)  # keep a reference alive
        self._ready = threading.Event()
        self.error = None

    def _callback(self, code, wparam, lparam):
        try:
            if code >= 0:
                info = ctypes.cast(lparam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                injected = bool(info.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED))
                if injected and ACCEPT_FOREIGN_INJECTED and info.dwExtraInfo != FF_TAG:
                    injected = False                  # test mode: foreign injected input counts as the mouse
                if self.handler(int(wparam), info.pt.x, info.pt.y, injected):
                    return 1
        except Exception:
            pass
        return user32.CallNextHookEx(self._hook, code, wparam, lparam)

    def run(self):
        try:
            self.tid = kernel32.GetCurrentThreadId()
            msg = w.MSG()
            # stop() needs a queue even before the first GetMessage call.
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, ctypes.cast(self._proc, ctypes.c_void_p),
                                                  kernel32.GetModuleHandleW(None), 0)
            if not self._hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
        except Exception as exc:
            self.error = exc
            logging.getLogger("focusflow").exception("Mouse hook failed; input remains under Windows control")
        finally:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
                self._hook = None
            self._ready.set()

    def wait_ready(self, timeout=5):
        if not self._ready.wait(timeout):
            raise RuntimeError("Mouse hook installation timed out")
        if self.error is not None:
            raise RuntimeError("Mouse hook installation failed") from self.error

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
