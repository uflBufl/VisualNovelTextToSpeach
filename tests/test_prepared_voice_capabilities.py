"""Prepared speech does not require a working optional live narrator."""

import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.audio_output_fixtures import FakeAudioOutput
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController, PreparedLiveChunkRoutes
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    LiveTTSRoute,
    PendingGeneratedAudioRoute,
)
from vntts.live import SpeechChunk
from vntts.playback import PlaybackOutcome, PlaybackStatus, PreparedPlayback
from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError
from vntts.settings import AppSettings
from vntts.speech_backend import SpeechBackendCapabilities
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class PreparedVoiceCapabilitiesTest(unittest.TestCase):
    def create_backend(self, *, prepared=True, original=False, omission=False):
        self.line = ChapterDialogue("line", "chapter", 1, "Ada", "Hello.", "digest")
        if original:
            self.line = replace(
                self.line,
                source_audio_status="available",
                source_audio_main_story=True,
            )
        resolver = ChapterVoicePreloader([self.line])
        library = SimpleNamespace(
            runtime_progress=False,
            find_with_preflight=Mock(
                return_value=(object() if prepared else None, "test")
            ),
            find_audio_event_omission=Mock(return_value=object() if omission else None),
            find_live_fallback=Mock(return_value=None),
        )
        registry = CharacterVoiceRegistry([CharacterVoice("Ada", "ada")])
        live = SimpleNamespace(
            name="pocket-tts",
            capabilities=SpeechBackendCapabilities(True, True, False),
            registry=registry,
            narrator_reference="alba",
            generation_profile="default",
            process=object(),
            prime=Mock(return_value=True),
            prepare_playback=Mock(),
        )
        backend = GeneratedAudioFallbackBackend(
            live, library, resolver, audio_output=FakeAudioOutput()
        )
        backend.live_mode_active = True
        return backend

    def test_prepared_probe_does_not_select_or_reserve_a_route(self):
        for options in ({}, {"original": True}, {"omission": True}):
            with self.subTest(options=options):
                backend = self.create_backend(**options)
                initial_match = backend.line_resolver.current_match
                with patch.object(backend, "prepare_route") as prepare_route:
                    self.assertFalse(
                        backend.needs_live_voice_for_observation("Ada", "Hello.")
                    )
                self.assertIs(backend.line_resolver.current_match, initial_match)
                prepare_route.assert_not_called()
                backend.live_backend.prepare_playback.assert_not_called()
                self.assertIsNone(
                    backend.generated_reservations.get(("line", "digest"))
                )

    def test_live_fallback_unknown_and_override_still_require_priming(self):
        backend = self.create_backend(prepared=False)
        self.assertTrue(backend.needs_live_voice_for_observation("Ada", "Hello."))
        self.assertTrue(backend.needs_live_voice_for_observation("Ada", "Unmatched."))
        backend.library.runtime_progress = True
        self.assertFalse(backend.needs_live_voice_for_observation("Ada", "Hello."))
        backend.library.find_live_fallback.return_value = SimpleNamespace(
            schema_version=1,
            provider="pocket-tts",
            model="pocket-tts",
            generation_profile="default",
        )
        self.assertTrue(backend.needs_live_voice_for_observation("Ada", "Hello."))
        backend = self.create_backend()
        backend.voice_override = lambda _character: True
        self.assertTrue(backend.needs_live_voice_for_observation("Ada", "Hello."))
        backend.voice_override = None
        backend.speed = 1.2
        self.assertTrue(backend.needs_live_voice_for_observation("Ada", "Hello."))
        backend.speed = 1.0
        backend.audio_source_policy = "live-tts-only"
        self.assertTrue(backend.needs_live_voice_for_observation("Ada", "Hello."))

    def test_ambiguous_equivalent_audio_skips_priming_without_reserving(self):
        backend = self.create_backend()
        backend.line_resolver = ChapterVoicePreloader(
            (self.line, replace(self.line, line_id="copy", chapter="other"))
        )
        backend.library.reserve = Mock()
        with patch.object(backend, "prepare_route") as prepare_route:
            self.assertFalse(backend.needs_live_voice_for_observation("Ada", "Hello."))
        backend.library.reserve.assert_not_called()
        prepare_route.assert_not_called()
        self.assertIsNone(backend.line_resolver.current_match)
        backend.library.find_with_preflight.return_value = (None, "missing")
        self.assertTrue(backend.needs_live_voice_for_observation("Ada", "Hello."))
        backend.library.find_with_preflight.return_value = (object(), "test")
        backend.voice_override = lambda _character: True
        self.assertTrue(backend.needs_live_voice_for_observation("Ada", "Hello."))

    def test_probe_never_temporarily_writes_the_live_cursor(self):
        class ReadOnlyCursor(ChapterVoicePreloader):
            def __setattr__(self, name, value):
                if name == "current_match" and name in self.__dict__:
                    raise AssertionError(
                        "Observation preflight changed the live cursor"
                    )
                super().__setattr__(name, value)

        backend = self.create_backend()
        backend.line_resolver = ReadOnlyCursor((self.line,))
        self.assertFalse(backend.needs_live_voice_for_observation("Ada", "Hello."))
        backend.line_resolver = ReadOnlyCursor(
            (self.line, replace(self.line, line_id="copy", chapter="other"))
        )
        self.assertFalse(backend.needs_live_voice_for_observation("Ada", "Hello."))

    def test_incompatible_declared_fallback_still_waits_for_prepared_audio(self):
        backend = self.create_backend(prepared=False)
        backend.library.runtime_progress = True
        backend.library.find_live_fallback.return_value = SimpleNamespace(
            schema_version=1,
            provider="other",
            model="other",
            generation_profile="default",
        )
        self.assertIsInstance(
            backend.prepare_route("Ada", "Hello."), PendingGeneratedAudioRoute
        )
        self.assertFalse(backend.needs_live_voice_for_observation("Ada", "Hello."))

    def test_prepared_observation_skips_both_prime_paths_but_fallback_primes(self):
        backend = self.create_backend()
        controller = AppController(AppSettings())
        controller.speech_backend = backend
        controller.voice_router = SimpleNamespace(
            registry=backend.live_backend.registry
        )
        controller.chapter_voice_preloader = Mock()
        with ThreadPoolExecutor(max_workers=1) as executor:
            controller.speech_executor = executor
            self.assertFalse(controller._prime_observed_voice("Ada", "Hello."))
            self.assertFalse(controller._prime_likely_chapter_voice("Ada", "Hello."))
            backend.live_backend.prime.assert_not_called()
            controller.chapter_voice_preloader.recommend.assert_not_called()
            backend.library.find_with_preflight.return_value = (None, "missing")
            self.assertTrue(controller._prime_observed_voice("Ada", "Hello."))
        backend.live_backend.prime.assert_called_once_with("Ada")

    def announcement_controller(self):
        backend = self.create_backend()
        self.errors = []
        self.statuses = []
        controller = AppController(
            AppSettings(speaker_announcement_mode="all-speakers"),
            error_handler=self.errors.append,
            status_handler=self.statuses.append,
        )
        controller.speech_backend = backend
        backend.live_backend.prepare_playback.return_value = PreparedPlayback(
            object(), None, None, None, "live:pocket-tts"
        )
        return controller

    def test_configuration_failure_preserves_dialogue_and_does_not_mark_spoken(self):
        logs = self.enterContext(self.assertLogs("vntts.controller", level="WARNING"))
        controller = self.announcement_controller()
        chunk = SpeechChunk(1, "Ada", "Hello.", ordinal=1, explicit_replay=True)
        dialogue = PreparedPlayback(object(), None, None, None, "generated")
        announcement, speaker = controller._prepare_speaker_announcement(
            chunk, dialogue
        )
        self.assertIsInstance(announcement, LiveTTSRoute)
        controller.live_reader = Mock()
        controller.live_reader.wait_until_playable.return_value = True
        failed = PlaybackOutcome(
            PlaybackStatus.FAILED,
            None,
            error="Accept the Pocket model terms and authenticate to enable cloning",
            error_type=TTSConfigurationError,
        )
        with (
            patch.object(
                controller, "_dispatch_speaker_announcement", return_value=failed
            ) as announce,
            patch.object(
                controller,
                "_dispatch_live_playback",
                return_value=PlaybackOutcome(PlaybackStatus.COMPLETED, 0.0),
            ) as play,
        ):
            routes = PreparedLiveChunkRoutes(dialogue, announcement, speaker)
            self.assertTrue(controller._play_live_chunk(chunk, routes))
            self.assertTrue(controller._play_live_chunk(chunk, routes))
        self.assertEqual(announce.call_count, 1)
        self.assertEqual(play.call_count, 2)
        self.assertIs(play.call_args.args[2], dialogue)
        self.assertIsNone(controller.last_visible_speaker_key)
        self.assertEqual(self.errors, [])
        self.assertEqual(len(logs.records), 1)
        self.assertIn("authenticate", logs.output[0])
        self.assertIn("dialogue will continue", logs.output[0])
        self.assertTrue(any("authenticate" in status for status in self.statuses))
        self.assertEqual(
            controller._prepare_speaker_announcement(chunk, dialogue), (None, None)
        )

    def test_preparation_capability_failure_is_retained_but_temporary_failure_retries(
        self,
    ):
        for failure in (
            TTSConfigurationError("Cloning unavailable"),
            TTSSynthesisError("Temporary failure"),
        ):
            with self.subTest(failure=type(failure).__name__):
                controller = self.announcement_controller()
                live = controller.speech_backend.live_backend
                controller.speech_backend.prepare_route = Mock(
                    return_value=PreparedPlayback(
                        object(), None, None, None, "generated"
                    )
                )
                live.prepare_playback.side_effect = failure
                chunk = SpeechChunk(1, "Ada", "Hello.", ordinal=1)
                with (
                    patch.object(
                        controller,
                        "_materialize_live_route",
                        side_effect=lambda route, _chunk: route,
                    ),
                    self.assertLogs("vntts.controller", level="WARNING") as logs,
                ):
                    controller._prepare_live_chunk(chunk)
                    controller._prepare_live_chunk(chunk)
                expected = 1 if isinstance(failure, TTSConfigurationError) else 2
                self.assertEqual(live.prepare_playback.call_count, expected)
                self.assertEqual(self.errors, [])
                self.assertEqual(len(logs.records), expected)
                self.assertTrue(all(str(failure) in item for item in logs.output))
                self.assertIsNone(controller.last_visible_speaker_key)

    def test_failed_speculative_prime_keeps_prepared_speech_and_required_live_error(
        self,
    ):
        controller = self.announcement_controller()
        controller.settings = AppSettings(speaker_announcement_mode="off")
        backend = controller.speech_backend
        controller.voice_router = SimpleNamespace(
            registry=backend.live_backend.registry
        )
        future = Future()
        controller.speech_executor = Mock()
        controller.speech_executor.submit.return_value = future
        self.assertTrue(controller._prime_observed_voice("Ada", "Unmatched prefix"))
        failure = TTSConfigurationError("Pocket cloning unavailable")
        with self.assertLogs("vntts.controller", level="WARNING") as logs:
            future.set_exception(failure)
        self.assertEqual(controller.voice_prime_futures, set())
        self.assertIn("Ada", logs.output[0])
        self.assertIs(logs.records[0].exc_info[1], failure)
        self.assertFalse(controller._prime_observed_voice("Ada", "Unmatched prefix"))
        controller.speech_executor.submit.assert_called_once()

        dialogue = PreparedPlayback(object(), None, None, None, "generated")
        backend.prepare_route = Mock(return_value=dialogue)
        controller.live_reader = Mock()
        controller.live_reader.wait_until_playable.return_value = True
        chunk = SpeechChunk(1, "Ada", "Hello.", ordinal=1)
        with (
            patch.object(
                controller,
                "_materialize_live_route",
                side_effect=lambda route, _chunk: route,
            ),
            patch.object(
                controller,
                "_dispatch_live_playback",
                return_value=PlaybackOutcome(PlaybackStatus.COMPLETED, 0.0),
            ) as play,
        ):
            self.assertIs(controller._prepare_live_chunk(chunk), dialogue)
            self.assertTrue(controller._play_live_chunk(chunk, dialogue))
        self.assertIs(play.call_args.args[2], dialogue)
        self.assertEqual(self.errors, [])
        controller.speech_backend = backend.live_backend
        backend.live_backend.prepare_playback.side_effect = failure
        with self.assertRaises(TTSConfigurationError) as caught:
            controller._prepare_live_chunk(chunk)
        self.assertIs(caught.exception, failure)

    def test_cancelled_or_successful_prime_cleans_up_without_warning(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                controller = self.announcement_controller()
                controller.voice_router = SimpleNamespace(
                    registry=controller.speech_backend.live_backend.registry
                )
                future = Future()
                controller.speech_executor = Mock()
                controller.speech_executor.submit.return_value = future
                self.assertTrue(controller._prime_observed_voice("Ada", "Unmatched"))
                with self.assertNoLogs("vntts.controller", level="WARNING"):
                    if cancel:
                        future.cancel()
                    else:
                        future.set_result(True)
                self.assertEqual(controller.voice_prime_futures, set())
                self.assertEqual(self.errors, [])

    def test_live_narrator_configuration_or_runtime_change_allows_retry(self):
        for change in (
            "narrator_reference",
            "process",
            "backend",
            "registry",
            "startup",
        ):
            with self.subTest(change=change):
                controller = self.announcement_controller()
                controller._report_speaker_announcement_error(
                    TTSConfigurationError("Locked")
                )
                live = controller.speech_backend.live_backend
                if change == "backend":
                    controller.speech_backend.live_backend = SimpleNamespace(
                        **vars(live)
                    )
                elif change == "registry":
                    live.registry = CharacterVoiceRegistry(
                        [CharacterVoice("Narrator", "new", reference=Path("voice.wav"))]
                    )
                elif change == "startup":
                    with patch.object(
                        type(controller.runtime_lifecycle), "prepare_startup"
                    ):
                        controller.prepare_startup()
                else:
                    setattr(
                        live, change, object() if change == "process" else "new-voice"
                    )
                route, speaker = controller._prepare_speaker_announcement(
                    SpeechChunk(1, "Ada", "Hello.", ordinal=1),
                    PreparedPlayback(object(), None, None, None, "generated"),
                )
                self.assertIsInstance(route, LiveTTSRoute)
                self.assertEqual(speaker, "Ada")


if __name__ == "__main__":
    unittest.main()
