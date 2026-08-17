"""Explicit named-speaker scope for live sessions without a story index."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

LIVE_SPEAKER_CORPUS_VERSION = 1


@dataclass(frozen=True)
class LiveSpeakerCorpus:
    name: str
    speakers: tuple[str, ...]
    path: Path
    sha256: str

    @classmethod
    def load(cls, path):
        selected_path = Path(path).expanduser()
        if selected_path.is_symlink():
            raise ValueError(
                f"live speaker corpus must not be a symlink: {selected_path}"
            )
        path = selected_path.resolve()
        if not path.is_file():
            raise ValueError(f"live speaker corpus is not a regular file: {path}")
        with selected_path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(
                    f"live speaker corpus is not a regular file: {selected_path}"
                )
            payload = source.read()
        if selected_path.is_symlink():
            raise ValueError(
                f"live speaker corpus must not be a symlink: {selected_path}"
            )
        current_path = selected_path.resolve()
        current = current_path.stat(follow_symlinks=False)
        if current_path != path or (opened.st_dev, opened.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise ValueError("live speaker corpus changed while it was being read")
        document = json.loads(payload.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("live speaker corpus root must be an object")
        version = document.get("schema_version")
        if isinstance(version, bool) or version != LIVE_SPEAKER_CORPUS_VERSION:
            raise ValueError(
                f"unsupported live speaker corpus schema version: {version}"
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
        return cls(
            name,
            tuple(speakers),
            path,
            hashlib.sha256(payload).hexdigest(),
        )

    def revalidate(self):
        current = type(self).load(self.path)
        if current.sha256 != self.sha256:
            raise ValueError(
                "live speaker corpus changed after settings were applied; "
                "apply settings again"
            )
        return self
