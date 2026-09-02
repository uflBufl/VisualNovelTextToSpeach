import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from vntts.audio_cache import PersistentAudioCache


class PersistentAudioCacheTest(unittest.TestCase):
    def test_key_includes_backend_model_voice_text_and_settings(self):
        with TemporaryDirectory() as temporary_directory:
            cache = PersistentAudioCache(temporary_directory)
            base = {
                "backend": "pocket",
                "model": "2.1",
                "voice": "selone",
                "text": "Hello   world.",
                "settings": {"speed": 1.0},
            }

            first = cache.key(**base)
            normalized = cache.key(**{**base, "text": "Hello world."})
            changed = {
                cache.key(**{**base, "backend": "chatterbox"}),
                cache.key(**{**base, "model": "2.2"}),
                cache.key(**{**base, "voice": "fatutu"}),
                cache.key(**{**base, "text": "Goodbye world."}),
                cache.key(**{**base, "settings": {"speed": 1.1}}),
            }

        self.assertEqual(first, normalized)
        self.assertNotIn(first, changed)
        self.assertEqual(len(changed), 5)

    def test_audio_survives_new_cache_instance(self):
        with TemporaryDirectory() as temporary_directory:
            first = PersistentAudioCache(temporary_directory)
            first.put("key", np.array([0.1, -0.1], dtype=np.float32))

            second = PersistentAudioCache(temporary_directory)
            audio = second.get("key")

        np.testing.assert_allclose(audio, [0.1, -0.1])

    def test_stereo_audio_survives_new_cache_instance(self):
        with TemporaryDirectory() as temporary_directory:
            first = PersistentAudioCache(temporary_directory)
            expected = np.array([[0.1, -0.1], [0.2, -0.2]], dtype=np.float32)
            first.put("stereo", expected)

            second = PersistentAudioCache(temporary_directory)
            audio = second.get("stereo")

        np.testing.assert_allclose(audio, expected)

    def test_prunes_oldest_entries_and_ignores_corruption(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache = PersistentAudioCache(root, max_entries=2)
            with patch("vntts.audio_cache.time_ns", return_value=1_000_000_000):
                cache.put("one", np.array([0.1], dtype=np.float32))
                cache.put("two", np.array([0.2], dtype=np.float32))
                cache.put("three", np.array([0.3], dtype=np.float32))
            (root / "three.npy").write_bytes(b"corrupt")

            files = sorted(path.stem for path in root.glob("*.npy"))

        self.assertEqual(files, ["three", "two"])
        self.assertIsNone(cache.get("three"))


if __name__ == "__main__":
    unittest.main()
