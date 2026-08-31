"""Capture one live dialog snapshot without routing speech."""

from typing import Any, Callable

from vntts.dialog_capture import analyze_dialog_snapshot
from vntts.ocr import default_minimum_ocr_confidence


def read_live_snapshot(
    screenshot_directory: Any,
    voice_registry: Any = None,
    capture_target: Any = None,
    minimum_confidence: float = default_minimum_ocr_confidence,
    uncertain_handler: Callable[[Any, float], Any] | None = None,
    uncertain_frame_recorder: Any = None,
    diagnostic_handler: Callable[[Any], Any] | None = None,
    voice_resolver: Callable[[str], Any] | None = None,
    ocr_language: str = "eng",
    correction_dictionary: Any = None,
) -> tuple[str | None, str]:
    image, _, result = analyze_dialog_snapshot(
        screenshot_directory,
        voice_registry,
        capture_target=capture_target,
        minimum_confidence=minimum_confidence,
        diagnostic_handler=diagnostic_handler,
        voice_resolver=voice_resolver,
        ocr_language=ocr_language,
        correction_dictionary=correction_dictionary,
    )
    if result.text and not result.is_confident(minimum_confidence):
        if uncertain_frame_recorder is not None:
            uncertain_frame_recorder.record(image, result, minimum_confidence)
        if uncertain_handler is not None:
            uncertain_handler(result, minimum_confidence)
        return None, ""
    if uncertain_frame_recorder is not None:
        uncertain_frame_recorder.reset()
    return result.character, result.text
