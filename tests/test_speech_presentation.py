import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts.generated_audio import GeneratedAudioFallbackBackend
from vntts.settings import AppSettings
from vntts.speech_presentation import (
    engine_model_label,
    narrator_voice_label,
    reading_policy_label,
    speech_configuration_label,
    speech_runtime_label,
)


class SpeechPresentationTest(unittest.TestCase):
    def test_compute_reports_actual_runtime_and_never_guesses_from_settings(self):
        backend = SimpleNamespace(runtime_status="GPU: RTX 2070 SUPER; auxiliary: CPU")
        self.assertIn("GPU: RTX 2070 SUPER", speech_runtime_label(backend))
        wrapped = object.__new__(GeneratedAudioFallbackBackend)
        wrapped.live_backend = backend
        self.assertEqual(speech_runtime_label(wrapped), speech_runtime_label(backend))
        backend.runtime_status = None
        backend.device = "cuda"
        self.assertIn("not running", speech_runtime_label(backend))
        del backend.runtime_status
        self.assertIn("unknown", speech_runtime_label(backend))
        self.assertIn("not loaded", speech_runtime_label(None))
        backend.health = {"device": "cuda", "accelerator": {"name": "RTX 2070 SUPER"}}
        backend.process = Mock()
        backend.process.poll.return_value = None
        self.assertEqual(
            speech_runtime_label(backend), "Compute: CUDA (RTX 2070 SUPER)"
        )
        backend.process.poll.return_value = 1
        self.assertIn("not running", speech_runtime_label(backend))

    def test_default_and_explicit_voice_are_the_same_across_journeys(self):
        self.assertIn("Narrator voice: Alba", speech_configuration_label(AppSettings()))
        settings = AppSettings(
            speech_backend="moss-tts",
            tts_model="my-model.gguf",
            voice_assignments={"Narrator": "character:centurion"},
        )
        self.assertIn("Narrator voice: Centurion", speech_configuration_label(settings))
        self.assertIn("Model: my-model.gguf", speech_configuration_label(settings))
        self.assertNotIn(
            "irrelevant-xtts", engine_model_label("pocket-tts", "irrelevant-xtts")
        )
        self.assertIn(
            "voice cloning", engine_model_label("pocket-tts", pocket_cloning=True)
        )

    def test_moss_model_label_matches_native_selection_without_loading_files(self):
        with (
            patch.dict(
                os.environ, {"VNTTS_MOSS_CPP_EXECUTABLE": "", "VNTTS_MOSS_GGUF": ""}
            ),
            patch("sys.platform", "win32"),
        ):
            for model in (None, "shraey/MOSS-TTS-Local-Transformer-v1.5-MLX-int8"):
                self.assertIn(
                    "Model: moss-tts-local-1.5-q8_0.gguf",
                    engine_model_label("moss-tts", model),
                )
            self.assertIn(
                "Model: custom.gguf",
                engine_model_label("moss-tts", "/models/custom.gguf"),
            )
        with (
            patch.dict(
                os.environ, {"VNTTS_MOSS_CPP_EXECUTABLE": "", "VNTTS_MOSS_GGUF": ""}
            ),
            patch("sys.platform", "darwin"),
            patch("platform.machine", return_value="arm64"),
        ):
            self.assertIn("shraey/MOSS", engine_model_label("moss-tts"))
            self.assertIn(
                "Model: custom/mlx", engine_model_label("moss-tts", "custom/mlx")
            )
            self.assertIn(
                "Model: custom.gguf",
                engine_model_label("moss-tts", "/models/custom.gguf"),
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
        self.assertIn("TTS is used for missing audio", reading_policy_label(settings))
        self.assertIn(
            "bypassed",
            reading_policy_label(settings.updated(audio_source_policy="live-tts-only")),
        )
        self.assertIn(
            "Narrator override",
            reading_policy_label(settings.updated(force_live_narrator=True)),
        )
