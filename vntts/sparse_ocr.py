"""Compact English line recognition for otherwise empty dialogue OCR."""

from __future__ import annotations

import math
from collections.abc import Callable
from numbers import Real
from pathlib import Path
from threading import Event
from typing import Any

import numpy as np
from PIL import Image, ImageOps
from scipy.ndimage import find_objects, label, maximum_filter

from vntts.asset_integrity import ModelIntegrityError
from vntts.document_identity import file_sha256
from vntts.model_assets import ModelAsset, ModelAssetManager, ModelDownloadCancelled

_RECOGNIZER_FILENAME = "en_PP-OCRv5_rec_mobile.onnx"
_RECOGNIZER_ASSET = ModelAsset(
    name="rapidocr/ppocrv5-en-mobile-rec",
    urls=(
        "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/"
        f"onnx/PP-OCRv5/rec/{_RECOGNIZER_FILENAME}",
    ),
)
_RECOGNIZER_SHA256 = "c3461add59bb4323ecba96a492ab75e06dda42467c9e3d0c18db5d1d21924be8"


def prepare_sparse_ocr_model(
    manager: ModelAssetManager | None = None,
    progress: Callable[[int | None, str], object] | None = None,
    cancel_event: Event | None = None,
) -> Path:
    """Prepare the pinned model explicitly; recognition never downloads it."""
    if cancel_event is not None and cancel_event.is_set():
        raise ModelDownloadCancelled("Sparse dialogue model preparation cancelled")
    directory = (manager or ModelAssetManager()).download(
        _RECOGNIZER_ASSET.name,
        asset=_RECOGNIZER_ASSET,
        progress=progress,
        cancel_event=cancel_event,
    )
    path = directory / _RECOGNIZER_FILENAME
    if file_sha256(path, error_type=ModelIntegrityError) != _RECOGNIZER_SHA256:
        raise ModelIntegrityError(
            "PP-OCRv5 English mobile recognizer checksum differs from the publisher "
            "catalog. Remove the cached recognizer and prepare it again."
        )
    if cancel_event is not None and cancel_event.is_set():
        raise ModelDownloadCancelled("Sparse dialogue model preparation cancelled")
    return path


def _otsu(pixels: np.ndarray) -> int:
    histogram = np.bincount(pixels.ravel(), minlength=256).astype(float)
    weight = np.cumsum(histogram)
    total = np.cumsum(histogram * np.arange(256))
    variance = np.zeros(256)
    valid = (weight > 0) & (weight < weight[-1])
    variance[valid] = (total[-1] * weight[valid] - total[valid] * weight[-1]) ** 2 / (
        weight[valid] * (weight[-1] - weight[valid])
    )
    return int(np.argmax(variance))


def isolate_sparse_line(image: Image.Image) -> Image.Image | None:
    """Keep exactly one compact foreground group, without guessing its text."""
    if image.width <= 0 or image.height <= 0:
        return None
    pixels = np.asarray(image.convert("L"))
    if np.median(pixels) > 127:
        pixels = 255 - pixels
    if int(pixels.max()) - int(pixels.min()) < 25:
        return None
    threshold = _otsu(pixels)
    upper = pixels[pixels > threshold]
    if not upper.size:
        return None
    mask = pixels > max(threshold, _otsu(upper))
    components, _ = label(mask, structure=np.ones((3, 3)))
    clean = np.zeros_like(mask)
    for index, box in enumerate(find_objects(components), 1):
        if box is None:
            continue
        y, x = box
        height, width = y.stop - y.start, x.stop - x.start
        component = components[box] == index
        if (
            component.sum() >= 2
            and height <= image.height * 0.6
            and width <= image.width * 0.25
        ):
            clean[box] |= component
    radius = max(2, round(image.height * 0.04))
    groups, _ = label(maximum_filter(clean, size=(radius * 2 + 1, radius * 4 + 1)))
    candidates = []
    for box in find_objects(groups):
        y, x = box
        yy, xx = np.nonzero(clean[box])
        left, top = x.start + int(xx.min()), y.start + int(yy.min())
        right, bottom = x.start + int(xx.max()) + 1, y.start + int(yy.max()) + 1
        width, height = right - left, bottom - top
        if left == 0 or top == 0 or right == image.width or bottom == image.height:
            continue
        if max(width, height) < 3 or height > image.height * 0.65:
            continue
        if width <= image.width * 0.35:
            candidates.append((left, top, right, bottom))
    if len(candidates) != 1:
        return None
    crop = image.convert("RGB").crop(candidates[0])
    background = tuple(int(value) for value in np.median(np.asarray(crop), axis=(0, 1)))
    return ImageOps.expand(
        crop, border=max(2, round(crop.height * 0.1)), fill=background
    )


class SparseLineRecognizer:
    """One lazy worker-owned recognizer; calls receive an already isolated crop."""

    def __init__(self, model_path: str | Path) -> None:
        self.model_path = Path(model_path)
        self._recognizer: Callable[..., Any] | None = None

    def _load_model(self) -> None:
        if self._recognizer is not None:
            return
        if not self.model_path.is_file():
            raise RuntimeError(
                "Sparse dialogue recognition model is missing. Prepare it before reading."
            )
        try:
            import rapidocr
            from rapidocr.ch_ppocr_rec import TextRecInput, TextRecognizer
            from rapidocr.utils.parse_parameters import ParseParams
            from rapidocr.utils.typings import (
                EngineType,
                LangRec,
                ModelType,
                OCRVersion,
            )
        except ImportError as error:
            raise RuntimeError(
                "Sparse dialogue recognition requires RapidOCR and ONNX Runtime. "
                "Run `uv sync`."
            ) from error

        class LocalTextRecognizer(TextRecognizer):
            def get_character_dict(self, cfg: Any) -> tuple[list[str], None]:
                if not self.session.have_key():
                    raise RuntimeError(
                        "Sparse dialogue model has no embedded character dictionary; "
                        "no dictionary was downloaded."
                    )
                return self.session.get_character_list(), None

        cfg = ParseParams.load(Path(rapidocr.__file__).parent / "config.yaml")
        cfg.Rec.model_path = str(self.model_path)
        cfg.Rec.font_path = None
        cfg.Rec.ocr_version = OCRVersion.PPOCRV5
        cfg.Rec.model_type = ModelType.MOBILE
        cfg.Rec.lang_type = LangRec.EN
        cfg.Rec.engine_type = EngineType.ONNXRUNTIME
        cfg.Rec.engine_cfg = cfg.EngineConfig.onnxruntime
        cfg.Rec.engine_cfg.intra_op_num_threads = 1
        cfg.Rec.engine_cfg.inter_op_num_threads = 1
        recognizer = LocalTextRecognizer(cfg.Rec)
        self._recognizer = lambda pixels: recognizer(TextRecInput([pixels]))

    def recognize(self, image: Image.Image) -> tuple[str, float]:
        """Return model text and its measured confidence in 0..100."""
        if image.width <= 0 or image.height <= 0:
            raise ValueError("Sparse dialogue recognition requires a nonempty crop")
        self._load_model()
        assert self._recognizer is not None
        pixels = np.ascontiguousarray(np.asarray(image.convert("RGB"))[:, :, ::-1])
        result = self._recognizer(pixels)
        if (
            result.txts is None
            or result.scores is None
            or len(result.txts) != 1
            or len(result.scores) != 1
        ):
            raise ValueError(
                "Sparse dialogue recognizer returned no single line result"
            )
        text, confidence = result.txts[0], result.scores[0]
        if not isinstance(text, str):
            raise ValueError("Sparse dialogue recognizer returned invalid text")
        if isinstance(confidence, bool) or not isinstance(confidence, Real):
            raise ValueError("Sparse dialogue recognizer returned invalid confidence")
        score = float(confidence)
        if not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("Sparse dialogue recognizer returned invalid confidence")
        return text.strip(), score * 100
