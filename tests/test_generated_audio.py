import hashlib
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
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
    GeneratedAudioRoute,
    LiveTTSRoute,
    PlaybackStatus,
    PreparedGeneratedAudio,
    PreparedSourceAudioPassThrough,
    SourceAudioRoute,
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

    def test_route_wrapper_has_no_payload_only_or_mutable_metric_facade(self):
        backend = GeneratedAudioFallbackBackend(
            self.create_live_backend(),
            None,
            self.create_resolver(),
            audio_output=FakeAudioOutput(),
        )

        self.assertFalse(hasattr(backend, "prepare"))
        self.assertFalse(hasattr(backend, "play"))
        for attribute in (
            "last_route_trace",
            "last_audio_source",
            "last_synthesis_ms",
            "last_first_audio_ms",
            "last_playback_ms",
            "last_playback_underrun",
            "last_generation_limited",
            "last_cache_source",
        ):
            self.assertFalse(hasattr(backend, attribute), attribute)

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

            route = backend.prepare_route("ADA", "Hello.")

        self.assertIsInstance(route, GeneratedAudioRoute)
        self.assertEqual(route.prepared.line_id, "game:1")
        live.prepare.assert_not_called()
        self.assertEqual(route.synthesis_ms, 0.0)
        self.assertEqual(route.trace.effective_source, "generated")
        self.assertEqual(route.trace.match_result, "exact")
        self.assertEqual(route.trace.line_id, "game:1")
        self.assertEqual(
            route.trace.artifact_preflight_state,
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

    def test_early_prefix_requires_verified_audio_and_carries_reserved_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            library, audio = self.create_library(root)
            resolver = self.create_resolver()
            live = self.create_live_backend()
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                resolver,
                audio_output=FakeAudioOutput(),
            )

            self.assertTrue(backend.has_generated_line(resolver.dialogue[0]))
            audio.unlink()
            route = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, GeneratedAudioRoute)
        self.assertEqual(
            route.trace.artifact_preflight_state, "generated-audio-entry-reserved"
        )
        live.prepare.assert_not_called()

        with TemporaryDirectory() as directory:
            root = Path(directory)
            library, audio = self.create_library(root)
            resolver = self.create_resolver()
            backend = GeneratedAudioFallbackBackend(
                self.create_live_backend(),
                library,
                resolver,
                audio_output=FakeAudioOutput(),
            )
            audio.unlink()

            self.assertFalse(backend.has_generated_line(resolver.dialogue[0]))

    def test_generated_wav_swap_between_identity_lookup_and_read_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            library, audio = self.create_library(root)
            original_find = library.index.find

            def swap_after_lookup(line_id, text_hash, verify_file=False):
                entry = original_find(line_id, text_hash, verify_file=verify_file)
                if entry is not None:
                    write_wav(audio, [0.75, 0.75, 0.75, 0.75])
                return entry

            library.index.find = swap_after_lookup

            prepared, state = library.find_with_preflight(
                "game:1", text_sha256("Hello.")
            )

        self.assertIsNone(prepared)
        self.assertEqual(state, "generated-audio-checksum-failed")

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

            route = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, GeneratedAudioRoute)
        self.assertEqual(route.trace.effective_source, "generated")
        self.assertEqual(
            route.trace.fallback_reason,
            "source-audio-missing",
        )
        self.assertEqual(route.trace.match_result, "exact")

    def test_normalized_exact_remains_eligible_and_ambiguous_match_uses_live(self):
        with TemporaryDirectory() as directory:
            library, _audio = self.create_library(Path(directory))
            dialogue = self.create_resolver().dialogue[0]
            resolver = Mock()
            resolver.resolve_exact_with_result.side_effect = (
                (dialogue, "normalized-exact"),
                (None, "ambiguous"),
            )
            live = self.create_live_backend()
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                resolver,
                audio_output=FakeAudioOutput(),
            )

            normalized = backend.prepare_route("Ada", "Hello.")
            ambiguous = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(normalized, GeneratedAudioRoute)
        self.assertEqual(normalized.trace.match_result, "normalized-exact")
        self.assertIsInstance(ambiguous, LiveTTSRoute)
        self.assertEqual(ambiguous.trace.match_result, "ambiguous")

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

            first = backend.prepare_route("Ada", "Hello.")
            changed = backend.prepare_route("Ada", "Changed.")

        self.assertIsInstance(first, LiveTTSRoute)
        self.assertIsInstance(changed, LiveTTSRoute)
        self.assertEqual(first.prepared, "live-audio")
        self.assertEqual(changed.prepared, "live-audio")
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

            route = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, LiveTTSRoute)
        self.assertEqual(route.prepared, "live-audio")
        live.prepare.assert_called_once_with("Ada", "Hello.")
        self.assertEqual(
            route.trace.fallback_reason,
            "manual-voice-override",
        )
        self.assertEqual(route.trace.match_result, "skipped")

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

            route = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, SourceAudioRoute)
        self.assertEqual(route.prepared.source_audio_id, "voice-7")
        self.assertIsNone(route.prepared.completion_seconds)
        self.assertEqual(route.trace.effective_source, "game")
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

        route = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, LiveTTSRoute)
        self.assertEqual(route.prepared, "live-audio")
        self.assertEqual(route.trace.effective_source, "live:live-test")
        self.assertIn(
            "source-audio-completion-unavailable",
            route.trace.fallback_reason,
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

        route = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, SourceAudioRoute)
        outcome = backend.play_route(route)
        self.assertIs(outcome.status, PlaybackStatus.PASSTHROUGH_UNOBSERVED)
        self.assertIsNone(outcome.playback_ms)
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

        route = backend.prepare_route("Ada", "Hello.")

        self.assertEqual(route.prepared.completion_seconds, 0.001)
        self.assertEqual(route.prepared.completion_source, "story-index")
        outcome = backend.play_route(route)
        self.assertIs(outcome.status, PlaybackStatus.COMPLETED)
        self.assertIsNotNone(outcome.playback_ms)

    def test_one_time_read_does_not_silently_pass_through_game_audio(self):
        live = self.create_live_backend()
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(source_audio_status="available"),
            audio_output=FakeAudioOutput(),
        )

        route = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, LiveTTSRoute)
        self.assertEqual(route.prepared, "live-audio")
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

            route = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, LiveTTSRoute)
        self.assertEqual(route.prepared, "live-audio")
        self.assertEqual(route.trace.effective_source, "live:live-test")
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

            route = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, GeneratedAudioRoute)
        self.assertEqual(route.trace.effective_source, "generated")
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

            self.assertIsInstance(backend.prepare_route("Ada", "Hello."), LiveTTSRoute)
            self.assertIsInstance(backend.prepare_route("Ada", "Hello."), LiveTTSRoute)

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
        route = GeneratedAudioRoute(
            PreparedGeneratedAudio(
                "game:1",
                hashlib.sha256(b"Hello.").hexdigest(),
                np.array([0.0, 0.5, -0.5], dtype=np.float32),
                24_000,
            ),
            Mock(effective_source="generated"),
        )

        interrupted = backend.play_route(route, playback_guard=lambda: False)
        self.assertIs(interrupted.status, PlaybackStatus.INTERRUPTED)
        self.assertEqual(audio_output.plays, [])
        completed = backend.play_route(route, playback_guard=lambda: True)
        self.assertIs(completed.status, PlaybackStatus.COMPLETED)

        played, sample_rate, options = audio_output.plays[0]
        np.testing.assert_allclose(played, [0.0, 0.25, -0.25])
        self.assertEqual(sample_rate, 24_000)
        self.assertEqual(options["latency"], "low")

    def test_generated_guard_change_during_wait_returns_interrupted(self):
        playable = True

        class GuardChangingOutput(FakeAudioOutput):
            def wait(self):
                nonlocal playable
                playable = False
                return Mock(output_underflow=False)

        backend = GeneratedAudioFallbackBackend(
            self.create_live_backend(),
            Mock(),
            Mock(),
            audio_output=GuardChangingOutput(),
        )
        route = GeneratedAudioRoute(
            PreparedGeneratedAudio(
                "game:1",
                hashlib.sha256(b"Hello.").hexdigest(),
                np.array([0.0, 0.25], dtype=np.float32),
                24_000,
            ),
            Mock(),
        )

        outcome = backend.play_route(route, playback_guard=lambda: playable)

        self.assertIs(outcome.status, PlaybackStatus.INTERRUPTED)
        self.assertEqual(outcome.first_audio_ms, 0.0)

    def test_preplay_guard_interruption_has_no_first_audio_or_device_output(self):
        output = FakeAudioOutput()
        backend = GeneratedAudioFallbackBackend(
            self.create_live_backend(),
            Mock(),
            Mock(),
            audio_output=output,
        )
        route = GeneratedAudioRoute(
            PreparedGeneratedAudio(
                "game:1",
                hashlib.sha256(b"Hello.").hexdigest(),
                np.array([0.0], dtype=np.float32),
                24_000,
            ),
            Mock(),
        )

        outcome = backend.play_route(route, playback_guard=lambda: False)

        self.assertIs(outcome.status, PlaybackStatus.INTERRUPTED)
        self.assertIsNone(outcome.first_audio_ms)
        self.assertEqual(output.plays, [])

    def test_generated_and_live_player_errors_return_failed_outcomes(self):
        class FailingOutput(FakeAudioOutput):
            def wait(self):
                raise RuntimeError("device failed")

        backend = GeneratedAudioFallbackBackend(
            self.create_live_backend(),
            Mock(),
            Mock(),
            audio_output=FailingOutput(),
        )
        generated = GeneratedAudioRoute(
            PreparedGeneratedAudio(
                "game:1",
                hashlib.sha256(b"Hello.").hexdigest(),
                np.array([0.0], dtype=np.float32),
                24_000,
            ),
            Mock(),
        )

        generated_outcome = backend.play_route(generated)

        live = self.create_live_backend()
        live.play.side_effect = RuntimeError("live failed")
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(),
            audio_output=FakeAudioOutput(),
        )
        live_route = backend.prepare_route("Ada", "Changed.")
        live_outcome = backend.play_route(live_route)

        self.assertIs(generated_outcome.status, PlaybackStatus.FAILED)
        self.assertIn("device failed", generated_outcome.error)
        self.assertIs(live_outcome.status, PlaybackStatus.FAILED)
        self.assertIn("live failed", live_outcome.error)

    def test_source_stop_cancels_wait_without_stopping_owned_device(self):
        output = FakeAudioOutput()
        backend = GeneratedAudioFallbackBackend(
            self.create_live_backend(),
            None,
            self.create_resolver(),
            audio_output=output,
        )
        route = SourceAudioRoute(
            PreparedSourceAudioPassThrough(
                "game:1",
                text_sha256("Hello."),
                completion_seconds=5.0,
            ),
            Mock(),
        )
        completed = Event()
        outcomes = []

        def play():
            outcomes.append(backend.play_route(route))
            completed.set()

        thread = Thread(target=play)
        thread.start()
        self.assertTrue(backend.source_audio_completion_stop.wait(0.01) is False)
        while not backend.playback_active:
            self.assertFalse(completed.wait(0.01))
        self.assertTrue(backend.stop())
        thread.join(1)

        self.assertTrue(completed.is_set())
        self.assertIs(outcomes[0].status, PlaybackStatus.INTERRUPTED)
        self.assertFalse(output.stopped)

    def test_live_route_keeps_prepare_metrics_and_propagates_limited_result(self):
        live = self.create_live_backend()
        first_audio = iter((10.0, 999.0))

        def prepare(_character, text):
            live.last_first_audio_ms = next(first_audio)
            live.last_synthesis_ms = 5.0 if text == "First." else 500.0
            return text

        live.prepare.side_effect = prepare
        live.last_playback_ms = 20.0
        live.last_playback_underrun = False
        live.last_generation_limited = True
        live.last_cache_source = "fresh-generation"
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(),
            audio_output=FakeAudioOutput(),
        )

        first = backend.prepare_route("Ada", "First.")
        second = backend.prepare_route("Ada", "Second.")
        outcome = backend.play_route(first)

        self.assertIsInstance(first, LiveTTSRoute)
        self.assertIsInstance(second, LiveTTSRoute)
        self.assertEqual(first.first_audio_ms, 10.0)
        self.assertEqual(first.synthesis_ms, 5.0)
        self.assertEqual(outcome.first_audio_ms, 10.0)
        self.assertEqual(outcome.synthesis_ms, 5.0)
        self.assertEqual(outcome.cache_source, "fresh-generation")
        self.assertEqual(outcome.audio_source, "live:live-test")
        self.assertTrue(outcome.generation_limited)


if __name__ == "__main__":
    unittest.main()
