"""Indexed local preparation jobs and ordered story selections."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from vntts.audio_store import _sync_directory, _sync_regular_file
from vntts.authoring.advisory_lock import exclusive_advisory_lock

_APPLICATION_ID = 0x564E5043
_VERSION = 3
_JOB_FIELDS = (
    "job_id",
    "created_at",
    "updated_at",
    "status",
    "provider_id",
    "game",
    "game_version",
    "game_id",
    "story_index",
    "story_index_sha256",
    "source_story_index",
    "source_story_index_sha256",
    "catalog_story_index_sha256",
)
_ESTIMATE_FIELDS = (
    "selected_lines",
    "original_audio_lines",
    "generation_lines",
    "speaker_count",
    "estimated_generation_minutes",
    "estimated_disk_bytes",
    "generation_text_characters",
    "rough_audio_seconds",
)
_PLAN_FIELDS = (
    "job_id",
    "created_at",
    "story_index_sha256",
    "voice_manifest",
    "voice_manifest_sha256",
    "synthesis_backend",
    "synthesis_model",
    "synthesis_language",
    "synthesis_profile",
    "pocket_voice_cloning",
    "xtts_terms_accepted",
    "synthesis_controls_sha256",
)
_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
    "updated_at TEXT NOT NULL, status TEXT NOT NULL, provider_id TEXT NOT NULL, "
    "game TEXT NOT NULL, game_version TEXT, game_id TEXT, story_index TEXT NOT NULL, "
    "story_index_sha256 TEXT NOT NULL, source_story_index TEXT, "
    "source_story_index_sha256 TEXT, catalog_story_index_sha256 TEXT NOT NULL, "
    + ", ".join(
        f"{name} INTEGER NOT NULL CHECK ({name} >= 0)" for name in _ESTIMATE_FIELDS
    )
    + ") STRICT",
    "CREATE INDEX jobs_content ON jobs(catalog_story_index_sha256, updated_at)",
    "CREATE TABLE job_stories (job_id TEXT NOT NULL REFERENCES jobs(job_id) "
    "ON DELETE CASCADE, ordinal INTEGER NOT NULL, story_id TEXT NOT NULL, "
    "PRIMARY KEY(job_id, ordinal)) STRICT",
    "CREATE TABLE job_lines (job_id TEXT NOT NULL REFERENCES jobs(job_id) "
    "ON DELETE CASCADE, ordinal INTEGER NOT NULL, line_id TEXT NOT NULL, "
    "PRIMARY KEY(job_id, ordinal)) STRICT",
    "CREATE TABLE content_selections (catalog_story_index_sha256 TEXT PRIMARY KEY) STRICT",
    "CREATE TABLE content_selection_stories (catalog_story_index_sha256 TEXT NOT NULL "
    "REFERENCES content_selections(catalog_story_index_sha256) ON DELETE CASCADE, "
    "ordinal INTEGER NOT NULL, story_id TEXT NOT NULL, "
    "PRIMARY KEY(catalog_story_index_sha256, ordinal)) STRICT",
    "CREATE TABLE voice_plans (job_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, "
    "created_at TEXT NOT NULL, story_index_sha256 TEXT NOT NULL, voice_manifest TEXT, "
    "voice_manifest_sha256 TEXT, synthesis_backend TEXT NOT NULL, synthesis_model TEXT, "
    "synthesis_language TEXT, synthesis_profile TEXT NOT NULL, "
    "pocket_voice_cloning INTEGER NOT NULL CHECK (pocket_voice_cloning IN (0, 1)), "
    "xtts_terms_accepted INTEGER NOT NULL CHECK (xtts_terms_accepted IN (0, 1)), "
    "synthesis_controls_sha256 TEXT NOT NULL) STRICT",
    "CREATE INDEX voice_plans_controls ON voice_plans(synthesis_controls_sha256)",
    "CREATE TABLE voice_groups (job_id TEXT NOT NULL REFERENCES voice_plans(job_id) "
    "ON DELETE CASCADE, ordinal INTEGER NOT NULL, group_id TEXT NOT NULL, "
    "character TEXT NOT NULL, source_id TEXT NOT NULL, control_sha256 TEXT NOT NULL, "
    "route TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(job_id, ordinal)) STRICT",
    "CREATE INDEX voice_groups_identity ON voice_groups(group_id, control_sha256)",
    "CREATE TABLE voice_plan_suggestions (job_id TEXT NOT NULL REFERENCES voice_plans(job_id) "
    "ON DELETE CASCADE, ordinal INTEGER NOT NULL, payload TEXT NOT NULL, "
    "PRIMARY KEY(job_id, ordinal)) STRICT",
    "CREATE TABLE voice_decisions (decision_key TEXT PRIMARY KEY, group_id TEXT NOT NULL, "
    "decision_context_sha256 TEXT NOT NULL, source_id TEXT NOT NULL, decided_at TEXT NOT NULL, "
    "UNIQUE(group_id, decision_context_sha256)) STRICT",
    "CREATE TABLE generation_inputs (identity TEXT PRIMARY KEY, job_id TEXT NOT NULL, "
    "directory TEXT NOT NULL UNIQUE, catalog_story_index_sha256 TEXT NOT NULL, "
    "queue_sha256 TEXT NOT NULL, payload TEXT NOT NULL) STRICT",
    "CREATE INDEX generation_inputs_job ON generation_inputs(job_id)",
    "CREATE INDEX generation_inputs_content ON generation_inputs(catalog_story_index_sha256)",
)
_PENDING_SCHEMA = (
    "CREATE TABLE input_publications (directory TEXT PRIMARY KEY REFERENCES "
    "generation_inputs(directory) ON DELETE CASCADE, staging TEXT NOT NULL) STRICT"
)
_SCHEMA = (*_SCHEMA, _PENDING_SCHEMA)


class PreparationCatalogError(ValueError):
    """The local catalog is unsafe, unsupported, or invalid."""


def _verify_catalog(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    objects = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if application != _APPLICATION_ID:
        raise PreparationCatalogError("Not a VNTTS preparation catalog")
    if version != _VERSION:
        raise PreparationCatalogError(
            f"Unsupported preparation catalog schema {version} (expected {_VERSION}). "
            "Existing recordings have not been changed. Open it with a compatible "
            "version, or back up the complete preparation directory, including "
            "generated audio, before starting with an empty directory."
        )
    if {row["sql"] for row in objects} != set(_SCHEMA):
        raise PreparationCatalogError("Preparation catalog schema changed")


class PreparationCatalog:
    def __init__(
        self,
        root: Path,
        *,
        filename: str = "preparation.sqlite3",
        owned_root: bool = False,
    ) -> None:
        self.path = root / filename
        self.owned_root = owned_root

    @property
    def required_marker(self) -> Path:
        return self.path.with_name(self.path.name + ".required")

    @property
    def initialization_lock(self) -> Path:
        return self.path.with_name(f".{self.path.name}.guard")

    def _check_missing_catalog(self) -> None:
        if (
            self.owned_root
            and not self.path.exists()
            and self.path.parent.is_dir()
            and any(
                path != self.initialization_lock
                and not path.name.startswith(".preparation-")
                for path in self.path.parent.iterdir()
            )
        ):
            if self.required_marker.exists():
                raise PreparationCatalogError(
                    "Previously initialized preparation catalog is missing; restore "
                    "preparation.sqlite3 from its verified backup. Back up the complete "
                    "preparation directory before starting with an empty directory."
                )
            raise PreparationCatalogError(
                "Preparation catalog is missing from an existing jobs directory; "
                "restore its catalog or back up the complete preparation directory, "
                "including generated audio, before starting with an empty directory."
            )

    def _initialize(self) -> None:
        temporary = None
        try:
            with NamedTemporaryFile(
                dir=self.path.parent,
                prefix=".preparation-",
                suffix=".sqlite3",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
            with closing(sqlite3.connect(temporary)) as connection:
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {_VERSION}")
                connection.commit()
            _sync_regular_file(temporary)
            try:
                os.link(temporary, self.path)
            except FileExistsError:
                pass
            with self.required_marker.open("a", encoding="utf-8") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(self.path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _ensure_initialized(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            return
        if self.owned_root:
            with exclusive_advisory_lock(self.initialization_lock, blocking=True):
                if not self.path.exists():
                    self._check_missing_catalog()
                    self._initialize()
        else:
            self._initialize()

    @contextmanager
    def _connection(
        self, *, write: bool = False
    ) -> Iterator[sqlite3.Connection | None]:
        connection = None
        try:
            for path in (
                self.path.parent,
                self.path,
                self.required_marker,
                self.path.with_name(self.path.name + "-journal"),
                self.path.with_name(self.path.name + "-wal"),
                self.path.with_name(self.path.name + "-shm"),
            ):
                if path.is_symlink() or path.is_junction():
                    raise PreparationCatalogError(
                        "Preparation catalog must not be a link"
                    )
            existed = self.path.exists()
            if not existed and not write:
                self._check_missing_catalog()
                existed = self.path.exists()
            if not write and not existed:
                yield None
                return
            if write:
                self._ensure_initialized()
            mode = "rw" if write else "ro"
            connection = sqlite3.connect(
                self.path.absolute().as_uri() + f"?mode={mode}", uri=True, timeout=30
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            _verify_catalog(connection)
            yield connection
            if write:
                connection.commit()
        except PreparationCatalogError as error:
            raise PreparationCatalogError(f"{error}\nCatalog: {self.path}") from error
        except (sqlite3.Error, OSError) as error:
            raise PreparationCatalogError(
                f"Unable to use preparation catalog {self.path}: {error}"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    def initialize(self) -> None:
        with self._connection(write=True):
            pass

    def load_job(self, job_id: str) -> dict[str, object] | None:
        with self._connection() as connection:
            if connection is None:
                return None
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return None if row is None else self._job_document(connection, row)

    def jobs(self, checksum: str | None = None) -> tuple[dict[str, object], ...]:
        with self._connection() as connection:
            if connection is None:
                return ()
            rows = connection.execute(
                "SELECT * FROM jobs"
                + (" WHERE catalog_story_index_sha256 = ?" if checksum else "")
                + " ORDER BY updated_at, job_id",
                () if checksum is None else (checksum,),
            ).fetchall()
            return tuple(self._job_document(connection, row) for row in rows)

    @staticmethod
    def _job_document(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, object]:
        if row["catalog_story_index_sha256"] != (
            row["source_story_index_sha256"] or row["story_index_sha256"]
        ):
            raise PreparationCatalogError(
                "Preparation catalog content identity changed"
            )
        document = {
            name: row[name]
            for name in _JOB_FIELDS
            if name != "catalog_story_index_sha256" and row[name] is not None
        }
        document["game_version"] = row["game_version"]
        document["estimate"] = {name: row[name] for name in _ESTIMATE_FIELDS}
        for table, field, output in (
            ("job_stories", "story_id", "selected_story_ids"),
            ("job_lines", "line_id", "selected_line_ids"),
        ):
            document[output] = [
                value[0]
                for value in connection.execute(
                    f"SELECT {field} FROM {table} WHERE job_id = ? ORDER BY ordinal",
                    (row["job_id"],),
                )
            ]
        return document

    def save_job(self, document: Mapping[str, object], *, replace: bool = True) -> None:
        fields = (*_JOB_FIELDS, *_ESTIMATE_FIELDS)
        estimate = document["estimate"]
        assert isinstance(estimate, Mapping)
        values = [document.get(name) for name in _JOB_FIELDS[:-1]]
        values.append(
            document.get("source_story_index_sha256") or document["story_index_sha256"]
        )
        values.extend(estimate[name] for name in _ESTIMATE_FIELDS)
        with self._connection(write=True) as connection:
            assert connection is not None
            cursor = connection.execute(
                f"INSERT INTO jobs ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)}) "
                + (
                    "ON CONFLICT(job_id) DO UPDATE SET "
                    + ", ".join(
                        f"{name} = excluded.{name}"
                        for name in fields
                        if name != "job_id"
                    )
                    if replace
                    else "ON CONFLICT(job_id) DO NOTHING"
                ),
                values,
            )
            if not cursor.rowcount:
                return
            for table, field, source in (
                ("job_stories", "story_id", "selected_story_ids"),
                ("job_lines", "line_id", "selected_line_ids"),
            ):
                selected = document[source]
                if not isinstance(selected, (tuple, list)):
                    raise PreparationCatalogError(
                        "Preparation selections must be ordered lists"
                    )
                connection.execute(
                    f"DELETE FROM {table} WHERE job_id = ?", (document["job_id"],)
                )
                connection.executemany(
                    f"INSERT INTO {table} (job_id, ordinal, {field}) VALUES (?, ?, ?)",
                    (
                        (document["job_id"], ordinal, value)
                        for ordinal, value in enumerate(selected)
                    ),
                )

    def selection(self, checksum: str) -> tuple[str, ...] | None:
        with self._connection() as connection:
            if (
                connection is None
                or connection.execute(
                    "SELECT 1 FROM content_selections WHERE catalog_story_index_sha256 = ?",
                    (checksum,),
                ).fetchone()
                is None
            ):
                return None
            return tuple(
                row[0]
                for row in connection.execute(
                    "SELECT story_id FROM content_selection_stories "
                    "WHERE catalog_story_index_sha256 = ? ORDER BY ordinal",
                    (checksum,),
                )
            )

    def save_selection(self, checksum: str, stories: Sequence[str]) -> None:
        with self._connection(write=True) as connection:
            assert connection is not None
            connection.execute(
                "INSERT INTO content_selections VALUES (?) ON CONFLICT DO NOTHING",
                (checksum,),
            )
            connection.execute(
                "DELETE FROM content_selection_stories WHERE catalog_story_index_sha256 = ?",
                (checksum,),
            )
            connection.executemany(
                "INSERT INTO content_selection_stories VALUES (?, ?, ?)",
                ((checksum, ordinal, story) for ordinal, story in enumerate(stories)),
            )

    def save_plan(
        self,
        document: Mapping[str, object],
        schema_version: int,
    ) -> None:
        groups = document.get("groups")
        suggestions = document.get("person_link_suggestions", [])
        if not isinstance(groups, list) or not isinstance(suggestions, list):
            raise PreparationCatalogError(
                "Voice plan groups and suggestions must be lists"
            )
        values = [document.get(name) for name in _PLAN_FIELDS]
        for field in ("pocket_voice_cloning", "xtts_terms_accepted"):
            value = document.get(field, False)
            if type(value) is not bool:
                raise PreparationCatalogError(
                    "Voice plan cloning and terms flags must be boolean"
                )
            values[_PLAN_FIELDS.index(field)] = int(value)
        fields = (*_PLAN_FIELDS, "schema_version")
        with self._connection(write=True) as connection:
            assert connection is not None
            connection.execute(
                f"INSERT INTO voice_plans ({', '.join(fields)}) "
                f"VALUES ({', '.join('?' for _ in fields)}) "
                "ON CONFLICT(job_id) DO UPDATE SET "
                + ", ".join(
                    f"{name} = excluded.{name}" for name in fields if name != "job_id"
                ),
                (*values, schema_version),
            )
            connection.execute(
                "DELETE FROM voice_groups WHERE job_id = ?", (document["job_id"],)
            )
            for ordinal, group in enumerate(groups):
                if not isinstance(group, dict):
                    raise PreparationCatalogError("Voice plan group must be an object")
                connection.execute(
                    "INSERT INTO voice_groups VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        document["job_id"],
                        ordinal,
                        group.get("group_id"),
                        group.get("character"),
                        group.get("source_id"),
                        group.get("control_sha256"),
                        group.get("route"),
                        json.dumps(
                            group, ensure_ascii=False, allow_nan=False, sort_keys=True
                        ),
                    ),
                )
            connection.execute(
                "DELETE FROM voice_plan_suggestions WHERE job_id = ?",
                (document["job_id"],),
            )
            connection.executemany(
                "INSERT INTO voice_plan_suggestions VALUES (?, ?, ?)",
                (
                    (
                        document["job_id"],
                        ordinal,
                        json.dumps(value, ensure_ascii=False, allow_nan=False),
                    )
                    for ordinal, value in enumerate(suggestions)
                ),
            )

    def plan_documents(self) -> tuple[dict[str, object], ...]:
        with self._connection() as connection:
            if connection is None:
                return ()
            documents = []
            for row in connection.execute("SELECT * FROM voice_plans ORDER BY job_id"):
                document: dict[str, object] = {name: row[name] for name in _PLAN_FIELDS}
                document["schema_version"] = row["schema_version"]
                for field in ("pocket_voice_cloning", "xtts_terms_accepted"):
                    document[field] = bool(document[field])
                for table, field in (
                    ("voice_groups", "groups"),
                    ("voice_plan_suggestions", "person_link_suggestions"),
                ):
                    document[field] = [
                        self._payload(value[0])
                        for value in connection.execute(
                            f"SELECT payload FROM {table} WHERE job_id = ? ORDER BY ordinal",
                            (row["job_id"],),
                        )
                    ]
                documents.append(document)
            return tuple(documents)

    @staticmethod
    def _payload(value: str) -> dict[str, object]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise PreparationCatalogError(
                "Voice preparation payload is invalid JSON"
            ) from error
        if not isinstance(payload, dict) or any(
            not isinstance(key, str) for key in payload
        ):
            raise PreparationCatalogError("Voice preparation payload must be an object")
        return payload

    def has_plan(self, job_id: str) -> bool:
        with self._connection() as connection:
            return (
                connection is not None
                and connection.execute(
                    "SELECT 1 FROM voice_plans WHERE job_id = ?", (job_id,)
                ).fetchone()
                is not None
            )

    def decision(self, decision_key: str) -> str | None:
        with self._connection() as connection:
            if connection is None:
                return None
            row = connection.execute(
                "SELECT source_id FROM voice_decisions WHERE decision_key = ?",
                (decision_key,),
            ).fetchone()
            return None if row is None else str(row[0])

    def save_decisions(self, decisions: Mapping[str, Mapping[str, object]]) -> None:
        with self._connection(write=True) as connection:
            assert connection is not None
            connection.executemany(
                "INSERT INTO voice_decisions VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(decision_key) DO UPDATE SET source_id = excluded.source_id, "
                "decided_at = excluded.decided_at",
                (
                    (
                        key,
                        value["group_id"],
                        value["decision_context_sha256"],
                        value["source_id"],
                        value["decided_at"],
                    )
                    for key, value in decisions.items()
                ),
            )

    def save_input(
        self,
        job_id: str,
        document: Mapping[str, object],
        directory: Path,
        *,
        staging: Path | None = None,
    ) -> None:
        if document.get("job_id", job_id) != job_id:
            raise PreparationCatalogError("Generation input belongs to a different job")
        payload = json.dumps(
            document, ensure_ascii=False, allow_nan=False, sort_keys=True
        )
        with self._connection(write=True) as connection:
            assert connection is not None
            job = connection.execute(
                "SELECT catalog_story_index_sha256 FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            content_hash = (
                job[0]
                if job is not None
                else document.get("catalog_story_index_sha256")
                or document.get("source_story_index_sha256")
            )
            values = (
                document.get("identity"),
                job_id,
                str(directory.expanduser().resolve()),
                content_hash,
                document.get("queue_sha256"),
                payload,
            )
            connection.execute(
                "INSERT INTO generation_inputs VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                values,
            )
            existing = connection.execute(
                "SELECT * FROM generation_inputs WHERE identity = ?", (values[0],)
            ).fetchone()
            if existing is None or tuple(existing) != values:
                raise PreparationCatalogError(
                    "Saved generation input identity conflicts with its catalog"
                )
            if staging is not None:
                connection.execute(
                    "INSERT INTO input_publications VALUES (?, ?) "
                    "ON CONFLICT(directory) DO NOTHING",
                    (values[2], str(staging.expanduser().resolve())),
                )
            else:
                connection.execute(
                    "DELETE FROM input_publications WHERE directory = ?", (values[2],)
                )

    def input_staging(self, directory: Path) -> Path | None:
        with self._connection() as connection:
            if connection is None:
                return None
            row = connection.execute(
                "SELECT staging FROM input_publications WHERE directory = ?",
                (str(directory.expanduser().resolve()),),
            ).fetchone()
            return None if row is None else Path(row[0])

    def discard_unpublished_input(self, directory: Path) -> None:
        with self._connection(write=True) as connection:
            assert connection is not None
            connection.execute(
                "DELETE FROM generation_inputs WHERE directory = ? AND directory IN "
                "(SELECT directory FROM input_publications)",
                (str(directory.expanduser().resolve()),),
            )

    def input_document(self, directory: Path) -> dict[str, object] | None:
        with self._connection() as connection:
            if connection is None:
                return None
            row = connection.execute(
                "SELECT identity, job_id, queue_sha256, payload FROM generation_inputs WHERE directory = ?",
                (str(directory.expanduser().resolve()),),
            ).fetchone()
            if row is None:
                return None
            document = self._payload(row["payload"])
            if (
                document.get("identity") != row["identity"]
                or document.get("queue_sha256") != row["queue_sha256"]
                or document.get("job_id", row["job_id"]) != row["job_id"]
            ):
                raise PreparationCatalogError(
                    "Saved generation input catalog identity changed"
                )
            return document

    def input_directories(self, content_hash: str | None = None) -> tuple[Path, ...]:
        with self._connection() as connection:
            if connection is None:
                return ()
            return tuple(
                Path(row[0])
                for row in connection.execute(
                    "SELECT directory FROM generation_inputs"
                    + (
                        " WHERE catalog_story_index_sha256 = ?"
                        if content_hash is not None
                        else ""
                    )
                    + " ORDER BY directory",
                    () if content_hash is None else (content_hash,),
                )
            )
