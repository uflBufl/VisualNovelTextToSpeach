"""Derived story summaries with indexed, lazily loaded ordered line identities."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, overload

from vntts_artifacts.story_index import StoryIndexDocument

from vntts.audio_store import _sync_directory, _sync_regular_file
from vntts.dialogue_search import text_projection as _projection
from vntts.document_identity import canonical_document_sha256, is_lowercase_sha256
from vntts.json_types import decode_json, is_json_object
from vntts.path_safety import open_regular_binary

if TYPE_CHECKING:
    from vntts.chapter_voice_preload import ChapterDialogue

_APPLICATION_ID = 0x564E5343
_VERSION = 4
_GRAM_BUCKET = 8
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
_V2_SCHEMA = (
    *_LEGACY_SCHEMA,
    "CREATE TABLE record_source (id INTEGER PRIMARY KEY CHECK(id=1), source_size INTEGER NOT NULL, "
    "header_length INTEGER NOT NULL, header_sha256 TEXT NOT NULL, record_count INTEGER NOT NULL, "
    "semantic_valid INTEGER NOT NULL CHECK(semantic_valid IN (0, 1)), receipt_sha256 TEXT NOT NULL) STRICT",
    "CREATE TABLE record_locations (line_id TEXT PRIMARY KEY, source_offset INTEGER NOT NULL, "
    "source_length INTEGER NOT NULL, row_sha256 TEXT NOT NULL) STRICT, WITHOUT ROWID",
)
_SCHEMA = (
    *_V2_SCHEMA,
    "CREATE TABLE dialogue_source (id INTEGER PRIMARY KEY CHECK(id=1), header BLOB NOT NULL) STRICT",
    "CREATE TABLE dialogue (id INTEGER PRIMARY KEY, line_id TEXT NOT NULL UNIQUE, "
    "chapter TEXT NOT NULL, speaker TEXT NOT NULL, speaker_key TEXT NOT NULL, symbol_only INTEGER NOT NULL, "
    "exact_key TEXT NOT NULL, normalized_key TEXT NOT NULL, projection TEXT NOT NULL, "
    "projection_length INTEGER NOT NULL, original_speech INTEGER NOT NULL, "
    "full_source_audio INTEGER NOT NULL, voice_character TEXT, payload TEXT NOT NULL) STRICT",
    "CREATE INDEX dialogue_chapter ON dialogue(chapter, id)",
    "CREATE INDEX dialogue_speaker ON dialogue(speaker_key, id)",
    "CREATE INDEX dialogue_exact ON dialogue(symbol_only, exact_key, speaker_key)",
    "CREATE INDEX dialogue_normalized ON dialogue(normalized_key, speaker_key)",
    "CREATE INDEX dialogue_projection ON dialogue(projection)",
    "CREATE INDEX dialogue_length ON dialogue(projection_length)",
    "CREATE INDEX dialogue_original ON dialogue(original_speech)",
    "CREATE INDEX dialogue_full_audio ON dialogue(full_source_audio)",
    "CREATE VIRTUAL TABLE dialogue_grams USING fts5(tokens, tokenize='ascii', detail='none', content='')",
    "CREATE VIRTUAL TABLE dialogue_frequency USING fts5vocab(dialogue_grams, 'row')",
)


@lru_cache(maxsize=1)
def _schema_objects() -> set[tuple[str, str, str | None]]:
    # SQLite owns FTS shadow schemas; compare with this runtime's template.
    with closing(sqlite3.connect(":memory:")) as connection:
        for statement in _SCHEMA:
            connection.execute(statement)
        return set(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        )


def _gram_token(gram: str, bucket: int) -> str:
    return f"p{bucket}q{len(gram)}g{gram.encode().hex()}"


def _gram_tokens(key: str) -> str:
    return " ".join(
        sorted(
            {
                _gram_token(key[index : index + width], index // _GRAM_BUCKET)
                for width in (1, 2, 3)
                for index in range(len(key) - width + 1)
            }
        )
    )


def _prefix_predicate(projection: str) -> tuple[str, tuple[object, ...]]:
    for index in range(len(projection) - 1, -1, -1):
        if ord(projection[index]) < 0x10FFFF:
            upper = projection[:index] + chr(ord(projection[index]) + 1)
            return "projection>=? AND projection<?", (projection, upper)
    return "projection>=?", (projection,)


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
        self, *, allow_legacy: bool = False, reusable: bool = False
    ) -> Iterator[sqlite3.Connection]:
        self._safe_path()
        try:
            with closing(
                sqlite3.connect(
                    self.path.absolute().as_uri() + "?mode=ro",
                    uri=True,
                    # Owners execute per thread; only explicit close crosses threads.
                    check_same_thread=not reusable,
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA trusted_schema = OFF")
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if connection.execute("PRAGMA application_id").fetchone()[
                    0
                ] != _APPLICATION_ID or version not in (1, 2, 3, _VERSION):
                    raise StoryCatalogError("Unsupported story catalog schema")
                if version in (3, _VERSION):
                    objects = {
                        tuple(row)
                        for row in connection.execute(
                            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                        )
                    }
                    valid_schema = objects == _schema_objects()
                else:
                    objects = {
                        row[0]
                        for row in connection.execute(
                            "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                        )
                    }
                    valid_schema = objects == set(
                        _LEGACY_SCHEMA if version == 1 else _V2_SCHEMA
                    )
                if not valid_schema:
                    raise StoryCatalogError("Story catalog schema changed")
                if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                    raise StoryCatalogError("Unsupported story catalog journal mode")
                if version != _VERSION and not allow_legacy:
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

    def open_dialogue(
        self,
        source_path: Path,
        story_sha256: str,
        evidence_sha256: str,
        *,
        cache_version: int,
    ) -> DialogueCatalog:
        """Pin one SQL revision; original source rows remain payload authority."""
        return DialogueCatalog(
            self,
            source_path,
            story_sha256,
            evidence_sha256,
            cache_version=cache_version,
        )

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
        progress: Callable[[int, int], None] | None = None,
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
                        connection, document, story, source_semantics_valid, progress
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
            # The story writer emits sorted UTF-8 JSON. Exact bytes prove the
            # match without parsing and serializing the record again.
            if raw == (
                json.dumps(
                    expected, ensure_ascii=False, sort_keys=True, allow_nan=False
                ).encode("utf-8")
                + b"\n"
            ):
                return expected
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
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        if type(semantic_valid) is not bool or story.path.is_symlink():
            raise StoryCatalogError("Story catalog source identity is invalid")
        total = len(story.records)
        if progress is not None:
            progress(0, total)
        from vntts.chapter_voice_preload import (
            ChapterVoicePreloader,
            _normalize,
            _normalize_exact_text,
            _source_audio_evidence_payload,
        )
        from vntts.dialog import is_symbol_only_text

        evidence_path = story.path.with_name("source-audio-semantic-evidence.json")
        evidence_signature = _file_signature(evidence_path)
        if (
            hashlib.sha256(_source_audio_evidence_payload(story.path)).hexdigest()
            != document["source_audio_semantic_evidence_sha256"]
        ):
            raise StoryCatalogError("Story catalog source evidence changed")
        dialogue = iter(
            ChapterVoicePreloader.dialogue_from_story_index(story.path, story)
        )
        digest = hashlib.sha256()
        try:
            with open_regular_binary(story.path) as stream:
                before = os.fstat(stream.fileno())
                header = stream.readline()
                cls._matching_source_json(header, story.metadata)
                digest.update(header)
                connection.execute(
                    "INSERT INTO dialogue_source VALUES (1, ?)", (header,)
                )
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
                    line = next(dialogue)
                    projection = _projection(record.text)
                    row_id = count + 1
                    connection.execute(
                        "INSERT INTO dialogue VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row_id,
                            record.line_id,
                            record.chapter,
                            record.speaker,
                            _normalize(record.speaker),
                            int(is_symbol_only_text(record.text)),
                            _normalize_exact_text(record.text),
                            _normalize(record.text),
                            projection,
                            len(projection),
                            int(line.has_original_speech),
                            int(line.has_full_source_audio),
                            record.voice_character,
                            json.dumps(asdict(line), ensure_ascii=False),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO dialogue_grams(rowid, tokens) VALUES (?, ?)",
                        (row_id, _gram_tokens(projection)),
                    )
                    digest.update(raw)
                    offset += len(raw)
                    count += 1
                    if progress is not None and (count % 1024 == 0 or count == total):
                        progress(count, total)
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
            _file_signature(evidence_path) != evidence_signature
            or next(records, None) is not None
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


def _file_signature(path: Path) -> tuple[int, ...] | None:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class DialogueCatalog:
    """Admit source files once; keep one SQL read transaction per thread until close."""

    def __init__(
        self,
        catalog: StoryCatalog,
        source_path: Path,
        story_sha256: str,
        evidence_sha256: str,
        *,
        cache_version: int,
    ) -> None:
        from vntts.content_revision import source_signature

        self.catalog = catalog
        self.source_path = Path(source_path).expanduser().absolute()
        self.source_sha256 = story_sha256
        self.evidence_sha256 = evidence_sha256
        self.cache_version = cache_version
        self.signature = source_signature(self.source_path)
        evidence_path = self.source_path.with_name(
            "source-audio-semantic-evidence.json"
        )
        evidence_signature = _file_signature(evidence_path)
        self._connections: dict[
            int, tuple[AbstractContextManager[sqlite3.Connection], sqlite3.Connection]
        ] = {}
        self._lock = threading.Lock()
        self._closed = False
        try:
            from vntts.chapter_voice_preload import _source_audio_evidence_payload

            if (
                hashlib.sha256(
                    _source_audio_evidence_payload(self.source_path)
                ).hexdigest()
                != evidence_sha256
            ):
                raise StoryCatalogError("Story catalog source evidence changed")
            connection = self._connection()
            self._source = connection.execute(
                "SELECT * FROM record_source WHERE id=1"
            ).fetchone()
            header = connection.execute(
                "SELECT header FROM dialogue_source WHERE id=1"
            ).fetchone()
            if self._source is None or header is None:
                raise StoryCatalogError("Story catalog dialogue source is missing")
            catalog._validate_record_source(self._source, story_sha256, evidence_sha256)
            self.header = bytes(header[0])
            if (
                len(self.header) != self._source["header_length"]
                or hashlib.sha256(self.header).hexdigest()
                != self._source["header_sha256"]
                or self.signature[3] != self._source["source_size"]
            ):
                raise StoryCatalogError("Story catalog dialogue source changed")
            metadata = decode_json(self.header)
            if not is_json_object(metadata):
                raise StoryCatalogError("Story catalog dialogue header is invalid")
            self.metadata = metadata
            self.count: int = self._source["record_count"]
            if (
                connection.execute("SELECT count(*) FROM dialogue").fetchone()[0]
                != self.count
            ):
                raise StoryCatalogError("Story catalog dialogue count changed")
            with open_regular_binary(self.source_path) as stream:
                if stream.read(len(self.header)) != self.header:
                    raise StoryCatalogError("Story catalog source header changed")
            if (
                source_signature(self.source_path) != self.signature
                or _file_signature(evidence_path) != evidence_signature
            ):
                raise StoryCatalogError("Story catalog source changed during admission")
        except BaseException:
            self.close()
            raise

    def _connection(self) -> sqlite3.Connection:
        self.ensure_open()
        identity = threading.get_ident()
        with self._lock:
            pair = self._connections.get(identity)
            if pair is None:
                manager = self.catalog._connection(reusable=True)
                connection = manager.__enter__()
                try:
                    row = connection.execute(
                        "SELECT story_sha256, evidence_sha256, cache_version FROM metadata WHERE id=1"
                    ).fetchone()
                    if row is None or tuple(row) != (
                        self.source_sha256,
                        self.evidence_sha256,
                        self.cache_version,
                    ):
                        raise StoryCatalogError(
                            "Story catalog dialogue identity changed"
                        )
                except BaseException:
                    manager.__exit__(None, None, None)
                    raise
                self._connections[identity] = (manager, connection)
                return connection
            return pair[1]

    def ensure_open(self) -> None:
        if self._closed:
            raise StoryCatalogError("Story catalog dialogue owner is closed")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for manager, _connection in self._connections.values():
                manager.__exit__(None, None, None)
            self._connections.clear()

    def __del__(self) -> None:
        # A replaced reading snapshot may release its last owner on a worker.
        if hasattr(self, "_lock"):
            try:
                self.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> DialogueCatalog:
        self.ensure_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _ids(
        self, where: str = "1", parameters: Sequence[object] = ()
    ) -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in self._connection().execute(
                f"SELECT line_id FROM dialogue WHERE {where} ORDER BY id", parameters
            )
        )

    def chapter_ids(self) -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in self._connection().execute(
                "SELECT chapter FROM dialogue GROUP BY chapter ORDER BY min(id)"
            )
        )

    def chapter_exists(self, chapter: str) -> bool:
        return (
            self._connection()
            .execute("SELECT 1 FROM dialogue WHERE chapter=? LIMIT 1", (chapter,))
            .fetchone()
            is not None
        )

    def line_exists(self, line_id: str) -> bool:
        return (
            self._connection()
            .execute("SELECT 1 FROM dialogue WHERE line_id=?", (line_id,))
            .fetchone()
            is not None
        )

    def line_ids(self, chapter: str | None = None) -> tuple[str, ...]:
        return self._ids() if chapter is None else self._ids("chapter=?", (chapter,))

    def iter_line_ids(self, *, original_only: bool = False) -> Iterator[str]:
        where = " WHERE original_speech=1" if original_only else ""
        with closing(
            self._connection().execute(
                f"SELECT line_id FROM dialogue{where} ORDER BY id"
            )
        ) as cursor:
            while True:
                self.ensure_open()
                batch = cursor.fetchmany(256)
                if not batch:
                    return
                yield from (row[0] for row in batch)

    def speakers(self, chapter: str | None = None) -> dict[str, str]:
        where, parameters = ("1", ()) if chapter is None else ("chapter=?", (chapter,))
        return dict(
            self._connection().execute(
                f"SELECT speaker_key, speaker FROM dialogue WHERE {where} GROUP BY speaker_key ORDER BY min(id)",
                parameters,
            )
        )

    def has_original_speech(self) -> bool:
        return (
            self._connection()
            .execute("SELECT 1 FROM dialogue WHERE original_speech=1 LIMIT 1")
            .fetchone()
            is not None
        )

    def has_full_source_audio(self) -> bool:
        return (
            self._connection()
            .execute("SELECT 1 FROM dialogue WHERE full_source_audio=1 LIMIT 1")
            .fetchone()
            is not None
        )

    def voice_role_for_id(self, line_id: str) -> str | None:
        row = (
            self._connection()
            .execute("SELECT voice_character FROM dialogue WHERE line_id=?", (line_id,))
            .fetchone()
        )
        return row[0] if row else None

    @contextmanager
    def query(self, cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        connection = self._connection()
        if cancelled is None:
            yield
            return
        from vntts.chapter_voice_preload import StoryMatchingCancelled

        if cancelled():
            raise StoryMatchingCancelled()
        connection.set_progress_handler(lambda: int(cancelled()), 1000)
        try:
            yield
        except sqlite3.OperationalError as error:
            if cancelled():
                raise StoryMatchingCancelled() from error
            raise StoryCatalogError(
                f"Unable to query story catalog: {error}"
            ) from error
        finally:
            connection.set_progress_handler(None, 0)

    def exact_ids(
        self,
        speaker_key: str | None,
        symbol_only: bool,
        exact_key: str,
    ) -> tuple[str, ...]:
        where = "symbol_only=? AND exact_key=?"
        parameters: tuple[object, ...] = (int(symbol_only), exact_key)
        if speaker_key is not None:
            where += " AND speaker_key=?"
            parameters += (speaker_key,)
        return self._ids(where, parameters)

    def normalized_ids(
        self, speaker_key: str | None, normalized_key: str
    ) -> tuple[str, ...]:
        return (
            self._ids("normalized_key=?", (normalized_key,))
            if speaker_key is None
            else self._ids(
                "normalized_key=? AND speaker_key=?", (normalized_key, speaker_key)
            )
        )

    def _selected(self, line_ids: Sequence[str]) -> dict[str, sqlite3.Row]:
        unique = tuple(dict.fromkeys(line_ids))
        rows: dict[str, sqlite3.Row] = {}
        connection = self._connection()
        for start in range(0, len(unique), 900):
            batch = unique[start : start + 900]
            placeholders = ",".join("?" for _ in batch)
            rows.update(
                (row["line_id"], row)
                for row in connection.execute(
                    f"SELECT line_id, payload FROM dialogue WHERE line_id IN ({placeholders})",
                    batch,
                )
            )
        return rows

    def dialogues(self, line_ids: Sequence[str]) -> tuple[ChapterDialogue, ...]:
        from vntts.chapter_voice_preload import ChapterDialogue

        rows = self._selected(line_ids)
        result = []
        try:
            for key in line_ids:
                if key not in rows:
                    continue
                line = ChapterDialogue(**json.loads(rows[key]["payload"]))
                if line.line_id != key:
                    raise StoryCatalogError(
                        "Story catalog dialogue line identity changed"
                    )
                result.append(line)
        except (KeyError, ValueError, TypeError) as error:
            raise StoryCatalogError("Story catalog dialogue payload changed") from error
        return tuple(result)

    def iter_seed_rows(
        self,
        projection: str,
        max_distance: int = 4,
        prefix: bool = False,
        chapter: str | None = None,
        original: bool | None = None,
    ) -> Iterator[tuple[str, str]]:
        """Stream identities and projections together, without a second SQL lookup."""
        where, parameters = self._seed_where(
            projection, max_distance, prefix, chapter, original
        )
        with closing(
            self._connection().execute(
                f"SELECT line_id, projection FROM dialogue WHERE {where} ORDER BY id",
                parameters,
            )
        ) as cursor:
            while True:
                self.ensure_open()
                batch = cursor.fetchmany(256)
                if not batch:
                    return
                yield from ((row[0], row[1]) for row in batch)

    def _seed_where(
        self,
        projection: str,
        max_distance: int,
        prefix: bool,
        chapter: str | None,
        original: bool | None,
    ) -> tuple[str, Sequence[object]]:
        if max_distance < 0:
            raise ValueError("The edit limit must be nonnegative")
        blocks = max_distance + 1
        if max_distance == 0:
            where, values = (
                _prefix_predicate(projection)
                if prefix
                else ("projection=?", (projection,))
            )
            conditions = [where]
            parameters = list(values)
        elif len(projection) < blocks:
            conditions = ["projection_length>=?"]
            parameters = [len(projection) - max_distance]
            if not prefix:
                conditions.append("projection_length<=?")
                parameters.append(len(projection) + max_distance)
        else:
            width = min(3, len(projection) // blocks)
            connection = self._connection()
            tokens: list[str] = []
            frequencies: dict[str, int] = {}
            for block in range(blocks):
                offset = len(projection) * block // blocks
                value = projection[offset : len(projection) * (block + 1) // blocks]
                options = []
                for relative in range(len(value) - width + 1):
                    position = offset + relative
                    terms = tuple(
                        _gram_token(value[relative : relative + width], bucket)
                        for bucket in range(
                            max(0, position - max_distance) // _GRAM_BUCKET,
                            (position + max_distance) // _GRAM_BUCKET + 1,
                        )
                    )
                    frequency = 0
                    for token in terms:
                        if token not in frequencies:
                            row = connection.execute(
                                "SELECT doc FROM dialogue_frequency WHERE term=?",
                                (token,),
                            ).fetchone()
                            frequencies[token] = row[0] if row else 0
                        frequency += frequencies[token]
                    options.append((frequency, terms))
                # A surviving block may shift by the whole edit budget in either direction.
                tokens.extend(min(options)[1])
            conditions = [
                "id IN (SELECT rowid FROM dialogue_grams WHERE dialogue_grams MATCH ?)",
                "projection_length>=?",
            ]
            parameters = [
                " OR ".join(sorted(set(tokens))),
                len(projection) - max_distance,
            ]
            if not prefix:
                conditions.append("projection_length<=?")
                parameters.append(len(projection) + max_distance)
        if chapter is not None:
            conditions.append("chapter=?")
            parameters.append(chapter)
        if original is not None:
            conditions.append("original_speech=?")
            parameters.append(int(original))
        return " AND ".join(conditions), parameters

    def records(self, line_ids: Sequence[str]) -> StoryIndexDocument:
        """Read requested original rows using the locations admitted with this owner."""
        from vntts.story_index_snapshot import load_story_index_snapshot

        connection = self._connection()
        locations = self.catalog._record_locations(connection, self._source, line_ids)
        if locations is None:
            raise StoryCatalogError("Story catalog selected line identities changed")
        rows = []
        with open_regular_binary(self.source_path) as stream:
            for _line_id, offset, length, _digest in locations:
                stream.seek(offset)
                raw = stream.read(length)
                rows.append(raw)
        metadata = dict(self.metadata)
        metadata.pop("selection_origin", None)
        metadata["line_count"] = len(rows)
        payload = (
            json.dumps(metadata, ensure_ascii=False).encode() + b"\n" + b"".join(rows)
        )
        return load_story_index_snapshot(self.source_path, payload)
