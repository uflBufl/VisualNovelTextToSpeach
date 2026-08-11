import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
from pytesseract import pytesseract as pytesseract_runtime

from vntts.dialog import is_probable_character_name, parse_dialog

default_dialog_region_file = Path("~/.config/vntts/dialog-region.json").expanduser()
default_minimum_ocr_confidence = 60.0


def configure_tesseract_process_environment():
    """Limit Tesseract without globally throttling PyTorch or other runtimes."""
    current = pytesseract_runtime.subprocess_args
    if getattr(current, "_vntts_limited_omp", False):
        return

    def subprocess_args(include_stdout=True):
        arguments = current(include_stdout=include_stdout)
        environment = dict(arguments.get("env") or os.environ)
        environment["OMP_THREAD_LIMIT"] = "1"
        arguments["env"] = environment
        return arguments

    subprocess_args._vntts_limited_omp = True
    pytesseract_runtime.subprocess_args = subprocess_args


configure_tesseract_process_environment()


@dataclass(frozen=True)
class DialogRegion:
    left: float
    top: float
    width: float
    height: float

    def __post_init__(self):
        values = (self.left, self.top, self.width, self.height)
        if not all(isinstance(value, (int, float)) for value in values):
            raise ValueError("Dialog region values must be numbers")
        if self.left < 0 or self.top < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("Dialog region values must be positive and normalized")
        if self.left + self.width > 1 or self.top + self.height > 1:
            raise ValueError("Dialog region must fit inside the screen")

    def crop(self, image):
        image_width, image_height = image.size
        return image.crop(
            (
                round(self.left * image_width),
                round(self.top * image_height),
                round((self.left + self.width) * image_width),
                round((self.top + self.height) * image_height),
            )
        )

    def capture_box(self, monitor):
        monitor_left = monitor.get("left", 0)
        monitor_top = monitor.get("top", 0)
        monitor_width = monitor["width"]
        monitor_height = monitor["height"]
        return {
            "left": monitor_left + round(self.left * monitor_width),
            "top": monitor_top + round(self.top * monitor_height),
            "width": max(1, round(self.width * monitor_width)),
            "height": max(1, round(self.height * monitor_height)),
        }

    def to_json(self):
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


default_dialog_region = DialogRegion(0.0, 0.68, 1.0, 0.32)


@dataclass(frozen=True)
class OCRLine:
    text: str
    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class OCRPreprocessingProfile:
    name: str
    contrast: float
    threshold: int


@dataclass(frozen=True)
class OCRResult:
    character: str
    text: str
    confidence: float
    profile: str
    attempts: int
    corrections: tuple[str, ...] = ()
    choice_detected: bool = False

    def is_confident(self, minimum=default_minimum_ocr_confidence):
        return bool(self.text.strip()) and self.confidence >= minimum


default_ocr_profiles = (
    OCRPreprocessingProfile("balanced", 1.8, 180),
    OCRPreprocessingProfile("dark-background", 2.2, 155),
    OCRPreprocessingProfile("light-background", 1.5, 205),
)


class UncertainFrameRecorder:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
        self.lock = Lock()
        self.last_fingerprint = None

    def record(self, image, result, minimum_confidence):
        fingerprint = (
            result.character,
            " ".join(result.text.split()),
            round(result.confidence, 1),
            result.profile,
        )
        with self.lock:
            if fingerprint == self.last_fingerprint:
                return None
            self.directory.mkdir(parents=True, exist_ok=True)
            stem = f"uncertain-{datetime.now():%Y-%m-%d-%H-%M-%S}-{uuid4().hex}"
            image_path = self.directory / f"{stem}.png"
            metadata_path = self.directory / f"{stem}.json"
            temporary_image = image_path.with_suffix(".png.tmp")
            temporary_metadata = metadata_path.with_suffix(".json.tmp")
            image.save(temporary_image, format="PNG")
            temporary_metadata.write_text(
                json.dumps(
                    {
                        "image": image_path.name,
                        "character": result.character,
                        "text": result.text,
                        "confidence": result.confidence,
                        "minimum_confidence": minimum_confidence,
                        "preprocessing_profile": result.profile,
                        "attempts": result.attempts,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary_image.replace(image_path)
            temporary_metadata.replace(metadata_path)
            self.last_fingerprint = fingerprint
            return image_path

    def reset(self):
        with self.lock:
            self.last_fingerprint = None


def parse_dialog_region(value):
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


def get_dialog_region_file():
    configured_path = os.environ.get("VNTTS_DIALOG_REGION_FILE")
    return (
        Path(configured_path).expanduser()
        if configured_path
        else default_dialog_region_file
    )


def load_dialog_region(path):
    path = Path(path).expanduser()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return DialogRegion(
            left=value["left"],
            top=value["top"],
            width=value["width"],
            height=value["height"],
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Unable to load dialog region {path}: {error}") from error


def save_dialog_region(region, path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(region.to_json(), indent=2) + "\n",
        encoding="utf-8",
    )


def get_dialog_region():
    configured_region = os.environ.get("VNTTS_DIALOG_REGION")
    if configured_region:
        try:
            return parse_dialog_region(configured_region)
        except ValueError as error:
            print(f"Invalid VNTTS_DIALOG_REGION: {error}; using saved/default region")

    region_file = get_dialog_region_file()
    if region_file.is_file():
        try:
            return load_dialog_region(region_file)
        except ValueError as error:
            print(f"{error}; using default region")
    return default_dialog_region


def preprocess_dialog_image(image, profile=None):
    profile = profile or default_ocr_profiles[0]
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image, cutoff=1)
    image = ImageEnhance.Contrast(image).enhance(profile.contrast)
    scale = max(1.0, min(2.0, 600 / max(1, image.height)))
    if scale > 1:
        image = image.resize(
            (round(image.width * scale), round(image.height * scale)),
            Image.Resampling.LANCZOS,
        )
    image = image.filter(ImageFilter.SHARPEN)
    return image.point(lambda value: 255 if value >= profile.threshold else 0)


def recognize_dialog_image(
    image,
    voice_registry=None,
    recognize_text=None,
    recognize_data=None,
    language="eng",
):
    result = recognize_dialog_image_result(
        image,
        voice_registry,
        recognize_text,
        recognize_data,
        language=language,
    )
    return result.character, result.text


def recognize_dialog_image_result(
    image,
    voice_registry=None,
    recognize_text=None,
    recognize_data=None,
    *,
    minimum_confidence=default_minimum_ocr_confidence,
    profiles=default_ocr_profiles,
    language="eng",
):
    if recognize_text is None:
        recognize_text = pytesseract.image_to_string
    if recognize_data is None:
        recognize_data = pytesseract.image_to_data

    best_result = None
    for attempt, profile in enumerate(profiles, start=1):
        processed_image = preprocess_dialog_image(image, profile)
        result = _recognize_preprocessed_dialog(
            processed_image,
            voice_registry,
            recognize_text,
            recognize_data,
            profile.name,
            attempt,
            language,
        )
        if best_result is None or _result_rank(result) > _result_rank(best_result):
            best_result = result
        if result.is_confident(minimum_confidence):
            return result

    return best_result or OCRResult("Narrator", "", 0.0, "none", 0)


def _recognize_preprocessed_dialog(
    image,
    voice_registry,
    recognize_text,
    recognize_data,
    profile_name,
    attempt,
    language,
):
    data = recognize_data(
        image,
        config="--psm 6",
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
        sparse_data = recognize_data(
            image,
            config="--psm 11",
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
        dialog_text = recognize_text(
            dialog_image,
            config="--psm 6",
            lang=language,
        )
        dialog_lines = clean_dialog_lines(dialog_text)
        if dialog_lines:
            dialog_data = recognize_data(
                dialog_image,
                config="--psm 6",
                output_type=pytesseract.Output.DICT,
                lang=language,
            )
            return OCRResult(
                character,
                " ".join(dialog_lines),
                calculate_ocr_confidence(dialog_data),
                profile_name,
                attempt,
            )

    recognized_text = recognize_text(
        image,
        config="--psm 6",
        lang=language,
    )
    character, text = parse_recognized_dialog(recognized_text, voice_registry)
    return OCRResult(
        character,
        text,
        calculate_ocr_confidence(data),
        profile_name,
        attempt,
        choice_detected=detect_choice_layout(
            data,
            image_width=image.width,
        ),
    )


def detect_choice_layout(data, *, image_width=None):
    """Recognize separated short response rows without guessing their text."""
    if not _has_ocr_geometry(data):
        return False
    lines = [line for line in extract_ocr_lines(data) if len(line.text) >= 2]
    if not 2 <= len(lines) <= 6:
        return False

    normalized = [line.text.lstrip() for line in lines]
    marked = sum(
        text.startswith((">", "•", "-", "1.", "2.", "3.", "A.", "B."))
        for text in normalized
    )
    if marked >= 2:
        return True

    heights = sorted(max(1, line.bottom - line.top) for line in lines)
    median_height = heights[len(heights) // 2]
    separated_rows = sum(
        following.top - current.bottom >= median_height * 0.75
        for current, following in zip(lines, lines[1:])
    )
    if separated_rows == 0:
        return False

    if image_width is None:
        image_width = max(line.right for line in lines)
    short_rows = sum(line.right - line.left <= image_width * 0.8 for line in lines)
    aligned = max(line.left for line in lines) - min(line.left for line in lines)
    return short_rows == len(lines) and aligned <= image_width * 0.2


def recognize_speaker(image, voice_registry, recognize_data):
    data = recognize_data(
        image,
        config="--psm 6",
        output_type=pytesseract.Output.DICT,
    )
    return recognize_speaker_from_data(
        data,
        voice_registry,
        image_width=image.width,
        image_height=image.height,
    )


def recognize_speaker_from_data(
    data,
    voice_registry=None,
    *,
    image_width=None,
    image_height=None,
):
    if not _has_ocr_geometry(data):
        return None
    lines = extract_ocr_lines(data)[:6]

    if voice_registry is not None:
        for line in lines:
            if len(line.text) > 40:
                continue
            voice = voice_registry.resolve_closest(line.text)
            if voice is not None and _has_dialog_below(line, lines):
                return voice.character, line

    candidates = []
    for position, line in enumerate(lines[:-1]):
        if len(line.text) > 40:
            continue
        if not is_probable_character_name(line.text):
            continue
        if image_height is not None and line.top > image_height * 0.6:
            continue

        dialog_lines = [
            candidate
            for candidate in lines[position + 1 :]
            if candidate.top > line.bottom
        ]
        if not dialog_lines:
            continue
        first_dialog_line = dialog_lines[0]
        if len(first_dialog_line.text) < 12:
            continue
        if image_width is not None:
            if line.right - line.left > image_width * 0.45:
                continue
            if abs(line.left - first_dialog_line.left) > image_width * 0.15:
                continue

        score = len(first_dialog_line.text)
        if len(line.text.split()) == 1 and line.text.istitle():
            score += 100
        score -= position
        candidates.append((score, line))

    if not candidates:
        return None
    _score, speaker_line = max(candidates, key=lambda candidate: candidate[0])
    return speaker_line.text.strip(), speaker_line


def _has_dialog_below(speaker_line, lines):
    return any(line.top > speaker_line.bottom and len(line.text) >= 3 for line in lines)


def _has_ocr_geometry(data):
    return {
        "block_num",
        "par_num",
        "line_num",
        "left",
        "top",
        "width",
        "height",
    }.issubset(data)


def calculate_ocr_confidence(data):
    weighted_confidence = 0.0
    total_weight = 0
    confidences = data.get("conf", [])
    for position, text in enumerate(data.get("text", [])):
        text = text.strip()
        if not text or position >= len(confidences):
            continue
        try:
            confidence = float(confidences[position])
        except (TypeError, ValueError):
            continue
        if confidence < 0:
            continue
        weight = max(1, sum(character.isalnum() for character in text))
        weighted_confidence += confidence * weight
        total_weight += weight
    return weighted_confidence / total_weight if total_weight else 0.0


def _result_rank(result):
    return result.confidence, len(result.text.strip())


def extract_ocr_lines(data):
    grouped_words = {}
    for position, text in enumerate(data.get("text", [])):
        text = text.strip()
        if not text:
            continue
        key = (
            data["block_num"][position],
            data["par_num"][position],
            data["line_num"][position],
        )
        grouped_words.setdefault(key, []).append(
            (
                text,
                data["left"][position],
                data["top"][position],
                data["width"][position],
                data["height"][position],
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


def crop_dialog_text(image, speaker_line):
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


def clean_dialog_lines(text):
    lines = []
    for line in (text or "").splitlines():
        line = _strip_trailing_ocr_glyphs(line.strip())
        alphanumeric_characters = sum(character.isalnum() for character in line)
        if alphanumeric_characters >= 3 or (
            alphanumeric_characters >= 2 and len(line.split()) == 1
        ):
            lines.append(line)
    return lines


def _strip_trailing_ocr_glyphs(line):
    tokens = line.split()
    while tokens:
        token = tokens[-1]
        if any(character.isalnum() for character in token):
            break
        if token and all(character in ".!?…" for character in token):
            break
        tokens.pop()
    return " ".join(tokens)


def parse_recognized_dialog(text, voice_registry=None):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if voice_registry is not None:
        for position, line in enumerate(lines[:6]):
            if len(line) > 40:
                continue
            voice = voice_registry.resolve_closest(line)
            if voice is None:
                continue
            dialog_lines = clean_dialog_lines("\n".join(lines[position + 1 :]))
            if dialog_lines:
                return voice.character, " ".join(dialog_lines)

    if len(lines) >= 2 and is_probable_character_name(lines[0]):
        dialog_lines = clean_dialog_lines("\n".join(lines[1:]))
        if dialog_lines:
            return lines[0], " ".join(dialog_lines)

    return parse_dialog(text)
