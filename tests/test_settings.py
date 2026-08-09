import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.settings import (
    AppSettings,
    get_config_directory,
    get_local_data_directory,
    load_app_settings,
)


class SettingsTest(unittest.TestCase):
    def test_windows_paths_use_roaming_settings_and_local_application_data(self):
        environment = {
            "APPDATA": r"C:\Users\Ada\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\Ada\AppData\Local",
        }

        self.assertEqual(
            get_config_directory(environment=environment, platform="win32"),
            Path(environment["APPDATA"]) / "VisualNovelTextToSpeech",
        )
        self.assertEqual(
            get_local_data_directory(environment=environment, platform="win32"),
            Path(environment["LOCALAPPDATA"]) / "VisualNovelTextToSpeech",
        )

    def test_settings_round_trip_as_json(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "settings.json"
            settings = AppSettings(
                onboarding_completed=True,
                xtts_terms_accepted=True,
                read_hotkey="<ctrl>+r",
                pause_hotkey="<ctrl>+p",
                skip_hotkey="<ctrl>+s",
                repeat_hotkey="<ctrl>+e",
                clear_queue_hotkey="<ctrl>+x",
                capture_mode="window",
                game_window_title="Reverse: 1999",
                tts_model="tts_models/multilingual/multi-dataset/xtts_v2",
                tts_language="en",
            )

            self.assertEqual(settings.save(path), path)
            loaded = load_app_settings(path, environment={})

        self.assertEqual(loaded, settings)

    def test_malformed_settings_file_uses_defaults(self):
        warnings = []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "settings.json"
            path.write_text("{not-json", encoding="utf-8")

            settings = load_app_settings(path, environment={}, warn=warnings.append)

        self.assertEqual(settings.read_hotkey, AppSettings().read_hotkey)
        self.assertIn("Unable to load settings", warnings[0])

    def test_invalid_fields_and_environment_values_do_not_prevent_startup(self):
        warnings = []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "read_hotkey": "<ctrl>+r",
                        "live_interval_ms": 350,
                        "live_stability_frames": "bad",
                    }
                ),
                encoding="utf-8",
            )

            settings = load_app_settings(
                path,
                environment={"VNTTS_LIVE_INTERVAL_MS": "also-bad"},
                warn=warnings.append,
            )

        self.assertEqual(settings.read_hotkey, "<ctrl>+r")
        self.assertEqual(settings.live_interval_ms, 350)
        self.assertEqual(
            settings.live_stability_frames,
            AppSettings().live_stability_frames,
        )
        self.assertTrue(any("VNTTS_LIVE_INTERVAL_MS" in value for value in warnings))

    def test_invalid_capture_mode_falls_back_to_screen(self):
        warnings = []

        settings = AppSettings.from_mapping(
            {"capture_mode": "desktop-magic"},
            warn=warnings.append,
        )

        self.assertEqual(settings.capture_mode, "screen")
        self.assertIn("capture_mode", warnings[0])


if __name__ == "__main__":
    unittest.main()
