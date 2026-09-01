from pathlib import Path
from time import monotonic, sleep

from PIL import Image
from vntts_artifacts.atomic_io import atomic_write_json

from vntts.cli import CLIReportResult
from vntts.ocr import (
    default_dialog_region,
    default_minimum_ocr_confidence,
    recognize_dialog_image_result,
)
from vntts.services.tts_engine import TTSEngine
from vntts.settings import get_local_data_directory
from vntts.voices import CharacterVoice, CharacterVoiceRegistry
from vntts.window_capture import WindowCaptureTarget

default_smoke_test_model = "tts_models/en/vctk/vits"
default_auto_advance_timeout_seconds = 8.0


def configure_release_smoke_arguments(parser):
    parser.add_argument("--release-smoke-test-image")
    parser.add_argument("--release-smoke-test-window-title")
    parser.add_argument("--release-smoke-test-report")
    parser.add_argument("--release-smoke-test-model", default=default_smoke_test_model)
    parser.add_argument("--release-smoke-test-expected-speaker")
    parser.add_argument("--release-smoke-test-auto-advance-expected-text")


def _write_report(report, report_path):
    report_path = (
        get_local_data_directory() / "release-smoke-test.json"
        if report_path is None
        else Path(report_path).expanduser()
    )
    atomic_write_json(report_path, report)
    return report_path


def run_release_smoke_test(
    *,
    image_path=None,
    window_title=None,
    report_path=None,
    model_name=default_smoke_test_model,
    expected_speaker=None,
    minimum_confidence=default_minimum_ocr_confidence,
    recognize=None,
    engine_factory=None,
    capture=None,
    auto_advance_expected_text=None,
    auto_advance=None,
    auto_advance_timeout_seconds=default_auto_advance_timeout_seconds,
):
    checks = []
    recognized_text = ""
    recognized_speaker = ""
    confidence = 0.0
    auto_advance_dispatched = False
    auto_advance_acknowledged = False
    try:
        if bool(image_path) == bool(window_title):
            raise ValueError("Provide exactly one smoke-test image or window title")
        if auto_advance_expected_text and not window_title:
            raise ValueError("Auto-advance verification requires a window title")

        if image_path:
            image_path = Path(image_path).expanduser().resolve()
            with Image.open(image_path) as screenshot:
                image = default_dialog_region.crop(screenshot.convert("RGB"))
            capture_source = str(image_path)
        else:
            if capture is None:
                from vntts.dialog_capture import capture_dialog

                capture = capture_dialog
            image, _output = capture(
                save_screenshot=False,
                capture_target=WindowCaptureTarget(window_title),
            )
            capture_source = f"window:{window_title}"
        checks.append(
            {
                "name": "Dialog capture",
                "status": "ok",
                "message": capture_source,
            }
        )

        recognize = recognize or recognize_dialog_image_result
        voice_registry = None
        if expected_speaker:
            voice_registry = CharacterVoiceRegistry(
                [CharacterVoice(expected_speaker, "release-smoke-test")]
            )
        result = recognize(
            image,
            voice_registry,
            minimum_confidence=minimum_confidence,
        )
        recognized_text = result.text
        recognized_speaker = result.character
        confidence = result.confidence
        if not recognized_text.strip():
            raise RuntimeError("OCR did not recognize dialog text")
        if not result.is_confident(minimum_confidence):
            raise RuntimeError(
                f"OCR confidence {confidence:.0f}% is below {minimum_confidence:g}%"
            )
        if (
            expected_speaker
            and recognized_speaker.casefold() != expected_speaker.casefold()
        ):
            raise RuntimeError(
                f"Expected speaker {expected_speaker!r}, got {recognized_speaker!r}"
            )
        checks.append(
            {
                "name": "Tesseract OCR",
                "status": "ok",
                "message": (
                    f"{recognized_speaker}: {recognized_text} "
                    f"({confidence:.0f}% confidence)"
                ),
            }
        )

        engine_factory = engine_factory or TTSEngine
        engine = engine_factory(model_name=model_name)
        engine.speak(recognized_text)
        checks.append(
            {
                "name": "Speech synthesis and playback",
                "status": "ok",
                "message": model_name,
            }
        )
        if auto_advance_expected_text:
            auto_advance = auto_advance or (
                lambda: _production_auto_advance(window_title)
            )
            if auto_advance() is not True:
                raise RuntimeError(
                    "Production controller did not dispatch auto advance"
                )
            auto_advance_dispatched = True
            deadline = monotonic() + auto_advance_timeout_seconds
            while monotonic() < deadline:
                image, _output = capture(
                    save_screenshot=False,
                    capture_target=WindowCaptureTarget(window_title),
                )
                advanced = recognize(
                    image,
                    voice_registry,
                    minimum_confidence=minimum_confidence,
                )
                speaker_matches = (
                    not expected_speaker
                    or advanced.character.casefold() == expected_speaker.casefold()
                )
                if (
                    advanced.is_confident(minimum_confidence)
                    and speaker_matches
                    and auto_advance_expected_text.casefold()
                    in advanced.text.casefold()
                ):
                    auto_advance_acknowledged = True
                    break
                sleep(0.1)
            if not auto_advance_acknowledged:
                raise RuntimeError(
                    "Auto advance was dispatched but the fixture did not show the "
                    "expected next dialog"
                )
            checks.append(
                {
                    "name": "Production auto advance",
                    "status": "ok",
                    "message": auto_advance_expected_text,
                }
            )
    except Exception as error:
        checks.append(
            {
                "name": "Release smoke test",
                "status": "error",
                "message": str(error),
            }
        )

    successful = all(check["status"] == "ok" for check in checks)
    report = {
        "success": successful,
        "source": str(image_path or f"window:{window_title}"),
        "model": model_name,
        "speaker": recognized_speaker,
        "text": recognized_text,
        "confidence": confidence,
        "auto_advance_dispatched": auto_advance_dispatched,
        "auto_advance_acknowledged": auto_advance_acknowledged,
        "auto_advance_controller": "AppController._auto_advance_dialog",
        "checks": checks,
    }
    return CLIReportResult(successful, _write_report(report, report_path))


def _production_auto_advance(window_title):
    from vntts.controller import AppController
    from vntts.settings import AppSettings

    controller = AppController(
        AppSettings(
            capture_mode="window",
            game_window_title=window_title,
            auto_advance_enabled=True,
            live_sequence_mode="off",
        )
    )
    try:
        return controller._auto_advance_dialog()
    finally:
        controller.shutdown()
