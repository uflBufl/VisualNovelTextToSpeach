import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, Mock, patch

from PIL import Image, ImageDraw

from vntts.diagnostics import DiagnosticSnapshot
from vntts.live import AdaptiveSpeechBackpressure, SpeechChunk
from vntts.main import (
    AppController,
    CapturedDialogFrame,
    OCRError,
    OCRUncertainError,
    ScreenCaptureError,
    capture_dialog,
    create_dialog_read_scheduler,
    create_screenshot_path,
    fingerprint_dialog_frame,
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
    recognize_screenshot_result,
    speak_live_chunk,
)
from vntts.ocr import DialogRegion, OCRResult
from vntts.services.tts_engine import AudioPlaybackError, TTSSynthesisError
from vntts.settings import AppSettings
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class MainTest(unittest.TestCase):
    def test_dialog_fingerprint_changes_when_only_the_text_changes(self):
        first = Image.new("RGB", (1200, 240), "#202020")
        second = first.copy()
        ImageDraw.Draw(first).text(
            (60, 100),
            "The first dialogue is visible on screen.",
            fill="white",
        )
        ImageDraw.Draw(second).text(
            (60, 100),
            "A completely different line replaced it.",
            fill="white",
        )

        first_fingerprint = fingerprint_dialog_frame(CapturedDialogFrame(first, 0))
        second_fingerprint = fingerprint_dialog_frame(CapturedDialogFrame(second, 0))

        self.assertNotEqual(first_fingerprint, second_fingerprint)
        self.assertEqual(
            first_fingerprint,
            fingerprint_dialog_frame(CapturedDialogFrame(first.copy(), 0)),
        )

    def test_one_time_read_routes_text_by_detected_character(self):
        voice_router = Mock()
        image = object()
        with (
            patch(
                "vntts.dialog_capture.capture_dialog",
                return_value=(image, Path("capture.png")),
            ),
            patch(
                "vntts.dialog_capture.recognize_screenshot_result",
                return_value=OCRResult("Lucy", "Hello.", 95.0, "balanced", 1),
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
                "vntts.dialog_capture.capture_dialog",
                return_value=(image, Path("capture.png")),
            ),
            patch(
                "vntts.dialog_capture.recognize_screenshot_result",
                return_value=OCRResult("Lucy", "Hello.", 95.0, "balanced", 1),
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
            "vntts.dialog_capture.mss.mss",
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
            with patch("vntts.dialog_capture.mss.mss") as mss_factory:
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
            with patch("vntts.dialog_capture.mss.mss") as mss_factory:
                mss_factory.return_value.__enter__.return_value = screen
                capture_dialog(
                    temporary_directory,
                    capture_target=capture_target,
                )

        capture_target.capture_box.assert_called_once()
        screen.grab.assert_called_once_with(capture_target.capture_box.return_value)

    def test_ocr_failure_identifies_tesseract_stage(self):
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result",
            side_effect=RuntimeError("tesseract unavailable"),
        ):
            with self.assertRaisesRegex(OCRError, "tesseract unavailable"):
                recognize_screenshot(object())

    def test_recognition_accepts_a_pluggable_ocr_backend(self):
        backend = Mock()
        backend.recognize.return_value = OCRResult(
            "Marcus",
            "Backend result.",
            95.0,
            "custom",
            1,
        )
        image = object()

        result = recognize_screenshot_result(
            image,
            ocr_language="eng+jpn",
            ocr_backend=backend,
        )

        self.assertEqual(result.text, "Backend result.")
        backend.recognize.assert_called_once_with(
            image,
            None,
            minimum_confidence=60.0,
            language="eng+jpn",
        )

    def test_one_time_read_rejects_uncertain_ocr(self):
        result = OCRResult("Marcus", "Garbled text", 32.0, "balanced", 3)
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", return_value=result
        ):
            with self.assertRaisesRegex(OCRUncertainError, "32%"):
                recognize_screenshot(object(), minimum_confidence=60)

    def test_live_read_withholds_uncertain_text_and_reports_confidence(self):
        result = OCRResult("Marcus", "Garbled text", 42.0, "balanced", 3)
        uncertain_handler = Mock()
        uncertain_frame_recorder = Mock()
        with (
            patch("vntts.dialog_capture.capture_dialog", return_value=(object(), None)),
            patch(
                "vntts.dialog_capture.recognize_screenshot_result", return_value=result
            ),
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
            patch("vntts.dialog_capture.capture_dialog", return_value=(object(), None)),
            patch(
                "vntts.dialog_capture.recognize_screenshot_result", return_value=result
            ),
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
                    patch("vntts.dialog_capture.read_dialog", side_effect=error),
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
            ocr_language="eng",
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
            patch("vntts.dialog_capture.datetime") as datetime_module,
            patch("vntts.dialog_capture.uuid4", side_effect=[first_id, second_id]),
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

    def test_saved_speech_controls_configure_synthesis_and_playback(self):
        configuration = get_tts_configuration(
            AppSettings(
                tts_model="tts_models/multilingual/multi-dataset/xtts_v2",
                output_volume_percent=65,
                speech_rate_percent=115,
            )
        )

        self.assertEqual(configuration["volume"], 0.65)
        self.assertEqual(configuration["synthesis_options"]["speed"], 1.15)

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

    def test_voice_router_uses_discovered_local_voice_pack(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            reference = root / "fatutu.ogg"
            reference.write_bytes(b"voice")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "voices": [
                            {
                                "character": "Fatutu",
                                "speaker": "reverse-1999-fatutu-v2",
                                "reference": reference.name,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "vntts.runtime_config.find_default_voice_manifest",
                return_value=manifest,
            ):
                voice_router = initialize_voice_router(Mock(), AppSettings())

        self.assertEqual(
            voice_router.registry.resolve("Fatutu").speaker,
            "reverse-1999-fatutu-v2",
        )

    def test_main_reports_tts_failure_without_starting_listener(self):
        tts_factory = Mock(side_effect=RuntimeError("model unavailable"))
        output = io.StringIO()
        errors = io.StringIO()

        with (
            redirect_stdout(output),
            redirect_stderr(errors),
            patch(
                "vntts.main.load_app_settings",
                return_value=AppSettings(speech_backend="coqui-xtts"),
            ),
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
        ocr_executor = Mock()
        speech_executor = Mock()
        playback_executor = Mock()
        live_reader = Mock()
        voice_router = Mock()
        model_assets = Mock()
        screenshot_directory = Path("custom/captures")
        settings = AppSettings(
            screenshot_directory=str(screenshot_directory),
            warm_up_voices=True,
            speech_backend="coqui-xtts",
        )

        with (
            redirect_stdout(io.StringIO()),
            patch(
                "vntts.controller.initialize_voice_router",
                return_value=voice_router,
            ),
            patch(
                "vntts.controller.ThreadPoolExecutor",
                side_effect=[
                    capture_executor,
                    ocr_executor,
                    speech_executor,
                    playback_executor,
                ],
            ),
            patch(
                "vntts.controller.LiveDialogReader",
                return_value=live_reader,
            ) as live_reader_factory,
            patch(
                "vntts.controller.create_dialog_read_scheduler",
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
        voice_router.warm_up.assert_called_once_with(
            progress=controller._warmup_progress
        )
        live_reader_factory.assert_called_once()
        schedule_dialog_read.assert_called_once_with(
            capture_executor,
            voice_router,
            screenshot_directory,
            live_reader=live_reader,
            error_handler=controller.error_handler,
            capture_target=None,
            speech_handler=controller._enqueue_dialog,
            minimum_confidence=60,
            uncertain_frame_recorder=None,
            diagnostic_handler=controller._publish_diagnostic,
            voice_resolver=controller._resolve_voice_label,
            ocr_language="eng",
            correction_dictionary=controller.correction_dictionary,
        )

    def test_controller_can_skip_startup_voice_warmup(self):
        tts = Mock()
        voice_router = Mock()
        statuses = []
        with (
            patch(
                "vntts.controller.initialize_voice_router", return_value=voice_router
            ),
            patch("vntts.controller.ThreadPoolExecutor", return_value=Mock()),
            patch("vntts.controller.LiveDialogReader", return_value=Mock()),
            patch("vntts.controller.create_dialog_read_scheduler", return_value=Mock()),
        ):
            controller = AppController(
                AppSettings(warm_up_voices=False),
                tts_factory=Mock(return_value=tts),
                status_handler=statuses.append,
            )

            self.assertTrue(controller.start())

        voice_router.warm_up.assert_not_called()
        self.assertTrue(any("voice warm-up skipped" in status for status in statuses))
        controller.shutdown()

    def test_controller_loads_only_chatterbox_when_selected(self):
        backend = Mock()
        backend.registry = Mock()
        backend.narrator_speaker = "Chatterbox default"
        backend.capabilities.concurrent_prepare_and_play = False
        backend_factory = Mock(return_value=backend)
        tts_factory = Mock()
        model_assets = Mock()
        registry = Mock()
        with (
            patch("vntts.controller.initialize_voice_registry", return_value=registry),
            patch("vntts.controller.ThreadPoolExecutor", return_value=Mock()),
            patch(
                "vntts.controller.LiveDialogReader", return_value=Mock()
            ) as live_reader_factory,
            patch("vntts.controller.create_dialog_read_scheduler", return_value=Mock()),
        ):
            controller = AppController(
                AppSettings(speech_backend="chatterbox-nano"),
                tts_factory=tts_factory,
                chatterbox_backend_factory=backend_factory,
                model_asset_manager_factory=Mock(return_value=model_assets),
            )

            self.assertTrue(controller.start())

        tts_factory.assert_not_called()
        model_assets.configure_environment.assert_not_called()
        model_assets.configure_huggingface_environment.assert_called_once_with()
        backend_factory.assert_called_once_with(
            registry,
            narrator_reference=None,
            volume=1.0,
        )
        self.assertIs(controller.tts, backend)
        self.assertIs(controller.voice_router, backend)
        self.assertIs(controller.speech_backend, backend)
        self.assertEqual(
            live_reader_factory.call_args.kwargs["max_speech_jobs"],
            1,
        )
        controller.shutdown()

    def test_controller_loads_only_pocket_tts_when_selected(self):
        backend = Mock()
        backend.registry = Mock()
        backend.narrator_speaker = "Pocket TTS default"
        backend.capabilities.concurrent_prepare_and_play = False
        pocket_factory = Mock(return_value=backend)
        chatterbox_factory = Mock()
        tts_factory = Mock()
        model_assets = Mock()
        registry = Mock()
        statuses = []
        with (
            patch("vntts.controller.initialize_voice_registry", return_value=registry),
            patch("vntts.controller.ThreadPoolExecutor", return_value=Mock()),
            patch("vntts.controller.LiveDialogReader", return_value=Mock()),
            patch("vntts.controller.create_dialog_read_scheduler", return_value=Mock()),
        ):
            controller = AppController(
                AppSettings(speech_backend="pocket-tts"),
                tts_factory=tts_factory,
                chatterbox_backend_factory=chatterbox_factory,
                pocket_backend_factory=pocket_factory,
                model_asset_manager_factory=Mock(return_value=model_assets),
                status_handler=statuses.append,
            )

            self.assertTrue(controller.start())

        self.assertIn("Loading Pocket TTS...", statuses)
        tts_factory.assert_not_called()
        chatterbox_factory.assert_not_called()
        model_assets.configure_huggingface_environment.assert_not_called()
        pocket_factory.assert_called_once_with(
            registry,
            narrator_reference=None,
            volume=1.0,
        )
        self.assertIs(controller.speech_backend, backend)
        controller.shutdown()

    def test_voice_warmup_failure_does_not_prevent_startup(self):
        tts = Mock()
        voice_router = Mock()
        voice_router.warm_up.side_effect = RuntimeError("invalid reference")
        statuses = []
        errors = []
        with (
            patch(
                "vntts.controller.initialize_voice_router", return_value=voice_router
            ),
            patch("vntts.controller.ThreadPoolExecutor", return_value=Mock()),
            patch("vntts.controller.LiveDialogReader", return_value=Mock()),
            patch("vntts.controller.create_dialog_read_scheduler", return_value=Mock()),
        ):
            controller = AppController(
                AppSettings(
                    warm_up_voices=True,
                    speech_backend="coqui-xtts",
                ),
                tts_factory=Mock(return_value=tts),
                status_handler=statuses.append,
                error_handler=errors.append,
            )

            self.assertTrue(controller.start())

        self.assertEqual(errors, [voice_router.warm_up.side_effect])
        self.assertTrue(any("load on demand" in status for status in statuses))
        controller.shutdown()

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
            settings.pause_hotkey,
            settings.skip_hotkey,
            settings.repeat_hotkey,
            settings.clear_queue_hotkey,
            settings.emergency_stop_hotkey,
            controller.read_once,
            controller.toggle_live,
            controller.toggle_speech_pause,
            controller.skip_current_speech,
            controller.repeat_last_speech,
            controller.clear_speech_queue,
            controller.emergency_stop,
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
            patch("vntts.dialog_capture.capture_dialog", return_value=(image, None)),
            patch(
                "vntts.dialog_capture.recognize_screenshot_result",
                return_value=OCRResult(
                    "Marcus",
                    "This is a complete test.",
                    95.0,
                    "balanced",
                    1,
                ),
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
        controller.live_reader.emergency_stop.return_value = True

        self.assertTrue(controller.toggle_speech_pause())
        self.assertTrue(controller.skip_current_speech())
        self.assertTrue(controller.repeat_last_speech())
        self.assertTrue(controller.clear_speech_queue())
        self.assertTrue(controller.emergency_stop())

        controller.live_reader.toggle_pause.assert_called_once_with()
        controller.live_reader.skip_current.assert_called_once_with()
        controller.live_reader.repeat_last.assert_called_once_with()
        controller.live_reader.clear_queue.assert_called_once_with()
        controller.live_reader.emergency_stop.assert_called_once_with()
        self.assertEqual(
            statuses[-1],
            "Emergency stop: live reading and speech stopped",
        )

    def test_controller_lists_and_previews_character_voices_on_speech_executor(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.live_reader = Mock()
        controller.live_reader.is_running = False
        controller.speech_executor = Mock()
        controller.voice_router = Mock()
        marcus = Mock(character="Marcus")
        lucy = Mock(character="Lucy")
        controller.voice_router.registry.voices = {
            "marcus": marcus,
            "marcus-alias": marcus,
            "lucy": lucy,
        }

        result = controller.preview_voice("Marcus", "  Hello.  ")

        self.assertIs(result, controller.speech_executor.submit.return_value)
        self.assertEqual(
            controller.available_voice_characters(),
            ["Narrator", "Lucy", "Marcus"],
        )
        controller.speech_executor.submit.assert_called_once_with(
            controller._preview_voice,
            "Marcus",
            "Hello.",
        )

    def test_applying_settings_updates_loaded_speech_controls(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.tts = Mock()

        controller.apply_settings(
            AppSettings(output_volume_percent=35, speech_rate_percent=125)
        )

        controller.tts.set_volume.assert_called_once_with(0.35)
        controller.tts.set_speed.assert_called_once_with(1.25)

    def test_controller_reports_current_recognized_dialog(self):
        dialogs = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            dialog_handler=lambda character, text: dialogs.append((character, text)),
        )

        controller._dialog_observed("Marcus", "A line visible in the tray")

        self.assertEqual(dialogs[-1], ("Marcus", "A line visible in the tray"))
        self.assertEqual(controller.history.snapshot()[0].character, "Marcus")
        self.assertEqual(
            controller.history.snapshot()[0].text,
            "A line visible in the tray",
        )

    def test_controller_offers_each_confident_unknown_speaker_once(self):
        offered = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            unknown_speaker_handler=offered.append,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.resolve_closest.return_value = None

        controller._dialog_observed("Selone", "First line")
        controller._dialog_observed("Selone", "Second line")
        controller._dialog_observed("Narrator", "Scene description")

        self.assertEqual(offered, ["Selone"])

    def test_controller_does_not_offer_configured_speaker(self):
        offered = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            unknown_speaker_handler=offered.append,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.resolve_closest.return_value = Mock()

        controller._dialog_observed("Kamuta", "A line")

        self.assertEqual(offered, [])

    def test_live_mode_uses_playback_safe_backend_threads(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.live_reader = Mock()
        controller.live_reader.toggle.side_effect = [True, False]
        controller.speech_backend = Mock()

        self.assertTrue(controller.toggle_live())
        self.assertFalse(controller.toggle_live())

        self.assertEqual(
            [
                call.args[0]
                for call in controller.speech_backend.set_live_mode_active.call_args_list
            ],
            [True, False],
        )

    def test_live_mode_checks_window_capture_before_starting(self):
        statuses = []
        errors = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            status_handler=statuses.append,
            error_handler=errors.append,
        )
        controller.live_reader = Mock(is_running=False)
        controller.capture_target = Mock()
        controller.capture_target.get_geometry.side_effect = RuntimeError(
            "Selected game window is not visible"
        )

        self.assertFalse(controller.toggle_live())

        controller.live_reader.toggle.assert_not_called()
        self.assertEqual(statuses, ["Live reading could not start"])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ScreenCaptureError)
        self.assertEqual(str(errors[0]), "Selected game window is not visible")

    def test_live_underflow_temporarily_disables_then_restores_prefetch(self):
        statuses = []
        now = [0.0]
        policy = AdaptiveSpeechBackpressure(
            normal_jobs=2,
            cooldown_seconds=10,
            clock=lambda: now[0],
        )
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            status_handler=statuses.append,
            speech_backpressure_factory=Mock(return_value=policy),
        )
        controller.live_reader = Mock()
        controller.speech_backend = Mock()
        controller.speech_backend.play.return_value = True
        controller.speech_backend.last_first_audio_ms = None
        controller.speech_backend.last_playback_underrun = True
        chunk = SpeechChunk(1, "Kamuta", "A line.")

        self.assertTrue(controller._play_live_chunk(chunk, "prepared"))
        self.assertEqual(controller.live_reader.max_speech_jobs, 1)
        self.assertIn("prefetch disabled", statuses[-1])

        controller.speech_backend.last_playback_underrun = False
        now[0] = 9.0
        controller._play_live_chunk(chunk, "prepared")
        self.assertEqual(controller.live_reader.max_speech_jobs, 1)

        now[0] = 10.0
        controller._play_live_chunk(chunk, "prepared")
        self.assertEqual(controller.live_reader.max_speech_jobs, 2)
        self.assertIn("prefetch restored", statuses[-1])

    def test_streaming_playback_records_reconstructed_first_pcm_timestamp(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.live_reader = Mock()
        controller.live_reader.wait_until_playable.return_value = True
        controller.speech_backend = Mock()
        controller.speech_backend.play.return_value = True
        controller.speech_backend.last_playback_underrun = False
        controller.speech_backend.last_first_audio_ms = 125.0
        chunk = SpeechChunk(1, "Kamuta", "A line.")

        controller._play_live_chunk(chunk, "prepared")

        timestamp = controller.live_reader.record_first_pcm.call_args.args[0]
        self.assertIsInstance(timestamp, float)

    def test_controller_primes_a_live_voice_as_soon_as_its_name_is_observed(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.speech_backend = Mock()
        controller.speech_executor = Mock()
        future = Mock()
        controller.speech_executor.submit.return_value = future

        controller._dialog_observed("Kamuta", "A partial line")
        controller._dialog_observed("Kamuta", "A partial line still appearing")

        controller.speech_executor.submit.assert_called_once_with(
            controller.speech_backend.prime,
            "Kamuta",
        )
        future.add_done_callback.assert_called_once_with(
            controller._voice_prime_finished
        )

    def test_controller_primes_one_likely_chapter_voice_per_observation(self):
        preloader = Mock()
        preloader.recommend.return_value = ("Fatutu", "Selone")
        registry = CharacterVoiceRegistry(
            [
                CharacterVoice("Fatutu", "fatutu"),
                CharacterVoice("Selone", "selone"),
            ]
        )
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
        )
        controller.voice_router = Mock(registry=registry)
        controller.speech_backend = Mock()
        controller.speech_executor = Mock()
        future = Mock()
        controller.speech_executor.submit.return_value = future

        controller._dialog_observed("Kamuta", "These old ones are enough")

        preloader.recommend.assert_called_once_with(
            "Kamuta", "These old ones are enough"
        )
        controller.speech_executor.submit.assert_called_once_with(
            controller.speech_backend.prime,
            "Fatutu",
        )

    def test_controller_does_not_prime_unknown_speaker_as_narrator(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.voice_router = Mock(registry=CharacterVoiceRegistry())
        controller.speech_backend = Mock()
        controller.speech_executor = Mock()

        self.assertFalse(controller._prime_observed_voice("Unknown NPC"))

        controller.speech_executor.submit.assert_not_called()

    def test_controller_reports_uncertain_ocr_confidence(self):
        dialogs = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            dialog_handler=lambda character, text: dialogs.append((character, text)),
        )
        controller.live_reader = Mock()

        controller._ocr_uncertain(
            OCRResult("Marcus", "Possibly incorrect", 42.4, "balanced", 3),
            60,
        )

        self.assertEqual(dialogs[-1][0], "OCR uncertain")
        self.assertIn("42% (requires 60%)", dialogs[-1][1])
        controller.live_reader.clear_queue.assert_called_once_with()

    def test_focus_loss_pauses_capture_without_suppressing_live_speech(self):
        statuses = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            status_handler=statuses.append,
        )
        controller.live_reader = Mock()

        controller._capture_state_changed(False, 1.0)
        controller._capture_state_changed(False, 1.0)
        controller._capture_state_changed(True, 0.2)

        controller.live_reader.clear_queue.assert_not_called()
        self.assertEqual(
            statuses,
            [
                "Game focus lost; live capture and auto advance paused",
                "Game focus restored; live reading resumed",
            ],
        )

    def test_auto_advance_pauses_when_ocr_detects_a_choice_menu(self):
        statuses = []
        controller = AppController(
            AppSettings(auto_advance_enabled=True),
            status_handler=statuses.append,
        )
        controller.last_diagnostic = DiagnosticSnapshot(
            None,
            text="Ask about the island Leave quietly",
            choice_detected=True,
        )

        with patch("vntts.controller.DialogueAdvancer") as advancer:
            advanced = controller._auto_advance_dialog()

        self.assertFalse(advanced)
        advancer.assert_not_called()
        self.assertIn("choice menu detected", statuses[-1])

    def test_controller_sets_coqui_acceptance_for_approved_xtts_use(self):
        tts = Mock()
        tts_factory = Mock(return_value=tts)
        voice_router = Mock()
        model_assets = Mock()
        settings = AppSettings(
            tts_model="tts_models/multilingual/multi-dataset/xtts_v2",
            xtts_terms_accepted=True,
            speech_backend="coqui-xtts",
        )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "vntts.controller.initialize_voice_router", return_value=voice_router
            ),
            patch(
                "vntts.controller.ThreadPoolExecutor",
                side_effect=[Mock(), Mock(), Mock(), Mock()],
            ),
            patch("vntts.controller.LiveDialogReader", return_value=Mock()),
            patch("vntts.controller.create_dialog_read_scheduler", return_value=Mock()),
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
