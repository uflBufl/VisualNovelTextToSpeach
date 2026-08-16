import hashlib
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import numpy as np
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    text_sha256,
    write_generated_audio_manifest,
)

from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioLibrary,
    PreparedGeneratedAudio,
    PreparedSourceAudioPassThrough,
)
from vntts.speech_backend import SpeechBackendCapabilities


class FakeAudioOutput:
    def __init__(self):
        self.plays = []
        self.stopped = False

    def query_devices(self, _device=None, _kind=None):
        return {"default_samplerate": 24_000}

    def play(self, samples, sample_rate, **options):
        self.plays.append((np.asarray(samples), sample_rate, options))

    def wait(self):
        return Mock(output_underflow=False)

    def stop(self):
        self.stopped = True


def write_wav(path, samples, sample_rate=24_000):
    values = np.asarray(samples, dtype=np.float32)
    pcm = np.round(np.clip(values, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())


class GeneratedAudioTest(unittest.TestCase):
    def create_library(self, root, *, text="Hello."):
        audio = root / "audio" / "line.wav"
        audio.parent.mkdir()
        write_wav(audio, [0.0, 0.25, -0.25, 0.0])
        manifest = root / "generated-audio.json"
        write_generated_audio_manifest(
            manifest,
            {},
            [
                {
                    "line_id": "game:1",
                    "text_sha256": text_sha256(text),
                    "audio": "audio/line.wav",
                    "audio_format": "wav-pcm16-mono",
                    "audio_sha256": sha256_file(audio),
                    "sample_rate": 24_000,
                    "sample_count": 4,
                }
            ],
        )
        return GeneratedAudioLibrary(GeneratedAudioIndex.load(manifest)), audio

    def create_resolver(
        self,
        *,
        text="Hello.",
        source_audio_status="unknown",
        source_audio_id=None,
        source_audio_duration_seconds=None,
    ):
        return ChapterVoicePreloader(
            [
                ChapterDialogue(
                    "game:1",
                    "1",
                    1,
                    "Ada",
                    text,
                    text_sha256(text),
                    source_audio_status,
                    source_audio_id,
                    source_audio_duration_seconds,
                )
            ]
        )

    def create_live_backend(self):
        backend = Mock()
        backend.name = "live-test"
        backend.capabilities = SpeechBackendCapabilities(True, False, True)
        backend.prepare.return_value = "live-audio"
        backend.play.return_value = True
        backend.stop.return_value = False
        return backend

    def test_exact_line_uses_verified_generated_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            library, _audio = self.create_library(root)
            live = self.create_live_backend()
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                self.create_resolver(),
                audio_output=FakeAudioOutput(),
            )

            prepared = backend.prepare("ADA", "Hello.")

        self.assertIsInstance(prepared, PreparedGeneratedAudio)
        self.assertEqual(prepared.line_id, "game:1")
        live.prepare.assert_not_called()
        self.assertEqual(backend.last_synthesis_ms, 0.0)
        self.assertEqual(backend.last_route_trace.effective_source, "generated")
        self.assertEqual(backend.last_route_trace.match_result, "exact")
        self.assertEqual(backend.last_route_trace.line_id, "game:1")
        self.assertEqual(
            backend.last_route_trace.artifact_preflight_state,
            "generated-audio-entry-verified",
        )

    def test_generated_manifest_declares_line_for_early_prefix_routing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            library, _audio = self.create_library(root)
            resolver = self.create_resolver()
            backend = GeneratedAudioFallbackBackend(
                self.create_live_backend(),
                library,
                resolver,
                audio_output=FakeAudioOutput(),
            )

            self.assertTrue(backend.has_generated_line(resolver.dialogue[0]))

    def test_game_audio_fallback_reason_is_kept_with_generated_route(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            library, _audio = self.create_library(root)
            backend = GeneratedAudioFallbackBackend(
                self.create_live_backend(),
                library,
                self.create_resolver(source_audio_status="missing"),
                audio_source_policy="prefer-game-audio",
                audio_output=FakeAudioOutput(),
            )
            backend.set_live_mode_active(True)

            prepared = backend.prepare("Ada", "Hello.")

        self.assertIsInstance(prepared, PreparedGeneratedAudio)
        self.assertEqual(backend.last_route_trace.effective_source, "generated")
        self.assertEqual(
            backend.last_route_trace.fallback_reason,
            "source-audio-missing",
        )
        self.assertEqual(backend.last_route_trace.match_result, "exact")

    def test_text_mismatch_and_non_default_speed_fall_back_to_live_tts(self):
        with TemporaryDirectory() as directory:
            library, _audio = self.create_library(Path(directory))
            live = self.create_live_backend()
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                self.create_resolver(),
                speed=1.1,
                audio_output=FakeAudioOutput(),
            )

            self.assertEqual(backend.prepare("Ada", "Hello."), "live-audio")
            self.assertEqual(backend.prepare("Ada", "Changed."), "live-audio")

        self.assertEqual(live.prepare.call_count, 2)

    def test_manual_voice_override_skips_matching_generated_audio(self):
        with TemporaryDirectory() as directory:
            library, _audio = self.create_library(Path(directory))
            live = self.create_live_backend()
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                self.create_resolver(),
                audio_output=FakeAudioOutput(),
            )
            backend.voice_override = lambda character: character == "Ada"

            prepared = backend.prepare("Ada", "Hello.")

        self.assertEqual(prepared, "live-audio")
        live.prepare.assert_called_once_with("Ada", "Hello.")
        self.assertEqual(
            backend.last_route_trace.fallback_reason,
            "manual-voice-override",
        )
        self.assertEqual(backend.last_route_trace.match_result, "skipped")

    def test_available_game_audio_bypasses_generated_and_live_tts(self):
        with TemporaryDirectory() as directory:
            library, _audio = self.create_library(Path(directory))
            live = self.create_live_backend()
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                self.create_resolver(
                    source_audio_status="available",
                    source_audio_id="voice-7",
                ),
                audio_source_policy="prefer-game-audio",
                audio_output=FakeAudioOutput(),
            )
            backend.set_live_mode_active(True)

            prepared = backend.prepare("Ada", "Hello.")

        self.assertIsInstance(prepared, PreparedSourceAudioPassThrough)
        self.assertEqual(prepared.source_audio_id, "voice-7")
        self.assertIsNone(prepared.completion_seconds)
        self.assertEqual(backend.last_audio_source, "game")
        live.prepare.assert_not_called()

    def test_auto_advance_falls_back_when_game_audio_has_no_completion(self):
        live = self.create_live_backend()
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(
                source_audio_status="available",
                source_audio_id="voice-7",
            ),
            audio_source_policy="prefer-game-audio",
            require_source_audio_completion=True,
            audio_output=FakeAudioOutput(),
        )
        backend.set_live_mode_active(True)

        prepared = backend.prepare("Ada", "Hello.")

        self.assertEqual(prepared, "live-audio")
        self.assertEqual(backend.last_audio_source, "live:live-test")
        self.assertIn(
            "source-audio-completion-unavailable",
            backend.last_route_trace.fallback_reason,
        )
        self.assertFalse(backend.will_use_source_audio("Ada", "Hello."))

    def test_available_game_audio_is_known_before_unknown_voice_prompting(self):
        live = self.create_live_backend()
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(source_audio_status="available"),
            audio_source_policy="prefer-game-audio",
            audio_output=FakeAudioOutput(),
        )

        self.assertFalse(backend.will_use_source_audio("Ada", "Hello."))
        backend.set_live_mode_active(True)

        self.assertTrue(backend.will_use_source_audio("Ada", "Hello."))
        self.assertFalse(backend.will_use_source_audio("Ada", "Different text."))

    def test_story_audio_routing_works_without_generated_library(self):
        live = self.create_live_backend()
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(source_audio_status="available"),
            audio_source_policy="prefer-game-audio",
            audio_output=FakeAudioOutput(),
        )
        backend.set_live_mode_active(True)

        prepared = backend.prepare("Ada", "Hello.")

        self.assertIsInstance(prepared, PreparedSourceAudioPassThrough)
        self.assertTrue(backend.play(prepared))
        self.assertIsNone(backend.last_playback_ms)
        live.prepare.assert_not_called()

    def test_story_audio_with_explicit_completion_waits_before_finishing(self):
        live = self.create_live_backend()
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(
                source_audio_status="available",
                source_audio_duration_seconds=0.001,
            ),
            audio_source_policy="prefer-game-audio",
            audio_output=FakeAudioOutput(),
        )
        backend.set_live_mode_active(True)

        prepared = backend.prepare("Ada", "Hello.")

        self.assertEqual(prepared.completion_seconds, 0.001)
        self.assertEqual(prepared.completion_source, "story-index")
        self.assertTrue(backend.play(prepared))
        self.assertIsNotNone(backend.last_playback_ms)

    def test_one_time_read_does_not_silently_pass_through_game_audio(self):
        live = self.create_live_backend()
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(source_audio_status="available"),
            audio_output=FakeAudioOutput(),
        )

        prepared = backend.prepare("Ada", "Hello.")

        self.assertEqual(prepared, "live-audio")
        live.prepare.assert_called_once_with("Ada", "Hello.")

    def test_live_tts_policy_skips_game_and_generated_audio(self):
        with TemporaryDirectory() as directory:
            library, _audio = self.create_library(Path(directory))
            live = self.create_live_backend()
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                self.create_resolver(source_audio_status="available"),
                audio_source_policy="live-tts-only",
                audio_output=FakeAudioOutput(),
            )
            backend.set_live_mode_active(True)

            prepared = backend.prepare("Ada", "Hello.")

        self.assertEqual(prepared, "live-audio")
        self.assertEqual(backend.last_audio_source, "live:live-test")
        live.prepare.assert_called_once_with("Ada", "Hello.")

    def test_generated_policy_does_not_pass_through_game_audio(self):
        with TemporaryDirectory() as directory:
            library, _audio = self.create_library(Path(directory))
            live = self.create_live_backend()
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                self.create_resolver(source_audio_status="available"),
                audio_source_policy="prefer-generated",
                audio_output=FakeAudioOutput(),
            )
            backend.set_live_mode_active(True)

            prepared = backend.prepare("Ada", "Hello.")

        self.assertIsInstance(prepared, PreparedGeneratedAudio)
        self.assertEqual(backend.last_audio_source, "generated")
        live.prepare.assert_not_called()

    def test_invalid_audio_source_policy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown audio source policy"):
            GeneratedAudioFallbackBackend(
                self.create_live_backend(),
                None,
                self.create_resolver(),
                audio_source_policy="surprise-me",
                audio_output=FakeAudioOutput(),
            )

    def test_modified_audio_falls_back_and_warns_once(self):
        warnings = []
        with TemporaryDirectory() as directory:
            library, audio = self.create_library(Path(directory))
            library.warn = warnings.append
            audio.write_bytes(b"modified")
            live = self.create_live_backend()
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                self.create_resolver(),
                audio_output=FakeAudioOutput(),
            )

            self.assertEqual(backend.prepare("Ada", "Hello."), "live-audio")
            self.assertEqual(backend.prepare("Ada", "Hello."), "live-audio")

        self.assertEqual(len(warnings), 1)
        self.assertIn("missing or modified", warnings[0])

    def test_generated_playback_applies_volume_and_honors_guard(self):
        audio_output = FakeAudioOutput()
        backend = GeneratedAudioFallbackBackend(
            self.create_live_backend(),
            Mock(),
            Mock(),
            volume=0.5,
            audio_output=audio_output,
        )
        prepared = PreparedGeneratedAudio(
            "game:1",
            hashlib.sha256(b"Hello.").hexdigest(),
            np.array([0.0, 0.5, -0.5], dtype=np.float32),
            24_000,
        )

        self.assertFalse(backend.play(prepared, playback_guard=lambda: False))
        self.assertEqual(audio_output.plays, [])
        self.assertTrue(backend.play(prepared, playback_guard=lambda: True))

        played, sample_rate, options = audio_output.plays[0]
        np.testing.assert_allclose(played, [0.0, 0.25, -0.25])
        self.assertEqual(sample_rate, 24_000)
        self.assertEqual(options["latency"], "low")


if __name__ == "__main__":
    unittest.main()
