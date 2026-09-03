"""Immutable evidence for a selected WAV rejected by the speech-silence gate."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import wave
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.document_identity import canonical_document_sha256, is_lowercase_sha256

SILENCE_FAILURE_EVIDENCE_SCHEMA = "vntts.authoring-silence-failure-evidence"
SILENCE_FAILURE_EVIDENCE_VERSION = 1


class SilenceFailureEvidenceError(RuntimeError):
    """A rejected-WAV evidence artifact cannot be published or trusted."""


def publish_silence_failure_evidence(output_directory, wav_payload, metadata):
    """Atomically publish one non-reviewable rejected WAV and its exact authority."""
    output = _new_directory(output_directory)
    if output.exists() or output.is_symlink():
        raise SilenceFailureEvidenceError(
            f"Silence-failure evidence destination already exists: {output}"
        )
    if not isinstance(wav_payload, bytes) or not wav_payload:
        raise SilenceFailureEvidenceError("Silence-failure WAV payload is invalid")
    _probe_pcm16_mono_bytes(wav_payload)
    if not isinstance(metadata, dict):
        raise SilenceFailureEvidenceError("Silence-failure metadata is invalid")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    try:
        wav = staging / "rejected.wav"
        wav.write_bytes(wav_payload)
        wav_sha256 = hashlib.sha256(wav_payload).hexdigest()
        if sha256_file(wav) != wav_sha256:
            raise SilenceFailureEvidenceError(
                "Copied silence-failure WAV checksum changed"
            )
        document = {
            "schema": SILENCE_FAILURE_EVIDENCE_SCHEMA,
            "schema_version": SILENCE_FAILURE_EVIDENCE_VERSION,
            "reviewable": False,
            "generated_outcome": False,
            "audio": "rejected.wav",
            "audio_sha256": wav_sha256,
            "metadata": metadata,
        }
        atomic_write_json(staging / "evidence.json", document, sort_keys=True)
        load_silence_failure_evidence(staging)
        _rename_no_replace(staging, output)
        return output
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def load_silence_failure_evidence(directory):
    """Validate one rejected-WAV evidence directory without making it reviewable."""
    root = Path(directory).expanduser().resolve()
    try:
        document = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SilenceFailureEvidenceError(
            f"Unable to read silence-failure evidence: {error}"
        ) from error
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema",
            "schema_version",
            "reviewable",
            "generated_outcome",
            "audio",
            "audio_sha256",
            "metadata",
        }
        or document.get("schema") != SILENCE_FAILURE_EVIDENCE_SCHEMA
        or document.get("schema_version") != SILENCE_FAILURE_EVIDENCE_VERSION
        or document.get("reviewable") is not False
        or document.get("generated_outcome") is not False
        or document.get("audio") != "rejected.wav"
        or not isinstance(document.get("metadata"), dict)
    ):
        raise SilenceFailureEvidenceError("Silence-failure evidence is malformed")
    metadata = document["metadata"]
    required_metadata = {
        "queue",
        "queue_sha256",
        "state",
        "state_sha256",
        "queue_id",
        "line_id",
        "text",
        "text_sha256",
        "state_item",
        "state_item_sha256",
        "synthesis_controls_sha256",
    }
    if set(metadata) != required_metadata:
        raise SilenceFailureEvidenceError(
            "Silence-failure evidence metadata is malformed"
        )
    for field in ("queue", "state", "queue_id", "line_id", "text"):
        if not isinstance(metadata[field], str) or not metadata[field]:
            raise SilenceFailureEvidenceError(
                "Silence-failure evidence identity is malformed"
            )
    if (
        not is_lowercase_sha256(metadata["queue_sha256"])
        or not is_lowercase_sha256(metadata["state_sha256"])
        or not is_lowercase_sha256(metadata["text_sha256"])
        or not is_lowercase_sha256(metadata["state_item_sha256"])
        or not is_lowercase_sha256(metadata["synthesis_controls_sha256"])
        or hashlib.sha256(metadata["text"].encode("utf-8")).hexdigest()
        != metadata["text_sha256"]
    ):
        raise SilenceFailureEvidenceError(
            "Silence-failure evidence hashes are malformed"
        )
    state_item = metadata["state_item"]
    if (
        not isinstance(state_item, dict)
        or state_item.get("status") != "failed"
        or (
            state_item.get("text_sha256") is not None
            and state_item.get("text_sha256") != metadata["text_sha256"]
        )
        or not isinstance(state_item.get("failure"), dict)
        or state_item["failure"].get("kind") != "speech_silence"
        or canonical_document_sha256(state_item) != metadata["state_item_sha256"]
    ):
        raise SilenceFailureEvidenceError(
            "Silence-failure evidence state item is malformed"
        )
    wav = root / "rejected.wav"
    if wav.is_symlink() or not wav.is_file():
        raise SilenceFailureEvidenceError("Silence-failure evidence WAV is missing")
    try:
        payload = wav.read_bytes()
        _probe_pcm16_mono_bytes(payload)
    except OSError as error:
        raise SilenceFailureEvidenceError(
            f"Unable to read silence-failure evidence WAV: {error}"
        ) from error
    if hashlib.sha256(payload).hexdigest() != document.get("audio_sha256"):
        raise SilenceFailureEvidenceError(
            "Silence-failure evidence WAV checksum changed"
        )
    return document


def _probe_pcm16_mono_bytes(payload):
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            if (
                source.getcomptype() != "NONE"
                or source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() < 1
                or source.getnframes() < 1
            ):
                raise ValueError("expected non-empty mono 16-bit PCM WAV")
            count = source.getnframes()
            if len(source.readframes(count)) != count * 2:
                raise ValueError("WAV sample data is incomplete")
    except (EOFError, ValueError, wave.Error) as error:
        raise SilenceFailureEvidenceError(
            f"Silence-failure WAV is invalid: {error}"
        ) from error


def _new_directory(value):
    path = Path(value).expanduser()
    if not path.name or path.name in {".", ".."}:
        raise SilenceFailureEvidenceError(
            "Silence-failure evidence requires a directory name"
        )
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.parent.resolve() / path.name


def _rename_no_replace(source, destination):
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        function = ctypes.CDLL(None, use_errno=True).renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(source_bytes, destination_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        function = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if function is None:
            raise SilenceFailureEvidenceError(
                "Atomic no-replace evidence publication is unavailable"
            )
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(-100, source_bytes, -100, destination_bytes, 1)
    elif os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError as error:
            raise SilenceFailureEvidenceError(
                f"Silence-failure evidence destination already exists: {destination}"
            ) from error
        return
    else:
        raise SilenceFailureEvidenceError(
            "Atomic no-replace evidence publication is unavailable"
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise SilenceFailureEvidenceError(
            f"Silence-failure evidence destination already exists: {destination}"
        )
    raise SilenceFailureEvidenceError(
        f"Unable to publish silence-failure evidence: {os.strerror(error_number)}"
    )
