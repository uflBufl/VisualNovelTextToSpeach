"""Transactional per-item working state; portable authoring snapshots stay JSON."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from vntts.audio_store import _sync_directory, _sync_regular_file
from vntts.json_types import decode_json
from vntts.path_safety import read_regular_bytes

_APPLICATION_ID = 0x564E4753
_VERSION = 1
_SCHEMA = """
CREATE TABLE run (
 id INTEGER PRIMARY KEY CHECK(id=1),
 queue_sha256 TEXT NOT NULL,
 metadata TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>=1),
 controls_kind TEXT NOT NULL CHECK(controls_kind IN ('absent','null','object'))
) STRICT;
CREATE TABLE items (
 queue_id TEXT PRIMARY KEY,
 line_id TEXT,
 text_sha256 TEXT,
 status TEXT NOT NULL CHECK(status IN ('generated','approved','failed','live_fallback','omitted')),
 review_status TEXT,
 voice_character TEXT,
 audio_path TEXT,
 audio_sha256 TEXT,
 attempts INTEGER NOT NULL CHECK(attempts>=0),
 payload TEXT NOT NULL
) STRICT;
CREATE INDEX items_line ON items(line_id, text_sha256);
CREATE INDEX items_status ON items(status);
CREATE TABLE controls (identity TEXT PRIMARY KEY, payload TEXT NOT NULL) STRICT;
"""
_SCHEMA_OBJECTS = {
    statement.split()[2]: statement.strip()
    for statement in _SCHEMA.split(";")
    if statement.strip()
}
_REVIEWS: dict[str, set[str | None]] = {
    "generated": {"pending_review", "rejected"},
    "approved": {"approved"},
    "failed": {None},
    "live_fallback": {"live_fallback"},
    "omitted": {"omitted"},
}
_ITEM_COLUMNS = "queue_id,line_id,text_sha256,status,review_status,voice_character,audio_path,audio_sha256,attempts,payload"


def state_path(output: Path) -> Path:
    """Prefer SQLite authority; preserved JSON is an import-only snapshot."""
    database = output / "generation-state.sqlite3"
    return (
        database
        if database.exists() or database.is_symlink()
        else output / "generation-state.json"
    )


def is_database(path: Path) -> bool:
    return path.suffix == ".sqlite3" or ".sqlite3." in path.name


def _path(path: Path) -> Path:
    path = path.expanduser()
    if path.parent.is_symlink():
        raise ValueError(
            f"Generation database directory is a symbolic link: {path.parent}"
        )
    return path.parent.resolve() / path.name


def _safe_path(path: Path) -> None:
    if path.parent.is_symlink():
        raise ValueError(
            f"Generation database directory is a symbolic link: {path.parent}"
        )
    for candidate in (
        path,
        *(
            path.with_name(path.name + suffix)
            for suffix in ("-journal", "-wal", "-shm")
        ),
    ):
        try:
            info = candidate.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Generation database must be a regular file: {candidate}")


def _verify(connection: sqlite3.Connection, *, snapshot: bool = False) -> None:
    if connection.execute("PRAGMA application_id").fetchone() != (
        _APPLICATION_ID,
    ) or connection.execute("PRAGMA user_version").fetchone() != (_VERSION,):
        raise ValueError("Unsupported generation database schema")
    actual = dict(
        connection.execute(
            "SELECT name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        )
    )
    if actual != _SCHEMA_OBJECTS:
        raise ValueError("Invalid generation database schema objects")
    if connection.execute("SELECT count(*) FROM run").fetchone() != (1,):
        raise ValueError("Generation database has no committed state")
    # Review identities capture the database file, so committed state must not live in a WAL.
    journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
    if journal not in ({"memory", "delete"} if snapshot else {"delete"}):
        raise ValueError("Unsupported generation database journal mode")


@contextmanager
def _connection(path: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    path = _path(path)
    _safe_path(path)
    try:
        with closing(
            sqlite3.connect(
                path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                uri=True,
                timeout=30,
                isolation_level=None,
            )
        ) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            if write:
                connection.execute("PRAGMA synchronous=FULL")
            else:
                connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                _verify(connection)
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    except sqlite3.Error as error:
        raise ValueError(f"Unable to access generation database: {error}") from error


def _object(payload: object) -> dict[str, object]:
    if not isinstance(payload, str):
        raise ValueError("Generation database record is malformed")
    value = decode_json(payload)
    if not isinstance(value, dict):
        raise ValueError("Generation database record must be an object")
    return value


def _item_index(item: dict[str, object]) -> tuple[object, ...]:
    return (
        item.get("line_id"),
        item.get("text_sha256"),
        item.get("status"),
        item.get("review_status"),
        item.get("voice_character"),
        item.get("path"),
        item.get("file_sha256"),
        item.get("attempts", 0),
    )


def _read(
    connection: sqlite3.Connection,
    *,
    selected_line_ids: frozenset[str] | None = None,
    allowed_queue_ids: frozenset[str] | None = None,
) -> dict[str, object]:
    row = connection.execute(
        "SELECT queue_sha256, metadata, controls_kind FROM run WHERE id=1"
    ).fetchone()
    if row is None:
        raise ValueError("Generation database has no committed state")
    state = _object(row[1])
    if (
        state.get("queue_sha256") != row[0]
        or "items" in state
        or "synthesis_controls" in state
    ):
        raise ValueError("Generation database metadata differs from its indexes")
    for key, status, review in connection.execute(
        "SELECT queue_id,status,review_status FROM items"
    ):
        if (
            (allowed_queue_ids is not None and key not in allowed_queue_ids)
            or status not in _REVIEWS
            or review not in _REVIEWS[status]
        ):
            raise ValueError(
                "Generation database contains unknown queue IDs or invalid item status"
            )
    items: dict[str, object] = {}
    queries: list[tuple[str, tuple[str, ...]]]
    if selected_line_ids is None or "reviewed_waveform_publication" in state:
        queries = [(f"SELECT {_ITEM_COLUMNS} FROM items", ())]
    else:
        # Failed/interrupted legacy records can omit line identity; retain their evidence.
        queries = [(f"SELECT {_ITEM_COLUMNS} FROM items WHERE line_id IS NULL", ())]
        selected = sorted(selected_line_ids)
        for start in range(0, len(selected), 900):
            chunk = tuple(selected[start : start + 900])
            queries.append(
                (
                    f"SELECT {_ITEM_COLUMNS} FROM items WHERE line_id IN ({','.join('?' for _ in chunk)})",
                    chunk,
                )
            )
    for query, parameters in queries:
        for key, *values, payload in connection.execute(query, parameters):
            item = _object(payload)
            if _item_index(item) != tuple(values):
                raise ValueError(
                    "Generation database item index differs from its evidence"
                )
            items[key] = item
    state["items"] = items
    controls = {
        key: decode_json(payload)
        for key, payload in connection.execute("SELECT identity,payload FROM controls")
    }
    if row[2] == "object":
        state["synthesis_controls"] = controls
    elif controls:
        raise ValueError("Generation database controls differ from their metadata")
    elif row[2] == "null":
        state["synthesis_controls"] = None
    return state


def read_state(
    path: Path,
    *,
    selected_line_ids: frozenset[str] | None = None,
    allowed_queue_ids: frozenset[str] | None = None,
) -> dict[str, object]:
    with _connection(path) as connection:
        return _read(
            connection,
            selected_line_ids=selected_line_ids,
            allowed_queue_ids=allowed_queue_ids,
        )


def read_snapshot(payload: bytes) -> dict[str, object]:
    """Decode an immutable byte snapshot used by review authority checks."""
    if len(payload) < 100 or not payload.startswith(b"SQLite format 3\x00"):
        raise ValueError("Invalid generation database snapshot header")
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.deserialize(payload)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA query_only=ON")
            _verify(connection, snapshot=True)
            return _read(connection)
    except sqlite3.Error as error:
        raise ValueError(
            f"Unable to read generation database snapshot: {error}"
        ) from error


def snapshot_file(path: Path) -> bytes:
    """Hold a shared SQLite lock while capturing exact committed file bytes."""
    path = _path(path)
    with _connection(path):
        return read_regular_bytes(path)


def _encode(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, OverflowError, RecursionError) as error:
        raise ValueError(f"Generation database value is not JSON: {error}") from error


def _write(
    connection: sqlite3.Connection,
    state: dict[str, object],
    changed_items: tuple[str, ...] | None,
) -> None:
    items = state.get("items")
    controls = state.get("synthesis_controls")
    controls_kind = (
        "object"
        if isinstance(controls, dict)
        else ("null" if "synthesis_controls" in state else "absent")
    )
    queue = state.get("queue_sha256")
    if (
        not isinstance(items, dict)
        or (controls is not None and not isinstance(controls, dict))
        or not isinstance(queue, str)
    ):
        raise ValueError(
            "Generation database requires items, controls and queue identity"
        )
    controls = {} if controls is None else controls
    encoded = _encode(
        {
            key: value
            for key, value in state.items()
            if key not in {"items", "synthesis_controls"}
        }
    )
    existing = connection.execute("SELECT queue_sha256 FROM run WHERE id=1").fetchone()
    if existing is not None and existing[0] != queue:
        raise ValueError("Generation database belongs to another queue")
    connection.execute(
        "INSERT INTO run VALUES(1,?,?,1,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,revision=run.revision+1,controls_kind=excluded.controls_kind",
        (queue, encoded, controls_kind),
    )
    selected = tuple(items) if changed_items is None else changed_items
    for key in selected:
        if not isinstance(key, str):
            raise ValueError("Generation database item key is malformed")
        if key not in items:
            connection.execute("DELETE FROM items WHERE queue_id=?", (key,))
            continue
        item = items[key]
        if (
            not isinstance(item, dict)
            or type(item.get("attempts", 0)) is not int
            or item.get("attempts", 0) < 0
        ):
            raise ValueError("Generation database item is malformed")
        index = _item_index(item)
        if (
            not isinstance(index[2], str)
            or index[2] not in _REVIEWS
            or (index[3] is not None and not isinstance(index[3], str))
            or index[3] not in _REVIEWS[index[2]]
        ):
            raise ValueError("Generation database item status is malformed")
        values = (key, *index, _encode(item))
        connection.execute(
            "INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(queue_id) DO UPDATE SET line_id=excluded.line_id,text_sha256=excluded.text_sha256,status=excluded.status,review_status=excluded.review_status,voice_character=excluded.voice_character,audio_path=excluded.audio_path,audio_sha256=excluded.audio_sha256,attempts=excluded.attempts,payload=excluded.payload WHERE items.payload<>excluded.payload",
            values,
        )
    if changed_items is None:
        for (key,) in connection.execute("SELECT queue_id FROM items").fetchall():
            if key not in items:
                connection.execute("DELETE FROM items WHERE queue_id=?", (key,))
    for key, value in controls.items():
        if not isinstance(key, str):
            raise ValueError("Generation database control identity is malformed")
        connection.execute(
            "INSERT INTO controls VALUES(?,?) ON CONFLICT(identity) DO UPDATE SET payload=excluded.payload WHERE controls.payload<>excluded.payload",
            (key, _encode(value)),
        )
    for (key,) in connection.execute("SELECT identity FROM controls").fetchall():
        if key not in controls:
            connection.execute("DELETE FROM controls WHERE identity=?", (key,))


def _initialize(path: Path, state: dict[str, object]) -> bool:
    """Publish only a committed complete state, never an empty schema."""
    _safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(
            dir=path.parent, suffix=".sqlite3", delete=False
        ) as stream:
            temporary = Path(stream.name)
        try:
            with closing(
                sqlite3.connect(temporary, isolation_level=None)
            ) as connection:
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _SCHEMA_OBJECTS.values():
                        connection.execute(statement)
                    connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                    connection.execute(f"PRAGMA user_version={_VERSION}")
                    _write(connection, state, None)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                _verify(connection)
                _read(connection)
        except sqlite3.Error as error:
            raise ValueError(
                f"Unable to initialize generation database: {error}"
            ) from error
        _sync_regular_file(temporary)
        _safe_path(path)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        _sync_directory(path.parent)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_state(
    path: Path,
    state: dict[str, object],
    *,
    changed_items: tuple[str, ...] | None = None,
) -> None:
    path = _path(path)
    _safe_path(path)
    if not path.exists() and _initialize(path, state):
        return
    with _connection(path, write=True) as connection:
        _write(connection, state, changed_items)


def active_attempt(path: Path) -> object:
    with _connection(path) as connection:
        row = connection.execute(
            "SELECT queue_sha256,metadata FROM run WHERE id=1"
        ).fetchone()
        if row is None:
            raise ValueError("Generation database has no committed state")
        state = _object(row[1])
        if state.get("queue_sha256") != row[0]:
            raise ValueError("Generation database queue identity changed")
        return state.get("active")
