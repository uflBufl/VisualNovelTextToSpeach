from typing import Protocol

from vntts.ocr import OCRResult, recognize_dialog_image_result


class OCRBackend(Protocol):
    name: str

    def recognize(
        self,
        image,
        voice_registry=None,
        *,
        minimum_confidence=60.0,
        language="eng",
    ) -> OCRResult: ...


class TesseractOCRBackend:
    name = "tesseract"

    def __init__(self, recognizer=None):
        self.recognizer = recognizer or recognize_dialog_image_result

    def recognize(
        self,
        image,
        voice_registry=None,
        *,
        minimum_confidence=60.0,
        language="eng",
    ):
        return self.recognizer(
            image,
            voice_registry,
            minimum_confidence=minimum_confidence,
            language=language,
        )
