"""One disposable diagnostic WAV per failed queue item, never published audio."""

from __future__ import annotations

import hashlib
import io
import wave
from pathlib import Path

from durable_file import atomic_write_bytes

from vntts.document_identity import is_lowercase_sha256
from vntts.path_safety import (
    contained_no_replace_destination,
    contained_regular_file,
    read_regular_bytes,
)


def _relative_path(queue_id: str) -> Path:
    if not isinstance(queue_id, str) or not queue_id:
        raise ValueError("Failed audio queue ID is missing")
    digest = hashlib.sha256(queue_id.encode("utf-8")).hexdigest()
    return Path(".failed-audio") / f"{digest}.wav"


def _read_relative(root: Path, relative: Path) -> bytes:
    path = contained_regular_file(root, relative, "Failed audio")
    before = path.stat()
    payload = read_regular_bytes(path)
    after = contained_regular_file(root, relative, "Failed audio").stat()
    if any(
        getattr(before, field) != getattr(after, field)
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    ):
        raise ValueError("Failed audio changed while it was read")
    _validate_wav(payload)
    return payload


def _validate_wav(payload: bytes) -> None:
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            count = source.getnframes()
            if (
                source.getcomptype() != "NONE"
                or source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() < 1
                or count < 1
                or len(source.readframes(count)) != count * 2
            ):
                raise ValueError("expected nonempty mono 16-bit PCM WAV")
    except (EOFError, wave.Error) as error:
        raise ValueError("Failed audio WAV is malformed") from error


def failed_audio_path(output_directory: Path, queue_id: str, metadata: object) -> Path:
    """Check retained metadata and availability without decoding the WAV."""
    expected = _relative_path(queue_id)
    if (
        not isinstance(metadata, dict)
        or metadata.get("created") is not True
        or metadata.get("path") != expected.as_posix()
        or not is_lowercase_sha256(metadata.get("sha256"))
    ):
        raise ValueError("Failed audio is unavailable")
    return contained_regular_file(output_directory, expected, "Failed audio")


def read_failed_audio(output_directory: Path, queue_id: str, metadata: object) -> bytes:
    """Read and validate the exact retained diagnostic before playback."""
    failed_audio_path(output_directory, queue_id, metadata)
    payload = _read_relative(output_directory, _relative_path(queue_id))
    assert isinstance(metadata, dict)
    if hashlib.sha256(payload).hexdigest() != metadata["sha256"]:
        raise ValueError("Failed audio checksum changed")
    return payload


def retain_failed_audio(
    output_directory: Path, queue_id: str, source: Path | bytes
) -> dict[str, object]:
    """Best-effort retention; an unavailable diagnostic never changes the failure."""
    metadata: dict[str, object] = {"created": False}
    try:
        if isinstance(source, bytes):
            metadata["created"] = bool(source)
            payload = source
            _validate_wav(payload)
        else:
            relative = source.relative_to(output_directory)
            metadata["created"] = source.exists() or source.is_symlink()
            payload = _read_relative(output_directory, relative)
        metadata["created"] = True
        relative = _relative_path(queue_id)
        directory = output_directory / relative.parent
        if directory.is_symlink():
            raise ValueError("Failed audio directory is a symbolic link")
        directory.mkdir(exist_ok=True)
        destination = contained_no_replace_destination(
            output_directory, relative, "Failed audio"
        )
        atomic_write_bytes(destination, payload)
        metadata.update(
            path=relative.as_posix(), sha256=hashlib.sha256(payload).hexdigest()
        )
    except Exception:
        # Optional diagnostics must preserve the synthesis failure.
        pass
    return metadata


def remove_failed_audio(output_directory: Path, queue_id: str) -> None:
    """Discard stale diagnostics after their replacement state is committed."""
    try:
        path = contained_regular_file(
            output_directory, _relative_path(queue_id), "Failed audio"
        )
        path.unlink()
    except OSError, ValueError:
        pass
