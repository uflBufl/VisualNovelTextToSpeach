"""Runtime-neutral identity for JSON-compatible documents."""

import hashlib
import json
from typing import TypeGuard


def canonical_document_sha256(document: object, *, allow_nan: bool = False) -> str:
    """Hash canonical JSON; strict finite JSON is default, legacy NaN is opt-in."""
    payload = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=allow_nan,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def is_lowercase_sha256(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = ["canonical_document_sha256", "is_lowercase_sha256"]
