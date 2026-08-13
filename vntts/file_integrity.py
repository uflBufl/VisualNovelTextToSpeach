"""Canonical streaming file-integrity helpers."""

import hashlib
from pathlib import Path


def sha256_file(path, *, chunk_size=1024 * 1024):
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
