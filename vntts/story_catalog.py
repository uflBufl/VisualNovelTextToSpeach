"""Derived story summaries with indexed, lazily loaded ordered line identities."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import overload

from vntts_artifacts.story_index import StoryIndexDocument

from vntts.audio_store import _sync_directory, _sync_regular_file
from vntts.document_identity import canonical_document_sha256, is_lowercase_sha256
from vntts.json_types import decode_json, is_json_object
from vntts.path_safety import open_regular_binary

_APPLICATION_ID = 0x564E5343
_VERSION = 2
_SCALARS = (
    "selection_id",
    "title",
    "kind",
    "order",
    "line_count",
    "speakable_lines",
    "original_audio_lines",
    "generation_lines",
    "speaker_count",
    "generation_text_characters",
    "parent_story_id",
    "parent_story_title",
)
_TEXT_FIELDS = ("speakers", "playback_speakers", "episode_titles", "character_names")
_RECORD_SOURCE_FIELDS = (
    "source_size",
    "header_length",
    "header_sha256",
    "record_count",
    "semantic_valid",
)
_LEGACY_SCHEMA = (
    "CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), cache_version INTEGER NOT NULL, "
    "story_sha256 TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, game TEXT NOT NULL, "
    "game_version TEXT, game_id TEXT) STRICT",
    "CREATE TABLE selections (ordinal INTEGER NOT NULL UNIQUE, selection_id TEXT PRIMARY KEY, "
    "title TEXT NOT NULL, kind TEXT NOT NULL, selection_order INTEGER NOT NULL, "
    "line_count INTEGER NOT NULL, speakable_lines INTEGER NOT NULL, original_audio_lines INTEGER NOT NULL, "
    "generation_lines INTEGER NOT NULL, speaker_count INTEGER NOT NULL, "
    "generation_text_characters INTEGER NOT NULL, parent_story_id TEXT, parent_story_title TEXT) STRICT",
    "CREATE TABLE selection_text (selection_id TEXT NOT NULL REFERENCES selections(selection_id), "
    "field TEXT NOT NULL, ordinal INTEGER NOT NULL, value TEXT NOT NULL, "
    "PRIMARY KEY(selection_id, field, ordinal)) STRICT",
    "CREATE TABLE selection_lines (selection_ordinal INTEGER NOT NULL REFERENCES selections(ordinal), "
    "ordinal INTEGER NOT NULL, line_id TEXT NOT NULL, PRIMARY KEY(selection_ordinal, ordinal)) STRICT, WITHOUT ROWID",
)
_SCHEMA = (
    *_LEGACY_SCHEMA,
    "CREATE TABLE record_source (id INTEGER PRIMARY KEY CHECK(id=1), source_size INTEGER NOT NULL, "
    "header_length INTEGER NOT NULL, header_sha256 TEXT NOT NULL, record_count INTEGER NOT NULL, "
    "semantic_valid INTEGER NOT NULL CHECK(semantic_valid IN (0, 1)), receipt_sha256 TEXT NOT NULL) STRICT",
    "CREATE TABLE record_locations (line_id TEXT PRIMARY KEY, source_offset INTEGER NOT NULL, "
    "source_length INTEGER NOT NULL, row_sha256 TEXT NOT NULL) STRICT, WITHOUT ROWID",
)


class StoryCatalogError(ValueError):
    """A derived catalog is invalid or no longer matches its source evidence."""


class _LegacyStoryCatalog(StoryCatalogError):
    """The exact previous derived schema requires a source-backed rebuild."""


class LazyLineIds(Sequence[str]):
    def __init__(
        self,
        catalog: StoryCatalog,
        selection_id: str,
        count: int,
        story_sha256: str,
        evidence_sha256: str,
    ) -> None:
        self.catalog = catalog
        self.selection_id = selection_id
        self._count = count
        self.story_sha256 = story_sha256
        self.evidence_sha256 = evidence_sha256
        self._values: tuple[str, ...] | None = None
        self.recover: Callable[[], tuple[str, ...]] | None = None

    def _load(self) -> tuple[str, ...]:
        if self._values is None:
            try:
                values = self.catalog.line_ids(
                    self.selection_id,
                    self._count,
                    self.story_sha256,
                    self.evidence_sha256,
                )
            except StoryCatalogError:
                if self.recover is None:
                    raise
                values = self.recover()
            if len(values) != self._count:
                raise StoryCatalogError(
                    "Story selection changed; refresh story content"
                )
            self._values = values
        return self._values

    def __len__(self) -> int:
        return self._count

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[str, ...]: ...

    def __getitem__(self, index: int | slice) -> str | tuple[str, ...]:
        return self._load()[index]

    def __iter__(self) -> Iterator[str]:
        return iter(self._load())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, LazyLineIds):
            return self._load() == other._load()
        if isinstance(other, tuple):
            return self._load() == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._load())

    def __deepcopy__(self, memo: dict[int, object]) -> tuple[str, ...]:
        return self._load()


class StoryCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _safe_path(self) -> None:
        if self.path.parent.is_symlink() or self.path.parent.is_junction():
            raise StoryCatalogError("Story catalog directory must not be a link")
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = self.path.with_name(self.path.name + suffix)
            try:
                info = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise StoryCatalogError("Story catalog must be a regular file")

    @contextmanager
    def _connection(
        self, *, allow_legacy: bool = False
    ) -> Iterator[sqlite3.Connection]:
        self._safe_path()
        try:
            with closing(
                sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True)
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA trusted_schema = OFF")
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if connection.execute("PRAGMA application_id").fetchone()[
                    0
                ] != _APPLICATION_ID or version not in (1, _VERSION):
                    raise StoryCatalogError("Unsupported story catalog schema")
                objects = {
                    row[0]
                    for row in connection.execute(
                        "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                    )
                }
                if objects != set(_LEGACY_SCHEMA if version == 1 else _SCHEMA):
                    raise StoryCatalogError("Story catalog schema changed")
                if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                    raise StoryCatalogError("Unsupported story catalog journal mode")
                if version == 1 and not allow_legacy:
                    raise _LegacyStoryCatalog(
                        "Story catalog requires a derived rebuild"
                    )
                yield connection
        except sqlite3.Error as error:
            raise StoryCatalogError(f"Unable to read story catalog: {error}") from error

    def load(
        self, story_sha256: str, evidence_sha256: str, cache_version: int
    ) -> dict[str, object] | None:
        self._safe_path()
        if not self.path.exists():
            return None
        try:
            return self._load(story_sha256, evidence_sha256, cache_version)
        except _LegacyStoryCatalog:
            return None

    def _load(
        self, story_sha256: str, evidence_sha256: str, cache_version: int
    ) -> dict[str, object] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM metadata WHERE id=1").fetchone()
            if row is None:
                raise StoryCatalogError("Story catalog metadata is missing")
            if (row["story_sha256"], row["evidence_sha256"], row["cache_version"]) != (
                story_sha256,
                evidence_sha256,
                cache_version,
            ):
                return None
            document: dict[str, object] = {
                "schema_version": cache_version,
                "source_story_index_sha256": story_sha256,
                "source_audio_semantic_evidence_sha256": evidence_sha256,
                "game": row["game"],
                "game_version": row["game_version"],
                "game_id": row["game_id"],
            }
            document["selections"] = self._selections(
                connection, story_sha256, evidence_sha256
            )
            return document

    def _selections(
        self, connection: sqlite3.Connection, story_sha256: str, evidence_sha256: str
    ) -> list[dict[str, object]]:
        counts = dict(
            connection.execute(
                "SELECT selection_ordinal, count(*) FROM selection_lines GROUP BY selection_ordinal"
            )
        )
        texts: dict[str, dict[str, list[str]]] = {}
        for selection_id, field, value in connection.execute(
            "SELECT selection_id, field, value FROM selection_text ORDER BY selection_id, field, ordinal"
        ):
            if field not in _TEXT_FIELDS:
                raise StoryCatalogError("Story catalog text field is invalid")
            texts.setdefault(selection_id, {}).setdefault(field, []).append(value)
        selections = []
        ordinals = set()
        for row in connection.execute("SELECT * FROM selections ORDER BY ordinal"):
            ordinals.add(row["ordinal"])
            if counts.get(row["ordinal"], 0) != row["line_count"]:
                raise StoryCatalogError("Story catalog line counts changed")
            document: dict[str, object] = {
                name: row["selection_order" if name == "order" else name]
                for name in _SCALARS
            }
            for field in _TEXT_FIELDS:
                document[field] = texts.get(row["selection_id"], {}).get(field, [])
            document["line_ids"] = LazyLineIds(
                self,
                row["selection_id"],
                row["line_count"],
                story_sha256,
                evidence_sha256,
            )
            selections.append(document)
        if set(counts) - ordinals or set(texts) - {
            str(value["selection_id"]) for value in selections
        }:
            raise StoryCatalogError("Story catalog contains foreign line identities")
        return selections

    def line_ids(
        self, selection_id: str, count: int, story_sha256: str, evidence_sha256: str
    ) -> tuple[str, ...]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT story_sha256, evidence_sha256 FROM metadata WHERE id=1"
            ).fetchone()
            if row is None or tuple(row) != (story_sha256, evidence_sha256):
                raise StoryCatalogError(
                    "Story catalog evidence changed before selection"
                )
            values = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT line_id FROM selection_lines WHERE selection_ordinal = "
                    "(SELECT ordinal FROM selections WHERE selection_id = ?) ORDER BY ordinal",
                    (selection_id,),
                )
            )
            if len(values) != count or any(not value.strip() for value in values):
                raise StoryCatalogError(
                    "Story catalog selected line identities changed"
                )
            return values

    def record_locations(
        self, story_sha256: str, evidence_sha256: str, line_ids: Sequence[str]
    ) -> tuple[tuple[str, int, int, str], ...] | None:
        """Fetch source-byte hints for selected IDs; callers must verify the raw rows."""
        self._safe_path()
        if not self.path.exists():
            return None
        try:
            with self._connection() as connection:
                metadata = connection.execute(
                    "SELECT story_sha256, evidence_sha256 FROM metadata WHERE id=1"
                ).fetchone()
                if metadata is None or tuple(metadata) != (
                    story_sha256,
                    evidence_sha256,
                ):
                    return None
                source = connection.execute(
                    "SELECT * FROM record_source WHERE id=1"
                ).fetchone()
                if source is None:
                    return None
                self._validate_record_source(source, story_sha256, evidence_sha256)
                if source["semantic_valid"] == 0:
                    return None
                return self._record_locations(connection, source, line_ids)
        except _LegacyStoryCatalog:
            return None

    @staticmethod
    def _validate_record_source(
        source: sqlite3.Row, story_sha256: str, evidence_sha256: str
    ) -> None:
        if (
            source["source_size"] < 1
            or not 0 < source["header_length"] <= source["source_size"]
            or source["record_count"] < 0
            or not is_lowercase_sha256(source["header_sha256"])
            or source["semantic_valid"] not in (0, 1)
            or source["receipt_sha256"]
            != StoryCatalog._record_source_receipt(
                dict(source), story_sha256, evidence_sha256
            )
        ):
            raise StoryCatalogError("Story catalog record-source metadata changed")

    @staticmethod
    def _record_source_receipt(
        source: Mapping[str, object], story_sha256: object, evidence_sha256: object
    ) -> str:
        # Detect damaged derived hints; selected raw source bytes remain the authority.
        return canonical_document_sha256(
            {
                "story_sha256": story_sha256,
                "evidence_sha256": evidence_sha256,
                **{field: source[field] for field in _RECORD_SOURCE_FIELDS},
            }
        )

    @staticmethod
    def _record_locations(
        connection: sqlite3.Connection, source: sqlite3.Row, line_ids: Sequence[str]
    ) -> tuple[tuple[str, int, int, str], ...] | None:
        if any(
            not isinstance(line_id, str) or not line_id.strip() for line_id in line_ids
        ):
            raise StoryCatalogError(
                "Story catalog selected line identities are invalid"
            )
        unique = tuple(dict.fromkeys(line_ids))
        if len(unique) > source["record_count"]:
            return None
        locations = {}
        for start in range(0, len(unique), 900):
            batch = unique[start : start + 900]
            placeholders = ",".join("?" for _ in batch)
            for row in connection.execute(
                "SELECT line_id, source_offset, source_length, row_sha256 FROM record_locations "
                f"WHERE line_id IN ({placeholders})",
                batch,
            ):
                line_id, offset, length, digest = tuple(row)
                if (
                    offset < source["header_length"]
                    or length < 1
                    or offset + length > source["source_size"]
                    or not is_lowercase_sha256(digest)
                ):
                    raise StoryCatalogError("Story catalog record location changed")
                locations[line_id] = (line_id, offset, length, digest)
        if len(locations) != len(unique):
            return None
        return tuple(locations[line_id] for line_id in line_ids)

    def _check_replaceable(self, cache_version: object) -> None:
        self._safe_path()
        if self.path.exists():
            with self._connection(allow_legacy=True) as connection:
                version = connection.execute(
                    "SELECT cache_version FROM metadata WHERE id=1"
                ).fetchone()
                if (
                    version is not None
                    and isinstance(cache_version, int)
                    and version[0] > cache_version
                ):
                    raise StoryCatalogError(
                        "Unsupported future story catalog cache version"
                    )

    def save(
        self,
        document: Mapping[str, object],
        *,
        story: StoryIndexDocument | None = None,
        source_semantics_valid: bool = True,
    ) -> None:
        self._check_replaceable(document["schema_version"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(
                dir=self.path.parent,
                prefix=".story-catalog-",
                suffix=".sqlite3",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
            with closing(sqlite3.connect(temporary)) as connection:
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {_VERSION}")
                self._write(connection, document)
                if story is not None:
                    self._write_records(
                        connection, document, story, source_semantics_valid
                    )
                connection.commit()
            _sync_regular_file(temporary)
            self._check_replaceable(document["schema_version"])
            os.replace(temporary, self.path)
            _sync_directory(self.path.parent)
        except sqlite3.Error as error:
            raise StoryCatalogError(f"Unable to save story catalog: {error}") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _matching_source_json(
        raw: bytes, expected: dict[str, object]
    ) -> dict[str, object]:
        try:
            actual = decode_json(raw)
            if not is_json_object(actual) or canonical_document_sha256(
                actual
            ) != canonical_document_sha256(expected):
                raise StoryCatalogError(
                    "Story catalog source differs from its parsed document"
                )
        except (UnicodeError, ValueError, TypeError) as error:
            raise StoryCatalogError(
                f"Story catalog source JSON changed: {error}"
            ) from error
        return actual

    @classmethod
    def _write_records(
        cls,
        connection: sqlite3.Connection,
        document: Mapping[str, object],
        story: StoryIndexDocument,
        semantic_valid: bool,
    ) -> None:
        if type(semantic_valid) is not bool or story.path.is_symlink():
            raise StoryCatalogError("Story catalog source identity is invalid")
        digest = hashlib.sha256()
        try:
            with open_regular_binary(story.path) as stream:
                before = os.fstat(stream.fileno())
                header = stream.readline()
                cls._matching_source_json(header, story.metadata)
                digest.update(header)
                offset = len(header)
                records = iter(story.records)
                count = 0
                for raw in stream:
                    record = next(records, None)
                    if record is None:
                        raise StoryCatalogError(
                            "Story catalog source record count changed"
                        )
                    actual = cls._matching_source_json(raw, record.to_record())
                    if actual.get("line_id") != record.line_id:
                        raise StoryCatalogError(
                            "Story catalog source line identity changed"
                        )
                    connection.execute(
                        "INSERT INTO record_locations VALUES (?, ?, ?, ?)",
                        (
                            record.line_id,
                            offset,
                            len(raw),
                            hashlib.sha256(raw).hexdigest(),
                        ),
                    )
                    digest.update(raw)
                    offset += len(raw)
                    count += 1
                after = os.fstat(stream.fileno())
            current = story.path.stat(follow_symlinks=False)
        except OSError as error:
            raise StoryCatalogError(
                f"Unable to index story catalog source: {error}"
            ) from error
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
        )
        # Windows stat() and fstat() can expose different meanings for ctime.
        if (
            next(records, None) is not None
            or offset != before.st_size
            or digest.hexdigest() != document["source_story_index_sha256"]
            or before.st_ctime_ns != after.st_ctime_ns
            or any(
                getattr(before, field) != getattr(info, field)
                for info in (after, current)
                for field in fields
            )
        ):
            raise StoryCatalogError("Story catalog source changed while indexing")
        source: dict[str, object] = {
            "source_size": offset,
            "header_length": len(header),
            "header_sha256": hashlib.sha256(header).hexdigest(),
            "record_count": count,
            "semantic_valid": int(semantic_valid),
        }
        receipt = cls._record_source_receipt(
            source,
            document["source_story_index_sha256"],
            document["source_audio_semantic_evidence_sha256"],
        )
        connection.execute(
            "INSERT INTO record_source VALUES (1, ?, ?, ?, ?, ?, ?)",
            (*(source[field] for field in _RECORD_SOURCE_FIELDS), receipt),
        )

    @staticmethod
    def _write(connection: sqlite3.Connection, document: Mapping[str, object]) -> None:
        connection.execute(
            "INSERT INTO metadata VALUES (1, ?, ?, ?, ?, ?, ?)",
            (
                document["schema_version"],
                document["source_story_index_sha256"],
                document["source_audio_semantic_evidence_sha256"],
                document["game"],
                document.get("game_version"),
                document.get("game_id"),
            ),
        )
        selections = document["selections"]
        if not isinstance(selections, list):
            raise StoryCatalogError("Story catalog selections must be a list")
        for ordinal, selection in enumerate(selections):
            if not isinstance(selection, dict):
                raise StoryCatalogError("Story catalog selection must be an object")
            connection.execute(
                "INSERT INTO selections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ordinal, *(selection[name] for name in _SCALARS)),
            )
            connection.executemany(
                "INSERT INTO selection_lines VALUES (?, ?, ?)",
                (
                    (ordinal, position, line_id)
                    for position, line_id in enumerate(selection["line_ids"])
                ),
            )
            for field in _TEXT_FIELDS:
                connection.executemany(
                    "INSERT INTO selection_text VALUES (?, ?, ?, ?)",
                    (
                        (selection["selection_id"], field, position, value)
                        for position, value in enumerate(selection[field])
                    ),
                )
