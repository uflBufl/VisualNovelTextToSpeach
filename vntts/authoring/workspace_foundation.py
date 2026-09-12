"""Leaf filesystem and value primitives for authoring workspaces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, MutableSequence
from pathlib import Path

from vntts.path_safety import contained_path, contained_regular_file, safe_relative_path


def read_regular_file(
    path: str | Path,
    label: str,
    *,
    error_type: type[Exception] = ValueError,
) -> bytes:
    """Read one non-symlink regular file with stable authoring error messages."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise error_type(f"{label.capitalize()} is missing or unsafe")
    try:
        return path.read_bytes()
    except OSError as error:
        raise error_type(f"Unable to read {label}: {error}") from error


def load_json_object(
    path: str | Path,
    description: str,
    *,
    error_type: type[Exception] = ValueError,
    object_label: str | None = None,
) -> dict[str, object]:
    """Load one JSON object from a filesystem path."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise error_type(f"Unable to read {description} {path}: {error}") from error
    if not isinstance(value, dict):
        raise error_type(f"{object_label or description.title()} must be a JSON object")
    return value


def load_json_object_snapshot(
    path: str | Path,
    description: str,
    *,
    error_type: type[Exception] = ValueError,
) -> tuple[dict[str, object], str, bytes]:
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


def require_sha256(
    value: object, label: str, *, error_type: type[Exception] = ValueError
) -> str:
    """Return one full hexadecimal SHA-256 value or fail with a typed error."""
    if not isinstance(value, str) or len(value) != 64:
        raise error_type(f"{label} must be a full SHA-256")
    try:
        int(value, 16)
    except ValueError as error:
        raise error_type(f"{label} must be hexadecimal") from error
    return value


def copy_workspace_tree_snapshot(
    source: str | Path,
    target: str | Path,
    snapshots: MutableSequence[tuple[Path, str]],
    *,
    error_type: type[Exception] = ValueError,
) -> None:
    """Copy one symlink-free immutable input tree and retain source hashes."""
    source = Path(source)
    target = Path(target)
    if source.is_symlink() or not source.is_dir():
        raise error_type("Outcome merge immutable input tree is invalid")
    target.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise error_type("Outcome merge immutable input tree contains a symlink")
        if path.is_dir():
            continue
        relative = path.relative_to(source)
        payload = read_regular_file(
            path,
            "outcome merge immutable input",
            error_type=error_type,
        )
        digest = hashlib.sha256(payload).hexdigest()
        destination = contained_path(
            target,
            relative,
            "Outcome merge immutable input",
            error_type=error_type,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        snapshots.append((path, digest))


def copy_generation_wavs(
    base_directory: str | Path,
    output: str | Path,
    state: Mapping[str, Mapping[str, Mapping[str, object]]],
    snapshots: MutableSequence[tuple[Path, str]],
    target_label: str,
    error_type: type[Exception] = ValueError,
    source_label: str = "Base WAV",
) -> None:
    """Copy checksum-bound generated WAVs into an immutable successor."""
    owners: dict[str, str] = {}
    for queue_id, result in state["items"].items():
        if not isinstance(result, dict) or not isinstance(result.get("path"), str):
            continue
        relative = safe_relative_path(
            result["path"],
            f"Base generation item {queue_id!r} path",
            error_type=error_type,
        )
        owner = owners.setdefault(relative.as_posix(), queue_id)
        if owner != queue_id:
            raise error_type(f"{source_label} path collides with {owner!r}")
        source = contained_path(
            Path(base_directory) / "generated-audio",
            relative,
            "Base generation WAV",
            error_type=error_type,
        )
        payload = read_regular_file(
            source, "base generation WAV", error_type=error_type
        )
        digest = hashlib.sha256(payload).hexdigest()
        expected = require_sha256(
            result.get("file_sha256"),
            f"Base item {queue_id!r} WAV SHA-256",
            error_type=error_type,
        )
        if digest != expected:
            raise error_type(f"{source_label} changed for {queue_id!r}")
        target = contained_path(output, relative, target_label, error_type=error_type)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        snapshots.append((source, digest))


__all__ = [
    "contained_path",
    "contained_regular_file",
    "copy_generation_wavs",
    "copy_workspace_tree_snapshot",
    "load_json_object",
    "load_json_object_snapshot",
    "read_regular_file",
    "require_sha256",
    "safe_relative_path",
]
