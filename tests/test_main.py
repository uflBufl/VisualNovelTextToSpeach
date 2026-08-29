import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import numpy as np
from PIL import Image, ImageDraw
from vntts_artifacts.hashing import text_sha256

from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import PreparedLiveChunkRoutes
from vntts.diagnostics import DiagnosticSnapshot
from vntts.generated_audio import (
    AudioRouteTrace,
    GeneratedAudioFallbackBackend,
    GeneratedAudioRoute,
    LiveFallbackDecision,
    LiveFallbackRoute,
    LiveTTSRoute,
    PlaybackOutcome,
    PlaybackStatus,
    PreparedGeneratedAudio,
    PreparedSourceAudioPassThrough,
    SourceAudioRoute,
)
from vntts.live import AdaptiveSpeechBackpressure, SpeechChunk
from vntts.live_sequence import (
    LiveSequenceChapter,
    LiveSequenceEvent,
    LiveSequencePlan,
    StoryCursorState,
)
from vntts.main import (
    AppController,
    CapturedDialogFrame,
    OCRError,
    OCRUncertainError,
    ScreenCaptureError,
    capture_dialog,
    create_dialog_read_scheduler,
    create_screenshot_path,
    dialog_glyphs_visible,
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
from vntts.playback import PreparedPlayback
from vntts.services.tts_engine import AudioPlaybackError, TTSSynthesisError
from vntts.settings import AppSettings
from vntts.speech_backend import SpeechBackendCapabilities
from vntts.support import GenerationTimelineLog
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class StubTypedPlaybackBackend:
    name = "typed-test"

    def __init__(self, outcome=None, prepared=None):
        self.outcome = outcome or PlaybackOutcome(
            PlaybackStatus.COMPLETED,
            10.0,
            first_audio_ms=1.0,
            audio_source="live:typed-test",
        )
        self.prepared = prepared or PreparedPlayback(
            SimpleNamespace(voice_key="rhiannon-v2"),
            5.0,
            None,
            "fresh-generation",
            "moss-tts:fresh-generation",
        )

    def prepare_playback(self, _character, _text):
        return self.prepared

    def play_prepared(self, _prepared, *, playback_guard=None):
        if playback_guard is not None and not playback_guard():
            return PlaybackOutcome(PlaybackStatus.INTERRUPTED, None)
        return self.outcome

    def play_route(self, _route, *, playback_guard=None):
        return self.play_prepared(None, playback_guard=playback_guard)


class RecordingAnnouncementBackend:
    name = "typed-announcement"

    def __init__(self, outcomes=None):
        self.prepare_calls = []
        self.play_calls = []
        self.outcomes = list(outcomes or ())

    def prepare_playback(self, character, text):
        self.prepare_calls.append((character, text))
        return PreparedPlayback(
            SimpleNamespace(
                voice_key=character.casefold(),
                character=character,
                text=text,
            ),
            2.0,
            None,
            "fresh-generation",
            "live:typed-announcement",
        )

    def play_prepared(self, prepared, *, playback_guard=None):
        self.play_calls.append((prepared.payload.character, prepared.payload.text))
        if playback_guard is not None and not playback_guard():
            return PlaybackOutcome(PlaybackStatus.INTERRUPTED, None)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return PlaybackOutcome(
            PlaybackStatus.COMPLETED,
            4.0,
            first_audio_ms=1.0,
            audio_source="live:typed-announcement",
        )


class FailingAnnouncementPrepareBackend(RecordingAnnouncementBackend):
    def prepare_playback(self, character, text):
        if character == "Narrator":
            raise RuntimeError("announcement preparation crashed")
        return super().prepare_playback(character, text)


def stub_route_trace(source, line_id=None):
    return AudioRouteTrace(
        None,
        source,
        "exact",
        None,
        None,
        line_id,
        "verified",
    )


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

    def test_dialog_fingerprint_ignores_dynamic_background_behind_same_text(self):
        first = Image.new("RGB", (1200, 240), "#202020")
        second = Image.new("RGB", (1200, 240), "#303030")
        first_draw = ImageDraw.Draw(first)
        second_draw = ImageDraw.Draw(second)
        for x in range(0, first.width, 80):
            first_draw.rectangle(
                (x, 0, x + 30, first.height),
                fill=(70 + (x // 80) % 3 * 20, 50, 90),
            )
            shifted_x = (x + 45) % first.width
            second_draw.rectangle(
                (shifted_x, 0, shifted_x + 30, second.height),
                fill=(90, 70 + (x // 80) % 3 * 20, 50),
            )
        text = "Alright, that makes five."
        first_draw.text((60, 100), text, fill="white")
        second_draw.text((60, 100), text, fill="white")

        self.assertEqual(
            fingerprint_dialog_frame(CapturedDialogFrame(first, 0)),
            fingerprint_dialog_frame(CapturedDialogFrame(second, 0)),
        )

    def test_dialog_fingerprint_changes_when_only_the_speaker_changes(self):
        first = Image.new("RGB", (1200, 240), "#202020")
        second = first.copy()
        first_draw = ImageDraw.Draw(first)
        second_draw = ImageDraw.Draw(second)
        first_draw.text((60, 35), "Rhiannon", fill="#d0d0d0")
        second_draw.text((60, 35), "Vertin", fill="#d0d0d0")
        for draw in (first_draw, second_draw):
            draw.text((60, 130), "The same dialogue.", fill="white")

        self.assertNotEqual(
            fingerprint_dialog_frame(CapturedDialogFrame(first, 0)),
            fingerprint_dialog_frame(CapturedDialogFrame(second, 0)),
        )

    def test_dialog_glyph_presence_accepts_ellipsis_and_rejects_empty_crop(self):
        empty = Image.new("RGB", (1200, 240), "#202020")
        ellipsis = empty.copy()
        ImageDraw.Draw(ellipsis).text((60, 100), "...", fill="white")

        self.assertFalse(dialog_glyphs_visible(CapturedDialogFrame(empty, 0)))
        self.assertTrue(dialog_glyphs_visible(CapturedDialogFrame(ellipsis, 0)))

    def test_dialog_glyph_presence_rejects_overexposed_crop(self):
        popup = Image.new("RGB", (1200, 240), "white")

        self.assertFalse(dialog_glyphs_visible(CapturedDialogFrame(popup, 0)))

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

    def test_artifact_audio_policy_waits_for_one_exact_complete_dialogue(self):
        routed = AppController(AppSettings(audio_source_policy="prefer-game-audio"))
        live = AppController(AppSettings(audio_source_policy="live-tts-only"))

        self.assertTrue(
            routed._get_live_configuration()["tracker_options"][
                "complete_dialogue_only"
            ]
        )
        self.assertFalse(
            live._get_live_configuration()["tracker_options"]["complete_dialogue_only"]
        )

    def test_artifact_policy_expands_unique_indexed_prefix_before_routing(self):
        controller = AppController(
            AppSettings(audio_source_policy="prefer-generated"),
            tts_factory=Mock(),
        )
        live_backend = Mock(
            name="pocket-tts",
            capabilities=SpeechBackendCapabilities(True, True, False, False),
        )
        live_backend.name = "pocket-tts"
        library = Mock()
        wrapper = GeneratedAudioFallbackBackend(
            live_backend,
            library,
            controller.chapter_voice_preloader,
            audio_output=Mock(),
        )
        controller.speech_backend = wrapper
        line = SimpleNamespace(text="The complete generated dialogue.")
        controller.chapter_voice_preloader.resolve_unique_prefix = Mock(
            return_value=line
        )

        configuration = controller._get_live_configuration()
        resolver = configuration["tracker_options"]["early_dialogue_resolver"]

        self.assertEqual(
            resolver("Rhiannon", "The complete generated"),
            line.text,
        )
        controller.chapter_voice_preloader.resolve_unique_prefix.assert_called_once_with(
            "Rhiannon",
            "The complete generated",
        )

    def test_unique_story_text_recovers_ocr_lost_speaker_before_routing(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.chapter_voice_preloader.canonical_speaker = Mock(
            return_value="Narrator"
        )
        controller.chapter_voice_preloader.resolve_unique_prefix_by_text = Mock(
            return_value=SimpleNamespace(speaker="Hotelier")
        )

        character = controller._canonical_observed_character(
            "Narrator",
            "You know, we once had a guest who insisted on bringing his horse",
        )

        self.assertEqual(character, "Hotelier")

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
                AppSettings(
                    speech_backend="coqui-xtts",
                    warm_up_voices=False,
                ),
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

    def test_isolated_worker_startup_receives_controller_cancellation(self):
        backend = Mock()
        backend.registry = Mock()
        backend.narrator_speaker = "Pocket TTS default"
        backend.capabilities.concurrent_prepare_and_play = False
        received = {}

        def pocket_factory(registry, **options):
            received["registry"] = registry
            received.update(options)
            return backend

        pocket_factory.supports_startup_cancellation = True
        registry = Mock()
        with (
            patch("vntts.controller.initialize_voice_registry", return_value=registry),
            patch("vntts.controller.ThreadPoolExecutor", return_value=Mock()),
            patch("vntts.controller.LiveDialogReader", return_value=Mock()),
            patch("vntts.controller.create_dialog_read_scheduler", return_value=Mock()),
        ):
            controller = AppController(
                AppSettings(speech_backend="pocket-tts"),
                pocket_backend_factory=pocket_factory,
                model_asset_manager_factory=Mock(),
            )

            self.assertTrue(controller.start())

        self.assertIs(received["registry"], registry)
        self.assertIs(received["startup_cancellation"], controller.shutdown_requested)
        controller.shutdown()

    def test_controller_loads_moss_with_model_language_and_huggingface_cache(self):
        backend = Mock()
        backend.registry = Mock()
        backend.narrator_speaker = "MOSS reference voice"
        backend.capabilities.concurrent_prepare_and_play = False
        moss_factory = Mock(return_value=backend)
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
                AppSettings(
                    speech_backend="moss-tts",
                    tts_model="local/moss-int8",
                    tts_language="en",
                    tts_speaker_wav="matilda.wav",
                ),
                tts_factory=tts_factory,
                moss_backend_factory=moss_factory,
                model_asset_manager_factory=Mock(return_value=model_assets),
                status_handler=statuses.append,
            )

            self.assertTrue(controller.start())

        self.assertIn("Loading MOSS-TTS...", statuses)
        tts_factory.assert_not_called()
        model_assets.configure_huggingface_environment.assert_called_once_with()
        moss_factory.assert_called_once_with(
            registry,
            narrator_reference="matilda.wav",
            volume=1.0,
            model_name="local/moss-int8",
            language="en",
            generation_profile="stable",
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
        live_hotkey_callback = listen_for_hotkeys.call_args.args[8]
        live_hotkey_callback()
        controller.toggle_live.assert_called_once_with()
        controller.shutdown.assert_called_once_with()

    def test_main_handles_keyboard_interrupt_without_traceback(self):
        settings = AppSettings()
        controller = Mock()
        controller.start.return_value = True
        errors = io.StringIO()

        with (
            redirect_stderr(errors),
            patch("vntts.main.load_app_settings", return_value=settings),
            patch("vntts.main.AppController", return_value=controller),
            patch(
                "vntts.main.listen_for_hotkeys",
                side_effect=KeyboardInterrupt,
            ),
        ):
            result = main()

        self.assertEqual(result, 130)
        self.assertEqual(errors.getvalue(), "")
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

    def test_controller_preview_uses_live_backend_rendering_boundary(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.voice_router = Mock()
        controller.speech_backend = Mock()

        controller._preview_voice("Marcus", "Preview this.")

        controller.speech_backend.speak.assert_called_once_with(
            "Marcus",
            "Preview this.",
        )
        controller.voice_router.speak.assert_not_called()

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

    def test_controller_lists_pocket_presets_and_assigns_one_immediately(self):
        controller = AppController(
            AppSettings(speech_backend="pocket-tts"),
            tts_factory=Mock(),
        )
        controller.live_reader = Mock(is_running=False)
        controller.speech_executor = Mock()
        controller.voice_router = Mock(registry=CharacterVoiceRegistry())

        choices = controller.available_voice_choices()
        updated = controller.assign_voice("Selone", "preset:alba")

        self.assertIn("preset:alba", [choice.id for choice in choices])
        self.assertEqual(updated.voice_assignments, {"Selone": "preset:alba"})
        self.assertEqual(
            controller.voice_router.registry.resolve("Selone").speaker,
            "alba",
        )
        self.assertFalse(controller._offer_unknown_speaker_mapping("Selone"))

    def test_narrator_fallback_voice_and_force_live_are_independent(self):
        controller = AppController(
            AppSettings(speech_backend="pocket-tts"),
            tts_factory=Mock(),
        )
        controller.live_reader = Mock(is_running=False)
        controller.voice_router = Mock(registry=CharacterVoiceRegistry())
        controller.voice_router.audio_cache = Mock()

        assigned = controller.assign_voice("Narrator", "preset:alba")
        self.assertFalse(controller._has_manual_voice_override("Narrator"))
        forced = controller.set_force_live_narrator(True)
        self.assertTrue(controller._has_manual_voice_override("Narrator"))
        generated_first = controller.set_force_live_narrator(False)
        self.assertFalse(controller._has_manual_voice_override("Narrator"))
        restored = controller.clear_voice_assignment("Narrator")

        self.assertEqual(assigned.voice_assignments, {"Narrator": "preset:alba"})
        self.assertFalse(assigned.force_live_narrator)
        self.assertTrue(forced.force_live_narrator)
        self.assertFalse(generated_first.force_live_narrator)
        self.assertEqual(restored.voice_assignments, {})
        self.assertFalse(restored.force_live_narrator)
        self.assertNotIn("narrator", controller.voice_router.registry.assignments)

    def test_controller_previews_a_catalog_choice_on_the_speech_executor(self):
        controller = AppController(
            AppSettings(speech_backend="pocket-tts"),
            tts_factory=Mock(),
        )
        controller.live_reader = Mock(is_running=False)
        controller.speech_executor = Mock()
        controller.voice_router = Mock(registry=CharacterVoiceRegistry())

        result = controller.preview_voice_choice("preset:marius", " Hello. ")

        self.assertIs(result, controller.speech_executor.submit.return_value)
        submitted = controller.speech_executor.submit.call_args.args
        self.assertEqual(submitted[0], controller._preview_voice_choice)
        self.assertEqual(submitted[1].id, "preset:marius")
        self.assertEqual(submitted[2], "Hello.")

    def test_controller_stops_only_the_loaded_preview_backend(self):
        controller = AppController(
            AppSettings(speech_backend="pocket-tts"),
            tts_factory=Mock(),
        )
        controller.live_reader = Mock(is_running=False)
        controller.speech_backend = Mock()

        self.assertTrue(controller.stop_voice_preview())

        controller.speech_backend.stop.assert_called_once_with()

    def test_applying_settings_updates_loaded_speech_controls(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.tts = Mock()

        controller.apply_settings(
            AppSettings(output_volume_percent=35, speech_rate_percent=125)
        )

        controller.tts.set_volume.assert_called_once_with(0.35)
        controller.tts.set_speed.assert_called_once_with(1.25)

    def test_applying_settings_keeps_loaded_backend_identity_until_restart(self):
        controller = AppController(
            AppSettings(speech_backend="pocket-tts", tts_model="pocket-tts"),
            tts_factory=Mock(),
        )
        loaded_backend = Mock(name="loaded-pocket-backend")
        controller.speech_backend = loaded_backend

        controller.apply_settings(
            AppSettings(
                speech_backend="moss-tts",
                tts_model="local-moss",
                tts_language="English",
                output_volume_percent=35,
            )
        )

        self.assertIs(controller.speech_backend, loaded_backend)
        self.assertEqual(controller.settings.speech_backend, "pocket-tts")
        self.assertEqual(controller.settings.tts_model, "pocket-tts")
        self.assertIsNone(controller.settings.tts_language)
        self.assertEqual(controller.settings.output_volume_percent, 35)

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

    def test_sequence_shadow_observes_canonical_line_without_changing_speech(self):
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "line_id": "reverse1999:1:1",
                        "speaker_name": "Rhiannon",
                        "text": "Known canonical line.",
                        "text_sha256": text_sha256("Known canonical line."),
                    }
                ]
            }
        )
        event = LiveSequenceEvent(
            "event-1",
            "1",
            1,
            "speech",
            "terminal",
            (),
            "reverse1999:1:1",
        )
        plan = LiveSequencePlan(
            Path("plan.json"),
            "reverse1999",
            "test",
            "1",
            Path("story.jsonl"),
            "1" * 64,
            "2" * 64,
            (LiveSequenceChapter("1", ("event-1",), ("event-1",)),),
            {"event-1": event},
            {"reverse1999:1:1": "event-1"},
        )
        pipeline = []
        controller = AppController(
            AppSettings(
                story_index="story.jsonl",
                live_sequence_plan="plan.json",
                live_sequence_mode="shadow",
            ),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
            live_sequence_plan_factory=Mock(return_value=plan),
            pipeline_event_handler=lambda *args, **kwargs: pipeline.append(
                (args, kwargs)
            ),
        )

        accepted = controller._dialog_observed(
            "Rhiannon",
            "Known canonical line.",
        )

        self.assertTrue(accepted)
        self.assertEqual(controller.story_cursor.state, StoryCursorState.LOCKED)
        self.assertEqual(controller.story_cursor.current_event_id, "event-1")
        self.assertEqual(pipeline[-1][0][0], "sequence-shadow")
        self.assertEqual(pipeline[-1][1]["line_id"], "reverse1999:1:1")

    def test_sequence_audio_manual_replaces_ocr_with_checksum_bound_line(self):
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "line_id": "reverse1999:1:1",
                        "speaker_name": "Rhiannon",
                        "text": "Known canonical line.",
                        "text_sha256": text_sha256("Known canonical line."),
                    }
                ]
            }
        )
        event = LiveSequenceEvent(
            "event-1",
            "1",
            1,
            "speech",
            "terminal",
            (),
            "reverse1999:1:1",
        )
        plan = LiveSequencePlan(
            Path("plan.json"),
            "reverse1999",
            "test",
            "1",
            Path("story.jsonl"),
            "1" * 64,
            "2" * 64,
            (LiveSequenceChapter("1", ("event-1",), ("event-1",)),),
            {"event-1": event},
            {"reverse1999:1:1": "event-1"},
        )
        pipeline = []
        controller = AppController(
            AppSettings(
                story_index="story.jsonl",
                live_sequence_plan="plan.json",
                live_sequence_mode="audio-manual",
                auto_advance_enabled=True,
            ),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
            live_sequence_plan_factory=Mock(return_value=plan),
            pipeline_event_handler=lambda *args, **kwargs: pipeline.append(
                (args, kwargs)
            ),
        )

        routed = controller._dialog_observed(
            "Rhiannon",
            "Known canonical line",
        )

        self.assertEqual(routed, ("Rhiannon", "Known canonical line."))
        self.assertEqual(controller.story_cursor.state, StoryCursorState.LOCKED)
        self.assertEqual(pipeline[-1][0][0], "sequence-audio-manual")
        self.assertIsNone(pipeline[-1][1]["previous_event_id"])
        unprepared_backend = Mock()
        controller.speech_backend = unprepared_backend
        status = controller.get_live_sequence_status()
        unprepared_backend.prepare_route.assert_not_called()
        self.assertEqual(status.expected_audio_route, "Live TTS")
        self.assertEqual(status.actual_audio_route, "-")
        self.assertIn("OCR idle", status.ocr_activity)
        controller.last_audio_route_trace = stub_route_trace(
            "generated-audio",
            "reverse1999:1:1",
        )
        self.assertEqual(
            controller.get_live_sequence_status().actual_audio_route,
            "generated-audio",
        )
        controller.last_audio_route_trace = stub_route_trace(
            "generated-audio",
            "reverse1999:other:9",
        )
        self.assertEqual(controller.get_live_sequence_status().actual_audio_route, "-")
        self.assertIsNone(controller._live_auto_advance_callback())
        with patch("vntts.controller.DialogueAdvancer") as advancer:
            self.assertFalse(controller._auto_advance_dialog())
        advancer.assert_not_called()
        live_backend = Mock()
        live_backend.name = "typed-test"
        live_backend.capabilities = SpeechBackendCapabilities(True, False, True)
        live_backend.prepare_playback.return_value = PreparedPlayback(
            SimpleNamespace(),
            1.0,
            None,
            "fresh-generation",
            "live:typed-test",
        )
        fallback = GeneratedAudioFallbackBackend(
            live_backend,
            None,
            preloader,
            audio_output=Mock(),
        )

        route = fallback.prepare_route(*routed)

        self.assertIsInstance(route, LiveTTSRoute)
        self.assertEqual(route.trace.line_id, "reverse1999:1:1")
        live_backend.prepare_playback.assert_called_once_with(
            "Rhiannon",
            "Known canonical line.",
        )

    def test_sequence_audio_manual_rejects_unexpected_known_line_and_stays_closed(self):
        rows = [
            {
                "chapter": "1",
                "sequence": sequence,
                "line_id": f"reverse1999:1:{sequence}",
                "speaker_name": "Rhiannon",
                "text": text,
                "text_sha256": text_sha256(text),
            }
            for sequence, text in (
                (1, "Expected line."),
                (9, "Unrelated known line."),
            )
        ]
        preloader = ChapterVoicePreloader.from_document({"dialogue": rows})
        events = {
            f"event-{sequence}": LiveSequenceEvent(
                f"event-{sequence}",
                "1",
                sequence,
                "speech",
                "terminal",
                (),
                f"reverse1999:1:{sequence}",
            )
            for sequence in (1, 9)
        }
        plan = LiveSequencePlan(
            Path("plan.json"),
            "reverse1999",
            "test",
            "1",
            Path("story.jsonl"),
            "1" * 64,
            "2" * 64,
            (
                LiveSequenceChapter(
                    "1",
                    ("event-1", "event-9"),
                    ("event-1", "event-9"),
                ),
            ),
            events,
            {f"reverse1999:1:{sequence}": f"event-{sequence}" for sequence in (1, 9)},
        )
        controller = AppController(
            AppSettings(
                story_index="story.jsonl",
                live_sequence_plan="plan.json",
                live_sequence_mode="audio-manual",
            ),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
            live_sequence_plan_factory=Mock(return_value=plan),
        )

        self.assertEqual(
            controller._dialog_observed("Rhiannon", "Expected line."),
            ("Rhiannon", "Expected line."),
        )
        self.assertFalse(
            controller._dialog_observed("Rhiannon", "Unrelated known line.")
        )
        self.assertEqual(
            controller.story_cursor.state,
            StoryCursorState.DESYNCHRONIZED,
        )
        self.assertFalse(controller._dialog_observed("Rhiannon", "Expected line."))

    def test_sequence_audio_manual_routes_stable_successor_without_ocr_text(self):
        rows = [
            {
                "chapter": "1",
                "sequence": sequence,
                "line_id": f"reverse1999:1:{sequence}",
                "speaker_name": speaker,
                "text": text,
                "text_sha256": text_sha256(text),
            }
            for sequence, speaker, text in (
                (1, "Rhiannon", "First canonical line."),
                (3, "Hotelier", "Second canonical line."),
            )
        ]
        preloader = ChapterVoicePreloader.from_document({"dialogue": rows})
        events = {
            "event-1": LiveSequenceEvent(
                "event-1",
                "1",
                1,
                "speech",
                "automatic",
                ("event-transition",),
                "reverse1999:1:1",
            ),
            "event-transition": LiveSequenceEvent(
                "event-transition",
                "1",
                2,
                "transition",
                "passive",
                ("event-3",),
                None,
            ),
            "event-3": LiveSequenceEvent(
                "event-3",
                "1",
                3,
                "speech",
                "terminal",
                (),
                "reverse1999:1:3",
            ),
        }
        plan = LiveSequencePlan(
            Path("plan.json"),
            "reverse1999",
            "test",
            "1",
            Path("story.jsonl"),
            "1" * 64,
            "2" * 64,
            (
                LiveSequenceChapter(
                    "1",
                    ("event-1",),
                    ("event-1", "event-transition", "event-3"),
                ),
            ),
            events,
            {
                "reverse1999:1:1": "event-1",
                "reverse1999:1:3": "event-3",
            },
        )
        controller = AppController(
            AppSettings(
                story_index="story.jsonl",
                live_sequence_plan="plan.json",
                live_sequence_mode="audio-manual",
            ),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
            live_sequence_plan_factory=Mock(return_value=plan),
        )
        first = controller._dialog_observed("Rhiannon", "First canonical line.")
        chunk = SpeechChunk(1, *first)

        event_id = controller._begin_sequence_playback(chunk)

        self.assertEqual(event_id, "event-1")
        self.assertEqual(controller.story_cursor.state, StoryCursorState.PLAYING)
        self.assertFalse(controller._stable_live_frame_route("new", True))
        controller._finish_sequence_playback(
            event_id,
            PlaybackOutcome(PlaybackStatus.COMPLETED, 1.0),
        )
        self.assertFalse(controller._stable_live_frame_route("new", False))
        self.assertEqual(
            controller._stable_live_frame_route("new", True),
            ("Hotelier", "Second canonical line."),
        )
        self.assertEqual(controller.story_cursor.current_event_id, "event-3")

    def test_sequence_audio_manual_failed_playback_keeps_current_event_closed(self):
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "line_id": "reverse1999:1:1",
                        "speaker_name": "Rhiannon",
                        "text": "Known canonical line.",
                        "text_sha256": text_sha256("Known canonical line."),
                    }
                ]
            }
        )
        event = LiveSequenceEvent(
            "event-1",
            "1",
            1,
            "speech",
            "terminal",
            (),
            "reverse1999:1:1",
        )
        plan = LiveSequencePlan(
            Path("plan.json"),
            "reverse1999",
            "test",
            "1",
            Path("story.jsonl"),
            "1" * 64,
            "2" * 64,
            (LiveSequenceChapter("1", ("event-1",), ("event-1",)),),
            {"event-1": event},
            {"reverse1999:1:1": "event-1"},
        )
        controller = AppController(
            AppSettings(
                story_index="story.jsonl",
                live_sequence_plan="plan.json",
                live_sequence_mode="audio-manual",
            ),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
            live_sequence_plan_factory=Mock(return_value=plan),
        )
        first = controller._dialog_observed("Rhiannon", "Known canonical line.")
        event_id = controller._begin_sequence_playback(SpeechChunk(1, *first))

        controller._finish_sequence_playback(
            event_id,
            PlaybackOutcome(PlaybackStatus.FAILED, None, error="failed"),
        )

        self.assertEqual(controller.story_cursor.current_event_id, "event-1")
        self.assertEqual(controller.story_cursor.reason, "playback-failed")
        self.assertFalse(controller._stable_live_frame_route("new", True))
        status = controller.get_live_sequence_status()
        self.assertTrue(status.recovery_required)
        self.assertIn("Playback failed", status.guidance)

    def test_sequence_manual_resync_lists_visible_events_and_handles_stopped_or_running(
        self,
    ):
        text = "Known canonical line."
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "line_id": "reverse1999:1:1",
                        "speaker_name": "Rhiannon",
                        "text": text,
                        "text_sha256": text_sha256(text),
                    }
                ]
            }
        )
        events = {
            "event-1": LiveSequenceEvent(
                "event-1",
                "1",
                1,
                "speech",
                "automatic",
                ("event-2",),
                "reverse1999:1:1",
            ),
            "event-2": LiveSequenceEvent(
                "event-2",
                "1",
                2,
                "silent",
                "terminal",
                (),
                None,
            ),
        }
        plan = LiveSequencePlan(
            Path("plan.json"),
            "reverse1999",
            "test",
            "1",
            Path("story.jsonl"),
            "1" * 64,
            "2" * 64,
            (LiveSequenceChapter("1", ("event-1",), ("event-1", "event-2")),),
            events,
            {"reverse1999:1:1": "event-1"},
        )
        dialogs = []
        statuses = []
        sequence_statuses = []
        controller = AppController(
            AppSettings(
                story_index="story.jsonl",
                live_sequence_plan="plan.json",
                live_sequence_mode="audio-manual",
            ),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
            live_sequence_plan_factory=Mock(return_value=plan),
            dialog_handler=lambda *args: dialogs.append(args),
            status_handler=statuses.append,
            sequence_status_handler=sequence_statuses.append,
        )
        stopped_reader = Mock(is_running=False)
        controller.live_reader = stopped_reader

        options = controller.live_sequence_anchor_options()

        self.assertEqual(
            [event_id for _label, event_id in options], ["event-1", "event-2"]
        )
        self.assertIn("Rhiannon: Known canonical line.", options[0][0])
        self.assertIn("Silent: silent dialogue", options[1][0])
        self.assertFalse(controller.resync_live_sequence("missing"))
        self.assertTrue(controller.resync_live_sequence("event-1"))
        self.assertEqual(controller.story_cursor.current_event_id, "event-1")
        self.assertTrue(controller.explicit_sequence_anchor_pending)
        self.assertEqual(sequence_statuses[-1].state, "locked")
        self.assertEqual(sequence_statuses[-1].event_id, "event-1")
        self.assertEqual(sequence_statuses[-1].speaker, "Rhiannon")
        self.assertEqual(preloader.current_match.chapter, "1")
        self.assertEqual(dialogs[-1], ("Rhiannon", text))
        stopped_reader.clear_queue.assert_not_called()

        running_reader = Mock(is_running=True)
        controller.live_reader = running_reader
        controller._enqueue_dialog = Mock(return_value=True)

        self.assertTrue(controller.resync_live_sequence("event-1"))

        running_reader.clear_queue.assert_called_once_with()
        running_reader.bind_current_frame_route.assert_called_once_with()
        controller._enqueue_dialog.assert_called_once_with("Rhiannon", text)
        self.assertIn("sequence 1", statuses[-1])

    def test_controller_identifies_live_scope_without_speaking_or_history(self):
        dialogs = []
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "line_id": "reverse1999:1:1",
                        "speaker_name": "Rhiannon",
                        "text": "A line visible in the game.",
                        "text_sha256": text_sha256("A line visible in the game."),
                    }
                ]
            }
        )
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            dialog_handler=lambda character, text: dialogs.append((character, text)),
            chapter_voice_preloader=preloader,
        )
        controller.live_reader = Mock(is_running=False)
        controller.voice_router = Mock()

        with patch(
            "vntts.controller.read_live_snapshot",
            return_value=("Rhiannon", "A line visible in the game."),
        ) as read_snapshot:
            identified = controller.identify_live_scope()

        self.assertTrue(identified)
        self.assertEqual(preloader.current_match.chapter, "1")
        self.assertEqual(preloader.current_match.sequence, 1)
        self.assertEqual(dialogs, [("Rhiannon", "A line visible in the game.")])
        self.assertEqual(controller.history.snapshot(), [])
        controller.voice_router.speak.assert_not_called()
        read_snapshot.assert_called_once()

    def test_controller_does_not_claim_scope_for_an_unmatched_dialog(self):
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "line_id": "reverse1999:1:1",
                        "speaker_name": "Rhiannon",
                        "text": "Known line.",
                        "text_sha256": text_sha256("Known line."),
                    }
                ]
            }
        )
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
        )
        controller.live_reader = Mock(is_running=False)
        controller.voice_router = Mock()

        with patch(
            "vntts.controller.read_live_snapshot",
            return_value=("Someone", "An unrelated line."),
        ):
            identified = controller.identify_live_scope()

        self.assertFalse(identified)
        self.assertIsNone(preloader.current_match)

    def test_controller_offers_each_confident_unknown_speaker_once(self):
        offered = []
        statuses = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            status_handler=statuses.append,
            dialog_handler=lambda _character, _text: None,
            unknown_speaker_handler=offered.append,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.resolve_closest.return_value = None
        controller.voice_router.registry.resolve.return_value = None

        controller._dialog_observed("Selone", "First line")
        controller._dialog_observed("Selone", "Second line")
        controller._dialog_observed("Narrator", "Scene description")

        self.assertEqual(offered, ["Selone"])
        self.assertIn("waiting for a voice choice", statuses[-1])

    def test_exact_unknown_label_uses_narrator_without_hiding_named_unknowns(self):
        offered = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            dialog_handler=lambda _character, _text: None,
            unknown_speaker_handler=offered.append,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.resolve_closest.return_value = None
        controller.voice_router.registry.resolve.return_value = None

        self.assertTrue(controller._dialog_observed("???", "Unattributed line"))
        self.assertFalse(controller._dialog_observed("Selone", "Named line"))

        self.assertEqual(offered, ["Selone"])

    def test_nearby_named_speaker_stays_unknown_at_backend_resolution_boundary(self):
        offered = []
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "speaker_name": "Selene",
                        "text": "This is not Selone.",
                    }
                ]
            }
        )
        preloader.recommend("Selene", "This is not Selone.")
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            dialog_handler=lambda _character, _text: None,
            unknown_speaker_handler=offered.append,
            chapter_voice_preloader=preloader,
        )
        controller.voice_router = SimpleNamespace(
            registry=CharacterVoiceRegistry([CharacterVoice("Selone", "selone-voice")])
        )
        controller.speech_backend = SimpleNamespace()

        self.assertEqual(controller.unresolved_live_speakers(), ("Selene",))
        self.assertFalse(controller._dialog_observed("Selene", "This is not Selone."))
        self.assertEqual(offered, ["Selene"])

        controller.allow_narrator_fallback("Selone")
        controller.reported_unknown_speakers.clear()
        controller.pending_unknown_speakers.clear()
        self.assertFalse(controller._dialog_observed("Selene", "This is not Selone."))
        self.assertEqual(offered, ["Selene", "Selene"])

    def test_controller_defers_unknown_voice_until_narrator_is_allowed(self):
        offered = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            dialog_handler=lambda _character, _text: None,
            unknown_speaker_handler=offered.append,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.resolve_closest.return_value = None
        controller.voice_router.registry.resolve.return_value = None

        self.assertFalse(controller._dialog_observed("Selone", "First line"))
        self.assertTrue(controller.allow_narrator_fallback("Selone"))
        self.assertTrue(controller._dialog_observed("Selone", "First line"))

        self.assertEqual(offered, ["Selone"])

    def test_controller_does_not_request_a_voice_for_original_game_audio(self):
        offered = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            dialog_handler=lambda _character, _text: None,
            unknown_speaker_handler=offered.append,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.resolve_closest.return_value = None
        controller.voice_router.registry.resolve.return_value = None
        controller.speech_backend = Mock()
        controller.speech_backend.will_use_source_audio.return_value = True

        self.assertTrue(controller._dialog_observed("Fledgling", "Coo..."))

        self.assertEqual(offered, [])

    def test_controller_does_not_offer_configured_speaker(self):
        offered = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            unknown_speaker_handler=offered.append,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.resolve.return_value = Mock()

        controller._dialog_observed("Kamuta", "A line")

        self.assertEqual(offered, [])

    def test_live_session_reports_an_unknown_speaker_again(self):
        offered = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            dialog_handler=lambda _character, _text: None,
            unknown_speaker_handler=offered.append,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.resolve_closest.return_value = None
        controller.voice_router.registry.resolve.return_value = None
        controller.live_reader = Mock()
        controller.live_reader.is_running = False
        controller.live_reader.toggle.return_value = True
        controller.speech_backend = Mock()

        controller._dialog_observed("Hotelier", "A one-time read.")
        controller.toggle_live()
        controller._dialog_observed("Hotelier", "The first live line.")

        self.assertEqual(offered, ["Hotelier", "Hotelier"])

    def test_live_voice_preflight_lists_only_named_speakers_needing_tts(self):
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "speaker_name": "???",
                        "text": "Unattributed",
                    },
                    {
                        "chapter": "1",
                        "sequence": 2,
                        "speaker_name": "Narrator",
                        "text": "Scene",
                    },
                    {
                        "chapter": "1",
                        "sequence": 3,
                        "speaker_name": "Marcus",
                        "text": "Mapped",
                    },
                    {
                        "chapter": "1",
                        "sequence": 4,
                        "speaker_name": "Fledgling",
                        "text": "Original only",
                    },
                    {
                        "chapter": "1",
                        "sequence": 5,
                        "speaker_name": "Selone",
                        "text": "Original first",
                    },
                    {
                        "chapter": "1",
                        "sequence": 6,
                        "speaker_name": "???",
                        "text": "Unattributed in scope",
                    },
                    {
                        "chapter": "1",
                        "sequence": 7,
                        "speaker_name": "Selone",
                        "text": "Needs TTS",
                    },
                    {
                        "chapter": "1",
                        "sequence": 8,
                        "speaker_name": "Hotelier",
                        "text": "Needs TTS too",
                    },
                    {
                        "chapter": "1",
                        "sequence": 9,
                        "speaker_name": "Distant speaker",
                        "text": "Outside lookahead",
                    },
                ]
            },
            lookahead_rows=4,
        )
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.resolve.return_value = None
        controller.speech_backend = Mock()
        controller.speech_backend.will_use_source_audio_in_live_mode.side_effect = (
            lambda _character, text: text in {"Original only", "Original first"}
        )

        self.assertIsNone(controller.unresolved_live_speakers())
        preloader.recommend("Selone", "Original first")
        self.assertEqual(
            controller.unresolved_live_speakers(),
            ("Selone", "Hotelier"),
        )

    def test_live_voice_preflight_accepts_an_authorized_exact_audio_route(self):
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "speaker_name": "Hotelier",
                        "text": "Welcome.",
                    }
                ]
            }
        )
        preloader.recommend("Hotelier", "Welcome.")
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.assignments = {}
        controller.voice_router.registry.resolve.return_value = None
        controller.speech_backend = Mock()
        controller.speech_backend.has_resolved_route_in_live_mode.return_value = True

        self.assertEqual(controller.unresolved_live_speakers(), ())
        controller.speech_backend.has_resolved_route_in_live_mode.assert_called_once_with(
            "Hotelier",
            "Welcome.",
        )

    def test_live_voice_preflight_narrator_approval_is_one_session_only(self):
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "speaker_name": "Selone",
                        "text": "Line",
                    }
                ]
            }
        )
        preloader.recommend("Selone", "Line")
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.assignments = {}
        controller.voice_router.registry.resolve.return_value = None
        controller.speech_backend = SimpleNamespace()
        controller.live_reader = Mock(is_running=False)
        controller.live_reader.toggle.return_value = True

        self.assertEqual(
            controller.approve_live_narrator_fallbacks(["Selone", "???"]),
            ("Selone",),
        )
        self.assertTrue(controller.toggle_live())
        self.assertFalse(controller._offer_unknown_speaker_mapping("Selone", "Line"))

        controller.live_reader.is_running = True
        controller.live_reader.toggle.return_value = False
        self.assertFalse(controller.toggle_live())
        self.assertTrue(controller._offer_unknown_speaker_mapping("Selone", "Line"))

        controller.reported_unknown_speakers.clear()
        controller.pending_unknown_speakers.clear()
        controller.live_reader.is_running = False
        controller.live_reader.toggle.return_value = True
        self.assertFalse(controller.toggle_live())
        self.assertEqual(controller.live_reader.toggle.call_count, 2)
        self.assertTrue(controller._offer_unknown_speaker_mapping("Selone", "Line"))

    def test_explicit_speaker_corpus_preflights_without_story_index(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "speakers.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "speakers": ["Rhiannon", "Hotelier", "Narrator", "???"],
                    }
                ),
                encoding="utf-8",
            )
            controller = AppController(
                AppSettings(live_speaker_corpus=str(path)),
                tts_factory=Mock(),
            )

        controller.voice_router = Mock()
        controller.voice_router.registry.assignments = {}
        controller.voice_router.registry.resolve.side_effect = lambda character: (
            object() if character == "Rhiannon" else None
        )
        controller.speech_backend = SimpleNamespace()

        self.assertEqual(controller.unresolved_live_speakers(), ("Hotelier",))

    def test_applying_settings_loads_a_new_explicit_speaker_corpus(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "speakers.json"
            path.write_text(
                json.dumps({"schema_version": 1, "speakers": ["Rhiannon", "Hotelier"]}),
                encoding="utf-8",
            )
            controller = AppController(AppSettings(), tts_factory=Mock())
            controller.voice_router = Mock()
            controller.voice_router.registry.assignments = {}
            controller.voice_router.registry.resolve.side_effect = lambda character: (
                object() if character == "Rhiannon" else None
            )
            controller.speech_backend = SimpleNamespace(name="typed-test")

            controller.apply_settings(AppSettings(live_speaker_corpus=str(path)))

            self.assertEqual(controller.unresolved_live_speakers(), ("Hotelier",))
            self.assertEqual(controller.live_speaker_corpus.path, path.resolve())
            self.assertIsNone(controller.live_speaker_corpus_error)

    def test_live_start_revalidates_the_bound_speaker_corpus(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "speakers.json"
            path.write_text(
                json.dumps({"schema_version": 1, "speakers": ["Rhiannon"]}),
                encoding="utf-8",
            )
            statuses = []
            controller = AppController(
                AppSettings(live_speaker_corpus=str(path)),
                tts_factory=Mock(),
                status_handler=statuses.append,
            )
            controller.live_reader = Mock(is_running=False)
            path.write_text(
                json.dumps({"schema_version": 1, "speakers": ["Hotelier"]}),
                encoding="utf-8",
            )

            self.assertFalse(controller.toggle_live())

        controller.live_reader.toggle.assert_not_called()
        self.assertIn("changed after settings were applied", statuses[-1])

    def test_settings_restart_routes_through_fresh_speaker_preflight(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "speakers.json"
            path.write_text(
                json.dumps({"schema_version": 1, "speakers": ["Rhiannon"]}),
                encoding="utf-8",
            )
            statuses = []
            controller = AppController(
                AppSettings(live_speaker_corpus=str(path)),
                tts_factory=Mock(),
                status_handler=statuses.append,
            )
            controller.live_reader = Mock(is_running=True)
            controller.live_reader.stop.side_effect = lambda: setattr(
                controller.live_reader, "is_running", False
            )

            controller.apply_settings(
                AppSettings(live_speaker_corpus=str(path.with_name("missing.json")))
            )

        controller.live_reader.start.assert_not_called()
        controller.live_reader.toggle.assert_not_called()
        self.assertIn("configured speaker corpus is invalid", statuses[-1])

    def test_invalid_explicit_speaker_corpus_blocks_live_start(self):
        statuses = []
        controller = AppController(
            AppSettings(live_speaker_corpus="missing-speakers.json"),
            tts_factory=Mock(),
            status_handler=statuses.append,
        )
        controller.live_reader = Mock(is_running=False)

        self.assertFalse(controller.toggle_live())
        controller.live_reader.toggle.assert_not_called()
        self.assertIn("configured speaker corpus is invalid", statuses[-1])

    def test_story_scope_takes_precedence_over_an_invalid_explicit_corpus(self):
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "speaker_name": "Rhiannon",
                        "text": "Line",
                    }
                ]
            }
        )
        preloader.recommend("Rhiannon", "Line")
        controller = AppController(
            AppSettings(live_speaker_corpus="missing-speakers.json"),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.assignments = {}
        controller.voice_router.registry.resolve.return_value = object()
        controller.speech_backend = SimpleNamespace()
        controller.live_reader = Mock(is_running=False)
        controller.live_reader.toggle.return_value = True

        self.assertTrue(controller.toggle_live())
        controller.live_reader.toggle.assert_called_once_with()

    def test_direct_live_toggle_requires_exact_fresh_scoped_approval(self):
        statuses = []
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "speaker_name": "Selone",
                        "text": "Line",
                    }
                ]
            }
        )
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            status_handler=statuses.append,
            chapter_voice_preloader=preloader,
        )
        controller.voice_router = Mock()
        controller.voice_router.registry.assignments = {}
        controller.voice_router.registry.resolve.return_value = None
        controller.speech_backend = SimpleNamespace()
        controller.live_reader = Mock(is_running=False)
        controller.live_reader.toggle.return_value = True

        self.assertFalse(controller.toggle_live())
        self.assertIn("read the current dialog once", statuses[-1])
        controller.live_reader.toggle.assert_not_called()

        preloader.recommend("Selone", "Line")
        controller.allow_narrator_fallback("Selone")
        self.assertFalse(controller.toggle_live())
        self.assertIn("explicitly approve Narrator for Selone", statuses[-1])
        controller.live_reader.toggle.assert_not_called()

        controller.approve_live_narrator_fallbacks(["Hotelier"])
        self.assertFalse(controller.toggle_live())
        self.assertEqual(controller.next_live_narrator_fallback_names, {})
        controller.live_reader.toggle.assert_not_called()

        controller.approve_live_narrator_fallbacks(["Selone"])
        self.assertTrue(controller.toggle_live())
        controller.live_reader.toggle.assert_called_once_with()

    def test_direct_live_toggle_rechecks_empty_scope_after_staged_approval(self):
        statuses = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            status_handler=statuses.append,
        )
        controller.live_reader = Mock(is_running=False)
        controller.live_reader.toggle.return_value = True
        controller.unresolved_live_speakers = Mock(side_effect=[("Selone",), (), ()])

        self.assertFalse(controller.toggle_live())
        controller.approve_live_narrator_fallbacks(["Selone"])

        self.assertFalse(controller.toggle_live())
        self.assertEqual(controller.next_live_narrator_fallback_names, {})
        self.assertIn("scope changed", statuses[-1])
        controller.live_reader.toggle.assert_not_called()

        self.assertTrue(controller.toggle_live())
        controller.live_reader.toggle.assert_called_once_with()

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
        controller.speech_backend = StubTypedPlaybackBackend(
            PlaybackOutcome(PlaybackStatus.COMPLETED, 10.0, underflowed=True)
        )
        prepared = controller.speech_backend.prepared
        chunk = SpeechChunk(1, "Kamuta", "A line.")

        self.assertTrue(controller._play_live_chunk(chunk, prepared))
        self.assertEqual(controller.live_reader.max_speech_jobs, 1)
        self.assertIn("prefetch disabled", statuses[-1])

        controller.speech_backend.outcome = PlaybackOutcome(
            PlaybackStatus.COMPLETED, 10.0
        )
        now[0] = 9.0
        controller._play_live_chunk(chunk, prepared)
        self.assertEqual(controller.live_reader.max_speech_jobs, 1)

        now[0] = 10.0
        controller._play_live_chunk(chunk, prepared)
        self.assertEqual(controller.live_reader.max_speech_jobs, 2)
        self.assertIn("prefetch restored", statuses[-1])

    def test_source_audio_without_completion_blocks_auto_advance_actionably(self):
        statuses = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            status_handler=statuses.append,
        )
        controller.live_reader = Mock()
        controller.live_reader.wait_until_playable.return_value = True
        controller.speech_backend = StubTypedPlaybackBackend(
            PlaybackOutcome(PlaybackStatus.PASSTHROUGH_UNOBSERVED, None)
        )
        chunk = SpeechChunk(7, "Rhiannon", "An original voiced line.")
        prepared = SourceAudioRoute(
            PreparedSourceAudioPassThrough(
                "reverse1999:1:2",
                "hash",
                "voice-7",
            ),
            stub_route_trace("game-source", "reverse1999:1:2"),
        )

        self.assertTrue(controller._play_live_chunk(chunk, prepared))

        controller.live_reader.block_auto_advance_for_generation.assert_called_once_with(
            7,
            ANY,
        )
        self.assertIn("completion timing is unavailable", statuses[-1])
        self.assertIn("advance manually", statuses[-1].casefold())

    def test_source_audio_with_completion_keeps_auto_advance_enabled(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.live_reader = Mock()
        controller.live_reader.wait_until_playable.return_value = True
        controller.speech_backend = StubTypedPlaybackBackend()
        prepared = SourceAudioRoute(
            PreparedSourceAudioPassThrough(
                "reverse1999:1:2",
                "hash",
                "voice-7",
                completion_seconds=2.5,
                completion_source="story-index",
            ),
            stub_route_trace("game-source", "reverse1999:1:2"),
        )

        self.assertTrue(
            controller._play_live_chunk(
                SpeechChunk(7, "Rhiannon", "An original voiced line."),
                prepared,
            )
        )

        controller.live_reader.block_auto_advance_for_generation.assert_not_called()
        controller.live_reader.seal_generation.assert_called_once_with(7)

    def test_generated_audio_completion_seals_late_ocr_suffixes(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.live_reader = Mock()
        controller.live_reader.wait_until_playable.return_value = True
        controller.speech_backend = StubTypedPlaybackBackend()
        prepared = GeneratedAudioRoute(
            PreparedGeneratedAudio("reverse1999:314601:41", "hash", Mock(), 48_000),
            stub_route_trace("generated-audio", "reverse1999:314601:41"),
        )

        self.assertTrue(
            controller._play_live_chunk(
                SpeechChunk(9, "Rhiannon", "I, erhm ..."),
                prepared,
            )
        )

        controller.live_reader.seal_generation.assert_called_once_with(9)

    def test_streaming_playback_records_reconstructed_first_pcm_timestamp(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.live_reader = Mock()
        controller.live_reader.wait_until_playable.return_value = True
        controller.speech_backend = StubTypedPlaybackBackend(
            PlaybackOutcome(
                PlaybackStatus.COMPLETED,
                20.0,
                first_audio_ms=125.0,
                audio_source="live:typed-test",
            )
        )
        chunk = SpeechChunk(1, "Kamuta", "A line.")

        controller._play_live_chunk(chunk, controller.speech_backend.prepared)

        timestamp = controller.live_reader.record_first_pcm.call_args.args[0]
        self.assertIsInstance(timestamp, float)

    def test_interrupted_typed_playback_blocks_auto_advance_and_does_not_seal(self):
        class InterruptedBackend:
            name = "typed-test"

            def play_route(self, _route, *, playback_guard=None):
                self.guard_value = playback_guard()
                return PlaybackOutcome(PlaybackStatus.INTERRUPTED, 5.0)

        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.live_reader = Mock()
        controller.live_reader.wait_until_playable.return_value = True
        controller.speech_backend = InterruptedBackend()
        route = GeneratedAudioRoute(
            PreparedGeneratedAudio(
                "game:1",
                "a" * 64,
                np.array([0.0], dtype=np.float32),
                24_000,
            ),
            AudioRouteTrace(
                None,
                "generated",
                "exact",
                None,
                None,
                "game:1",
                "generated-audio-entry-verified",
            ),
        )

        result = controller._play_live_chunk(
            SpeechChunk(4, "Rhiannon", "A line."), route
        )

        self.assertFalse(result)
        controller.live_reader.seal_generation.assert_not_called()
        controller.live_reader.block_auto_advance_for_generation.assert_called_once()

    def test_failed_typed_playback_blocks_auto_advance_before_error_surfaces(self):
        timeline_events = []

        class FailedBackend:
            name = "typed-test"

            def play_route(self, _route, *, playback_guard=None):
                self.guard_value = playback_guard()
                return PlaybackOutcome(
                    PlaybackStatus.FAILED,
                    5.0,
                    error="device failed",
                )

        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                timeline_events.append((stage, details))
            ),
        )
        controller.live_reader = Mock()
        controller.live_reader.wait_until_playable.return_value = True
        controller.speech_backend = FailedBackend()
        route = GeneratedAudioRoute(
            PreparedGeneratedAudio(
                "game:1",
                "a" * 64,
                np.array([0.0], dtype=np.float32),
                24_000,
            ),
            AudioRouteTrace(
                None,
                "generated",
                "exact",
                None,
                None,
                "game:1",
                "generated-audio-entry-verified",
            ),
        )

        with self.assertRaisesRegex(AudioPlaybackError, "device failed"):
            controller._play_live_chunk(SpeechChunk(4, "Rhiannon", "A line."), route)

        controller.live_reader.seal_generation.assert_not_called()
        controller.live_reader.block_auto_advance_for_generation.assert_called_once()
        outcome = next(
            event for event in timeline_events if event[0] == "playback-outcome"
        )
        self.assertEqual(outcome[1]["outcome"], "failed")

    def test_moss_safety_limit_is_visible_and_recorded_without_blocking_completion(
        self,
    ):
        statuses = []
        timeline_events = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            status_handler=statuses.append,
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                timeline_events.append((stage, generation, occurred_at, details))
            ),
        )
        controller.live_reader = Mock()
        controller.live_reader.wait_until_playable.return_value = True
        controller.speech_backend = StubTypedPlaybackBackend(
            PlaybackOutcome(
                PlaybackStatus.COMPLETED,
                20.0,
                generation_limited=True,
                first_audio_ms=100.0,
                audio_source="moss-tts:fresh-generation",
            )
        )

        self.assertTrue(
            controller._play_live_chunk(
                SpeechChunk(4, "Rhiannon", "I, erhm ..."),
                controller.speech_backend.prepared,
            )
        )

        self.assertTrue(any("safety limit" in status for status in statuses))
        completion = next(
            event for event in timeline_events if event[0] == "playback-completion"
        )
        self.assertEqual(completion[1], 4)
        self.assertTrue(completion[3]["generation_limited"])

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

    def test_controller_wraps_live_backend_when_generated_audio_is_configured(self):
        library = Mock()
        library.index.entries = (Mock(), Mock())
        library_factory = Mock(return_value=library)
        wrapped_backend = Mock()
        backend_factory = Mock(return_value=wrapped_backend)
        controller = AppController(
            AppSettings(
                story_index="story.jsonl",
                generated_audio_manifest="generated-audio.json",
                audio_source_policy="prefer-generated",
            ),
            tts_factory=Mock(),
            generated_audio_library_factory=library_factory,
            generated_audio_backend_factory=backend_factory,
        )
        live_backend = Mock()
        controller.speech_backend = live_backend

        self.assertTrue(controller._configure_generated_audio_backend())

        library_factory.assert_called_once_with(
            "generated-audio.json",
            warn=controller.status_handler,
        )
        backend_factory.assert_called_once_with(
            live_backend,
            library,
            controller.chapter_voice_preloader,
            volume=1.0,
            speed=1.0,
            audio_source_policy="prefer-generated",
        )
        self.assertIs(controller.speech_backend, wrapped_backend)

    def test_controller_wraps_live_backend_for_source_audio_without_generations(self):
        wrapped_backend = Mock()
        backend_factory = Mock(return_value=wrapped_backend)
        library_factory = Mock()
        controller = AppController(
            AppSettings(
                story_index="story.jsonl",
                audio_source_policy="prefer-game-audio",
            ),
            tts_factory=Mock(),
            generated_audio_library_factory=library_factory,
            generated_audio_backend_factory=backend_factory,
        )
        live_backend = Mock()
        controller.speech_backend = live_backend

        self.assertTrue(controller._configure_generated_audio_backend())

        library_factory.assert_not_called()
        backend_factory.assert_called_once_with(
            live_backend,
            None,
            controller.chapter_voice_preloader,
            volume=1.0,
            speed=1.0,
            audio_source_policy="prefer-game-audio",
            require_source_audio_completion=False,
        )
        self.assertIs(controller.speech_backend, wrapped_backend)

    def test_persisted_narrator_assignment_does_not_override_unknown_source_audio(self):
        text = "An unattributed source line."
        preloader = ChapterVoicePreloader(
            [
                ChapterDialogue(
                    "game:unknown:1",
                    "unknown",
                    1,
                    "???",
                    text,
                    text_sha256(text),
                    "available",
                    "voice-unknown",
                )
            ]
        )
        controller = AppController(
            AppSettings(
                story_index="story.jsonl",
                audio_source_policy="prefer-game-audio",
                voice_assignments={"Narrator": "preset:alba"},
                force_live_narrator=True,
            ),
            tts_factory=Mock(),
            chapter_voice_preloader=preloader,
        )
        live_backend = Mock()
        live_backend.name = "pocket-tts"
        live_backend.capabilities = SpeechBackendCapabilities(True, True, False)
        controller.speech_backend = live_backend

        self.assertTrue(controller._configure_generated_audio_backend())
        controller.speech_backend.set_live_mode_active(True)
        route = controller.speech_backend.prepare_route("???", text)

        self.assertIsInstance(route, SourceAudioRoute)
        self.assertFalse(controller.speech_backend.voice_override("???"))
        self.assertTrue(controller.speech_backend.voice_override("Narrator"))
        live_backend.prepare_playback.assert_not_called()

    def test_controller_keeps_live_backend_without_story_identity(self):
        statuses = []
        library_factory = Mock()
        controller = AppController(
            AppSettings(
                generated_audio_manifest="generated-audio.json",
                audio_source_policy="prefer-generated",
            ),
            tts_factory=Mock(),
            status_handler=statuses.append,
            generated_audio_library_factory=library_factory,
        )
        live_backend = Mock()
        controller.speech_backend = live_backend

        self.assertFalse(controller._configure_generated_audio_backend())

        self.assertIs(controller.speech_backend, live_backend)
        library_factory.assert_not_called()
        self.assertIn("story index", statuses[-1])

    def test_live_tts_policy_does_not_load_or_wrap_story_audio(self):
        library_factory = Mock()
        backend_factory = Mock()
        controller = AppController(
            AppSettings(
                story_index="story.jsonl",
                generated_audio_manifest="generated-audio.json",
                audio_source_policy="live-tts-only",
            ),
            tts_factory=Mock(),
            generated_audio_library_factory=library_factory,
            generated_audio_backend_factory=backend_factory,
        )
        live_backend = Mock(name="moss-tts")
        live_backend.name = "moss-tts"
        controller.speech_backend = live_backend

        self.assertFalse(controller._configure_generated_audio_backend())

        self.assertIs(controller.speech_backend, live_backend)
        library_factory.assert_not_called()
        backend_factory.assert_not_called()

    def test_controller_does_not_prime_unknown_speaker_as_narrator(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.voice_router = Mock(registry=CharacterVoiceRegistry())
        controller.speech_backend = Mock()
        controller.speech_executor = Mock()

        self.assertFalse(controller._prime_observed_voice("Unknown NPC"))

        controller.speech_executor.submit.assert_not_called()

    def test_controller_primes_exact_unknown_label_as_narrator(self):
        controller = AppController(AppSettings(), tts_factory=Mock())
        controller.voice_router = Mock(registry=CharacterVoiceRegistry())
        controller.speech_backend = Mock()
        controller.speech_executor = Mock()

        self.assertTrue(controller._prime_observed_voice("???"))

        controller.speech_executor.submit.assert_called_once_with(
            controller.speech_backend.prime, "Narrator"
        )

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

    def test_auto_advance_status_distinguishes_dispatch_failure_and_confirmation(self):
        statuses = []
        controller = AppController(
            AppSettings(auto_advance_enabled=True),
            status_handler=statuses.append,
        )

        with patch("vntts.controller.DialogueAdvancer") as advancer:
            self.assertTrue(controller._auto_advance_dialog())

        advancer.return_value.advance.assert_called_once_with()
        self.assertEqual(statuses, [])

        controller._auto_advance_state_changed("dispatched", 4, 1)
        controller._auto_advance_state_changed("waiting", 4, 1)
        controller._auto_advance_state_changed("failed", 4, 1)
        controller._auto_advance_state_changed("confirmed", 4, 1)

        self.assertIn("waiting for dialogue change", statuses[0])
        self.assertIn("continuing to wait", statuses[1])
        self.assertIn("no second key was sent", statuses[2])
        self.assertEqual(statuses[3], "Auto advance confirmed by new dialogue")

    def test_auto_advance_reports_that_it_is_waiting_for_game_focus(self):
        statuses = []
        controller = AppController(AppSettings(), status_handler=statuses.append)

        controller._auto_advance_state_changed("focus-wait", 4, 0)

        self.assertEqual(
            statuses,
            ["Auto advance is waiting; focus the selected game window"],
        )

    def test_disabling_auto_advance_cancels_reader_state_machine(self):
        controller = AppController(AppSettings(auto_advance_enabled=True))
        controller.live_reader = Mock()

        self.assertFalse(controller.set_auto_advance_enabled(False))

        controller.live_reader.set_auto_advance.assert_called_once_with(None)

    def test_diagnostic_does_not_scrape_legacy_backend_metrics(self):
        diagnostics = []
        controller = AppController(
            AppSettings(),
            diagnostic_handler=diagnostics.append,
        )
        controller.speech_backend = Mock(
            last_synthesis_ms=12.0,
            last_playback_ms=34.0,
            last_first_audio_ms=5.0,
        )
        controller.last_audio_source_description = "MOSS persistent cache"

        controller._publish_diagnostic(DiagnosticSnapshot(None))

        self.assertEqual(
            diagnostics[-1].audio_source,
            "Not selected",
        )

    def test_diagnostic_uses_route_bound_outcome_metrics(self):
        diagnostics = []
        controller = AppController(
            AppSettings(),
            diagnostic_handler=diagnostics.append,
        )
        controller.speech_backend = Mock(
            last_synthesis_ms=500.0,
            last_playback_ms=900.0,
            last_first_audio_ms=999.0,
        )
        controller.last_audio_source_description = "Live TTS (moss-tts)"
        outcome = PlaybackOutcome(
            PlaybackStatus.COMPLETED,
            20.0,
            first_audio_ms=10.0,
            synthesis_ms=5.0,
            cache_source="fresh-generation",
        )

        controller._publish_diagnostic(
            DiagnosticSnapshot(None), outcome, "Live TTS (moss-tts)"
        )

        self.assertEqual(diagnostics[-1].synthesis_ms, 5.0)
        self.assertEqual(diagnostics[-1].playback_ms, 20.0)
        self.assertEqual(diagnostics[-1].last_first_audio_ms, 10.0)
        self.assertEqual(diagnostics[-1].cache_source, "fresh-generation")

    def test_typed_playback_diagnostic_keeps_route_local_source_and_metrics(self):
        diagnostics = []
        controller = AppController(
            AppSettings(),
            tts_factory=Mock(),
            diagnostic_handler=diagnostics.append,
        )
        controller.live_reader = Mock()
        controller.live_reader.wait_until_playable.return_value = True
        controller.last_diagnostic = DiagnosticSnapshot(None)

        class ConcurrentPrepareBackend:
            name = "typed-test"

            def play_route(self, _route, *, playback_guard=None):
                self.guard_value = playback_guard()
                controller.last_audio_source_description = "Live TTS (route B)"
                return PlaybackOutcome(
                    PlaybackStatus.COMPLETED,
                    20.0,
                    first_audio_ms=10.0,
                    synthesis_ms=5.0,
                    cache_source="generated-audio",
                    audio_source="generated",
                )

        controller.speech_backend = ConcurrentPrepareBackend()
        route = GeneratedAudioRoute(
            PreparedGeneratedAudio(
                "game:1",
                "a" * 64,
                np.array([0.0], dtype=np.float32),
                24_000,
            ),
            AudioRouteTrace(
                None,
                "generated",
                "exact",
                None,
                None,
                "game:1",
                "generated-audio-entry-verified",
            ),
        )

        controller._play_live_chunk(SpeechChunk(4, "Rhiannon", "A line."), route)

        self.assertEqual(diagnostics[-1].audio_source, "Generated audio (line game:1)")
        self.assertEqual(diagnostics[-1].synthesis_ms, 5.0)
        self.assertEqual(diagnostics[-1].playback_ms, 20.0)
        self.assertEqual(diagnostics[-1].last_first_audio_ms, 10.0)
        self.assertEqual(diagnostics[-1].cache_source, "generated-audio")

    def test_live_prepare_reports_generation_scoped_audio_route(self):
        traces = []
        timeline_events = []
        registry = CharacterVoiceRegistry(
            [
                CharacterVoice(
                    "Rhiannon",
                    "rhiannon-v2",
                    references=(Path("rhiannon-reference.wav"),),
                )
            ]
        )
        controller = AppController(
            AppSettings(audio_source_policy="live-tts-only"),
            route_trace_handler=traces.append,
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                timeline_events.append((stage, generation, occurred_at, details))
            ),
        )
        controller.voice_router = Mock(registry=registry)
        controller.speech_backend = StubTypedPlaybackBackend()

        controller._prepare_live_chunk(SpeechChunk(9, "Rhiannon", "I, erhm ..."))

        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self.assertEqual(trace.generation, 9)
        self.assertEqual(trace.effective_source, "moss-tts:fresh-generation")
        self.assertEqual(trace.fallback_reason, "policy-live-tts-only")
        self.assertEqual(trace.voice_reference_id, "voice:rhiannon-v2:reference-1")
        self.assertEqual(
            trace.artifact_preflight_state, "not-requested-live-tts-policy"
        )
        self.assertEqual(
            [event[0] for event in timeline_events],
            ["route-decision", "voice-resolution"],
        )
        self.assertEqual(timeline_events[0][1], 9)
        self.assertEqual(
            timeline_events[0][3]["effective_source"],
            "moss-tts:fresh-generation",
        )
        self.assertEqual(
            timeline_events[1][3]["voice_reference_id"],
            "voice:rhiannon-v2:reference-1",
        )

    def test_speaker_change_announcement_is_separate_live_audio_and_not_repeated(self):
        statuses = []
        timeline = []
        backend = RecordingAnnouncementBackend()
        registry = CharacterVoiceRegistry([CharacterVoice("Rhiannon", "rhiannon-v2")])
        controller = AppController(
            AppSettings(
                audio_source_policy="live-tts-only",
                announce_speaker_changes=True,
            ),
            status_handler=statuses.append,
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                timeline.append((stage, generation, details))
            ),
        )
        controller.voice_router = Mock(registry=registry, narrator_speaker="Centurion")
        controller.speech_backend = backend
        controller.live_reader = Mock(max_speech_jobs=2)
        controller.live_reader.wait_until_playable.return_value = True

        first = SpeechChunk(1, "Rhiannon", "First line.", ordinal=1)
        prepared = controller._prepare_live_chunk(first)
        result = controller._play_live_chunk(first, prepared)
        same_speaker = controller._prepare_live_chunk(
            SpeechChunk(2, "Rhiannon", "Second line.", ordinal=1)
        )
        changed_speaker = controller._prepare_live_chunk(
            SpeechChunk(3, "Hotelier", "Welcome.", ordinal=1)
        )

        self.assertIsInstance(prepared, PreparedLiveChunkRoutes)
        self.assertTrue(result)
        self.assertNotIsInstance(same_speaker, PreparedLiveChunkRoutes)
        self.assertIsInstance(changed_speaker, PreparedLiveChunkRoutes)
        self.assertEqual(
            backend.prepare_calls,
            [
                ("Rhiannon", "First line."),
                ("Narrator", "Rhiannon."),
                ("Rhiannon", "Second line."),
                ("Hotelier", "Welcome."),
                ("Narrator", "Hotelier."),
            ],
        )
        self.assertEqual(
            backend.play_calls,
            [("Narrator", "Rhiannon."), ("Rhiannon", "First line.")],
        )
        self.assertEqual(
            [event[0] for event in timeline].count("speaker-announcement-route"),
            2,
        )
        self.assertEqual(
            [event[0] for event in timeline].count("speaker-announcement-outcome"),
            1,
        )
        self.assertEqual(
            [event[0] for event in timeline].count("playback-outcome"),
            1,
        )
        self.assertIn("Announcing speaker: Rhiannon", statuses)

    def test_speaker_announcement_skips_game_audio_and_maps_unknown_to_narrator(self):
        backend = RecordingAnnouncementBackend()
        controller = AppController(
            AppSettings(announce_speaker_changes=True),
            tts_factory=Mock(),
        )
        controller.voice_router = Mock(
            registry=CharacterVoiceRegistry(), narrator_speaker="Centurion"
        )
        controller.speech_backend = backend
        source = SourceAudioRoute(
            PreparedSourceAudioPassThrough("game:1", "a" * 64, "voice-1", 1.0),
            stub_route_trace("game-source", "game:1"),
        )

        announcement, speaker = controller._prepare_speaker_announcement(
            SpeechChunk(1, "Rhiannon", "Original line.", ordinal=1), source
        )
        same_speaker, _same_label = controller._prepare_speaker_announcement(
            SpeechChunk(2, "Rhiannon", "Generated line.", ordinal=1),
            backend.prepare_playback("Rhiannon", "Generated line."),
        )
        narrator_announcement, narrator = controller._prepare_speaker_announcement(
            SpeechChunk(3, "???", "Unknown line.", ordinal=1),
            backend.prepare_playback("???", "Unknown line."),
        )

        self.assertIsNone(announcement)
        self.assertIsNone(speaker)
        self.assertIsNone(same_speaker)
        self.assertIsNotNone(narrator_announcement)
        self.assertEqual(narrator, "Narrator")
        self.assertEqual(backend.prepare_calls[-1], ("Narrator", "Narrator."))

    def test_fallback_role_mode_announces_only_bound_generated_narrator_roles(self):
        backend = RecordingAnnouncementBackend()
        controller = AppController(
            AppSettings(speaker_announcement_mode="narrator-fallback-roles"),
            tts_factory=Mock(),
        )
        controller.voice_router = Mock(
            registry=CharacterVoiceRegistry(), narrator_speaker="Centurion"
        )
        controller.speech_backend = backend

        def generated(role=None):
            return GeneratedAudioRoute(
                PreparedGeneratedAudio(
                    "game:1",
                    "a" * 64,
                    np.array([0.0], dtype=np.float32),
                    24_000,
                    narrator_fallback_role=role,
                ),
                stub_route_trace("generated", "game:1"),
            )

        true_narrator, _ = controller._prepare_speaker_announcement(
            SpeechChunk(1, "Narrator", "Narration.", ordinal=1), generated()
        )
        poacher, poacher_label = controller._prepare_speaker_announcement(
            SpeechChunk(2, "Poacher I", "Stop there.", ordinal=1),
            generated("Poacher I"),
        )
        same_poacher, _ = controller._prepare_speaker_announcement(
            SpeechChunk(3, "Poacher I", "Again.", ordinal=1),
            generated("Poacher I"),
        )
        narrator_again, _ = controller._prepare_speaker_announcement(
            SpeechChunk(4, "Narrator", "Narration.", ordinal=1), generated()
        )
        poacher_again, _ = controller._prepare_speaker_announcement(
            SpeechChunk(5, "Poacher I", "Return.", ordinal=1),
            generated("Poacher I"),
        )
        unknown, unknown_label = controller._prepare_speaker_announcement(
            SpeechChunk(6, "???", "Who am I?", ordinal=1), generated("Unknown")
        )

        def live_fallback(queue_id, requested):
            return LiveFallbackRoute(
                backend.prepare_playback("Narrator", "Fallback line."),
                LiveFallbackDecision(
                    "vntts.authoring-live-fallback-decision",
                    1,
                    "reference_unavailable_after_audit",
                    "pocket-tts",
                    "pocket-tts",
                    "default",
                    queue_id,
                    queue_id,
                    "a" * 64,
                    requested,
                    requested,
                    None,
                    "2026-08-28T00:00:00+00:00",
                    "b" * 64,
                ),
                stub_route_trace("live-fallback", queue_id),
                None,
                None,
            )

        aderyn_fallback, aderyn_label = controller._prepare_speaker_announcement(
            SpeechChunk(7, "Aderyn", "Fallback line.", ordinal=1),
            live_fallback("game:aderyn", "Aderyn"),
        )
        unknown_fallback, unknown_fallback_label = (
            controller._prepare_speaker_announcement(
                SpeechChunk(8, "???", "Fallback line.", ordinal=1),
                live_fallback("game:unknown", "Narrator"),
            )
        )

        self.assertIsNone(true_narrator)
        self.assertIsNotNone(poacher)
        self.assertEqual(poacher_label, "Poacher I")
        self.assertIsNone(same_poacher)
        self.assertIsNone(narrator_again)
        self.assertIsNotNone(poacher_again)
        self.assertIsNotNone(unknown)
        self.assertEqual(unknown_label, "Unknown")
        self.assertIsNotNone(aderyn_fallback)
        self.assertEqual(aderyn_label, "Aderyn")
        self.assertIsNotNone(unknown_fallback)
        self.assertEqual(unknown_fallback_label, "Unknown")
        self.assertEqual(
            backend.prepare_calls,
            [
                ("Narrator", "Poacher I."),
                ("Narrator", "Poacher I."),
                ("Narrator", "Unknown."),
                ("Narrator", "Fallback line."),
                ("Narrator", "Aderyn."),
                ("Narrator", "Fallback line."),
                ("Narrator", "Unknown."),
            ],
        )

    def test_fallback_role_mode_does_not_announce_live_or_game_routes(self):
        backend = RecordingAnnouncementBackend()
        controller = AppController(
            AppSettings(speaker_announcement_mode="narrator-fallback-roles"),
            tts_factory=Mock(),
        )
        controller.voice_router = Mock(
            registry=CharacterVoiceRegistry(), narrator_speaker="Centurion"
        )
        controller.speech_backend = backend
        live = backend.prepare_playback("Narrator", "Line.")
        source = SourceAudioRoute(
            PreparedSourceAudioPassThrough("game:1", "a" * 64, "voice-1", 1.0),
            stub_route_trace("game-source", "game:1"),
        )

        live_announcement, _ = controller._prepare_speaker_announcement(
            SpeechChunk(1, "Poacher I", "Line.", ordinal=1), live
        )
        source_announcement, _ = controller._prepare_speaker_announcement(
            SpeechChunk(2, "Poacher I", "Original.", ordinal=1), source
        )

        self.assertIsNone(live_announcement)
        self.assertIsNone(source_announcement)
        self.assertEqual(backend.prepare_calls, [("Narrator", "Line.")])

    def test_failed_speaker_announcement_does_not_skip_dialogue(self):
        errors = []
        backend = RecordingAnnouncementBackend(
            outcomes=[
                PlaybackOutcome(
                    PlaybackStatus.FAILED,
                    None,
                    error="announcement device failed",
                ),
                PlaybackOutcome(PlaybackStatus.COMPLETED, 4.0),
            ]
        )
        controller = AppController(
            AppSettings(announce_speaker_changes=True),
            error_handler=errors.append,
        )
        controller.voice_router = Mock(
            registry=CharacterVoiceRegistry(), narrator_speaker="Centurion"
        )
        controller.speech_backend = backend
        controller.live_reader = Mock(max_speech_jobs=2)
        controller.live_reader.wait_until_playable.return_value = True
        chunk = SpeechChunk(1, "Rhiannon", "Line.", ordinal=1)

        prepared = controller._prepare_live_chunk(chunk)
        result = controller._play_live_chunk(chunk, prepared)

        self.assertTrue(result)
        self.assertEqual(len(backend.play_calls), 2)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], AudioPlaybackError)

    def test_speaker_announcement_exception_does_not_skip_dialogue(self):
        errors = []
        statuses = []
        backend = RecordingAnnouncementBackend(
            outcomes=[
                RuntimeError("announcement output crashed"),
                PlaybackOutcome(PlaybackStatus.COMPLETED, 4.0),
            ]
        )
        controller = AppController(
            AppSettings(announce_speaker_changes=True),
            status_handler=statuses.append,
            error_handler=errors.append,
        )
        controller.voice_router = Mock(
            registry=CharacterVoiceRegistry(), narrator_speaker="Centurion"
        )
        controller.speech_backend = backend
        controller.live_reader = Mock(max_speech_jobs=2)
        controller.live_reader.wait_until_playable.return_value = True
        chunk = SpeechChunk(1, "Rhiannon", "Line.", ordinal=1)

        prepared = controller._prepare_live_chunk(chunk)
        result = controller._play_live_chunk(chunk, prepared)

        self.assertTrue(result)
        self.assertEqual(len(backend.play_calls), 2)
        self.assertEqual(str(errors[0]), "announcement output crashed")
        self.assertIn("continuing dialogue", statuses[-2])

    def test_speaker_announcement_prepare_exception_keeps_dialogue_route(self):
        errors = []
        statuses = []
        backend = FailingAnnouncementPrepareBackend()
        controller = AppController(
            AppSettings(announce_speaker_changes=True),
            status_handler=statuses.append,
            error_handler=errors.append,
        )
        controller.voice_router = Mock(
            registry=CharacterVoiceRegistry(), narrator_speaker="Centurion"
        )
        controller.speech_backend = backend

        prepared = controller._prepare_live_chunk(
            SpeechChunk(1, "Rhiannon", "Line.", ordinal=1)
        )

        self.assertIsInstance(prepared, PreparedPlayback)
        self.assertEqual(prepared.payload.text, "Line.")
        self.assertEqual(str(errors[0]), "announcement preparation crashed")
        self.assertIn("continuing dialogue", statuses[-1])

    def test_live_prepare_keeps_two_chunk_scoped_voice_resolution_events(self):
        registry = CharacterVoiceRegistry(
            [
                CharacterVoice(
                    "Rhiannon",
                    "rhiannon-v2",
                    references=(Path("rhiannon-reference.wav"),),
                )
            ]
        )
        timelines = GenerationTimelineLog()
        controller = AppController(
            AppSettings(audio_source_policy="live-tts-only"),
            pipeline_event_handler=timelines.record,
        )
        controller.voice_router = Mock(registry=registry)
        controller.speech_backend = StubTypedPlaybackBackend()

        controller._prepare_live_chunk(
            SpeechChunk(9, "Rhiannon", "First chunk.", ordinal=1)
        )
        controller._prepare_live_chunk(
            SpeechChunk(9, "Rhiannon", "Second chunk.", ordinal=2)
        )

        voice_events = [
            event
            for event in timelines.snapshot()[0]["events"]
            if event["stage"] == "voice-resolution"
        ]
        self.assertEqual(len(voice_events), 2)
        self.assertEqual([event["chunk_ordinal"] for event in voice_events], [1, 2])

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
