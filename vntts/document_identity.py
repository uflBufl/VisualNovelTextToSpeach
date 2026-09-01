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


__all__ = ["canonical_document_sha256"]
