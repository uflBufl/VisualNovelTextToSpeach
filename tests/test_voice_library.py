import hashlib
import json
import os
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from vntts.voice_library import VoiceLibrary, VoiceLibraryError, VoiceSelection


def write_wav(path: Path, frames: bytes) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(frames)


class VoiceLibraryTest(unittest.TestCase):
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

    def test_explicit_routes_replace_one_variant_binding_and_support_presets(
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
            self.assertEqual(len(library.bindings()), 4)
            self.assertTrue(library.clear("Unknown"))
            self.assertIsNone(library.binding("Unknown"))
            self.assertFalse(library.clear("Unknown"))

    def test_linked_person_shares_bindings_without_merging_variants(self) -> None:
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
            self.assertIsNone(library.binding("Aderyn", variant_key="adult"))
            self.assertIsNone(library.binding("Aderyn", variant_key="child"))
            self.assertEqual(
                library.binding("Rhiannon", variant_key="child").source_sha256,
                child_choice.sha256,
            )
            self.assertTrue(library.unlink_person("Aderyn"))
            self.assertIsNone(library.binding("Aderyn", variant_key="adult"))
            self.assertEqual(
                library.binding("Rhiannon", variant_key="adult").source_sha256,
                adult_choice.sha256,
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
                child_choice.sha256,
            )
            self.assertEqual(
                library.binding("Aderyn", variant_key="child").source_sha256,
                child_variant.sha256,
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


if __name__ == "__main__":
    unittest.main()
