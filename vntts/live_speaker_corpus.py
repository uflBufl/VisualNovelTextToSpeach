"""Explicit named-speaker scope for live sessions without a story index."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vntts.versioned_json import read_versioned_json

LIVE_SPEAKER_CORPUS_VERSION = 1


@dataclass(frozen=True)
class LiveSpeakerCorpus:
    name: str
    speakers: tuple[str, ...]

    @classmethod
    def load(cls, path):
        path = Path(path).expanduser().resolve()
        document = read_versioned_json(
            path,
            schema_version=LIVE_SPEAKER_CORPUS_VERSION,
            document_name="live speaker corpus",
        )
        raw_speakers = document.get("speakers")
        if not isinstance(raw_speakers, list) or not raw_speakers:
            raise ValueError(
                "live speaker corpus must contain a non-empty speakers list"
            )
        speakers = []
        seen = set()
        for index, value in enumerate(raw_speakers, start=1):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"live speaker corpus speaker {index} must be a non-empty string"
                )
            speaker = value.strip()
            key = speaker.casefold()
            if key in seen:
                raise ValueError(f"duplicate live speaker corpus entry: {speaker}")
            seen.add(key)
            speakers.append(speaker)
        name = str(document.get("name") or path.stem).strip() or path.stem
        return cls(name, tuple(speakers))
