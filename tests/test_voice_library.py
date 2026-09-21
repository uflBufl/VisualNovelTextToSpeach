import hashlib
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.voice_library import VoiceLibrary, VoiceLibraryError


def write_wav(path: Path, frames: bytes) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(frames)


class VoiceLibraryTest(unittest.TestCase):
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

    def test_explicit_routes_replace_one_variant_binding_and_support_presets(self) -> None:
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

            self.assertEqual(library.binding("Sonetto", variant_key="old").source_id, "preset:alba")
            self.assertIsNone(library.resolve_source_path("Sonetto", variant_key="old"))
            self.assertEqual(library.binding("Narrator").route, "narrator")
            self.assertEqual(library.binding("Unknown").route, "live-fallback")
            self.assertEqual(len(library.bindings()), 4)

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
            with self.assertRaisesRegex(VoiceLibraryError, "cannot have a voice source"):
                library.select("Role", route="narrator", source_id="preset:alba")


if __name__ == "__main__":
    unittest.main()
