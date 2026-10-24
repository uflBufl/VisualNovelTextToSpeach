import subprocess
import sys
import textwrap
import unittest
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread, current_thread
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

from tests.symlink_support import symlink_or_skip
from vntts.audio_cache import BoundedCache, PersistentAudioCache
from vntts.audio_store import AudioCatalogError
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
    def test_worker_import_and_persistent_cache_work_without_soundfile(self):
        code = textwrap.dedent("""            import sys
            from io import BytesIO
            from tempfile import TemporaryDirectory
            import numpy as np
            sys.path.insert(0, sys.argv[1])
            sys.modules["soundfile"] = None
            from vntts.audio_cache import PersistentAudioCache
            from vntts.speech_worker import worker_main
            assert worker_main(input_stream=BytesIO(), output_stream=BytesIO()) == 2
            with TemporaryDirectory() as directory:
                cache = PersistentAudioCache(directory, sample_rate=16000)
                assert cache.put("saved-key", np.array([0.125, -0.125])) is not None
                restored = PersistentAudioCache(directory, sample_rate=16000)
                np.testing.assert_array_equal(restored.get("saved-key"), [0.125, -0.125])
            """)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                code,
                str(Path(__file__).resolve().parents[1]),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pcm16_quantization_matches_existing_soundfile_encoding(self):
        boundaries = np.array(
            [
                -2,
                -1,
                -0.9,
                -1.5 / 32768,
                -0.5 / 32768,
                0,
                0.5 / 32768,
                1.5 / 32768,
                0.9,
                1,
                2,
            ],
            dtype=np.float32,
        )
        random = (
            np.random.default_rng(3)
            .uniform(-1.1, 1.1, size=(1000, 2))
            .astype(np.float32)
        )
        for audio in (boundaries, boundaries.reshape(-1, 1), random):
            with self.subTest(shape=audio.shape), TemporaryDirectory() as directory:
                reference = BytesIO()
                sf.write(reference, audio, 16000, format="WAV", subtype="PCM_16")
                reference.seek(0)
                expected, _ = sf.read(reference, dtype="float32")
                cache = PersistentAudioCache(directory, sample_rate=16000)
                self.assertIsNotNone(cache.put("exact", audio))
                np.testing.assert_array_equal(cache.get("exact"), expected)

    def test_invalid_sample_rates_are_rejected_before_storage(self):
        with TemporaryDirectory() as directory:
            for rate in (0, -1, True, 16000.0, "16000", None):
                with self.subTest(rate=rate), self.assertRaises(ValueError):
                    PersistentAudioCache(directory, sample_rate=rate)
            self.assertEqual(list(Path(directory).iterdir()), [])

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
            self.assertEqual(first, "7097c9a017baff1dba82c00032f2ecd779d3bb5e84cee68c")
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

    def test_wrong_sample_rate_requires_restoring_correct_authority(self):
        with TemporaryDirectory() as directory:
            PersistentAudioCache(directory, sample_rate=16_000).put(
                "line", np.array([0.125])
            )
            with self.assertRaises(AudioCatalogError):
                PersistentAudioCache(directory, sample_rate=24_000).get("line")

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

    def test_corrupt_missing_and_symlinked_registered_waveforms_fail_closed(self):
        for failure in ("corrupt", "missing", "symlink"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                cache = PersistentAudioCache(directory, sample_rate=16_000)
                path = cache.put("line", np.array([0.125]))
                if failure == "symlink":
                    outside = Path(directory) / "outside.wav"
                    outside.write_bytes(path.read_bytes())
                    path.unlink()
                    symlink_or_skip(path, outside)
                elif failure == "missing":
                    path.unlink()
                else:
                    path.write_bytes(b"corrupt")
                with self.assertRaises(AudioCatalogError):
                    cache.get("line")

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
            self.assertEqual(list(cache.store.iter_bindings()), [])
            self.assertEqual(list(Path(directory).glob("tmp*.wav")), [])

    def test_unregistered_store_requires_upgrade_without_opening_array(self):
        with TemporaryDirectory() as directory:
            saved = Path(directory) / "paid.npy"
            saved.write_bytes(b"preserve original samples")
            cache = PersistentAudioCache(directory, sample_rate=16_000)
            with patch("numpy.load", side_effect=AssertionError("legacy read")):
                with self.assertRaises(AudioCatalogError):
                    cache.get("paid")
                with self.assertRaises(AudioCatalogError):
                    cache.put("other", [0.125])
            self.assertEqual(saved.read_bytes(), b"preserve original samples")

    def test_sqlite_lookup_ignores_poisoned_legacy_decoys(self):
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(directory, sample_rate=16_000)
            saved = cache.put("paid", [0.125])
            before = saved.stat()
            (Path(directory) / "paid.npy").write_bytes(b"poison")
            with patch("numpy.load", side_effect=AssertionError("legacy read")):
                np.testing.assert_array_equal(
                    PersistentAudioCache(directory, sample_rate=16_000).get("paid"),
                    [0.125],
                )
            self.assertEqual(saved.stat().st_ino, before.st_ino)
            cache.store.catalog_path.unlink()
            with self.assertRaises(AudioCatalogError):
                cache.get("paid")


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
