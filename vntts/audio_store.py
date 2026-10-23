"""Durable immutable WAV objects and atomic same-filesystem references."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import wave
from pathlib import Path
from uuid import uuid4

from durable_file import atomic_write_json

from vntts.path_safety import (
    open_regular_binary,
    open_regular_candidate,
    read_regular_text,
)


class SharedAudioStoreError(OSError):
    """A recording cannot be admitted or linked without risking saved work."""


def _regular(path: Path) -> Path:
    path = path.expanduser().parent.resolve() / path.name
    if path.is_symlink() or not path.is_file():
        raise SharedAudioStoreError(f"Audio must be a regular file: {path}")
    return path


def _digest(path: Path) -> str:
    with open_regular_binary(_regular(path)) as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _verify_wav(path: Path) -> None:
    with open_regular_binary(_regular(path)) as source:
        try:
            with wave.open(source, "rb") as audio:
                channels = audio.getnchannels()
                frames = audio.getnframes()
                if (
                    audio.getcomptype() != "NONE"
                    or audio.getsampwidth() != 2
                    or channels not in (1, 2)
                    or audio.getframerate() < 1
                    or frames < 1
                    or frames * channels * 2
                    > os.fstat(source.fileno()).st_size - source.tell()
                    or len(audio.readframes(frames)) != frames * channels * 2
                ):
                    raise SharedAudioStoreError(f"Invalid PCM16 WAV: {path}")
        except (wave.Error, EOFError) as error:
            raise SharedAudioStoreError(f"Invalid PCM16 WAV: {path}") from error


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def link_shared_audio(
    source: str | Path,
    destination: str | Path,
    expected_sha256: str | None = None,
) -> None:
    """Atomically link verified bytes; never mutate an existing recording inode."""
    source = _regular(Path(source))
    digest = _digest(source)
    if expected_sha256 is not None and digest != expected_sha256:
        raise SharedAudioStoreError(f"Audio checksum changed: {source}")
    destination = Path(destination).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = destination.parent.resolve() / destination.name
    if destination.is_symlink():
        raise SharedAudioStoreError(
            f"Audio destination is a symbolic link: {destination}"
        )
    if destination.exists():
        if source.samefile(destination):
            return
        if _digest(destination) != digest:
            raise SharedAudioStoreError(
                f"Audio destination has different content: {destination}"
            )
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.link")
    try:
        os.link(source, temporary)
        if _digest(temporary) != digest:
            raise SharedAudioStoreError(f"Audio changed while linking: {source}")
        os.replace(temporary, destination)
        _sync_directory(destination.parent)
    except OSError as error:
        raise SharedAudioStoreError(
            f"Cannot hard-link saved audio from {source} to {destination}; "
            "the shared store and local output must support hard links on the same filesystem"
        ) from error
    finally:
        temporary.unlink(missing_ok=True)


class SharedAudioStore:
    """One content-addressed WAV per waveform, with non-evicting synthesis bindings."""

    def __init__(self, directory: str | Path) -> None:
        requested = Path(directory).expanduser()
        if requested.is_symlink():
            raise SharedAudioStoreError(f"Audio store is a symbolic link: {requested}")
        self.directory = requested.parent.resolve() / requested.name

    def _directory(self, name: str) -> Path:
        path = self.directory / name
        if self.directory.is_symlink() or path.is_symlink():
            raise SharedAudioStoreError(f"Audio store is a symbolic link: {path}")
        return path

    def _binding(self, key: str) -> Path:
        if (
            not isinstance(key, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,200}", key) is None
        ):
            raise ValueError("Invalid audio synthesis key")
        return self._directory("bindings") / f"{key}.json"

    def get(self, key: str) -> Path | None:
        binding = self._binding(key)
        if not binding.exists() and not binding.is_symlink():
            return None
        if binding.is_symlink():
            raise SharedAudioStoreError(f"Audio binding is a symbolic link: {binding}")
        try:
            record = json.loads(read_regular_text(binding, encoding="utf-8"))
            digest = record["audio_sha256"]
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            ):
                raise ValueError("Invalid audio object digest")
        except (ValueError, TypeError, KeyError) as error:
            raise SharedAudioStoreError(f"Invalid audio binding: {binding}") from error
        path = self._directory("objects") / f"{digest}.wav"
        if _digest(path) != digest:
            raise SharedAudioStoreError(f"Saved audio checksum changed: {path}")
        _verify_wav(path)
        return path

    def put(self, key: str, source: str | Path) -> Path:
        binding = self._binding(key)
        source = _regular(Path(source))
        _verify_wav(source)
        digest = _digest(source)
        objects = self._directory("objects")
        objects.mkdir(parents=True, exist_ok=True)
        destination = objects / f"{digest}.wav"
        if destination.is_symlink():
            raise SharedAudioStoreError(
                f"Audio object is a symbolic link: {destination}"
            )
        if not destination.exists():
            try:
                os.link(source, destination)
            except FileExistsError:
                pass
            except OSError as error:
                raise SharedAudioStoreError(
                    "The shared WAV store and audio source must support hard links on the same filesystem"
                ) from error
        if _digest(destination) != digest:
            raise SharedAudioStoreError(f"Saved audio checksum changed: {destination}")
        _verify_wav(destination)
        # Windows FlushFileBuffers requires a handle with write access.
        with open(
            destination,
            "r+b" if os.name == "nt" else "rb",
            opener=open_regular_candidate,
        ) as audio:
            if not stat.S_ISREG(os.fstat(audio.fileno()).st_mode):
                raise SharedAudioStoreError(
                    f"Audio must be a regular file: {destination}"
                )
            os.fsync(audio.fileno())
        _sync_directory(objects)
        binding.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(binding, {"schema_version": 1, "audio_sha256": digest})
        return destination
