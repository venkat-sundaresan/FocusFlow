"""Window caption buttons (minimise / maximise-restore / close) as dwell targets.

UI Automation reports the whole title bar for a point over these buttons, so they never became targets. Instead we
ask Windows itself: WM_NCHITTEST at the pointer answers HTMINBUTTON / HTMAXBUTTON / HTCLOSE exactly as it would for a
real click, and a short sideways scan finds each button's true extent. DWM's DWMWA_CAPTION_BUTTON_BOUNDS only bounds
the whole strip (it is wider than the three buttons), so it is used for the title bar's height and as a quick
pre-filter, never to guess button positions.

Works for standard frames and any frame that answers WM_NCHITTEST honestly (most native, UWP/WinUI and Chromium
frames). A frame that answers HTCAPTION over its own drawn buttons is left to UI Automation."""
import ctypes
from ctypes import wintypes as w

from . import winapi
from .targets import Target

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.WindowFromPoint.argtypes = [w.POINT]
user32.WindowFromPoint.restype = w.HWND
user32.GetAncestor.argtypes = [w.HWND, w.UINT]
user32.GetAncestor.restype = w.HWND
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
user32.IsIconic.argtypes = [w.HWND]
user32.IsZoomed.argtypes = [w.HWND]
user32.IsWindowVisible.argtypes = [w.HWND]
user32.GetDpiForWindow.argtypes = [w.HWND]
user32.GetDpiForWindow.restype = w.UINT
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.IsWindow.argtypes = [w.HWND]
user32.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)]
user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
try:
    _dwm = ctypes.windll.dwmapi
    _dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
except OSError:
    _dwm = None

DWMWA_CAPTION_BUTTON_BOUNDS = 5
GA_ROOT = 2
WM_NCHITTEST = 0x0084
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE, SC_MAXIMIZE, SC_RESTORE, SC_CLOSE = 0xF020, 0xF030, 0xF120, 0xF060
SMTO_ABORTIFHUNG = 0x0002
HTMINBUTTON, HTMAXBUTTON, HTCLOSE = 8, 9, 20            # HTREDUCE == HTMINBUTTON, HTZOOM == HTMAXBUTTON
WS_CAPTION, WS_SYSMENU = 0xC00000, 0x80000
MAX_BUTTON_PX = 200                                      # scan limit either side of the pointer
SCAN_STEP = 2


def root_window_at(x, y):
    h = user32.WindowFromPoint(w.POINT(int(x), int(y)))
    return user32.GetAncestor(h, GA_ROOT) if h else None


def hit_code(hwnd, x, y):
    """What Windows says is at (x, y) in `hwnd` (an HT* code), or None if the window does not answer in 40 ms
    (hung windows are never waited on)."""
    out = ctypes.c_size_t(0)
    lparam = ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)
    ok = user32.SendMessageTimeoutW(hwnd, WM_NCHITTEST, 0, lparam, SMTO_ABORTIFHUNG, 40, ctypes.byref(out))
    return int(ctypes.c_int(out.value).value) if ok else None


def _strip(hwnd, wr):
    """(left, top, right, bottom) of DWM's caption-button strip in physical px, or None. Used only to pre-filter and
    for the bar height. DWM answers in the window's DPI space, so scale up for DPI-virtualised (unaware) programs."""
    if _dwm is None:
        return None
    cb = w.RECT()
    if _dwm.DwmGetWindowAttribute(hwnd, DWMWA_CAPTION_BUTTON_BOUNDS, ctypes.byref(cb), ctypes.sizeof(cb)) != 0:
        return None
    try:
        f = winapi.dpi_scale_at((wr.left + wr.right) // 2, (wr.top + wr.bottom) // 2) / ((user32.GetDpiForWindow(hwnd) or 96) / 96.0)
    except Exception:
        f = 1.0
    f = max(0.5, min(4.0, f))
    return (wr.left + int(cb.left * f), wr.top + int(cb.top * f), wr.left + int(cb.right * f), wr.top + int(cb.bottom * f))


def name_for(code, maximised):
    return {HTMINBUTTON: "Minimize", HTMAXBUTTON: "Restore" if maximised else "Maximize", HTCLOSE: "Close"}.get(code)


def scan_extent(hit, x, y, code):
    """Pure given `hit(x, y) -> code`: [left, right] of the run of `code` containing x, scanning along row y."""
    left = right = x
    while left - SCAN_STEP >= x - MAX_BUTTON_PX and hit(left - SCAN_STEP, y) == code:
        left -= SCAN_STEP
    while right + SCAN_STEP <= x + MAX_BUTTON_PX and hit(right + SCAN_STEP, y) == code:
        right += SCAN_STEP
    return left, right + 1                              # [left, right) in px


def target_at(x, y):
    """The caption-button Target under (x, y), or None. Only when the pointer is *inside* a button."""
    hwnd = root_window_at(x, y)
    if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    style = user32.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF
    if not (style & WS_SYSMENU or (style & WS_CAPTION) == WS_CAPTION):
        return None                                     # no title bar (tool tips, our overlay, borderless pop-ups)
    wr = w.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    strip = _strip(hwnd, wr)
    if strip is not None:
        l, t, r, b = strip
        if not (l - 8 <= x < r + 8 and t <= y < b + 1):
            return None                                 # quick reject: nowhere near the caption buttons
    else:
        t, b = wr.top, wr.top + 60
        if not (wr.top <= y < b and x >= wr.right - 400):
            return None
    code = hit_code(hwnd, x, y)
    name = name_for(code, bool(user32.IsZoomed(hwnd)))
    if name is None:
        return None
    left, right = scan_extent(lambda px, py: hit_code(hwnd, px, py), int(x), int(y), code)
    rect = (left, int(t), right, int(b))
    return Target(f"caption:{hwnd}:{name}", rect, f"{name} button", "Button", "caption",
                  hit_point=((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2), data=(hwnd, code))


def command_for(code, maximised):
    """The system command the button stands for."""
    if code == HTMINBUTTON:
        return SC_MINIMIZE
    if code == HTMAXBUTTON:
        return SC_RESTORE if maximised else SC_MAXIMIZE
    if code == HTCLOSE:
        return SC_CLOSE
    return None


def press(target):
    """Do what the button does by posting the same WM_SYSCOMMAND Windows itself sends when it is clicked.

    Synthesised mouse clicks are not reliable on non-client caption buttons (they enter a modal tracking loop that
    does not treat injected input like hardware), so we do not depend on them. The program receives the usual command:
    an app that asks "save changes?" on close still asks. Returns True if the command was posted."""
    try:
        hwnd, code = target.data
    except (TypeError, ValueError):
        return False
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    sc = command_for(code, bool(user32.IsZoomed(hwnd)))
    return bool(sc and user32.PostMessageW(hwnd, WM_SYSCOMMAND, sc, 0))
