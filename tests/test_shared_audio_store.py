import hashlib
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts_artifacts.audio import write_pcm16_wav

from tests.bulk_generation_fixtures import (
    SyntheticRenderer,
    audio_samples,
    queue_item,
    write_queue,
)
from tests.symlink_support import symlink_or_skip
from vntts.audio_store import SharedAudioStore, SharedAudioStoreError, link_shared_audio
from vntts.authoring.bulk_generation import run_bulk_generation
from vntts.synthesis import SynthesisCompletion


class SharedAudioStoreTest(unittest.TestCase):
    def audio(self, path):
        write_pcm16_wav(path, audio_samples(), 16000)
        return path

    def test_bindings_and_versions_share_one_recording_after_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.audio(root / "old.wav")
            original = source.read_bytes()
            store = SharedAudioStore(root / "store")
            saved = store.put("first", source)
            self.assertEqual(store.put("second", source), saved)
            first, second = root / "version-1.wav", root / "version-2.wav"
            link_shared_audio(saved, first)
            link_shared_audio(SharedAudioStore(store.directory).get("second"), second)
            self.assertTrue(first.samefile(second))
            self.assertTrue(first.samefile(saved))
            self.assertEqual(len(list((store.directory / "objects").glob("*.wav"))), 1)
            first.unlink()
            source.unlink()
            self.assertEqual(second.read_bytes(), original)

    def test_verified_legacy_copy_is_relinked_without_changing_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.audio(root / "original.wav")
            duplicate = root / "copy.wav"
            duplicate.write_bytes(source.read_bytes())
            store = SharedAudioStore(root / "store")
            saved = store.put("key", source)
            link_shared_audio(saved, duplicate)
            self.assertTrue(saved.samefile(source))
            self.assertTrue(saved.samefile(duplicate))

    def test_corrupt_recording_fails_closed_and_keeps_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.audio(root / "old.wav")
            store = SharedAudioStore(root / "store")
            saved = store.put("key", source)
            # Replacement simulates a corrupt store entry without modifying a linked version.
            saved.unlink()
            saved.write_bytes(b"corrupt")
            with self.assertRaises(SharedAudioStoreError):
                store.get("key")
            self.assertGreater(source.stat().st_size, 100)

    def test_link_failure_leaves_existing_recording_unchanged(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.audio(root / "source.wav")
            target = root / "target.wav"
            target.write_bytes(source.read_bytes())
            before = target.read_bytes()
            with patch(
                "vntts.audio_store.os.link", side_effect=OSError("cross-device")
            ):
                with self.assertRaises(SharedAudioStoreError):
                    link_shared_audio(source, target)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(list(root.glob("*.link")), [])

    def test_sync_failure_does_not_publish_binding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.audio(root / "source.wav")
            store = SharedAudioStore(root / "store")
            with patch(
                "vntts.audio_store.os.fsync", side_effect=OSError("failed sync")
            ):
                with self.assertRaises(OSError):
                    store.put("key", source)
            self.assertIsNone(store.get("key"))
            self.assertGreater(source.stat().st_size, 100)

    def test_windows_flush_uses_write_capable_handle_without_changing_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.audio(root / "source.wav")
            before = source.read_bytes()
            store = SharedAudioStore(root / "store")
            windows = Mock(wraps=os)
            windows.name = "nt"
            with (
                patch("vntts.audio_store.os", windows),
                patch("vntts.audio_store.open", wraps=open, create=True) as opened,
            ):
                saved = store.put("key", source)
            opened.assert_called_once()
            self.assertEqual(opened.call_args.args[1], "r+b")
            windows.fsync.assert_called_once()
            self.assertEqual(saved.read_bytes(), before)
            self.assertTrue(saved.samefile(source))
            self.assertEqual(store.get("key"), saved)

    def test_store_refuses_symlink_objects_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SharedAudioStore(root / "store")
            store.directory.mkdir()
            outside = root / "outside"
            outside.mkdir()
            symlink_or_skip(
                store.directory / "objects", outside, target_is_directory=True
            )
            with self.assertRaises(SharedAudioStoreError):
                store.put("key", self.audio(root / "source.wav"))
            self.assertEqual(list(outside.iterdir()), [])

    def test_declared_wav_size_is_rejected_before_reading_samples(self):
        import struct

        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.audio(root / "source.wav")
            payload = bytearray(source.read_bytes())
            offset = payload.index(b"data") + 4
            struct.pack_into("<I", payload, offset, 0x7FFFFFFE)
            source.write_bytes(payload)
            with patch(
                "wave.Wave_read.readframes",
                side_effect=AssertionError("unbounded read"),
            ):
                with self.assertRaises(SharedAudioStoreError):
                    SharedAudioStore(root / "store").put("key", source)


class _KeyedRenderer(SyntheticRenderer):
    def synthesis_key(self, request):
        return hashlib.sha256(
            json.dumps(
                [request.voice, request.text, request.seed, request.generation_profile]
            ).encode()
        ).hexdigest()


class FinalWavReuseTest(unittest.TestCase):
    def generate(self, queue, output, renderer, **kwargs):
        return run_bulk_generation(
            queue,
            output,
            renderer,
            provider="synthetic",
            model="synthetic-v1",
            retries=0,
            synthesis_cache_policy="use",
            **kwargs,
        )

    def test_reopened_renderer_reuses_exact_final_wav_without_render_or_requantization(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = write_queue(root / "queue.jsonl", [queue_item()])
            first_renderer, reopened_renderer = _KeyedRenderer(), _KeyedRenderer()
            self.generate(queue, root / "first", first_renderer)
            self.generate(queue, root / "second", reopened_renderer)
            first = next((root / "first/audio").rglob("*.wav"))
            second = next((root / "second/audio").rglob("*.wav"))
            self.assertEqual(len(first_renderer.requests), 1)
            self.assertEqual(reopened_renderer.requests, [])
            self.assertTrue(first.samefile(second))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(list(root.rglob("*.npy")), [])

    def test_cancelled_shared_reuse_does_not_publish_and_can_resume(self):
        for phase in ("generating", "validating", "publishing"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory)
                queue = write_queue(root / "queue.jsonl", [queue_item()])
                self.generate(queue, root / "first", _KeyedRenderer())
                original = next((root / "first/audio").rglob("*.wav"))
                original_bytes = original.read_bytes()
                cancellation = threading.Event()

                def cancel(state):
                    active = state.get("active")
                    if isinstance(active, dict) and active.get("phase") == phase:
                        cancellation.set()

                renderer = _KeyedRenderer()
                result = self.generate(
                    queue,
                    root / "second",
                    renderer,
                    cancellation=cancellation,
                    progress_callback=cancel,
                )
                self.assertTrue(cancellation.is_set())
                self.assertTrue(result.cancelled)
                self.assertEqual(result.generated, 0)
                self.assertEqual(renderer.requests, [])
                self.assertEqual(list((root / "second").rglob("*.wav")), [])
                self.assertEqual(original.read_bytes(), original_bytes)
                resumed = _KeyedRenderer()
                result = self.generate(queue, root / "second", resumed)
                self.assertFalse(result.cancelled)
                self.assertEqual(result.generated, 1)
                self.assertEqual(resumed.requests, [])
                published = next((root / "second/audio").rglob("*.wav"))
                self.assertTrue(original.samefile(published))

    def test_changed_seed_does_not_reuse_different_synthesis(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = write_queue(root / "queue.jsonl", [queue_item()])
            self.generate(queue, root / "first", _KeyedRenderer(), seed=0)
            changed = _KeyedRenderer()
            self.generate(queue, root / "second", changed, seed=1)
            self.assertEqual(len(changed.requests), 1)

    def test_incomplete_render_does_not_become_a_reusable_recording(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = write_queue(root / "queue.jsonl", [queue_item()])
            self.generate(
                queue, root / "failed", _KeyedRenderer([SynthesisCompletion.LIMITED])
            )
            self.assertEqual(list((root / "audio-store").rglob("*.wav")), [])
            self.assertEqual(list((root / "audio-store").rglob("*.json")), [])
            retried = _KeyedRenderer()
            self.generate(queue, root / "retry", retried)
            self.assertEqual(len(retried.requests), 1)
