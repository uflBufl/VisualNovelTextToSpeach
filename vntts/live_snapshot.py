"""Capture one live dialog snapshot without routing speech."""

from collections.abc import Callable

from vntts.diagnostics import DiagnosticSnapshot
from vntts.dialog_capture import PathInput, analyze_dialog_snapshot
from vntts.ocr import (
    DialogRegion,
    OCRResult,
    UncertainFrameRecorder,
    VoiceRegistry,
    default_minimum_ocr_confidence,
)
from vntts.ocr_corrections import OCRCorrectionDictionary


def read_live_snapshot(
    screenshot_directory: PathInput,
    voice_registry: VoiceRegistry | None = None,
    capture_target: object | None = None,
    minimum_confidence: float = default_minimum_ocr_confidence,
    uncertain_handler: Callable[[OCRResult, float], object] | None = None,
    uncertain_frame_recorder: UncertainFrameRecorder | None = None,
    diagnostic_handler: Callable[[DiagnosticSnapshot], object] | None = None,
    voice_resolver: Callable[[str], str] | None = None,
    ocr_language: str = "eng",
    correction_dictionary: OCRCorrectionDictionary | None = None,
    region: DialogRegion | None = None,
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
        region=region,
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
