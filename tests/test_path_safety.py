import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.pregeneration_fixtures import ambiguous_fixture
from vntts import (
    assets,
    game_audio_decoder,
    pregeneration_audition,
    support,
    versioned_json,
    voice_candidate_cache,
)
from vntts.authoring.generation_manifest import (
    BulkGenerationError,
    safe_generation_relative_path,
)
from vntts.live_replay import _read_contained_file
from vntts.live_replay_sequence_seal import SequenceReplaySealError, _read_regular_file
from vntts.live_speaker_corpus import LiveSpeakerCorpus
from vntts.path_safety import contained_regular_file, safe_relative_path
from vntts.runtime_ownership import read_record
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


class BoundedDocumentAdmissionTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO admission")
    def test_bounded_readers_reject_regular_path_swapped_to_fifo_with_own_fallbacks(
        self,
    ):
        native_open, native_path_open = os.open, Path.open
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, _group, _manifest = ambiguous_fixture(root)
            reference = root / "verified.json"
            fifo = root / "replacement"
            default = object()
            readers = (
                ("runtime", lambda: read_record(reference), None, {}),
                (
                    "snapshot",
                    lambda: versioned_json.read_versioned_json_snapshot(
                        reference, schema_version=1, document_name="test"
                    ),
                    OSError,
                    None,
                ),
                (
                    "revision",
                    lambda: versioned_json.file_revision(reference),
                    OSError,
                    None,
                ),
                (
                    "voice manifest",
                    lambda: assets.read_json(reference, default),
                    None,
                    default,
                ),
                (
                    "model checksum",
                    lambda: assets.ModelAssetManager._read_checksum_manifest(reference),
                    assets.ModelIntegrityError,
                    None,
                ),
                (
                    "support tail",
                    lambda: support._read_bounded_json_lines(reference, 128),
                    None,
                    (),
                ),
                (
                    "support timelines",
                    lambda: support._read_previous_generation_timelines(reference),
                    None,
                    None,
                ),
                (
                    "support startup",
                    lambda: support.PregenerationSupportState(reference).report(),
                    None,
                    {"available": False},
                ),
                (
                    "support summary",
                    lambda: support._pregeneration_state_summary(reference),
                    None,
                    {"available": False},
                ),
                (
                    "support pack",
                    lambda: support._active_pack_identity(str(reference), 0, 2),
                    None,
                    {"available": False},
                ),
                (
                    "support story",
                    lambda: support._active_story_ids(root, {"path": reference.name}),
                    None,
                    {"active_story_ids_available": False},
                ),
                (
                    "candidate references",
                    lambda: voice_candidate_cache._references_in_document(
                        root, reference
                    ),
                    None,
                    None,
                ),
                (
                    "decoder",
                    lambda: game_audio_decoder._probe_cached_decoder(
                        root / "decoder.exe", None
                    ),
                    None,
                    False,
                ),
                (
                    "preview sidecar",
                    lambda: pregeneration_audition._cached_preview_metadata(
                        root / "verified.wav", "identity", plan
                    ),
                    pregeneration_audition.VoiceAuditionError,
                    None,
                ),
            )
            for label, read, error_type, expected in readers:
                with self.subTest(reader=label):
                    reference.unlink(missing_ok=True)
                    reference.write_bytes(b"{}")
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

                    def reject_blocking_path_open(path, *args, **kwargs):
                        if Path(path) == reference:
                            self.fail("bounded input must not use blocking Path.open")
                        return native_path_open(path, *args, **kwargs)

                    with (
                        patch("vntts.path_safety.os.open", side_effect=swap_and_open),
                        patch.object(Path, "open", reject_blocking_path_open),
                    ):
                        if error_type is not None:
                            with self.assertRaisesRegex(error_type, "regular file"):
                                read()
                        else:
                            result = read()
                            if isinstance(expected, dict):
                                self.assertEqual(
                                    {key: result[key] for key in expected}, expected
                                )
                            else:
                                self.assertEqual(result, expected)
                    self.assertEqual(len(descriptors), 1)
                    with self.assertRaises(OSError):
                        os.fstat(descriptors[0])


if __name__ == "__main__":
    unittest.main()
