"""Transactional per-item working state and immutable SQLite review snapshots."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter, process_time
from uuid import uuid4

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


@dataclass
class _PhaseMeasurement:
    elapsed: float = 0.0
    cpu: float = 0.0
    count: int = 0
    failed: bool = False


_generation_measurements: ContextVar[dict[str, _PhaseMeasurement] | None] = ContextVar(
    "generation_measurements", default=None
)


@contextmanager
def measure_generation_phase(name: str) -> Iterator[None]:
    """Aggregate repeated work without publishing a log event for each item."""
    measurements = _generation_measurements.get()
    if measurements is None:
        yield
        return
    started, cpu_started = perf_counter(), process_time()
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        phase = measurements.setdefault(name, _PhaseMeasurement())
        phase.elapsed += perf_counter() - started
        phase.cpu += process_time() - cpu_started
        phase.count += 1
        phase.failed |= failed


@contextmanager
def measure_generation_run() -> Iterator[None]:
    """Flush bounded inclusive phase totals once, including failed executions."""
    from vntts.support import performance_operation_id, record_background_operation

    if _generation_measurements.get() is not None:
        yield
        return
    measurements: dict[str, _PhaseMeasurement] = {}
    token = _generation_measurements.set(measurements)
    operation_id = uuid4().hex
    operation_token = performance_operation_id.set(operation_id)
    try:
        with measure_generation_phase("total"):
            yield
    finally:
        _generation_measurements.reset(token)
        performance_operation_id.reset(operation_token)
        for name, phase in measurements.items():
            record_background_operation(
                f"bulk-generation-{name}",
                phase.elapsed * 1000,
                "failed" if phase.failed else "complete",
                cpu_ms=phase.cpu * 1000,
                count=phase.count,
                operation_id=operation_id,
                parent_operation="bulk-generation-total" if name != "total" else None,
                timing_scope="inclusive",
            )


@dataclass(frozen=True)
class GenerationProgress:
    queue_sha256: str
    generated: int
    failed: int
    other_terminal: int
    active: object
    ready_lines: tuple[tuple[str, str], ...]


def state_path(output: Path) -> Path:
    """Return the only working-state authority."""
    return output / "generation-state.sqlite3"


def require_registered_output(output: Path) -> None:
    """Refuse unregistered saved work before a caller considers fresh synthesis."""
    database = state_path(output)
    validate_database_path(database)
    if database.exists():
        return
    try:
        contents = tuple(output.iterdir())
    except FileNotFoundError:
        return
    bookkeeping = {
        ".generation-lease.json",
        ".generation-lease.guard",
        "stdout.log",
        "stderr.log",
    }
    for entry in contents:
        info = entry.stat(follow_symlinks=False)
        if entry.name in bookkeeping and stat.S_ISREG(info.st_mode):
            continue
        if entry.name == "interrupted" and stat.S_ISDIR(info.st_mode):
            if all(
                re.fullmatch(
                    r"generation-lease-json-[0-9a-f]{12}(?:-[0-9]+)?\.json",
                    archived.name,
                )
                and stat.S_ISREG(archived.stat(follow_symlinks=False).st_mode)
                for archived in entry.iterdir()
            ):
                continue
        raise ValueError(
            "Generation SQLite state is missing for existing output; run the explicit storage upgrade"
        )


def _path(path: Path) -> Path:
    path = path.expanduser()
    if path.parent.is_symlink():
        raise ValueError(
            f"Generation database directory is a symbolic link: {path.parent}"
        )
    return path.parent.resolve() / path.name


def validate_database_path(path: Path) -> None:
    """Admit the database and SQLite sidecars without reading item evidence."""
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
    with measure_generation_phase("database-write" if write else "database-read"):
        path = _path(path)
        validate_database_path(path)
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
            raise ValueError(
                f"Unable to access generation database: {error}"
            ) from error


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


def _validate_item_headers(
    connection: sqlite3.Connection,
    allowed_queue_ids: frozenset[str] | None,
) -> None:
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


def _read_items(
    connection: sqlite3.Connection, selected_line_ids: frozenset[str] | None
) -> dict[str, object]:
    items: dict[str, object] = {}
    queries: list[tuple[str, tuple[str, ...]]]
    if selected_line_ids is None:
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
    return items


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
    _validate_item_headers(connection, allowed_queue_ids)
    state["items"] = _read_items(
        connection,
        None if "reviewed_waveform_publication" in state else selected_line_ids,
    )
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


def read_audio_paths(path: Path) -> tuple[str, ...]:
    """Read all successful paths without decoding unselected item evidence."""
    with _connection(path) as connection:
        paths = tuple(
            row[0]
            for row in connection.execute(
                "SELECT audio_path FROM items WHERE status IN ('generated','approved')"
            )
        )
        if not all(isinstance(value, str) for value in paths):
            raise ValueError("Generation database successful audio path is malformed")
        return paths


def read_progress(path: Path) -> GenerationProgress:
    """Read progress columns without loading completed evidence or controls."""
    with _connection(path) as connection:
        row = connection.execute(
            "SELECT queue_sha256, json_extract(metadata, '$.queue_sha256'), "
            "json_extract(metadata, '$.active'), json_type(metadata, '$.items'), "
            "json_type(metadata, '$.synthesis_controls'), json_extract(metadata, '$.schema'), "
            "json_type(metadata, '$.schema_version'), json_extract(metadata, '$.schema_version') "
            "FROM run WHERE id=1"
        ).fetchone()
        if row is None or row[0] != row[1] or row[3] is not None or row[4] is not None:
            raise ValueError("Generation database queue identity changed")
        if row[5:] != ("vntts.authoring-generation-state", "integer", 1):
            raise ValueError("Unsupported generation working-state schema")
        counts = _progress_counts(connection)
        ready = tuple(
            (line_id, text_sha256)
            for line_id, text_sha256 in connection.execute(
                "SELECT line_id, text_sha256 FROM items WHERE status IN ('generated','approved') "
                "AND line_id IS NOT NULL AND text_sha256 IS NOT NULL ORDER BY line_id, text_sha256"
            )
        )
        return GenerationProgress(
            queue_sha256=row[0],
            generated=counts[0],
            failed=counts[1],
            other_terminal=counts[2],
            active=None if row[2] is None else _object(row[2]),
            ready_lines=ready,
        )


def _progress_counts(connection: sqlite3.Connection) -> tuple[int, int, int]:
    counts: dict[str, int] = {}
    for status, review, count in connection.execute(
        "SELECT status, review_status, count(*) FROM items GROUP BY status, review_status"
    ):
        if status not in _REVIEWS or review not in _REVIEWS[status]:
            raise ValueError("Generation database item status is malformed")
        counts[status] = counts.get(status, 0) + count
    cancelled = 0
    for kind, status, review, failure, marker in connection.execute(
        "SELECT json_type(payload), json_extract(payload, '$.status'), "
        "json_extract(payload, '$.review_status'), json_extract(payload, '$.failure.kind'), "
        "json_type(payload, '$.failure.attempt_counted') "
        "FROM items WHERE status='failed'"
    ):
        if kind != "object" or status != "failed" or review is not None:
            raise ValueError(
                "Generation database failure differs from its progress index"
            )
        if failure == "cancelled" and marker != "false":
            raise ValueError(
                "Counted cancellation requires the explicit storage upgrade"
            )
        cancelled += failure == "cancelled"
    return (
        counts.get("generated", 0) + counts.get("approved", 0),
        counts.get("failed", 0) - cancelled,
        counts.get("live_fallback", 0) + counts.get("omitted", 0),
    )


@measure_generation_phase("database-snapshot-read")
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
    *,
    controls_unchanged: bool = False,
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
    existing = connection.execute(
        "SELECT queue_sha256,controls_kind FROM run WHERE id=1"
    ).fetchone()
    if existing is not None and existing[0] != queue:
        raise ValueError("Generation database belongs to another queue")
    if controls_unchanged and (existing is None or existing[1] != controls_kind):
        raise ValueError("Generation database controls kind changed")
    connection.execute(
        "INSERT INTO run VALUES(1,?,?,1,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,revision=run.revision+1,controls_kind=excluded.controls_kind",
        (queue, encoded, controls_kind),
    )
    _write_items(connection, items, changed_items)
    if not controls_unchanged:
        _write_controls(connection, controls)


def _write_items(
    connection: sqlite3.Connection,
    items: dict[str, object],
    changed_items: tuple[str, ...] | None,
) -> None:
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


def _write_controls(
    connection: sqlite3.Connection, controls: dict[str, object]
) -> None:
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


@measure_generation_phase("database-initialize")
def _initialize(path: Path, state: dict[str, object]) -> bool:
    """Publish only a committed complete state, never an empty schema."""
    validate_database_path(path)
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
        validate_database_path(path)
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
    controls_unchanged: bool = False,
) -> None:
    """Save state; skip controls only when the caller guarantees they are unchanged."""
    path = _path(path)
    validate_database_path(path)
    if not path.exists() and _initialize(path, state):
        return
    with _connection(path, write=True) as connection:
        _write(connection, state, changed_items, controls_unchanged=controls_unchanged)


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
        if (
            state.get("schema"),
            type(state.get("schema_version")),
            state.get("schema_version"),
        ) != ("vntts.authoring-generation-state", int, 1):
            raise ValueError("Unsupported generation working-state schema")
        return state.get("active")
