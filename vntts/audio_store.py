"""Durable immutable WAV objects and atomic same-filesystem references."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from uuid import uuid4

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


def _read_wav(path: Path, expected_sha256: str | None = None) -> tuple[bytes, str]:
    path = _regular(path)
    with open_regular_binary(path) as source:
        before = os.fstat(source.fileno())
        payload = source.read()
        after = os.fstat(source.fileno())
    current = path.stat(follow_symlinks=False)
    fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    if len(payload) != before.st_size or any(
        getattr(before, field) != getattr(info, field)
        for info in (after, current)
        for field in fields
    ):
        raise SharedAudioStoreError(f"Audio changed while reading: {path}")
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise SharedAudioStoreError(f"Saved audio checksum changed: {path}")
    source_bytes = BytesIO(payload)
    try:
        with wave.open(source_bytes, "rb") as audio:
            channels = audio.getnchannels()
            frames = audio.getnframes()
            if (
                audio.getcomptype() != "NONE"
                or audio.getsampwidth() != 2
                or channels not in (1, 2)
                or audio.getframerate() < 1
                or frames < 1
                or frames * channels * 2 > len(payload) - source_bytes.tell()
                or len(audio.readframes(frames)) != frames * channels * 2
            ):
                raise SharedAudioStoreError(f"Invalid PCM16 WAV: {path}")
    except (wave.Error, EOFError) as error:
        raise SharedAudioStoreError(f"Invalid PCM16 WAV: {path}") from error
    return payload, digest


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_regular_file(path: Path) -> None:
    # Windows FlushFileBuffers requires a handle with write access.
    with open(
        path, "r+b" if os.name == "nt" else "rb", opener=open_regular_candidate
    ) as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise SharedAudioStoreError(f"File must be regular: {path}")
        os.fsync(source.fileno())


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


_CATALOG_SCHEMA = {
    "bindings": "CREATE TABLE bindings (synthesis_key TEXT PRIMARY KEY NOT NULL, audio_sha256 TEXT NOT NULL)",
    "legacy_errors": "CREATE TABLE legacy_errors (filename TEXT PRIMARY KEY NOT NULL, error TEXT NOT NULL)",
}


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

    @property
    def catalog_path(self) -> Path:
        return self.directory / "audio-catalog.sqlite3"

    @staticmethod
    def _validate_key(key: str) -> None:
        if (
            not isinstance(key, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,200}", key) is None
        ):
            raise ValueError("Invalid audio synthesis key")

    def _object(self, digest: str) -> Path:
        if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise SharedAudioStoreError("Invalid audio object digest")
        path = self._directory("objects") / f"{digest}.wav"
        _read_wav(path, digest)
        return path

    def _check_catalog_paths(self, path: Path) -> None:
        if self.directory.is_symlink():
            raise SharedAudioStoreError(
                f"Audio store is a symbolic link: {self.directory}"
            )
        for candidate in (
            path,
            *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm")),
        ):
            try:
                info = candidate.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise SharedAudioStoreError(
                    f"Audio catalog must be a regular file: {candidate}"
                )

    def _read_legacy_binding(self, binding: Path) -> tuple[str, str]:
        self._validate_key(binding.stem)
        if binding.suffix != ".json" or binding.is_symlink():
            raise ValueError("Not a regular legacy synthesis binding")
        payload = read_regular_text(binding, encoding="utf-8")
        record = json.loads(payload)
        if (
            not isinstance(record, dict)
            or set(record) != {"schema_version", "audio_sha256"}
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 1
        ):
            raise ValueError("Unsupported legacy audio binding schema")
        digest = record["audio_sha256"]
        self._object(digest)
        return payload, digest

    def _initialize_catalog(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self._directory(f".audio-catalog.{uuid4().hex}.sqlite3")
        imported: list[tuple[Path, str, str]] = []
        try:
            with self._connect(temporary, create=True) as connection:
                connection.execute("BEGIN IMMEDIATE")
                for statement in _CATALOG_SCHEMA.values():
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 1")
                legacy = self._directory("bindings")
                if legacy.exists() and not legacy.is_dir():
                    raise SharedAudioStoreError(
                        f"Invalid legacy audio bindings: {legacy}"
                    )
                for binding in sorted(legacy.iterdir()) if legacy.exists() else ():
                    try:
                        payload, digest = self._read_legacy_binding(binding)
                        connection.execute(
                            "INSERT INTO bindings VALUES (?, ?)", (binding.stem, digest)
                        )
                        imported.append((binding, payload, digest))
                    except (OSError, ValueError, TypeError, KeyError) as error:
                        connection.execute(
                            "INSERT INTO legacy_errors VALUES (?, ?)",
                            (binding.name, str(error)),
                        )
                connection.commit()
                self._validate_catalog(connection)
            _sync_regular_file(temporary)
            try:
                os.link(temporary, self.catalog_path)
            except FileExistsError:
                pass
            _sync_directory(self.directory)
            # Keep every original until the committed, published catalog agrees.
            with self._connect(self.catalog_path) as connection:
                self._validate_catalog(connection)
                for binding, payload, digest in imported:
                    row = connection.execute(
                        "SELECT audio_sha256 FROM bindings WHERE synthesis_key = ?",
                        (binding.stem,),
                    ).fetchone()
                    if row != (digest,):
                        raise SharedAudioStoreError(
                            f"Legacy binding conflicts with audio catalog: {binding}"
                        )
                    self._object(digest)
                    if (
                        binding.exists()
                        and not binding.is_symlink()
                        and read_regular_text(binding, encoding="utf-8") == payload
                    ):
                        binding.unlink(missing_ok=True)
            if imported:
                _sync_directory(self._directory("bindings"))
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def _connect(
        self, path: Path, *, create: bool = False
    ) -> Iterator[sqlite3.Connection]:
        self._check_catalog_paths(path)
        connection = None
        try:
            connection = sqlite3.connect(
                path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
                uri=True,
                timeout=30,
                isolation_level=None,
            )
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA synchronous = FULL")
            yield connection
        except sqlite3.Error as error:
            raise SharedAudioStoreError(
                f"Invalid or unavailable audio catalog: {path}: {error}"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _validate_catalog(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA user_version").fetchone() != (1,):
            raise SharedAudioStoreError("Unsupported audio catalog schema version")
        actual = dict(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        )
        if actual != _CATALOG_SCHEMA:
            raise SharedAudioStoreError("Invalid audio catalog schema")

    @contextmanager
    def _catalog(self, *, create: bool = False) -> Iterator[sqlite3.Connection | None]:
        path = self.catalog_path
        self._check_catalog_paths(path)
        if not path.exists():
            legacy = self._directory("bindings")
            if not create and (
                not legacy.exists()
                or (legacy.is_dir() and next(legacy.iterdir(), None) is None)
            ):
                yield None
                return
            self._initialize_catalog()
        with self._connect(path) as connection:
            self._validate_catalog(connection)
            yield connection

    def iter_bindings(self) -> Iterator[tuple[str, str]]:
        """Iterate catalog keys and checksums without loading waveform bytes."""
        with self._catalog() as connection:
            if connection is not None:
                for key, digest in connection.execute(
                    "SELECT synthesis_key, audio_sha256 FROM bindings ORDER BY synthesis_key"
                ):
                    self._validate_key(key)
                    if (
                        not isinstance(digest, str)
                        or re.fullmatch(r"[a-f0-9]{64}", digest) is None
                    ):
                        raise SharedAudioStoreError("Invalid audio object digest")
                    yield key, digest

    def iter_legacy_issues(self) -> Iterator[tuple[Path, str]]:
        """Report preserved legacy files that could not be safely imported."""
        with self._catalog() as connection:
            if connection is not None:
                for name, error in connection.execute(
                    "SELECT filename, error FROM legacy_errors ORDER BY filename"
                ):
                    yield self._directory("bindings") / name, error

    def has_saved_entries(self) -> bool:
        """Detect saved work, including retained unimportable legacy files."""
        with self._catalog() as connection:
            if (
                connection is not None
                and connection.execute(
                    "SELECT 1 FROM bindings UNION ALL SELECT 1 FROM legacy_errors LIMIT 1"
                ).fetchone()
            ):
                return True
        objects = self._directory("objects")
        return objects.exists() and next(objects.iterdir(), None) is not None

    def _binding_digest(self, key: str) -> str | None:
        self._validate_key(key)
        with self._catalog() as connection:
            if connection is None:
                return None
            error = connection.execute(
                "SELECT error FROM legacy_errors WHERE filename = ?", (f"{key}.json",)
            ).fetchone()
            row = connection.execute(
                "SELECT audio_sha256 FROM bindings WHERE synthesis_key = ?", (key,)
            ).fetchone()
            if row is None:
                if error is not None:
                    raise SharedAudioStoreError(
                        f"Invalid legacy audio binding for {key}: {error[0]}"
                    )
                return None
        digest = row[0]
        if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise SharedAudioStoreError("Invalid audio object digest")
        return digest

    def get(self, key: str) -> Path | None:
        digest = self._binding_digest(key)
        return self._object(digest) if digest is not None else None

    def get_bytes(self, key: str) -> bytes | None:
        """Return the exact verified snapshot without requiring a second file read."""
        digest = self._binding_digest(key)
        if digest is None:
            return None
        payload, _ = _read_wav(self._directory("objects") / f"{digest}.wav", digest)
        return payload

    def put(self, key: str, source: str | Path) -> Path:
        self._validate_key(key)
        source = _regular(Path(source))
        digest = _read_wav(source)[1]
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
        _read_wav(destination, digest)
        _sync_regular_file(destination)
        _sync_directory(objects)
        with self._catalog(create=True) as connection:
            assert connection is not None
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO bindings VALUES (?, ?) ON CONFLICT(synthesis_key) DO UPDATE SET audio_sha256 = excluded.audio_sha256",
                (key, digest),
            )
            connection.execute(
                "DELETE FROM legacy_errors WHERE filename = ?", (f"{key}.json",)
            )
            connection.commit()
        return destination
