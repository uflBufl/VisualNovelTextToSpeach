import os
import unittest
from collections import OrderedDict
from pathlib import Path
from stat import S_IFREG
from tempfile import TemporaryDirectory
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from vntts.audio_cache import BoundedCache, PersistentAudioCache


class BoundedCacheTest(unittest.TestCase):
    def test_read_keeps_its_value_until_concurrent_eviction_or_clear(self):
        for clear in (False, True):
            with self.subTest(clear=clear):
                moved, release = Event(), Event()
                writer_started, writer_finished = Event(), Event()
                returned, errors = [], []
                reader = None

                class PausingValues(OrderedDict):
                    def move_to_end(values, key):
                        super().move_to_end(key)
                        if current_thread() is reader:
                            moved.set()
                            if not release.wait(3):
                                raise TimeoutError("reader was not released")

                cache = BoundedCache(1)
                cache._values = PausingValues()
                cache.put("one", 1)

                def read():
                    try:
                        returned.append(cache.get("one"))
                    except Exception as error:
                        errors.append(error)

                mutate = cache.clear if clear else lambda: cache.put("two", 2)

                def write():
                    writer_started.set()
                    try:
                        mutate()
                    except Exception as error:
                        errors.append(error)
                    finally:
                        writer_finished.set()

                reader = Thread(target=read)
                writer = Thread(target=write)
                reader.start()
                try:
                    self.assertTrue(moved.wait(2))
                    writer.start()
                    self.assertTrue(writer_started.wait(2))
                    self.assertFalse(writer_finished.wait(0.1))
                finally:
                    release.set()
                    reader.join(2)
                    if writer.ident is not None:
                        writer.join(2)
                self.assertFalse(reader.is_alive())
                self.assertFalse(writer.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(returned, [1])
                self.assertIsNone(cache.get("one"))
                self.assertEqual(cache.get("two"), None if clear else 2)

    def test_eviction_allows_reentrant_value_cleanup(self):
        cache = BoundedCache(1)
        observed = []

        class Value:
            def __del__(self):
                observed.append(cache.get("two"))

        cache.put("one", Value())
        cache.put("two", 2)

        self.assertEqual(observed, [2])


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

    def test_single_stereo_frame_keeps_its_channel_shape(self):
        with TemporaryDirectory() as temporary_directory:
            cache = PersistentAudioCache(temporary_directory)
            expected = np.array([[0.1, -0.1]], dtype=np.float32)
            cache.put("stereo", expected)

            restored = PersistentAudioCache(temporary_directory).get("stereo")

        self.assertEqual(restored.shape, (1, 2))
        np.testing.assert_array_equal(restored, expected)

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

    def test_archives_with_numeric_names_are_not_waveforms(self):
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(directory)
            path = Path(directory) / "archive.npy"
            with path.open("wb") as destination:
                np.savez(destination, **{"1": np.array([0.1]), "2": np.array([-0.1])})
            original = path.read_bytes()

            self.assertIsNone(cache.get("archive"))
            self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO admission")
    def test_special_entries_are_misses_before_parsing_and_recency_updates(self):
        native_open, native_path_open = os.open, Path.open
        for swap in (False, True):
            with self.subTest(swap=swap), TemporaryDirectory() as directory:
                cache = PersistentAudioCache(directory)
                expected = np.array([0.1, -0.1], dtype=np.float32)
                path = cache.put("entry", expected)
                original = path.read_bytes()
                fifo = Path(directory) / "replacement"
                os.mkfifo(fifo)
                if not swap:
                    fifo.replace(path)
                descriptors = []

                def admit(pathname, flags):
                    self.assertEqual(Path(pathname), path)
                    self.assertTrue(
                        flags & os.O_NONBLOCK, "cache must not wait for a writer"
                    )
                    if swap:
                        fifo.replace(path)
                    descriptor = native_open(pathname, flags)
                    descriptors.append(descriptor)
                    return descriptor

                def reject_blocking_path_open(candidate, *args, **kwargs):
                    if candidate == path:
                        self.fail("cache entry must not use blocking Path.open")
                    return native_path_open(candidate, *args, **kwargs)

                with (
                    patch("vntts.path_safety.os.open", side_effect=admit),
                    patch.object(Path, "open", reject_blocking_path_open),
                    patch("vntts.audio_cache.np.lib.format.read_array") as parse,
                    patch.object(cache, "_touch_newest") as touch,
                ):
                    self.assertIsNone(cache.get("entry"))
                    parse.assert_not_called()
                    touch.assert_not_called()
                self.assertEqual(len(descriptors), 1)
                with self.assertRaises(OSError):
                    os.fstat(descriptors[0])
                path.unlink()
                path.write_bytes(original)
                np.testing.assert_array_equal(cache.get("entry"), expected)

    def test_uncacheable_audio_preserves_the_existing_entry(self):
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(directory)
            expected = np.array([0.1, -0.1], dtype=np.float32)
            path = cache.put("existing", expected)
            original = path.read_bytes()
            for value in ("not audio", [[0.1], [0.2, 0.3]], {}, [10**1000]):
                with self.subTest(value_type=type(value).__name__):
                    self.assertIsNone(cache.put("existing", value))
                    self.assertEqual(path.read_bytes(), original)
                    np.testing.assert_array_equal(cache.get("existing"), expected)

    def test_recency_update_failure_does_not_hide_valid_audio(self):
        with TemporaryDirectory() as temporary_directory:
            cache = PersistentAudioCache(temporary_directory)
            expected = np.array([0.1, -0.1], dtype=np.float32)
            cache.put("cached", expected)

            with patch("vntts.audio_cache.os.utime", side_effect=PermissionError):
                cached = cache.get("cached")
                written = cache.put("new", expected)

            np.testing.assert_allclose(cached, expected)
            self.assertEqual(written, cache.directory / "new.npy")
            np.testing.assert_allclose(cache.get("new"), expected)

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
