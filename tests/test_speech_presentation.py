import unittest

from vntts.settings import AppSettings
from vntts.speech_presentation import (
    engine_model_label,
    narrator_voice_label,
    reading_policy_label,
    speech_configuration_label,
)


class SpeechPresentationTest(unittest.TestCase):
    def test_default_and_explicit_voice_are_the_same_across_journeys(self):
        self.assertIn("Narrator voice: Alba", speech_configuration_label(AppSettings()))
        settings = AppSettings(
            speech_backend="moss-tts",
            tts_model="my/model",
            voice_assignments={"Narrator": "character:centurion"},
        )
        self.assertIn("Narrator voice: Centurion", speech_configuration_label(settings))
        self.assertIn("Model: my/model", speech_configuration_label(settings))
        self.assertIn("shraey/MOSS", engine_model_label("moss-tts"))
        self.assertNotIn(
            "irrelevant-xtts", engine_model_label("pocket-tts", "irrelevant-xtts")
        )
        self.assertIn(
            "voice cloning", engine_model_label("pocket-tts", pocket_cloning=True)
        )

    def test_pack_narrator_does_not_invent_identity_before_runtime_load(self):
        settings = AppSettings(voice_assignments={"Narrator": "character:narrator"})
        self.assertIn("after loading", narrator_voice_label(settings))
        self.assertIn(
            "Narrator voice: Centurion",
            speech_configuration_label(settings, narrator="Centurion"),
        )

    def test_reading_policy_discloses_saved_audio_and_override(self):
        settings = AppSettings(
            generated_audio_manifest="saved.json",
            audio_source_policy="prefer-generated",
        )
        self.assertIn("Play prepared recordings", reading_policy_label(settings))
        self.assertIn("TTS is used for lines", reading_policy_label(settings))
        self.assertIn(
            "bypassed",
            reading_policy_label(settings.updated(audio_source_policy="live-tts-only")),
        )
        self.assertIn(
            "Narrator override",
            reading_policy_label(settings.updated(force_live_narrator=True)),
        )
