import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts.atomic_io import atomic_write_json


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
    def __init__(self, directory):
        self.directory = Path(directory).expanduser()

    def pending_samples(self):
        if not self.directory.is_dir():
            return []
        samples = []
        for metadata_path in sorted(
            self.directory.glob("uncertain-*.json"),
            reverse=True,
        ):
            sample = self._load_sample(metadata_path)
            if sample is not None:
                samples.append(sample)
        return samples

    def mark_resolved(self, sample, *, scope=None, corrections=None):
        payload = json.loads(sample.metadata_path.read_text(encoding="utf-8"))
        payload["resolved"] = True
        payload["resolved_at"] = datetime.now(timezone.utc).isoformat()
        if scope:
            payload["correction_scope"] = scope
        if corrections:
            payload["corrections"] = dict(corrections)
        atomic_write_json(sample.metadata_path, payload)

    def _load_sample(self, metadata_path):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            if payload.get("resolved") is True:
                return None
            image_path = metadata_path.parent / payload["image"]
            if not image_path.is_file():
                return None
            return OCRReviewSample(
                metadata_path=metadata_path,
                image_path=image_path,
                character=str(payload.get("character") or "Narrator"),
                text=str(payload.get("text") or ""),
                confidence=float(payload.get("confidence", 0)),
                minimum_confidence=float(payload.get("minimum_confidence", 0)),
                preprocessing_profile=str(
                    payload.get("preprocessing_profile") or "unknown"
                ),
                attempts=int(payload.get("attempts", 0)),
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
