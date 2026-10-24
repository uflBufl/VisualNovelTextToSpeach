import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import unittest
from contextlib import closing
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
            self.assertTrue(opened.called)
            self.assertTrue(
                all(call.args[1] == "r+b" for call in opened.call_args_list)
            )
            self.assertGreaterEqual(windows.fsync.call_count, 1)
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

    def legacy_binding(self, store, key, source):
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        objects = store.directory / "objects"
        objects.mkdir(parents=True, exist_ok=True)
        saved = objects / f"{digest}.wav"
        if not saved.exists():
            os.link(source, saved)
        bindings = store.directory / "bindings"
        bindings.mkdir(exist_ok=True)
        binding = bindings / f"{key}.json"
        binding.write_text(json.dumps({"schema_version": 1, "audio_sha256": digest}))
        return binding, saved

    def test_missing_lookup_does_not_create_storage(self):
        with TemporaryDirectory() as directory:
            store = SharedAudioStore(Path(directory) / "missing")
            self.assertIsNone(store.get("missing"))
            self.assertEqual(list(store.iter_bindings()), [])
            self.assertFalse(store.has_saved_entries())
            self.assertFalse(store.directory.exists())

    def test_json_bindings_import_once_with_same_inode_and_preserved_unknowns(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SharedAudioStore(root / "store")
            source = self.audio(root / "source.wav")
            binding, saved = self.legacy_binding(store, "legacy", source)
            corrupt = binding.with_name("corrupt.json")
            corrupt.write_text("broken")
            unknown = binding.with_name("notes.txt")
            unknown.write_text("keep me")
            future = binding.with_name("future.json")
            future.write_text(
                json.dumps({"schema_version": 2, "audio_sha256": saved.stem})
            )
            self.assertTrue(store.get("legacy").samefile(source))
            self.assertFalse(binding.exists())
            self.assertEqual(corrupt.read_text(), "broken")
            self.assertEqual(unknown.read_text(), "keep me")
            self.assertTrue(future.exists())
            reopened = SharedAudioStore(store.directory)
            with patch(
                "vntts.audio_store.json.loads",
                side_effect=AssertionError("reparsed legacy JSON"),
            ):
                self.assertTrue(reopened.get("legacy").samefile(saved))
                self.assertEqual(
                    list(reopened.iter_bindings()), [("legacy", saved.stem)]
                )
                self.assertTrue(reopened.has_saved_entries())
                with self.assertRaises(SharedAudioStoreError):
                    reopened.get("corrupt")
            with closing(sqlite3.connect(store.catalog_path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone(), (1,)
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT synthesis_key, audio_sha256 FROM bindings"
                    ).fetchall(),
                    [("legacy", saved.stem)],
                )

    def test_empty_legacy_directory_lookup_does_not_create_catalog(self):
        with TemporaryDirectory() as directory:
            store = SharedAudioStore(Path(directory) / "store")
            (store.directory / "bindings").mkdir(parents=True)
            self.assertIsNone(store.get("missing"))
            self.assertFalse(store.catalog_path.exists())

    def test_concurrent_catalog_conflict_preserves_original_legacy_binding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SharedAudioStore(root / "store")
            binding, saved = self.legacy_binding(
                store, "key", self.audio(root / "legacy.wav")
            )
            source = root / "other.wav"
            write_pcm16_wav(source, [0.125, -0.25], 16000)
            competing = SharedAudioStore(root / "competing")
            competing.put("key", source)
            link = os.link

            def publish_competing(source, destination, *args, **kwargs):
                if Path(destination) == store.catalog_path:
                    link(competing.catalog_path, destination)
                    raise FileExistsError("competing catalog won publication")
                return link(source, destination, *args, **kwargs)

            original = binding.read_bytes()
            with patch("vntts.audio_store.os.link", side_effect=publish_competing):
                with self.assertRaisesRegex(SharedAudioStoreError, "conflicts"):
                    store.get("key")
            self.assertEqual(binding.read_bytes(), original)
            self.assertTrue(saved.samefile(root / "legacy.wav"))
            self.assertEqual(
                list(store.iter_bindings()), list(competing.iter_bindings())
            )

    def test_import_sync_failure_keeps_original_binding_and_recovers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SharedAudioStore(root / "store")
            source = self.audio(root / "source.wav")
            binding, saved = self.legacy_binding(store, "legacy", source)
            original = binding.read_bytes()
            with patch(
                "vntts.audio_store.os.fsync", side_effect=OSError("interrupted")
            ):
                with self.assertRaises(OSError):
                    store.get("legacy")
            self.assertEqual(binding.read_bytes(), original)
            self.assertFalse(store.catalog_path.exists())
            self.assertEqual(SharedAudioStore(store.directory).get("legacy"), saved)
            self.assertFalse(binding.exists())

    def test_import_readback_failure_keeps_original_and_reopens_published_catalog(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SharedAudioStore(root / "store")
            binding, saved = self.legacy_binding(
                store, "legacy", self.audio(root / "source.wav")
            )
            verify = store._object
            calls = 0

            def interrupted(digest):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise SharedAudioStoreError("readback interrupted")
                return verify(digest)

            with patch.object(store, "_object", side_effect=interrupted):
                with self.assertRaises(SharedAudioStoreError):
                    store.get("legacy")
            self.assertTrue(binding.exists())
            self.assertEqual(SharedAudioStore(store.directory).get("legacy"), saved)
            self.assertTrue(binding.exists())

    def test_refresh_updates_mapping_and_preserves_old_recording(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SharedAudioStore(root / "store")
            first = store.put("key", self.audio(root / "first.wav"))
            original = first.read_bytes()
            source = root / "second.wav"
            write_pcm16_wav(source, [0.125, -0.25], 16000)
            second = store.put("key", source)
            self.assertNotEqual(first, second)
            self.assertEqual(store.get("key"), second)
            self.assertEqual(first.read_bytes(), original)
            self.assertTrue(first.samefile(root / "first.wav"))

    def test_failed_commit_rolls_back_mapping_without_losing_either_wav(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SharedAudioStore(root / "store")
            first = store.put("key", self.audio(root / "first.wav"))
            source = root / "second.wav"
            write_pcm16_wav(source, [0.125, -0.25], 16000)
            connect = sqlite3.connect

            class FailedCommit(sqlite3.Connection):
                def commit(self):
                    raise sqlite3.OperationalError("failed commit")

            def failing_connection(*args, **kwargs):
                return connect(*args, **kwargs, factory=FailedCommit)

            with patch(
                "vntts.audio_store.sqlite3.connect", side_effect=failing_connection
            ):
                with self.assertRaises(SharedAudioStoreError):
                    store.put("key", source)
            self.assertEqual(store.get("key"), first)
            self.assertEqual(len(list((store.directory / "objects").glob("*.wav"))), 2)
            self.assertGreater(source.stat().st_size, 44)

    def test_concurrent_process_publication_keeps_every_binding_and_one_inode(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.audio(root / "source.wav")
            store = SharedAudioStore(root / "store")
            program = "from vntts.audio_store import SharedAudioStore; import sys; SharedAudioStore(sys.argv[1]).put(sys.argv[2], sys.argv[3])"
            children = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        program,
                        str(store.directory),
                        f"key-{index}",
                        str(source),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for index in range(6)
            ]
            try:
                for child in children:
                    _stdout, stderr = child.communicate(timeout=30)
                    self.assertEqual(child.returncode, 0, stderr.decode())
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.communicate()
            self.assertEqual(len(list(store.iter_bindings())), 6)
            for index in range(6):
                self.assertTrue(store.get(f"key-{index}").samefile(source))
            self.assertEqual(len(list((store.directory / "objects").glob("*.wav"))), 1)
            self.assertEqual(list(store.directory.glob(".audio-catalog.*")), [])

    def test_corrupt_or_future_catalog_is_never_replaced(self):
        for damage in ("corrupt", "future", "empty"):
            with self.subTest(damage=damage), TemporaryDirectory() as directory:
                root = Path(directory)
                store = SharedAudioStore(root / "store")
                source = self.audio(root / "source.wav")
                store.directory.mkdir()
                if damage == "corrupt":
                    store.catalog_path.write_bytes(b"saved damaged catalog")
                elif damage == "empty":
                    store.catalog_path.touch()
                else:
                    with closing(sqlite3.connect(store.catalog_path)) as connection:
                        connection.execute("PRAGMA user_version = 999")
                before = store.catalog_path.read_bytes()
                for operation in (
                    lambda: store.get("key"),
                    lambda: store.put("key", source),
                    store.has_saved_entries,
                ):
                    with self.assertRaises(SharedAudioStoreError):
                        operation()
                    self.assertEqual(store.catalog_path.read_bytes(), before)

    def test_catalog_and_journal_symlinks_are_rejected_without_touching_target(self):
        for suffix in ("", "-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix), TemporaryDirectory() as directory:
                root = Path(directory)
                store = SharedAudioStore(root / "store")
                store.directory.mkdir()
                outside = root / "outside"
                outside.write_bytes(b"preserved")
                symlink_or_skip(Path(str(store.catalog_path) + suffix), outside)
                with self.assertRaises(SharedAudioStoreError):
                    store.get("key")
                self.assertEqual(outside.read_bytes(), b"preserved")


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
