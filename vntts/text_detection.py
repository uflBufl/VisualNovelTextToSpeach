"""Full-frame text candidates; detection never grants permission to speak."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from importlib import import_module
from numbers import Real
from pathlib import Path
from threading import Event
from typing import Any

import numpy as np
from PIL import Image

from vntts.asset_integrity import ModelIntegrityError
from vntts.document_identity import file_sha256
from vntts.model_assets import ModelAsset, ModelAssetManager, ModelDownloadCancelled
from vntts.ocr import DialogRegion

_DETECTOR_ASSET = ModelAsset(
    name="rapidocr/ppocrv4-mobile-det",
    urls=(
        "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/"
        "onnx/PP-OCRv4/det/ch_PP-OCRv4_det_mobile.onnx",
    ),
)
_DETECTOR_SHA256 = "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9"


def prepare_text_detection_models(
    manager: ModelAssetManager | None = None,
    progress: Callable[[int | None, str], object] | None = None,
    cancel_event: Event | None = None,
) -> tuple[Path, Path]:
    """Explicit runtime preparation; inference itself never downloads assets."""
    if cancel_event is not None and cancel_event.is_set():
        raise ModelDownloadCancelled("Screen text model preparation cancelled")
    try:
        rapidocr = import_module("rapidocr")
        import_module("onnxruntime")
    except ImportError as error:
        raise RuntimeError(
            "Screen text detection requires RapidOCR and ONNX Runtime. Run `uv sync`."
        ) from error
    package_file = rapidocr.__file__
    if package_file is None:
        raise RuntimeError("RapidOCR installation has no model directory")
    recognizer_path = Path(package_file).parent / "models" / "PP-OCRv6_rec_small.onnx"
    if not recognizer_path.is_file():
        raise RuntimeError(
            f"Screen text recognition model is missing: {recognizer_path}. "
            "Install a RapidOCR release bundling PP-OCRv6 small recognition."
        )
    directory = (manager or ModelAssetManager()).download(
        _DETECTOR_ASSET.name,
        asset=_DETECTOR_ASSET,
        progress=progress,
        cancel_event=cancel_event,
    )
    detector_path = directory / "ch_PP-OCRv4_det_mobile.onnx"
    if file_sha256(detector_path, error_type=ModelIntegrityError) != _DETECTOR_SHA256:
        raise ModelIntegrityError(
            "PP-OCRv4 mobile detector checksum differs from the publisher catalog. "
            "Remove the cached detector and prepare it again."
        )
    if cancel_event is not None and cancel_event.is_set():
        raise ModelDownloadCancelled("Screen text model preparation cancelled")
    return detector_path, recognizer_path


@dataclass(frozen=True)
class TextLine:
    """Frame-relative normalized geometry and recognition confidence in 0..100."""

    box: DialogRegion
    text: str
    confidence: float

    def __post_init__(self) -> None:
        if not isinstance(self.box, DialogRegion) or not isinstance(self.text, str):
            raise ValueError("Text lines require a normalized box and text")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, Real)
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 100
        ):
            raise ValueError(
                "Text line confidence must be finite and between 0 and 100"
            )


@dataclass(frozen=True)
class TextBlock:
    lines: tuple[TextLine, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.lines, tuple)
            or not self.lines
            or not all(isinstance(line, TextLine) for line in self.lines)
        ):
            raise ValueError("Text blocks require a nonempty tuple of lines")

    @property
    def box(self) -> DialogRegion:
        left = min(line.box.left for line in self.lines)
        top = min(line.box.top for line in self.lines)
        right = max(line.box.left + line.box.width for line in self.lines)
        bottom = max(line.box.top + line.box.height for line in self.lines)
        return DialogRegion(left, top, right - left, bottom - top)

    @property
    def text(self) -> str:
        return " ".join(line.text for line in self.lines)


def group_text_lines(lines: Iterable[TextLine]) -> tuple[TextBlock, ...]:
    """Group neighboring, similarly sized lines without screen-position rules."""
    rows = tuple(lines)
    return tuple(
        TextBlock(tuple(rows[index] for index in group))
        for group in _group_box_indices(tuple(line.box for line in rows))
    )


def center_in_region(box: DialogRegion, region: DialogRegion) -> bool:
    x, y = box.left + box.width / 2, box.top + box.height / 2
    return (
        region.left <= x <= region.left + region.width
        and region.top <= y <= region.top + region.height
    )


def _group_box_indices(boxes: Sequence[DialogRegion]) -> tuple[tuple[int, ...], ...]:
    def position(index: int) -> tuple[float, float]:
        return boxes[index].top, boxes[index].left

    groups: list[list[int]] = []
    for index in sorted(range(len(boxes)), key=position):
        neighbors = [
            group
            for group in groups
            if any(_neighbor(boxes[item], boxes[index]) for item in group)
        ]
        if not neighbors:
            groups.append([index])
            continue
        neighbors[0].append(index)
        for group in neighbors[1:]:
            neighbors[0].extend(group)
            groups.remove(group)
    return tuple(tuple(sorted(group, key=position)) for group in groups)


def _neighbor(a: DialogRegion, b: DialogRegion) -> bool:
    height = min(a.height, b.height)
    if max(a.height, b.height) > 1.7 * height:
        return False
    if a.top > b.top:
        a, b = b, a
    gap = b.top - (a.top + a.height)
    overlap = min(a.left + a.width, b.left + b.width) - max(a.left, b.left)
    return -0.25 * height <= gap <= 1.8 * height and (
        overlap >= 0.35 * min(a.width, b.width) or abs(a.left - b.left) <= 0.75 * height
    )


class ScreenTextDetector:
    """Lazy PP-OCRv4 mobile detection and PP-OCRv6 small line recognition.

    Models must already exist in RapidOCR's installed models directory, or be
    supplied explicitly. No model, dictionary or font downloads are performed.
    A detector belongs to one worker; inference calls must not overlap.
    """

    def __init__(
        self,
        *,
        detector_path: str | Path | None = None,
        recognizer_path: str | Path | None = None,
    ) -> None:
        self.detector_path = detector_path
        self.recognizer_path = recognizer_path
        self._detector: Callable[..., Any] | None = None
        self._recognizer: Callable[..., Any] | None = None
        self._crop_line: Callable[..., Any] | None = None

    def _load_models(self) -> None:
        if self._detector is not None:
            return
        try:
            import onnxruntime  # noqa: F401
            import rapidocr
            from rapidocr.ch_ppocr_det import TextDetector
            from rapidocr.ch_ppocr_rec import TextRecInput, TextRecognizer
            from rapidocr.utils.parse_parameters import ParseParams
            from rapidocr.utils.process_img import get_rotate_crop_image
            from rapidocr.utils.typings import ModelType, OCRVersion
        except ImportError as error:
            raise RuntimeError(
                "Screen text detection requires RapidOCR and ONNX Runtime. "
                "Run `uv sync`."
            ) from error

        root = Path(rapidocr.__file__).parent
        detector_path = (
            Path(self.detector_path)
            if self.detector_path
            else (root / "models" / "ch_PP-OCRv4_det_mobile.onnx")
        )
        recognizer_path = (
            Path(self.recognizer_path)
            if self.recognizer_path
            else (root / "models" / "PP-OCRv6_rec_small.onnx")
        )
        for path in (detector_path, recognizer_path):
            if not path.is_file():
                raise RuntimeError(
                    f"Screen text model is missing: {path}. Prepare PP-OCRv4 mobile "
                    "detection and PP-OCRv6 small recognition assets before reading; "
                    "or pass their local paths to ScreenTextDetector."
                )

        class LocalTextRecognizer(TextRecognizer):
            def get_character_dict(self, cfg: Any) -> tuple[list[str], None]:
                if not self.session.have_key():
                    raise RuntimeError(
                        "Screen text recognition requires an ONNX model with an "
                        "embedded character dictionary; no dictionary was downloaded."
                    )
                return self.session.get_character_list(), None

        cfg = ParseParams.load(root / "config.yaml")
        cfg.Det.ocr_version = OCRVersion.PPOCRV4
        cfg.Det.model_type = ModelType.MOBILE
        cfg.Det.model_path = str(detector_path)
        cfg.Det.limit_side_len = 640
        cfg.Det.limit_type = "max"
        cfg.Rec.ocr_version = OCRVersion.PPOCRV6
        cfg.Rec.model_type = ModelType.SMALL
        cfg.Rec.model_path = str(recognizer_path)
        cfg.Rec.font_path = None
        for section in (cfg.Det, cfg.Rec):
            section.engine_cfg = cfg.EngineConfig.onnxruntime
            section.engine_cfg.intra_op_num_threads = 1
            section.engine_cfg.inter_op_num_threads = 1
        detector = TextDetector(cfg.Det)
        recognizer = LocalTextRecognizer(cfg.Rec)
        self._recognizer = lambda crops: recognizer(TextRecInput(crops))
        self._crop_line = get_rotate_crop_image
        self._detector = detector

    def detect(
        self,
        image: Image.Image,
        *,
        allowed_region: DialogRegion | None = None,
        covered_region: DialogRegion | None = None,
    ) -> tuple[TextLine, ...]:
        if image.width <= 0 or image.height <= 0:
            raise ValueError("Screen text detection requires a nonempty image")
        self._load_models()
        assert self._detector and self._recognizer and self._crop_line
        original = image.convert("RGB")
        small = original.copy()
        small.thumbnail((640, 640), Image.Resampling.LANCZOS)
        pixels = np.ascontiguousarray(np.asarray(small)[:, :, ::-1])
        output = self._detector(pixels)
        if output.boxes is None or not len(output.boxes):
            return ()
        boxes = np.asarray(output.boxes, dtype=np.float64)
        if boxes.ndim != 3 or boxes.shape[1:] != (4, 2) or not np.isfinite(boxes).all():
            raise ValueError("Screen text detector returned malformed line boxes")
        boxes *= np.array(
            [original.width / small.width, original.height / small.height]
        )
        boxes[:, :, 0] = boxes[:, :, 0].clip(0, original.width)
        boxes[:, :, 1] = boxes[:, :, 1].clip(0, original.height)
        normalized: list[DialogRegion] = []
        valid_boxes = []
        for box in boxes:
            left, top = box.min(axis=0) / original.size
            right, bottom = box.max(axis=0) / original.size
            if right <= left or bottom <= top:
                raise ValueError("Screen text detector returned an empty line box")
            region = DialogRegion(
                float(left), float(top), float(right - left), float(bottom - top)
            )
            if allowed_region is not None and not allowed_region.contains(region):
                continue
            normalized.append(region)
            valid_boxes.append(box.astype(np.float32))
        if covered_region is not None:
            # Keep mixed groups whole: an inside line can complete a caption.
            retained = {
                index
                for group in _group_box_indices(normalized)
                if any(
                    not center_in_region(normalized[item], covered_region)
                    for item in group
                )
                for index in group
            }
            valid_boxes = [
                box for index, box in enumerate(valid_boxes) if index in retained
            ]
            normalized = [
                region for index, region in enumerate(normalized) if index in retained
            ]
        if not valid_boxes:
            return ()
        full_pixels = np.ascontiguousarray(np.asarray(original)[:, :, ::-1])
        recognized = self._recognizer(
            [self._crop_line(full_pixels, box) for box in valid_boxes]
        )
        if recognized.txts is None or recognized.scores is None:
            raise ValueError("Screen text recognizer returned no line results")
        lines = []
        for box, text, score in zip(
            normalized, recognized.txts, recognized.scores, strict=True
        ):
            if not isinstance(text, str):
                raise ValueError("Screen text recognizer returned invalid text")
            if isinstance(score, bool) or not isinstance(score, Real):
                raise ValueError("Screen text recognizer returned invalid confidence")
            line = TextLine(box, text.strip(), float(score) * 100)
            if line.text:
                lines.append(line)
        return tuple(sorted(lines, key=lambda line: (line.box.top, line.box.left)))
