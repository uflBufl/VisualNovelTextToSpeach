import math
from collections.abc import Iterable, Sequence
from numbers import Real
from typing import Protocol, TypeAlias

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from vntts.ocr import (
    OCRResult,
    VoiceRegistry,
    default_minimum_ocr_confidence,
    parse_recognized_dialog,
    recognize_dialog_image_result,
)

RapidOCRBox: TypeAlias = Iterable[Sequence[float]]
RapidOCRLine: TypeAlias = tuple[RapidOCRBox, str, float]


class OCRRecognizer(Protocol):
    def __call__(
        self,
        image: Image.Image,
        voice_registry: VoiceRegistry | None = None,
        *,
        minimum_confidence: float = default_minimum_ocr_confidence,
        language: str = "eng",
    ) -> OCRResult: ...


class RapidOCREngine(Protocol):
    def __call__(self, image: NDArray[np.uint8], *, use_cls: bool) -> object: ...


class OCRBackend(Protocol):
    name: str

    def recognize(
        self,
        image: Image.Image,
        voice_registry: VoiceRegistry | None = None,
        *,
        minimum_confidence: float = default_minimum_ocr_confidence,
        language: str = "eng",
    ) -> OCRResult: ...


class TesseractOCRBackend:
    name = "tesseract"
    distribution_names = ("pytesseract",)

    def __init__(self, recognizer: OCRRecognizer | None = None) -> None:
        self.recognizer = recognizer or recognize_dialog_image_result

    def recognize(
        self,
        image: Image.Image,
        voice_registry: VoiceRegistry | None = None,
        *,
        minimum_confidence: float = default_minimum_ocr_confidence,
        language: str = "eng",
    ) -> OCRResult:
        return self.recognizer(
            image,
            voice_registry,
            minimum_confidence=minimum_confidence,
            language=language,
        )


class RapidOCRBackend:
    name = "rapidocr-onnx"
    distribution_names = ("rapidocr", "onnxruntime", "opencv-python")

    def __init__(self, engine: RapidOCREngine | None = None) -> None:
        if engine is None:
            try:
                from rapidocr import RapidOCR
            except ImportError as error:
                raise RuntimeError(
                    "RapidOCR is not installed. Run `uv sync --extra rapidocr`."
                ) from error
            engine = RapidOCR()
        self.engine = engine

    def recognize(
        self,
        image: Image.Image,
        voice_registry: VoiceRegistry | None = None,
        *,
        minimum_confidence: float = default_minimum_ocr_confidence,
        language: str = "eng",
    ) -> OCRResult:
        del minimum_confidence
        if language.casefold() not in {"eng", "en"}:
            raise ValueError("The RapidOCR prototype currently supports English only")
        output = self.engine(np.asarray(image.convert("RGB")), use_cls=False)
        lines = self._ordered_lines(output)
        recognized = "\n".join(text for _box, text, _score in lines)
        character, text = parse_recognized_dialog(recognized, voice_registry)
        total_characters = sum(max(1, len(text)) for _box, text, _score in lines)
        confidence = (
            sum(max(1, len(text)) * score for _box, text, score in lines)
            / total_characters
            * 100
            if total_characters
            else 0.0
        )
        if not math.isfinite(confidence):
            raise ValueError("RapidOCR confidence aggregate is non-finite")
        return OCRResult(
            character,
            text,
            confidence,
            self.name,
            1,
        )

    @staticmethod
    def _ordered_lines(output: object) -> list[RapidOCRLine]:
        boxes = getattr(output, "boxes", None)
        texts = getattr(output, "txts", None)
        scores = getattr(output, "scores", None)
        if boxes is None or texts is None or scores is None:
            return []
        lines: list[RapidOCRLine] = []
        for box, text, raw_score in zip(boxes, texts, scores, strict=True):
            if isinstance(raw_score, bool) or not isinstance(raw_score, Real):
                raise ValueError("RapidOCR score must be a finite real number")
            try:
                score = float(raw_score)
            except (OverflowError, TypeError, ValueError) as error:
                raise ValueError(
                    "RapidOCR score must be a finite real number"
                ) from error
            if not math.isfinite(score):
                raise ValueError("RapidOCR score must be a finite real number")
            lines.append((box, text, score))
        lines.sort(
            key=lambda item: (
                min(float(point[1]) for point in item[0]),
                min(float(point[0]) for point in item[0]),
            )
        )
        return lines
