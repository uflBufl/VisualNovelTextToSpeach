"""Leaf filesystem and value primitives for authoring workspaces."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath


def safe_relative_path(value, label, *, error_type=ValueError):
    """Validate one canonical POSIX-relative path without touching the filesystem."""
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise error_type(f"{label} must be a POSIX-relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise error_type(f"{label} must stay inside its workspace")
    return Path(*pure.parts)


def contained_path(root, relative, label, *, error_type=ValueError):
    """Resolve a relative path and require it to stay inside its canonical root."""
    root = Path(root).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise error_type(f"{label} leaves its owning directory") from error
    return path


def read_regular_file(path, label, *, error_type=ValueError):
    """Read one non-symlink regular file with stable authoring error messages."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise error_type(f"{label.capitalize()} is missing or unsafe")
    try:
        return path.read_bytes()
    except OSError as error:
        raise error_type(f"Unable to read {label}: {error}") from error


def load_json_object(path, description, *, error_type=ValueError):
    """Load one JSON object from a filesystem path."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise error_type(f"Unable to read {description} {path}: {error}") from error
    if not isinstance(value, dict):
        raise error_type(f"{description.title()} must be a JSON object")
    return value


def load_json_object_snapshot(path, description, *, error_type=ValueError):
    """Read and decode one JSON object while retaining exact payload identity."""
    try:
        payload = Path(path).read_bytes()
    except OSError as error:
        raise error_type(f"Unable to read {description} {path}: {error}") from error
    digest = hashlib.sha256(payload).hexdigest()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise error_type(f"Unable to read {description} {path}: {error}") from error
    if not isinstance(value, dict):
        raise error_type(f"{description.title()} must be a JSON object")
    return value, digest, payload


def require_sha256(value, label, *, error_type=ValueError):
    """Return one full hexadecimal SHA-256 value or fail with a typed error."""
    if not isinstance(value, str) or len(value) != 64:
        raise error_type(f"{label} must be a full SHA-256")
    try:
        int(value, 16)
    except ValueError as error:
        raise error_type(f"{label} must be hexadecimal") from error
    return value


__all__ = [
    "contained_path",
    "load_json_object",
    "load_json_object_snapshot",
    "read_regular_file",
    "require_sha256",
    "safe_relative_path",
]
