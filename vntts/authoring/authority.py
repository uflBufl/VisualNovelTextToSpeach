"""Shared immutable-file primitives for authoring authority projections."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


class AuthoringAuthorityError(RuntimeError):
    """An immutable authoring input cannot be captured or published safely."""


@dataclass(frozen=True)
class AuthoritySnapshot:
    """One exact regular-file payload captured from a canonical path."""

    path: Path
    payload: bytes
    sha256: str

    def json_document(self, label):
        """Decode one captured JSON object without reopening the source path."""
        try:
            document = json.loads(self.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AuthoringAuthorityError(f"Unable to read {label}: {error}") from error
        if not isinstance(document, dict):
            raise AuthoringAuthorityError(f"{label.capitalize()} must be an object")
        return document


def canonical_document_sha256(document):
    """Return the stable SHA-256 of one JSON-compatible document."""
    payload = json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def capture_authority_file(path, label, *, root=None):
    """Read one non-symlink regular file once and retain its exact identity."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = candidate.resolve()
    if candidate.is_symlink() or not candidate.is_file():
        raise AuthoringAuthorityError(
            f"{label.capitalize()} is unavailable: {candidate}"
        )
    resolved = candidate.resolve()
    if root is not None:
        canonical_root = Path(root).expanduser().resolve()
        try:
            resolved.relative_to(canonical_root)
        except ValueError as error:
            raise AuthoringAuthorityError(
                f"{label.capitalize()} leaves its canonical root"
            ) from error
    try:
        payload = candidate.read_bytes()
    except OSError as error:
        raise AuthoringAuthorityError(f"Unable to read {label}: {error}") from error
    if (
        candidate.is_symlink()
        or not candidate.is_file()
        or candidate.resolve() != resolved
    ):
        raise AuthoringAuthorityError(
            f"{label.capitalize()} changed while it was captured: {candidate}"
        )
    return AuthoritySnapshot(
        resolved,
        payload,
        hashlib.sha256(payload).hexdigest(),
    )


def assert_authority_snapshot(snapshot, label="authority"):
    """Require one captured path to remain the same regular-file payload."""
    path = snapshot.path
    if path.is_symlink() or not path.is_file():
        raise AuthoringAuthorityError(f"{label.capitalize()} changed: {path}")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise AuthoringAuthorityError(f"Unable to recheck {label}: {error}") from error
    if hashlib.sha256(payload).hexdigest() != snapshot.sha256:
        raise AuthoringAuthorityError(f"{label.capitalize()} changed: {path}")


def write_json_document_no_replace(output, document, label):
    """Atomically publish one JSON document while refusing replacement."""
    path = Path(output).expanduser().resolve()
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as error:
        raise AuthoringAuthorityError(
            f"{label.title()} output exists: {path}"
        ) from error
    except OSError as error:
        raise AuthoringAuthorityError(
            f"Unable to publish {label} {path}: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


__all__ = [
    "AuthoritySnapshot",
    "AuthoringAuthorityError",
    "assert_authority_snapshot",
    "canonical_document_sha256",
    "capture_authority_file",
    "write_json_document_no_replace",
]
