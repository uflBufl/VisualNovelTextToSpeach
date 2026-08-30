"""Pinned, authoring-only Whisper model installation and integrity checks."""

from __future__ import annotations

import copy
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

from vntts_artifacts.atomic_io import atomic_write_json, atomic_write_text

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import BulkGenerationError, sha256_control_path
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
)
from vntts.settings import get_local_data_directory

MANAGED_ASR_SCHEMA = "vntts.managed-authoring-asr-model"
MANAGED_ASR_VERSION = 1


class ManagedAsrModelError(RuntimeError):
    """The pinned authoring ASR model is missing, corrupt, or unavailable."""


@dataclass(frozen=True)
class ManagedAsrModel:
    """One immutable authoring model descriptor."""

    model_id: str
    repository: str
    revision: str
    tree_sha256: str
    files: tuple[str, ...]
    snapshot_license: str
    snapshot_license_url: str
    upstream_license: str
    upstream_license_url: str


WHISPER_TINY_EN = ManagedAsrModel(
    model_id="openai-whisper-tiny.en",
    repository="openai/whisper-tiny.en",
    revision="87c7102498dcde7456f24cfd30239ca606ed9063",
    tree_sha256="d69d7c69a342b4cf4274fe974559249fdb240d14813cd7d03cb9094955a7240b",
    files=(
        "added_tokens.json",
        "config.json",
        "generation_config.json",
        "merges.txt",
        "model.safetensors",
        "normalizer.json",
        "preprocessor_config.json",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.json",
    ),
    snapshot_license="Apache-2.0",
    snapshot_license_url="https://huggingface.co/openai/whisper-tiny.en",
    upstream_license="MIT",
    upstream_license_url="https://github.com/openai/whisper/blob/main/LICENSE",
)


def managed_asr_root(root=None):
    """Return the device-local authoring model root."""
    if root is not None:
        return Path(root).expanduser().resolve()
    return (get_local_data_directory() / "authoring" / "models" / "asr").resolve()


def managed_asr_installation(model=WHISPER_TINY_EN, *, root=None):
    """Return the immutable installation directory for ``model``."""
    return managed_asr_root(root) / model.model_id / model.revision


def _metadata(model):
    body = {
        "schema": MANAGED_ASR_SCHEMA,
        "schema_version": MANAGED_ASR_VERSION,
        "model_id": model.model_id,
        "repository": model.repository,
        "revision": model.revision,
        "tree_sha256": model.tree_sha256,
        "files": list(model.files),
        "licenses": [
            {
                "scope": "hugging-face-snapshot",
                "spdx": model.snapshot_license,
                "url": model.snapshot_license_url,
            },
            {
                "scope": "upstream-openai-whisper",
                "spdx": model.upstream_license,
                "url": model.upstream_license_url,
            },
        ],
    }
    return {**body, "installation_id": canonical_document_sha256(body)}


def _notice(model):
    return (
        f"{model.repository} at revision {model.revision}\n"
        f"Snapshot license: {model.snapshot_license} ({model.snapshot_license_url})\n"
        f"Upstream OpenAI Whisper license: {model.upstream_license} "
        f"({model.upstream_license_url})\n"
        "This model is an optional offline authoring dependency. Published game "
        "packs and live playback do not download or load it.\n"
    )


def _tree_sha256(path):
    try:
        return sha256_control_path(path)
    except BulkGenerationError as error:
        raise ManagedAsrModelError(str(error)) from error


def managed_asr_status(model=WHISPER_TINY_EN, *, root=None):
    """Return a deterministic, read-only status document."""
    installation = managed_asr_installation(model, root=root)
    model_directory = installation / "model"
    metadata_path = installation / "managed-model.json"
    notice_path = installation / "THIRD_PARTY_NOTICES.txt"
    status = "missing"
    actual_sha256 = None
    reason = None
    if installation.exists():
        if not installation.is_dir() or not model_directory.is_dir():
            status = "invalid"
            reason = "installation shape is invalid"
        else:
            actual_sha256 = _tree_sha256(model_directory)
            if actual_sha256 != model.tree_sha256:
                status = "invalid"
                reason = "model tree checksum changed"
            else:
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    notice = notice_path.read_text(encoding="utf-8")
                except (OSError, ValueError) as error:
                    status = "invalid"
                    reason = f"installation metadata is unavailable: {error}"
                else:
                    if metadata != _metadata(model) or notice != _notice(model):
                        status = "invalid"
                        reason = "installation metadata changed"
                    else:
                        status = "installed"
    return {
        "model_id": model.model_id,
        "repository": model.repository,
        "revision": model.revision,
        "installation": str(installation),
        "model_directory": str(model_directory),
        "status": status,
        "expected_tree_sha256": model.tree_sha256,
        "actual_tree_sha256": actual_sha256,
        "reason": reason,
        "licenses": copy.deepcopy(_metadata(model)["licenses"]),
    }


def resolve_managed_asr_model(model=WHISPER_TINY_EN, *, root=None):
    """Resolve an already installed model or fail with actionable guidance."""
    status = managed_asr_status(model, root=root)
    if status["status"] != "installed":
        detail = f": {status['reason']}" if status["reason"] else ""
        raise ManagedAsrModelError(
            f"Managed ASR model {model.model_id} is {status['status']}{detail}. "
            "Run 'vntts-pregenerate asr-model-install'."
        )
    return Path(status["model_directory"])


def _download_file(model, filename):
    try:
        from huggingface_hub import hf_hub_download

        return Path(
            hf_hub_download(
                repo_id=model.repository,
                filename=filename,
                revision=model.revision,
            )
        )
    except Exception as error:
        raise ManagedAsrModelError(
            f"Unable to download pinned ASR file {filename!r}: {error}"
        ) from error


def install_managed_asr_model(
    model=WHISPER_TINY_EN,
    *,
    root=None,
    source=None,
    fetch_file=None,
):
    """Atomically import or download and verify one pinned model snapshot."""
    existing = managed_asr_status(model, root=root)
    if existing["status"] == "installed":
        return existing
    if existing["status"] == "invalid":
        raise ManagedAsrModelError(
            "Refusing to overwrite an invalid managed ASR installation: "
            f"{existing['installation']}"
        )

    installation = managed_asr_installation(model, root=root)
    installation.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=f".{model.model_id}-", dir=installation.parent))
    source_directory = None if source is None else Path(source).expanduser().resolve()
    fetch = _download_file if fetch_file is None else fetch_file
    try:
        model_directory = staging / "model"
        model_directory.mkdir()
        for filename in model.files:
            candidate = (
                source_directory / filename
                if source_directory is not None
                else Path(fetch(model, filename))
            )
            if not candidate.is_file():
                raise ManagedAsrModelError(
                    f"Pinned ASR source file is missing: {candidate}"
                )
            shutil.copyfile(candidate, model_directory / filename)
        actual_sha256 = _tree_sha256(model_directory)
        if actual_sha256 != model.tree_sha256:
            raise ManagedAsrModelError(
                "Pinned ASR model checksum mismatch: "
                f"expected {model.tree_sha256}, got {actual_sha256}"
            )
        atomic_write_json(
            staging / "managed-model.json", _metadata(model), sort_keys=True
        )
        atomic_write_text(staging / "THIRD_PARTY_NOTICES.txt", _notice(model))
        try:
            rename_directory_no_replace(staging, installation)
        except AtomicPublicationError as error:
            if not installation.exists():
                raise ManagedAsrModelError(str(error)) from error
            if managed_asr_status(model, root=root)["status"] != "installed":
                raise ManagedAsrModelError(
                    "Managed ASR installation raced with a different publication"
                ) from None
        return managed_asr_status(model, root=root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


__all__ = [
    "MANAGED_ASR_SCHEMA",
    "MANAGED_ASR_VERSION",
    "WHISPER_TINY_EN",
    "ManagedAsrModel",
    "ManagedAsrModelError",
    "install_managed_asr_model",
    "managed_asr_installation",
    "managed_asr_root",
    "managed_asr_status",
    "resolve_managed_asr_model",
]
