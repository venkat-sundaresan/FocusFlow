"""Settings: a small JSON file in %APPDATA%/FocusFlow, with change listeners and a schema migration."""
import copy
import json
import os
import secrets
import threading
from pathlib import Path

APP_DIR = Path(os.environ.get("FOCUSFLOW_HOME") or Path(os.environ.get("APPDATA", str(Path.home()))) / "FocusFlow")
SCHEMA_VERSION = 3

DEFAULTS = {
    "schema_version": SCHEMA_VERSION,
    "enabled": True,
    "ml_enabled": False,          # opt in; no model imports or raw acquisition while disabled
    "ml_mode": "shadow",         # shadow | experimental; not part of saved pointer profiles
    "ml_model_path": "",         # empty = bundled/local stress-trained model location
    # ---- the values a profile controls (see profiles.PROFILE_KEYS); defaults == the Comfort profile ----
    "jitter_enabled": True,       # PointerLab AdaptivePointer on the real cursor
    "smoothing_strength": 5,      # 0..10; 5 reproduces the original filter, higher = more smoothing
    "sensitivity": 5,             # 1..10, tremor-detector sensitivity (dwell tolerance)
    "dwell_enabled": True,
    "dwell_ms": 4000,             # 4 s: slow enough that resting a hand on a control never clicks it by accident
    "snap_enabled": True,         # dwell on a clearly-nearest target when the pointer is just outside it
    "snap_radius_px": 36,
    "attraction_strength": 4,     # 0..10, magnetic pull toward one unambiguous nearby target (0 = off)
    "attraction_radius_px": 36,
    "near_miss_correct": True,    # redirect a physical click that landed just outside a target
    "miss_radius_px": 24,
    "adaptation_enabled": True,   # 3 nearby misses in a row enlarge that target's hit area
    "bounce_filter": False,       # swallow physical re-clicks that arrive faster than bounce_ms
    "bounce_ms": 120,
    "tolerance_px": 8,            # extra wander allowed while dwelling (grows with tremor)
    # ---- not part of a profile ----
    "dwell_cooldown_ms": 1500,
    "assist_dwell_factor": 1.5,   # snapped dwells wait this much longer
    "hover_speed_px_s": 250,      # smoothed speed under which the pointer counts as "settled"
    "suggest_stronger": True,     # offer stronger smoothing when repeated short reversals are detected
    "active_profile": "Comfort",
    "profiles": {},               # custom profiles: name -> {profile keys}
    "app_profiles": {},           # foreground exe name -> profile name
    # ---- accessibility tools ----
    "large_cursor": False,
    "cursor_size": 3,             # 1..6
    "cursor_color": "yellow",     # yellow | white | cyan | black
    "pointer_trails": False,
    "trail_length": 24,
    "highlight_targets": False,
    "osk_enabled": False,
    "osk_dwell_ms": 700,
    "voice_enabled": False,
    "switch_enabled": False,
    "switch_auto_scan": False,
    "switch_scan_ms": 1200,
    # ---- privacy / startup ----
    "log_target_names": True,
    "autostart": False,
    "autostart_asked": False,
    "show_window_on_start": True,
    "extension_enabled": True,
    "autopair_extension": True,
    "hotkeys": {
        "toggle": "ctrl+alt+f",
        "emergency": "ctrl+alt+shift+f",
        "window": "ctrl+alt+d",
        "profile": "ctrl+alt+p",
        "lab": "ctrl+alt+l",
        "keyboard": "ctrl+alt+k",
        "settings": "ctrl+alt+o",
        "undo": "ctrl+alt+z",
        "switch_next": "ctrl+alt+n",
        "switch_select": "ctrl+alt+m",
    },
    "token": "",
}

# Settings that only existed for the removed Study Mode / launcher / dock.
REMOVED_KEYS = ("apps", "study_mode", "study_minutes", "show_dock_on_start", "damping")
REMOVED_HOTKEYS = ("study", "browser", "notes", "timer", "dock")


def migrate(on_disk):
    """Bring a settings dict written by an older FocusFlow up to SCHEMA_VERSION. Pure; returns a new dict."""
    d = dict(on_disk)
    version = d.get("schema_version", 1)
    if version < 2:
        damping = d.get("damping", 0) or 0
        if damping > 0 and "smoothing_strength" not in d:       # experimental v1 damping -> real smoothing
            d["jitter_enabled"] = True
            d["smoothing_strength"] = min(10, 5 + (damping + 1) // 2)
        if "show_dock_on_start" in d and "show_window_on_start" not in d:
            d["show_window_on_start"] = d["show_dock_on_start"]
        hk = dict(d.get("hotkeys") or {})
        if "dock" in hk and "window" not in hk:
            hk["window"] = hk["dock"]
        d["hotkeys"] = hk
    if version < 3 and d.get("dwell_ms") == 1200:        # still on the untouched old default: adopt the new 4 s one
        d.pop("dwell_ms")
    for k in REMOVED_KEYS:
        d.pop(k, None)
    if isinstance(d.get("hotkeys"), dict):
        for k in REMOVED_HOTKEYS:
            d["hotkeys"].pop(k, None)
    d["schema_version"] = SCHEMA_VERSION
    return d


class Settings:
    def __init__(self, path=None):
        self.path = Path(path) if path else APP_DIR / "settings.json"
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self._listeners = []
        self.load()
        if not self._data.get("token"):
            self._data["token"] = secrets.token_urlsafe(24)
            self.save()

    def load(self):
        try:
            on_disk = json.loads(self.path.read_text(encoding="utf-8-sig"))   # Notepad/PowerShell add a BOM
        except (OSError, ValueError):
            return
        if not isinstance(on_disk, dict):
            return
        needs_save = on_disk.get("schema_version", 1) < SCHEMA_VERSION
        if needs_save:
            try:                                   # keep the pre-migration file, once
                backup = self.path.with_name(self.path.stem + ".v1.bak")
                if not backup.exists():
                    backup.write_text(json.dumps(on_disk, indent=2), encoding="utf-8")
            except OSError:
                pass
        on_disk = migrate(on_disk)
        with self._lock:
            for key, val in on_disk.items():
                if key not in DEFAULTS:
                    continue
                if isinstance(DEFAULTS[key], dict) and key not in ("profiles", "app_profiles") and isinstance(val, dict):
                    self._data[key] = {**DEFAULTS[key], **val}      # new hotkeys appear for old files
                else:
                    self._data[key] = val
        if needs_save:
            self.save()

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def __getitem__(self, key):
        with self._lock:
            return self._data[key]

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value, notify=True, persist=True):
        with self._lock:
            if key not in DEFAULTS:
                raise KeyError(key)
            self._data[key] = value
            if persist:
                self.save()
        if notify:
            self._notify(key)

    def set_many(self, values, persist=True):
        with self._lock:
            for key in values:
                if key not in DEFAULTS:
                    raise KeyError(key)
            self._data.update(copy.deepcopy(values))
            if persist:
                self.save()
        for key in values:
            self._notify(key)

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def public(self):
        """Snapshot without the pairing token."""
        snap = self.snapshot()
        snap.pop("token", None)
        return snap

    def on_change(self, fn):
        self._listeners.append(fn)

    def _notify(self, key):
        for fn in list(self._listeners):
            try:
                fn(key)
            except Exception:
                pass
