import hashlib
import os
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from vntts.voice_library import VoiceLibrary, VoiceLibraryError


def write_wav(path: Path, frames: bytes) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(frames)


class VoiceLibraryTest(unittest.TestCase):
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

    def test_windows_reference_is_opened_in_binary_mode(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "voice.wav"
            write_wav(reference, b"\x1a\r\n")
            real_open = os.open
            binary_flag = 0x40000000
            flags_seen = 0

            def windows_open(path, flags, *args):
                nonlocal flags_seen
                if Path(path) == reference:
                    flags_seen = flags
                return real_open(path, flags & ~binary_flag, *args)

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
            duplicate = library.discover("Mrs. Owen", first)
            library.discover("Mrs. Owen", second, bind_if_missing=True)

            self.assertEqual(selected.sha256, duplicate.sha256)
            self.assertEqual(len(library.alternatives("Mrs Owen")), 2)
            self.assertEqual(library.binding("Mrs Owen").source_sha256, selected.sha256)
            self.assertEqual(library.resolve_source_path("Mrs Owen"), selected.path)
            self.assertEqual(len(list(library.blobs_path.glob("*.wav"))), 2)

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
