import os
import struct
import unittest
from collections import OrderedDict
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread, current_thread
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

from tests.symlink_support import symlink_or_skip
from vntts.audio_cache import BoundedCache, PersistentAudioCache
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    MossTTSVoiceRouterBackend,
    PocketTTSVoiceRouterBackend,
)
from vntts.speech_backend_runtime import SpeechCacheKeyFactory
from vntts.synthesis import SynthesisCachePolicy, SynthesisRequest
from vntts.voices import CharacterVoiceRegistry


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
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(directory, sample_rate=16_000)
            base = {
                "backend": "pocket",
                "model": "2.1",
                "voice": "selone",
                "text": "Hello   world.",
                "settings": {"speed": 1.0},
            }
            first = cache.key(**base)
            self.assertEqual(first, cache.key(**{**base, "text": "Hello world."}))
            changed = {
                cache.key(**{**base, "backend": "chatterbox"}),
                cache.key(**{**base, "model": "2.2"}),
                cache.key(**{**base, "voice": "fatutu"}),
                cache.key(**{**base, "text": "Goodbye world."}),
                cache.key(**{**base, "settings": {"speed": 1.1}}),
            }
            self.assertNotIn(first, changed)
            self.assertEqual(len(changed), 5)

    def test_final_wav_survives_restart_without_npy_or_temporary_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = PersistentAudioCache(root, sample_rate=24_000)
            audio = np.array([-2.0, -0.1, 0.0, 0.1, 2.0], dtype=np.float32)
            path = cache.put("line", audio)
            self.assertIsNotNone(path)
            self.assertEqual(path.suffix, ".wav")
            info = sf.info(path)
            self.assertEqual(
                (info.samplerate, info.channels, info.subtype), (24_000, 1, "PCM_16")
            )
            restored = PersistentAudioCache(root, sample_rate=24_000).get("line")
            expected, _ = sf.read(path, dtype="float32")
            np.testing.assert_array_equal(restored, expected)
            np.testing.assert_allclose(restored, np.clip(audio, -1, 1), atol=1 / 32768)
            self.assertEqual(list(root.glob("*.npy")), [])
            self.assertEqual(list(root.glob("tmp*.wav")), [])

    def test_stereo_and_single_stereo_frame_preserve_channel_shape(self):
        for audio in (
            np.array([[0.125, -0.125], [0.25, -0.25]], dtype=np.float32),
            np.array([[0.125, -0.125]], dtype=np.float32),
        ):
            with self.subTest(shape=audio.shape), TemporaryDirectory() as directory:
                first = PersistentAudioCache(directory, sample_rate=16_000)
                first.put("stereo", audio)
                restored = PersistentAudioCache(directory, sample_rate=16_000).get(
                    "stereo"
                )
                np.testing.assert_array_equal(restored, audio)

    def test_positive_legacy_limit_never_evicts_durable_waveforms(self):
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(directory, sample_rate=16_000, max_entries=1)
            for number in range(4):
                self.assertIsNotNone(cache.put(str(number), np.array([number / 8])))
            for number in range(4):
                np.testing.assert_array_equal(cache.get(str(number)), [number / 8])

    def test_zero_limit_disables_reads_and_writes(self):
        with TemporaryDirectory() as directory:
            enabled = PersistentAudioCache(directory, sample_rate=16_000)
            enabled.put("line", np.array([0.125]))
            disabled = PersistentAudioCache(
                directory, sample_rate=16_000, max_entries=0
            )
            self.assertIsNone(disabled.get("line"))
            self.assertIsNone(disabled.put("new", np.array([0.25])))
            self.assertIsNone(enabled.get("new"))

    def test_wrong_sample_rate_is_a_miss(self):
        with TemporaryDirectory() as directory:
            PersistentAudioCache(directory, sample_rate=16_000).put(
                "line", np.array([0.125])
            )
            self.assertIsNone(
                PersistentAudioCache(directory, sample_rate=24_000).get("line")
            )

    def test_invalid_audio_preserves_existing_entry(self):
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(directory, sample_rate=16_000)
            cache.put("line", np.array([0.125]))
            for invalid in ([], [np.nan], [np.inf], object(), np.zeros((2, 2, 2))):
                with self.subTest(invalid=repr(invalid)):
                    self.assertIsNone(cache.put("line", invalid))
                    np.testing.assert_array_equal(cache.get("line"), [0.125])

    def test_keys_cannot_escape_cache_directory(self):
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(Path(directory) / "cache", sample_rate=16_000)
            for key in ("../outside", "", 3, "a/b", "a\\b"):
                self.assertIsNone(cache.put(key, [0.125]))
                self.assertIsNone(cache.get(key))
            self.assertFalse((Path(directory) / "outside.wav").exists())

    def test_corrupt_and_symlinked_waveforms_are_misses(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink), TemporaryDirectory() as directory:
                cache = PersistentAudioCache(directory, sample_rate=16_000)
                path = cache.put("line", np.array([0.125]))
                if symlink:
                    outside = Path(directory) / "outside.wav"
                    outside.write_bytes(path.read_bytes())
                    path.unlink()
                    symlink_or_skip(path, outside)
                else:
                    path.write_bytes(b"corrupt")
                self.assertIsNone(cache.get("line"))

    def test_symlinked_store_root_is_refused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            alias = root / "alias"
            symlink_or_skip(alias, target, target_is_directory=True)
            with self.assertRaises(OSError):
                PersistentAudioCache(alias, sample_rate=16_000)
            self.assertEqual(list(target.iterdir()), [])

    def test_failed_publish_cleans_temporary_wav(self):
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(directory, sample_rate=16_000)
            with patch.object(cache.store, "put", side_effect=OSError("full disk")):
                self.assertIsNone(cache.put("line", [0.125]))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_legacy_audio_migrates_after_successful_durable_publish(self):
        for audio in (np.array([0.125, -0.125]), np.array([[0.125, -0.125]])):
            with self.subTest(shape=audio.shape), TemporaryDirectory() as directory:
                root = Path(directory)
                legacy = root / "line.npy"
                np.save(legacy, audio, allow_pickle=False)
                cache = PersistentAudioCache(root, sample_rate=16_000)
                np.testing.assert_array_equal(cache.get("line"), audio)
                self.assertFalse(legacy.exists())
                self.assertIsNotNone(cache.store.get("line"))
                np.testing.assert_array_equal(
                    PersistentAudioCache(root, sample_rate=16_000).get("line"), audio
                )

    def test_failed_migration_preserves_legacy_bytes(self):
        with TemporaryDirectory() as directory:
            legacy = Path(directory) / "line.npy"
            np.save(legacy, np.array([0.125]), allow_pickle=False)
            before = legacy.read_bytes()
            cache = PersistentAudioCache(directory, sample_rate=16_000)
            with patch.object(cache.store, "put", side_effect=OSError("full disk")):
                self.assertIsNone(cache.get("line"))
            self.assertEqual(legacy.read_bytes(), before)
            self.assertEqual(list(Path(directory).glob("*.wav")), [])

    def test_migration_preserves_substituted_legacy_entry(self):
        with TemporaryDirectory() as directory:
            legacy = Path(directory) / "line.npy"
            np.save(legacy, np.array([0.125]), allow_pickle=False)
            cache = PersistentAudioCache(directory, sample_rate=16_000)
            publish = cache.store.put

            def substitute(*arguments):
                result = publish(*arguments)
                replacement = Path(directory) / "replacement.npy"
                np.save(replacement, np.array([0.25]), allow_pickle=False)
                replacement.replace(legacy)
                return result

            with patch.object(cache.store, "put", side_effect=substitute):
                np.testing.assert_array_equal(cache.get("line"), [0.125])
            np.testing.assert_array_equal(np.load(legacy), [0.25])

    def test_legacy_symlinks_and_nonregular_files_are_misses_before_parsing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = PersistentAudioCache(root, sample_rate=16_000)
            outside = root / "outside.npy"
            np.save(outside, np.array([0.125]), allow_pickle=False)
            symlink_or_skip(root / "link.npy", outside)
            (root / "folder.npy").mkdir()
            entries = ["link", "folder"]
            if hasattr(os, "mkfifo"):
                os.mkfifo(root / "fifo.npy")
                entries.append("fifo")
            with patch("vntts.audio_cache._read_cached_array") as parse:
                for key in entries:
                    self.assertIsNone(cache.get(key))
                parse.assert_not_called()
            self.assertTrue(outside.exists())

    def test_malformed_and_unsafe_legacy_arrays_are_misses_without_deletion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = PersistentAudioCache(root, sample_rate=16_000)
            for invalid in (
                np.array([np.nan]),
                np.array([object()]),
                np.zeros((2, 2, 2)),
            ):
                np.save(root / "invalid.npy", invalid, allow_pickle=True)
                before = (root / "invalid.npy").read_bytes()
                self.assertIsNone(cache.get("invalid"))
                self.assertEqual((root / "invalid.npy").read_bytes(), before)
            for corrupt in (b"", b"corrupt", b"\x93NUMPY\x01\x00\xff\xff"):
                (root / "corrupt.npy").write_bytes(corrupt)
                self.assertIsNone(cache.get("corrupt"))
                self.assertEqual((root / "corrupt.npy").read_bytes(), corrupt)

    def test_declared_legacy_body_is_refused_before_array_allocation(self):
        with TemporaryDirectory() as directory:
            legacy = Path(directory) / "huge.npy"
            header = repr(
                {"descr": "<f4", "fortran_order": False, "shape": (2**50,)}
            ).encode()
            legacy.write_bytes(
                b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header)) + header
            )
            with patch("vntts.audio_cache.np.fromfile") as allocate:
                self.assertIsNone(
                    PersistentAudioCache(directory, sample_rate=16_000).get("huge")
                )
                allocate.assert_not_called()
            self.assertTrue(legacy.exists())

    def test_malformed_npy_headers_are_misses_without_changing_entries(self):
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(directory, sample_rate=16_000)
            path = Path(directory) / "corrupt.npy"
            for version in ((1, 0), (2, 0), (3, 0)):
                with self.subTest(version=version, damage="shape overflow"):
                    with path.open("wb") as destination:
                        np.lib.format.write_array_header_2_0(
                            destination,
                            {
                                "descr": "<f4",
                                "fortran_order": False,
                                "shape": (2**100,),
                            },
                        )
                    if version != (2, 0):
                        payload = path.read_bytes()
                        header = payload[12:]
                        path.write_bytes(
                            np.lib.format.magic(*version)
                            + struct.pack(
                                "<H" if version == (1, 0) else "<I", len(header)
                            )
                            + header
                        )
                    original = path.read_bytes()
                    with patch.object(cache.store, "put") as publish:
                        self.assertIsNone(cache.get("corrupt"))
                        publish.assert_not_called()
                    self.assertEqual(path.read_bytes(), original)
                if version != (3, 0):
                    with self.subTest(version=version, damage="unterminated tokens"):
                        header = b"(\n"
                        path.write_bytes(
                            np.lib.format.magic(*version)
                            + struct.pack(
                                "<H" if version == (1, 0) else "<I", len(header)
                            )
                            + header
                        )
                        original = path.read_bytes()
                        self.assertIsNone(cache.get("corrupt"))
                        self.assertEqual(path.read_bytes(), original)
            expected = np.array([0.125, -0.125], dtype=np.float32)
            cache.put("corrupt", expected)
            np.testing.assert_array_equal(cache.get("corrupt"), expected)

    def test_declared_npy_body_is_refused_before_array_allocation(self):
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(directory, sample_rate=16_000)
            path = Path(directory) / "declared.npy"
            for version in ((1, 0), (2, 0), (3, 0)):
                for shape, descriptor, data in (
                    ((2**40,), "<f4", b""),
                    ((1,), "|S10000000", b""),
                    ((-1,), "<f4", b"\0" * 16),
                    ((2,), "<f4", b"\0" * 4),
                    ((1,), "|O", b""),
                ):
                    with self.subTest(
                        version=version, shape=shape, descriptor=descriptor
                    ):
                        with path.open("wb") as output:
                            np.lib.format.write_array_header_2_0(
                                output,
                                {
                                    "descr": descriptor,
                                    "fortran_order": False,
                                    "shape": shape,
                                },
                            )
                            output.write(data)
                        if version != (2, 0):
                            raw = path.read_bytes()
                            length = struct.unpack_from("<I", raw, 8)[0]
                            header = raw[12 : 12 + length]
                            path.write_bytes(
                                np.lib.format.magic(*version)
                                + struct.pack(
                                    "<H" if version == (1, 0) else "<I", length
                                )
                                + header
                                + raw[12 + length :]
                            )
                        before = path.read_bytes()
                        with (
                            patch(
                                "vntts.audio_cache.np.fromfile",
                                return_value=np.empty(0, dtype=np.float32),
                            ) as allocate,
                            patch.object(cache.store, "put") as publish,
                        ):
                            self.assertIsNone(cache.get("declared"))
                            allocate.assert_not_called()
                            publish.assert_not_called()
                        self.assertEqual(path.read_bytes(), before)
            expected = np.array([0.125, -0.125], dtype=np.float32)
            cache.put("declared", expected)
            np.testing.assert_array_equal(cache.get("declared"), expected)

    def test_legacy_body_truncated_after_extent_check_is_preserved(self):
        with TemporaryDirectory() as directory:
            legacy = Path(directory) / "truncated.npy"
            audio = np.array([0.125, -0.125], dtype=np.float32)
            np.save(legacy, audio, allow_pickle=False)
            header = legacy.read_bytes()[: -audio.nbytes]
            original_fromfile = np.fromfile

            def truncate(source, **arguments):
                legacy.write_bytes(header)
                return original_fromfile(source, **arguments)

            cache = PersistentAudioCache(directory, sample_rate=16_000)
            with patch("vntts.audio_cache.np.fromfile", side_effect=truncate) as read:
                self.assertIsNone(cache.get("truncated"))
                read.assert_called_once()
            self.assertEqual(legacy.read_bytes(), header)
            self.assertIsNone(cache.store.get("truncated"))

    def test_historical_npy_versions_layouts_and_dtypes_migrate(self):
        arrays = (
            np.array([[0.125, 0.25], [0.375, 0.5]], dtype=">f4"),
            np.asfortranarray(np.arange(6, dtype=np.float64).reshape(3, 2) / 8),
            np.array(["0.125", "0.25"], dtype="U5"),
            np.array(0.25, dtype=np.float32),
        )
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(directory, sample_rate=16_000)
            for version in ((1, 0), (2, 0), (3, 0)):
                for number, original in enumerate(arrays):
                    with self.subTest(version=version, array=number):
                        key = f"historical-{version[0]}-{number}"
                        legacy = Path(directory) / f"{key}.npy"
                        with legacy.open("wb") as output:
                            np.lib.format.write_array(
                                output, original, version=version, allow_pickle=False
                            )
                        loaded = cache.get(key)
                        normalized = np.atleast_1d(original).astype(np.float32)
                        np.testing.assert_array_equal(loaded, normalized)
                        self.assertFalse(legacy.exists())
                        self.assertEqual(loaded.dtype, np.float32)


class SynthesisKeyTest(unittest.TestCase):
    def test_request_identity_ignores_storage_policy_without_loading_conditioning(self):
        for backend_type in (
            ChatterboxNanoVoiceRouterBackend,
            PocketTTSVoiceRouterBackend,
            MossTTSVoiceRouterBackend,
        ):
            with (
                self.subTest(backend=backend_type.name),
                TemporaryDirectory() as directory,
            ):
                backend = object.__new__(backend_type)
                backend.registry = CharacterVoiceRegistry()
                backend.narrator_reference = "alba"
                backend.speed = 1.0
                backend.language = "English"
                backend.generation_profile = (
                    "stable" if backend_type is MossTTSVoiceRouterBackend else "default"
                )
                backend.generation_options = {"temperature": 0.8}
                backend.streaming_first_chunk_frames = 4
                backend.streaming_interval = 0.25
                backend.persistent_audio_cache = PersistentAudioCache(
                    directory, sample_rate=16_000, max_entries=0
                )
                backend.persistent_cache_keys = SpeechCacheKeyFactory(
                    backend.persistent_audio_cache,
                    backend=backend_type.name,
                    model=object(),
                    model_identity="fixture-model",
                    sample_rate=16_000,
                )
                backend.model = Mock()
                backend._resolve_voice_state = Mock(
                    side_effect=AssertionError("loaded voice state")
                )
                backend._resolve_prompt_codes = Mock(
                    side_effect=AssertionError("loaded prompt codes")
                )
                if backend_type is not ChatterboxNanoVoiceRouterBackend:
                    backend._resolve_voice_source = Mock(
                        return_value=("narrator", Path(directory) / "reference.wav")
                    )
                request = SynthesisRequest(
                    voice="Narrator",
                    text="  Hello   world.  ",
                    generation_profile=backend.generation_profile,
                    cache_policy=SynthesisCachePolicy.BYPASS,
                )
                first = backend.synthesis_key(request)
                backend.persistent_audio_cache.max_entries = 20
                second = backend.synthesis_key(
                    SynthesisRequest(
                        voice=request.voice,
                        text="Hello world.",
                        generation_profile=backend.generation_profile,
                        cache_policy=SynthesisCachePolicy.USE,
                    )
                )
                self.assertEqual(first, second)
                backend.speed = 1.1
                self.assertNotEqual(first, backend.synthesis_key(request))
                backend.model.generate.assert_not_called()
                backend._resolve_voice_state.assert_not_called()
                backend._resolve_prompt_codes.assert_not_called()
                self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
