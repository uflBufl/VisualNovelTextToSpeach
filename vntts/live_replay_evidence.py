"""Runtime-neutral admission and decoding of immutable replay evidence."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import TypeAlias

from vntts.json_types import decode_json, is_json_object
from vntts.path_safety import open_regular_candidate

PathInput: TypeAlias = str | os.PathLike[str]
JSONDocument: TypeAlias = dict[str, object]


class SequenceReplaySealError(RuntimeError):
    """A raw capture cannot be safely bound to one exact sequence."""


def _decode_json(payload: bytes, label: str) -> JSONDocument:
    try:
        document = decode_json(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SequenceReplaySealError(f"{label} is not valid JSON: {error}") from error
    if not is_json_object(document):
        raise SequenceReplaySealError(f"{label} root must be an object")
    return document


def _read_regular_file(value: PathInput, label: str) -> tuple[Path, bytes]:
    selected = Path(value).expanduser()
    if selected.is_symlink():
        raise SequenceReplaySealError(f"{label} must not be a symlink: {selected}")
    path = selected.resolve()
    try:
        with open(path, "rb", opener=open_regular_candidate) as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise SequenceReplaySealError(f"{label} must be a regular file: {path}")
            payload = source.read()
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise SequenceReplaySealError(f"Unable to read {label}: {error}") from error
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise SequenceReplaySealError(f"{label} changed while being read: {path}")
    return path, payload
