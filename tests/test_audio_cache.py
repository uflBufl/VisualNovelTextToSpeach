import unittest
from pathlib import Path
from stat import S_IFREG
from tempfile import TemporaryDirectory
from types import SimpleNamespace
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

    def test_invalid_audio_is_rejected_on_write_and_read(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache = PersistentAudioCache(root)
            invalid = np.array([0.1, np.nan], dtype=np.float32)
            self.assertIsNone(cache.put("invalid", invalid))
            self.assertFalse((root / "invalid.npy").exists())
            with (root / "invalid.npy").open("wb") as destination:
                np.save(destination, invalid, allow_pickle=False)
            self.assertIsNone(cache.get("invalid"))

    def test_cache_works_when_no_follow_utime_is_unavailable(self):
        with TemporaryDirectory() as temporary_directory:
            cache = PersistentAudioCache(temporary_directory)
            with patch("vntts.audio_cache.os.utime", side_effect=NotImplementedError):
                path = cache.put("windows", np.array([0.1, -0.1], dtype=np.float32))
                audio = cache.get("windows")

        self.assertIsNotNone(path)
        np.testing.assert_allclose(audio, [0.1, -0.1])

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
            self.assertIsNone(cache.get("three"))
            (root / "three.npy").write_bytes(b"")
            self.assertIsNone(cache.get("three"))

        self.assertEqual(files, ["three", "two"])

    def test_prunes_by_creation_time_when_modified_times_tie(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache = PersistentAudioCache(root, max_entries=2)
            for index, key in enumerate(("one", "two", "three"), start=1):
                with (root / f"{key}.npy").open("wb") as destination:
                    np.save(destination, np.array([index], dtype=np.float32))
            creation_times = {"one": 1, "two": 2, "three": 3}

            def tied_modified_times(path, *, follow_symlinks=True):
                return SimpleNamespace(
                    st_mode=S_IFREG,
                    st_mtime_ns=0,
                    st_ctime_ns=creation_times[path.stem],
                )

            with patch(
                "vntts.audio_cache.Path.stat",
                autospec=True,
                side_effect=tied_modified_times,
            ):
                cache._prune()

            files = sorted(path.stem for path in root.glob("*.npy"))

        self.assertEqual(files, ["three", "two"])

    def test_keys_cannot_escape_cache_directory(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache = PersistentAudioCache(root / "cache")
            outside = root / "outside.npy"

            self.assertIsNone(cache.put("../outside", np.array([0.1])))
            self.assertIsNone(cache.get("../outside"))

            self.assertFalse(outside.exists())

    def test_cache_reads_do_not_follow_symlinks(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "cache"
            directory.mkdir()
            outside = root / "outside.npy"
            with outside.open("wb") as destination:
                np.save(destination, np.array([0.1], dtype=np.float32))
            (directory / "linked.npy").symlink_to(outside)

            self.assertIsNone(PersistentAudioCache(directory).get("linked"))

    def test_stray_symlink_does_not_break_cache_or_count_toward_limit(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cache = PersistentAudioCache(root, max_entries=2)
            cache.put("one", np.array([0.1], dtype=np.float32))
            (root / "broken.npy").symlink_to(root / "missing.npy")

            np.testing.assert_allclose(cache.get("one"), [0.1])
            self.assertIsNotNone(cache.put("two", np.array([0.2], dtype=np.float32)))
            self.assertIsNotNone(cache.put("three", np.array([0.3], dtype=np.float32)))

            self.assertEqual(
                {path.stem for path in root.glob("*.npy") if not path.is_symlink()},
                {"two", "three"},
            )


if __name__ == "__main__":
    unittest.main()
