"""Screen capture, OCR, and one-shot dialog processing."""

import json
import os
import sys
import warnings
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import blake2b
from pathlib import Path
from time import monotonic
from typing import Protocol, TypeAlias
from uuid import uuid4

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from scipy.ndimage import label, maximum_filter

from vntts.audio_cache import BoundedCache
from vntts.capture_provenance import CaptureProvenance
from vntts.diagnostics import DiagnosticSnapshot
from vntts.dialog import (
    is_empty,
    is_symbol_only_text,
    ocr_texts_equivalent,
    speak_dialog,
    symbol_text_key,
)
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
    canonical_image: Image.Image | None = None
    capture_region: DialogRegion | None = None
    layout_id: str = "dialogue"
    layout_kind: str = "dialogue"
    navigation_allowed: bool = True
    recognized_result: OCRResult | None = None
    layout_detection_ms: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ocr_regions",
            resolve_ocr_regions(self.image, self.ocr_regions)
            if self.layout_kind == "dialogue"
            else None,
        )


class DialogLayoutSession:
    """Retain detected geometry until the ordered capture context changes."""

    def __init__(self) -> None:
        self._context: object = object()
        self._regions: OCRRegions | None = None

    def apply(
        self, frame: CapturedDialogFrame, *, context: object = None
    ) -> CapturedDialogFrame:
        source = frame.provenance
        signature = (
            context,
            frame.image.size,
            None
            if source is None
            else (
                source.run_id,
                source.window_id,
                source.owner_pid,
                source.session_id,
                source.layout_epoch,
            ),
        )
        if signature != self._context:
            self._context = signature
            self._regions = None
        regions = frame.ocr_regions
        if regions is not None and regions._resolved:
            self._regions = regions
        elif self._regions is not None:
            return replace(frame, ocr_regions=self._regions)
        return frame


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
    canonical_handler: Callable[[Image.Image], None] | None = None,
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
        if canonical_handler is not None:
            canonical_handler(canonical)
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
        return _resolve_ocr_result(
            result, correction_dictionary, result_resolver, raw_result_observer
        )
    except OCRPreviewCancelled:
        raise
    except Exception as error:
        raise OCRError(str(error)) from error


def _resolve_ocr_result(
    result: OCRResult,
    corrections: OCRCorrectionDictionary | None,
    resolver: Callable[[OCRResult], OCRResult] | None,
    observer: Callable[[OCRResult], None] | None,
) -> OCRResult:
    if observer is not None:
        try:
            observer(result)
        except Exception:
            pass
    if corrections is not None:
        result = corrections.correct_result(result)
    return resolver(result) if resolver is not None else result


def capture_live_frame(
    screenshot_directory: PathInput,
    capture_target: object | None = None,
    *,
    region: DialogRegion | None = None,
    clock: Clock = monotonic,
    ocr_regions: OCRRegions | None = None,
    run_id: str | None = None,
    provenance_handler: Callable[[CaptureProvenance], object] | None = None,
    layout_session: DialogLayoutSession | None = None,
    layout_context: object = None,
) -> CapturedDialogFrame:
    capture_started = clock()
    records: list[CaptureProvenance] = []
    canonical_frames: list[Image.Image] = []

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
        canonical_handler=canonical_frames.append,
    )
    frame = CapturedDialogFrame(
        image=image,
        capture_ms=(clock() - capture_started) * 1000,
        ocr_regions=ocr_regions,
        provenance=records[-1] if records else None,
        canonical_image=canonical_frames[-1] if canonical_frames else None,
        capture_region=region or get_dialog_region(),
    )
    return (
        frame
        if layout_session is None
        else layout_session.apply(
            frame,
            context=(run_id, capture_target, region, ocr_regions, layout_context),
        )
    )


def _frame_image(frame: object) -> Image.Image:
    image = getattr(frame, "image", None)
    if not isinstance(image, Image.Image):
        raise TypeError("Dialog frame has no image")
    return image


def fingerprint_dialog_frame(frame: object) -> bytes:
    """Fingerprint bright dialogue glyphs while ignoring the animated backdrop."""
    if getattr(frame, "layout_kind", "dialogue") != "dialogue":
        result = getattr(frame, "recognized_result", None)
        text = result.text if isinstance(result, OCRResult) else ""
        return blake2b(text.encode("utf-8"), digest_size=16).digest()
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


class DialogFrameEquivalence:
    """Compare admitted dialogue identities within their original frame/run/owner."""

    def __init__(
        self,
        context: Callable[[], tuple[str, str | None] | None],
    ) -> None:
        self.context = context
        self._results: BoundedCache[
            int, tuple[object, tuple[str, str | None], str, str]
        ] = BoundedCache(8)

    def record_admitted(
        self,
        frame: object,
        character: str,
        text: str,
        context: tuple[str, str | None],
    ) -> None:
        if text.strip():
            self._results.put(id(frame), (frame, context, character, text))

    def bind_route(self, frame: object, context: tuple[str, str | None]) -> None:
        evidence = self._results.get(id(frame))
        if (
            evidence is not None
            and evidence[0] is frame
            and evidence[1][0] == context[0]
        ):
            self._results.put(id(frame), (frame, context, evidence[2], evidence[3]))

    def __call__(self, previous: object, current: object) -> bool:
        # Touch the fixed route anchor even while the latest frame lacks OCR.
        before = self._results.get(id(previous))
        after = self._results.get(id(current))
        context = self.context()
        if (
            context is not None
            and before is not None
            and after is not None
            and before[0] is previous
            and after[0] is current
            and before[1] == after[1] == context
        ):
            equivalent = ocr_texts_equivalent(
                before[2], after[2]
            ) and ocr_texts_equivalent(before[3], after[3], punctuation_fallback=True)
            return equivalent and self.context() == context
        return False


def dialog_glyphs_visible(frame: object) -> bool:
    """Return whether the dialogue band has plausible bright text pixels.

    This deliberately remains a cheap, OCR-free fail-closed gate. Three pixels
    are enough for an anti-aliased ellipsis in the smallest supported test font,
    while a mostly bright crop is treated as a popup or calibration failure
    rather than dialogue.
    """
    if getattr(frame, "layout_kind", "dialogue") != "dialogue":
        result = getattr(frame, "recognized_result", None)
        return isinstance(result, OCRResult) and bool(result.text.strip())
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
    return getattr(frame, "navigation_allowed", True) and bool(
        _dialog_completion_cue_pixels(frame)
    )


def _without_continue_indicator(image: Image.Image) -> Image.Image:
    image = image.copy()
    width, height = image.size
    image.paste(0, (round(width * 0.92), round(height * 0.68), width, height))
    return image


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
    precomputed = getattr(frame, "recognized_result", None)
    result = (
        _resolve_ocr_result(
            precomputed, correction_dictionary, result_resolver, raw_result_observer
        )
        if isinstance(precomputed, OCRResult)
        else recognize_screenshot_result(
            image,
            voice_registry,
            minimum_confidence,
            ocr_language,
            correction_dictionary,
            ocr_regions=getattr(frame, "ocr_regions", None),
            result_resolver=result_resolver,
            raw_result_observer=raw_result_observer,
        )
    )
    regions = getattr(frame, "ocr_regions", None)
    dialogue_image = (
        regions.dialogue.crop(image) if isinstance(regions, OCRRegions) else image
    )
    visual_ellipsis = getattr(
        frame, "layout_kind", "dialogue"
    ) == "dialogue" and detect_standalone_ellipsis_frame(
        dialogue_image, dialogue_only=isinstance(regions, OCRRegions)
    )
    recognized_text = (
        "..."
        if visual_ellipsis and not is_symbol_only_text(result.text)
        else result.text
    )
    visual_text_confirmed = (
        visual_ellipsis and symbol_text_key(recognized_text) == "..."
    )
    recognized_character = result.character
    if recognized_text == "..." and ellipsis_speaker_resolver is not None:
        recognized_character = ellipsis_speaker_hint(
            result.character,
            result.text,
            ellipsis_speaker_resolver,
        )
    ocr_ms = (clock() - ocr_started) * 1000 + getattr(frame, "layout_detection_ms", 0.0)
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
            not visual_text_confirmed
            or bool(result.speaker_note)
            or (
                result.speaker_confidence is not None
                and result.speaker_confidence < minimum_confidence
            )
        )
        and (recognized_text or result.speaker_note)
        and not result.is_readable(minimum_confidence)
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
    if result.text and not result.is_readable(minimum_confidence):
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
    frame_selector: Callable[[CapturedDialogFrame], CapturedDialogFrame] | None = None,
) -> tuple[Image.Image, Path | None, OCRResult]:
    canonical: list[Image.Image] = []
    provenance: list[CaptureProvenance] = []
    with capture_context() if capture_context is not None else nullcontext():
        capture_started = clock()
        image, output = capture_dialog(
            screenshot_directory,
            save_screenshot=save_screenshot,
            region=region,
            capture_target=capture_target,
            canonical_handler=canonical.append if frame_selector is not None else None,
            provenance_handler=provenance.append
            if frame_selector is not None
            else None,
        )
        capture_ms = (clock() - capture_started) * 1000

    ocr_started = clock()
    selected = None
    if frame_selector is not None:
        selected = frame_selector(
            CapturedDialogFrame(
                image,
                capture_ms,
                ocr_regions,
                provenance=provenance[-1] if provenance else None,
                canonical_image=canonical[-1] if canonical else None,
                capture_region=region or get_dialog_region(),
            )
        )
        image, ocr_regions = selected.image, selected.ocr_regions
        if output is not None and selected.layout_kind == "caption":
            metadata = json.dumps(
                image.info.get("capture_provenance", {}), sort_keys=True
            )
            png_metadata = PngInfo()
            png_metadata.add_text("capture_provenance", metadata)
            image.save(output, pnginfo=png_metadata)
            _save_capture_sidecar(output, metadata)
    precomputed = selected.recognized_result if selected is not None else None
    if selected is not None and selected.layout_kind == "pending":
        precomputed = OCRResult("Narrator", "", 0, "layout-pending", 0)
    result = (
        _resolve_ocr_result(precomputed, correction_dictionary, result_resolver, None)
        if precomputed is not None
        else recognize_screenshot_result(
            image,
            voice_registry,
            minimum_confidence,
            ocr_language,
            correction_dictionary,
            ocr_regions=ocr_regions,
            result_resolver=result_resolver,
        )
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
    frame_selector: Callable[[CapturedDialogFrame], CapturedDialogFrame] | None = None,
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
        frame_selector=frame_selector,
    )
    if result.text and not result.is_readable(minimum_confidence):
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
    frame_selector: Callable[[CapturedDialogFrame], CapturedDialogFrame] | None = None,
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
            frame_selector,
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
