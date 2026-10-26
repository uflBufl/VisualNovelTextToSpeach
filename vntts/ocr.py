import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Protocol, Self, TypeVar
from uuid import uuid4

import numpy as np
import pytesseract
from durable_file import atomic_write_json
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from pytesseract import pytesseract as pytesseract_runtime
from scipy.ndimage import find_objects, label, maximum_filter, minimum_filter
from vntts_artifacts.atomic_io import atomic_output_group

from vntts.dialog import (
    MAX_CHARACTER_NAME_LENGTH,
    is_probable_character_name,
    parse_dialog,
)
from vntts.json_types import decode_json
from vntts.ocr_review import OCR_REVIEW_SCHEMA_VERSION
from vntts.path_safety import read_regular_text
from vntts.tesseract_config import require_sauvola_cli, tesseract_config
from vntts.versioned_json import write_versioned_json

default_dialog_region_file = Path("~/.config/vntts/dialog-region.json").expanduser()
default_minimum_ocr_confidence = 60.0

OcrData = Mapping[str, Sequence[object]]


class VoiceRegistry(Protocol):
    def resolve_closest_character(self, text: str) -> str | None: ...


OcrTextRecognizer = Callable[..., str]
OcrDataRecognizer = Callable[..., OcrData]
OcrCombinedRecognizer = Callable[
    [Image.Image, str, Callable[[], float | None]], tuple[str, OcrData]
]
_OcrValue = TypeVar("_OcrValue")


class OCRPreviewCancelled(RuntimeError):
    """The user cancelled a pending or active OCR preview."""


def _call_ocr(
    recognizer: Callable[..., _OcrValue],
    image: Image.Image,
    remaining_timeout: Callable[[], float | None],
    **options: object,
) -> _OcrValue:
    timeout = remaining_timeout()
    if timeout is not None:
        options["timeout"] = timeout
    return recognizer(image, **options)


def _ocr_budget(
    timeout: float | None, cancelled: Callable[[], bool] | None
) -> Callable[[], float | None]:
    if timeout is not None:
        try:
            finite = math.isfinite(timeout)
        except OverflowError:
            finite = False
        if not finite or timeout <= 0:
            raise ValueError("OCR timeout must be positive and finite")
    deadline = monotonic() + timeout if timeout is not None else None

    def remaining() -> float | None:
        if cancelled is not None and cancelled():
            raise OCRPreviewCancelled("OCR preview cancelled")
        if deadline is None:
            return None
        seconds = deadline - monotonic()
        if seconds <= 0:
            raise TimeoutError("OCR preview timed out")
        return seconds

    return remaining


def configure_tesseract_process_environment() -> None:
    """Limit Tesseract without globally throttling PyTorch or other runtimes."""
    current = pytesseract_runtime.subprocess_args
    if getattr(current, "_vntts_limited_omp", False):
        return

    def subprocess_args(include_stdout: bool = True) -> dict[str, object]:
        arguments: dict[str, object] = current(include_stdout=include_stdout)
        environment = os.environ.copy()
        current_environment = arguments.get("env")
        if isinstance(current_environment, Mapping):
            environment.update(
                (key, value)
                for key, value in current_environment.items()
                if isinstance(key, str) and isinstance(value, str)
            )
        environment["OMP_THREAD_LIMIT"] = "1"
        arguments["env"] = environment
        return arguments

    setattr(subprocess_args, "_vntts_limited_omp", True)
    pytesseract_runtime.subprocess_args = subprocess_args


configure_tesseract_process_environment()


@dataclass(frozen=True)
class DialogRegion:
    left: float
    top: float
    width: float
    height: float

    def __post_init__(self) -> None:
        values = (self.left, self.top, self.width, self.height)
        if not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
            for value in values
        ):
            raise ValueError("Dialog region values must be finite numbers")
        if self.left < 0 or self.top < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("Dialog region values must be positive and normalized")
        if self.left + self.width > 1 or self.top + self.height > 1:
            raise ValueError("Dialog region must fit inside the screen")

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        if not isinstance(value, Mapping):
            raise ValueError("dialog_region must be an object")
        coordinates: list[float] = []
        for name in ("left", "top", "width", "height"):
            coordinate = value.get(name)
            if not isinstance(coordinate, (int, float)):
                raise ValueError(f"dialog_region {name} must be a number")
            coordinates.append(coordinate)
        return cls(*coordinates)

    def crop(self, image: Image.Image) -> Image.Image:
        return image.crop(self._pixel_box(*image.size))

    def capture_box(self, monitor: Mapping[str, int]) -> dict[str, int]:
        monitor_left = monitor.get("left", 0)
        monitor_top = monitor.get("top", 0)
        monitor_width = monitor["width"]
        monitor_height = monitor["height"]
        left, top, right, bottom = self._pixel_box(monitor_width, monitor_height)
        return {
            "left": monitor_left + left,
            "top": monitor_top + top,
            "width": right - left,
            "height": bottom - top,
        }

    def _pixel_box(self, width: int, height: int) -> tuple[int, int, int, int]:
        if width <= 0 or height <= 0:
            raise ValueError("Capture dimensions must be positive")
        left = min(width - 1, round(self.left * width))
        top = min(height - 1, round(self.top * height))
        right = min(width, max(left + 1, round((self.left + self.width) * width)))
        bottom = min(height, max(top + 1, round((self.top + self.height) * height)))
        return left, top, right, bottom

    def to_json(self) -> dict[str, float]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


default_dialog_region = DialogRegion(0.0, 0.68, 1.0, 0.32)


@dataclass(frozen=True)
class OCRRegions:
    """Independent OCR rectangles normalized inside one captured image."""

    speaker: DialogRegion
    dialogue: DialogRegion

    def __post_init__(self) -> None:
        if not isinstance(self.speaker, DialogRegion) or not isinstance(
            self.dialogue, DialogRegion
        ):
            raise ValueError("OCR regions must contain speaker and dialogue rectangles")
        if max(self.speaker.left, self.dialogue.left) < min(
            self.speaker.left + self.speaker.width,
            self.dialogue.left + self.dialogue.width,
        ) and max(self.speaker.top, self.dialogue.top) < min(
            self.speaker.top + self.speaker.height,
            self.dialogue.top + self.dialogue.height,
        ):
            raise ValueError("Speaker and dialogue OCR regions must not overlap")

    @classmethod
    def from_mapping(cls, value: object) -> "OCRRegions":
        if not isinstance(value, Mapping):
            raise ValueError("ocr_regions must be an object")

        return cls(
            DialogRegion.from_mapping(value.get("speaker")),
            DialogRegion.from_mapping(value.get("dialogue")),
        )

    def to_json(self) -> dict[str, dict[str, float]]:
        return {"speaker": self.speaker.to_json(), "dialogue": self.dialogue.to_json()}


def resolve_ocr_regions(
    image: Image.Image, configured: OCRRegions | None = None
) -> OCRRegions | None:
    """Follow an unambiguous thin divider; retain calibration on other layouts."""
    width, height = image.size
    if width < 64 or height < 24:
        return configured
    left, right = 0, width
    if configured is not None:
        left = min(
            configured.speaker._pixel_box(width, height)[0],
            configured.dialogue._pixel_box(width, height)[0],
        )
        right = max(
            configured.speaker._pixel_box(width, height)[2],
            configured.dialogue._pixel_box(width, height)[2],
        )
    divider = _dialogue_divider(
        np.asarray(image.convert("L"), dtype=np.float32), left, right
    )
    if divider is None:
        return configured
    line_left, line_right, band_top, band_bottom = divider
    padding = max(4, round(width * 0.015))
    name_left, name_right = (
        max(0, line_left - padding),
        min(width, line_right + padding),
    )
    text_left, text_right = name_left, name_right
    text_bottom = height
    if configured is not None:
        text_left, _, text_right, text_bottom = configured.dialogue._pixel_box(
            width, height
        )
    name_top = max(0, band_top - max(8, round(height * 0.35)))
    name_bottom = band_top
    if name_bottom <= name_top or band_bottom >= text_bottom:
        return configured
    name_box = _nameplate_bounds(image, (name_left, name_top, name_right, name_bottom))
    if configured is not None:
        x, _, end_x, _ = configured.speaker._pixel_box(width, height)
        # Detect on the same pixels even when the caller already resolved this frame.
        if max(x, name_box[0]) < min(end_x, name_box[2]):
            name_box = (
                max(x, name_box[0]),
                name_box[1],
                min(end_x, name_box[2]),
                name_box[3],
            )
        else:
            name_box = (x, name_top, end_x, name_bottom)

    def region(box: tuple[int, int, int, int]) -> DialogRegion:
        x, y, end_x, end_y = box
        return DialogRegion(
            x / width,
            y / height,
            min(1 - x / width, (end_x - x) / width),
            min(1 - y / height, (end_y - y) / height),
        )

    return OCRRegions(
        region(name_box), region((text_left, band_bottom, text_right, text_bottom))
    )


def _dialogue_divider(
    pixels: np.ndarray, left: int, right: int
) -> tuple[int, int, int, int] | None:
    """Locate one thin rule that can safely separate the speaker and dialogue."""
    height, _ = pixels.shape
    ridge = (
        pixels[1:-1, left:right]
        - (pixels[:-2, left:right] + pixels[2:, left:right]) / 2
    )
    support = np.abs(ridge) > 2
    counts = support.sum(axis=1)
    counts[:2] = 0
    counts[round(height * 0.75) :] = 0
    candidates = np.flatnonzero(counts >= (right - left) * 0.4)
    if not candidates.size:
        return None
    row = int(candidates[np.argmax(counts[candidates])])
    # A panel border or several rules do not establish which one separates roles.
    if any(
        abs(int(other) - row) > 3 and counts[other] >= counts[row] * 0.9
        for other in candidates
    ):
        return None
    runs, _ = label(support[row])
    spans = [bounds[0] for bounds in find_objects(runs) if bounds is not None]
    longest = max(spans, key=lambda span: span.stop - span.start)
    if longest.stop - longest.start < (right - left) * 0.35:
        return None
    spans = [span for span in spans if span.stop - span.start >= (right - left) * 0.02]
    line_left = left + min(span.start for span in spans)
    line_right = left + max(span.stop for span in spans)
    divider = row + 1
    # Remove only the thin rule itself, without shaving nearby glyphs.
    band_top = divider
    band_bottom = divider + 1
    while band_top > 1 and counts[band_top - 2] >= counts[row] * 0.8:
        band_top -= 1
    while band_bottom < height - 1 and counts[band_bottom - 1] >= counts[row] * 0.8:
        band_bottom += 1
    if band_bottom - band_top > max(5, round(height * 0.02)):
        return None
    return line_left, line_right, band_top, band_bottom


def _nameplate_bounds(
    image: Image.Image, box: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """Tighten the name around strokes aligned with the divider's left edge."""
    left, top, right, bottom = box
    pixels = np.asarray(image.crop(box).convert("L"), dtype=np.int16)
    light = float(np.median(pixels)) < 128
    contrast = (
        pixels - minimum_filter(pixels, size=3)
        if light
        else maximum_filter(pixels, size=3) - pixels
    )
    foreground = (contrast >= 16) & (
        pixels >= np.median(pixels) + 32 if light else pixels <= np.median(pixels) - 32
    )
    components, _ = label(foreground, structure=np.ones((3, 3)))
    glyphs = [
        bounds
        for bounds in find_objects(components)
        if bounds is not None
        and bounds[0].stop - bounds[0].start >= 4
        and 1 <= bounds[1].stop - bounds[1].start <= (bottom - top) * 2
        and bounds[0].stop - bounds[0].start < (bottom - top) * 0.9
    ]
    anchors = [g for g in glyphs if g[1].start <= max(12, (right - left) * 0.06)]
    if not anchors:
        if (
            not any(
                g[1].stop - g[1].start <= (g[0].stop - g[0].start) * 3 for g in glyphs
            )
            and int(pixels.max()) - int(pixels.min()) >= 32
            and not _isolated_speaker_strokes(pixels)
        ):
            # A wide empty band can contain scenery edges far from the name's
            # anchor. Preserve unanchored glyphs and uniformly faint labels.
            right = min(right, left + max(12, round((right - left) * 0.06)))
            return left, top, right, bottom
        return box
    anchor = max(anchors, key=lambda g: g[0].stop)
    baseline = anchor[0].stop
    aligned = [
        g for g in glyphs if abs(g[0].stop - baseline) <= max(3, (bottom - top) * 0.15)
    ]
    aligned.sort(key=lambda g: g[1].start)
    start = end = aligned.index(anchor)
    gap = max(12, anchor[0].stop - anchor[0].start)
    while start > 0 and aligned[start][1].start - aligned[start - 1][1].stop <= gap:
        start -= 1
    while (
        end + 1 < len(aligned)
        and aligned[end + 1][1].start - aligned[end][1].stop <= gap
    ):
        end += 1
    aligned = aligned[start : end + 1]
    # Include detached dots/accents by padding the aligned glyph line vertically.
    padding = max(3, round((bottom - top) * 0.1))
    return (
        max(left, left + min(g[1].start for g in aligned) - padding),
        max(top, top + min(g[0].start for g in aligned) - padding),
        min(right, left + max(g[1].stop for g in aligned) + padding),
        min(bottom, top + max(g[0].stop for g in aligned) + padding),
    )


def _speaker_stroke_background(patch: np.ndarray, y: slice, x: slice) -> np.ndarray:
    rows = np.concatenate((patch[: y.start], patch[y.stop :]), axis=0)
    columns = np.concatenate((patch[:, : x.start], patch[:, x.stop :]), axis=1)
    if not rows.size or not columns.size:
        rows = columns = corners = patch
    else:
        corners = np.concatenate((rows[:, : x.start], rows[:, x.stop :]), axis=1)
    # A clipped stroke must not supply its own estimated background.
    background: np.ndarray = (
        np.median(columns, axis=1)[:, None]
        + np.median(rows, axis=0)[None, :]
        - np.median(corners)
    )
    return background


def _isolated_speaker_strokes(pixels: np.ndarray) -> bool:
    """Retain faint ink over smooth local background despite distant scenery."""
    height, width = pixels.shape
    for dark, contrast in enumerate(
        (
            pixels - minimum_filter(pixels, size=3),
            maximum_filter(pixels, size=3) - pixels,
        )
    ):
        components, _ = label(contrast >= 16, structure=np.ones((3, 3)))
        for index, bounds in enumerate(find_objects(components), start=1):
            if bounds is None:
                continue
            y, x = bounds
            h, w = y.stop - y.start, x.stop - x.start
            if h < 4 or not 1 <= w <= h * 3:
                continue
            top, left = max(0, y.start - 2 * h), max(0, x.start - 2 * h)
            patch = pixels[
                top : min(height, y.stop + 2 * h), left : min(width, x.stop + 2 * h)
            ]
            # Row/column medians tolerate ramps and stripes without treating
            # faint letters as part of the surrounding scene's brightness.
            background = _speaker_stroke_background(
                patch,
                slice(y.start - top, y.stop - top),
                slice(x.start - left, x.stop - left),
            )
            if np.mean(np.abs(patch - background) <= 2) < 0.8:
                continue
            residual = (
                pixels[bounds]
                - background[
                    y.start - top : y.stop - top, x.start - left : x.stop - left
                ]
            )
            ink = (
                np.abs(residual)
                if y.start == 0 or y.stop == height
                else (-residual if dark else residual)[components[bounds] == index]
            )
            if ink.max() >= 16:
                return True
    return False


def combine_ocr_regions(
    speaker: DialogRegion,
    dialogue: DialogRegion,
    capture_region: DialogRegion | None = None,
) -> tuple[DialogRegion, OCRRegions]:
    """Combine screen selections into one capture and two relative crops."""
    OCRRegions(speaker, dialogue)
    regions = (
        (speaker, dialogue)
        if capture_region is None
        else (speaker, dialogue, capture_region)
    )
    left = min(region.left for region in regions)
    top = min(region.top for region in regions)
    right = max(region.left + region.width for region in regions)
    bottom = max(region.top + region.height for region in regions)
    capture = DialogRegion(left, top, right - left, bottom - top)

    def relative(region: DialogRegion) -> DialogRegion:
        x = (region.left - left) / capture.width
        y = (region.top - top) / capture.height
        return DialogRegion(
            x,
            y,
            min(1 - x, region.width / capture.width),
            min(1 - y, region.height / capture.height),
        )

    return capture, OCRRegions(relative(speaker), relative(dialogue))


@dataclass(frozen=True)
class OCRLine:
    text: str
    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class OCRResult:
    character: str
    text: str
    confidence: float
    profile: str
    attempts: int
    corrections: tuple[str, ...] = ()
    processed_image: Image.Image | None = field(default=None, repr=False, compare=False)
    speaker_image: Image.Image | None = field(default=None, repr=False, compare=False)
    speaker_confidence: float | None = None
    dialogue_confidence: float | None = None
    speaker_note: str = ""

    def is_confident(self, minimum: float = default_minimum_ocr_confidence) -> bool:
        return bool(self.text.strip()) and self.confidence >= minimum


def detect_standalone_ellipsis_frame(
    image: object, *, dialogue_only: bool = False
) -> bool:
    """Detect three isolated dialogue-band dots without relying on OCR text."""
    if not isinstance(image, Image.Image):
        return False
    rgb = image.convert("RGB")
    width, height = rgb.size
    right = min(width, max(30, round(width * 0.12)))
    top = 0 if dialogue_only else min(height - 1, round(height * 0.38))
    bottom = (
        height if dialogue_only else min(height, max(top + 1, round(height * 0.88)))
    )
    pixels = np.asarray(rgb.crop((0, top, right, bottom)))
    brightness = pixels.min(axis=2)
    bright = (brightness >= 150) & (pixels.max(axis=2) - brightness <= 45)
    if not 6 <= np.count_nonzero(bright) <= 30:
        return False
    components, count = label(bright, structure=np.ones((3, 3)))
    if count != 3:
        return False
    centers = []
    for index in range(1, count + 1):
        ys, xs = np.nonzero(components == index)
        if not 2 <= len(xs) <= 12 or np.ptp(xs) > 3 or np.ptp(ys) > 3:
            return False
        centers.append((float(xs.mean()), float(ys.mean())))
    centers.sort()
    gaps = [centers[index + 1][0] - centers[index][0] for index in range(2)]
    return max(center[1] for center in centers) - min(
        center[1] for center in centers
    ) <= 2.5 and all(4 <= gap <= 9 for gap in gaps)


class UncertainFrameRecorder:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser()
        self.lock = Lock()
        self.last_fingerprint: tuple[str, str, float, str] | None = None

    def record(
        self, image: Image.Image, result: OCRResult, minimum_confidence: float
    ) -> Path | None:
        fingerprint = (
            result.character,
            " ".join(result.text.split()),
            round(result.confidence, 1),
            result.profile,
        )
        with self.lock:
            if fingerprint == self.last_fingerprint:
                return None
            stem = f"uncertain-{datetime.now():%Y-%m-%d-%H-%M-%S}-{uuid4().hex}"
            image_path = self.directory / f"{stem}.png"
            metadata_path = self.directory / f"{stem}.json"
            with atomic_output_group(image_path, metadata_path) as (
                temporary_image,
                temporary_metadata,
            ):
                image.save(temporary_image, format="PNG")
                write_versioned_json(
                    temporary_metadata,
                    OCR_REVIEW_SCHEMA_VERSION,
                    {
                        "image": image_path.name,
                        "character": result.character,
                        "text": result.text,
                        "confidence": result.confidence,
                        "minimum_confidence": minimum_confidence,
                        "preprocessing_profile": result.profile,
                        "attempts": result.attempts,
                    },
                )
            self.last_fingerprint = fingerprint
            return image_path

    def reset(self) -> None:
        with self.lock:
            self.last_fingerprint = None


def parse_dialog_region(value: str) -> DialogRegion:
    try:
        values = [float(part.strip()) for part in value.split(",")]
    except (AttributeError, ValueError) as error:
        raise ValueError(
            "Dialog region must be left,top,width,height normalized to 0..1"
        ) from error
    if len(values) != 4:
        raise ValueError(
            "Dialog region must be left,top,width,height normalized to 0..1"
        )
    return DialogRegion(*values)


def get_dialog_region_file() -> Path:
    configured_path = os.environ.get("VNTTS_DIALOG_REGION_FILE")
    return (
        Path(configured_path).expanduser()
        if configured_path
        else default_dialog_region_file
    )


def load_dialog_region(path: str | Path) -> DialogRegion:
    path = Path(path).expanduser()
    try:
        value = decode_json(read_regular_text(path, encoding="utf-8"))
        return DialogRegion.from_mapping(value)
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Unable to load dialog region {path}: {error}") from error


def save_dialog_region(
    region: DialogRegion, path: str | Path, *, ocr_regions: OCRRegions | None = None
) -> None:
    path = Path(path).expanduser()
    value: dict[str, object] = dict(region.to_json())
    if ocr_regions is not None:
        value["ocr_regions"] = ocr_regions.to_json()
    atomic_write_json(path, value)


def load_ocr_regions(path: str | Path) -> OCRRegions | None:
    value = decode_json(read_regular_text(Path(path).expanduser(), encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Dialog region configuration must be an object")
    regions = value.get("ocr_regions")
    return None if regions is None else OCRRegions.from_mapping(regions)


def get_ocr_regions(
    capture_region: DialogRegion,
    profile_region: DialogRegion | None = None,
    profile_regions: OCRRegions | None = None,
) -> OCRRegions | None:
    if profile_region is not None:
        return profile_regions if capture_region == profile_region else None
    path = get_dialog_region_file()
    try:
        if not path.is_file() or capture_region != load_dialog_region(path):
            return None
        return load_ocr_regions(path)
    except OSError, TypeError, ValueError:
        return None


def get_dialog_region(profile_region: DialogRegion | None = None) -> DialogRegion:
    configured_region = os.environ.get("VNTTS_DIALOG_REGION")
    if configured_region:
        try:
            return parse_dialog_region(configured_region)
        except ValueError as error:
            print(f"Invalid VNTTS_DIALOG_REGION: {error}; using saved/default region")

    if profile_region is not None:
        return profile_region

    region_file = get_dialog_region_file()
    if region_file.is_file():
        try:
            return load_dialog_region(region_file)
        except ValueError as error:
            print(f"{error}; using default region")
    return default_dialog_region


def preprocess_dialog_image(image: Image.Image) -> Image.Image:
    image = ImageOps.grayscale(image)
    # A short line can occupy less than 1% of the crop; clipping loses its glyphs.
    image = ImageOps.autocontrast(image)
    image = ImageEnhance.Contrast(image).enhance(1.8)
    scale = max(1.0, min(2.0, 600 / max(1, image.height)))
    if scale > 1:
        image = image.resize(
            (round(image.width * scale), round(image.height * scale)),
            Image.Resampling.LANCZOS,
        )
    image = image.filter(ImageFilter.SHARPEN)
    return ImageOps.invert(image) if np.median(np.asarray(image)) < 128 else image


def recognize_dialog_image(
    image: Image.Image,
    voice_registry: VoiceRegistry | None = None,
    recognize_text: OcrTextRecognizer | None = None,
    recognize_data: OcrDataRecognizer | None = None,
    language: str = "eng",
    *,
    recognize_combined: OcrCombinedRecognizer | None = None,
) -> tuple[str, str]:
    result = recognize_dialog_image_result(
        image,
        voice_registry,
        recognize_text,
        recognize_data,
        language=language,
        recognize_combined=recognize_combined,
    )
    return result.character, result.text


def recognize_dialog_image_result(
    image: Image.Image,
    voice_registry: VoiceRegistry | None = None,
    recognize_text: OcrTextRecognizer | None = None,
    recognize_data: OcrDataRecognizer | None = None,
    *,
    minimum_confidence: float = default_minimum_ocr_confidence,
    language: str = "eng",
    timeout: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    ocr_regions: OCRRegions | None = None,
    recognize_combined: OcrCombinedRecognizer | None = None,
) -> OCRResult:
    remaining_timeout = _ocr_budget(timeout, cancelled)
    if recognize_data is None or (
        recognize_combined is None and recognize_text is None
    ):
        require_sauvola_cli(str(pytesseract_runtime.tesseract_cmd), remaining_timeout())
    if recognize_combined is None and recognize_text is None and recognize_data is None:
        recognize_combined = _recognize_psm6_text_data
    if recognize_text is None:
        recognize_text = pytesseract.image_to_string
    if recognize_data is None:
        recognize_data = pytesseract.image_to_data

    ocr_regions = resolve_ocr_regions(image, ocr_regions)
    if ocr_regions is not None:
        speaker_image = ocr_regions.speaker.crop(image)
        speaker = _recognize_isolated_region(
            speaker_image,
            recognize_text,
            recognize_data,
            language,
            remaining_timeout,
            speaker=True,
            recognize_combined=recognize_combined,
        )
        dialogue = _recognize_isolated_region(
            ocr_regions.dialogue.crop(image),
            recognize_text,
            recognize_data,
            language,
            remaining_timeout,
            speaker=False,
            recognize_combined=recognize_combined,
        )
        unreadable_speaker = not speaker.text.strip() and speaker_glyphs_visible(
            speaker_image
        )
        character = normalize_recognized_speaker(speaker.text, voice_registry)
        return OCRResult(
            character,
            dialogue.text,
            min(speaker.confidence, dialogue.confidence)
            if speaker.text.strip() or unreadable_speaker
            else dialogue.confidence,
            dialogue.profile,
            max(speaker.attempts, dialogue.attempts),
            processed_image=dialogue.processed_image,
            speaker_image=speaker.processed_image,
            speaker_confidence=speaker.confidence
            if speaker.text.strip() or unreadable_speaker
            else None,
            dialogue_confidence=dialogue.confidence,
            speaker_note="Visible name could not be read" if unreadable_speaker else "",
        )

    remaining_timeout()
    return _recognize_preprocessed_dialog(
        preprocess_dialog_image(image),
        voice_registry,
        recognize_text,
        recognize_data,
        language,
        recognize_combined,
        remaining_timeout,
    )


def _recognize_isolated_region(
    image: Image.Image,
    recognize_text: OcrTextRecognizer,
    recognize_data: OcrDataRecognizer,
    language: str,
    remaining_timeout: Callable[[], float | None],
    *,
    speaker: bool,
    recognize_combined: OcrCombinedRecognizer | None,
) -> OCRResult:
    has_speaker_glyphs = not speaker or speaker_glyphs_visible(image)
    remaining_timeout()
    processed = preprocess_dialog_image(image)
    if speaker:
        histogram = processed.histogram()
        processed = ImageOps.expand(
            processed, border=10, fill=255 if histogram[255] > histogram[0] else 0
        )
        data = _call_ocr(
            recognize_data,
            processed,
            remaining_timeout,
            config=tesseract_config(7),
            output_type=pytesseract.Output.DICT,
            lang=language,
        )
        text = (
            " ".join(word for word, _confidence in _recognized_words(data))
            if has_speaker_glyphs
            else ""
        )
    else:
        if recognize_combined is not None:
            text, data = recognize_combined(processed, language, remaining_timeout)
        else:
            text = _call_ocr(
                recognize_text,
                processed,
                remaining_timeout,
                config=tesseract_config(6),
                lang=language,
            )
            data = _call_ocr(
                recognize_data,
                processed,
                remaining_timeout,
                config=tesseract_config(6),
                output_type=pytesseract.Output.DICT,
                lang=language,
            )
        # The explicit dialogue rectangle supplies layout authority. Keep
        # one-letter words and punctuation instead of nameplate/UI heuristics.
        text = " ".join(line.strip() for line in text.splitlines()).strip()
        words = _recognized_words(data)
        cleaned = _trim_trailing_ocr_noise(words)
        if len(cleaned) < len(words):
            text = " ".join(word for word, _confidence in cleaned)
    result = OCRResult(
        "Narrator",
        text,
        calculate_ocr_confidence(data),
        "sauvola",
        1,
        processed_image=processed,
    )
    if (
        not speaker
        and not text
        and detect_standalone_ellipsis_frame(image, dialogue_only=True)
    ):
        return replace(result, text="...")
    return result


def _recognize_preprocessed_dialog(
    image: Image.Image,
    voice_registry: VoiceRegistry | None,
    recognize_text: OcrTextRecognizer,
    recognize_data: OcrDataRecognizer,
    language: str,
    recognize_combined: OcrCombinedRecognizer | None,
    remaining_timeout: Callable[[], float | None],
) -> OCRResult:
    if recognize_combined is not None:
        recognized_text, data = recognize_combined(image, language, remaining_timeout)
    else:
        recognized_text = None
        data = _call_ocr(
            recognize_data,
            image,
            remaining_timeout,
            config=tesseract_config(6),
            output_type=pytesseract.Output.DICT,
            lang=language,
        )

    speaker = recognize_speaker_from_data(
        data,
        voice_registry,
        image_width=image.width,
        image_height=image.height,
    )
    if speaker is None and _has_ocr_geometry(data):
        # PSM 6 is reliable for paragraph text but can merge a long decorative
        # separator into the short nameplate above it. Sparse-layout analysis
        # keeps those regions independent and recovers names such as Fatutu.
        sparse_data = _call_ocr(
            recognize_data,
            image,
            remaining_timeout,
            config=tesseract_config(11),
            output_type=pytesseract.Output.DICT,
            lang=language,
        )
        speaker = recognize_speaker_from_data(
            sparse_data,
            voice_registry,
            image_width=image.width,
            image_height=image.height,
        )
    if speaker is not None:
        character, speaker_line = speaker
        dialog_image = crop_dialog_text(image, speaker_line)
        if recognize_combined is not None:
            dialog_text, dialog_data = recognize_combined(
                dialog_image, language, remaining_timeout
            )
        else:
            dialog_text = _call_ocr(
                recognize_text,
                dialog_image,
                remaining_timeout,
                config=tesseract_config(6),
                lang=language,
            )
            dialog_data = None
        dialog_lines = clean_dialog_lines(dialog_text)
        if dialog_lines:
            if dialog_data is None:
                dialog_data = _call_ocr(
                    recognize_data,
                    dialog_image,
                    remaining_timeout,
                    config=tesseract_config(6),
                    output_type=pytesseract.Output.DICT,
                    lang=language,
                )
            dialog_lines = clean_dialog_lines_from_data(dialog_data, dialog_lines)
            return OCRResult(
                character,
                " ".join(dialog_lines),
                calculate_ocr_confidence(dialog_data),
                "sauvola",
                1,
                processed_image=dialog_image,
                speaker_image=image,
                dialogue_confidence=calculate_ocr_confidence(dialog_data),
            )

    if recognized_text is None:
        recognized_text = _call_ocr(
            recognize_text,
            image,
            remaining_timeout,
            config=tesseract_config(6),
            lang=language,
        )
    character, text = parse_recognized_dialog(recognized_text, voice_registry)
    return OCRResult(
        character,
        text,
        calculate_ocr_confidence(data),
        "sauvola",
        1,
        processed_image=image,
    )


def _recognize_psm6_text_data(
    image: Image.Image,
    language: str,
    remaining_timeout: Callable[[], float | None],
) -> tuple[str, OcrData]:
    """Read the same PSM 6 text and TSV from one Tesseract process."""
    with pytesseract_runtime.save(image) as (base, input_filename):
        pytesseract_runtime.run_tesseract(
            input_filename=input_filename,
            output_filename_base=base,
            extension="txt tsv",
            lang=language,
            config=f"{tesseract_config(6)} -c tessedit_create_tsv=1",
            timeout=remaining_timeout() or 0,
        )
        text = pytesseract_runtime._read_output(f"{base}.txt")
        data = pytesseract_runtime.file_to_dict(
            pytesseract_runtime._read_output(f"{base}.tsv"), "\t", -1
        )
    return text, data


def recognize_speaker(
    image: Image.Image,
    voice_registry: VoiceRegistry | None,
    recognize_data: OcrDataRecognizer,
) -> tuple[str, OCRLine] | None:
    data = recognize_data(
        image,
        config=tesseract_config(6),
        output_type=pytesseract.Output.DICT,
    )
    return recognize_speaker_from_data(
        data,
        voice_registry,
        image_width=image.width,
        image_height=image.height,
    )


def recognize_speaker_from_data(
    data: OcrData,
    voice_registry: VoiceRegistry | None = None,
    *,
    image_width: int | None = None,
    image_height: int | None = None,
) -> tuple[str, OCRLine] | None:
    if not _has_ocr_geometry(data):
        return None
    lines = extract_ocr_lines(data)[:6]

    if voice_registry is not None:
        for line in lines:
            if len(line.text) > MAX_CHARACTER_NAME_LENGTH:
                continue
            character = voice_registry.resolve_closest_character(line.text)
            if character is not None and _has_dialog_below(line, lines):
                return character, line

    candidates = []
    for position, line in enumerate(lines[:-1]):
        if len(line.text) > MAX_CHARACTER_NAME_LENGTH:
            continue
        if _normalize_unknown_nameplate(
            line.text
        ) != "???" and not is_probable_character_name(line.text):
            continue
        if image_height is not None and line.top > image_height * 0.6:
            continue

        dialog_lines = [
            candidate
            for candidate in lines[position + 1 :]
            if candidate.top > line.bottom
        ]
        if image_width is not None:
            if line.right - line.left > image_width * 0.45:
                continue
            dialog_lines = [
                candidate
                for candidate in dialog_lines
                if abs(line.left - candidate.left) <= image_width * 0.15
            ]
        if not dialog_lines:
            continue
        first_dialog_line = dialog_lines[0]
        # Short, complete lines such as ``This ...``, ``No.`` or ``...`` are
        # common in visual novels. Geometry already proves that this line sits
        # below a plausible nameplate; requiring twelve characters here loses
        # those lines before the checksum-bound sequence matcher can see them.
        if len(first_dialog_line.text.strip()) < 3:
            continue

        score = len(first_dialog_line.text)
        if len(line.text.split()) == 1 and line.text.istitle():
            score += 100
        score -= position
        candidates.append((score, line))

    if not candidates:
        return None
    _score, speaker_line = max(candidates, key=lambda candidate: candidate[0])
    character = _normalize_unknown_nameplate(speaker_line.text)
    # The game's ``???`` nameplate is consistently read as ``22``. Keep this
    # normalization local to the geometrically isolated label; other numbers pass.
    return character, speaker_line


def _normalize_unknown_nameplate(text: str) -> str:
    value = text.strip()
    # Tesseract reads the game's ``???`` as 22, 22?, or 2???; do not turn
    # ordinary numeric NPC labels such as 6 or 37 into an unknown speaker.
    if value == "22" or (
        2 <= len(value) <= 4 and "?" in value and set(value) <= {"2", "?"}
    ):
        return "???"
    return value


def speaker_glyphs_visible(image: Image.Image) -> bool:
    pixels = np.asarray(image.convert("L"), dtype=np.int16)
    # Test local strokes before autocontrast can amplify empty scenery.
    # shortcut: qualify fonts below 4px or 16 levels of contrast before widening this gate.
    for contrast in (
        pixels - minimum_filter(pixels, size=3),
        maximum_filter(pixels, size=3) - pixels,
    ):
        foreground = contrast >= 16
        components, _count = label(foreground, structure=np.ones((3, 3)))
        for bounds in find_objects(components):
            if bounds is None:
                continue
            height = bounds[0].stop - bounds[0].start
            width = bounds[1].stop - bounds[1].start
            if (
                height >= 4
                and 1 <= width <= height * 3
                and np.count_nonzero(foreground[bounds]) >= 4
            ):
                return True
    return False


def normalize_recognized_speaker(
    text: str, voice_registry: VoiceRegistry | None = None
) -> str:
    character = _normalize_unknown_nameplate(" ".join(text.split())) or "Narrator"
    if voice_registry is not None and character != "Narrator":
        return voice_registry.resolve_closest_character(character) or character
    return character


def _has_dialog_below(speaker_line: OCRLine, lines: Sequence[OCRLine]) -> bool:
    return any(line.top > speaker_line.bottom and len(line.text) >= 3 for line in lines)


def _has_ocr_geometry(data: OcrData) -> bool:
    return {
        "block_num",
        "par_num",
        "line_num",
        "left",
        "top",
        "width",
        "height",
    }.issubset(data)


def calculate_ocr_confidence(data: OcrData) -> float:
    weighted_confidence = 0.0
    total_weight = 0
    for text, confidence in _trim_trailing_ocr_noise(_recognized_words(data)):
        weight = max(1, sum(character.isalnum() for character in text))
        weighted_confidence += confidence * weight
        total_weight += weight
    return weighted_confidence / total_weight if total_weight else 0.0


def extract_ocr_lines(data: OcrData) -> list[OCRLine]:
    grouped_words: dict[
        tuple[object, object, object], list[tuple[str, int, int, int, int]]
    ] = {}
    for position, raw_text in enumerate(data.get("text", [])):
        if not isinstance(raw_text, str):
            continue
        text = raw_text.strip()
        if not text:
            continue
        try:
            left = data["left"][position]
            top = data["top"][position]
            width = data["width"][position]
            height = data["height"][position]
            block = data["block_num"][position]
            paragraph = data["par_num"][position]
            line = data["line_num"][position]
        except IndexError:
            continue
        if not (
            isinstance(left, int)
            and isinstance(top, int)
            and isinstance(width, int)
            and isinstance(height, int)
        ):
            continue
        key = (
            block,
            paragraph,
            line,
        )
        try:
            words = grouped_words.setdefault(key, [])
        except TypeError:
            continue
        words.append(
            (
                text,
                left,
                top,
                width,
                height,
            )
        )

    lines = []
    for words in grouped_words.values():
        left = min(word[1] for word in words)
        top = min(word[2] for word in words)
        right = max(word[1] + word[3] for word in words)
        bottom = max(word[2] + word[4] for word in words)
        lines.append(
            OCRLine(
                text=" ".join(word[0] for word in words),
                left=left,
                top=top,
                right=right,
                bottom=bottom,
            )
        )
    return sorted(lines, key=lambda line: (line.top, line.left))


def crop_dialog_text(image: Image.Image, speaker_line: OCRLine) -> Image.Image:
    horizontal_margin = round(image.width * 0.02)
    vertical_margin = round(image.height * 0.03)
    return image.crop(
        (
            max(0, speaker_line.left - horizontal_margin),
            min(image.height, speaker_line.bottom + vertical_margin),
            image.width,
            image.height,
        )
    )


def clean_dialog_lines(text: str) -> list[str]:
    lines = []
    for line in (text or "").splitlines():
        line = _strip_trailing_ocr_glyphs(line.strip())
        alphanumeric_characters = sum(character.isalnum() for character in line)
        if (
            _is_silent_ellipsis(line)
            or alphanumeric_characters >= 3
            or (alphanumeric_characters >= 2 and len(line.split()) == 1)
        ):
            lines.append(line)
    return lines


def _is_silent_ellipsis(text: str) -> bool:
    return "".join(str(text).split()) in {"...", "…"}


def clean_dialog_lines_from_data(
    data: OcrData, fallback_lines: Sequence[str]
) -> list[str]:
    """Remove low-confidence background words appended after a full sentence."""
    fallback_lines = list(fallback_lines)
    words = _recognized_words(data)
    cleaned_words = _trim_trailing_ocr_noise(words)
    if len(cleaned_words) == len(words):
        return fallback_lines
    cleaned = clean_dialog_lines(" ".join(word for word, _ in cleaned_words))
    return cleaned or fallback_lines


def _recognized_words(data: OcrData) -> list[tuple[str, float]]:
    words: list[tuple[str, float]] = []
    confidences = data.get("conf", [])
    for position, raw_text in enumerate(data.get("text", [])):
        if not isinstance(raw_text, str):
            continue
        text = raw_text.strip()
        if not text or position >= len(confidences):
            continue
        raw_confidence = confidences[position]
        if isinstance(raw_confidence, bool) or not isinstance(
            raw_confidence, (str, int, float)
        ):
            continue
        try:
            confidence = float(raw_confidence)
        except TypeError, ValueError, OverflowError:
            continue
        # Tesseract reports word confidence on a 0..100 percent scale.
        if not 0 <= confidence <= 100:
            continue
        words.append((text, confidence))
    return words


def _last_confident_terminal_position(words: Sequence[tuple[str, float]]) -> int | None:
    terminal_position = None
    for position, (text, confidence) in enumerate(words):
        if text.rstrip("\"'”’)]}").endswith((".", "!", "?", "…")) and confidence >= 60:
            terminal_position = position
    return terminal_position


def _trim_trailing_ocr_noise(
    words: Sequence[tuple[str, float]],
) -> Sequence[tuple[str, float]]:
    terminal_position = _last_confident_terminal_position(words)
    if terminal_position is None or terminal_position == len(words) - 1:
        return words
    suffix = words[terminal_position + 1 :]
    return (
        words[: terminal_position + 1]
        if all(_is_suspicious_trailing_word(*word) for word in suffix)
        else words
    )


def _is_suspicious_trailing_word(word: str, confidence: float) -> bool:
    alphanumeric_characters = sum(character.isalnum() for character in word)
    return (
        alphanumeric_characters == 0
        or confidence < 45
        or (alphanumeric_characters <= 3 and confidence < 65)
    )


def _strip_trailing_ocr_glyphs(line: str) -> str:
    tokens = line.split()
    while tokens:
        token = tokens[-1]
        if any(character.isalnum() for character in token):
            break
        if token and all(character in ".!?…" for character in token):
            break
        tokens.pop()
    return " ".join(tokens)


def parse_recognized_dialog(
    text: str, voice_registry: VoiceRegistry | None = None
) -> tuple[str, str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if voice_registry is not None:
        for position, line in enumerate(lines[:6]):
            if len(line) > MAX_CHARACTER_NAME_LENGTH:
                continue
            character = voice_registry.resolve_closest_character(line)
            if character is None:
                continue
            dialog_lines = clean_dialog_lines("\n".join(lines[position + 1 :]))
            if dialog_lines:
                return character, " ".join(dialog_lines)

    if lines and _normalize_unknown_nameplate(lines[0]) == "???":
        dialog_lines = clean_dialog_lines("\n".join(lines[1:]))
        if dialog_lines:
            return "???", " ".join(dialog_lines)

    if len(lines) >= 2 and is_probable_character_name(lines[0]):
        dialog_lines = clean_dialog_lines("\n".join(lines[1:]))
        if dialog_lines:
            return lines[0], " ".join(dialog_lines)

    character, dialog = parse_dialog(text)
    return character, dialog
