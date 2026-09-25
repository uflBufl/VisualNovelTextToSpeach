from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

from vntts.versioned_json import (
    file_revision,
    load_versioned_json,
    read_versioned_json,
    write_versioned_json_if_unchanged,
)

OCR_REVIEW_SCHEMA_VERSION = 1


def _float_field(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError("OCR review number must be numeric")
    result = float(value)
    if not isfinite(result):
        raise ValueError("OCR review number must be finite")
    return result


def _int_field(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError("OCR review count must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        result = int(value)
    else:
        raise TypeError("OCR review count must be an integer")
    if result < 0:
        raise ValueError("OCR review count must not be negative")
    return result


@dataclass(frozen=True)
class OCRReviewSample:
    metadata_path: Path
    image_path: Path
    character: str
    text: str
    confidence: float
    minimum_confidence: float
    preprocessing_profile: str
    attempts: int


class OCRReviewStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser()

    def pending_samples(self) -> list[OCRReviewSample]:
        if not self.directory.is_dir():
            return []
        samples: list[OCRReviewSample] = []
        for metadata_path in sorted(
            self.directory.glob("uncertain-*.json"),
            reverse=True,
        ):
            sample = self._load_sample(metadata_path)
            if sample is not None:
                samples.append(sample)
        return samples

    def mark_resolved(
        self,
        sample: OCRReviewSample,
        *,
        scope: str | None = None,
        corrections: Mapping[str, str] | None = None,
    ) -> None:
        revision = file_revision(sample.metadata_path)
        payload = read_versioned_json(
            sample.metadata_path,
            schema_version=OCR_REVIEW_SCHEMA_VERSION,
            document_name="OCR review metadata",
            allow_unversioned=True,
        )
        if self._sample_from_payload(sample.metadata_path, payload) != sample:
            raise RuntimeError("OCR review sample changed before resolution")
        payload["resolved"] = True
        payload["resolved_at"] = datetime.now(timezone.utc).isoformat()
        if scope:
            payload["correction_scope"] = scope
        if corrections:
            payload["corrections"] = dict(corrections)
        write_versioned_json_if_unchanged(
            sample.metadata_path,
            OCR_REVIEW_SCHEMA_VERSION,
            payload,
            revision=revision,
            document_name="OCR review metadata",
        )

    def _load_sample(self, metadata_path: Path) -> OCRReviewSample | None:
        if metadata_path.is_symlink():
            return None

        def decode(payload: dict[str, object]) -> OCRReviewSample | None:
            return self._sample_from_payload(metadata_path, payload)

        def fallback() -> None:
            return None

        sample = load_versioned_json(
            metadata_path,
            schema_version=OCR_REVIEW_SCHEMA_VERSION,
            document_name="OCR review metadata",
            decode=decode,
            fallback=fallback,
            allow_unversioned=True,
        )
        if sample is not None and not isinstance(sample, OCRReviewSample):
            raise TypeError("OCR review loader returned an invalid sample")
        return sample

    @staticmethod
    def _sample_from_payload(
        metadata_path: Path,
        payload: dict[str, object],
    ) -> OCRReviewSample | None:
        if payload.get("resolved") is True:
            return None
        image = payload["image"]
        if not isinstance(image, str):
            raise TypeError("OCR review image must be a path string")
        image_name = Path(image)
        if image_name.is_absolute() or image_name.name != image:
            raise ValueError("OCR review image must be in the review directory")
        image_path = metadata_path.parent / image_name
        if image_path.is_symlink() or not image_path.is_file():
            return None
        return OCRReviewSample(
            metadata_path=metadata_path,
            image_path=image_path,
            character=str(payload.get("character") or "Narrator"),
            text=str(payload.get("text") or ""),
            confidence=_float_field(payload.get("confidence", 0)),
            minimum_confidence=_float_field(payload.get("minimum_confidence", 0)),
            preprocessing_profile=str(
                payload.get("preprocessing_profile") or "unknown"
            ),
            attempts=_int_field(payload.get("attempts", 0)),
        )
