"""Screen capture, OCR, and one-shot dialog processing."""

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from hashlib import blake2b
from pathlib import Path
from time import monotonic
from uuid import uuid4

import mss
from PIL import Image

from vntts.diagnostics import DiagnosticSnapshot
from vntts.dialog import is_empty, speak_dialog
from vntts.ocr import (
    default_minimum_ocr_confidence,
    get_dialog_region,
    recognize_dialog_image_result,
)
from vntts.ocr_backend import TesseractOCRBackend
from vntts.services.tts_engine import AudioPlaybackError, TTSError
from vntts.voices import VoiceManifestError
from vntts.window_capture import ensure_screen_capture_supported

default_screenshot_directory = Path("logs/screenshots")


class ScreenCaptureError(RuntimeError):
    pass


class OCRError(RuntimeError):
    pass


class OCRUncertainError(OCRError):
    def __init__(self, result, minimum_confidence):
        self.result = result
        self.minimum_confidence = minimum_confidence
        super().__init__(
            f"confidence {result.confidence:.0f}% is below the required "
            f"{minimum_confidence}%"
        )


class TTSInitializationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CapturedDialogFrame:
    image: Image.Image
    capture_ms: float


def get_screenshot_directory(settings=None):
    if settings is not None:
        return Path(settings.screenshot_directory).expanduser()

    configured_directory = os.environ.get("VNTTS_SCREENSHOT_DIR")
    if not configured_directory:
        return default_screenshot_directory

    return Path(configured_directory)


def create_screenshot_path(screenshot_directory):
    screenshot_directory = Path(screenshot_directory)
    formatted_date = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return screenshot_directory / f"dialog-{formatted_date}-{uuid4().hex}.png"


def capture_dialog(
    screenshot_directory=None,
    *,
    save_screenshot=True,
    region=None,
    capture_target=None,
):
    try:
        ensure_screen_capture_supported()
        if screenshot_directory is None:
            screenshot_directory = get_screenshot_directory()
        screenshot_directory = Path(screenshot_directory)
        screenshot_directory.mkdir(parents=True, exist_ok=True)

        with mss.mss() as sct:
            region = region or get_dialog_region()
            dialog_box = (
                capture_target.capture_box(region)
                if capture_target is not None
                else region.capture_box(sct.monitors[1])
            )
            screenshot = sct.grab(dialog_box)

            image = Image.frombytes(
                "RGB",
                screenshot.size,
                screenshot.bgra,
                "raw",
                "BGRX",
            )

        output = None
        if save_screenshot:
            output = create_screenshot_path(screenshot_directory)
            image.save(output)
        return image, output
    except Exception as error:
        raise ScreenCaptureError(str(error)) from error


def recognize_screenshot_result(
    image,
    voice_registry=None,
    minimum_confidence=default_minimum_ocr_confidence,
    ocr_language="eng",
    correction_dictionary=None,
    ocr_backend=None,
):
    try:
        backend = ocr_backend or TesseractOCRBackend(recognize_dialog_image_result)
        result = backend.recognize(
            image,
            voice_registry,
            minimum_confidence=minimum_confidence,
            language=ocr_language,
        )
        return (
            correction_dictionary.correct_result(result)
            if correction_dictionary is not None
            else result
        )
    except Exception as error:
        raise OCRError(str(error)) from error


def capture_live_frame(screenshot_directory, capture_target=None, *, clock=monotonic):
    capture_started = clock()
    image, _output = capture_dialog(
        screenshot_directory,
        save_screenshot=False,
        capture_target=capture_target,
    )
    return CapturedDialogFrame(
        image=image,
        capture_ms=(clock() - capture_started) * 1000,
    )


def fingerprint_dialog_frame(frame):
    """Cheap fingerprint that preserves small glyph changes after downsampling."""
    grayscale = frame.image.convert("L").resize(
        (256, 64),
        Image.Resampling.LANCZOS,
    )
    # Coarse quantization ignores insignificant capture noise, while retaining
    # several intensity levels so a new sentence cannot disappear like it did
    # in the previous 96x54 binary mask.
    quantized = grayscale.point(tuple((value // 16) * 16 for value in range(256)))
    return blake2b(quantized.tobytes(), digest_size=16).digest()


def recognize_live_frame(
    frame,
    voice_registry=None,
    minimum_confidence=default_minimum_ocr_confidence,
    uncertain_handler=None,
    uncertain_frame_recorder=None,
    diagnostic_handler=None,
    voice_resolver=None,
    ocr_language="eng",
    correction_dictionary=None,
    *,
    clock=monotonic,
):
    ocr_started = clock()
    result = recognize_screenshot_result(
        frame.image,
        voice_registry,
        minimum_confidence,
        ocr_language,
        correction_dictionary,
    )
    ocr_ms = (clock() - ocr_started) * 1000
    snapshot = DiagnosticSnapshot(
        image=frame.image,
        character=result.character or "Narrator",
        text=result.text,
        confidence=result.confidence,
        preprocessing_profile=result.profile,
        voice=(
            voice_resolver(result.character)
            if voice_resolver is not None
            else "Not loaded"
        ),
        capture_ms=frame.capture_ms,
        ocr_ms=ocr_ms,
        corrections=result.corrections,
        choice_detected=result.choice_detected,
    )
    if diagnostic_handler is not None:
        diagnostic_handler(snapshot)
    if result.text and not result.is_confident(minimum_confidence):
        if uncertain_frame_recorder is not None:
            uncertain_frame_recorder.record(
                frame.image,
                result,
                minimum_confidence,
            )
        if uncertain_handler is not None:
            uncertain_handler(result, minimum_confidence)
        return None, ""
    if uncertain_frame_recorder is not None:
        uncertain_frame_recorder.reset()
    return result.character, result.text


def recognize_screenshot(
    image,
    voice_registry=None,
    minimum_confidence=default_minimum_ocr_confidence,
    ocr_language="eng",
    correction_dictionary=None,
):
    result = recognize_screenshot_result(
        image,
        voice_registry,
        minimum_confidence,
        ocr_language,
        correction_dictionary,
    )
    if result.text and not result.is_confident(minimum_confidence):
        raise OCRUncertainError(result, minimum_confidence)
    return result.character, result.text


def analyze_dialog_snapshot(
    screenshot_directory,
    voice_registry=None,
    capture_target=None,
    minimum_confidence=default_minimum_ocr_confidence,
    *,
    save_screenshot=False,
    diagnostic_handler=None,
    voice_resolver=None,
    clock=monotonic,
    ocr_language="eng",
    correction_dictionary=None,
):
    capture_started = clock()
    image, output = capture_dialog(
        screenshot_directory,
        save_screenshot=save_screenshot,
        capture_target=capture_target,
    )
    capture_ms = (clock() - capture_started) * 1000

    ocr_started = clock()
    result = recognize_screenshot_result(
        image,
        voice_registry,
        minimum_confidence,
        ocr_language,
        correction_dictionary,
    )
    ocr_ms = (clock() - ocr_started) * 1000
    snapshot = DiagnosticSnapshot(
        image=image,
        character=result.character or "Narrator",
        text=result.text,
        confidence=result.confidence,
        preprocessing_profile=result.profile,
        voice=(
            voice_resolver(result.character)
            if voice_resolver is not None
            else "Not loaded"
        ),
        capture_ms=capture_ms,
        ocr_ms=ocr_ms,
        corrections=result.corrections,
        choice_detected=result.choice_detected,
    )
    if diagnostic_handler is not None:
        diagnostic_handler(snapshot)
    return image, output, result


def read_dialog(
    voice_router,
    screenshot_directory,
    capture_target=None,
    speech_handler=None,
    minimum_confidence=default_minimum_ocr_confidence,
    uncertain_frame_recorder=None,
    diagnostic_handler=None,
    voice_resolver=None,
    ocr_language="eng",
    correction_dictionary=None,
):
    image, output, result = analyze_dialog_snapshot(
        screenshot_directory,
        voice_router.registry,
        capture_target=capture_target,
        minimum_confidence=minimum_confidence,
        save_screenshot=True,
        diagnostic_handler=diagnostic_handler,
        voice_resolver=voice_resolver,
        ocr_language=ocr_language,
        correction_dictionary=correction_dictionary,
    )
    if result.text and not result.is_confident(minimum_confidence):
        error = OCRUncertainError(result, minimum_confidence)
        if uncertain_frame_recorder is not None:
            uncertain_frame_recorder.record(
                image,
                error.result,
                minimum_confidence,
            )
        raise error
    character, text = result.character, result.text
    if uncertain_frame_recorder is not None:
        uncertain_frame_recorder.reset()

    if is_empty(text):
        print(f"Screenshot {output} has no text")
    else:
        print(f"{character} is speaking now")
        print(f"Screenshot {output} with text:\n{text}")

        if speech_handler is None:
            speak_dialog(text, lambda value: voice_router.speak(character, value))
        else:
            speech_handler(character, text)


def read_dialog_safely(
    voice_router,
    screenshot_directory,
    error_handler=None,
    capture_target=None,
    speech_handler=None,
    minimum_confidence=default_minimum_ocr_confidence,
    uncertain_frame_recorder=None,
    diagnostic_handler=None,
    voice_resolver=None,
    ocr_language="eng",
    correction_dictionary=None,
):
    try:
        read_dialog(
            voice_router,
            screenshot_directory,
            capture_target,
            speech_handler,
            minimum_confidence,
            uncertain_frame_recorder,
            diagnostic_handler,
            voice_resolver,
            ocr_language,
            correction_dictionary,
        )
    except Exception as error:
        (error_handler or report_runtime_error)(error)


def format_runtime_error(error):
    if isinstance(error, ScreenCaptureError):
        message = f"Screen capture failed: {error}"
    elif isinstance(error, OCRError):
        message = f"Tesseract OCR failed: {error}"
    elif isinstance(error, TTSInitializationError):
        message = f"Unable to initialize TTS engine: {error}"
    elif isinstance(error, TTSError):
        message = f"TTS model or synthesis failed: {error}"
    elif isinstance(error, AudioPlaybackError):
        message = f"Audio playback failed: {error}"
    elif isinstance(error, VoiceManifestError):
        message = f"Voice configuration failed: {error}"
    else:
        message = f"Unexpected dialog processing failure: {error}"
    return message


def report_runtime_error(error):
    print(format_runtime_error(error), file=sys.stderr)
