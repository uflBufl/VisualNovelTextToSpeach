import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts.generated_audio import GeneratedAudioFallbackBackend
from vntts.settings import AppSettings
from vntts.speech_presentation import (
    compact_runtime_label,
    engine_model_label,
    narrator_voice_label,
    playback_labels,
    playback_status_label,
    readable_model_name,
    reading_policy_label,
    speech_configuration_label,
    speech_configuration_rows,
    speech_model_label,
    speech_runtime_label,
)
from vntts.voice_library import VoiceLibrary


class SpeechPresentationTest(unittest.TestCase):
    def test_model_only_label_uses_model_identity_without_backend_recommendations(self):
        self.assertEqual(
            speech_model_label("pocket-tts", "unused-model", pocket_cloning=True),
            "Pocket TTS with voice cloning",
        )
        self.assertEqual(speech_model_label("pocket-tts"), "Pocket TTS preset-only")
        self.assertEqual(
            speech_model_label("moss-tts", "moss-tts-local-v1.5-mlx-int8"),
            "MOSS Local v1.5 · 8-bit MLX",
        )
        self.assertEqual(speech_model_label("moss-tts", "custom/model"), "model")

    def test_compact_compute_retains_device_and_cpu_fallback_warning(self):
        raw = (
            "Compute: GPU: RTX 2070 SUPER (37 GPU layers); audio model/codec: CPU; "
            "auxiliary CPU workers: 4; fallback: memory pressure"
        )
        compact = compact_runtime_label(raw)
        self.assertNotIn("layers", compact)
        self.assertNotIn("workers", compact)
        self.assertIn("RTX 2070 SUPER", compact)
        self.assertIn("codec: CPU", compact)
        self.assertIn("fallback: memory pressure", compact)

    def test_routine_playback_statuses_are_short_without_losing_route_kind(self):
        for source, expected in (
            (
                "Generated audio (line private-id)\nRecorded with: moss-tts; model: private-model; voice ID: private-voice",
                "Prepared recording · no generation",
            ),
            (
                "Original game audio (line private-id, completion 1.25s)",
                "Played by the game · no generation",
            ),
            ("Live TTS (pocket-tts)", "New speech"),
            ("MOSS fresh generation (voice private-id)", "MOSS · new speech"),
            (
                "MOSS memory cache (voice private-id)",
                "Saved preview or cached speech · no generation",
            ),
            (
                "MOSS persistent cache (voice private-id)",
                "Saved preview or cached speech · no generation",
            ),
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    playback_status_label("Audio source for Nick: Guard: " + source),
                    "Nick: Guard: " + expected,
                )

    def test_playback_status_preserves_warnings_and_unrecognized_routes(self):
        messages = (
            "Audio underrun detected; live speech prefetch disabled temporarily",
            "Auto advance paused: source-audio completion is unavailable",
            "Audio source for Narrator: Authorized live fallback (moss-tts/model)",
            "Audio source for Narrator: Waiting for prepared audio (line private-id)",
            "Audio source for Narrator: Authorized silent audio event omission",
            "Audio source for Narrator: Original game audio (line private-id, completion unavailable)",
            "Audio source for Narrator: Original game cue (1.00s including post-roll), then Authorized live fallback (moss-tts/model)",
            "Audio source for Narrator: unknown route details",
            "Audio source for Narrator: Generated audio failed to load",
            "Audio source for Narrator: Generated audio (line private-id)\nRecorded with: moss-tts\nWarning: audio incomplete",
            "",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(playback_status_label(message), message)

    def test_live_voice_keeps_variant_and_hides_only_explicit_internal_identity(self):
        voice, _ = playback_labels(
            "MOSS live", "Rhiannon (child); voice ID: private-id"
        )
        self.assertEqual(voice, "Rhiannon (child)")
        self.assertEqual(playback_labels("MOSS live", "Rhiannon (child)")[0], voice)

    def test_candidate_identity_is_readable_across_configuration_and_playback(self):
        identity = "a" * 64
        diagnostic = f"Player candidate Centurion {identity[:12]}; voice ID: player-candidate:{identity}"
        settings = AppSettings(speech_backend="moss-tts")
        self.assertEqual(
            speech_configuration_rows(settings, narrator=diagnostic)[0],
            ("Narrator", "Centurion"),
        )
        summary = speech_configuration_label(
            settings, narrator=diagnostic, compact=True
        )
        self.assertIn("Narrator voice: Centurion", summary)
        self.assertNotIn("Player candidate", summary)
        self.assertNotIn(identity, summary)
        self.assertEqual(playback_labels("MOSS live", diagnostic)[0], "Centurion")
        recorded = (
            "Generated audio (line 1)\nRecorded with: moss-tts; source voice: "
            + diagnostic
        )
        self.assertEqual(
            playback_labels(recorded, "Different live narrator")[0], "Centurion"
        )

    def test_voice_display_preserves_custom_names_and_unproven_candidate_names(self):
        candidate_name = "Player candidate My custom voice aaaaaaaaaaaa"
        for name, identity in (
            ("Rhiannon (child)", "imported-voice"),
            ("Alba", "alba"),
            (candidate_name, "custom-id"),
            (candidate_name, "player-candidate:" + "b" * 64),
            (candidate_name, "player-candidate:not-a-digest"),
        ):
            with self.subTest(name=name, identity=identity):
                self.assertEqual(
                    playback_labels("MOSS live", name + "; voice ID: " + identity)[0],
                    name,
                )
        self.assertEqual(
            playback_labels("MOSS live", candidate_name)[0], candidate_name
        )

    def test_live_tts_is_new_speech_with_engine_kept_in_playback_details(self):
        for backend in (
            "pocket-tts",
            "coqui-xtts",
            "chatterbox-nano",
            "qwen-tts",
            "moss-tts",
            "moss-tts-delay",
            "custom-engine",
        ):
            with self.subTest(backend=backend):
                self.assertEqual(
                    playback_labels(f"Live TTS ({backend})", "Alba"),
                    ("Alba", "New speech"),
                )

    def test_moss_cache_is_not_presented_as_new_speech(self):
        for cache in ("memory cache", "persistent cache"):
            with self.subTest(cache=cache):
                self.assertEqual(
                    playback_labels(f"MOSS {cache} (voice Alba)", "Alba"),
                    ("Alba", "Saved preview or cached speech · no generation"),
                )
        self.assertEqual(
            playback_labels("MOSS fresh generation (voice Alba)", "Alba"),
            ("Alba", "MOSS · new speech"),
        )

    def test_saved_voice_identity_is_separate_from_live_defaults_and_hashes(self):
        source = (
            "Generated audio (line opaque-id)\nRecorded with: moss-tts; "
            "model: openmoss-cpp:sha256:"
            + "a" * 64
            + "; voice role: Aderyn; source voice: Rhiannon (child); voice ID: voice-123"
        )
        voice, label = playback_labels(source, "Alba")
        self.assertEqual(voice, "Rhiannon (child)")
        self.assertIn("no generation", label)
        self.assertNotIn("sha256", label)
        self.assertNotIn("opaque-id", label)
        self.assertNotIn("Alba", label)
        self.assertIn(
            "not recorded", playback_labels("Generated audio (line 1)", "Alba")[0]
        )

    def test_compact_model_identity_keeps_exact_version_in_details(self):
        model = "shraey/MOSS-TTS-Local-Transformer-v1.5-MLX-int8"
        with patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=False):
            label = engine_model_label("moss-tts", model, compact=True)
            self.assertIn("8-bit MLX", label)
            self.assertNotIn("shraey/", label)
            self.assertIn(model, engine_model_label("moss-tts", model))

    def test_snapshot_paths_show_the_model_and_keep_exact_path_in_details(self):
        model = (
            "/cache/models--shraey--MOSS-TTS-Local-Transformer-v1.5-MLX-int8/"
            "snapshots/" + "a" * 40
        )
        for path in (model, model.replace("/", "\\") + "\\"):
            with self.subTest(path=path):
                self.assertEqual(readable_model_name(path), "Local v1.5 · 8-bit MLX")
        with patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=False):
            self.assertIn(model, engine_model_label("moss-tts", model))
        self.assertEqual(readable_model_name("/custom/snapshots/model"), "model")
        self.assertEqual(readable_model_name("/models--org--/snapshots/rev"), "rev")

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
        for health, label in (
            (
                {"device": "metal", "accelerator": {"name": "Test chip"}},
                "Compute: METAL (Test chip)",
            ),
            ({"device": "metal"}, "Compute: METAL"),
            ({"device": "cpu"}, "Compute: CPU"),
        ):
            with self.subTest(health=health):
                backend.health = health
                self.assertEqual(speech_runtime_label(backend), label)
        backend.process.poll.return_value = 1
        self.assertIn("not running", speech_runtime_label(backend))

    def test_default_and_explicit_voice_are_the_same_across_journeys(self):
        self.assertIn("Narrator voice: Alba", speech_configuration_label(AppSettings()))
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        voices = VoiceLibrary(Path(directory.name) / "voices")
        voices.select("Narrator", route="voice", source_id="character:centurion")
        settings = AppSettings(
            speech_backend="moss-tts",
            tts_model="my-model.gguf",
        )
        self.assertEqual(narrator_voice_label(settings, voices), "Centurion")
        self.assertIn("Model: my-model.gguf", speech_configuration_label(settings))
        self.assertIn(
            "Configured model: my-model.gguf", speech_configuration_label(settings)
        )
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
