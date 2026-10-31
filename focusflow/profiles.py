"""Adaptive profiles, the smoothing-strength curve and the experiment modes. Pure (no Windows calls)."""
import copy

PROFILE_KEYS = (
    "jitter_enabled", "smoothing_strength", "sensitivity",
    "dwell_enabled", "dwell_ms", "snap_enabled", "snap_radius_px",
    "attraction_strength", "attraction_radius_px",
    "near_miss_correct", "miss_radius_px", "adaptation_enabled",
    "bounce_filter", "bounce_ms", "tolerance_px",
)

BUILTIN = {
    "Precision": {
        "blurb": "Minimal smoothing and assistance. The mouse stays almost exactly as it is.",
        "values": {
            "jitter_enabled": True, "smoothing_strength": 3, "sensitivity": 3,
            "dwell_enabled": False, "dwell_ms": 1500, "snap_enabled": False, "snap_radius_px": 24,
            "attraction_strength": 0, "attraction_radius_px": 24,
            "near_miss_correct": False, "miss_radius_px": 16, "adaptation_enabled": False,
            "bounce_filter": False, "bounce_ms": 120, "tolerance_px": 6,
        },
    },
    "Comfort": {
        "blurb": "Balanced stabilisation and dwell clicking. The original PointerLab filter, unchanged.",
        "values": {
            "jitter_enabled": True, "smoothing_strength": 5, "sensitivity": 5,
            "dwell_enabled": True, "dwell_ms": 4000, "snap_enabled": True, "snap_radius_px": 36,
            "attraction_strength": 4, "attraction_radius_px": 36,
            "near_miss_correct": True, "miss_radius_px": 24, "adaptation_enabled": True,
            "bounce_filter": False, "bounce_ms": 120, "tolerance_px": 8,
        },
    },
    "Maximum Assistance": {
        "blurb": "Stronger stabilisation, wider attraction, bigger hit areas and repeat-click protection.",
        "values": {
            "jitter_enabled": True, "smoothing_strength": 9, "sensitivity": 8,
            "dwell_enabled": True, "dwell_ms": 1000, "snap_enabled": True, "snap_radius_px": 56,
            "attraction_strength": 7, "attraction_radius_px": 56,
            "near_miss_correct": True, "miss_radius_px": 36, "adaptation_enabled": True,
            "bounce_filter": True, "bounce_ms": 150, "tolerance_px": 14,
        },
    },
}
BUILTIN_NAMES = tuple(BUILTIN)


def cutoff_scale(strength):
    """Smoothing strength 0..10 -> multiplier on the original cut-off curve. 5 is the original filter (1.0);
    each +3 halves the cut-off (twice the smoothing); each -3 doubles it (about half the smoothing)."""
    s = max(0.0, min(10.0, float(strength)))
    return 2.0 ** ((5.0 - s) / 3.0)


def all_profiles(cfg):
    """name -> dict of profile values (built-ins first, then custom)."""
    out = {n: dict(p["values"]) for n, p in BUILTIN.items()}
    for name, vals in (cfg["profiles"] or {}).items():
        base = dict(BUILTIN["Comfort"]["values"])
        base.update({k: v for k, v in vals.items() if k in PROFILE_KEYS})
        out[name] = base
    return out


def current_values(cfg):
    return {k: cfg[k] for k in PROFILE_KEYS}


def matches(cfg, name):
    prof = all_profiles(cfg).get(name)
    return prof is not None and all(cfg[k] == v for k, v in prof.items())


def apply_profile(cfg, name, persist=True, remember=True):
    """Copy a profile's values into the live settings. `remember` makes it the user's chosen profile
    (False for the temporary per-application switch)."""
    prof = all_profiles(cfg).get(name)
    if prof is None:
        return False
    values = copy.deepcopy(prof)
    if remember:
        values["active_profile"] = name
    cfg.set_many(values, persist=persist)
    return True


def save_custom(cfg, name):
    name = (name or "").strip()[:32]
    if not name or name in BUILTIN:
        return None
    custom = dict(cfg["profiles"])
    custom[name] = current_values(cfg)
    cfg.set("profiles", custom)
    cfg.set("active_profile", name)
    return name


def delete_custom(cfg, name):
    custom = dict(cfg["profiles"])
    if name not in custom:
        return False
    del custom[name]
    cfg.set("profiles", custom)
    if cfg["active_profile"] == name:
        apply_profile(cfg, "Comfort")
    rules = {k: v for k, v in cfg["app_profiles"].items() if v != name}
    cfg.set("app_profiles", rules)
    return True


def next_profile(cfg):
    names = list(all_profiles(cfg))
    cur = cfg["active_profile"]
    return names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]


def summary(values):
    bits = []
    if values["jitter_enabled"]:
        bits.append(f"smoothing {values['smoothing_strength']}/10")
    if values["dwell_enabled"]:
        bits.append(f"dwell {values['dwell_ms'] / 1000:.1f}s")
    if values["attraction_strength"]:
        bits.append(f"attraction {values['attraction_radius_px']}px")
    if values["near_miss_correct"] or values["adaptation_enabled"]:
        bits.append(f"hit area {values['miss_radius_px']}px")
    if values["bounce_filter"]:
        bits.append("repeat-click guard")
    return " · ".join(bits) or "no assistance"


# ---- experiment modes ---------------------------------------------------------------------------------------
# Each mode masks the live profile: a feature group runs only if the mode allows it AND the profile enables it.
EXPERIMENT_MODES = (
    ("Normal mouse operation", {"jitter": False, "dwell": False, "adapt": False}),
    ("Jitter reduction only", {"jitter": True, "dwell": False, "adapt": False}),
    ("Dwell clicking only", {"jitter": False, "dwell": True, "adapt": False}),
    ("Target adaptation only", {"jitter": False, "dwell": False, "adapt": True}),
    ("Full FocusFlow assistance", {"jitter": True, "dwell": True, "adapt": True}),
)
MODE_NAMES = tuple(n for n, _ in EXPERIMENT_MODES)
FULL_MASK = {"jitter": True, "dwell": True, "adapt": True}


def mode_mask(name):
    for n, m in EXPERIMENT_MODES:
        if n == name:
            return dict(m)
    raise KeyError(name)
