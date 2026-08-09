import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, Mock, patch

from vntts.live import SpeechChunk
from vntts.main import (
    AppController,
    OCRError,
    OCRUncertainError,
    ScreenCaptureError,
    capture_dialog,
    create_dialog_read_scheduler,
    create_screenshot_path,
    get_live_configuration,
    get_screenshot_directory,
    get_tts_configuration,
    initialize_tts,
    initialize_voice_router,
    main,
    read_dialog,
    read_dialog_safely,
    read_live_snapshot,
    recognize_screenshot,
    speak_live_chunk,
)
from vntts.ocr import DialogRegion, OCRResult
from vntts.services.tts_engine import AudioPlaybackError, TTSSynthesisError
from vntts.settings import AppSettings


class MainTest(unittest.TestCase):
    def test_one_time_read_routes_text_by_detected_character(self):
        voice_router = Mock()
        image = object()
        with (
            patch(
                "vntts.main.capture_dialog",
                return_value=(image, Path("capture.png")),
            ),
            patch(
                "vntts.main.recognize_screenshot",
                return_value=("Lucy", "Hello."),
            ),
            redirect_stdout(io.StringIO()),
        ):
            read_dialog(voice_router, Path("captures"))

        voice_router.speak.assert_called_once_with("Lucy", "Hello.")

    def test_live_chunk_routes_text_by_detected_character(self):
        voice_router = Mock()

        with redirect_stdout(io.StringIO()):
            speak_live_chunk(
                voice_router,
                SpeechChunk(1, "Regulus", "Rock and roll!"),
            )

        voice_router.speak.assert_called_once_with("Regulus", "Rock and roll!")

    def test_live_chunk_passes_stale_playback_guard(self):
        voice_router = Mock()
        playback_guard = Mock(return_value=True)

        with redirect_stdout(io.StringIO()):
            speak_live_chunk(
                voice_router,
                SpeechChunk(2, "Regulus", "Still current."),
                playback_guard,
            )

        voice_router.speak.assert_called_once_with(
            "Regulus",
            "Still current.",
            playback_guard=playback_guard,
        )

    def test_one_time_read_can_enqueue_speech_in_shared_queue(self):
        voice_router = Mock()
        speech_handler = Mock()
        image = object()
        with (
            patch(
                "vntts.main.capture_dialog", return_value=(image, Path("capture.png"))
            ),
            patch(
                "vntts.main.recognize_screenshot",
                return_value=("Lucy", "Hello."),
            ),
            redirect_stdout(io.StringIO()),
        ):
            read_dialog(
                voice_router,
                Path("captures"),
                speech_handler=speech_handler,
            )

        speech_handler.assert_called_once_with("Lucy", "Hello.")
        voice_router.speak.assert_not_called()

    def test_capture_failure_identifies_screen_capture_stage(self):
        with patch(
            "vntts.main.mss.mss",
            side_effect=RuntimeError("display unavailable"),
        ):
            with self.assertRaisesRegex(ScreenCaptureError, "display unavailable"):
                capture_dialog()

    def test_capture_creates_configured_directory_and_saves_rgb_image(self):
        screenshot = Mock(size=(1, 1), bgra=b"\x00\x00\x00\xff")
        screen = Mock()
        screen.monitors = [None, {"height": 100, "width": 100}]
        screen.grab.return_value = screenshot

        with TemporaryDirectory() as temporary_directory:
            screenshot_directory = Path(temporary_directory) / "nested"
            with patch("vntts.main.mss.mss") as mss_factory:
                mss_factory.return_value.__enter__.return_value = screen
                image, output = capture_dialog(
                    screenshot_directory,
                    region=DialogRegion(0.1, 0.6, 0.8, 0.3),
                )

            self.assertEqual(image.mode, "RGB")
            self.assertEqual(output.parent, screenshot_directory)
            self.assertTrue(output.is_file())
            screen.grab.assert_called_once_with(
                {"left": 10, "top": 60, "width": 80, "height": 30}
            )

    def test_capture_uses_selected_window_geometry(self):
        screenshot = Mock(size=(1, 1), bgra=b"\x00\x00\x00\xff")
        screen = Mock()
        screen.grab.return_value = screenshot
        capture_target = Mock()
        capture_target.capture_box.return_value = {
            "left": 300,
            "top": 700,
            "width": 1200,
            "height": 250,
        }

        with TemporaryDirectory() as temporary_directory:
            with patch("vntts.main.mss.mss") as mss_factory:
                mss_factory.return_value.__enter__.return_value = screen
                capture_dialog(
                    temporary_directory,
                    capture_target=capture_target,
                )

        capture_target.capture_box.assert_called_once()
        screen.grab.assert_called_once_with(capture_target.capture_box.return_value)

    def test_ocr_failure_identifies_tesseract_stage(self):
        with patch(
            "vntts.main.recognize_dialog_image_result",
            side_effect=RuntimeError("tesseract unavailable"),
        ):
            with self.assertRaisesRegex(OCRError, "tesseract unavailable"):
                recognize_screenshot(object())

    def test_one_time_read_rejects_uncertain_ocr(self):
        result = OCRResult("Marcus", "Garbled text", 32.0, "balanced", 3)
        with patch("vntts.main.recognize_dialog_image_result", return_value=result):
            with self.assertRaisesRegex(OCRUncertainError, "32%"):
                recognize_screenshot(object(), minimum_confidence=60)

    def test_live_read_withholds_uncertain_text_and_reports_confidence(self):
        result = OCRResult("Marcus", "Garbled text", 42.0, "balanced", 3)
        uncertain_handler = Mock()
        uncertain_frame_recorder = Mock()
        with (
            patch("vntts.main.capture_dialog", return_value=(object(), None)),
            patch("vntts.main.recognize_screenshot_result", return_value=result),
        ):
            snapshot = read_live_snapshot(
                Path("captures"),
                minimum_confidence=60,
                uncertain_handler=uncertain_handler,
                uncertain_frame_recorder=uncertain_frame_recorder,
            )

        self.assertEqual(snapshot, (None, ""))
        uncertain_handler.assert_called_once_with(result, 60)
        uncertain_frame_recorder.record.assert_called_once_with(
            ANY,
            result,
            60,
        )

    def test_live_read_resets_diagnostic_deduplication_after_confident_text(self):
        result = OCRResult("Marcus", "Reliable text", 92.0, "balanced", 1)
        recorder = Mock()
        with (
            patch("vntts.main.capture_dialog", return_value=(object(), None)),
            patch("vntts.main.recognize_screenshot_result", return_value=result),
        ):
            snapshot = read_live_snapshot(
                Path("captures"),
                uncertain_frame_recorder=recorder,
            )

        self.assertEqual(snapshot, ("Marcus", "Reliable text"))
        recorder.reset.assert_called_once_with()

    def test_runtime_failures_are_reported_by_stage(self):
        failures = [
            (ScreenCaptureError("no display"), "Screen capture failed: no display"),
            (OCRError("ocr crashed"), "Tesseract OCR failed: ocr crashed"),
            (
                TTSSynthesisError("model crashed"),
                "TTS model or synthesis failed: model crashed",
            ),
            (
                AudioPlaybackError("device lost"),
                "Audio playback failed: device lost",
            ),
        ]

        for error, expected_message in failures:
            with self.subTest(error=error):
                errors = io.StringIO()
                with (
                    redirect_stderr(errors),
                    patch("vntts.main.read_dialog", side_effect=error),
                ):
                    read_dialog_safely(Mock(), Path("captures"))

                self.assertEqual(errors.getvalue().strip(), expected_message)

    def test_scheduler_allows_retry_after_failed_job_finishes(self):
        executor = Mock()
        failed_job = Mock()
        failed_job.done.return_value = True
        retry_job = Mock()
        executor.submit.side_effect = [failed_job, retry_job]
        tts = Mock()
        screenshot_directory = Path("captures")
        schedule_dialog_read = create_dialog_read_scheduler(
            executor,
            tts,
            screenshot_directory,
        )

        schedule_dialog_read()
        schedule_dialog_read()

        self.assertEqual(executor.submit.call_count, 2)
        executor.submit.assert_called_with(
            read_dialog_safely,
            tts,
            screenshot_directory,
            minimum_confidence=60.0,
        )

    def test_scheduler_rejects_one_time_read_while_live_mode_is_active(self):
        executor = Mock()
        live_reader = Mock(is_running=True)
        output = io.StringIO()
        schedule_dialog_read = create_dialog_read_scheduler(
            executor,
            Mock(),
            Path("captures"),
            live_reader=live_reader,
        )

        with redirect_stdout(output):
            schedule_dialog_read()

        executor.submit.assert_not_called()
        self.assertIn("Stop live reading", output.getvalue())

    def test_screenshot_directory_is_configurable(self):
        with patch.dict(
            "os.environ",
            {"VNTTS_SCREENSHOT_DIR": "custom/captures"},
        ):
            self.assertEqual(
                get_screenshot_directory(),
                Path("custom/captures"),
            )

    def test_screenshot_names_do_not_collide_within_one_second(self):
        first_id = Mock(hex="first")
        second_id = Mock(hex="second")
        timestamp = Mock()
        timestamp.strftime.return_value = "2026-08-08-12-00-00"

        with (
            patch("vntts.main.datetime") as datetime_module,
            patch("vntts.main.uuid4", side_effect=[first_id, second_id]),
        ):
            datetime_module.now.return_value = timestamp
            first = create_screenshot_path(Path("captures"))
            second = create_screenshot_path(Path("captures"))

        self.assertEqual(
            first,
            Path("captures/dialog-2026-08-08-12-00-00-first.png"),
        )
        self.assertEqual(
            second,
            Path("captures/dialog-2026-08-08-12-00-00-second.png"),
        )
        self.assertNotEqual(first, second)

    def test_initialize_tts_reports_loading_progress(self):
        tts = object()
        tts_factory = Mock(return_value=tts)
        output = io.StringIO()

        with redirect_stdout(output):
            result = initialize_tts(tts_factory)

        self.assertIs(result, tts)
        tts_factory.assert_called_once_with()
        self.assertEqual(
            output.getvalue().splitlines(),
            ["Loading TTS model...", "TTS model loaded"],
        )

    def test_tts_configuration_is_read_from_environment(self):
        with patch.dict(
            "os.environ",
            {
                "VNTTS_TTS_MODEL": "xtts",
                "VNTTS_TTS_SPEAKER": "reverse-1999-lucy",
                "VNTTS_TTS_LANGUAGE": "en",
                "VNTTS_TTS_SPEAKER_WAV": "/path/to/lucy.ogg",
            },
            clear=True,
        ):
            self.assertEqual(
                get_tts_configuration(),
                {
                    "model_name": "xtts",
                    "speaker": "reverse-1999-lucy",
                    "language": "en",
                    "speaker_wav": "/path/to/lucy.ogg",
                },
            )

    def test_initialize_tts_passes_environment_configuration(self):
        tts = object()
        tts_factory = Mock(return_value=tts)

        with (
            patch.dict(
                "os.environ",
                {
                    "VNTTS_TTS_MODEL": "xtts",
                    "VNTTS_TTS_SPEAKER": "reverse-1999-lucy",
                    "VNTTS_TTS_LANGUAGE": "en",
                },
                clear=True,
            ),
            redirect_stdout(io.StringIO()),
        ):
            result = initialize_tts(tts_factory)

        self.assertIs(result, tts)
        tts_factory.assert_called_once_with(
            model_name="xtts",
            speaker="reverse-1999-lucy",
            language="en",
        )

    def test_tts_profile_is_read_from_environment(self):
        with patch.dict(
            "os.environ",
            {"VNTTS_TTS_PROFILE": "expressive"},
            clear=True,
        ):
            configuration = get_tts_configuration()

        self.assertEqual(configuration["synthesis_options"]["temperature"], 0.95)
        self.assertFalse(configuration["synthesis_options"]["split_sentences"])

    def test_invalid_tts_profile_uses_stable_profile(self):
        errors = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {"VNTTS_TTS_PROFILE": "robot"},
                clear=True,
            ),
            redirect_stderr(errors),
        ):
            configuration = get_tts_configuration()

        self.assertEqual(configuration["synthesis_options"]["temperature"], 0.70)
        self.assertIn("Using 'stable'", errors.getvalue())

    def test_voice_router_uses_configured_narrator(self):
        tts = Mock()
        with patch.dict(
            "os.environ",
            {"VNTTS_NARRATOR_SPEAKER": "Claribel Dervla"},
            clear=True,
        ):
            voice_router = initialize_voice_router(tts)

        self.assertIs(voice_router.tts, tts)
        self.assertEqual(voice_router.narrator_speaker, "Claribel Dervla")

    def test_main_reports_tts_failure_without_starting_listener(self):
        tts_factory = Mock(side_effect=RuntimeError("model unavailable"))
        output = io.StringIO()
        errors = io.StringIO()

        with (
            redirect_stdout(output),
            redirect_stderr(errors),
            patch("vntts.main.listen_for_hotkeys") as listen_for_hotkeys,
        ):
            result = main(tts_factory)

        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "Loading TTS model...\n")
        self.assertIn(
            "Unable to initialize TTS engine: model unavailable",
            errors.getvalue(),
        )
        listen_for_hotkeys.assert_not_called()

    def test_live_configuration_is_read_from_environment(self):
        with patch.dict(
            "os.environ",
            {
                "VNTTS_LIVE_INTERVAL_MS": "150",
                "VNTTS_LIVE_STABILITY_FRAMES": "3",
                "VNTTS_LIVE_IDLE_FLUSH_MS": "600",
                "VNTTS_LIVE_MIN_CHUNK_CHARACTERS": "12",
            },
            clear=True,
        ):
            self.assertEqual(
                get_live_configuration(),
                {
                    "interval_seconds": 0.15,
                    "tracker_options": {
                        "stability_frames": 3,
                        "idle_flush_seconds": 0.6,
                        "min_chunk_characters": 12,
                    },
                },
            )

    def test_invalid_live_configuration_uses_defaults(self):
        output = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "VNTTS_LIVE_INTERVAL_MS": "bad",
                    "VNTTS_LIVE_STABILITY_FRAMES": "1",
                },
                clear=True,
            ),
            redirect_stdout(output),
        ):
            configuration = get_live_configuration()

        self.assertEqual(configuration["interval_seconds"], 0.2)
        self.assertEqual(configuration["tracker_options"]["stability_frames"], 2)
        self.assertIn("Invalid VNTTS_LIVE_INTERVAL_MS", output.getvalue())
        self.assertIn("Invalid VNTTS_LIVE_STABILITY_FRAMES", output.getvalue())

    def test_controller_passes_initialized_tts_to_dialog_scheduler(self):
        tts = object()
        tts_factory = Mock(return_value=tts)
        schedule_dialog_read = Mock()
        capture_executor = Mock()
        speech_executor = Mock()
        live_reader = Mock()
        voice_router = Mock()
        model_assets = Mock()
        screenshot_directory = Path("custom/captures")
        settings = AppSettings(screenshot_directory=str(screenshot_directory))

        with (
            redirect_stdout(io.StringIO()),
            patch(
                "vntts.main.initialize_voice_router",
                return_value=voice_router,
            ),
            patch(
                "vntts.main.ThreadPoolExecutor",
                side_effect=[capture_executor, speech_executor],
            ),
            patch(
                "vntts.main.LiveDialogReader",
                return_value=live_reader,
            ) as live_reader_factory,
            patch(
                "vntts.main.create_dialog_read_scheduler",
                schedule_dialog_read,
            ),
        ):
            controller = AppController(
                settings,
                tts_factory=tts_factory,
                model_asset_manager_factory=Mock(return_value=model_assets),
            )
            result = controller.start()

        self.assertTrue(result)
        model_assets.configure_environment.assert_called_once_with()
        live_reader_factory.assert_called_once()
        schedule_dialog_read.assert_called_once_with(
            capture_executor,
            voice_router,
            screenshot_directory,
            live_reader=live_reader,
            error_handler=controller.error_handler,
            capture_target=None,
            speech_handler=live_reader.enqueue,
            minimum_confidence=60,
            uncertain_frame_recorder=None,
        )

    def test_main_connects_hotkeys_to_controller_and_shuts_it_down(self):
        settings = AppSettings(
            read_hotkey="<ctrl>+h",
            live_hotkey="<ctrl>+l",
        )
        controller = Mock()
        controller.start.return_value = True

        with (
            patch("vntts.main.load_app_settings", return_value=settings),
            patch("vntts.main.AppController", return_value=controller),
            patch("vntts.main.listen_for_hotkeys") as listen_for_hotkeys,
        ):
            result = main()

        self.assertEqual(result, 0)
        listen_for_hotkeys.assert_called_once_with(
            "<ctrl>+h",
            "<ctrl>+l",
            "<ctrl>+<shift>+p",
            "<ctrl>+<shift>+s",
            "<ctrl>+<shift>+r",
            "<ctrl>+<shift>+x",
            controller.read_once,
            controller.toggle_live,
            controller.toggle_speech_pause,
            controller.skip_current_speech,
            controller.repeat_last_speech,
            controller.clear_speech_queue,
        )
        controller.shutdown.assert_called_once_with()

    def test_controller_applies_window_capture_settings_without_restart(self):
        capture_target = Mock()
        capture_target_factory = Mock(return_value=capture_target)
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            capture_target_factory=capture_target_factory,
        )

        controller.apply_settings(
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
            )
        )

        capture_target_factory.assert_called_once_with("Reverse: 1999")
        self.assertIs(controller.capture_target, capture_target)

    def test_controller_end_to_end_test_recognizes_and_speaks_dialog(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.live_reader = Mock()
        controller.voice_router = Mock()
        controller.capture_target = Mock()
        image = object()

        with (
            patch("vntts.main.capture_dialog", return_value=(image, None)),
            patch(
                "vntts.main.recognize_screenshot",
                return_value=("Marcus", "This is a complete test."),
            ),
        ):
            character, text = controller.test_current_dialog()

        self.assertEqual(character, "Marcus")
        self.assertEqual(text, "This is a complete test.")
        controller.voice_router.speak.assert_called_once_with(
            "Marcus",
            "This is a complete test.",
        )

    def test_controller_exposes_speech_queue_controls(self):
        statuses = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            status_handler=statuses.append,
        )
        controller.live_reader = Mock()
        controller.live_reader.toggle_pause.return_value = True
        controller.live_reader.skip_current.return_value = True
        controller.live_reader.repeat_last.return_value = True
        controller.live_reader.clear_queue.return_value = True

        self.assertTrue(controller.toggle_speech_pause())
        self.assertTrue(controller.skip_current_speech())
        self.assertTrue(controller.repeat_last_speech())
        self.assertTrue(controller.clear_speech_queue())

        controller.live_reader.toggle_pause.assert_called_once_with()
        controller.live_reader.skip_current.assert_called_once_with()
        controller.live_reader.repeat_last.assert_called_once_with()
        controller.live_reader.clear_queue.assert_called_once_with()
        self.assertEqual(statuses[-1], "Speech queue cleared")

    def test_controller_reports_current_recognized_dialog(self):
        dialogs = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            dialog_handler=lambda character, text: dialogs.append((character, text)),
        )

        controller._dialog_observed("Marcus", "A line visible in the tray")

        self.assertEqual(dialogs[-1], ("Marcus", "A line visible in the tray"))

    def test_controller_reports_uncertain_ocr_confidence(self):
        dialogs = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            dialog_handler=lambda character, text: dialogs.append((character, text)),
        )

        controller._ocr_uncertain(
            OCRResult("Marcus", "Possibly incorrect", 42.4, "balanced", 3),
            60,
        )

        self.assertEqual(dialogs[-1][0], "OCR uncertain")
        self.assertIn("42% (requires 60%)", dialogs[-1][1])

    def test_controller_sets_coqui_acceptance_for_approved_xtts_use(self):
        tts = Mock()
        tts_factory = Mock(return_value=tts)
        voice_router = Mock()
        model_assets = Mock()
        settings = AppSettings(
            tts_model="tts_models/multilingual/multi-dataset/xtts_v2",
            xtts_terms_accepted=True,
        )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("vntts.main.initialize_voice_router", return_value=voice_router),
            patch("vntts.main.ThreadPoolExecutor", side_effect=[Mock(), Mock()]),
            patch("vntts.main.LiveDialogReader", return_value=Mock()),
            patch("vntts.main.create_dialog_read_scheduler", return_value=Mock()),
        ):
            controller = AppController(
                settings,
                tts_factory=tts_factory,
                model_asset_manager_factory=Mock(return_value=model_assets),
            )
            self.assertTrue(controller.start())
            self.assertEqual(os.environ["COQUI_TOS_AGREED"], "1")
            model_assets.configure_environment.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
