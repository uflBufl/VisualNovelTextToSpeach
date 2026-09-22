"""Shared filesystem lifecycle for pinned local authoring models."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, TypeAlias, TypedDict

from vntts_artifacts.atomic_io import atomic_write_json, atomic_write_text
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)

PathInput: TypeAlias = str | Path
JsonDocument: TypeAlias = dict[str, object]
ModelStatus: TypeAlias = Literal["missing", "invalid", "installed"]
VerificationResult: TypeAlias = tuple[str | None, str | None, dict[str, str | None]]


class ManagedModelStatus(TypedDict):
    model_id: str
    repository: str
    revision: str
    installation: str
    model_directory: str
    status: ModelStatus
    reason: str | None
    expected_tree_sha256: NotRequired[str]
    actual_tree_sha256: NotRequired[str | None]
    expected_files: NotRequired[Mapping[str, str]]
    actual_files: NotRequired[dict[str, str | None]]
    licenses: NotRequired[object]
    runtime: NotRequired[object]


@dataclass(frozen=True)
class ManagedModelFiles:
    """Immutable files and integrity controls for one pinned model."""

    model_id: str
    repository: str
    revision: str
    files: tuple[str, ...]
    tree_sha256: str | None = None
    file_sha256s: Mapping[str, str] | None = None


def model_installation(root: PathInput, model: ManagedModelFiles) -> Path:
    return Path(root) / model.model_id / model.revision


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
        if candidate.is_file():
            relative = candidate.relative_to(path).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(bytes.fromhex(sha256_file(candidate)))
    return digest.hexdigest()


def _verify(model_directory: Path, model: ManagedModelFiles) -> VerificationResult:
    actual_files: dict[str, str | None] = {}
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
    installation: PathInput,
    model: ManagedModelFiles,
    *,
    metadata: JsonDocument,
    notice: str,
    error_type: type[RuntimeError] = RuntimeError,
) -> ManagedModelStatus:
    """Return common read-only status for one immutable model installation."""
    installation = Path(installation)
    model_directory = installation / "model"
    status: ModelStatus = "missing"
    reason: str | None = None
    actual_tree: str | None = None
    actual_files: dict[str, str | None] = {}
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
    result: ManagedModelStatus = {
        "model_id": model.model_id,
        "repository": model.repository,
        "revision": model.revision,
        "installation": str(installation),
        "model_directory": str(model_directory),
        "status": status,
        "reason": reason,
    }
    if model.tree_sha256 is not None:
        result["expected_tree_sha256"] = model.tree_sha256
        result["actual_tree_sha256"] = actual_tree
    if model.file_sha256s is not None:
        result["expected_files"] = model.file_sha256s
        result["actual_files"] = actual_files
    return result


def install_managed_model(
    installation: PathInput,
    model: ManagedModelFiles,
    *,
    metadata: JsonDocument,
    notice: str,
    source: PathInput | None = None,
    fetch_file: Callable[[str], PathInput],
    error_type: type[RuntimeError] = RuntimeError,
    model_label: str = "model",
) -> ManagedModelStatus:
    """Copy, verify and atomically publish one pinned local model."""
    existing = managed_model_status(
        installation, model, metadata=metadata, notice=notice, error_type=error_type
    )
    if existing["status"] == "installed":
        return existing
    if existing["status"] == "invalid":
        raise error_type(
            f"Refusing to overwrite an invalid managed {model_label} installation: "
            f"{existing['installation']}"
        )

    installation = Path(installation)
    installation.parent.mkdir(parents=True, exist_ok=True)
    source = None if source is None else Path(source).expanduser().resolve()
    with staged_directory(installation.parent, prefix=f".{model.model_id}-") as staging:
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
                or managed_model_status(
                    installation,
                    model,
                    metadata=metadata,
                    notice=notice,
                    error_type=error_type,
                )["status"]
                != "installed"
            ):
                raise error_type(str(error)) from error
        return managed_model_status(
            installation, model, metadata=metadata, notice=notice, error_type=error_type
        )


__all__ = [
    "ManagedModelFiles",
    "ManagedModelStatus",
    "install_managed_model",
    "managed_model_status",
    "model_installation",
]
