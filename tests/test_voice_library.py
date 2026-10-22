import hashlib
import io
import json
import os
import shutil
import stat
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

import vntts.voice_library as voice_library
from tests.authoring_fixtures import tree_hashes
from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from vntts.voice_library import (
    VoiceDiscovery,
    VoiceLibrary,
    VoiceLibraryError,
    VoiceSelection,
)


def write_wav(path: Path, frames: bytes) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(frames)


class _CopySnapshotProbe:
    def __init__(self, source: VoiceLibrary, new_reference: Path) -> None:
        self.source = source
        self.new_reference = new_reference
        self.copytree_started = Event()
        self.writer_finished = Event()
        self.writer_reached = Event()
        self.writer_completed = Event()
        self.writer_errors: list[BaseException] = []
        self.worker: Thread | None = None

    def __enter__(self):
        if self._lock.acquire(blocking=False):
            if self.copytree_started.is_set():
                self.writer_completed.set()
                self.writer_reached.set()
        else:
            self.writer_reached.set()
            self._lock.acquire()
        return self

    def __exit__(self, *args) -> None:
        self._lock.release()

    def start(self) -> None:
        self._lock = voice_library.RLock()

        def writer() -> None:
            try:
                self.copytree_started.wait(5)
                self.source.discover("Role", self.new_reference)
            except BaseException as error:  # pragma: no cover - surfaced below
                self.writer_errors.append(error)
            finally:
                self.writer_finished.set()

        self.worker = Thread(target=writer)
        self.worker.start()

    def finish(self) -> None:
        self.copytree_started.set()
        if not self.writer_finished.wait(5):
            raise AssertionError("writer did not finish")
        assert self.worker is not None
        self.worker.join(5)

    def copytree(self, source_path, destination_path, *args, **kwargs):
        del args, kwargs
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        snapshot = {
            path.relative_to(source_path): path.read_bytes()
            for path in source_path.rglob("*")
            if path.is_file()
            and path.name not in {"voice-library.json", ".voice-library.lock"}
        }
        self.copytree_started.set()
        if not self.writer_reached.wait(5):
            raise AssertionError("writer did not reach the source lock")
        destination_path.mkdir(parents=True)
        for relative, payload in snapshot.items():
            target = destination_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        if self.writer_completed.is_set() and not self.writer_finished.wait(5):
            raise AssertionError("writer did not finish publishing")
        (destination_path / "voice-library.json").write_bytes(
            (source_path / "voice-library.json").read_bytes()
        )
        return destination_path


class VoiceLibraryTest(unittest.TestCase):
    def test_discovery_batch_reads_once_and_only_publishes_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            references = tuple(root / f"reference-{index}.wav" for index in range(6))
            for index, path in enumerate(references):
                write_wav(path, bytes((index, 0)) * 24)
            library = VoiceLibrary(root / "library")
            requests = tuple(VoiceDiscovery("Role", path) for path in references)
            with (
                patch.object(library, "_load", wraps=library._load) as load,
                patch.object(library, "_write", wraps=library._write) as write,
            ):
                first = library.discover_many(requests)
                load.assert_called_once()
                write.assert_called_once()
                load.reset_mock()
                write.reset_mock()
                self.assertEqual(library.discover_many(requests), first)
                load.assert_called_once()
                write.assert_not_called()

    def test_discovery_batch_failure_rolls_back_choices_and_unpublished_blobs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.wav"
            write_wav(reference, b"\0\0" * 24)
            library = VoiceLibrary(root / "library")
            library.select("Existing", route="narrator")
            before = library.path.read_bytes()
            rollback = library.binding_rollback()
            requests = (
                VoiceDiscovery("Role", reference, bind_if_missing=True),
                VoiceDiscovery("Invalid", root / "missing.wav"),
            )
            with self.assertRaises(VoiceLibraryError):
                library.discover_many(requests, rollback=rollback)
            self.assertEqual(library.path.read_bytes(), before)
            self.assertFalse(tuple(library.blobs_path.glob("*.wav")))
            self.assertFalse(rollback._before)
            self.assertFalse(rollback._expected)

    def test_failed_batch_cleans_new_blobs_with_one_fresh_index_read(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            library = VoiceLibrary(root / "library")
            requests = []
            for index in range(6):
                reference = root / f"reference-{index}.wav"
                write_wav(reference, index.to_bytes(2, "little") * 24)
                requests.append(VoiceDiscovery("Role", reference))
            requests.append(VoiceDiscovery("Role", root / "missing.wav"))
            with patch.object(library, "_load", wraps=library._load) as load:
                with self.assertRaises(VoiceLibraryError):
                    library.discover_many(requests)
                self.assertEqual(load.call_count, 2)  # admission and guarded cleanup
            self.assertFalse(library.path.exists())
            self.assertFalse(tuple(library.blobs_path.glob("*.wav")))

    def test_read_snapshot_reuses_validation_and_detects_choices_and_corruption(self):
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(directory)
            library.select("Role", route="narrator")
            with patch.object(
                library, "_decode_index", wraps=library._decode_index
            ) as decode:
                first = library.read_snapshot()
                self.assertIs(library.read_snapshot(first), first)
                decode.assert_called_once()
            with self.assertRaisesRegex(VoiceLibraryError, "read-only"):
                first.clear("Role")
            library.select("Role", route="live-fallback")
            updated = library.read_snapshot(first)
            self.assertIsNot(updated, first)
            self.assertFalse(updated.has_same_routing(first))
            self.assertEqual(updated.binding("Role").route, "live-fallback")
            library.path.write_text("invalid JSON", encoding="utf-8")
            with self.assertRaises(VoiceLibraryError):
                library.read_snapshot(updated)

    def test_snapshot_provenance_cannot_be_changed_through_returned_records(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "reference.wav"
            write_wav(reference, b"\0\0" * 24)
            library = VoiceLibrary(root / "library")
            library.discover("Role", reference, evidence={"label": "original"})
            library.select("Role", route="narrator", evidence={"label": "original"})
            snapshot = library.read_snapshot()
            snapshot.binding("Role").provenance["evidence"]["label"] = "changed"
            snapshot.alternatives("Role")[0].discovery["evidence"]["label"] = "changed"
            self.assertEqual(
                snapshot.binding("Role").provenance["evidence"], {"label": "original"}
            )
            self.assertEqual(
                snapshot.alternatives("Role")[0].discovery["evidence"],
                {"label": "original"},
            )

    def test_deep_library_json_reports_read_error_without_rewriting(self):
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(directory)
            payload = b"[0]"
            library.path.write_bytes(payload)
            decoder_error = RecursionError("native parser limit")
            with (
                patch(
                    "vntts.json_types.json.loads", side_effect=decoder_error
                ) as decode,
                self.assertRaisesRegex(
                    VoiceLibraryError, "Unable to read voice library"
                ) as caught,
            ):
                library.bindings()
            decode.assert_called_once_with(payload.decode("utf-8"))
            self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)
            self.assertIs(caught.exception.__cause__.__cause__, decoder_error)
            self.assertEqual(library.path.read_bytes(), payload)

    def test_invalid_index_encoding_preserves_domain_error(self) -> None:
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(directory)
            library.path.write_bytes(b"\xff")
            with self.assertRaisesRegex(
                VoiceLibraryError, "Unable to read voice library"
            ):
                library.bindings()

    def test_thread_lock_creates_one_shared_lock_per_path(self) -> None:
        with (
            patch.object(voice_library, "_THREAD_LOCKS", {}),
            patch.object(voice_library, "RLock", wraps=voice_library.RLock) as create,
        ):
            first = voice_library._thread_lock(Path("first"))
            self.assertIs(voice_library._thread_lock(Path("first")), first)
            self.assertIsNot(voice_library._thread_lock(Path("second")), first)
            self.assertEqual(create.call_count, 2)

    def test_truncated_wav_uses_library_error_for_input_and_blob(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "truncated.wav"
            reference.write_bytes(b"RIFF")
            library = VoiceLibrary(root / "library")

            with self.assertRaisesRegex(VoiceLibraryError, "not a WAV file"):
                library.discover("Role", reference)

            checksum = hashlib.sha256(b"RIFF").hexdigest()
            library.blobs_path.mkdir(parents=True)
            library._blob_path(checksum).write_bytes(b"RIFF")
            with self.assertRaisesRegex(VoiceLibraryError, "not a WAV file"):
                library._validate_blob(checksum)

    def test_ogg_reference_error_identifies_character_file_and_recovery(self) -> None:
        with TemporaryDirectory() as directory:
            reference = Path(directory) / "lucy.ogg"
            reference.write_bytes(b"OggS")

            with self.assertRaises(VoiceLibraryError) as caught:
                VoiceLibrary(Path(directory) / "library").discover("Lucy", reference)

            message = str(caught.exception)
            self.assertIn("'Lucy'", message)
            self.assertIn(str(reference), message)
            self.assertIn("convert this audio to WAV", message)
            self.assertIsInstance(caught.exception.__cause__, VoiceLibraryError)

    def test_batch_selection_is_atomic_when_a_later_choice_is_invalid(self) -> None:
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(Path(directory) / "library")

            with self.assertRaisesRegex(VoiceLibraryError, "exactly one source"):
                library.select_many(
                    (
                        VoiceSelection("Alice", "narrator"),
                        VoiceSelection("Bob", "voice"),
                    )
                )

            self.assertEqual(library.bindings(), ())

    def test_missing_required_document_fields_are_rejected_without_rewriting(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x00\x00")
            library = VoiceLibrary(root / "library")
            choice = library.discover("Role", reference, bind_if_missing=True)
            original = library.path.read_text()
            for version in (1, 2, 3):
                for collection in ("alternatives", "bindings"):
                    for field in json.loads(original)[collection]["role:"]:
                        with self.subTest(
                            version=version, collection=collection, field=field
                        ):
                            document = json.loads(original)
                            document["version"] = version
                            if version < 3:
                                document.pop("person_link_migrations")
                            if version == 1:
                                document.pop("person_aliases")
                            del document[collection]["role:"][field]
                            library.path.write_text(json.dumps(document))
                            index = library.path.read_bytes()
                            for read in (
                                library.validate,
                                library.bindings,
                                lambda: library.alternatives("Role"),
                            ):
                                with self.assertRaises(VoiceLibraryError):
                                    read()
                                self.assertEqual(library.path.read_bytes(), index)
            document = json.loads(original)
            document["alternatives"]["role:"]["extra"] = "retained"
            document["bindings"]["role:"]["extra"] = "retained"
            library.path.write_text(json.dumps(document))
            library.validate()
            self.assertIsNone(library.binding("Role").variant_key)
            self.assertIsNone(library.binding("Role").source_id)
            self.assertEqual(library.alternatives("Role")[0].sha256, choice.sha256)

    def test_persisted_person_names_use_writer_identity_validation(self):
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(directory)
            library.select("Aderyn", route="narrator")
            library.link_person("Rhiannon", "Aderyn")
            original = library.path.read_text()
            for alias, canonical, variant in (
                ("", "Rhiannon", None),
                ("aderyn", "!!!", None),
                ("aderyn", "Rhiannon", ""),
                ("aderyn", "Rhiannon", "!!!"),
            ):
                with self.subTest(alias=alias, canonical=canonical, variant=variant):
                    document = json.loads(original)
                    document["person_aliases"] = {alias: canonical}
                    document["person_link_migrations"] = {
                        alias: [
                            {
                                "role": alias,
                                "variant_key": variant,
                                "linked_variant_key": voice_library._linked_variant_key(
                                    alias, variant
                                ),
                            }
                        ]
                    }
                    library.path.write_text(json.dumps(document))
                    index = library.path.read_bytes()
                    for read in (
                        library.validate,
                        lambda: library.canonical_role("Aderyn"),
                        lambda: library.unlink_person("Aderyn"),
                    ):
                        with self.assertRaises(VoiceLibraryError):
                            read()
                        self.assertEqual(library.path.read_bytes(), index)
            library.path.write_text(original)
            library.validate()
            self.assertTrue(library.unlink_person("Aderyn"))
            self.assertEqual(library.binding("Aderyn").route, "narrator")

    def test_boolean_document_version_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            root.mkdir()
            (root / "voice-library.json").write_text(
                json.dumps({"version": True, "alternatives": {}, "bindings": {}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(VoiceLibraryError, "Unsupported"):
                VoiceLibrary(root).bindings()

    def test_reader_rejects_alias_chains_that_writer_cannot_create(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            root.mkdir()
            document = {
                "version": 3,
                "alternatives": {},
                "bindings": {},
                "person_aliases": {"a": "B", "b": "C"},
                "person_link_migrations": {
                    key: [
                        {
                            "role": key,
                            "variant_key": None,
                            "linked_variant_key": f"story-name:{key}",
                        }
                    ]
                    for key in ("a", "b")
                },
            }
            (root / "voice-library.json").write_text(json.dumps(document))

            with self.assertRaisesRegex(VoiceLibraryError, "must not form a chain"):
                VoiceLibrary(root).canonical_role("A")

    def test_malformed_alternative_checksum_uses_library_error(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x00\x00")
            library = VoiceLibrary(root / "library")
            library.discover("Role", reference)
            document = json.loads(library.path.read_text(encoding="utf-8"))
            group = next(iter(document["alternatives"].values()))
            group["items"][0]["sha256"] = {}
            library.path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(VoiceLibraryError, "checksum is invalid"):
                library.alternatives("Role")

    def test_concurrent_role_updates_are_both_retained(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            first = VoiceLibrary(root)
            second = VoiceLibrary(root)
            first_loaded = Event()
            release_first = Event()
            second_loaded = Event()
            original_first_load = first._load
            original_second_load = second._load

            def blocked_first_load():
                document = original_first_load()
                first_loaded.set()
                release_first.wait(1)
                return document

            def observed_second_load():
                document = original_second_load()
                second_loaded.set()
                return document

            first._load = blocked_first_load
            second._load = observed_second_load
            first_thread = Thread(
                target=first.select,
                kwargs={"role": "Alice", "route": "narrator"},
            )
            second_thread = Thread(
                target=second.select,
                kwargs={"role": "Bob", "route": "narrator"},
            )
            first_thread.start()
            self.assertTrue(first_loaded.wait(1))
            second_thread.start()
            second_loaded.wait(0.1)
            release_first.set()
            first_thread.join(1)
            second_thread.join(1)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(
                {binding.role for binding in VoiceLibrary(root).bindings()},
                {"Alice", "Bob"},
            )

    def test_validate_reads_each_unique_blob_once_and_checks_unbound_alternatives(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            shared, unused = root / "shared.wav", root / "unused.wav"
            write_wav(shared, b"\x00\x00")
            write_wav(unused, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            first = library.discover("Alice", shared, bind_if_missing=True)
            library.discover("Bob", shared, bind_if_missing=True)
            other = library.discover("Unbound", unused)
            index = library.path.read_bytes()
            payloads = [first.path.read_bytes(), other.path.read_bytes()]
            with (
                patch.object(
                    voice_library,
                    "read_regular_bytes",
                    wraps=voice_library.read_regular_bytes,
                ) as capture,
                patch.object(
                    voice_library.hashlib, "sha256", wraps=voice_library.hashlib.sha256
                ) as hashing,
                patch.object(
                    voice_library.wave, "open", wraps=voice_library.wave.open
                ) as headers,
            ):
                library.validate()
            self.assertEqual(
                [call.args[0] for call in capture.call_args_list],
                [first.path, other.path],
            )
            self.assertEqual(
                [call.args[0] for call in hashing.call_args_list], payloads
            )
            self.assertEqual(headers.call_count, 2)
            for alternative in (first, other):
                with self.subTest(role=alternative.role):
                    payload = alternative.path.read_bytes()
                    try:
                        alternative.path.write_bytes(b"corrupted")
                        with self.assertRaisesRegex(
                            VoiceLibraryError, "checksum failed"
                        ):
                            library.validate()
                    finally:
                        alternative.path.write_bytes(payload)
                    self.assertEqual(library.path.read_bytes(), index)
            library.validate()

    def test_validate_rejects_bound_checksum_missing_from_inventory_before_blob_reads(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x00\x00")
            library = VoiceLibrary(root / "library")
            library.discover("Role", reference, bind_if_missing=True)
            document = json.loads(library.path.read_bytes())
            document["alternatives"]["role:"]["items"] = []
            library.path.write_text(json.dumps(document))
            index = library.path.read_bytes()
            with (
                patch.object(voice_library, "read_regular_bytes") as capture,
                patch.object(voice_library.hashlib, "sha256") as hashing,
                self.assertRaisesRegex(VoiceLibraryError, "not an alternative"),
            ):
                library.validate()
            capture.assert_not_called()
            hashing.assert_not_called()
            self.assertEqual(library.path.read_bytes(), index)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_copy_to_refuses_fifo_members_and_recovers_after_caller_cleanup(self):
        for member in ("index", "blob", "extra"):
            with self.subTest(member=member), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                reference = root / "reference.wav"
                write_wav(reference, b"\x01\x00")
                source = VoiceLibrary(root / "source" / "custom.json")
                alternative = source.discover("Role", reference, bind_if_missing=True)
                extra = source.root / "extra.txt"
                extra.write_bytes(b"complete-library extra")
                selected = {
                    "index": source.path,
                    "blob": alternative.path,
                    "extra": extra,
                }[member]
                before = tree_hashes(source.root)
                destination = root / "copy"
                original_copy = voice_library.copy_regular_file
                refused = []

                def copy(original, target, **kwargs):
                    if Path(original) != selected:
                        return original_copy(original, target, **kwargs)
                    gate: RegularFileGate = {"path": selected, "armed": True}
                    with (
                        swap_at_acquisition(self, gate),
                        self.assertRaisesRegex(OSError, "regular file") as caught,
                    ):
                        original_copy(original, target, **kwargs)
                    self.assertFalse(Path(target).exists())
                    refused.append((Path(target), caught.exception))
                    raise caught.exception

                with (
                    patch.object(voice_library, "copy_regular_file", copy),
                    self.assertRaises(shutil.Error) as caught,
                ):
                    source.copy_to(destination / "renamed.json")
                self.assertEqual(len(refused), 1)
                failed_target, error = refused[0]
                self.assertEqual(
                    caught.exception.args[0],
                    [(str(selected), str(failed_target), str(error))],
                )
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(destination.is_dir())
                self.assertFalse(failed_target.exists())
                self.assertEqual(tree_hashes(source.root), before)
                source.validate()
                shutil.rmtree(destination)
                recovered = source.copy_to(destination / "renamed.json")
                recovered.validate()
                expected = before.copy()
                expected["renamed.json"] = expected.pop("custom.json")
                self.assertEqual(tree_hashes(destination), expected)
                self.assertEqual(recovered.binding("Role"), source.binding("Role"))
                self.assertEqual(tree_hashes(source.root), before)
                with self.assertRaisesRegex(VoiceLibraryError, "already exists"):
                    source.copy_to(destination / "renamed.json")
                self.assertEqual(tree_hashes(destination), expected)

    def test_copy_to_preserves_interrupt_and_caller_owned_partial_tree(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = VoiceLibrary(root / "source")
            source.select("Role", route="narrator")
            before = tree_hashes(source.root)
            destination = root / "copy"
            original_copy = voice_library.copy_regular_file
            interrupt = KeyboardInterrupt("copy interrupted")
            copied_members = []

            def copy(original, target, **kwargs):
                original_copy(original, target, **kwargs)
                copied_members.append(Path(target))
                raise interrupt

            with (
                patch.object(voice_library, "copy_regular_file", copy),
                self.assertRaises(KeyboardInterrupt) as caught,
            ):
                source.copy_to(destination)
            self.assertIs(caught.exception, interrupt)
            self.assertEqual(len(copied_members), 1)
            self.assertTrue(copied_members[0].is_file())
            self.assertEqual(tree_hashes(source.root), before)
            shutil.rmtree(destination)
            copied = source.copy_to(destination)
            copied.validate()
            self.assertEqual(tree_hashes(copied.root), before)

    def test_copy_to_preserves_complete_tree_metadata_and_person_aliases(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            reference = root / "reference.wav"
            write_wav(reference, b"\x01\x00")
            source = VoiceLibrary(root / "source")
            source.discover("Role", reference, bind_if_missing=True)
            source.link_person("Role", "Alias")
            extra_directory = source.root / "extra" / "nested"
            extra_directory.mkdir(parents=True)
            (extra_directory / "payload.bin").write_bytes(b"extra member")
            metadata = {}
            for member in (source.root, *source.root.rglob("*")):
                member.chmod(0o750 if member.is_dir() else 0o600)
                os.utime(member, ns=(1_700_000_000_000_000_000,) * 2)
                captured = member.stat()
                metadata[member.relative_to(source.root)] = (
                    stat.S_IMODE(captured.st_mode),
                    captured.st_mtime_ns,
                )
            before = tree_hashes(source.root)
            copied = source.copy_to(root / "copy")
            self.assertEqual(tree_hashes(copied.root), before)
            self.assertEqual(copied.person_aliases(), source.person_aliases())
            self.assertEqual(copied.binding("Alias"), source.binding("Alias"))
            copied.validate()
            for relative, expected in metadata.items():
                with self.subTest(member=relative):
                    original = (source.root / relative).stat()
                    staged = (copied.root / relative).stat()
                    self.assertEqual(
                        (stat.S_IMODE(original.st_mode), original.st_mtime_ns), expected
                    )
                    self.assertEqual(
                        (stat.S_IMODE(staged.st_mode), staged.st_mtime_ns), expected
                    )
            self.assertEqual(tree_hashes(source.root), before)

    def test_copy_to_follows_extra_symlinks_and_aggregates_dangling_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = VoiceLibrary(root / "source")
            source.select("Role", route="narrator")
            external = root / "external"
            external.mkdir()
            payload = external / "payload.bin"
            payload.write_bytes(b"external member contents")
            file_alias = source.root / "file-alias"
            directory_alias = source.root / "directory-alias"
            try:
                file_alias.symlink_to(payload)
                directory_alias.symlink_to(external, target_is_directory=True)
            except OSError as error:
                if os.name != "nt" or error.winerror != 1314:
                    raise
                self.skipTest(f"Symlink creation requires Windows privilege: {error}")
            copied = source.copy_to(root / "copy")
            self.assertFalse((copied.root / file_alias.name).is_symlink())
            self.assertFalse((copied.root / directory_alias.name).is_symlink())
            self.assertEqual(
                (copied.root / file_alias.name).read_bytes(), payload.read_bytes()
            )
            self.assertEqual(
                (copied.root / directory_alias.name / payload.name).read_bytes(),
                payload.read_bytes(),
            )
            copied.validate()
            dangling = source.root / "dangling"
            dangling.symlink_to(root / "missing-member")
            destination = root / "partial"
            with self.assertRaises(shutil.Error) as caught:
                source.copy_to(destination)
            failures = caught.exception.args[0]
            self.assertEqual(len(failures), 1)
            self.assertEqual(
                failures[0][:2], (str(dangling), str(destination / dangling.name))
            )
            self.assertFalse((destination / dangling.name).exists())
            self.assertEqual(
                (destination / file_alias.name).read_bytes(), payload.read_bytes()
            )
            self.assertTrue(dangling.is_symlink())
            self.assertEqual(payload.read_bytes(), b"external member contents")

    def test_copy_to_missing_index_creates_neither_tree(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = VoiceLibrary(root / "missing")
            copied = source.copy_to(root / "copy")
            self.assertFalse(source.root.exists())
            self.assertFalse(copied.root.exists())
            self.assertEqual(copied.bindings(), ())

    def test_copy_to_keeps_index_and_blobs_from_one_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old_reference = root / "old.wav"
            new_reference = root / "new.wav"
            write_wav(old_reference, b"\x00\x00")
            write_wav(new_reference, b"\x01\x00")
            source = VoiceLibrary(root / "source")
            source.discover("Role", old_reference)
            destination = root / "copy"
            lock_path = source.root / ".voice-library.lock"
            probe = _CopySnapshotProbe(source, new_reference)
            with (
                patch.object(
                    voice_library,
                    "_THREAD_LOCKS",
                    {lock_path: probe},
                ),
                patch.object(voice_library.shutil, "copytree", probe.copytree),
            ):
                probe.start()
                try:
                    copied = source.copy_to(destination)
                finally:
                    probe.finish()
            assert probe.worker is not None
            self.assertFalse(probe.worker.is_alive())
            self.assertEqual(probe.writer_errors, [])
            copied.validate()

    def test_copy_to_preserves_a_custom_index_path(self) -> None:
        for source_name, destination in (
            ("custom.json", "copy"),
            ("voice-library.json", "copy/renamed.json"),
            ("custom.json", "copy/other.json"),
        ):
            with self.subTest(source=source_name, destination=destination):
                with TemporaryDirectory() as directory:
                    root = Path(directory)
                    reference = root / "reference.wav"
                    write_wav(reference, b"\x01\x00")
                    source = VoiceLibrary(root / "source" / source_name)
                    source.discover("Role", reference, bind_if_missing=True)
                    source.path.chmod(0o600)
                    os.utime(source.path, ns=(1_700_000_000_000_000_000,) * 2)
                    before = source.path.stat()
                    copied = source.copy_to(root / destination)
                    after = copied.path.stat()
                    self.assertEqual(
                        stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode)
                    )
                    self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
                    self.assertEqual(copied.binding("Role"), source.binding("Role"))
                    self.assertEqual(
                        tuple(item.sha256 for item in copied.alternatives("Role")),
                        tuple(item.sha256 for item in source.alternatives("Role")),
                    )
                    self.assertTrue(source.path.is_file())
                    self.assertTrue(copied.path.is_file())
                    copied.validate()

    def test_rollback_restores_only_unchanged_roles(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            library = VoiceLibrary(root)
            rollback = library.binding_rollback()

            library.select("Alice", route="narrator", rollback=rollback)
            VoiceLibrary(root).select("Bob", route="live-fallback")

            self.assertTrue(library.rollback_bindings(rollback))
            self.assertIsNone(library.binding("Alice"))
            self.assertEqual(library.binding("Bob").route, "live-fallback")

    def test_rollback_does_not_overwrite_a_newer_choice_for_the_same_role(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            library = VoiceLibrary(root)
            rollback = library.binding_rollback()

            library.select("Alice", route="narrator", rollback=rollback)
            VoiceLibrary(root).select("Alice", route="live-fallback")

            self.assertFalse(library.rollback_bindings(rollback))
            self.assertEqual(library.binding("Alice").route, "live-fallback")

    def test_rollback_preserves_a_peer_choice_between_own_writes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            library = VoiceLibrary(root)
            rollback = library.binding_rollback()

            library.select("Alice", route="narrator", rollback=rollback)
            VoiceLibrary(root).select("Alice", route="live-fallback")
            library.select("Alice", route="narrator", rollback=rollback)

            self.assertTrue(library.rollback_bindings(rollback))
            self.assertEqual(library.binding("Alice").route, "live-fallback")

    def test_rollback_tracks_a_duplicate_role_batch_once(self) -> None:
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(Path(directory) / "library")
            rollback = library.binding_rollback()

            library.select_many(
                (
                    VoiceSelection("Alice", "narrator"),
                    VoiceSelection("Alice", "live-fallback"),
                ),
                rollback=rollback,
            )

            self.assertTrue(library.rollback_bindings(rollback))
            self.assertIsNone(library.binding("Alice"))

    def test_reused_rollback_token_preserves_original_choice_across_duplicate_batch(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "library"
            library = VoiceLibrary(root)
            for peer_before_batch in (False, True):
                with self.subTest(peer_before_batch=peer_before_batch):
                    library.clear("Alice")
                    rollback = library.binding_rollback()
                    library.select(
                        "Alice", route="narrator", timestamp="first", rollback=rollback
                    )
                    expected = (
                        VoiceLibrary(root).select(
                            "Alice", route="voice", source_id="preset:alba"
                        )
                        if peer_before_batch
                        else None
                    )
                    results = library.select_many(
                        (
                            VoiceSelection("Alice", "narrator", timestamp="second"),
                            VoiceSelection("Alice", "live-fallback", timestamp="third"),
                        ),
                        rollback=rollback,
                    )
                    self.assertEqual(
                        tuple(item.route for item in results),
                        ("narrator", "live-fallback"),
                    )
                    self.assertTrue(library.rollback_bindings(rollback))
                    self.assertEqual(library.binding("Alice"), expected)

    def test_reference_descriptor_cleanup_preserves_read_error(self) -> None:
        native_close, native_fstat = os.close, os.fstat
        for primary_type in (OSError, KeyboardInterrupt, None):
            with self.subTest(primary=primary_type), TemporaryDirectory() as directory:
                root = Path(directory)
                reference = root / "voice.wav"
                write_wav(reference, b"\x00\x00")
                primary = (
                    primary_type("reference read failed") if primary_type else None
                )
                if primary is not None:
                    primary.add_note("original read note")
                cleanup_error = OSError("descriptor close failed")
                cleanup_error.add_note("nested close note")

                def fail_close(descriptor):
                    native_close(descriptor)
                    raise cleanup_error

                with (
                    patch(
                        "vntts.voice_library.os.close", side_effect=fail_close
                    ) as close,
                    patch(
                        "vntts.voice_library.os.fstat",
                        wraps=native_fstat,
                        side_effect=primary,
                    ),
                    self.assertRaises(primary_type or OSError) as raised,
                ):
                    VoiceLibrary(root / "library").discover("Role", reference)

                self.assertIs(raised.exception, primary or cleanup_error)
                close.assert_called_once()
                if primary is not None:
                    self.assertEqual(
                        primary.__notes__,
                        [
                            "original read note",
                            "Voice reference descriptor cleanup failed: "
                            "descriptor close failed",
                            "nested close note",
                        ],
                    )

    def test_windows_reference_is_opened_in_binary_mode(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x1a\r\n")
            real_open = os.open
            native_binary_flag = getattr(os, "O_BINARY", 0)
            binary_flag = 0x40000000
            flags_seen = 0

            def windows_open(path, flags, *args):
                nonlocal flags_seen
                if Path(path) == reference:
                    flags_seen = flags
                flags = flags & ~binary_flag | native_binary_flag
                return real_open(path, flags, *args)

            with (
                patch("vntts.voice_library.os.O_BINARY", binary_flag, create=True),
                patch("vntts.voice_library.os.open", side_effect=windows_open),
            ):
                VoiceLibrary(root / "library").discover("Role", reference)

            self.assertEqual(flags_seen & binary_flag, binary_flag)

    def test_windows_cross_stat_identity_does_not_reject_stable_reference(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x00\x00")
            real_fstat = os.fstat

            def windows_fstat(descriptor):
                value = real_fstat(descriptor)
                return SimpleNamespace(
                    st_mode=value.st_mode,
                    st_dev=value.st_dev + 1,
                    st_ino=value.st_ino + 1,
                    st_size=value.st_size,
                    st_mtime_ns=value.st_mtime_ns,
                )

            with (
                patch("vntts.voice_library._CROSS_STAT_IDENTITY_RELIABLE", False),
                patch("vntts.voice_library.os.fstat", side_effect=windows_fstat),
            ):
                result = VoiceLibrary(root / "library").discover("Role", reference)

            self.assertTrue(result.path.is_file())

    def test_discovery_deduplicates_blobs_and_does_not_replace_binding(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first.wav", root / "second.wav"
            write_wav(first, b"\x00\x00")
            write_wav(second, b"\x01\x00")
            library = VoiceLibrary(root / "fixed-library.json")
            selected = library.discover("Mrs. Owen", first, bind_if_missing=True)
            with patch.object(library, "_write", wraps=library._write) as write:
                duplicate = library.discover("Mrs. Owen", first)
                write.assert_not_called()
            library.discover("Mrs. Owen", second, bind_if_missing=True)

            self.assertEqual(selected.sha256, duplicate.sha256)
            self.assertEqual(len(library.alternatives("Mrs Owen")), 2)
            self.assertEqual(library.binding("Mrs Owen").source_sha256, selected.sha256)
            self.assertEqual(library.resolve_source_path("Mrs Owen"), selected.path)
            self.assertEqual(len(list(library.blobs_path.glob("*.wav"))), 2)

    def test_failed_discovery_publication_removes_only_unreferenced_blob(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = root / "old.wav", root / "new.wav"
            write_wav(old, b"\x00\x00")
            write_wav(new, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            original = library.discover("Ada", old)
            prior_index = library.path.read_bytes()

            with self.assertRaisesRegex(VoiceLibraryError, "JSON data"):
                library.discover("Ada", new, evidence=object())
            self.assertEqual(list(library.blobs_path.glob("*.wav")), [original.path])

            original_store = library._store_blob

            def store_then_fail(checksum, payload):
                original_store(checksum, payload)
                raise OSError("post-store failure")

            with (
                patch.object(library, "_store_blob", side_effect=store_then_fail),
                self.assertRaisesRegex(OSError, "post-store failure"),
            ):
                library.discover("Ada", new)
            self.assertEqual(list(library.blobs_path.glob("*.wav")), [original.path])

            with (
                patch.object(library, "_write", side_effect=OSError("disk full")),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                library.discover("Ada", new)

            self.assertEqual(library.path.read_bytes(), prior_index)
            self.assertEqual(list(library.blobs_path.glob("*.wav")), [original.path])

            original_write = library._write

            def publish_then_fail(document):
                original_write(document)
                raise OSError("post-publish failure")

            with (
                patch.object(library, "_write", side_effect=publish_then_fail),
                self.assertRaisesRegex(OSError, "post-publish failure"),
            ):
                library.discover("Ada", new)

            self.assertEqual(len(library.alternatives("Ada")), 2)
            self.assertEqual(len(list(library.blobs_path.glob("*.wav"))), 2)

    def test_existing_alternative_can_be_bound_without_rediscovery(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x00\x00")
            library = VoiceLibrary(root / "library")
            alternative = library.discover("Role", reference)

            with patch.object(library, "_write", wraps=library._write) as write:
                library.discover("Role", reference, bind_if_missing=True)
                write.assert_called_once()

            self.assertEqual(library.binding("Role").source_sha256, alternative.sha256)

    def test_explicit_routes_replace_canonical_binding_and_support_presets(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x00\x00")
            library = VoiceLibrary(root / "library")
            alternative = library.discover("Sonetto", reference, variant_key="young")
            library.select(
                "Sonetto",
                variant_key="young",
                route="voice",
                source_sha256=alternative.sha256,
                method="automatic",
                algorithm="matcher-v1",
                evidence={"score": 0.9},
            )
            library.select(
                "Sonetto",
                variant_key="old",
                route="voice",
                source_id="preset:alba",
            )
            library.select("Narrator", route="narrator")
            library.select("Unknown", route="live-fallback")

            self.assertEqual(
                library.binding("Sonetto", variant_key="old").source_id, "preset:alba"
            )
            self.assertIsNone(library.resolve_source_path("Sonetto", variant_key="old"))
            self.assertEqual(library.binding("Narrator").route, "narrator")
            self.assertEqual(library.binding("Unknown").route, "live-fallback")
            self.assertEqual(len(library.bindings()), 3)
            self.assertTrue(library.clear("Unknown"))
            self.assertIsNone(library.binding("Unknown"))
            self.assertFalse(library.clear("Unknown"))

    def test_linked_person_shares_binding_and_retains_variant_alternatives(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            adult, child = root / "adult.wav", root / "child.wav"
            write_wav(adult, b"\x00\x00")
            write_wav(child, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            adult_choice = library.discover("Rhiannon", adult, variant_key="adult")
            child_choice = library.discover("Rhiannon", child, variant_key="child")
            library.select(
                "Rhiannon",
                variant_key="adult",
                route="voice",
                source_sha256=adult_choice.sha256,
            )
            library.select(
                "Rhiannon",
                variant_key="child",
                route="voice",
                source_sha256=child_choice.sha256,
            )

            library.link_person("Rhiannon", "Aderyn")

            self.assertEqual(library.canonical_role("Aderyn"), "Rhiannon")
            self.assertEqual(
                library.binding("Aderyn", variant_key="adult"),
                library.binding("Rhiannon"),
            )
            self.assertEqual(
                library.binding("Aderyn", variant_key="child"),
                library.binding("Rhiannon"),
            )
            self.assertEqual(
                library.binding("Rhiannon", variant_key="child").source_sha256,
                child_choice.sha256,
            )
            self.assertTrue(library.unlink_person("Aderyn"))
            self.assertIsNone(library.binding("Aderyn", variant_key="adult"))
            self.assertEqual(
                library.binding("Rhiannon", variant_key="adult").source_sha256,
                child_choice.sha256,
            )

    def test_linked_names_share_active_voice_and_restore_prior_choice_on_unlink(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            adult, child = root / "adult.wav", root / "child.wav"
            write_wav(adult, b"\x00\x00")
            write_wav(child, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            adult_choice = library.discover("Rhiannon", adult, bind_if_missing=True)
            child_choice = library.discover("Aderyn", child, bind_if_missing=True)
            child_variant = library.discover("Aderyn", child, variant_key="child")
            library.select(
                "Aderyn",
                variant_key="child",
                route="voice",
                source_sha256=child_variant.sha256,
            )
            library.link_person("Rhiannon", "Aderyn")

            self.assertEqual(
                library.binding("Rhiannon").source_sha256, adult_choice.sha256
            )
            self.assertEqual(library.linked_variant_key("Aderyn"), "story-name:aderyn")
            self.assertEqual(
                library.binding("Aderyn").source_sha256, adult_choice.sha256
            )
            self.assertEqual(
                library.binding(
                    "Aderyn", variant_key="story-name:aderyn"
                ).source_sha256,
                adult_choice.sha256,
            )
            self.assertEqual(
                library.binding(
                    "Rhiannon", variant_key="story-name:aderyn"
                ).source_sha256,
                adult_choice.sha256,
            )
            self.assertEqual(
                library.binding("Aderyn", variant_key="child").source_sha256,
                adult_choice.sha256,
            )
            self.assertTrue(library.unlink_person("Aderyn"))
            self.assertEqual(
                library.binding("Aderyn").source_sha256, child_choice.sha256
            )

    def test_v2_link_migration_restores_alias_display_on_unlink(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x00\x00")
            library = VoiceLibrary(root / "library")
            library.discover("Aderyn", reference, bind_if_missing=True)
            library.link_person("Rhiannon", "Aderyn")
            legacy = json.loads(library.path.read_text(encoding="utf-8"))
            legacy["version"] = 2
            legacy.pop("person_link_migrations")
            library.path.write_text(json.dumps(legacy), encoding="utf-8")

            library.select("Bob", route="narrator")
            self.assertTrue(library.unlink_person("Aderyn"))

            self.assertEqual(library.binding("Aderyn").role, "Aderyn")
            self.assertEqual(library.alternatives("Aderyn")[0].role, "Aderyn")

    def test_old_variants_migrate_once_with_manual_choice_and_stay_cleared(self):
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(directory)
            library.select("Role", route="voice", source_id="manual")
            document = json.loads(library.path.read_text())
            manual = document["bindings"].pop("role:")
            manual["variant_key"] = "young"
            manual["provenance"]["timestamp"] = "2020-01-01T00:00:00+00:00"
            automatic = json.loads(json.dumps(manual))
            automatic["variant_key"] = "old"
            automatic["source_id"] = "automatic"
            automatic["provenance"].update(
                method="automatic", timestamp="2030-01-01T00:00:00+00:00"
            )
            document.update(
                version=3, bindings={"role:young": manual, "role:old": automatic}
            )
            document.pop("archived_bindings")
            library.path.write_text(json.dumps(document))
            original = library.path.read_bytes()

            self.assertEqual(
                library.binding("Role", variant_key="old").source_id, "manual"
            )
            self.assertEqual(library.path.read_bytes(), original)
            self.assertEqual(len(library.bindings()), 1)
            self.assertTrue(library.clear("Role", variant_key="young"))
            self.assertIsNone(
                VoiceLibrary(directory).binding("Role", variant_key="old")
            )
            migrated = json.loads(library.path.read_text())
            self.assertEqual(migrated["version"], voice_library.VOICE_LIBRARY_VERSION)
            self.assertEqual(len(migrated["archived_bindings"]), 2)
            library.select("Other", route="narrator")
            self.assertEqual(
                json.loads(library.path.read_text())["archived_bindings"],
                migrated["archived_bindings"],
            )

    def test_legacy_canonical_base_beats_newer_manual_variant(self):
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(directory)
            library.select("Role", route="voice", source_id="base", method="automatic")
            document = json.loads(library.path.read_text())
            variant = json.loads(json.dumps(document["bindings"]["role:"]))
            variant["variant_key"] = "young"
            variant["source_id"] = "variant"
            variant["provenance"].update(method="manual", timestamp="2999-01-01")
            document["bindings"]["role:young"] = variant
            document["version"] = 3
            document.pop("archived_bindings")
            library.path.write_text(json.dumps(document))

            self.assertEqual(
                library.binding("Role", variant_key="young").source_id, "base"
            )
            library.select("Role", variant_key="young", route="voice", source_id="new")
            self.assertEqual(library.binding("Role").source_id, "new")
            self.assertEqual(len(library.bindings()), 1)

    def test_same_person_variants_share_selection_clear_and_guarded_rollback(self):
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(directory)
            library.select(
                "Role", variant_key="young", route="voice", source_id="manual"
            )
            library.link_person("Role", "Alias")
            kept = library.select(
                "Alias",
                variant_key="old",
                route="narrator",
                only_if_unbound=True,
                method="automatic",
            )
            self.assertEqual(kept.source_id, "manual")
            rollback = library.binding_rollback()
            library.clear("Alias", variant_key="old", rollback=rollback)
            self.assertIsNone(library.binding("Role", variant_key="young"))
            self.assertTrue(library.rollback_bindings(rollback))
            self.assertEqual(library.binding("Alias").source_id, "manual")
            rollback = library.binding_rollback()
            library.select(
                "Role", variant_key="young", route="narrator", rollback=rollback
            )
            VoiceLibrary(directory).select(
                "Alias", variant_key="old", route="live-fallback"
            )
            self.assertFalse(library.rollback_bindings(rollback))
            self.assertEqual(library.binding("Role").route, "live-fallback")

    def test_wavs_from_all_variants_and_linked_archives_are_selectable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            alternative = library.discover(
                "Alias", reference, variant_key="young", bind_if_missing=True
            )
            library.select("Role", route="narrator")
            library.link_person("Role", "Alias")
            self.assertEqual(len(library.bindings()), 1)
            self.assertEqual(
                library.binding("Alias", variant_key="young").route, "narrator"
            )
            self.assertEqual(
                library.binding("Role", variant_key="story-name:alias:young").route,
                "narrator",
            )
            library.select(
                "Alias",
                variant_key="other-story",
                route="voice",
                source_sha256=alternative.sha256,
            )
            self.assertEqual(library.resolve_source_paths("Role"), (alternative.path,))
            library.validate()
            self.assertTrue(library.unlink_person("Alias"))
            self.assertEqual(library.binding("Alias").source_sha256, alternative.sha256)
            self.assertEqual(library.binding("Role").source_sha256, alternative.sha256)
            self.assertEqual(
                library.alternatives("Alias", variant_key="young")[0].sha256,
                alternative.sha256,
            )
            library.validate()

    def test_discovery_only_binds_one_canonical_choice_across_variants(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first.wav", root / "second.wav"
            write_wav(first, b"\x00\x00")
            write_wav(second, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            chosen = library.discover(
                "Role", first, variant_key="young", bind_if_missing=True
            )
            library.link_person("Role", "Alias")
            discovered = library.discover(
                "Alias", second, variant_key="old", bind_if_missing=True
            )
            self.assertNotEqual(chosen.sha256, discovered.sha256)
            self.assertEqual(
                library.binding("Alias", variant_key="old").source_sha256, chosen.sha256
            )
            self.assertEqual(len(library.bindings()), 1)
            self.assertIsNone(library.bindings()[0].variant_key)
            library.select("Role", route="voice", source_sha256=discovered.sha256)
            self.assertEqual(library.binding("Alias").source_sha256, discovered.sha256)

    def test_linked_variant_discovery_retains_inventory_and_unlinks_without_collision(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first.wav", root / "second.wav"
            write_wav(first, b"\x00\x00")
            write_wav(second, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            previous = library.discover("Alias", first, variant_key="young")
            library.link_person("Role", "Alias")
            self.assertEqual(
                library.alternatives("Alias", variant_key="young")[0].sha256,
                previous.sha256,
            )
            discovered = library.discover("Alias", second, variant_key="young")
            new_variant = library.discover("Alias", second, variant_key="old")
            self.assertTrue(library.unlink_person("Alias"))
            self.assertEqual(
                {
                    item.sha256
                    for item in library.alternatives("Alias", variant_key="young")
                },
                {previous.sha256, discovered.sha256},
            )
            self.assertEqual(
                library.alternatives("Alias", variant_key="old")[0].sha256,
                new_variant.sha256,
            )
            library.validate()

    def test_legacy_variant_wav_promotes_selected_metadata_without_losing_inventory(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            choice = library.discover(
                "Role", reference, variant_key="young", bind_if_missing=True
            )
            document = json.loads(library.path.read_text())
            binding = document["bindings"].pop("role:")
            binding["variant_key"] = "young"
            document["bindings"]["role:young"] = binding
            del document["alternatives"]["role:"]
            document["version"] = 3
            document.pop("archived_bindings")
            library.path.write_text(json.dumps(document))

            self.assertEqual(library.resolve_source_paths("Role"), (choice.path,))
            self.assertEqual(
                library.alternatives("Role", variant_key="young")[0].sha256,
                choice.sha256,
            )
            self.assertEqual(library.alternatives("Role")[0].sha256, choice.sha256)
            library.select("Other", route="narrator")
            library.validate()
            self.assertEqual(
                len(json.loads(library.path.read_text())["archived_bindings"]), 1
            )
            self.assertEqual(list(library.blobs_path.glob("*.wav")), [choice.path])

    def test_legacy_linked_alias_inventory_remains_visible_after_rediscovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first.wav", root / "second.wav"
            write_wav(first, b"\x00\x00")
            write_wav(second, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            previous = library.discover("Alias", first, variant_key="young")
            document = json.loads(library.path.read_text())
            document["version"] = 3
            document.pop("archived_bindings")
            document["person_aliases"] = {"alias": "Role"}
            document.pop("person_link_restores")
            document["person_link_migrations"] = {
                "alias": [
                    {
                        "role": "Alias",
                        "variant_key": None,
                        "linked_variant_key": "story-name:alias",
                    }
                ]
            }
            library.path.write_text(json.dumps(document))
            discovered = library.discover("Alias", second, variant_key="young")
            self.assertEqual(
                {
                    item.sha256
                    for item in library.alternatives("Alias", variant_key="young")
                },
                {previous.sha256, discovered.sha256},
            )
            self.assertTrue(library.unlink_person("Alias"))
            self.assertEqual(len(library.alternatives("Alias", variant_key="young")), 2)
            library.validate()

    def test_archived_reference_inventory_is_inert_validated_and_survives_unlink(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference, foreign = root / "voice.wav", root / "foreign.wav"
            write_wav(reference, b"\x00\x00")
            write_wav(foreign, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            chosen = library.discover("Alias", reference, bind_if_missing=True)
            library.select("Role", route="narrator")
            library.link_person("Role", "Alias")
            archived = library.archived_bindings("Alias")
            self.assertEqual(archived, library.archived_bindings("Role"))
            self.assertEqual(archived, library.archived_bindings())
            self.assertEqual(library.archived_bindings("Other"), ())
            self.assertEqual(library.reference_paths(archived[0]), (chosen.path,))
            library.clear("Role")
            self.assertEqual(library.bindings(), ())
            self.assertEqual(library.reference_paths(archived[0]), (chosen.path,))
            self.assertTrue(library.unlink_person("Alias"))
            self.assertEqual(library.archived_bindings("Role"), ())
            self.assertEqual(library.reference_paths(archived[0]), (chosen.path,))
            foreign_checksum = hashlib.sha256(foreign.read_bytes()).hexdigest()
            library._blob_path(foreign_checksum).write_bytes(foreign.read_bytes())
            foreign_binding = voice_library.VoiceBinding(
                "Alias", None, "voice", (foreign_checksum,), None, {}
            )
            with self.assertRaisesRegex(VoiceLibraryError, "not recorded"):
                library.reference_paths(foreign_binding)
            chosen.path.write_bytes(b"corrupt")
            with self.assertRaisesRegex(VoiceLibraryError, "checksum failed"):
                library.reference_paths(archived[0])

    def test_legacy_variant_timestamp_order_normalizes_offsets_and_falls_back_stably(
        self,
    ):
        for first_timestamp, second_timestamp, expected in (
            ("2026-10-15T10:00:00+03:00", "2026-10-15T08:30:00+00:00", "second"),
            ("2026-10-15T10:00:00+03:00", "2026-10-15T07:00:00Z", "second"),
            ("2026-10-15T10:00:00", "2026-10-15T08:30:00", "first"),
            ("earlier", "later", "second"),
        ):
            with (
                self.subTest(first=first_timestamp, second=second_timestamp),
                TemporaryDirectory() as directory,
            ):
                library = VoiceLibrary(directory)
                library.select("Role", route="voice", source_id="first")
                document = json.loads(library.path.read_text())
                first = document["bindings"].pop("role:")
                first["variant_key"] = "first"
                first["provenance"]["timestamp"] = first_timestamp
                second = json.loads(json.dumps(first))
                second["variant_key"] = "second"
                second["source_id"] = "second"
                second["provenance"]["timestamp"] = second_timestamp
                document.update(
                    version=3, bindings={"role:first": first, "role:second": second}
                )
                document.pop("archived_bindings")
                library.path.write_text(json.dumps(document))
                self.assertEqual(library.binding("Role").source_id, expected)
                self.assertEqual(len(library.archived_bindings("Role")), 2)

    def test_repeated_links_restore_only_the_choice_captured_by_that_link(self):
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(directory)
            library.select("Alias", route="voice", source_id="preset:alba")
            library.select("Role", route="voice", source_id="preset:marius")
            library.link_person("Role", "Alias")
            library.unlink_person("Alias")
            self.assertEqual(library.binding("Alias").source_id, "preset:alba")
            library.clear("Alias")
            library.link_person("Role", "Alias")
            library.select("Alias", route="voice", source_id="preset:anna")
            library.unlink_person("Alias")
            self.assertIsNone(library.binding("Alias"))
            self.assertEqual(library.binding("Role").source_id, "preset:anna")
            archive_count = len(library.archived_bindings())
            library.link_person("Role", "Alias")
            library.unlink_person("Alias")
            self.assertIsNone(library.binding("Alias"))
            self.assertEqual(len(library.archived_bindings()), archive_count)
            library.select("Alias", route="voice", source_id="preset:jean")
            library.link_person("Role", "Alias")
            library.select("Role", route="voice", source_id="preset:cosette")
            library.unlink_person("Alias")
            self.assertEqual(library.binding("Alias").source_id, "preset:jean")
            self.assertEqual(library.binding("Role").source_id, "preset:cosette")

    def test_rollback_does_not_restore_an_alias_after_it_became_linked(self):
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(directory)
            library.select("Alias", route="voice", source_id="preset:alba")
            library.select("Role", route="voice", source_id="preset:marius")
            rollback = library.binding_rollback()
            library.clear("Alias", rollback=rollback)
            VoiceLibrary(directory).link_person("Role", "Alias")
            self.assertFalse(library.rollback_bindings(rollback))
            self.assertEqual(library.binding("Alias").source_id, "preset:marius")
            library.unlink_person("Alias")
            self.assertIsNone(library.binding("Alias"))

    def test_legacy_current_link_restore_is_inferred_without_old_history(self):
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(directory)
            library.select("Alias", route="voice", source_id="preset:alba")
            library.select("Role", route="voice", source_id="preset:marius")
            library.link_person("Role", "Alias")
            document = json.loads(library.path.read_text())
            document["version"] = 3
            document.pop("person_link_restores")
            archived = document.pop("archived_bindings")
            document["bindings"]["role:storynamealias"] = archived[0]
            library.path.write_text(json.dumps(document))
            self.assertTrue(library.unlink_person("Alias"))
            self.assertEqual(library.binding("Alias").source_id, "preset:alba")
            library.clear("Alias")
            library.link_person("Role", "Alias")
            library.unlink_person("Alias")
            self.assertIsNone(library.binding("Alias"))

    def test_invalid_current_link_restore_is_rejected_without_rewriting(self):
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(directory)
            library.select("Alias", route="voice", source_id="preset:alba")
            library.link_person("Role", "Alias")
            original = json.loads(library.path.read_text())
            for index in (True, -1, 100, "0"):
                with self.subTest(index=index):
                    document = json.loads(json.dumps(original))
                    document["person_link_restores"]["alias"] = index
                    library.path.write_text(json.dumps(document))
                    before = library.path.read_bytes()
                    with self.assertRaisesRegex(VoiceLibraryError, "restore"):
                        library.unlink_person("Alias")
                    self.assertEqual(library.path.read_bytes(), before)

    def test_composite_selection_keeps_all_selected_references_in_order(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first.wav", root / "second.wav"
            write_wav(first, b"\x00\x00")
            write_wav(second, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            first_choice = library.discover("Dobharchu", first)
            second_choice = library.discover("Dobharchu", second)
            binding = library.select(
                "Dobharchu",
                route="voice",
                source_sha256s=(second_choice.sha256, first_choice.sha256),
            )

            self.assertEqual(
                binding.source_sha256s, (second_choice.sha256, first_choice.sha256)
            )
            self.assertIsNone(binding.source_sha256)
            self.assertEqual(
                library.resolve_source_paths("Dobharchu"),
                (second_choice.path, first_choice.path),
            )
            self.assertIsNone(library.resolve_source_path("Dobharchu"))

    def test_missing_unselected_blob_does_not_break_selected_resolution(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            selected, unused = root / "selected.wav", root / "unused.wav"
            write_wav(selected, b"\x00\x00")
            write_wav(unused, b"\x01\x00")
            library = VoiceLibrary(root / "library")
            choice = library.discover("Role", selected, bind_if_missing=True)
            other = library.discover("Role", unused)
            other.path.unlink()

            self.assertEqual(library.resolve_source_path("Role"), choice.path)
            with self.assertRaisesRegex(VoiceLibraryError, "missing or unsafe"):
                library.validate()

    def test_selection_rejects_unknown_or_invalid_sources(self) -> None:
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(Path(directory) / "library")
            with self.assertRaisesRegex(VoiceLibraryError, "not an alternative"):
                library.select(
                    "Role",
                    route="voice",
                    source_sha256=hashlib.sha256(b"missing").hexdigest(),
                )
            with self.assertRaisesRegex(
                VoiceLibraryError, "cannot have a voice source"
            ):
                library.select("Role", route="narrator", source_id="preset:alba")

    def test_blob_header_uses_checksum_bound_payload(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x00\x00")
            library = VoiceLibrary(root / "library")
            alternative = library.discover("Role", reference)
            index = library.path.read_bytes()
            open_wav = wave.open

            def replace_before_header(source, mode):
                alternative.path.write_bytes(b"replaced after checksum")
                return open_wav(source, mode)

            with patch.object(wave, "open", side_effect=replace_before_header):
                self.assertEqual(
                    library._validate_blob(alternative.sha256), alternative.path
                )
            self.assertEqual(library.path.read_bytes(), index)
            with self.assertRaisesRegex(VoiceLibraryError, "checksum failed"):
                library.validate()
            self.assertEqual(library.path.read_bytes(), index)

    def test_blob_header_accepts_stereo_and_non_pcm16_wav(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            library = VoiceLibrary(root / "library")
            for channels, width in ((2, 1), (1, 3)):
                with self.subTest(channels=channels, width=width):
                    reference = root / f"voice-{channels}-{width}.wav"
                    with wave.open(str(reference), "wb") as output:
                        output.setnchannels(channels)
                        output.setsampwidth(width)
                        output.setframerate(24_000)
                        output.writeframes(b"\0" * channels * width * 4)
                    alternative = library.discover(
                        f"Role {channels}-{width}", reference
                    )
                    self.assertEqual(
                        library._validate_blob(alternative.sha256), alternative.path
                    )
                    with wave.open(
                        io.BytesIO(alternative.path.read_bytes()), "rb"
                    ) as audio:
                        self.assertEqual(audio.getnchannels(), channels)
                        self.assertEqual(audio.getsampwidth(), width)
            library.validate()


if __name__ == "__main__":
    unittest.main()
