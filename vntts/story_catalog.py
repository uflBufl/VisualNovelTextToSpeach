"""Derived story summaries with indexed, lazily loaded ordered line identities."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import overload

from vntts.audio_store import _sync_directory, _sync_regular_file

_APPLICATION_ID = 0x564E5343
_VERSION = 1
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
_SCHEMA = (
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


class StoryCatalogError(ValueError):
    """A derived catalog is invalid or no longer matches its source evidence."""


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
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._safe_path()
        try:
            with closing(
                sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True)
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA trusted_schema = OFF")
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                if (
                    connection.execute("PRAGMA application_id").fetchone()[0]
                    != _APPLICATION_ID
                    or connection.execute("PRAGMA user_version").fetchone()[0]
                    != _VERSION
                ):
                    raise StoryCatalogError("Unsupported story catalog schema")
                objects = {
                    row[0]
                    for row in connection.execute(
                        "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                    )
                }
                if objects != set(_SCHEMA):
                    raise StoryCatalogError("Story catalog schema changed")
                if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                    raise StoryCatalogError("Unsupported story catalog journal mode")
                yield connection
        except sqlite3.Error as error:
            raise StoryCatalogError(f"Unable to read story catalog: {error}") from error

    def load(
        self, story_sha256: str, evidence_sha256: str, cache_version: int
    ) -> dict[str, object] | None:
        self._safe_path()
        if not self.path.exists():
            return None
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

    def save(self, document: Mapping[str, object]) -> None:
        self._safe_path()
        if self.path.exists():
            with self._connection() as connection:
                version = connection.execute(
                    "SELECT cache_version FROM metadata WHERE id=1"
                ).fetchone()
                current_version = document["schema_version"]
                if (
                    version is not None
                    and isinstance(current_version, int)
                    and version[0] > current_version
                ):
                    raise StoryCatalogError(
                        "Unsupported future story catalog cache version"
                    )
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
                connection.commit()
            _sync_regular_file(temporary)
            self._safe_path()
            os.replace(temporary, self.path)
            _sync_directory(self.path.parent)
        except sqlite3.Error as error:
            raise StoryCatalogError(f"Unable to save story catalog: {error}") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

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
