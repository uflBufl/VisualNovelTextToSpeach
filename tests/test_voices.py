import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    CharacterVoiceRouter,
    VoiceManifestError,
    normalize_character_name,
)


class CharacterVoiceRegistryTest(unittest.TestCase):
    def test_manifest_must_be_an_object(self):
        with TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            manifest_path.write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(
                VoiceManifestError,
                "Voice manifest must be a JSON object",
            ):
                CharacterVoiceRegistry.from_file(manifest_path)

    def test_names_are_normalized_for_ocr_variations(self):
        registry = CharacterVoiceRegistry(
            [
                CharacterVoice(
                    "Ms. NewBabel",
                    "reverse-1999-ms-newbabel",
                    aliases=("MS NEWBABEL",),
                )
            ]
        )

        self.assertEqual(
            registry.resolve("  Ms NewBabel  ").speaker,
            "reverse-1999-ms-newbabel",
        )
        self.assertEqual(
            registry.resolve("MS. NEWBABEL").speaker,
            "reverse-1999-ms-newbabel",
        )

    def test_manifest_resolves_reference_relative_to_manifest(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest_path = directory / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "voices": [
                            {
                                "character": "Lucy",
                                "speaker": "reverse-1999-lucy",
                                "reference": "references/lucy.ogg",
                                "aliases": ["LUCY"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            registry = CharacterVoiceRegistry.from_file(manifest_path)

            self.assertEqual(
                registry.resolve("Lucy").reference,
                (directory / "references" / "lucy.ogg").resolve(),
            )

    def test_manifest_resolves_multiple_references(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest_path = directory / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "voices": [
                            {
                                "character": "Marcus",
                                "speaker": "reverse-1999-marcus-v2",
                                "references": [
                                    "references/marcus-01.ogg",
                                    "references/marcus-02.ogg",
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            voice = CharacterVoiceRegistry.from_file(manifest_path).resolve("Marcus")

            self.assertEqual(
                voice.references,
                (
                    (directory / "references" / "marcus-01.ogg").resolve(),
                    (directory / "references" / "marcus-02.ogg").resolve(),
                ),
            )

    def test_duplicate_normalized_names_are_rejected(self):
        with self.assertRaisesRegex(VoiceManifestError, "Duplicate voice"):
            CharacterVoiceRegistry(
                [
                    CharacterVoice("APPLe", "first"),
                    CharacterVoice("apple", "second"),
                ]
            )

    def test_normalization_preserves_letters_and_numbers(self):
        self.assertEqual(normalize_character_name(" 37 "), "37")
        self.assertEqual(normalize_character_name("An-an Lee"), "ananlee")


class CharacterVoiceRouterTest(unittest.TestCase):
    def test_narrator_uses_configured_default_voice(self):
        tts = Mock()
        router = CharacterVoiceRouter(
            tts,
            narrator_speaker="Claribel Dervla",
        )

        router.speak("Narrator", "The storm is coming.")

        tts.speak.assert_called_once_with(
            "The storm is coming.",
            speaker="Claribel Dervla",
        )

    def test_unknown_character_uses_narrator_voice(self):
        tts = Mock()
        router = CharacterVoiceRouter(tts)

        router.speak("Unknown Person", "Hello.")

        tts.speak.assert_called_once_with("Hello.", speaker=None)

    def test_cached_character_voice_does_not_reload_reference(self):
        tts = Mock()
        tts.has_speaker.return_value = True
        registry = CharacterVoiceRegistry(
            [CharacterVoice("Lucy", "reverse-1999-lucy", Path("unused.ogg"))]
        )
        router = CharacterVoiceRouter(tts, registry)

        router.speak("LUCY", "Hello.")

        tts.speak.assert_called_once_with(
            "Hello.",
            speaker="reverse-1999-lucy",
            speaker_wav=None,
        )

    def test_character_voice_forwards_playback_guard(self):
        tts = Mock()
        tts.has_speaker.return_value = True
        playback_guard = Mock(return_value=True)
        registry = CharacterVoiceRegistry([CharacterVoice("Lucy", "reverse-1999-lucy")])

        CharacterVoiceRouter(tts, registry).speak(
            "Lucy",
            "Hello.",
            playback_guard=playback_guard,
        )

        tts.speak.assert_called_once_with(
            "Hello.",
            speaker="reverse-1999-lucy",
            speaker_wav=None,
            playback_guard=playback_guard,
        )

    def test_uncached_character_is_cloned_from_local_reference(self):
        with TemporaryDirectory() as temporary_directory:
            reference = Path(temporary_directory) / "lucy.ogg"
            reference.touch()
            tts = Mock()
            tts.has_speaker.return_value = False
            registry = CharacterVoiceRegistry(
                [CharacterVoice("Lucy", "reverse-1999-lucy", reference)]
            )
            router = CharacterVoiceRouter(tts, registry)

            router.speak("Lucy", "Hello.")

            tts.speak.assert_called_once_with(
                "Hello.",
                speaker="reverse-1999-lucy",
                speaker_wav=str(reference),
            )

    def test_uncached_character_uses_all_local_references(self):
        with TemporaryDirectory() as temporary_directory:
            references = tuple(
                Path(temporary_directory) / filename
                for filename in ("marcus-01.ogg", "marcus-02.ogg")
            )
            for reference in references:
                reference.touch()
            tts = Mock()
            tts.has_speaker.return_value = False
            registry = CharacterVoiceRegistry(
                [
                    CharacterVoice(
                        "Marcus",
                        "reverse-1999-marcus-v2",
                        references=references,
                    )
                ]
            )

            CharacterVoiceRouter(tts, registry).speak("Marcus", "Hello.")

            tts.speak.assert_called_once_with(
                "Hello.",
                speaker="reverse-1999-marcus-v2",
                speaker_wav=[str(reference) for reference in references],
            )

    def test_warm_up_synthesizes_narrator_and_every_character_without_playback(self):
        with TemporaryDirectory() as temporary_directory:
            reference = Path(temporary_directory) / "marcus.wav"
            reference.touch()
            tts = Mock()
            tts.has_speaker.return_value = False
            registry = CharacterVoiceRegistry(
                [CharacterVoice("Marcus", "reverse-1999-marcus", reference)]
            )
            progress = Mock()
            router = CharacterVoiceRouter(
                tts,
                registry,
                narrator_speaker="Claribel Dervla",
            )

            count = router.warm_up(progress=progress)

        self.assertEqual(count, 2)
        self.assertEqual(
            tts.synthesize.call_args_list,
            [
                unittest.mock.call(
                    "Voice ready.",
                    speaker="Claribel Dervla",
                ),
                unittest.mock.call(
                    "Voice ready.",
                    speaker="reverse-1999-marcus",
                    speaker_wav=str(reference),
                ),
            ],
        )
        tts.speak.assert_not_called()
        self.assertEqual(
            progress.call_args_list,
            [
                unittest.mock.call(1, 2, "Narrator"),
                unittest.mock.call(2, 2, "Marcus"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
