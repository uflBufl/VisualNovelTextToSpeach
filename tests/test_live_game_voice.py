import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

from r1999extractor.story_voice_candidates import StoryVoiceCandidateError

from tests.pregeneration_fixtures import clean_wav_bytes
from vntts.game_audio_decoder import DecoderSetupCancelled
from vntts.game_content_importer import (
    GameContentImportCancelled,
    Reverse1999GameImporter,
)
from vntts.live_game_voice import LiveGameVoiceReferences


class LiveGameVoiceReferencesTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = self.write_manifest("first")
        self.reference = SimpleNamespace(
            line_id="playable-voice:3061:1",
            text="A source reference spoken by this character.",
            source_bank="mianvoc_hero3061.bnk",
        )
        self.importer = Mock(spec=Reverse1999GameImporter)
        self.importer.narrator_references.return_value = (self.reference,)
        self.importer.prepare_voice_roles.return_value = self.manifest
        self.references = LiveGameVoiceReferences(self.importer)

    def write_manifest(self, name, *, seconds=4.0, amplitude=0.1):
        root = self.root / name
        root.mkdir()
        (root / "reference.wav").write_bytes(
            clean_wav_bytes(seconds=seconds, amplitude=amplitude)
        )
        path = root / "manifest.json"
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Horropedia spoken reference 1",
                            "speaker": "narrator-source",
                            "references": ["reference.wav"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def resolve(self, character="Horropedia", cancellation=None):
        return self.references.resolve(character, cancellation=cancellation)

    def test_resolves_character_without_chapter_preparation_and_reuses_reference(self):
        voice = self.resolve()
        self.assertIsNotNone(voice)
        self.assertEqual(voice.character, "Horropedia")
        self.assertEqual(voice.source_character, "Horropedia")
        self.assertEqual(voice.speaker, "horropedia")
        self.assertEqual(voice.reference_transcript, self.reference.text)
        self.assertIs(self.resolve(" HORROPEDIA "), voice)
        self.importer.prepare_voice_roles.assert_called_once_with(
            ("Horropedia",),
            None,
            narrator=True,
            narrator_line_id=self.reference.line_id,
            speech_backend="pocket-tts",
        )
        self.importer.narrator_references.assert_called_once_with("Horropedia")
        self.importer.import_installed.assert_not_called()
        self.importer.prepare_voice_candidates.assert_not_called()

    def test_missing_character_and_ambiguous_manifest_keep_cached_fallback(self):
        self.importer.narrator_references.return_value = ()
        self.assertIsNone(self.resolve("Unknown"))
        self.assertIsNone(self.resolve("Unknown"))
        self.importer.prepare_voice_roles.assert_not_called()
        self.importer.narrator_references.return_value = (self.reference,)
        document = json.loads(self.manifest.read_text())
        document["voices"].append(
            {**document["voices"][0], "character": "Another source"}
        )
        self.manifest.write_text(json.dumps(document), encoding="utf-8")
        self.assertIsNone(self.resolve())
        self.assertIsNone(self.resolve())
        self.importer.prepare_voice_roles.assert_called_once()

    def test_rejected_or_long_main_reference_tries_next_ranked_spoken_line(self):
        for label, arguments in (
            ("quiet", {"amplitude": 0.0}),
            ("long", {"seconds": 13.0}),
        ):
            with self.subTest(label=label):
                rejected = self.write_manifest(label, **arguments)
                second = SimpleNamespace(
                    **{**vars(self.reference), "line_id": "playable-voice:3061:2"}
                )
                self.importer.narrator_references.return_value = (
                    self.reference,
                    second,
                )
                self.importer.prepare_voice_roles.side_effect = [
                    rejected,
                    self.manifest,
                ]
                references = LiveGameVoiceReferences(self.importer)
                voice = references.resolve("Horropedia")
                self.assertEqual(
                    voice.reference,
                    (self.manifest.parent / "reference.wav").resolve(),
                )
                self.assertEqual(
                    self.importer.prepare_voice_roles.call_args.kwargs[
                        "narrator_line_id"
                    ],
                    second.line_id,
                )

    def test_new_runtime_refreshes_the_role(self):
        self.assertIsNotNone(self.resolve())
        self.references = LiveGameVoiceReferences(self.importer)
        self.assertIsNotNone(self.resolve())
        self.assertEqual(self.importer.prepare_voice_roles.call_count, 2)

    def test_cached_lookup_does_not_reread_source_or_reference(self):
        voice = self.resolve()
        voice.reference.unlink()
        self.assertIs(self.resolve(), voice)
        self.importer.prepare_voice_roles.assert_called_once()

    def test_failed_reference_listing_keeps_cached_fallback(self):
        self.importer.narrator_references.side_effect = StoryVoiceCandidateError(
            "No suitable spoken references"
        )
        with self.assertLogs("vntts.live_game_voice", level="WARNING"):
            self.assertIsNone(self.resolve())
        self.assertIsNone(self.resolve())
        self.importer.narrator_references.assert_called_once()

    def test_cancellation_propagates_and_allows_retry(self):
        for error in (
            GameContentImportCancelled("cancelled"),
            DecoderSetupCancelled("cancelled"),
        ):
            with self.subTest(error=error):
                self.importer.prepare_voice_roles.side_effect = [error, self.manifest]
                references = LiveGameVoiceReferences(self.importer)
                with self.assertRaises(type(error)):
                    references.resolve("Horropedia")
                self.assertIsNotNone(references.resolve("Horropedia"))

    def test_cancelled_lookup_does_not_return_cached_voice(self):
        self.assertIsNotNone(self.resolve())
        cancellation = Event()
        cancellation.set()
        with self.assertRaises(DecoderSetupCancelled):
            self.resolve(cancellation=cancellation)

    def test_unreadable_reference_keeps_fallback(self):
        self.manifest.with_name("reference.wav").write_bytes(b"not a WAV")
        with self.assertLogs("vntts.live_game_voice", level="WARNING"):
            self.assertIsNone(self.resolve())


if __name__ == "__main__":
    unittest.main()
