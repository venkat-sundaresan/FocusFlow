import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from focusflow import profiles
from focusflow.config import DEFAULTS, SCHEMA_VERSION, Settings, migrate


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "settings.json")

    def tearDown(self):
        self.dir.cleanup()

    def write(self, data, bom=False):
        with open(self.path, "w", encoding="utf-8-sig" if bom else "utf-8") as f:
            json.dump(data, f)

    def test_old_settings_are_migrated_keeping_the_users_values(self):
        self.write({"dwell_ms": 2000, "snap_radius_px": 50, "damping": 6, "show_dock_on_start": False,
                    "apps": [{"id": "notes"}], "study_mode": ["notes"], "study_minutes": 30,
                    "hotkeys": {"toggle": "ctrl+alt+g", "dock": "ctrl+alt+x", "study": "ctrl+alt+s", "timer": "ctrl+alt+t"}})
        cfg = Settings(self.path)
        self.assertEqual(cfg["dwell_ms"], 2000)                       # untouched user value
        self.assertEqual(cfg["snap_radius_px"], 50)
        self.assertFalse(cfg["show_window_on_start"])                 # renamed setting carried over
        self.assertEqual(cfg["hotkeys"]["toggle"], "ctrl+alt+g")      # custom shortcut kept
        self.assertEqual(cfg["hotkeys"]["window"], "ctrl+alt+x")      # old "dock" key became "window"
        self.assertIn("emergency", cfg["hotkeys"])                    # new shortcuts appear
        for gone in ("study", "timer", "dock", "browser", "notes"):
            self.assertNotIn(gone, cfg["hotkeys"])
        self.assertTrue(cfg["jitter_enabled"])
        self.assertGreater(cfg["smoothing_strength"], 5)              # v1 damping became real smoothing
        self.assertEqual(cfg["schema_version"], SCHEMA_VERSION)
        saved = json.load(open(self.path, encoding="utf-8"))
        for gone in ("apps", "study_mode", "study_minutes", "damping", "show_dock_on_start"):
            self.assertNotIn(gone, saved)
        self.assertTrue(os.path.exists(os.path.join(self.dir.name, "settings.v1.bak")))   # original kept once

    def test_users_still_on_the_old_1_2s_default_move_to_4s_but_tuned_values_are_kept(self):
        self.write({"dwell_ms": 1200, "schema_version": 2})
        self.assertEqual(Settings(self.path)["dwell_ms"], 4000)
        self.write({"dwell_ms": 1800, "schema_version": 2})                 # a deliberate choice
        self.assertEqual(Settings(self.path)["dwell_ms"], 1800)
        self.write({"dwell_ms": 1200, "schema_version": SCHEMA_VERSION})    # chosen explicitly after the migration
        self.assertEqual(Settings(self.path)["dwell_ms"], 1200)

    def test_the_default_dwell_time_is_four_seconds_in_settings_and_the_comfort_profile(self):
        self.assertEqual(DEFAULTS["dwell_ms"], 4000)
        self.assertEqual(profiles.BUILTIN["Comfort"]["values"]["dwell_ms"], 4000)
        self.assertTrue(profiles.matches(Settings(self.path), "Comfort"))

    def test_migration_is_idempotent(self):
        once = migrate({"damping": 4, "hotkeys": {"dock": "ctrl+alt+d"}})
        self.assertEqual(migrate(once), once)

    def test_a_settings_file_with_a_utf8_bom_is_read(self):
        self.write({"dwell_ms": 1700, "schema_version": SCHEMA_VERSION}, bom=True)   # what Notepad/PowerShell write
        self.assertEqual(Settings(self.path)["dwell_ms"], 1700)

    def test_corrupt_or_missing_files_fall_back_to_defaults(self):
        open(self.path, "w").write("{not json")
        self.assertEqual(Settings(self.path)["dwell_ms"], DEFAULTS["dwell_ms"])
        self.assertEqual(Settings(os.path.join(self.dir.name, "none.json"))["enabled"], True)

    def test_unknown_keys_are_ignored_and_set_rejects_them(self):
        self.write({"mystery": 1, "schema_version": SCHEMA_VERSION})
        cfg = Settings(self.path)
        with self.assertRaises(KeyError):
            cfg["mystery"]
        with self.assertRaises(KeyError):
            cfg.set("mystery", 2)

    def test_pairing_token_is_generated_once_and_never_in_the_public_view(self):
        t1 = Settings(self.path)["token"]
        self.assertTrue(t1)
        self.assertEqual(Settings(self.path)["token"], t1)
        self.assertNotIn("token", Settings(self.path).public())

    def test_set_many_notifies_each_key_and_persists_only_when_asked(self):
        cfg = Settings(self.path)
        seen = []
        cfg.on_change(seen.append)
        cfg.set_many({"dwell_ms": 900, "snap_radius_px": 40}, persist=False)
        self.assertEqual(sorted(seen), ["dwell_ms", "snap_radius_px"])
        self.assertNotEqual(json.load(open(self.path, encoding="utf-8")).get("dwell_ms"), 900)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = Settings(os.path.join(self.dir.name, "s.json"))

    def tearDown(self):
        self.dir.cleanup()

    def test_defaults_are_the_comfort_profile(self):
        self.assertTrue(profiles.matches(self.cfg, "Comfort"))

    def test_the_three_builtins_form_an_assistance_ladder(self):
        b = profiles.BUILTIN
        p, c, m = (b[n]["values"] for n in ("Precision", "Comfort", "Maximum Assistance"))
        self.assertLess(p["smoothing_strength"], c["smoothing_strength"])
        self.assertLess(c["smoothing_strength"], m["smoothing_strength"])
        self.assertLessEqual(p["attraction_strength"], c["attraction_strength"])
        self.assertLessEqual(c["miss_radius_px"], m["miss_radius_px"])
        self.assertFalse(p["dwell_enabled"])
        self.assertTrue(m["bounce_filter"])
        for v in (p, c, m):
            self.assertEqual(set(v), set(profiles.PROFILE_KEYS))

    def test_comfort_reproduces_the_original_filter_exactly(self):
        self.assertEqual(profiles.cutoff_scale(profiles.BUILTIN["Comfort"]["values"]["smoothing_strength"]), 1.0)

    def test_stronger_strength_always_means_a_lower_cutoff(self):
        scales = [profiles.cutoff_scale(s) for s in range(0, 11)]
        self.assertTrue(all(a > b for a, b in zip(scales, scales[1:])))
        self.assertEqual(profiles.cutoff_scale(-5), profiles.cutoff_scale(0))
        self.assertEqual(profiles.cutoff_scale(99), profiles.cutoff_scale(10))

    def test_apply_profile_copies_values_and_remembers_the_choice(self):
        self.assertTrue(profiles.apply_profile(self.cfg, "Precision"))
        self.assertEqual(self.cfg["active_profile"], "Precision")
        self.assertFalse(self.cfg["dwell_enabled"])
        self.assertTrue(profiles.matches(self.cfg, "Precision"))
        self.assertFalse(profiles.apply_profile(self.cfg, "Nope"))

    def test_a_temporary_application_profile_does_not_change_the_users_choice(self):
        profiles.apply_profile(self.cfg, "Maximum Assistance", persist=False, remember=False)
        self.assertEqual(self.cfg["active_profile"], "Comfort")
        self.assertEqual(self.cfg["smoothing_strength"], 9)

    def test_custom_profiles_save_apply_persist_and_delete(self):
        self.cfg.set("dwell_ms", 2200)
        self.assertEqual(profiles.save_custom(self.cfg, "  Evening  "), "Evening")
        profiles.apply_profile(self.cfg, "Precision")
        profiles.apply_profile(self.cfg, "Evening")
        self.assertEqual(self.cfg["dwell_ms"], 2200)
        self.assertEqual(Settings(self.cfg.path)["profiles"]["Evening"]["dwell_ms"], 2200)    # persisted
        self.cfg.set("app_profiles", {"a.exe": "Evening", "b.exe": "Precision"})
        self.assertTrue(profiles.delete_custom(self.cfg, "Evening"))
        self.assertEqual(self.cfg["active_profile"], "Comfort")                                  # falls back safely
        self.assertEqual(self.cfg["app_profiles"], {"b.exe": "Precision"})                       # rules cleaned up
        self.assertFalse(profiles.delete_custom(self.cfg, "Evening"))

    def test_builtin_names_cannot_be_overwritten_and_empty_names_are_rejected(self):
        self.assertIsNone(profiles.save_custom(self.cfg, "Comfort"))
        self.assertIsNone(profiles.save_custom(self.cfg, "   "))

    def test_a_tampered_custom_profile_cannot_inject_unknown_keys(self):
        self.cfg.set("profiles", {"X": {"dwell_ms": 800, "token": "evil", "enabled": False}})
        vals = profiles.all_profiles(self.cfg)["X"]
        self.assertEqual(vals["dwell_ms"], 800)
        self.assertNotIn("token", vals)
        self.assertNotIn("enabled", vals)

    def test_next_profile_cycles_through_builtins_then_customs(self):
        profiles.save_custom(self.cfg, "Zed")
        seen, name = [], self.cfg["active_profile"]
        for _ in range(4):
            name = profiles.next_profile(self.cfg)
            profiles.apply_profile(self.cfg, name)
            seen.append(name)
        self.assertEqual(len(set(seen)), 4)

    def test_experiment_modes_cover_the_five_required_comparisons(self):
        self.assertEqual(len(profiles.MODE_NAMES), 5)
        self.assertEqual(profiles.mode_mask("Normal mouse operation"), {"jitter": False, "dwell": False, "adapt": False})
        self.assertEqual(profiles.mode_mask("Full FocusFlow assistance"), profiles.FULL_MASK)
        with self.assertRaises(KeyError):
            profiles.mode_mask("bogus")


if __name__ == "__main__":
    unittest.main()
