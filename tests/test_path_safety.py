import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts.authoring.generation_manifest import (
    BulkGenerationError,
    safe_generation_relative_path,
)
from vntts.live_replay import _read_contained_file
from vntts.live_replay_sequence_seal import SequenceReplaySealError, _read_regular_file
from vntts.live_speaker_corpus import LiveSpeakerCorpus
from vntts.path_safety import contained_regular_file, safe_relative_path
from vntts.voice_library import VoiceLibrary
from vntts.voices import VoiceManifestError, _read_owned_voice_reference


class PathBoundaryError(RuntimeError):
    pass


class PathSafetyTest(unittest.TestCase):
    def test_generation_relative_path_rejects_nul_with_its_domain_error(self):
        self.assertEqual(
            safe_generation_relative_path("./nested//voice.wav", "Audio"),
            Path("nested/voice.wav"),
        )
        for value in ("\x00", "nested/bad\x00.wav"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(BulkGenerationError, "relative POSIX path"),
            ):
                safe_generation_relative_path(value, "Audio")

    def test_embedded_nul_uses_requested_error_boundary(self):
        with TemporaryDirectory() as directory:
            for value in ("\x00", "nested/bad\x00.json", Path("bad\x00.json")):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(PathBoundaryError, "POSIX-relative"):
                        safe_relative_path(
                            str(value), "artifact", error_type=PathBoundaryError
                        )
                    with self.assertRaisesRegex(PathBoundaryError, "leaves its root"):
                        contained_regular_file(
                            directory, value, "artifact", error_type=PathBoundaryError
                        )


class RegularFileAdmissionTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO admission")
    def test_readers_reject_regular_path_swapped_to_fifo_before_open(self):
        native_open = os.open
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            reference = root / "voice.wav"
            fifo = root / "replacement"
            readers = (
                lambda: VoiceLibrary(root / "library").discover("Role", reference),
                lambda: _read_owned_voice_reference(root, reference),
                lambda: LiveSpeakerCorpus.load(reference),
                lambda: _read_regular_file(reference, "Sequence plan"),
                lambda: _read_contained_file(root, reference.name, "Replay item"),
            )
            for index, read in enumerate(readers):
                with self.subTest(reader=index):
                    reference.unlink(missing_ok=True)
                    reference.write_bytes(b"regular file before acquisition")
                    os.mkfifo(fifo)
                    descriptors = []

                    def swap_and_open(path, flags):
                        self.assertEqual(Path(path), reference)
                        self.assertTrue(
                            flags & os.O_NONBLOCK,
                            "reader must not wait for a FIFO writer",
                        )
                        fifo.replace(reference)
                        descriptor = native_open(path, flags)
                        descriptors.append(descriptor)
                        return descriptor

                    with patch("vntts.path_safety.os.open", side_effect=swap_and_open):
                        with self.assertRaisesRegex(
                            (ValueError, VoiceManifestError, SequenceReplaySealError),
                            "regular file",
                        ):
                            read()
                    self.assertEqual(len(descriptors), 1)
                    with self.assertRaises(OSError):
                        os.fstat(descriptors[0])


if __name__ == "__main__":
    unittest.main()
