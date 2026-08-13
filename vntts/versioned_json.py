"""Versioned JSON loading and atomic publication for user-owned documents."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

from vntts.atomic_io import atomic_write_json

Document = TypeVar("Document")


def read_versioned_json(
    path,
    *,
    schema_version,
    document_name,
    allow_older=False,
    allow_unversioned=False,
):
    """Read one JSON object and enforce its document compatibility policy."""
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{document_name} root must be an object")
    if "schema_version" not in payload and allow_unversioned:
        return payload
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"{document_name} schema version is missing or invalid")
    if version > schema_version or (version != schema_version and not allow_older):
        raise ValueError(f"unsupported {document_name} schema version: {version}")
    return payload


def load_versioned_json(
    path,
    *,
    schema_version,
    document_name,
    decode: Callable[[dict], Document],
    fallback: Callable[[], Document],
    warn=None,
    allow_older=False,
    allow_unversioned=False,
):
    """Load and decode a document, returning a fresh fallback on any damage."""
    path = Path(path)
    if not path.is_file():
        return fallback()
    warn = (lambda _message: None) if warn is None else warn
    try:
        payload = read_versioned_json(
            path,
            schema_version=schema_version,
            document_name=document_name,
            allow_older=allow_older,
            allow_unversioned=allow_unversioned,
        )
        return decode(payload)
    except (
        AttributeError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        warn(f"Unable to load {document_name} from {path}: {error}")
        return fallback()


def write_versioned_json(path, schema_version, fields: Mapping):
    """Atomically publish a JSON object with one authoritative schema version."""
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        raise ValueError("document schema version must be a positive integer")
    supplied_version = fields.get("schema_version")
    if "schema_version" in fields and (
        isinstance(supplied_version, bool) or supplied_version != schema_version
    ):
        raise ValueError("document schema version conflicts with its writer")
    payload = dict(fields)
    payload["schema_version"] = schema_version
    return atomic_write_json(path, payload)
