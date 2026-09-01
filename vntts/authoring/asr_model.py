"""Pinned, authoring-only Whisper model installation and integrity checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.managed_model_installation import (
    ManagedModelFiles,
    install_managed_model,
    managed_model_status,
    model_installation,
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
    return model_installation(managed_asr_root(root), _files(model))


def _files(model):
    return ManagedModelFiles(
        model.model_id,
        model.repository,
        model.revision,
        model.files,
        tree_sha256=model.tree_sha256,
    )


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


def managed_asr_status(model=WHISPER_TINY_EN, *, root=None):
    """Return a deterministic, read-only status document."""
    installation = managed_asr_installation(model, root=root)
    metadata = _metadata(model)
    status = managed_model_status(
        installation,
        _files(model),
        metadata=metadata,
        notice=_notice(model),
        error_type=ManagedAsrModelError,
    )
    status["licenses"] = metadata["licenses"]
    return status


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
    installation = managed_asr_installation(model, root=root)
    fetch = _download_file if fetch_file is None else fetch_file
    result = install_managed_model(
        installation,
        _files(model),
        metadata=_metadata(model),
        notice=_notice(model),
        source=source,
        fetch_file=lambda filename: fetch(model, filename),
        error_type=ManagedAsrModelError,
        model_label="ASR model",
    )
    result["licenses"] = _metadata(model)["licenses"]
    return result


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
