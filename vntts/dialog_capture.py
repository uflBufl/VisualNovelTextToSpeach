"""Screen capture, OCR, and one-shot dialog processing."""

import json
import os
import sys
import warnings
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from hashlib import blake2b
from pathlib import Path
from time import monotonic
from typing import Protocol, TypeAlias
from uuid import uuid4

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageFilter
from PIL.PngImagePlugin import PngInfo
from scipy.ndimage import label, maximum_filter

from vntts.capture_provenance import CaptureProvenance
from vntts.diagnostics import DiagnosticSnapshot
from vntts.dialog import is_empty, speak_dialog
from vntts.native_capture import NativeFrame
from vntts.ocr import (
    DialogRegion,
    OCRPreviewCancelled,
    OCRRegions,
    OCRResult,
    UncertainFrameRecorder,
    VoiceRegistry,
    default_minimum_ocr_confidence,
    detect_standalone_ellipsis_frame,
    get_dialog_region,
    resolve_ocr_regions,
)
from vntts.ocr_backend import OCRBackend, TesseractOCRBackend
from vntts.ocr_corrections import OCRCorrectionDictionary
from vntts.ocr_worker import recognize_dialog_image_result
from vntts.services.tts_engine import AudioPlaybackError, TTSError
from vntts.voices import VoiceManifestError
from vntts.window_capture import (
    DisplayCaptureTarget,
    WindowCaptureError,
    WindowCaptureTarget,
    ensure_screen_capture_supported,
)

default_screenshot_directory = Path("logs/screenshots")
PathInput: TypeAlias = str | os.PathLike[str]
Clock: TypeAlias = Callable[[], float]


class ScreenshotSettings(Protocol):
    @property
    def screenshot_directory(self) -> str: ...


class DialogVoiceRouter(Protocol):
    registry: VoiceRegistry

    def speak(self, character: str, text: str) -> object: ...


class ScreenCaptureError(RuntimeError):
    provenance: CaptureProvenance | None = None


class OCRError(RuntimeError):
    pass


class OCRUncertainError(OCRError):
    def __init__(self, result: OCRResult, minimum_confidence: float) -> None:
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
    ocr_regions: OCRRegions | None = None
    provenance: CaptureProvenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "ocr_regions", resolve_ocr_regions(self.image, self.ocr_regions)
        )


def get_screenshot_directory(settings: ScreenshotSettings | None = None) -> Path:
    if settings is not None:
        return Path(settings.screenshot_directory).expanduser()

    configured_directory = os.environ.get("VNTTS_SCREENSHOT_DIR")
    if not configured_directory:
        return default_screenshot_directory

    return Path(configured_directory)


def create_screenshot_path(screenshot_directory: PathInput) -> Path:
    screenshot_directory = Path(screenshot_directory)
    formatted_date = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return screenshot_directory / f"dialog-{formatted_date}-{uuid4().hex}.png"


def _capture_provenance(
    capture_id: str,
    run_id: str | None,
    started: float,
    capture_target: object,
    frame: NativeFrame | None,
    dialog_box: dict[str, int] | None,
    image: Image.Image | None,
    error: BaseException | None = None,
) -> CaptureProvenance:
    source = (
        capture_target if isinstance(capture_target, DisplayCaptureTarget) else None
    )
    window = (
        capture_target.last_window
        if isinstance(capture_target, WindowCaptureTarget)
        else None
    )
    return CaptureProvenance(
        capture_id=capture_id,
        run_id=run_id,
        started_monotonic=started,
        completed_monotonic=monotonic(),
        requested_window_title=(
            capture_target.window_title
            if isinstance(capture_target, WindowCaptureTarget)
            else None
        ),
        window_id=window.handle if window is not None else None,
        window_title=window.title if window is not None else None,
        owner_pid=window.process_id if window is not None else None,
        window_bounds=(
            dict(
                zip(
                    ("left", "top", "width", "height"),
                    frame.desktop_bounds,
                    strict=True,
                )
            )
            if frame
            else None
        ),
        capture_bounds=dialog_box,
        output_dimensions=image.size if image is not None else None,
        success=error is None,
        error_type=type(error).__name__ if error is not None else None,
        mechanism=frame.backend if frame else "native",
        scope=frame.scope
        if frame
        else (
            "selected_window"
            if isinstance(capture_target, WindowCaptureTarget)
            else "display"
        ),
        session_id=frame.session_id if frame else None,
        frame_sequence=frame.frame_sequence if frame else None,
        layout_epoch=frame.layout_epoch if frame else None,
        acquired_monotonic=frame.acquired_at + source.clock_offset
        if frame and source
        else None,
        source_monotonic=(
            frame.source_time + source.clock_offset
            if frame and source and frame.source_time is not None
            else None
        ),
        clock_uncertainty_ms=source.clock_uncertainty * 1000
        if frame and source
        else None,
    )


def capture_dialog(
    screenshot_directory: PathInput | None = None,
    *,
    save_screenshot: bool = True,
    region: DialogRegion | None = None,
    capture_target: object | None = None,
    run_id: str | None = None,
    provenance_handler: Callable[[CaptureProvenance], object] | None = None,
) -> tuple[Image.Image, Path | None]:
    capture_id = uuid4().hex
    started = monotonic()
    frame = None
    owned = capture_target is None
    target = DisplayCaptureTarget() if owned else capture_target
    dialog_box = None
    image = None

    def provenance(error: BaseException | None = None) -> CaptureProvenance:
        return _capture_provenance(
            capture_id,
            run_id,
            started,
            target,
            frame,
            dialog_box,
            image,
            error,
        )

    try:
        ensure_screen_capture_supported()
        if screenshot_directory is None:
            screenshot_directory = get_screenshot_directory()
        screenshot_directory = Path(screenshot_directory)
        if save_screenshot:
            screenshot_directory.mkdir(parents=True, exist_ok=True)

        if not isinstance(target, DisplayCaptureTarget):
            raise WindowCaptureError("Unsupported native capture source")
        frame = target.capture_native()
        x, y, width, height = frame.content_rect
        canonical = frame.to_image().crop((x, y, x + width, y + height))
        region = region or get_dialog_region()
        dialog_box = region.capture_box({"width": width, "height": height})
        image = region.crop(canonical)
    except Exception as error:
        raise _capture_failure(error, provenance(error), provenance_handler) from error
    finally:
        if owned and isinstance(target, DisplayCaptureTarget):
            target.close()

    record = provenance()
    image.info["capture_provenance"] = record.as_dict()
    output = None
    if save_screenshot:
        output = create_screenshot_path(screenshot_directory)
        metadata_json = json.dumps(record.as_dict(), sort_keys=True)
        png_metadata = PngInfo()
        png_metadata.add_text("capture_provenance", metadata_json)
        try:
            image.save(output, pnginfo=png_metadata)
        except Exception as error:
            record = provenance(error)
            image.info["capture_provenance"] = record.as_dict()
            raise _capture_failure(error, record, provenance_handler) from error
        _save_capture_sidecar(output, metadata_json)
    _notify_capture_provenance(provenance_handler, record)
    return image, output


def _save_capture_sidecar(output: Path, metadata_json: str) -> None:
    try:
        output.with_suffix(".capture.json").write_text(
            metadata_json + "\n", encoding="utf-8"
        )
    except OSError as error:
        # The PNG still embeds provenance when its optional sidecar cannot be saved.
        warnings.warn(
            f"Unable to save capture provenance sidecar: {type(error).__name__}",
            RuntimeWarning,
            stacklevel=2,
        )


def _capture_failure(
    error: BaseException,
    record: CaptureProvenance,
    handler: Callable[[CaptureProvenance], object] | None,
) -> ScreenCaptureError:
    failure = ScreenCaptureError(str(error))
    failure.provenance = record
    _notify_capture_provenance(handler, record)
    return failure


def _notify_capture_provenance(
    handler: Callable[[CaptureProvenance], object] | None, record: CaptureProvenance
) -> None:
    if handler is None:
        return
    try:
        handler(record)
    except Exception as error:
        warnings.warn(
            f"Unable to record capture provenance: {type(error).__name__}",
            RuntimeWarning,
            stacklevel=2,
        )


def recognize_screenshot_result(
    image: Image.Image,
    voice_registry: VoiceRegistry | None = None,
    minimum_confidence: float = default_minimum_ocr_confidence,
    ocr_language: str = "eng",
    correction_dictionary: OCRCorrectionDictionary | None = None,
    ocr_backend: OCRBackend | None = None,
    ocr_regions: OCRRegions | None = None,
    result_resolver: Callable[[OCRResult], OCRResult] | None = None,
    raw_result_observer: Callable[[OCRResult], None] | None = None,
) -> OCRResult:
    try:
        backend = ocr_backend or TesseractOCRBackend(recognize_dialog_image_result)
        if ocr_regions is None:
            result = backend.recognize(
                image,
                voice_registry,
                minimum_confidence=minimum_confidence,
                language=ocr_language,
            )
        else:
            result = backend.recognize(
                image,
                voice_registry,
                minimum_confidence=minimum_confidence,
                language=ocr_language,
                ocr_regions=ocr_regions,
            )
        if raw_result_observer is not None:
            try:
                raw_result_observer(result)
            except Exception:
                pass
        result = (
            correction_dictionary.correct_result(result)
            if correction_dictionary is not None
            else result
        )
        return result_resolver(result) if result_resolver is not None else result
    except OCRPreviewCancelled:
        raise
    except Exception as error:
        raise OCRError(str(error)) from error


def capture_live_frame(
    screenshot_directory: PathInput,
    capture_target: object | None = None,
    *,
    region: DialogRegion | None = None,
    clock: Clock = monotonic,
    ocr_regions: OCRRegions | None = None,
    run_id: str | None = None,
    provenance_handler: Callable[[CaptureProvenance], object] | None = None,
) -> CapturedDialogFrame:
    capture_started = clock()
    records: list[CaptureProvenance] = []

    def captured(record: CaptureProvenance) -> None:
        records.append(record)
        if provenance_handler is not None:
            _notify_capture_provenance(provenance_handler, record)

    image, _output = capture_dialog(
        screenshot_directory,
        save_screenshot=False,
        region=region,
        capture_target=capture_target,
        run_id=run_id,
        provenance_handler=captured,
    )
    return CapturedDialogFrame(
        image=image,
        capture_ms=(clock() - capture_started) * 1000,
        ocr_regions=ocr_regions,
        provenance=records[-1] if records else None,
    )


def _frame_image(frame: object) -> Image.Image:
    image = getattr(frame, "image", None)
    if not isinstance(image, Image.Image):
        raise TypeError("Dialog frame has no image")
    return image


def fingerprint_dialog_frame(frame: object) -> bytes:
    """Fingerprint bright dialogue glyphs while ignoring the animated backdrop."""
    grayscale = _frame_image(frame).convert("L")
    # Dialogue boxes in supported games are translucent, so hashing the whole
    # crop makes character animation and video behind the box look like new
    # dialogue. Keep only the bright glyph cores before downsampling. Using the
    # cores also avoids tiny anti-aliasing changes where text meets the moving
    # background while still preserving additions to typewriter text.
    width, height = grayscale.size

    def glyph_band(
        top: int,
        bottom: int,
        output_height: int,
        minimum_brightness: int,
        *,
        ignore_continue_indicator: bool = False,
        region_image: Image.Image | None = None,
    ) -> bytes:
        band = (
            grayscale.crop((0, top, width, bottom))
            if region_image is None
            else region_image.convert("L")
        )
        if ignore_continue_indicator:
            # Clear the chrome before measuring the band's brightest pixel;
            # otherwise a blinking indicator also changes the threshold used
            # for every dialogue glyph outside the ignored area.
            indicator_left = round(width * 0.92)
            indicator_top = max(0, round(height * 0.68) - top)
            band.paste(0, (indicator_left, indicator_top, width, bottom - top))
        brightest = band.getextrema()[1]
        assert isinstance(brightest, (int, float))
        # Fully covered glyph pixels keep the same value even when the
        # anti-aliased edge is composited over a changing background.
        glyph_threshold = max(minimum_brightness, brightest)
        mask = band.point(
            tuple(255 if value >= glyph_threshold else 0 for value in range(256))
        )
        mask = Image.fromarray(
            maximum_filter(np.asarray(mask), size=5, mode="nearest")
        ).resize(
            (256, output_height),
            Image.Resampling.LANCZOS,
        )
        # Collapse resize ringing so sub-pixel capture noise cannot invalidate
        # the cache.
        glyph_bytes: bytes = mask.point(
            tuple(255 if value >= 32 else 0 for value in range(256))
        ).tobytes()
        return glyph_bytes

    # Speaker labels are often dimmer than dialogue, so fingerprint the upper
    # label and lower text bands independently. The overlap accommodates games
    # whose first dialogue line starts unusually high.
    regions = getattr(frame, "ocr_regions", None)
    if isinstance(regions, OCRRegions):
        source = _frame_image(frame)
        label = glyph_band(
            0, height, 27, 180, region_image=regions.speaker.crop(source)
        )
        dialog = glyph_band(
            0,
            height,
            45,
            200,
            region_image=regions.dialogue.crop(_without_continue_indicator(source)),
        )
        return blake2b(label + dialog, digest_size=16).digest()
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


def _dialog_equivalence_boxes(
    previous: object, current: object
) -> list[tuple[int, int, int, int]]:
    width, height = _frame_image(previous).size
    monitor = {"left": 0, "top": 0, "width": width, "height": height}
    region_pairs = []
    for frame in (previous, current):
        regions = getattr(frame, "ocr_regions", None)
        region_pairs.append(
            (regions.speaker, regions.dialogue)
            if isinstance(regions, OCRRegions)
            else (DialogRegion(0, 0, 1, 0.36), DialogRegion(0, 0.25, 1, 0.75))
        )
    boxes = []
    for first, second in zip(*region_pairs, strict=True):
        a, b = first.capture_box(monitor), second.capture_box(monitor)
        boxes.append(
            (
                min(a["left"], b["left"]),
                min(a["top"], b["top"]),
                max(a["left"] + a["width"], b["left"] + b["width"]),
                max(a["top"] + a["height"], b["top"] + b["height"]),
            )
        )

    return boxes


def _dialog_equivalence_bands(
    frame: object, boxes: list[tuple[int, int, int, int]]
) -> tuple[NDArray[np.int16], ...]:
    image = _frame_image(frame).convert("L")
    for point in _dialog_completion_cue_pixels(frame):
        image.putpixel(point, 0)
    # Padding retains edge glyphs when the comparison translates a whole band.
    return tuple(
        np.pad(np.asarray(image.crop(box), dtype=np.int16), 2) for box in boxes
    )


def _dialog_band_equivalent(
    before: NDArray[np.int16], after: NDArray[np.int16]
) -> bool:
    difference = np.abs(before - after)
    thresholds = zip(
        (160, max(160, int(before.max()) - 20)),
        (160, max(160, int(after.max()) - 20)),
        strict=True,
    )
    for first, second in thresholds:
        changed = (before >= first) != (after >= second)
        if not changed.any():
            continue
        if np.any(difference[changed] > 4):
            return False
        # Only tiny intensity crossings beside stable glyph cores are edge noise;
        # a new isolated punctuation pixel must still invalidate the comparison.
        stable_core = (before >= first + 4) & (after >= second + 4) & (difference <= 4)
        supported = maximum_filter(stable_core, size=3, mode="constant")
        if not supported[changed].all():
            return False
    return True


def _dialog_frames_equivalent(
    previous: object, current: object, maximum_translation: int
) -> bool:
    before, after = _frame_image(previous), _frame_image(current)
    if before.size != after.size:
        return False
    if not dialog_glyphs_visible(previous) or not dialog_glyphs_visible(current):
        return False

    boxes = _dialog_equivalence_boxes(previous, current)
    before_bands = _dialog_equivalence_bands(previous, boxes)
    after_bands = _dialog_equivalence_bands(current, boxes)
    if not (before_bands[-1] >= 160).any() or not (after_bands[-1] >= 160).any():
        return False
    offsets = sorted(range(-maximum_translation, maximum_translation + 1), key=abs)
    for dy in offsets:
        for dx in offsets:
            if all(
                _dialog_band_equivalent(np.roll(left, (dy, dx), axis=(0, 1)), right)
                for left, right in zip(before_bands, after_bands, strict=True)
            ):
                return True
    return False


def dialog_frames_equivalent(previous: object, current: object) -> bool:
    """Compare glyphs with one shared translation bounded by two pixels."""
    return _dialog_frames_equivalent(previous, current, 2)


def dialog_render_frames_equivalent(previous: object, current: object) -> bool:
    """Compare a fixed render anchor, allowing only supported intensity noise."""
    return _dialog_frames_equivalent(previous, current, 0)


def fingerprint_dialog_render_activity(frame: object) -> bytes:
    """Fingerprint high-fidelity dialogue glyph activity, not screen identity.

    The ordinary dialogue fingerprint is intentionally compact and may map a
    nearly complete typewriter prefix to the same identity as the full line.
    Render completion needs the opposite tradeoff: retain small new glyphs, but
    ignore the portrait, continue indicator and most animated backdrop pixels.
    """
    grayscale = _frame_image(frame).convert("L")
    width, height = grayscale.size
    left = min(width - 1, max(0, round(width * 0.18)))
    right = max(left + 1, min(width, round(width * 0.92)))
    top = min(height - 1, max(0, round(height * 0.25)))
    regions = getattr(frame, "ocr_regions", None)
    band = (
        regions.dialogue.crop(_without_continue_indicator(grayscale))
        if isinstance(regions, OCRRegions)
        else grayscale.crop((left, top, right, height))
    )
    brightest = band.getextrema()[1]
    assert isinstance(brightest, (int, float))
    threshold = max(205, brightest - 20)
    mask = band.point(
        tuple(255 if value >= threshold else 0 for value in range(256))
    ).filter(ImageFilter.MaxFilter(3))
    if mask.width > 1024:
        output_height = max(1, round(mask.height * 1024 / mask.width))
        mask = mask.resize((1024, output_height), Image.Resampling.NEAREST)
    return blake2b(mask.tobytes(), digest_size=16).digest()


def dialog_glyphs_visible(frame: object) -> bool:
    """Return whether the dialogue band has plausible bright text pixels.

    This deliberately remains a cheap, OCR-free fail-closed gate. Three pixels
    are enough for an anti-aliased ellipsis in the smallest supported test font,
    while a mostly bright crop is treated as a popup or calibration failure
    rather than dialogue.
    """
    grayscale = _frame_image(frame).convert("L")
    width, height = grayscale.size
    dialog_top = min(height - 1, round(height * 0.25))
    dialog = grayscale.crop((0, dialog_top, width, height))
    regions = getattr(frame, "ocr_regions", None)
    if isinstance(regions, OCRRegions):
        dialog = regions.dialogue.crop(grayscale)
    histogram: list[int] = dialog.histogram()
    bright_pixels = sum(histogram[160:])
    dialog_width: int = dialog.width
    dialog_height: int = dialog.height
    pixels = max(1, dialog_width * dialog_height)
    return 3 <= bright_pixels <= round(pixels * 0.25)


def _pixel_components(mask: NDArray[np.bool_]) -> list[list[tuple[int, int]]]:
    # Diagonal neighbors belong to the same glyph, just like horizontal ones.
    labels, count = label(mask, structure=np.ones((3, 3)))
    components: list[list[tuple[int, int]]] = [[] for _ in range(count)]
    ys, xs = np.nonzero(mask)
    for x, y, component in zip(xs, ys, labels[ys, xs], strict=True):
        components[int(component) - 1].append((int(x), int(y)))
    return components


def _dialog_completion_cue_pixels(frame: object) -> list[tuple[int, int]]:
    if not dialog_glyphs_visible(frame):
        return []
    grayscale = _frame_image(frame).convert("L")
    width, height = grayscale.size
    left = min(width - 1, round(width * 0.94))
    top = min(height - 1, round(height * 0.68))
    bottom = min(height, max(top + 1, round(height * 0.96)))
    region = grayscale.crop((left, top, width, bottom))
    components = _pixel_components(np.asarray(region) >= 150)

    minimum_component_pixels = max(4, round(width / 240))
    maximum_width = max(6, round(width * 0.02))
    maximum_height = max(6, round(height * 0.12))
    for component in components:
        xs = [point[0] for point in component]
        ys = [point[1] for point in component]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        component_width = max_x - min_x + 1
        component_height = max_y - min_y + 1
        center_x = left + (min_x + max_x) / 2
        center_y = top + (min_y + max_y) / 2
        aspect_ratio = component_width / component_height
        if (
            minimum_component_pixels <= len(component) <= 500
            and 3 <= component_width <= maximum_width
            and 3 <= component_height <= maximum_height
            and width * 0.95 <= center_x <= width * 0.99
            and height * 0.78 <= center_y <= height * 0.93
            and 0.65 <= aspect_ratio <= 2.5
            and max_y < region.height - 1
            and len(component) / (component_width * component_height) >= 0.12
        ):
            return [(left + x, top + y) for x, y in component]
    return []


def dialog_completion_cue_visible(frame: object) -> bool:
    """Detect the game's small downward continue indicator, not dialogue text."""
    return bool(_dialog_completion_cue_pixels(frame))


def _without_continue_indicator(image: Image.Image) -> Image.Image:
    image = image.copy()
    width, height = image.size
    image.paste(0, (round(width * 0.92), round(height * 0.68), width, height))
    return image


def is_standalone_ellipsis_text(value: object) -> bool:
    """Accept Unicode or ASCII ellipsis with arbitrary surrounding whitespace."""
    text = "".join(str(value).split())
    return text.replace("…", "...") == "..."


def ellipsis_speaker_hint(
    character: str | None,
    raw_text: str | None,
    story_resolver: object,
) -> str:
    """Recover an ellipsis nameplate only from checksum-bound story speakers."""
    observed = str(character or "Narrator").strip() or "Narrator"
    if observed.casefold() not in {"narrator", "unknown", "???"}:
        return observed
    prefix = str(raw_text or "").strip().casefold()
    speaker_names = getattr(story_resolver, "speaker_names", {})
    if not isinstance(speaker_names, Mapping):
        return observed
    for name in sorted(set(speaker_names.values()), key=len, reverse=True):
        normalized = str(name).strip()
        if normalized and prefix.startswith(normalized.casefold()):
            return normalized
    return observed


def recognize_live_frame(
    frame: object,
    voice_registry: VoiceRegistry | None = None,
    minimum_confidence: float = default_minimum_ocr_confidence,
    uncertain_handler: Callable[[OCRResult, float], object] | None = None,
    uncertain_frame_recorder: UncertainFrameRecorder | None = None,
    diagnostic_handler: Callable[[DiagnosticSnapshot], object] | None = None,
    voice_resolver: Callable[[str], str] | None = None,
    ocr_language: str = "eng",
    correction_dictionary: OCRCorrectionDictionary | None = None,
    ellipsis_speaker_resolver: object | None = None,
    *,
    clock: Clock = monotonic,
    result_resolver: Callable[[OCRResult], OCRResult] | None = None,
    raw_result_observer: Callable[[OCRResult], None] | None = None,
) -> tuple[str | None, str]:
    image = _frame_image(frame)
    capture_ms = getattr(frame, "capture_ms", None)
    if not isinstance(capture_ms, (int, float)) or isinstance(capture_ms, bool):
        raise TypeError("Dialog frame has no capture timing")
    ocr_started = clock()
    result = recognize_screenshot_result(
        image,
        voice_registry,
        minimum_confidence,
        ocr_language,
        correction_dictionary,
        ocr_regions=getattr(frame, "ocr_regions", None),
        result_resolver=result_resolver,
        raw_result_observer=raw_result_observer,
    )
    regions = getattr(frame, "ocr_regions", None)
    dialogue_image = (
        regions.dialogue.crop(image) if isinstance(regions, OCRRegions) else image
    )
    recognized_text = (
        "..."
        if detect_standalone_ellipsis_frame(
            dialogue_image, dialogue_only=isinstance(regions, OCRRegions)
        )
        else result.text
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
        image=image,
        character=recognized_character or "Narrator",
        text=recognized_text,
        confidence=result.confidence,
        preprocessing_profile=result.profile,
        voice=(
            voice_resolver(recognized_character)
            if voice_resolver is not None
            else "Not loaded"
        ),
        capture_ms=capture_ms,
        ocr_ms=ocr_ms,
        corrections=result.corrections,
        processed_image=result.processed_image,
        speaker_image=result.speaker_image,
        speaker_confidence=result.speaker_confidence,
        dialogue_confidence=result.dialogue_confidence,
        speaker_note=result.speaker_note,
        capture_provenance=getattr(image, "info", {}).get("capture_provenance"),
    )
    if diagnostic_handler is not None:
        diagnostic_handler(snapshot)
    if (
        (
            recognized_text != "..."
            or bool(result.speaker_note)
            or (
                result.speaker_confidence is not None
                and result.speaker_confidence < minimum_confidence
            )
        )
        and (recognized_text or result.speaker_note)
        and not result.is_confident(minimum_confidence)
    ):
        if uncertain_frame_recorder is not None:
            uncertain_frame_recorder.record(
                image,
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
    image: Image.Image,
    voice_registry: VoiceRegistry | None = None,
    minimum_confidence: float = default_minimum_ocr_confidence,
    ocr_language: str = "eng",
    correction_dictionary: OCRCorrectionDictionary | None = None,
) -> tuple[str, str]:
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
    screenshot_directory: PathInput,
    voice_registry: VoiceRegistry | None = None,
    capture_target: object | None = None,
    minimum_confidence: float = default_minimum_ocr_confidence,
    *,
    save_screenshot: bool = False,
    diagnostic_handler: Callable[[DiagnosticSnapshot], object] | None = None,
    voice_resolver: Callable[[str], str] | None = None,
    clock: Clock = monotonic,
    ocr_language: str = "eng",
    correction_dictionary: OCRCorrectionDictionary | None = None,
    region: DialogRegion | None = None,
    ocr_regions: OCRRegions | None = None,
    result_resolver: Callable[[OCRResult], OCRResult] | None = None,
    capture_context: Callable[[], AbstractContextManager[None]] | None = None,
) -> tuple[Image.Image, Path | None, OCRResult]:
    with capture_context() if capture_context is not None else nullcontext():
        capture_started = clock()
        image, output = capture_dialog(
            screenshot_directory,
            save_screenshot=save_screenshot,
            region=region,
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
        ocr_regions=ocr_regions,
        result_resolver=result_resolver,
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
        processed_image=result.processed_image,
        speaker_image=result.speaker_image,
        speaker_confidence=result.speaker_confidence,
        dialogue_confidence=result.dialogue_confidence,
        speaker_note=result.speaker_note,
        capture_provenance=getattr(image, "info", {}).get("capture_provenance"),
    )
    if diagnostic_handler is not None:
        diagnostic_handler(snapshot)
    return image, output, result


def read_dialog(
    voice_router: DialogVoiceRouter,
    screenshot_directory: PathInput,
    capture_target: object | None = None,
    speech_handler: Callable[[str, str], object] | None = None,
    minimum_confidence: float = default_minimum_ocr_confidence,
    uncertain_frame_recorder: UncertainFrameRecorder | None = None,
    diagnostic_handler: Callable[[DiagnosticSnapshot], object] | None = None,
    voice_resolver: Callable[[str], str] | None = None,
    ocr_language: str = "eng",
    correction_dictionary: OCRCorrectionDictionary | None = None,
    region: DialogRegion | None = None,
    ocr_regions: OCRRegions | None = None,
    result_resolver: Callable[[OCRResult], OCRResult] | None = None,
) -> None:
    image, output, result = analyze_dialog_snapshot(
        screenshot_directory,
        voice_router.registry,
        capture_target=capture_target,
        region=region,
        minimum_confidence=minimum_confidence,
        save_screenshot=True,
        diagnostic_handler=diagnostic_handler,
        voice_resolver=voice_resolver,
        ocr_language=ocr_language,
        correction_dictionary=correction_dictionary,
        ocr_regions=ocr_regions,
        result_resolver=result_resolver,
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
    voice_router: DialogVoiceRouter,
    screenshot_directory: PathInput,
    error_handler: Callable[[Exception], object] | None = None,
    capture_target: object | None = None,
    speech_handler: Callable[[str, str], object] | None = None,
    minimum_confidence: float = default_minimum_ocr_confidence,
    uncertain_frame_recorder: UncertainFrameRecorder | None = None,
    diagnostic_handler: Callable[[DiagnosticSnapshot], object] | None = None,
    voice_resolver: Callable[[str], str] | None = None,
    ocr_language: str = "eng",
    correction_dictionary: OCRCorrectionDictionary | None = None,
    region: DialogRegion | None = None,
    ocr_regions: OCRRegions | None = None,
    result_resolver: Callable[[OCRResult], OCRResult] | None = None,
) -> None:
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
            region,
            ocr_regions,
            result_resolver,
        )
    except Exception as error:
        (error_handler or report_runtime_error)(error)


def format_runtime_error(error: BaseException) -> str:
    if isinstance(error, WindowCaptureError) or (
        isinstance(error, ScreenCaptureError)
        and isinstance(error.__cause__, WindowCaptureError)
    ):
        message = f"Screen capture unavailable: {error}"
    elif isinstance(error, ScreenCaptureError):
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


def report_runtime_error(error: BaseException) -> None:
    print(format_runtime_error(error), file=sys.stderr)
