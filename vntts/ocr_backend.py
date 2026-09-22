from collections.abc import Iterable, Sequence
from typing import Protocol, TypeAlias

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from vntts.ocr import (
    OCRResult,
    VoiceRegistry,
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
        minimum_confidence: float = 60.0,
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
        minimum_confidence: float = 60.0,
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
        minimum_confidence: float = 60.0,
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
        minimum_confidence: float = 60.0,
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
        lines = list(zip(boxes, texts, scores, strict=True))
        lines.sort(
            key=lambda item: (
                min(float(point[1]) for point in item[0]),
                min(float(point[0]) for point in item[0]),
            )
        )
        return lines
