"""Versioned JSON loading and atomic publication for user-owned documents."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import TypeVar

from durable_file import atomic_write_json

from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.json_types import decode_json
from vntts.path_safety import open_regular_binary

Document = TypeVar("Document")
_DOCUMENT_READ_LIMIT = 64 * 1024 * 1024


class StaleDocumentError(OSError):
    """A document changed since it was loaded and must be reopened."""


def read_versioned_json_snapshot(
    path: str | Path,
    *,
    schema_version: int,
    document_name: str,
    allow_older: bool = False,
    allow_unversioned: bool = False,
) -> tuple[dict[str, object], bytes]:
    """Read one document, returning its payload and exact raw-byte digest."""
    path = Path(path)
    with open_regular_binary(path) as source:
        raw = source.read(_DOCUMENT_READ_LIMIT + 1)
    if len(raw) > _DOCUMENT_READ_LIMIT:
        raise ValueError(f"{document_name} exceeds the size limit")
    revision = sha256(raw).digest()
    payload = decode_json(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{document_name} root must be an object")
    if "schema_version" not in payload and allow_unversioned:
        return payload, revision
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"{document_name} schema version is missing or invalid")
    if version > schema_version or (version != schema_version and not allow_older):
        raise ValueError(f"unsupported {document_name} schema version: {version}")
    return payload, revision


def read_versioned_json(
    path: str | Path,
    *,
    schema_version: int,
    document_name: str,
    allow_older: bool = False,
    allow_unversioned: bool = False,
) -> dict[str, object]:
    """Read one JSON object and enforce its document compatibility policy."""
    payload, _revision = read_versioned_json_snapshot(
        path,
        schema_version=schema_version,
        document_name=document_name,
        allow_older=allow_older,
        allow_unversioned=allow_unversioned,
    )
    return payload


def load_versioned_json(
    path: str | Path,
    *,
    schema_version: int,
    document_name: str,
    decode: Callable[[dict[str, object]], Document],
    fallback: Callable[[], Document],
    warn: Callable[[str], object] | None = None,
    allow_older: bool = False,
    allow_unversioned: bool = False,
    on_revision: Callable[[bytes], None] | None = None,
) -> Document:
    """Load and decode a document, returning a fresh fallback on any damage."""
    path = Path(path)
    if not path.is_file():
        return fallback()
    warn = (lambda _message: None) if warn is None else warn
    try:
        payload, revision = read_versioned_json_snapshot(
            path,
            schema_version=schema_version,
            document_name=document_name,
            allow_older=allow_older,
            allow_unversioned=allow_unversioned,
        )
        document = decode(payload)
        if on_revision is not None:
            on_revision(revision)
        return document
    except (
        AttributeError,
        OSError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        warn(f"Unable to load {document_name} from {path}: {error}")
        return fallback()


def write_versioned_json(
    path: str | Path,
    schema_version: int,
    fields: Mapping[str, object],
) -> Path:
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


def file_revision(path: Path) -> bytes | None:
    try:
        with open_regular_binary(path) as source:
            raw = source.read(_DOCUMENT_READ_LIMIT + 1)
    except FileNotFoundError:
        return None
    if len(raw) > _DOCUMENT_READ_LIMIT:
        raise OSError(f"{path} exceeds the document size limit")
    return sha256(raw).digest()


def write_versioned_json_if_unchanged(
    path: Path,
    schema_version: int,
    fields: Mapping[str, object],
    *,
    revision: bytes | None,
    document_name: str,
) -> bytes:
    """Reject stale whole-document writes while keeping atomic publication."""
    lock_path = path.with_name(f"{path.name}.lock")
    with exclusive_advisory_lock(lock_path, blocking=True):
        if file_revision(path) != revision:
            raise StaleDocumentError(
                f"{document_name} changed on disk; reopen before saving"
            )
        write_versioned_json(path, schema_version, fields)
        updated = file_revision(path)
        if updated is None:
            raise OSError(f"{document_name} disappeared after saving")
        return updated
