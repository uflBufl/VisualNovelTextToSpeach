import hashlib
import io
import os
import subprocess
import sys
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import soundfile as sf
from vntts_artifacts.audio import write_pcm16_wav

from tests.symlink_support import symlink_or_skip
from vntts.audio_cache import PersistentAudioCache
from vntts.audio_store import AudioCatalogError, SharedAudioStore, SharedAudioStoreError
from vntts.path_safety import open_regular_binary
from vntts.pregeneration_queue import (
    PregenerationQueueError,
    _normalize_reference_wav,
    _write_effective_voices,
)
from vntts.voices import CharacterVoice


def materialize(root, source, expected, job):
    staging = root / "jobs" / job / ".generation-input"
    staging.mkdir(parents=True)
    voice = CharacterVoice(character="voice", speaker="voice", reference=source)
    entries = _write_effective_voices(
        staging,
        {
            "routes": {"voice": (voice, (expected,), "voice")},
            "narrator_roles": (),
            "line_voice_characters": {},
        },
    )
    return staging / entries[0]["references"][0], entries


class SharedReferenceAudioTest(unittest.TestCase):
    def source(self, root):
        source = root / "raw.wav"
        sf.write(source, [[0.25, -0.125], [0.75, 0.125]], 16000, subtype="FLOAT")
        return source, hashlib.sha256(source.read_bytes()).hexdigest()

    def test_reopened_inputs_normalize_once_and_keep_identical_controls_and_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, expected = self.source(root)
            original = source.read_bytes()
            with patch(
                "vntts.pregeneration_queue._normalize_reference_wav",
                wraps=_normalize_reference_wav,
            ) as normalize:
                first, controls = materialize(root, source, expected, "first")
                second, reopened_controls = materialize(
                    root, source, expected, "second"
                )
            self.assertEqual(normalize.call_count, 1)
            self.assertEqual(controls, reopened_controls)
            self.assertEqual(controls[0]["references"], [f"references/{expected}.wav"])
            samples, rate = sf.read(
                io.BytesIO(original), dtype="float32", always_2d=True
            )
            previous = root / "previous.wav"
            write_pcm16_wav(previous, samples.mean(axis=1, dtype=np.float64), int(rate))
            self.assertEqual(first.read_bytes(), previous.read_bytes())
            self.assertTrue(first.samefile(second))
            self.assertEqual(source.read_bytes(), original)
            store = SharedAudioStore(root / "jobs" / "audio-store")
            self.assertTrue(first.samefile(store.get(f"reference-pcm16-v1-{expected}")))
            self.assertEqual(len(list((store.directory / "objects").glob("*.wav"))), 1)

    def test_changed_source_is_checked_even_when_reference_is_saved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, expected = self.source(root)
            first, _ = materialize(root, source, expected, "first")
            original, inode = first.read_bytes(), first.stat().st_ino
            sf.write(source, [0.125, 0.5], 16000, subtype="FLOAT")
            with self.assertRaisesRegex(PregenerationQueueError, "reference changed"):
                materialize(root, source, expected, "stale")
            changed = hashlib.sha256(source.read_bytes()).hexdigest()
            second, _ = materialize(root, source, changed, "changed")
            self.assertNotEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(
                (first.read_bytes(), first.stat().st_ino), (original, inode)
            )

    def test_corrupt_saved_reference_fails_without_replacing_previous_input(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, expected = self.source(root)
            first, _ = materialize(root, source, expected, "first")
            original, inode = first.read_bytes(), first.stat().st_ino
            store = SharedAudioStore(root / "jobs" / "audio-store")
            saved = store.get(f"reference-pcm16-v1-{expected}")
            saved.unlink()
            saved.write_bytes(b"corrupt")
            with self.assertRaises(SharedAudioStoreError):
                materialize(root, source, expected, "second")
            self.assertEqual(
                (first.read_bytes(), first.stat().st_ino), (original, inode)
            )

    def test_reference_guard_symlink_is_rejected_without_touching_its_target(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, expected = self.source(root)
            first, _ = materialize(root, source, expected, "first")
            original = first.read_bytes()
            guard = root / "jobs/audio-store/.reference-normalization.guard"
            guard.unlink()
            outside = root / "outside"
            outside.write_bytes(b"unrelated")
            symlink_or_skip(guard, outside)
            with self.assertRaises(OSError):
                materialize(root, source, expected, "second")
            self.assertEqual(outside.read_bytes(), b"unrelated")
            self.assertEqual(first.read_bytes(), original)

    def test_reference_link_failure_preserves_shared_and_previous_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, expected = self.source(root)
            first, _ = materialize(root, source, expected, "first")
            original = first.read_bytes()
            with patch(
                "vntts.audio_store.os.link", side_effect=OSError("cross-device")
            ):
                with self.assertRaises(SharedAudioStoreError):
                    materialize(root, source, expected, "second")
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual(
                len(list((root / "jobs/audio-store/objects").glob("*.wav"))), 1
            )

    def test_concurrent_inputs_normalize_once_and_share_the_same_inode(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, expected = self.source(root)
            script = """
from pathlib import Path
import sys
from unittest.mock import patch
from tests.test_shared_reference_audio import materialize
from vntts.pregeneration_queue import _normalize_reference_wav
root, source, expected, job = sys.argv[1:]
def normalize(*args):
    with (Path(root) / "normalizations").open("a") as witness:
        witness.write("normalized\\n")
    _normalize_reference_wav(*args)
with patch("vntts.pregeneration_queue._normalize_reference_wav", side_effect=normalize):
    materialize(Path(root), Path(source), expected, job)
"""
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(root),
                        str(source),
                        expected,
                        str(i),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for i in range(4)
            ]
            for process in processes:
                _, errors = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, errors.decode())
            self.assertEqual((root / "normalizations").read_text(), "normalized\n")
            recordings = list(
                (root / "jobs").glob("*/.generation-input/references/*.wav")
            )
            self.assertEqual(len(recordings), 4)
            self.assertTrue(all(path.samefile(recordings[0]) for path in recordings))


class AudioSnapshotTest(unittest.TestCase):
    def test_store_put_reads_twice_and_cache_hit_reads_one_verified_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            write_pcm16_wav(source, [0.125, 0.25], 16000)
            store = SharedAudioStore(root / "store")
            with patch(
                "vntts.audio_store.open_regular_binary", wraps=open_regular_binary
            ) as read:
                store.put("key", source)
            self.assertEqual(read.call_count, 2)
            cache = PersistentAudioCache(store.directory, sample_rate=16000)
            with (
                patch(
                    "vntts.audio_store.open_regular_binary", wraps=open_regular_binary
                ) as read,
                patch("vntts.audio_cache.wave.open", wraps=wave.open) as decode,
            ):
                np.testing.assert_array_equal(cache.get("key"), [0.125, 0.25])
            self.assertEqual(read.call_count, 1)
            # The shared decoder validates store bytes, then decodes that snapshot.
            self.assertEqual(decode.call_count, 2)
            for call in decode.call_args_list:
                self.assertIsInstance(call.args[0], io.BytesIO)
            self.assertEqual(
                decode.call_args_list[0].args[0].getvalue(),
                decode.call_args_list[1].args[0].getvalue(),
            )

    def test_cache_decodes_verified_bytes_and_rechecks_replacement_next_time(self):
        with TemporaryDirectory() as directory:
            cache = PersistentAudioCache(directory, sample_rate=16000)
            saved = cache.put("key", [0.125, 0.25])
            read = cache.store.get_bytes

            def replace_after_read(key):
                payload = read(key)
                saved.unlink()
                saved.write_bytes(b"corrupt")
                return payload

            with patch.object(cache.store, "get_bytes", side_effect=replace_after_read):
                np.testing.assert_array_equal(cache.get("key"), [0.125, 0.25])
            with self.assertRaises(AudioCatalogError):
                cache.get("key")
            with self.assertRaises(AudioCatalogError):
                PersistentAudioCache(directory, sample_rate=16000).get("key")

    def test_path_replacement_during_snapshot_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            write_pcm16_wav(source, [0.125, 0.25], 16000)
            store = SharedAudioStore(root / "store")
            store.put("key", source)

            @contextmanager
            def replace_after_read(path):
                with open_regular_binary(path) as stream:
                    yield stream
                payload = path.read_bytes()
                path.unlink()
                path.write_bytes(payload)

            with patch("vntts.audio_store.open_regular_binary", replace_after_read):
                with self.assertRaisesRegex(
                    SharedAudioStoreError, "changed while reading"
                ):
                    store.get_bytes("key")

    def test_mutation_between_snapshot_and_publication_never_publishes_binding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            write_pcm16_wav(source, [0.125, 0.25], 16000)
            before = source.stat()
            store = SharedAudioStore(root / "store")
            store.initialize()
            link = os.link

            def mutate_then_link(original, target):
                write_pcm16_wav(source, [0.5, 0.75], 16000)
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
                link(original, target)

            with patch("vntts.audio_store.os.link", side_effect=mutate_then_link):
                with self.assertRaisesRegex(SharedAudioStoreError, "checksum changed"):
                    store.put("key", source)
            self.assertIsNone(store.get("key"))
