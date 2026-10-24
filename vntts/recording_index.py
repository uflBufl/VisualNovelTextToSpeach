"""Rebuildable, integrity-checked readiness receipts for verified generation inputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from vntts.audio_store import _sync_directory, _sync_regular_file
from vntts.authoring.advisory_lock import exclusive_advisory_lock

Source = tuple[str, str, str]
Recording = tuple[Path, str]
Row = tuple[str, str, int, str | None, str | None]
_APPLICATION_ID = 0x564E5249
_VERSION = 2
# Bucket receipts detect missing rows without reading unrelated queue lines.
_BUCKET_COUNT = 256
_SCHEMA = (
    "CREATE TABLE directories (directory TEXT PRIMARY KEY NOT NULL, "
    "queue_sha256 TEXT NOT NULL, state_sha256 TEXT NOT NULL, input_sha256 TEXT NOT NULL, "
    "receipt_sha256 TEXT NOT NULL) STRICT",
    "CREATE TABLE buckets (directory TEXT NOT NULL REFERENCES directories(directory) "
    "ON DELETE CASCADE, bucket INTEGER NOT NULL CHECK (bucket BETWEEN 0 AND 255), "
    "receipt_sha256 TEXT NOT NULL, PRIMARY KEY(directory, bucket)) STRICT",
    "CREATE TABLE lines (directory TEXT NOT NULL REFERENCES directories(directory) "
    "ON DELETE CASCADE, bucket INTEGER NOT NULL CHECK (bucket BETWEEN 0 AND 255), "
    "line_id TEXT NOT NULL, text_sha256 TEXT NOT NULL, "
    "checked INTEGER NOT NULL CHECK (checked IN (0, 1)), audio_path TEXT, audio_sha256 TEXT, "
    "PRIMARY KEY(directory, bucket, line_id), CHECK ((audio_path IS NULL AND audio_sha256 IS NULL) "
    "OR (checked = 1 AND audio_path IS NOT NULL AND audio_sha256 IS NOT NULL))) STRICT",
)
# Only this exact obsolete derived schema may be discarded, never unknown objects.
_OBSOLETE_SCHEMA = (
    _SCHEMA[0],
    "CREATE TABLE lines (directory TEXT NOT NULL REFERENCES directories(directory) "
    "ON DELETE CASCADE, line_id TEXT NOT NULL, text_sha256 TEXT NOT NULL, "
    "checked INTEGER NOT NULL CHECK (checked IN (0, 1)), audio_path TEXT, audio_sha256 TEXT, "
    "PRIMARY KEY(directory, line_id), CHECK ((audio_path IS NULL AND audio_sha256 IS NULL) "
    "OR (checked = 1 AND audio_path IS NOT NULL AND audio_sha256 IS NOT NULL))) STRICT",
)


class RecordingIndexError(ValueError):
    """The derived index cannot be used without bypassing its trust boundary."""


class _InvalidContents(ValueError):
    pass


class _ObsoleteIndex(RecordingIndexError):
    pass


def _sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise _InvalidContents("Invalid recording index checksum")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise _InvalidContents("Invalid recording index identity")
    return value


def _source(value: Source) -> Source:
    if not isinstance(value, tuple) or len(value) != 3:
        raise _InvalidContents("Invalid recording index source")
    return _sha(value[0]), _sha(value[1]), _sha(value[2])


def _directory(value: Path) -> str:
    value = Path(value).expanduser().absolute()
    return str(value.parent.resolve() / value.name)


def _bucket(line: str) -> int:
    return hashlib.sha256(line.encode()).digest()[0]


def _receipt(
    directory: str, source: Source, rows: list[Row], bucket: int | None = None
) -> str:
    digest = hashlib.sha256()
    for value in ((directory, *source, bucket), *rows):
        digest.update(
            json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_row(value: tuple[object, ...]) -> Row:
    if len(value) != 5:
        raise _InvalidContents("Invalid recording index row")
    line, text, checked, path, checksum = value
    if type(checked) is not int or checked not in (0, 1):
        raise _InvalidContents("Invalid recording index readiness")
    if path is None and checksum is None:
        return _text(line), _sha(text), checked, None, None
    if checked != 1 or not isinstance(path, str) or not Path(_text(path)).is_absolute():
        raise _InvalidContents("Invalid recording index path")
    return _text(line), _sha(text), checked, path, _sha(checksum)


def _validate_schema(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,):
        raise RecordingIndexError("Unsupported recording index application")
    version = connection.execute("PRAGMA user_version").fetchone()
    actual = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    statements = {row[0] for row in actual}
    if version == (1,) and statements == set(_OBSOLETE_SCHEMA):
        raise _ObsoleteIndex("Rebuild the derived recording index")
    if version != (_VERSION,):
        raise RecordingIndexError("Unsupported recording index schema version")
    if statements != set(_SCHEMA):
        raise RecordingIndexError("Unsupported recording index schema")


class RecordingIndex:
    """Persist derived queue membership; consumers must still verify WAV bytes."""

    def __init__(self, root: Path) -> None:
        requested = Path(root).expanduser().absolute()
        self.root = requested.parent.resolve() / requested.name
        self.path = self.root / "recordings.sqlite3"

    def _check_paths(self) -> os.stat_result | None:
        for path in (
            self.root,
            self.root / ".recordings.guard",
            self.path,
            *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm")),
        ):
            try:
                info = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            expected = stat.S_ISDIR if path == self.root else stat.S_ISREG
            if not expected(info.st_mode) or path.is_junction():
                raise RecordingIndexError(
                    "Recording index must not use links or special files"
                )
        try:
            return self.path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None

    @contextmanager
    def _connection(
        self, *, write: bool = False
    ) -> Iterator[sqlite3.Connection | None]:
        before = self._check_paths()
        if before is None:
            yield None
            return
        mode = "rw" if write else "ro"
        with closing(
            sqlite3.connect(self.path.as_uri() + f"?mode={mode}", uri=True, timeout=30)
        ) as connection:
            after = self._check_paths()
            if after is None or (before.st_dev, before.st_ino) != (
                after.st_dev,
                after.st_ino,
            ):
                raise RecordingIndexError("Recording index changed while opening")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            _validate_schema(connection)
            yield connection
            if write:
                connection.commit()

    def _initialize(self, *, replace: os.stat_result | None = None) -> None:
        temporary = None
        try:
            with NamedTemporaryFile(
                dir=self.root, prefix=".recordings-", suffix=".sqlite3", delete=False
            ) as stream:
                temporary = Path(stream.name)
            with closing(sqlite3.connect(temporary)) as connection:
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {_VERSION}")
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                connection.commit()
            _sync_regular_file(temporary)
            current = self._check_paths()
            if replace is not None:
                fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
                if current is None or any(
                    getattr(current, field) != getattr(replace, field)
                    for field in fields
                ):
                    raise RecordingIndexError(
                        "Corrupt recording index changed before replacement"
                    )
                os.replace(temporary, self.path)
            else:
                os.link(temporary, self.path)
            _sync_directory(self.root)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _rows(
        connection: sqlite3.Connection,
        directory: str,
        requested: Mapping[str, str] | None = None,
    ) -> tuple[Source, list[Row]] | None:
        saved = connection.execute(
            "SELECT queue_sha256, state_sha256, input_sha256, receipt_sha256 FROM directories WHERE directory = ?",
            (directory,),
        ).fetchone()
        if saved is None:
            return None
        source = _source(saved[:3])
        if _sha(saved[3]) != _receipt(directory, source, []):
            raise _InvalidContents("Recording index source receipt changed")
        selected = (
            tuple(range(_BUCKET_COUNT))
            if requested is None
            else tuple(sorted({_bucket(line) for line in requested}))
        )
        if not selected:
            return source, []
        placeholders = ",".join("?" for _ in selected)
        parameters = (directory, *selected)
        receipts = dict(
            connection.execute(
                f"SELECT bucket, receipt_sha256 FROM buckets WHERE directory = ? AND bucket IN ({placeholders})",
                parameters,
            )
        )
        if set(receipts) != set(selected):
            raise _InvalidContents("Recording index bucket receipt is missing")
        grouped: dict[int, list[Row]] = {bucket: [] for bucket in selected}
        for bucket, *values in connection.execute(
            f"SELECT bucket, line_id, text_sha256, checked, audio_path, audio_sha256 FROM lines WHERE directory = ? AND bucket IN ({placeholders}) ORDER BY bucket, line_id",
            parameters,
        ):
            row = _validate_row(tuple(values))
            if _bucket(row[0]) != bucket:
                raise _InvalidContents("Recording index line moved between buckets")
            grouped[bucket].append(row)
        for bucket, rows in grouped.items():
            if _sha(receipts[bucket]) != _receipt(directory, source, rows, bucket):
                raise _InvalidContents("Recording index receipt changed")
        return source, [row for rows in grouped.values() for row in rows]

    def load(
        self, directory: Path, source: Source, requested: Mapping[str, str]
    ) -> tuple[dict[str, Recording], frozenset[str]] | None:
        source = _source(source)
        requested = {_text(line): _sha(text) for line, text in requested.items()}
        try:
            with self._connection() as connection:
                if (
                    connection is None
                    or (
                        saved := self._rows(
                            connection, _directory(directory), requested
                        )
                    )
                    is None
                ):
                    return None
                if saved[0] != source:
                    return None
                recordings: dict[str, Recording] = {}
                unchecked = set()
                for line, text, checked, path, checksum in saved[1]:
                    if requested.get(line) != text:
                        continue
                    if not checked:
                        unchecked.add(line)
                    elif path is not None and checksum is not None:
                        recordings[line] = Path(path), checksum
                return recordings, frozenset(unchecked)
        except sqlite3.Error, _InvalidContents, _ObsoleteIndex:
            return None

    def save(
        self,
        directory: Path,
        source: Source,
        lines: Mapping[str, str],
        checked: Mapping[str, Recording | None],
    ) -> None:
        source = _source(source)
        directory_key = _directory(directory)
        canonical = {_text(line): _sha(text) for line, text in lines.items()}
        updates: dict[str, Row] = {}
        for line, recording in checked.items():
            if line not in canonical:
                raise RecordingIndexError(
                    "Checked recording is outside the canonical queue"
                )
            path, checksum = (None, None) if recording is None else recording
            updates[line] = _validate_row(
                (
                    line,
                    canonical[line],
                    1,
                    None if path is None else str(path),
                    checksum,
                )
            )
        self._check_paths()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with exclusive_advisory_lock(
                self.root / ".recordings.guard", blocking=True
            ):
                if self._check_paths() is None:
                    self._initialize()
                observed = self._check_paths()
                try:
                    self._save(directory_key, source, canonical, updates)
                except (_ObsoleteIndex, sqlite3.DatabaseError) as error:
                    if isinstance(error, sqlite3.DatabaseError):
                        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
                        if code not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                            raise
                    if any(
                        Path(str(self.path) + suffix).exists()
                        for suffix in ("-journal", "-wal", "-shm")
                    ):
                        raise RecordingIndexError(
                            "Cannot replace a corrupt recording index with active journal files"
                        ) from error
                    if observed is None:
                        raise RecordingIndexError(
                            "Corrupt recording index disappeared"
                        ) from error
                    self._initialize(replace=observed)
                    self._save(directory_key, source, canonical, updates)
        except (sqlite3.Error, OSError) as error:
            raise RecordingIndexError(
                f"Unable to save recording index: {error}"
            ) from error

    def _save(
        self,
        directory: str,
        source: Source,
        lines: dict[str, str],
        updates: dict[str, Row],
    ) -> None:
        with self._connection(write=True) as connection:
            assert connection is not None
            try:
                saved = self._rows(connection, directory)
            except _InvalidContents:
                saved = None
            rows: dict[str, Row] = {
                line: (line, text, 0, None, None) for line, text in lines.items()
            }
            if saved is not None and saved[0] == source:
                rows.update(
                    {row[0]: row for row in saved[1] if lines.get(row[0]) == row[1]}
                )
            rows.update(updates)
            grouped: dict[int, list[Row]] = {
                bucket: [] for bucket in range(_BUCKET_COUNT)
            }
            for line in sorted(rows):
                grouped[_bucket(line)].append(rows[line])
            # Corrupt derived indexes can contain children left without their parent.
            connection.execute("DELETE FROM lines WHERE directory = ?", (directory,))
            connection.execute("DELETE FROM buckets WHERE directory = ?", (directory,))
            connection.execute(
                "DELETE FROM directories WHERE directory = ?", (directory,)
            )
            connection.execute(
                "INSERT INTO directories VALUES (?, ?, ?, ?, ?)",
                (directory, *source, _receipt(directory, source, [])),
            )
            connection.executemany(
                "INSERT INTO buckets VALUES (?, ?, ?)",
                (
                    (directory, bucket, _receipt(directory, source, values, bucket))
                    for bucket, values in grouped.items()
                ),
            )
            connection.executemany(
                "INSERT INTO lines VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (directory, bucket, *row)
                    for bucket, values in grouped.items()
                    for row in values
                ),
            )
