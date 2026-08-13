import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts.settings import (
    AppSettings,
    get_config_directory,
    get_local_data_directory,
    load_app_settings,
)


class SettingsTest(unittest.TestCase):
    def test_schema_11_idle_delay_migrates_to_lower_live_latency(self):
        settings = AppSettings.from_mapping(
            {
                "schema_version": 11,
                "live_idle_flush_ms": 700,
            }
        )

        self.assertEqual(settings.schema_version, 16)
        self.assertEqual(settings.live_idle_flush_ms, 400)

    def test_current_schema_preserves_an_explicit_idle_delay(self):
        settings = AppSettings.from_mapping(
            {
                "schema_version": 16,
                "live_idle_flush_ms": 700,
            }
        )

        self.assertEqual(settings.live_idle_flush_ms, 700)

    def test_speech_backend_can_be_selected_from_environment(self):
        settings = AppSettings().with_environment_overrides(
            {"VNTTS_SPEECH_BACKEND": "chatterbox-nano"}
        )

        self.assertEqual(settings.speech_backend, "chatterbox-nano")

    def test_pocket_tts_backend_can_be_selected(self):
        settings = AppSettings.from_mapping({"speech_backend": "pocket-tts"})

        self.assertEqual(settings.speech_backend, "pocket-tts")

    def test_pocket_tts_is_the_default_backend(self):
        self.assertEqual(AppSettings().speech_backend, "pocket-tts")

    def test_unknown_speech_backend_uses_default(self):
        warnings = []

        settings = AppSettings.from_mapping(
            {"speech_backend": "instant-magic"},
            warn=warnings.append,
        )

        self.assertEqual(settings.speech_backend, "pocket-tts")
        self.assertTrue(any("speech_backend" in warning for warning in warnings))

    def test_paths_use_platformdirs(self):
        config_path = Path("/config/vntts")
        data_path = Path("/data/vntts")
        with (
            patch(
                "vntts.settings.user_config_path", return_value=config_path
            ) as config,
            patch("vntts.settings.user_data_path", return_value=data_path) as data,
        ):
            self.assertEqual(get_config_directory(), config_path)
            self.assertEqual(get_local_data_directory(), data_path)
        app_name = (
            "VisualNovelTextToSpeech"
            if sys.platform in {"darwin", "win32"}
            else "vntts"
        )
        config.assert_called_once_with(app_name, appauthor=False, roaming=True)
        data.assert_called_once_with(app_name, appauthor=False)

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
                emergency_stop_hotkey="<ctrl>+q",
                ocr_minimum_confidence=72,
                retain_uncertain_frames=True,
                ocr_diagnostics_directory="custom/ocr-diagnostics",
                capture_mode="window",
                game_window_title="Reverse: 1999",
                tts_model="tts_models/multilingual/multi-dataset/xtts_v2",
                tts_language="en",
                output_volume_percent=72,
                speech_rate_percent=115,
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

    def test_ocr_confidence_must_be_between_zero_and_one_hundred(self):
        warnings = []

        settings = AppSettings.from_mapping(
            {"ocr_minimum_confidence": 101},
            warn=warnings.append,
        )

        self.assertEqual(
            settings.ocr_minimum_confidence,
            AppSettings().ocr_minimum_confidence,
        )
        self.assertTrue(
            any("ocr_minimum_confidence" in warning for warning in warnings)
        )

    def test_speech_controls_are_range_checked(self):
        warnings = []

        settings = AppSettings.from_mapping(
            {
                "output_volume_percent": 101,
                "speech_rate_percent": 49,
            },
            warn=warnings.append,
        )

        self.assertEqual(
            settings.output_volume_percent,
            AppSettings().output_volume_percent,
        )
        self.assertEqual(
            settings.speech_rate_percent,
            AppSettings().speech_rate_percent,
        )
        self.assertEqual(len(warnings), 2)

    def test_invalid_auto_advance_key_falls_back_safely(self):
        warnings = []

        settings = AppSettings.from_mapping(
            {"auto_advance_enabled": True, "auto_advance_key": "delete"},
            warn=warnings.append,
        )

        self.assertTrue(settings.auto_advance_enabled)
        self.assertEqual(settings.auto_advance_key, "space")
        self.assertTrue(any("auto_advance_key" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
