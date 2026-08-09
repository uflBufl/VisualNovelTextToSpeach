import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from vntts.dialog import parse_dialog

default_dialog_region_file = Path("~/.config/vntts/dialog-region.json").expanduser()


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


def preprocess_dialog_image(image):
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image, cutoff=1)
    image = ImageEnhance.Contrast(image).enhance(1.8)
    scale = max(1.0, min(2.0, 600 / max(1, image.height)))
    if scale > 1:
        image = image.resize(
            (round(image.width * scale), round(image.height * scale)),
            Image.Resampling.LANCZOS,
        )
    image = image.filter(ImageFilter.SHARPEN)
    return image.point(lambda value: 255 if value >= 180 else 0)


def recognize_dialog_image(
    image,
    voice_registry=None,
    recognize_text=None,
    recognize_data=None,
):
    if recognize_text is None:
        recognize_text = pytesseract.image_to_string
    if recognize_data is None:
        recognize_data = pytesseract.image_to_data
    image = preprocess_dialog_image(image)

    if voice_registry is not None:
        speaker = recognize_speaker(image, voice_registry, recognize_data)
        if speaker is not None:
            voice, speaker_line = speaker
            dialog_image = crop_dialog_text(image, speaker_line)
            dialog_text = recognize_text(dialog_image, config="--psm 6")
            dialog_lines = clean_dialog_lines(dialog_text)
            if dialog_lines:
                return voice.character, " ".join(dialog_lines)

    recognized_text = recognize_text(
        image,
        config="--psm 6",
    )
    return parse_recognized_dialog(recognized_text, voice_registry)


def recognize_speaker(image, voice_registry, recognize_data):
    data = recognize_data(
        image,
        config="--psm 6",
        output_type=pytesseract.Output.DICT,
    )
    for line in extract_ocr_lines(data)[:6]:
        if len(line.text) > 40:
            continue
        voice = voice_registry.resolve_closest(line.text)
        if voice is not None:
            return voice, line
    return None


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
            round(image.width * 0.95),
            image.height,
        )
    )


def clean_dialog_lines(text):
    lines = []
    for line in (text or "").splitlines():
        line = line.strip()
        alphanumeric_characters = sum(character.isalnum() for character in line)
        if alphanumeric_characters >= 3 or (
            alphanumeric_characters >= 2 and len(line.split()) == 1
        ):
            lines.append(line)
    return lines


def parse_recognized_dialog(text, voice_registry=None):
    if voice_registry is None:
        return parse_dialog(text)

    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for position, line in enumerate(lines[:6]):
        if len(line) > 40:
            continue
        voice = voice_registry.resolve_closest(line)
        if voice is None:
            continue
        dialog_lines = clean_dialog_lines("\n".join(lines[position + 1 :]))
        if dialog_lines:
            return voice.character, " ".join(dialog_lines)

    return parse_dialog(text)
