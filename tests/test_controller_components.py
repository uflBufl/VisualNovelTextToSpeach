import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from unittest.mock import ANY, Mock, patch

from vntts.controller import AppController
from vntts.controller_components import (
    LiveSessionComponent,
    RuntimeLifecycleComponent,
)
from vntts.generated_audio import GeneratedAudioFallbackBackend
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.playback import PlaybackOutcome, PlaybackStatus, PreparedPlayback
from vntts.playback_observer import observe_playback_start, playback_started_context
from vntts.settings import AppSettings
from vntts.speech_backend import XTTSVoiceRouterBackend
from vntts.voices import CharacterVoice, CharacterVoiceRegistry, CharacterVoiceRouter


class ControllerComponentsTest(unittest.TestCase):
    def test_history_replay_carries_actual_start_observer_into_speech_executor(self):
        controller = AppController(AppSettings())
        controller.voice_router = Mock()
        controller.live_reader = Mock(is_running=False)
        observed = Mock()
        controller._preview_voice = lambda *_args: playback_started_context.get()()
        with ThreadPoolExecutor(max_workers=1) as executor:
            controller.speech_executor = executor
            with observe_playback_start(observed):
                controller.replay_dialog("Marcus", "Hello.").result()
        observed.assert_called_once_with()

    def test_live_dispatch_requires_typed_outcome_from_dynamic_backend(self):
        class Backend:
            def __init__(self, outcome):
                self.outcome = outcome

            def play_prepared(self, _prepared, *, playback_guard):
                self.guard = playback_guard
                return self.outcome

        controller = AppController(AppSettings())
        reader, chunk = Mock(), Mock()
        prepared = PreparedPlayback(object(), None, None, None, "test")
        for outcome in (None, True, "completed", object()):
            with self.subTest(outcome=outcome):
                controller.speech_backend = Backend(outcome)
                with self.assertRaisesRegex(TypeError, "typed playback"):
                    controller._dispatch_live_playback(reader, chunk, prepared)
        for status in PlaybackStatus:
            with self.subTest(status=status):
                outcome = PlaybackOutcome(status, 10.0)
                backend = Backend(outcome)
                controller.speech_backend = backend
                self.assertIs(
                    controller._dispatch_live_playback(reader, chunk, prepared), outcome
                )
                backend.guard()
                reader.wait_until_playable.assert_called_with(chunk)

    def test_interruption_uses_the_playback_backend_before_the_synthesis_engine(self):
        controller = AppController(AppSettings())
        controller.tts = Mock()
        controller.speech_backend = Mock()
        controller.speech_backend.stop.return_value = True

        self.assertTrue(controller._interrupt_speech())
        controller.speech_backend.stop.assert_called_once_with()
        controller.tts.stop.assert_not_called()
        controller.speech_backend = object()
        controller.tts.stop.return_value = False
        self.assertFalse(controller._interrupt_speech())
        controller.tts.stop.assert_called_once_with()

    def test_read_once_does_not_resume_emergency_during_live_shutdown(self):
        controller = Mock()
        controller.live_reader.is_running = True

        self.assertFalse(LiveSessionComponent(controller).read_once())

        controller.live_reader.resume_after_emergency.assert_not_called()
        controller.schedule_dialog_read.assert_not_called()

    def test_emergency_stop_cancels_pending_one_time_read(self):
        controller = Mock(story_cursor_lock=RLock())
        controller.schedule_dialog_read.cancel.return_value = True
        controller.live_reader.emergency_stop.return_value = False

        self.assertTrue(LiveSessionComponent(controller).emergency_stop())

        controller.schedule_dialog_read.cancel.assert_called_once_with()
        controller.live_reader.emergency_stop.assert_called_once_with()

    def test_stop_live_never_starts_a_reader_that_finished_before_the_worker(self):
        controller = AppController(AppSettings())
        reader = Mock(is_running=False)
        reader.stop.return_value = False
        controller.live_reader = reader
        controller.narrator_fallback_speakers.add("A")
        controller.narrator_fallback_names["A"] = "A"
        controller.allow_unscoped_live_reading = True
        controller._set_backend_live_mode = Mock()

        self.assertFalse(controller.stop_live())

        reader.stop.assert_called_once_with()
        reader.toggle.assert_not_called()
        reader.start.assert_not_called()
        self.assertFalse(controller.narrator_fallback_speakers)
        self.assertFalse(controller.narrator_fallback_names)
        self.assertFalse(controller.allow_unscoped_live_reading)
        controller._set_backend_live_mode.assert_called_once_with(False)

    def test_settings_change_cancels_pending_one_time_read(self):
        controller = Mock(is_live_running=False)

        self.assertFalse(
            RuntimeLifecycleComponent(controller)._stop_live_for_settings()
        )

        controller.schedule_dialog_read.cancel.assert_called_once_with()

    def test_auto_advance_enable_fails_closed_without_capture_authority(self):
        for capture_mode, sequence_mode, expected_reason in (
            ("screen", "off", "selected game window"),
            ("window", "audio-manual", "never sends advance keys"),
        ):
            with self.subTest(capture_mode=capture_mode, sequence_mode=sequence_mode):
                controller = Mock(story_cursor_lock=RLock())
                controller.settings = AppSettings(
                    capture_mode=capture_mode,
                    live_sequence_mode=sequence_mode,
                    auto_advance_enabled=False,
                )
                controller.speech_backend = None
                controller.live_reader = Mock()
                component = LiveSessionComponent(controller)

                self.assertFalse(component.set_auto_advance_enabled(True))

                self.assertFalse(controller.settings.auto_advance_enabled)
                controller.live_reader.set_auto_advance.assert_called_once_with(
                    controller._live_auto_advance_callback.return_value
                )
                self.assertIn(
                    expected_reason,
                    controller.status_handler.call_args.args[0],
                )


if __name__ == "__main__":
    unittest.main()


class RuntimeLifecycleTest(unittest.TestCase):
    def test_startup_and_settings_refresh_wire_current_dialog_read_inputs(self):
        backend = Mock()
        reader = Mock(is_running=False)
        scheduler_factory = Mock()
        with (
            patch(
                "vntts.controller.initialize_voice_registry",
                return_value=CharacterVoiceRegistry(),
            ),
            patch("vntts.controller.ThreadPoolExecutor", return_value=Mock()),
            patch("vntts.controller.LiveDialogReader", return_value=reader),
            patch("vntts.controller.create_dialog_read_scheduler", scheduler_factory),
        ):
            controller = AppController(
                AppSettings(speech_backend="pocket-tts", warm_up_voices=False),
                pocket_backend_factory=Mock(return_value=backend),
                model_asset_manager_factory=Mock(),
            )
            self.addCleanup(controller.shutdown)
            self.assertTrue(controller.start())
            initial_capture_target = controller.capture_target
            self.assertEqual(scheduler_factory.call_count, 1)
            self.assertTrue(
                controller.apply_settings(
                    controller.settings.updated(
                        screenshot_directory="new-screenshots",
                        ocr_language="rus",
                        ocr_minimum_confidence=80,
                    )
                )
            )

        self.assertEqual(scheduler_factory.call_count, 2)
        self.assertIsNot(initial_capture_target, controller.capture_target)
        initial, refreshed = scheduler_factory.call_args_list
        for call in (initial, refreshed):
            self.assertIs(
                call.kwargs["correction_dictionary_provider"](),
                controller.correction_dictionary,
            )
        self.assertEqual(initial.args[:2], refreshed.args[:2])
        self.assertEqual(refreshed.args[2], Path("new-screenshots"))
        expected = {
            "live_reader": reader,
            "error_handler": controller.error_handler,
            "capture_target": controller.capture_target,
            "speech_handler": controller._enqueue_dialog,
            "shutdown_requested": controller.shutdown_requested,
            "minimum_confidence": 80,
            "uncertain_frame_recorder": controller.uncertain_frame_recorder,
            "diagnostic_handler": controller._publish_diagnostic,
            "voice_resolver": controller._resolve_voice_label,
            "ocr_language": "rus",
            "correction_dictionary_provider": ANY,
            "region_provider": controller._capture_region,
            "ocr_regions_provider": controller._capture_ocr_regions,
            "result_resolver": controller._resolve_story_ocr_result,
        }
        self.assertEqual(refreshed.kwargs, expected)
        self.assertEqual(
            initial.kwargs,
            {
                **expected,
                "capture_target": initial_capture_target,
                "minimum_confidence": 60,
                "ocr_language": "eng",
            },
        )

    def test_correction_refresh_updates_next_read_without_replacing_pending_read(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "corrections.json"
            store = OCRCorrectionStore(path)
            store.upsert_entries({"Mareus": "Marcus"})
            controller = AppController(
                AppSettings(active_profile_id="game"), correction_store=store
            )
            self.addCleanup(controller.shutdown)
            controller.voice_router = Mock()
            controller.live_reader = Mock(is_running=False)
            controller.capture_executor = executor = Mock()
            first_read, second_read = Future(), Future()
            executor.submit.side_effect = [first_read, second_read]
            controller.runtime_lifecycle._configure_dialog_read(Path("captures"))
            schedule = controller.schedule_dialog_read
            self.assertTrue(schedule())
            original_dictionary = executor.submit.call_args.kwargs[
                "correction_dictionary"
            ]

            external = OCRCorrectionStore.load(path)
            external.upsert_entries({"Mareus": "Profile Marcus"}, "game")
            controller.refresh_corrections()

            self.assertIs(controller.schedule_dialog_read, schedule)
            self.assertEqual(original_dictionary.correct_text("Mareus")[0], "Marcus")
            self.assertFalse(schedule())
            self.assertEqual(executor.submit.call_count, 1)
            first_read.set_result(None)
            self.assertTrue(schedule())
            current_dictionary = executor.submit.call_args.kwargs[
                "correction_dictionary"
            ]
            self.assertEqual(
                current_dictionary.correct_text("Mareus")[0], "Profile Marcus"
            )
            second_read.set_result(None)

    def test_backend_initialization_resolves_narrator_once(self):
        for voice in (
            None,
            CharacterVoice("Narrator", "Ada"),
            CharacterVoice("Narrator", "Ada", references=(Path("ada.wav"),)),
        ):
            with self.subTest(voice=voice):
                registry = CharacterVoiceRegistry(() if voice is None else (voice,))
                backend_factory = Mock(return_value=Mock())
                controller = AppController(
                    AppSettings(speech_backend="moss-tts"),
                    moss_backend_factory=backend_factory,
                    model_asset_manager_factory=Mock(),
                )
                controller.voice_registry_initializer = Mock(return_value=registry)
                with patch.object(
                    registry, "resolve", wraps=registry.resolve
                ) as resolve:
                    self.assertTrue(
                        controller.runtime_lifecycle._initialize_backend(False)
                    )
                resolve.assert_called_once_with("Narrator")
                self.assertEqual(
                    backend_factory.call_args.kwargs["narrator_reference"],
                    None if voice is None or not voice.references else Path("ada.wav"),
                )
                controller.shutdown()

    def test_audio_settings_update_each_owner_once(self):
        for route in (
            "tts-only",
            "live",
            "generated-live",
            "xtts",
            "generated-xtts",
            "distinct",
        ):
            with self.subTest(route=route):
                controller = AppController(
                    AppSettings(
                        output_volume_percent=35,
                        speech_rate_percent=125,
                        tts_profile="fast",
                    )
                )
                tts = Mock()
                if route == "tts-only":
                    backend = None
                elif route.endswith("live"):
                    backend = tts
                elif route.endswith("xtts"):
                    backend = XTTSVoiceRouterBackend(CharacterVoiceRouter(tts))
                else:
                    backend = Mock()
                controller.tts = tts
                controller.speech_backend = backend
                if route.startswith("generated"):
                    controller.speech_backend = GeneratedAudioFallbackBackend(
                        backend,
                        None,
                        controller.chapter_voice_preloader,
                        audio_source_policy="prefer-game-audio",
                    )
                controller.runtime_lifecycle._apply_runtime_audio_settings()
                if route.endswith("live"):
                    tts.set_generation_profile.assert_called_once_with("fast")
                tts.set_volume.assert_called_once_with(0.35)
                tts.set_speed.assert_called_once_with(1.25)
                if route == "distinct":
                    backend.set_volume.assert_called_once_with(0.35)
                    backend.set_speed.assert_called_once_with(1.25)
                    backend.set_generation_profile.assert_called_once_with("fast")
                if route.startswith("generated"):
                    self.assertEqual(controller.speech_backend.volume, 0.35)
                    self.assertEqual(controller.speech_backend.speed, 1.25)
