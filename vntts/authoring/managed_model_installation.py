"""Shared filesystem lifecycle for pinned local authoring models."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Mapping

from vntts_artifacts.atomic_io import atomic_write_json, atomic_write_text
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
)


@dataclass(frozen=True)
class ManagedModelFiles:
    """Immutable files and integrity controls for one pinned model."""

    model_id: str
    repository: str
    revision: str
    files: tuple[str, ...]
    tree_sha256: str | None = None
    file_sha256s: Mapping[str, str] | None = None


def model_installation(root, model):
    return Path(root) / model.model_id / model.revision


def _tree_sha256(path):
    digest = hashlib.sha256()
    for candidate in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
        if candidate.is_file():
            relative = candidate.relative_to(path).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(bytes.fromhex(sha256_file(candidate)))
    return digest.hexdigest()


def _verify(model_directory, model):
    actual_files = {}
    for filename, expected in (model.file_sha256s or {}).items():
        path = model_directory / filename
        actual_files[filename] = sha256_file(path) if path.is_file() else None
        if actual_files[filename] != expected:
            return f"model file changed: {filename}", None, actual_files
    actual_tree = _tree_sha256(model_directory)
    if model.tree_sha256 is not None and actual_tree != model.tree_sha256:
        return "model tree checksum changed", actual_tree, actual_files
    return None, actual_tree, actual_files


def managed_model_status(
    installation, model, *, metadata, notice, error_type=RuntimeError
):
    """Return common read-only status for one immutable model installation."""
    installation = Path(installation)
    model_directory = installation / "model"
    status, reason, actual_tree, actual_files = "missing", None, None, {}
    if installation.exists():
        if not installation.is_dir() or not model_directory.is_dir():
            status, reason = "invalid", "installation shape is invalid"
        else:
            try:
                reason, actual_tree, actual_files = _verify(model_directory, model)
            except OSError as error:
                raise error_type(
                    f"Unable to read managed model {model_directory}: {error}"
                ) from error
            if reason:
                status = "invalid"
            else:
                try:
                    actual_metadata = json.loads(
                        (installation / "managed-model.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    actual_notice = (
                        installation / "THIRD_PARTY_NOTICES.txt"
                    ).read_text(encoding="utf-8")
                except (OSError, ValueError) as error:
                    status, reason = (
                        "invalid",
                        f"installation metadata is unavailable: {error}",
                    )
                else:
                    if actual_metadata != metadata or actual_notice != notice:
                        status, reason = "invalid", "installation metadata changed"
                    else:
                        status = "installed"
    result = {
        "model_id": model.model_id,
        "repository": model.repository,
        "revision": model.revision,
        "installation": str(installation),
        "model_directory": str(model_directory),
        "status": status,
        "reason": reason,
    }
    if model.tree_sha256 is not None:
        result.update(
            expected_tree_sha256=model.tree_sha256,
            actual_tree_sha256=actual_tree,
        )
    if model.file_sha256s is not None:
        result.update(expected_files=model.file_sha256s, actual_files=actual_files)
    return result


def install_managed_model(
    installation,
    model,
    *,
    metadata,
    notice,
    source=None,
    fetch_file,
    error_type=RuntimeError,
    model_label="model",
):
    """Copy, verify and atomically publish one pinned local model."""
    status_args = {"metadata": metadata, "notice": notice, "error_type": error_type}
    existing = managed_model_status(installation, model, **status_args)
    if existing["status"] == "installed":
        return existing
    if existing["status"] == "invalid":
        raise error_type(
            f"Refusing to overwrite an invalid managed {model_label} installation: "
            f"{existing['installation']}"
        )

    installation = Path(installation)
    installation.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=f".{model.model_id}-", dir=installation.parent))
    source = None if source is None else Path(source).expanduser().resolve()
    try:
        model_directory = staging / "model"
        model_directory.mkdir()
        for filename in model.files:
            candidate = (
                source / filename if source is not None else Path(fetch_file(filename))
            )
            if not candidate.is_file():
                raise error_type(
                    f"Pinned {model_label} source file is missing: {candidate}"
                )
            shutil.copyfile(candidate, model_directory / filename)
        reason, actual_tree, _actual_files = _verify(model_directory, model)
        if reason == "model tree checksum changed":
            reason = (
                f"checksum mismatch: expected {model.tree_sha256}, got {actual_tree}"
            )
        if reason:
            raise error_type(f"Pinned {model_label} {reason}")
        atomic_write_json(staging / "managed-model.json", metadata, sort_keys=True)
        atomic_write_text(staging / "THIRD_PARTY_NOTICES.txt", notice)
        try:
            rename_directory_no_replace(staging, installation)
        except AtomicPublicationError as error:
            if (
                not installation.exists()
                or managed_model_status(installation, model, **status_args)["status"]
                != "installed"
            ):
                raise error_type(str(error)) from error
        return managed_model_status(installation, model, **status_args)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


__all__ = [
    "ManagedModelFiles",
    "install_managed_model",
    "managed_model_status",
    "model_installation",
]
