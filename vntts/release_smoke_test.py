from pathlib import Path

from PIL import Image

from vntts.atomic_io import atomic_write_json
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
):
    checks = []
    recognized_text = ""
    recognized_speaker = ""
    confidence = 0.0
    try:
        if bool(image_path) == bool(window_title):
            raise ValueError("Provide exactly one smoke-test image or window title")

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
        "checks": checks,
    }
    return successful, _write_report(report, report_path)
