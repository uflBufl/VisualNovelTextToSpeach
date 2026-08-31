import hashlib
import json
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
    LiveFallbackRoute,
    LiveTTSRoute,
    PlaybackStatus,
    PreparedGeneratedAudio,
    PreparedSourceAudioPassThrough,
    SourceAudioRoute,
)
from vntts.playback import PreparedPlayback, outcome_for_prepared
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


class ExplicitStreamAudioOutput(FakeAudioOutput):
    def __init__(self, sample_rate=48_000):
        super().__init__()
        self.sample_rate = sample_rate
        self.streams = []

    def query_devices(self, _device=None, _kind=None, **_options):
        return {"default_samplerate": self.sample_rate}

    def OutputStream(self, **options):
        owner = self

        class Stream:
            def __init__(self):
                self.options = options
                self.samples = None
                self.aborted = False

            def __enter__(self):
                owner.streams.append(self)
                return self

            def __exit__(self, _error_type, _error, _traceback):
                return False

            def write(self, samples):
                self.samples = np.asarray(samples)
                return False

            def abort(self):
                self.aborted = True

        return Stream()


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

    def test_optional_library_rejects_symlink_escape_from_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_root = root / "manifest"
            manifest_root.mkdir()
            external = root / "outside.wav"
            write_wav(external, [0.0, 0.25, -0.25, 0.0])
            (manifest_root / "linked.wav").symlink_to(external)
            manifest = manifest_root / "generated-audio.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema": "vntts.generated-audio",
                        "schema_version": 1,
                        "entry_count": 1,
                        "entries": [
                            {
                                "line_id": "game:1",
                                "text_sha256": text_sha256("Hello."),
                                "audio": "linked.wav",
                                "audio_format": "wav-pcm16-mono",
                                "audio_sha256": sha256_file(external),
                                "sample_rate": 24_000,
                                "sample_count": 4,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            warnings = []

            library = GeneratedAudioLibrary.load_optional(
                manifest, warn=warnings.append
            )

        self.assertIsNone(library)
        self.assertEqual(len(warnings), 1)
        self.assertIn("manifest directory", warnings[0])

    def test_lossless_manifest_exposes_bound_narrator_fallback_role(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
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
                        "text_sha256": text_sha256("Hello."),
                        "audio": "audio/line.wav",
                        "audio_format": "wav-pcm16-mono",
                        "audio_sha256": sha256_file(audio),
                        "sample_rate": 24_000,
                        "sample_count": 4,
                        "speaker": "Poacher I",
                        "requested_voice_character": "Poacher I",
                        "voice_character": "Narrator",
                        "synthesis_fallback": {
                            "schema_version": 1,
                            "kind": "missing_voice_to_narrator",
                            "policy": {
                                "schema_version": 1,
                                "mode": "narrator_roles",
                                "roles": ["Poacher I"],
                            },
                            "source_voice_character": "Poacher I",
                            "synthesis_voice_character": "Narrator",
                            "narrator_character": "Centurion",
                        },
                    }
                ],
            )

            library = GeneratedAudioLibrary.load_optional(manifest)
            prepared = library.find("game:1", text_sha256("Hello."))

        self.assertEqual(prepared.narrator_fallback_role, "Poacher I")

    def test_lossless_manifest_maps_unattributed_narrator_to_unknown(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "line.wav"
            write_wav(audio, [0.0, 0.25, -0.25, 0.0])
            manifest = root / "generated-audio.json"
            write_generated_audio_manifest(
                manifest,
                {},
                [
                    {
                        "line_id": "game:unknown",
                        "text_sha256": text_sha256("Who is there?"),
                        "audio": "line.wav",
                        "audio_format": "wav-pcm16-mono",
                        "audio_sha256": sha256_file(audio),
                        "sample_rate": 24_000,
                        "sample_count": 4,
                        "speaker": "???",
                        "requested_voice_character": "Narrator",
                        "voice_character": "Narrator",
                    }
                ],
            )

            prepared = GeneratedAudioLibrary.load_optional(manifest).find(
                "game:unknown", text_sha256("Who is there?")
            )

        self.assertEqual(prepared.narrator_fallback_role, "Unknown")

    def create_resolver(
        self,
        *,
        speaker="Ada",
        text="Hello.",
        source_audio_status="unknown",
        source_audio_id=None,
        source_audio_duration_seconds=None,
        source_audio_completeness="full",
    ):
        return ChapterVoicePreloader(
            [
                ChapterDialogue(
                    "game:1",
                    "1",
                    1,
                    speaker,
                    text,
                    text_sha256(text),
                    source_audio_status,
                    source_audio_id,
                    source_audio_duration_seconds,
                    source_audio_completeness,
                )
            ]
        )

    def create_live_backend(self):
        backend = Mock()
        backend.name = "live-test"
        backend.capabilities = SpeechBackendCapabilities(True, False, True)
        backend.prepare.return_value = "live-audio"
        backend.play.return_value = True
        backend.prepare_playback.return_value = PreparedPlayback(
            "live-audio", None, None, "fresh-generation", "live:live-test"
        )
        backend.play_prepared.side_effect = lambda prepared, **_kwargs: (
            outcome_for_prepared(
                prepared,
                PlaybackStatus.COMPLETED,
                0.0,
                first_audio_ms=prepared.first_audio_ms,
            )
        )
        backend.stop.return_value = False
        return backend

    def create_live_fallback_library(self, root, *, model="pocket-tts"):
        text = "Hello."
        manifest = root / "generated-audio.json"
        decision = {
            "schema": "vntts.authoring-live-fallback-decision",
            "schema_version": 1,
            "reason": "generated_audio_rejected",
            "provider": "pocket-tts",
            "model": model,
            "generation_profile": "default",
            "queue_id": "queue:1",
            "line_id": "game:1",
            "text_sha256": text_sha256(text),
            "speaker": "Ada",
            "requested_voice_character": "Narrator",
            "previous_result_sha256": "b" * 64,
            "decided_at": "2026-08-18T12:00:00+00:00",
        }
        decision["decision_sha256"] = hashlib.sha256(
            json.dumps(
                decision,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        write_generated_audio_manifest(
            manifest,
            {
                "vntts.authoring.live_fallback": {
                    "schema_version": 1,
                    "mode": "explicit",
                    "entries": [decision],
                }
            },
            [],
        )
        return GeneratedAudioLibrary.load_optional(manifest)

    def test_explicit_live_fallback_uses_only_bound_pocket_backend(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            library = self.create_live_fallback_library(root)
            live = self.create_live_backend()
            live.name = "pocket-tts"
            live.model_identity = None
            live.model_name = "pocket-tts"
            live.generation_profile = "default"
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                self.create_resolver(),
                audio_output=FakeAudioOutput(),
            )

            route = backend.prepare_route("Ada", "Hello.")
            outcome = backend.play_route(route)
            live.model_name = "different-model"
            with self.assertRaisesRegex(ValueError, "authorized fallback"):
                backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, LiveFallbackRoute)
        self.assertEqual(route.decision.reason, "generated_audio_rejected")
        self.assertEqual(route.trace.effective_source, "live-fallback")
        self.assertEqual(
            route.trace.artifact_preflight_state, "live-fallback-authorized"
        )
        self.assertTrue(outcome.successful)
        self.assertEqual(outcome.audio_source, "live-fallback")
        live.prepare_playback.assert_called_with("Narrator", "Hello.")

    def test_malformed_live_fallback_disables_optional_generated_library(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "generated-audio.json"
            write_generated_audio_manifest(
                manifest,
                {
                    "vntts.authoring.live_fallback": {
                        "schema_version": 1,
                        "mode": "implicit",
                        "entries": [],
                    }
                },
                [],
            )
            warnings = []
            library = GeneratedAudioLibrary.load_optional(
                manifest, warn=warnings.append
            )

        self.assertIsNone(library)
        self.assertEqual(len(warnings), 1)
        self.assertIn("live fallback ledger is malformed", warnings[0])

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
        live.prepare_playback.assert_not_called()
        self.assertEqual(route.synthesis_ms, 0.0)
        self.assertEqual(route.trace.effective_source, "generated")
        self.assertEqual(route.trace.match_result, "exact")
        self.assertEqual(route.trace.line_id, "game:1")
        self.assertEqual(
            route.trace.artifact_preflight_state,
            "generated-audio-entry-verified",
        )

    def test_verified_generation_resolves_live_voice_preflight(self):
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

            resolved = backend.has_resolved_route_in_live_mode("Ada", "Hello.")

        self.assertTrue(resolved)
        self.assertIsNone(resolver.current_match)

    def test_explicit_live_fallback_resolves_live_voice_preflight(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            library = self.create_live_fallback_library(root)
            live = self.create_live_backend()
            live.name = "pocket-tts"
            live.model_identity = None
            live.model_name = "pocket-tts"
            live.generation_profile = "default"
            resolver = self.create_resolver()
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                resolver,
                audio_output=FakeAudioOutput(),
            )

            resolved = backend.has_resolved_route_in_live_mode("Ada", "Hello.")

        self.assertTrue(resolved)
        self.assertIsNone(resolver.current_match)

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

    def test_early_generated_reservation_never_bypasses_original_audio_or_override(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            library, _audio = self.create_library(root)
            source_resolver = self.create_resolver(source_audio_status="available")
            source_backend = GeneratedAudioFallbackBackend(
                self.create_live_backend(),
                library,
                source_resolver,
                audio_source_policy="prefer-game-audio",
                audio_output=FakeAudioOutput(),
            )
            overridden_backend = GeneratedAudioFallbackBackend(
                self.create_live_backend(),
                library,
                self.create_resolver(),
                audio_source_policy="prefer-generated",
                audio_output=FakeAudioOutput(),
            )
            overridden_backend.voice_override = lambda _speaker: True

            self.assertFalse(
                source_backend.reserve_generated_line_for_early_playback(
                    source_resolver.dialogue[0]
                )
            )
            self.assertFalse(
                overridden_backend.reserve_generated_line_for_early_playback(
                    overridden_backend.line_resolver.dialogue[0]
                )
            )

    def test_early_generated_reservation_accepts_exact_generated_route(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            library, _audio = self.create_library(root)
            resolver = self.create_resolver(source_audio_status="absent")
            backend = GeneratedAudioFallbackBackend(
                self.create_live_backend(),
                library,
                resolver,
                audio_source_policy="prefer-game-audio",
                audio_output=FakeAudioOutput(),
            )

            self.assertTrue(
                backend.reserve_generated_line_for_early_playback(resolver.dialogue[0])
            )

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
        live.prepare_playback.assert_not_called()

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
        self.assertEqual(first.prepared.payload, "live-audio")
        self.assertEqual(changed.prepared.payload, "live-audio")
        self.assertEqual(live.prepare_playback.call_count, 2)

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
        self.assertEqual(route.prepared.payload, "live-audio")
        live.prepare_playback.assert_called_once_with("Ada", "Hello.")
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
        live.prepare_playback.assert_not_called()

    def test_unknown_label_preserves_available_game_audio_before_narrator(self):
        live = self.create_live_backend()
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(
                speaker="???",
                source_audio_status="available",
                source_audio_id="voice-unknown",
            ),
            audio_source_policy="prefer-game-audio",
            audio_output=FakeAudioOutput(),
        )
        backend.set_live_mode_active(True)

        route = backend.prepare_route("???", "Hello.")

        self.assertIsInstance(route, SourceAudioRoute)
        self.assertEqual(route.prepared.source_audio_id, "voice-unknown")
        live.prepare_playback.assert_not_called()

    def test_unknown_label_uses_narrator_only_for_live_tts(self):
        live = self.create_live_backend()
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(speaker="???"),
            audio_source_policy="live-tts-only",
            audio_output=FakeAudioOutput(),
        )

        route = backend.prepare_route("???", "Hello.")

        self.assertIsInstance(route, LiveTTSRoute)
        live.prepare_playback.assert_called_once_with("Narrator", "Hello.")

    def test_unknown_label_preserves_verified_generated_audio(self):
        with TemporaryDirectory() as directory:
            library, _audio = self.create_library(Path(directory))
            live = self.create_live_backend()
            backend = GeneratedAudioFallbackBackend(
                live,
                library,
                self.create_resolver(speaker="???"),
                audio_source_policy="prefer-generated",
                audio_output=FakeAudioOutput(),
            )

            route = backend.prepare_route("???", "Hello.")

        self.assertIsInstance(route, GeneratedAudioRoute)
        live.prepare_playback.assert_not_called()

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
        self.assertEqual(route.prepared.payload, "live-audio")
        self.assertEqual(route.trace.effective_source, "live:live-test")
        self.assertIn(
            "source-audio-completion-unavailable",
            route.trace.fallback_reason,
        )
        self.assertFalse(backend.will_use_source_audio("Ada", "Hello."))

    def test_available_game_audio_is_known_before_unknown_voice_prompting(self):
        live = self.create_live_backend()
        resolver = self.create_resolver(source_audio_status="available")
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            resolver,
            audio_source_policy="prefer-game-audio",
            audio_output=FakeAudioOutput(),
        )

        self.assertFalse(backend.will_use_source_audio("Ada", "Hello."))
        self.assertTrue(backend.will_use_source_audio_in_live_mode("Ada", "Hello."))
        self.assertIsNone(resolver.current_match)
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
        live.prepare_playback.assert_not_called()

    def test_explicit_line_id_routes_repeated_text_without_resolution_ambiguity(self):
        repeated = "The same words appear twice."
        resolver = ChapterVoicePreloader(
            [
                ChapterDialogue(
                    f"game:{sequence}",
                    "1",
                    sequence,
                    "Ada",
                    repeated,
                    text_sha256(repeated),
                    "available",
                    f"voice-{sequence}",
                    0.1,
                )
                for sequence in (1, 2)
            ]
        )
        backend = GeneratedAudioFallbackBackend(
            self.create_live_backend(),
            None,
            resolver,
            audio_source_policy="prefer-game-audio",
            audio_output=FakeAudioOutput(),
        )
        backend.set_live_mode_active(True)

        route = backend.prepare_route(
            "Ada",
            repeated,
            line_id="game:2",
        )

        self.assertIsInstance(route, SourceAudioRoute)
        self.assertEqual(route.prepared.line_id, "game:2")
        self.assertEqual(route.trace.line_id, "game:2")

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

        self.assertEqual(route.prepared.completion_seconds, 0.351)
        self.assertEqual(
            route.prepared.completion_source,
            "story-index+conservative-postroll",
        )
        outcome = backend.play_route(route)
        self.assertIs(outcome.status, PlaybackStatus.COMPLETED)
        self.assertIsNotNone(outcome.playback_ms)

    def test_partial_source_cue_waits_then_routes_the_full_line_to_tts(self):
        live = self.create_live_backend()
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(
                source_audio_status="available",
                source_audio_duration_seconds=1.25,
                source_audio_completeness="partial",
            ),
            audio_source_policy="prefer-game-audio",
            audio_output=FakeAudioOutput(),
        )
        backend.set_live_mode_active(True)

        route = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, LiveTTSRoute)
        self.assertEqual(route.source_audio_lead_seconds, 1.6)
        self.assertIn("source-audio-partial-cue", route.trace.fallback_reason)
        self.assertFalse(backend.will_use_source_audio("Ada", "Hello."))

    def test_partial_source_cue_is_included_in_first_audio_latency(self):
        live = self.create_live_backend()
        clock = Mock(side_effect=[10.0, 11.6])
        backend = GeneratedAudioFallbackBackend(
            live,
            None,
            self.create_resolver(
                source_audio_status="available",
                source_audio_duration_seconds=1.25,
                source_audio_completeness="partial",
            ),
            audio_source_policy="prefer-game-audio",
            audio_output=FakeAudioOutput(),
            clock=clock,
        )
        backend.set_live_mode_active(True)
        route = backend.prepare_route("Ada", "Hello.")
        live.play_prepared.side_effect = None
        live.play_prepared.return_value = outcome_for_prepared(
            route.prepared,
            PlaybackStatus.COMPLETED,
            250.0,
            first_audio_ms=25.0,
        )
        backend._wait_for_source_audio_lead = Mock(return_value=True)

        outcome = backend.play_route(route)

        self.assertAlmostEqual(outcome.playback_ms, 1850.0)
        self.assertAlmostEqual(outcome.first_audio_ms, 1625.0)

    def test_unknown_source_completeness_stays_manual_despite_measured_duration(self):
        backend = GeneratedAudioFallbackBackend(
            self.create_live_backend(),
            None,
            self.create_resolver(
                source_audio_status="available",
                source_audio_duration_seconds=1.25,
                source_audio_completeness="unknown",
            ),
            audio_source_policy="prefer-game-audio",
            audio_output=FakeAudioOutput(),
        )
        backend.set_live_mode_active(True)

        route = backend.prepare_route("Ada", "Hello.")

        self.assertIsInstance(route, SourceAudioRoute)
        self.assertIsNone(route.prepared.completion_seconds)

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
        self.assertEqual(route.prepared.payload, "live-audio")
        live.prepare_playback.assert_called_once_with("Ada", "Hello.")

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
        self.assertEqual(route.prepared.payload, "live-audio")
        self.assertEqual(route.trace.effective_source, "live:live-test")
        live.prepare_playback.assert_called_once_with("Ada", "Hello.")

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
        live.prepare_playback.assert_not_called()

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

    def test_generated_playback_uses_explicit_stream_across_mixed_rates(self):
        audio_output = ExplicitStreamAudioOutput(sample_rate=48_000)
        backend = GeneratedAudioFallbackBackend(
            self.create_live_backend(),
            Mock(),
            Mock(),
            audio_output=audio_output,
        )

        outcomes = []
        for sample_rate in (24_000, 48_000, 24_000):
            route = GeneratedAudioRoute(
                PreparedGeneratedAudio(
                    f"game:{sample_rate}",
                    hashlib.sha256(str(sample_rate).encode()).hexdigest(),
                    np.zeros(sample_rate // 100, dtype=np.float32),
                    sample_rate,
                ),
                Mock(effective_source="generated"),
            )
            outcomes.append(backend.play_route(route))

        self.assertEqual(audio_output.plays, [])
        self.assertEqual(len(audio_output.streams), 3)
        for stream in audio_output.streams:
            self.assertEqual(stream.options["samplerate"], 48_000)
            self.assertEqual(stream.options["channels"], 1)
            self.assertEqual(stream.options["dtype"], "float32")
            self.assertEqual(stream.samples.shape, (480, 1))
        self.assertEqual(
            [outcome.source_sample_rate for outcome in outcomes],
            [24_000, 48_000, 24_000],
        )
        self.assertEqual(
            [outcome.playback_sample_rate for outcome in outcomes],
            [48_000, 48_000, 48_000],
        )
        self.assertEqual(
            [outcome.expected_playback_ms for outcome in outcomes],
            [10.0, 10.0, 10.0],
        )

    def test_stop_aborts_active_explicit_generated_stream(self):
        entered = Event()
        released = Event()
        audio_output = ExplicitStreamAudioOutput()
        original_factory = audio_output.OutputStream

        def blocking_stream(**options):
            stream = original_factory(**options)

            def write(samples):
                stream.samples = np.asarray(samples)
                entered.set()
                released.wait(1)
                return False

            def abort():
                stream.aborted = True
                released.set()

            stream.write = write
            stream.abort = abort
            return stream

        audio_output.OutputStream = blocking_stream
        backend = GeneratedAudioFallbackBackend(
            self.create_live_backend(), Mock(), Mock(), audio_output=audio_output
        )
        route = GeneratedAudioRoute(
            PreparedGeneratedAudio(
                "game:1",
                hashlib.sha256(b"Hello.").hexdigest(),
                np.zeros(480, dtype=np.float32),
                48_000,
            ),
            Mock(effective_source="generated"),
        )
        outcomes = []
        worker = Thread(target=lambda: outcomes.append(backend.play_route(route)))

        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertTrue(backend.stop())
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(audio_output.streams[0].aborted)
        self.assertIs(outcomes[0].status, PlaybackStatus.INTERRUPTED)

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
        live.play_prepared.side_effect = RuntimeError("live failed")
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
            first_ms = next(first_audio)
            synthesis_ms = 5.0 if text == "First." else 500.0
            return PreparedPlayback(
                text,
                synthesis_ms,
                first_ms,
                "fresh-generation",
                "live:live-test",
            )

        live.prepare_playback.side_effect = prepare
        live.play_prepared.side_effect = lambda prepared, **_kwargs: (
            outcome_for_prepared(
                prepared,
                PlaybackStatus.COMPLETED,
                20.0,
                generation_limited=True,
                first_audio_ms=prepared.first_audio_ms,
            )
        )
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
