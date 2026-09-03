"""Runtime-neutral identity for JSON-compatible documents."""

import hashlib
import json


def canonical_document_sha256(document):
    payload = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def is_lowercase_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = ["canonical_document_sha256", "is_lowercase_sha256"]
