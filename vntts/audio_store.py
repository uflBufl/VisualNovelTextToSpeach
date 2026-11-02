"""Durable immutable WAV objects and atomic same-filesystem references."""

from __future__ import annotations

import hashlib
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
)


class SharedAudioStoreError(OSError):
    """A recording cannot be admitted or linked without risking saved work."""


class AudioCatalogError(SharedAudioStoreError):
    """Saved key authority is unavailable; generating again would lose reuse."""


def _regular(path: Path) -> Path:
    path = path.expanduser().parent.resolve() / path.name
    if path.is_symlink() or not path.is_file():
        raise SharedAudioStoreError(f"Audio must be a regular file: {path}")
    return path


def _digest(path: Path) -> str:
    with open_regular_binary(_regular(path)) as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _read_wav(path: Path, expected_sha256: str | None = None) -> tuple[bytes, str]:
    with open_regular_binary(_regular(path)) as source:
        payload = source.read()
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
    """Atomically link saved audio without overwriting different recording bytes."""
    source = _regular(Path(source))
    destination = Path(destination).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = destination.parent.resolve() / destination.name
    if destination.is_symlink():
        raise SharedAudioStoreError(
            f"Audio destination is a symbolic link: {destination}"
        )
    destination_exists = destination.exists()
    if destination_exists:
        _regular(destination)
        if source.samefile(destination):
            return
        digest = expected_sha256 if expected_sha256 is not None else _digest(source)
        if _digest(destination) != digest:
            raise SharedAudioStoreError(
                f"Audio destination has different content: {destination}"
            )
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.link")
    try:
        os.link(source, temporary)
        if destination_exists:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
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
    "unavailable_keys": "CREATE TABLE unavailable_keys (synthesis_key TEXT PRIMARY KEY NOT NULL, reason TEXT NOT NULL)",
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

    def _object_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise AudioCatalogError("Invalid audio object digest")
        try:
            return _regular(self._directory("objects") / f"{digest}.wav")
        except OSError as error:
            raise AudioCatalogError(
                f"Saved audio object {digest} is unavailable; restore its verified WAV before generating: {error}"
            ) from error

    def _check_catalog_paths(self, path: Path) -> None:
        if self.directory.is_symlink():
            raise AudioCatalogError(f"Audio store is a symbolic link: {self.directory}")
        for candidate in (
            path,
            *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm")),
        ):
            try:
                info = candidate.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise AudioCatalogError(
                    f"Audio catalog must be a regular file: {candidate}"
                )

    def _initialize_catalog(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self._require_empty_store()
        except AudioCatalogError:
            if not self.catalog_path.exists():
                raise
            # A competing fresh-store initializer published first; validate it below.
            return
        temporary = self._directory(f".audio-catalog.{uuid4().hex}.sqlite3")
        try:
            with self._connect(temporary, create=True) as connection:
                connection.execute("BEGIN IMMEDIATE")
                for statement in _CATALOG_SCHEMA.values():
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 2")
                connection.commit()
                self._validate_catalog(connection)
            _sync_regular_file(temporary)
            try:
                os.link(temporary, self.catalog_path)
            except FileExistsError:
                pass
            _sync_directory(self.directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _require_empty_store(self) -> None:
        if self.directory.exists() and any(
            path.name != ".reference-normalization.guard"
            and not path.name.startswith(".audio-catalog.")
            for path in self.directory.iterdir()
        ):
            raise AudioCatalogError(
                f"Saved audio catalog is missing: {self.catalog_path}. "
                "Clear saved preparation data to generate it again."
            )

    def initialize(self) -> None:
        """Create a fresh key catalog before publishing temporary/object files."""
        with self._catalog(create=True):
            pass

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
            raise AudioCatalogError(
                f"Invalid or unavailable audio catalog: {path}: {error}"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _validate_catalog(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()
        if version != (2,):
            raise AudioCatalogError("Unsupported audio catalog schema version")
        actual = dict(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        )
        if actual != _CATALOG_SCHEMA:
            raise AudioCatalogError("Invalid audio catalog schema")

    @contextmanager
    def _catalog(self, *, create: bool = False) -> Iterator[sqlite3.Connection | None]:
        path = self.catalog_path
        self._check_catalog_paths(path)
        if not path.exists():
            if not create:
                self._require_empty_store()
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
                        raise AudioCatalogError("Invalid audio object digest")
                    yield key, digest

    def has_saved_entries(self) -> bool:
        """Detect durable recordings and keys whose original work is unavailable."""
        with self._catalog() as connection:
            if (
                connection is not None
                and connection.execute(
                    "SELECT 1 FROM bindings UNION ALL SELECT 1 FROM unavailable_keys LIMIT 1"
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
                "SELECT reason FROM unavailable_keys WHERE synthesis_key = ?", (key,)
            ).fetchone()
            row = connection.execute(
                "SELECT audio_sha256 FROM bindings WHERE synthesis_key = ?", (key,)
            ).fetchone()
            if row is None:
                if error is not None:
                    raise AudioCatalogError(
                        f"Saved audio for {key} is unavailable: {error[0]}"
                    )
                return None
        digest = row[0]
        if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise AudioCatalogError("Invalid audio object digest")
        return digest

    def get(self, key: str) -> Path | None:
        digest = self._binding_digest(key)
        return self._object_path(digest) if digest is not None else None

    def get_bytes(self, key: str) -> bytes | None:
        """Read saved bytes once; decoding belongs to the consumer."""
        digest = self._binding_digest(key)
        if digest is None:
            return None
        path = self._object_path(digest)
        try:
            with open_regular_binary(path) as source:
                return source.read()
        except OSError as error:
            raise AudioCatalogError(
                f"Saved audio object {digest} cannot be read: {error}"
            ) from error

    def put(
        self,
        key: str,
        source: str | Path,
        *,
        expected_sha256: str | None = None,
    ) -> Path:
        self._validate_key(key)
        self.initialize()
        source = _regular(Path(source))
        objects = self._directory("objects")
        digest = None
        if source.parent == objects and source.suffix == ".wav":
            with self._catalog() as connection:
                assert connection is not None
                if connection.execute(
                    "SELECT 1 FROM bindings WHERE audio_sha256 = ? LIMIT 1",
                    (source.stem,),
                ).fetchone():
                    digest = source.stem
            if digest is not None and expected_sha256 not in (None, digest):
                raise SharedAudioStoreError(f"Saved audio checksum differs: {source}")
        if digest is None:
            digest = _read_wav(source, expected_sha256)[1]
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
        _regular(destination)
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
                "DELETE FROM unavailable_keys WHERE synthesis_key = ?", (key,)
            )
            connection.commit()
        return destination
