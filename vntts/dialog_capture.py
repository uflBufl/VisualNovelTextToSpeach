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
from PIL import Image, ImageFilter

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
    """Fingerprint bright dialogue glyphs while ignoring the animated backdrop."""
    grayscale = frame.image.convert("L")
    # Dialogue boxes in supported games are translucent, so hashing the whole
    # crop makes character animation and video behind the box look like new
    # dialogue. Keep only the bright glyph cores before downsampling. Using the
    # cores also avoids tiny anti-aliasing changes where text meets the moving
    # background while still preserving additions to typewriter text.
    width, height = grayscale.size

    def glyph_band(
        top,
        bottom,
        output_height,
        minimum_brightness,
        *,
        ignore_continue_indicator=False,
    ):
        band = grayscale.crop((0, top, width, bottom))
        if ignore_continue_indicator:
            # Clear the chrome before measuring the band's brightest pixel;
            # otherwise a blinking indicator also changes the threshold used
            # for every dialogue glyph outside the ignored area.
            indicator_left = round(width * 0.92)
            indicator_top = max(0, round(height * 0.68) - top)
            band.paste(0, (indicator_left, indicator_top, width, bottom - top))
        brightest = band.getextrema()[1]
        # Fully covered glyph pixels keep the same value even when the
        # anti-aliased edge is composited over a changing background.
        glyph_threshold = max(minimum_brightness, brightest)
        mask = band.point(
            tuple(255 if value >= glyph_threshold else 0 for value in range(256))
        )
        mask = mask.filter(ImageFilter.MaxFilter(5)).resize(
            (256, output_height),
            Image.Resampling.LANCZOS,
        )
        # Collapse resize ringing so sub-pixel capture noise cannot invalidate
        # the cache.
        return mask.point(
            tuple(255 if value >= 32 else 0 for value in range(256))
        ).tobytes()

    # Speaker labels are often dimmer than dialogue, so fingerprint the upper
    # label and lower text bands independently. The overlap accommodates games
    # whose first dialogue line starts unusually high.
    label_bottom = max(1, round(height * 0.36))
    dialog_top = min(height - 1, round(height * 0.25))
    label = glyph_band(0, label_bottom, 27, 180)
    dialog = glyph_band(
        dialog_top,
        height,
        45,
        200,
        ignore_continue_indicator=True,
    )
    return blake2b(label + dialog, digest_size=16).digest()


def fingerprint_dialog_render_activity(frame):
    """Fingerprint high-fidelity dialogue glyph activity, not screen identity.

    The ordinary dialogue fingerprint is intentionally compact and may map a
    nearly complete typewriter prefix to the same identity as the full line.
    Render completion needs the opposite tradeoff: retain small new glyphs, but
    ignore the portrait, continue indicator and most animated backdrop pixels.
    """
    grayscale = frame.image.convert("L")
    width, height = grayscale.size
    left = min(width - 1, max(0, round(width * 0.18)))
    right = max(left + 1, min(width, round(width * 0.92)))
    top = min(height - 1, max(0, round(height * 0.25)))
    band = grayscale.crop((left, top, right, height))
    brightest = band.getextrema()[1]
    threshold = max(205, brightest - 20)
    mask = band.point(
        tuple(255 if value >= threshold else 0 for value in range(256))
    ).filter(ImageFilter.MaxFilter(3))
    if mask.width > 1024:
        output_height = max(1, round(mask.height * 1024 / mask.width))
        mask = mask.resize((1024, output_height), Image.Resampling.NEAREST)
    return blake2b(mask.tobytes(), digest_size=16).digest()


def dialog_glyphs_visible(frame):
    """Return whether the dialogue band has plausible bright text pixels.

    This deliberately remains a cheap, OCR-free fail-closed gate. Three pixels
    are enough for an anti-aliased ellipsis in the smallest supported test font,
    while a mostly bright crop is treated as a popup or calibration failure
    rather than dialogue.
    """
    grayscale = frame.image.convert("L")
    width, height = grayscale.size
    dialog_top = min(height - 1, round(height * 0.25))
    dialog = grayscale.crop((0, dialog_top, width, height))
    histogram = dialog.histogram()
    bright_pixels = sum(histogram[160:])
    pixels = max(1, dialog.width * dialog.height)
    return 3 <= bright_pixels <= round(pixels * 0.25)


def dialog_completion_cue_visible(frame):
    """Detect the game's small downward continue indicator, not dialogue text."""
    if not dialog_glyphs_visible(frame):
        return False
    grayscale = frame.image.convert("L")
    width, height = grayscale.size
    left = min(width - 1, round(width * 0.94))
    top = min(height - 1, round(height * 0.68))
    bottom = min(height, max(top + 1, round(height * 0.96)))
    region = grayscale.crop((left, top, width, bottom))
    bright = {
        (x, y)
        for y in range(region.height)
        for x in range(region.width)
        if region.getpixel((x, y)) >= 150
    }
    components = []
    while bright:
        pending = [bright.pop()]
        component = []
        while pending:
            x, y = pending.pop()
            component.append((x, y))
            for neighbor_y in range(max(0, y - 1), min(region.height, y + 2)):
                for neighbor_x in range(max(0, x - 1), min(region.width, x + 2)):
                    neighbor = (neighbor_x, neighbor_y)
                    if neighbor in bright:
                        bright.remove(neighbor)
                        pending.append(neighbor)
        components.append(component)

    minimum_component_pixels = max(4, round(width / 240))
    maximum_width = max(6, round(width * 0.02))
    maximum_height = max(6, round(height * 0.12))
    for component in components:
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        component_width = max(xs) - min(xs) + 1
        component_height = max(ys) - min(ys) + 1
        center_x = left + (min(xs) + max(xs)) / 2
        center_y = top + (min(ys) + max(ys)) / 2
        aspect_ratio = component_width / component_height
        if (
            minimum_component_pixels <= len(component) <= 500
            and 3 <= component_width <= maximum_width
            and 3 <= component_height <= maximum_height
            and width * 0.95 <= center_x <= width * 0.99
            and height * 0.78 <= center_y <= height * 0.93
            and 0.65 <= aspect_ratio <= 2.5
            and max(ys) < region.height - 1
            and len(component) / (component_width * component_height) >= 0.12
        ):
            return True
    return False


def detect_standalone_ellipsis_frame(image):
    """Detect three isolated dialogue-band dots without relying on OCR text."""
    if not isinstance(image, Image.Image):
        return False
    rgb = image.convert("RGB")
    width, height = rgb.size
    right = min(width, max(30, round(width * 0.12)))
    top = min(height - 1, round(height * 0.38))
    bottom = min(height, max(top + 1, round(height * 0.88)))
    pixels = rgb.load()
    bright = set()
    for y in range(top, bottom):
        for x in range(right):
            red, green, blue = pixels[x, y]
            if (
                min(red, green, blue) >= 150
                and max(red, green, blue) - min(red, green, blue) <= 45
            ):
                bright.add((x, y))
    if not 6 <= len(bright) <= 30:
        return False
    components = []
    remaining = set(bright)
    while remaining:
        pending = [remaining.pop()]
        component = []
        while pending:
            point = pending.pop()
            component.append(point)
            x, y = point
            for neighbor in (
                (x - 1, y - 1),
                (x, y - 1),
                (x + 1, y - 1),
                (x - 1, y),
                (x + 1, y),
                (x - 1, y + 1),
                (x, y + 1),
                (x + 1, y + 1),
            ):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    pending.append(neighbor)
        components.append(component)
    if len(components) != 3:
        return False
    centers = []
    for component in components:
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        if (
            not 2 <= len(component) <= 12
            or max(xs) - min(xs) > 3
            or max(ys) - min(ys) > 3
        ):
            return False
        centers.append((sum(xs) / len(xs), sum(ys) / len(ys)))
    centers.sort()
    gaps = [centers[index + 1][0] - centers[index][0] for index in range(2)]
    return max(center[1] for center in centers) - min(
        center[1] for center in centers
    ) <= 2.5 and all(4 <= gap <= 9 for gap in gaps)


def is_standalone_ellipsis_text(value):
    """Accept Unicode or ASCII ellipsis with arbitrary surrounding whitespace."""
    text = "".join(str(value).split())
    return text.replace("…", "...") == "..."


def ellipsis_speaker_hint(character, raw_text, story_resolver):
    """Recover an ellipsis nameplate only from checksum-bound story speakers."""
    observed = str(character or "Narrator").strip() or "Narrator"
    if observed.casefold() not in {"narrator", "unknown", "???"}:
        return observed
    prefix = str(raw_text or "").strip().casefold()
    speaker_names = getattr(story_resolver, "speaker_names", {})
    for name in sorted(set(speaker_names.values()), key=len, reverse=True):
        normalized = str(name).strip()
        if normalized and prefix.startswith(normalized.casefold()):
            return normalized
    return observed


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
    ellipsis_speaker_resolver=None,
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
    recognized_text = (
        "..." if detect_standalone_ellipsis_frame(frame.image) else result.text
    )
    recognized_character = result.character
    if recognized_text == "..." and ellipsis_speaker_resolver is not None:
        recognized_character = ellipsis_speaker_hint(
            result.character,
            result.text,
            ellipsis_speaker_resolver,
        )
    ocr_ms = (clock() - ocr_started) * 1000
    snapshot = DiagnosticSnapshot(
        image=frame.image,
        character=recognized_character or "Narrator",
        text=recognized_text,
        confidence=result.confidence,
        preprocessing_profile=result.profile,
        voice=(
            voice_resolver(recognized_character)
            if voice_resolver is not None
            else "Not loaded"
        ),
        capture_ms=frame.capture_ms,
        ocr_ms=ocr_ms,
        corrections=result.corrections,
    )
    if diagnostic_handler is not None:
        diagnostic_handler(snapshot)
    if (
        recognized_text != "..."
        and result.text
        and not result.is_confident(minimum_confidence)
    ):
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
    return recognized_character, recognized_text


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
