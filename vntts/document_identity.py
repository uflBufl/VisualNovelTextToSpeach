"""Runtime-neutral checksums for documents and files."""

import hashlib
import json
from os import PathLike
from string import hexdigits
from typing import TypeGuard

from durable_file import sha256_file


def file_sha256(
    path: str | PathLike[str], *, error_type: type[Exception] = ValueError
) -> str:
    """Stream one file checksum through the caller's domain error boundary."""
    try:
        return sha256_file(path)
    except OSError as error:
        raise error_type(str(error)) from error


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


def is_sha256(value: object) -> TypeGuard[str]:
    """Require exactly 64 ASCII hexadecimal digits, retaining either case."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in hexdigits for character in value)
    )


def is_lowercase_sha256(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "canonical_document_sha256",
    "file_sha256",
    "is_lowercase_sha256",
    "is_sha256",
]
