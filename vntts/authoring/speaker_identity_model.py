"""Pinned, diagnostic-only speaker-embedding model installation."""

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

MODEL_ID = "speechbrain-ecapa-voxceleb"
REPOSITORY = "speechbrain/spkrec-ecapa-voxceleb"
REVISION = "0f99f2d0ebe89ac095bcc5903c4dd8f72b367286"
IMPLEMENTATION = "speechbrain"
IMPLEMENTATION_VERSION = "1.0.3"
IMPLEMENTATION_REVISION = "31c1e329048c0380dc7f2acbe680c44a036b6286"
MODEL_LICENSE = "Apache-2.0"
MODEL_LICENSE_URL = f"https://huggingface.co/{REPOSITORY}"
IMPLEMENTATION_LICENSE = "Apache-2.0"
IMPLEMENTATION_LICENSE_URL = (
    "https://github.com/speechbrain/speechbrain/blob/v1.0.3/LICENSE"
)
MODEL_FILES = {
    "classifier.ckpt": "fd9e3634fe68bd0a427c95e354c0c677374f62b3f434e45b78599950d860d535",
    "embedding_model.ckpt": "0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2",
    "hyperparams.yaml": "6f78854fa04ba59e761437b76a2575d3aba5e5016de3e9b69f0c9a5077fb1a41",
    "label_encoder.txt": "e13c3a167bb4112685670ee896d20e2b565af16b3a4ceeaa8689fa4d22adb8b9",
    "mean_var_norm_emb.ckpt": "cd70225b05b37be64fc5a95e24395d804231d43f74b2e1e5a513db7b69b34c33",
}
MANAGED_MODEL_SCHEMA = "vntts.managed-speaker-embedding-model"
MANAGED_MODEL_VERSION = 1


class SpeakerIdentityModelError(RuntimeError):
    """The pinned speaker-embedding model is missing, corrupt, or unavailable."""


@dataclass(frozen=True)
class ManagedSpeakerIdentityModel:
    model_id: str = MODEL_ID
    repository: str = REPOSITORY
    revision: str = REVISION
    implementation: str = IMPLEMENTATION
    implementation_version: str = IMPLEMENTATION_VERSION
    implementation_revision: str = IMPLEMENTATION_REVISION


def _files():
    return ManagedModelFiles(
        MODEL_ID,
        REPOSITORY,
        REVISION,
        tuple(MODEL_FILES),
        file_sha256s=MODEL_FILES,
    )


def managed_speaker_identity_root(root=None):
    if root is not None:
        return Path(root).expanduser().resolve()
    return (
        get_local_data_directory() / "authoring" / "models" / "speaker-identity"
    ).resolve()


def managed_speaker_identity_installation(*, root=None):
    return model_installation(managed_speaker_identity_root(root), _files())


def _metadata():
    body = {
        "schema": MANAGED_MODEL_SCHEMA,
        "schema_version": MANAGED_MODEL_VERSION,
        "model_id": MODEL_ID,
        "repository": REPOSITORY,
        "revision": REVISION,
        "files": MODEL_FILES,
        "implementation": {
            "package": IMPLEMENTATION,
            "version": IMPLEMENTATION_VERSION,
            "revision": IMPLEMENTATION_REVISION,
        },
        "licenses": [
            {
                "scope": "model-snapshot",
                "spdx": MODEL_LICENSE,
                "url": MODEL_LICENSE_URL,
            },
            {
                "scope": "inference-implementation",
                "spdx": IMPLEMENTATION_LICENSE,
                "url": IMPLEMENTATION_LICENSE_URL,
            },
        ],
        "runtime": {
            "device": "cpu",
            "optional_dependency": "speaker-identity",
            "sample_rate_hz": 16000,
        },
    }
    return {**body, "installation_id": canonical_document_sha256(body)}


def _notice():
    return (
        f"{REPOSITORY} at revision {REVISION}\n"
        f"Model snapshot license: {MODEL_LICENSE} ({MODEL_LICENSE_URL})\n"
        f"SpeechBrain {IMPLEMENTATION_VERSION} license: {IMPLEMENTATION_LICENSE} "
        f"({IMPLEMENTATION_LICENSE_URL})\n"
        "This model is optional, local and diagnostic-only. It is not included in "
        "game packs and cannot change voice assignments or review authority.\n"
    )


def managed_speaker_identity_status(*, root=None):
    installation = managed_speaker_identity_installation(root=root)
    metadata = _metadata()
    status = managed_model_status(
        installation,
        _files(),
        metadata=metadata,
        notice=_notice(),
        error_type=SpeakerIdentityModelError,
    )
    status["licenses"] = metadata["licenses"]
    status["runtime"] = metadata["runtime"]
    return status


def resolve_managed_speaker_identity_model(*, root=None):
    status = managed_speaker_identity_status(root=root)
    if status["status"] != "installed":
        detail = f": {status['reason']}" if status["reason"] else ""
        raise SpeakerIdentityModelError(
            f"Managed speaker-identity model is {status['status']}{detail}. "
            "Run 'vntts-pregenerate speaker-identity-model-install'."
        )
    return Path(status["model_directory"])


def _download_file(filename):
    try:
        from huggingface_hub import hf_hub_download

        return Path(
            hf_hub_download(
                repo_id=REPOSITORY,
                filename=filename,
                revision=REVISION,
            )
        )
    except Exception as error:
        raise SpeakerIdentityModelError(
            f"Unable to download pinned speaker model file {filename!r}: {error}"
        ) from error


def install_managed_speaker_identity_model(*, root=None, source=None, fetch_file=None):
    installation = managed_speaker_identity_installation(root=root)
    fetch = _download_file if fetch_file is None else fetch_file
    result = install_managed_model(
        installation,
        _files(),
        metadata=_metadata(),
        notice=_notice(),
        source=source,
        fetch_file=fetch,
        error_type=SpeakerIdentityModelError,
        model_label="speaker-model",
    )
    metadata = _metadata()
    result["licenses"] = metadata["licenses"]
    result["runtime"] = metadata["runtime"]
    return result


__all__ = [
    "MODEL_FILES",
    "ManagedSpeakerIdentityModel",
    "SpeakerIdentityModelError",
    "install_managed_speaker_identity_model",
    "managed_speaker_identity_status",
    "resolve_managed_speaker_identity_model",
]
