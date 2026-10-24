"""Explicit, recoverable upgrade; never imported by ordinary storage readers."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Sequence
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from vntts.audio_store import (
    _CATALOG_SCHEMA,
    SharedAudioStore,
    _read_wav,
    _sync_directory,
    _sync_regular_file,
)
from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.authoring.generation_lease import (
    GenerationLease,
    process_is_alive,
    process_started_at,
)
from vntts.authoring.generation_state import (
    load_generation_state_from_snapshot,
    load_stable_generation_queue,
)
from vntts.document_identity import (
    is_lowercase_sha256,
    sha256_file,
)
from vntts.generation_database import (
    read_state,
    validate_database_path,
    write_state,
)
from vntts.path_safety import copy_regular_file, read_regular_bytes
from vntts.pregeneration_queue import _validate_input_publication
from vntts.pregeneration_setup import PregenerationJob, job_schema_version
from vntts.pregeneration_voices import _decision_key, voice_plan_schema_version
from vntts.preparation_catalog import PreparationCatalog


class StorageUpgradeError(ValueError):
    """An upgrade cannot preserve the existing authority without more evidence."""


class _Upgrade:
    def __init__(
        self,
        root: Path,
        backup: Path,
        *,
        apply: bool,
        workspace_roots: Sequence[Path] = (),
    ) -> None:
        self.root = root.resolve()
        self.backup_root = backup.resolve()
        self.apply = apply
        self.roots = (self.root, *(path.absolute() for path in workspace_roots))
        self.publications: dict[Path, tuple[bytes, str]] = {}
        self.sources: dict[Path, str] = {}
        self.counts: dict[str, int] = {}
        self.retirements: list[Path] = []
        self.leases: dict[Path, str] = {}
        self.owned_leases: dict[Path, GenerationLease] = {}

    def safe_path(self, path: Path) -> None:
        if not any(path.is_relative_to(root) for root in self.roots):
            raise StorageUpgradeError(f"Path is outside explicit upgrade roots: {path}")
        for candidate in (path, *path.parents):
            if candidate.is_symlink() or candidate.is_junction():
                raise StorageUpgradeError(
                    f"Upgrade paths must not be links: {candidate}"
                )

    def capture(self, path: Path) -> bytes:
        self.safe_path(path)
        if not path.is_file():
            raise StorageUpgradeError(f"Upgrade input must be a regular file: {path}")
        if path.suffix == ".sqlite3":
            _check_sqlite_source(path)
        payload = read_regular_bytes(path)
        digest = hashlib.sha256(payload).hexdigest()
        expected = self.sources.setdefault(path, digest)
        if digest != expected or sha256_file(path) != digest:
            raise StorageUpgradeError(f"Upgrade source changed: {path}")
        return payload

    def document(self, path: Path, version: int) -> dict[str, object]:
        value = json.loads(self.capture(path))
        if (
            not isinstance(value, dict)
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != version
        ):
            raise StorageUpgradeError(f"Unsupported upgrade document: {path}")
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return value

    def counted(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def retire(self, paths: Sequence[Path]) -> None:
        self.retirements.extend(paths)

    def verify_sources(self) -> None:
        for path, expected in self.sources.items():
            self.safe_path(path)
            if sha256_file(path) != expected:
                raise StorageUpgradeError(f"Upgrade source changed: {path}")

    def check_backups(self) -> None:
        for path, digest in self.sources.items():
            target = (
                self.backup_root / "revisions" / digest / _backup_identity(self, path)
            )
            _safe_backup_path(self.backup_root, target)
            if target.exists() and (
                not target.is_file() or sha256_file(target) != digest
            ):
                raise StorageUpgradeError(f"Backup verification failed: {target}")

    def backup_sources(self) -> None:
        self.verify_sources()
        self.check_backups()
        for path, digest in self.sources.items():
            target = (
                self.backup_root / "revisions" / digest / _backup_identity(self, path)
            )
            _safe_backup_path(self.backup_root, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if sha256_file(target) != digest:
                    raise StorageUpgradeError(f"Backup verification failed: {target}")
                continue
            copy_regular_file(path, target)
            _sync_regular_file(target)
            _sync_directory(target.parent)
            if sha256_file(target) != digest:
                raise StorageUpgradeError(f"Backup verification failed: {target}")
        self.verify_sources()

    def stage(self, target: Path, payload: bytes, kind: str) -> None:
        self.safe_path(target)
        if target.exists():
            self.capture(target)
        self.publications[target] = (payload, kind)

    def finish(self) -> None:
        excluded = set(self.publications) | {
            lease.path for lease in self.owned_leases.values()
        }
        for path, digest in self.sources.items():
            if path not in excluded:
                self.safe_path(path)
                if sha256_file(path) != digest:
                    raise StorageUpgradeError(
                        f"Upgrade source changed before retirement: {path}"
                    )
        paths = tuple(dict.fromkeys(self.retirements))
        for path in paths:
            if sha256_file(path) != self.sources[path]:
                raise StorageUpgradeError(
                    f"Upgrade source changed before retirement: {path}"
                )
        for path in paths:
            path.unlink()
            _sync_directory(path.parent)


def _safe_backup_path(root: Path, path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise StorageUpgradeError(f"Backup paths must not be links: {candidate}")
        if candidate != path and candidate.exists() and not candidate.is_dir():
            raise StorageUpgradeError(
                f"Backup ancestor must be a directory: {candidate}"
            )


def _check_sqlite_sidecars(path: Path) -> None:
    for candidate in (
        path,
        *(
            path.with_name(path.name + suffix)
            for suffix in ("-wal", "-shm", "-journal")
        ),
    ):
        if candidate.is_symlink() or candidate.is_junction():
            raise StorageUpgradeError(f"SQLite path must not be a link: {candidate}")
        if candidate.exists() and not stat.S_ISREG(
            candidate.stat(follow_symlinks=False).st_mode
        ):
            raise StorageUpgradeError(
                f"SQLite input must be a regular file: {candidate}"
            )
        if candidate != path and candidate.exists():
            raise StorageUpgradeError(
                f"SQLite sidecar requires recovery before upgrade: {candidate}"
            )


def _check_sqlite_source(path: Path) -> None:
    _check_sqlite_sidecars(path)
    payload = read_regular_bytes(path)
    if (
        len(payload) < 100
        or not payload.startswith(b"SQLite format 3\x00")
        or payload[18:20] != b"\x01\x01"
    ):
        raise StorageUpgradeError(
            f"SQLite input requires rollback-journal recovery: {path}"
        )
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.deserialize(payload)
        connection.execute("PRAGMA trusted_schema=OFF")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise StorageUpgradeError(f"SQLite integrity verification failed: {path}")


def _existing_audio_catalog(
    upgrade: _Upgrade, store: SharedAudioStore
) -> tuple[dict[str, str], dict[str, str]]:
    store._check_catalog_paths(store.catalog_path)
    bindings: dict[str, str] = {}
    unavailable: dict[str, str] = {}
    if not store.catalog_path.exists():
        return bindings, unavailable
    payload = upgrade.capture(store.catalog_path)
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.deserialize(payload)
        connection.execute("PRAGMA trusted_schema=OFF")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        objects = dict(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        )
        previous = {
            "bindings": _CATALOG_SCHEMA["bindings"],
            "legacy_errors": "CREATE TABLE legacy_errors (filename TEXT PRIMARY KEY NOT NULL, error TEXT NOT NULL)",
        }
        if objects != ({1: previous, 2: _CATALOG_SCHEMA}.get(version)):
            raise StorageUpgradeError(
                f"Unsupported audio catalog: {store.catalog_path}"
            )
        bindings.update(
            connection.execute("SELECT synthesis_key, audio_sha256 FROM bindings")
        )
        if version == 2:
            unavailable.update(
                connection.execute("SELECT synthesis_key, reason FROM unavailable_keys")
            )
        else:
            for filename, reason in connection.execute(
                "SELECT filename, error FROM legacy_errors"
            ):
                if (
                    not isinstance(filename, str)
                    or Path(filename).name != filename
                    or not filename.endswith(".json")
                ):
                    raise StorageUpgradeError("Invalid saved audio error identity")
                unavailable[Path(filename).stem] = reason
    for key, reason in unavailable.items():
        store._validate_key(key)
        if not isinstance(reason, str) or not reason.strip():
            raise StorageUpgradeError("Invalid unavailable audio evidence")
    return bindings, unavailable


def _audio_bindings(
    upgrade: _Upgrade, store: SharedAudioStore, bindings: dict[str, str]
) -> list[Path]:
    originals = []
    upgrade.safe_path(store.directory / "bindings")
    for path in sorted((store.directory / "bindings").glob("*.json")):
        document = upgrade.document(path, 1)
        store._validate_key(path.stem)
        digest = document.get("audio_sha256")
        if set(document) != {
            "schema_version",
            "audio_sha256",
        } or not is_lowercase_sha256(digest):
            raise StorageUpgradeError(f"Invalid audio binding: {path}")
        if path.stem in bindings and bindings[path.stem] != digest:
            raise StorageUpgradeError(f"Conflicting synthesis binding: {path}")
        assert isinstance(digest, str)
        bindings[path.stem] = digest
        originals.append(path)
        upgrade.counted("audio_bindings")
    return originals


def _audio_arrays(
    upgrade: _Upgrade,
    store: SharedAudioStore,
    bindings: dict[str, str],
    unavailable: dict[str, str],
) -> list[Path]:
    for path in sorted(store.directory.glob("*.npy")):
        store._validate_key(path.stem)
        upgrade.capture(path)
        if path.stem in bindings:
            upgrade.counted("preserved_bound_arrays")
        else:
            unavailable[path.stem] = (
                "Original samples remain in place and in the upgrade backup; verified model/sample-rate evidence is required before reuse"
            )
            upgrade.counted("preserved_unproven_arrays")
    return []


def _stage_audio_catalog(
    upgrade: _Upgrade,
    store: SharedAudioStore,
    bindings: dict[str, str],
    unavailable: dict[str, str],
) -> None:
    with TemporaryDirectory(prefix="vntts-audio-upgrade-") as directory:
        temporary = Path(directory) / "audio-catalog.sqlite3"
        with closing(sqlite3.connect(temporary)) as connection:
            for statement in _CATALOG_SCHEMA.values():
                connection.execute(statement)
            connection.execute("PRAGMA user_version=2")
            connection.executemany(
                "INSERT INTO bindings VALUES (?, ?)", sorted(bindings.items())
            )
            connection.executemany(
                "INSERT INTO unavailable_keys VALUES (?, ?)",
                sorted(unavailable.items()),
            )
            connection.commit()
        staged = SharedAudioStore(Path(directory))
        if dict(staged.iter_bindings()) != bindings:
            raise StorageUpgradeError("Staged audio bindings differ")
        upgrade.stage(store.catalog_path, temporary.read_bytes(), "audio")


def _assert_unchanged_target(upgrade: _Upgrade, path: Path) -> None:
    upgrade.safe_path(path)
    if path.suffix == ".sqlite3":
        _check_sqlite_sidecars(path)
        if path.exists():
            _check_sqlite_source(path)
    expected = upgrade.sources.get(path)
    if (path.exists() and expected is None) or (
        expected is not None and sha256_file(path) != expected
    ):
        raise StorageUpgradeError(f"SQLite target changed during upgrade: {path}")


def _audio_catalog(upgrade: _Upgrade, directory: Path) -> None:
    upgrade.safe_path(directory)
    store = SharedAudioStore(directory)
    bindings, unavailable = _existing_audio_catalog(upgrade, store)
    originals = _audio_bindings(upgrade, store, bindings)
    if (
        not store.catalog_path.exists()
        and (directory / "objects").exists()
        and not bindings
    ):
        raise StorageUpgradeError(
            f"Audio key inventory is missing; restore its catalog: {directory}"
        )
    for key, digest in bindings.items():
        store._validate_key(key)
        if not is_lowercase_sha256(digest):
            raise StorageUpgradeError("Invalid audio binding digest")
        object_path = directory / "objects" / f"{digest}.wav"
        upgrade.safe_path(object_path)
        _read_wav(object_path, digest)
        upgrade.capture(object_path)
        unavailable.pop(key, None)
    originals.extend(_audio_arrays(upgrade, store, bindings, unavailable))
    _stage_audio_catalog(upgrade, store, bindings, unavailable)
    upgrade.retire(originals)


def _check_generation_lease(upgrade: _Upgrade, output: Path) -> None:
    path = output / ".generation-lease.json"
    if not path.exists() and not path.is_symlink():
        return
    document = upgrade.document(path, 1)
    if document.get("schema") != "vntts.authoring-generation-lease":
        raise StorageUpgradeError("Unknown generation lease blocks upgrade")
    if document.get("hostname") not in {None, socket.gethostname()}:
        raise StorageUpgradeError("Foreign generation lease blocks upgrade")
    pid = document.get("pid")
    if type(pid) is not int or pid <= 0:
        raise StorageUpgradeError("Invalid generation lease blocks upgrade")
    if process_is_alive(pid):
        started = document.get("process_started_at")
        if started is None or process_started_at(pid) in {None, started}:
            raise StorageUpgradeError("Active generation process blocks upgrade")


def _normalize_cancellations(document: dict[str, object]) -> dict[str, object]:
    normalized = copy.deepcopy(document)
    items = normalized.get("items")
    if not isinstance(items, dict):
        raise StorageUpgradeError("Generation items must be an object")
    for result in items.values():
        if not isinstance(result, dict) or result.get("status") != "failed":
            continue
        failure = result.get("failure")
        if (
            not isinstance(failure, dict)
            or failure.get("kind") != "cancelled"
            or failure.get("attempt_counted") is False
        ):
            continue
        if normalized.get("active") is not None:
            raise StorageUpgradeError(
                "Counted cancellation with an active attempt needs explicit recovery"
            )
        if (
            "attempt_counted" in failure
            and type(failure["attempt_counted"]) is not bool
        ):
            raise StorageUpgradeError("Cancellation attempt marker is invalid")
        attempts = result.get("attempts", 0)
        if type(attempts) is not int or attempts < 0:
            raise StorageUpgradeError("Cancellation attempts are invalid")
        counts = _normalized_provider_counts(result, attempts)
        result["attempts"] = max(0, attempts - 1)
        if "attempts_by_provider" in result:
            result["attempts_by_provider"] = counts
        failure["attempt_counted"] = False
    return normalized


def _validate_generation_document(
    upgrade: _Upgrade, target: Path, document: dict[str, object], queue_path: Path
) -> tuple[dict[str, object], str]:
    upgrade.capture(queue_path)
    queue, checksum = load_stable_generation_queue(queue_path)
    validate_database_path(target)
    normalized = _normalize_cancellations(document)
    load_generation_state_from_snapshot(
        target, queue, checksum, state_document=normalized
    )
    _check_generation_lease(upgrade, target.parent)
    upgrade.leases[target.parent] = checksum
    items = normalized.get("items")
    assert isinstance(items, dict)
    for result in items.values():
        if isinstance(result, dict) and result.get("status") in {
            "generated",
            "approved",
        }:
            upgrade.capture(target.parent / result["path"])
    return normalized, checksum


def _stage_generation_state(
    upgrade: _Upgrade, target: Path, document: dict[str, object], checksum: str
) -> None:
    if target.exists() and read_state(target) == document:
        return
    with TemporaryDirectory(prefix="vntts-generation-upgrade-") as directory:
        staged = Path(directory) / target.name
        write_state(staged, document)
        if read_state(staged) != document:
            raise StorageUpgradeError("Generation state readback differs")
        upgrade.stage(target, staged.read_bytes(), "generation")


def _generation_state(
    upgrade: _Upgrade, path: Path, queue_path: Path, *, preserve_source: bool = False
) -> None:
    document = json.loads(upgrade.capture(path))
    if not isinstance(document, dict):
        raise StorageUpgradeError(f"Generation state must be an object: {path}")
    target = path.with_suffix(".sqlite3")
    normalized, checksum = _validate_generation_document(
        upgrade, target, document, queue_path
    )
    if target.exists():
        upgrade.capture(target)
        if _normalize_cancellations(read_state(target)) != normalized:
            raise StorageUpgradeError(f"SQLite state conflicts with source: {path}")
    _stage_generation_state(upgrade, target, normalized, checksum)
    if not preserve_source:
        upgrade.retire([path])
    upgrade.counted("generation_states")


def _generation_sqlite_state(upgrade: _Upgrade, target: Path, queue_path: Path) -> None:
    upgrade.capture(target)
    document = read_state(target)
    normalized, checksum = _validate_generation_document(
        upgrade, target, document, queue_path
    )
    _stage_generation_state(upgrade, target, normalized, checksum)
    if document != normalized:
        upgrade.counted("normalized_sqlite_generation_states")


def _decisions(upgrade: _Upgrade, path: Path, catalog: PreparationCatalog) -> None:
    document = upgrade.document(path, 1)
    values = document.get("decisions")
    if not isinstance(values, dict):
        raise StorageUpgradeError("Voice decisions must be an object")
    for key, value in values.items():
        if (
            not isinstance(value, dict)
            or not is_lowercase_sha256(key)
            or not is_lowercase_sha256(value.get("decision_context_sha256"))
        ):
            raise StorageUpgradeError("Invalid voice decision")
        for field in ("group_id", "source_id", "decided_at"):
            if not isinstance(value.get(field), str) or not value[field].strip():
                raise StorageUpgradeError("Invalid voice decision identity")
        if _decision_key(value["group_id"], value["decision_context_sha256"]) != key:
            raise StorageUpgradeError("Voice decision key changed")
        existing = catalog.decision(key)
        if existing is not None and existing != value["source_id"]:
            raise StorageUpgradeError("Voice decision conflicts with SQLite")
    if upgrade.apply:
        catalog.save_decisions(values, replace=False)
        if any(
            catalog.decision(key) != value["source_id"] for key, value in values.items()
        ):
            raise StorageUpgradeError("Voice decision readback differs")
    upgrade.counted("voice_decisions")


def _job(upgrade: _Upgrade, path: Path, catalog: PreparationCatalog) -> None:
    document = upgrade.document(path, job_schema_version)
    job = PregenerationJob.from_document(document)
    canonical = job.to_document()
    if (
        job.job_id != path.parent.name
        or {k: v for k, v in document.items() if k != "schema_version"} != canonical
    ):
        raise StorageUpgradeError("Job directory or document identity changed")
    catalog.save_job(canonical, replace=False)
    if catalog.load_job(job.job_id) != canonical:
        raise StorageUpgradeError("Job conflicts with SQLite")
    upgrade.counted("jobs")


def _selection(upgrade: _Upgrade, path: Path, catalog: PreparationCatalog) -> None:
    document = upgrade.document(path, 1)
    checksum, selected = (
        document.get("story_index_sha256"),
        document.get("selected_story_ids"),
    )
    if (
        set(document) != {"schema_version", "story_index_sha256", "selected_story_ids"}
        or not is_lowercase_sha256(checksum)
        or path.stem != checksum
    ):
        raise StorageUpgradeError("Invalid saved story selection")
    if (
        not isinstance(selected, list)
        or any(not isinstance(value, str) or not value.strip() for value in selected)
        or len(set(selected)) != len(selected)
    ):
        raise StorageUpgradeError("Invalid saved story selection")
    catalog.save_selection(checksum, selected, replace=False)
    if catalog.selection(checksum) != tuple(selected):
        raise StorageUpgradeError("Story selection conflicts with SQLite")
    upgrade.counted("selections")


def _voice_plan(upgrade: _Upgrade, path: Path, catalog: PreparationCatalog) -> None:
    document = upgrade.document(path, voice_plan_schema_version)
    if document.get("job_id") != path.parent.name:
        raise StorageUpgradeError("Voice plan directory identity changed")
    canonical = dict(document)
    for key, default in (
        ("pocket_voice_cloning", False),
        ("xtts_terms_accepted", False),
        ("person_link_suggestions", []),
    ):
        canonical.setdefault(key, default)
    catalog.save_plan(canonical, voice_plan_schema_version, replace=False)
    saved = next(
        (
            value
            for value in catalog.plan_documents()
            if value["job_id"] == document["job_id"]
        ),
        None,
    )
    if saved != canonical:
        raise StorageUpgradeError(
            "Voice plan conflicts with SQLite or has unknown fields"
        )
    upgrade.counted("voice_plans")


def _generation_input(
    upgrade: _Upgrade, path: Path, catalog: PreparationCatalog
) -> None:
    document = upgrade.document(path, 4)
    job_id = _validate_input_publication(path.parent, document)
    catalog.save_input(job_id, document, path.parent)
    if catalog.input_document(path.parent) != document:
        raise StorageUpgradeError("Input readback differs")
    for name in (
        "queue.jsonl",
        "voice-manifest.json",
        "story-index.jsonl",
        "source-audio-semantic-evidence.json",
    ):
        candidate = path.parent / name
        if candidate.exists():
            upgrade.capture(candidate)
    upgrade.counted("generation_inputs")


def _preparation_documents(
    upgrade: _Upgrade, jobs: Path, catalog: PreparationCatalog
) -> None:
    sources = (
        ("*/job.json", _job),
        ("selections/*.json", _selection),
        ("*/voice-plan.json", _voice_plan),
        ("*/generation-input-*/input.json", _generation_input),
    )
    for pattern, handler in sources:
        for path in sorted(jobs.glob(pattern)):
            handler(upgrade, path, catalog)
            upgrade.retire([path])
    decisions = jobs.parent / "voice-decisions.json"
    if decisions.exists():
        _decisions(upgrade, decisions, catalog)
        upgrade.retire([decisions])


def _check_preparation_inventory(jobs: Path) -> None:
    if not jobs.exists():
        return
    for path in jobs.iterdir():
        if not path.is_dir() or path.name in {
            "audio-store",
            "synthesis-cache",
            "selections",
            "story-snapshots",
        }:
            continue
        if not (path / "job.json").is_file():
            raise StorageUpgradeError(
                "Job authority is missing; restore the preparation catalog before upgrading"
            )


def _preparation(upgrade: _Upgrade) -> None:
    jobs = upgrade.root / "pregeneration" / "jobs"
    upgrade.safe_path(jobs)
    catalog = PreparationCatalog(jobs)
    if not catalog.path.exists() and catalog.required_marker.exists():
        raise StorageUpgradeError(
            "The preparation catalog was lost; restore its backup before upgrading"
        )
    payload = upgrade.capture(catalog.path) if catalog.path.exists() else None
    if payload is None:
        _check_preparation_inventory(jobs)
    with TemporaryDirectory(prefix="vntts-preparation-upgrade-") as directory:
        staged = PreparationCatalog(Path(directory))
        if payload is not None:
            staged.path.write_bytes(payload)
        staged.begin_upgrade()
        _preparation_documents(upgrade, jobs, staged)
        staged.complete_upgrade()
        _readback_digest(staged.path, "preparation")
        upgrade.stage(catalog.path, staged.path.read_bytes(), "preparation")
        upgrade.stage(catalog.required_marker, b"", "marker")
    for path in sorted(jobs.glob("*/generation-output-*/generation-state.json")):
        source = path.parent.with_name(
            path.parent.name.replace("generation-output-", "generation-input-", 1)
        )
        _generation_state(upgrade, path, source / "queue.jsonl")
    for target in sorted(jobs.glob("*/generation-output-*/generation-state.sqlite3")):
        if target.with_suffix(".json").exists():
            continue
        source = target.parent.with_name(
            target.parent.name.replace("generation-output-", "generation-input-", 1)
        )
        _generation_sqlite_state(upgrade, target, source / "queue.jsonl")


def _backup_identity(upgrade: _Upgrade, path: Path) -> Path:
    if path.is_relative_to(upgrade.root):
        return path.relative_to(upgrade.root)
    root = next(root for root in upgrade.roots if path.is_relative_to(root))
    identity = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
    return Path("external-workspaces") / identity / path.relative_to(root)


def _normalized_provider_counts(
    result: dict[str, object], attempts: int
) -> dict[str, int]:
    values = result.get("attempts_by_provider")
    if values is None:
        return {}
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or type(value) is not int or value < 0
        for key, value in values.items()
    ):
        raise StorageUpgradeError("Cancellation provider counts are invalid")
    if sum(values.values()) > attempts:
        raise StorageUpgradeError("Cancellation provider counts exceed attempts")
    counts = dict(values)
    provider = result.get("provider")
    if isinstance(provider, str) and provider in counts:
        counts[provider] = max(0, counts[provider] - 1)
    elif attempts and counts:
        raise StorageUpgradeError(
            "Cancelled provider cannot be established from saved evidence"
        )
    return counts


def _workspace(upgrade: _Upgrade, root: Path) -> None:
    upgrade.safe_path(root)
    path = root / "generated-audio" / "generation-state.json"
    if path.exists() or path.is_symlink():
        _generation_state(upgrade, path, root / "queue.jsonl", preserve_source=True)
    elif path.with_suffix(".sqlite3").exists():
        _generation_sqlite_state(
            upgrade, path.with_suffix(".sqlite3"), root / "queue.jsonl"
        )


def _inventory(upgrade: _Upgrade) -> None:
    stores = (
        "audio-cache/pocket-tts",
        "audio-cache/moss-tts",
        "audio-cache/chatterbox-nano",
        "audio-cache/qwen-tts",
        "pregeneration/jobs/audio-store",
        "pregeneration/jobs/synthesis-cache/audio",
    )
    for relative in stores:
        store = upgrade.root / relative
        upgrade.safe_path(store)
        if store.exists():
            if not store.is_dir():
                raise StorageUpgradeError(f"Audio store is not a directory: {store}")
            _audio_catalog(upgrade, store)
    _preparation(upgrade)
    for root in upgrade.roots[1:]:
        _workspace(upgrade, root)


def _readback_digest(path: Path, kind: str) -> str:
    if kind == "generation":
        value = read_state(path)
    elif kind == "audio":
        store = SharedAudioStore(path.parent)
        bindings = dict(store.iter_bindings())
        for digest in bindings.values():
            _read_wav(store.directory / "objects" / f"{digest}.wav", digest)
        value = {"bindings": bindings}
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            value["unavailable"] = dict(
                connection.execute("SELECT synthesis_key, reason FROM unavailable_keys")
            )
    else:
        catalog = PreparationCatalog(path.parent)
        with catalog._connection() as connection:
            assert connection is not None
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            value = {
                table: [
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                ]
                for table in tables
            }
        catalog.jobs()
        catalog.plan_documents()
        for directory in catalog.input_directories():
            catalog.input_document(directory)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _fresh_readback(path: Path, kind: str, payload: bytes) -> None:
    digest = hashlib.sha256(payload).hexdigest()
    if sha256_file(path) != digest:
        raise StorageUpgradeError(f"Published storage bytes differ: {path}")
    if kind == "marker":
        return
    expected = _readback_digest(path, kind)
    code = "from pathlib import Path; from vntts.storage_upgrade import _readback_digest; import sys; print(_readback_digest(Path(sys.argv[1]), sys.argv[2]))"
    result = subprocess.run(
        [sys.executable, "-c", code, str(path), kind],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip() != expected or sha256_file(path) != digest:
        raise StorageUpgradeError(f"Fresh-process readback differs: {path}")


def _publish(upgrade: _Upgrade, target: Path, payload: bytes) -> None:
    _assert_unchanged_target(upgrade, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        dir=target.parent, prefix=".storage-upgrade-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        _assert_unchanged_target(upgrade, target)
        os.replace(temporary, target)
        _sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _apply(upgrade: _Upgrade) -> None:
    upgrade.backup_sources()
    with ExitStack() as stack:
        for output, checksum in upgrade.leases.items():
            lease = stack.enter_context(
                GenerationLease(output, checksum, process_checker=process_is_alive)
            )
            upgrade.owned_leases[output] = lease
        publications = sorted(
            upgrade.publications.items(),
            key=lambda item: {
                "audio": 0,
                "generation": 1,
                "preparation": 2,
                "marker": 3,
            }[item[1][1]],
        )
        for target, (payload, _) in publications:
            for lease in upgrade.owned_leases.values():
                lease.assert_owned()
            _publish(upgrade, target, payload)
        for target, (payload, kind) in upgrade.publications.items():
            _fresh_readback(target, kind, payload)
        upgrade.finish()
        for lease in upgrade.owned_leases.values():
            lease.mark_committed()


def upgrade_storage(
    root: Path,
    backup: Path,
    *,
    apply: bool = False,
    workspace_roots: Sequence[Path] = (),
) -> dict[str, object]:
    """Offline only: close the app and every worker before dry-run or apply."""
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise StorageUpgradeError("Data root must be an existing regular directory")
    if backup.absolute().is_relative_to(root.absolute()):
        raise StorageUpgradeError("Backup must be outside the active data root")
    _safe_backup_path(backup.absolute(), backup.absolute())
    upgrade = _Upgrade(root, backup, apply=False, workspace_roots=workspace_roots)
    _inventory(upgrade)
    upgrade.verify_sources()
    upgrade.check_backups()
    if apply:
        with exclusive_advisory_lock(root / ".storage-upgrade.guard", blocking=False):
            upgrade.verify_sources()
            _apply(upgrade)
    return {
        "schema_version": 1,
        "applied": apply,
        "data_root": str(root),
        "backup_root": str(backup),
        "backup": str(backup),
        "counts": upgrade.counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline storage upgrade. Close the app and all workers before running; dry-run is the default."
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--workspace",
        type=Path,
        action="append",
        default=[],
        help="Explicit authoring workspace root; repeat as needed",
    )
    arguments = parser.parse_args(argv)
    try:
        report = upgrade_storage(
            arguments.data_root,
            arguments.backup,
            apply=arguments.apply,
            workspace_roots=arguments.workspace,
        )
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
