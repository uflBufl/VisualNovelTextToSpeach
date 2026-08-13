import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.services.tts_engine import TTSConfigurationError
from vntts.speech_backend_runtime import (
    BoundedCache,
    validate_speed,
    validate_volume,
    voice_artifact_cache_path,
)


class SpeechBackendRuntimeTest(unittest.TestCase):
    def test_bounded_cache_evicts_the_least_recently_used_value(self):
        cache = BoundedCache(2)
        cache.put("first", 1)
        cache.put("second", 2)
        self.assertEqual(cache.get("first"), 1)

        cache.put("third", 3)

        self.assertIsNone(cache.get("second"))
        self.assertEqual(cache.get("first"), 1)
        self.assertEqual(cache.get("third"), 3)

    def test_speech_settings_share_validation(self):
        self.assertEqual(validate_volume(0.5), 0.5)
        self.assertEqual(validate_speed(1), 1.0)
        with self.assertRaisesRegex(TTSConfigurationError, "between 0 and 1"):
            validate_volume(2)
        with self.assertRaisesRegex(TTSConfigurationError, "number"):
            validate_speed(False)

    def test_voice_artifact_path_changes_with_source_content(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "voice.wav"
            source.write_bytes(b"first")
            first = voice_artifact_cache_path(
                root,
                voice_key="Alice Voice",
                source=source,
                model_identity="model:v1",
                suffix=".state",
            )
            source.write_bytes(b"second-content")
            second = voice_artifact_cache_path(
                root,
                voice_key="Alice Voice",
                source=source,
                model_identity="model:v1",
                suffix=".state",
            )

        self.assertNotEqual(first, second)
        self.assertTrue(first.name.startswith("alice-voice-"))


if __name__ == "__main__":
    unittest.main()
