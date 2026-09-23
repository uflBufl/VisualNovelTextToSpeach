"""Non-destructive preservation import for Reverse: 1999 listening sessions."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, TypeGuard

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.import_paths import default_import_root
from vntts.authoring.private_files import private_file_is_restricted
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.workspace_foundation import load_json_object, require_sha256
from vntts.path_safety import safe_relative_path

SESSION_SCHEMA = "r1999.model-listening-session"
KEY_SCHEMA = "r1999.model-listening-key"
REPORT_SCHEMA = "r1999.model-listening-report"
SCHEMA_VERSION = 1
IMPORT_SCHEMA = "vntts.authoring-listening-import"
IMPORT_SCHEMA_VERSION = 1
LEGACY_DIMENSIONS = ("timbre", "accent", "naturalness", "pronunciation")

PathInput = str | Path
JsonObject = dict[str, object]
Artifact = tuple[str, Path, Path, str]
SourceControl = tuple[Path, str]


class _ReportStats(TypedDict):
    model_id: str
    provider: str
    model: str
    wins: int
    losses: int
    ties: int
    reviewed_trials: int


class _PairwiseStats(TypedDict):
    trials: int
    left_wins: int
    right_wins: int
    ties: int


class ListeningImportError(RuntimeError):
    """A listening session cannot be preserved without ambiguity or data loss."""


@dataclass(frozen=True)
class ListeningImportInspection:
    session_directory: Path
    logical_identity: str
    source_fingerprint: str
    trial_count: int
    completed_count: int
    audio_count: int
    report_present: bool
    artifacts: tuple[tuple[str, Path, Path, str], ...]
    source_controls: tuple[tuple[Path, str], ...] = ()
    key_mode: int | None = 0o600


@dataclass(frozen=True)
class ListeningImportResult:
    destination: Path
    manifest: dict[str, object]
    created: bool


def inspect_listening_session(
    session_directory: PathInput,
) -> ListeningImportInspection:
    """Validate a complete legacy listening session without copying it."""
    root = Path(session_directory).expanduser().resolve()
    if not root.is_dir():
        raise ListeningImportError(f"Listening session is not a directory: {root}")
    session_path = root / "session.json"
    key_path = root / ".blind-key.json"
    report_path = root / "report.json"
    session, session_sha256 = _load_schema_snapshot(
        session_path, SESSION_SCHEMA, "listening session"
    )
    key, key_sha256 = _load_schema_snapshot(key_path, KEY_SCHEMA, "blind-listening key")
    if not private_file_is_restricted(key_path):
        raise ListeningImportError("Blind-listening key mode must be 0600")
    key_mode = None if sys.platform == "win32" else key_path.stat().st_mode & 0o777
    trials, audio = _validate_session(root, session)
    audio_sha256 = {relative: sha256_file(path) for relative, path in audio.items()}
    source_controls = _validate_key(
        session, key, key_path, key_sha256, trials, audio_sha256
    )
    report_sha256 = None
    if report_path.is_file():
        report, report_sha256 = _load_schema_snapshot(
            report_path, REPORT_SCHEMA, "listening report"
        )
        _validate_report(session_path, session, key, report)

    artifacts: list[Artifact] = [
        (
            "listening_session",
            session_path,
            Path("session.json"),
            session_sha256,
        ),
        (
            "blind_listening_key",
            key_path,
            Path(".blind-key.json"),
            key_sha256,
        ),
    ]
    if report_path.is_file():
        if report_sha256 is None:
            raise ListeningImportError("Listening report checksum is unavailable")
        artifacts.append(
            (
                "listening_report",
                report_path,
                Path("report.json"),
                report_sha256,
            )
        )
    for relative, source in sorted(audio.items(), key=lambda item: item[0].as_posix()):
        artifacts.append(("blind_audio", source, relative, audio_sha256[relative]))
    logical_payload = {
        "source_kind": session.get("source_kind"),
        "source_sha256": session.get("source_sha256"),
        "blind_key_sha256": session.get("blind_key_sha256"),
    }
    logical_identity = hashlib.sha256(_canonical(logical_payload)).hexdigest()
    fingerprint = hashlib.sha256(
        _canonical(
            [
                (
                    role,
                    destination.as_posix(),
                    digest,
                    key_mode if role == "blind_listening_key" else None,
                )
                for role, source, destination, digest in artifacts
            ]
        )
    ).hexdigest()
    inspection = ListeningImportInspection(
        session_directory=root,
        logical_identity=logical_identity,
        source_fingerprint=fingerprint,
        trial_count=len(trials),
        completed_count=_int_field(session, "completed_count"),
        audio_count=len(audio),
        report_present=report_path.is_file(),
        artifacts=tuple(artifacts),
        source_controls=source_controls,
        key_mode=key_mode,
    )
    _verify_controls_unchanged(inspection)
    return inspection


def import_listening_session(
    session_directory: PathInput, destination_root: PathInput | None = None
) -> ListeningImportResult:
    """Stage and atomically preserve one explicitly selected listening session."""
    inspection = inspect_listening_session(session_directory)
    destination_root = (
        Path(destination_root or default_import_root()).expanduser().resolve()
    )
    destination_root.mkdir(parents=True, exist_ok=True)
    import_id = f"listening-{inspection.logical_identity[:24]}"
    destination = destination_root / import_id
    manifest = _manifest(inspection, import_id)
    if destination.exists():
        return _validate_existing(destination, inspection)

    with staged_directory(destination_root, prefix=f".{import_id}-") as staging:
        for role, source, relative, digest in inspection.artifacts:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256_file(target) != digest:
                raise ListeningImportError(
                    f"Listening artifact changed during import: {source}"
                )
            if (
                role == "blind_listening_key"
                and inspection.key_mode is not None
                and ((target.stat().st_mode & 0o777) != inspection.key_mode)
            ):
                raise ListeningImportError(
                    f"Blind-listening key mode changed during import: {source}"
                )
        atomic_write_json(staging / "import.json", manifest, sort_keys=True)
        _verify_controls_unchanged(inspection)
        try:
            rename_directory_no_replace(staging, destination)
        except AtomicPublicationError, OSError:
            if destination.exists():
                return _validate_existing(destination, inspection)
            raise
    return ListeningImportResult(destination, manifest, True)


def _validate_session(
    root: Path, session: JsonObject
) -> tuple[list[JsonObject], dict[Path, Path]]:
    trials = _validate_session_header(session)
    trial_ids: set[str] = set()
    audio: dict[Path, Path] = {}
    validated_trials: list[JsonObject] = []
    completed = 0
    for index, trial in enumerate(trials):
        if not _is_json_object(trial):
            raise ListeningImportError(f"Listening trial {index} must be an object")
        completed += _validate_session_trial(root, trial, trial_ids, audio)
        validated_trials.append(trial)
    if session.get("completed_count") != completed:
        raise ListeningImportError("Listening session completed_count is inconsistent")
    return validated_trials, audio


def _validate_session_header(session: JsonObject) -> list[object]:
    source_kind = session.get("source_kind")
    if not isinstance(source_kind, str) or not source_kind.strip():
        raise ListeningImportError(
            "Listening session source_kind must be non-empty text"
        )
    _require_sha256(session.get("source_sha256"), "session source_sha256")
    _require_sha256(session.get("blind_key_sha256"), "session blind_key_sha256")
    trials = session.get("trials")
    if not isinstance(trials, list) or session.get("trial_count") != len(trials):
        raise ListeningImportError("Listening session trial count is inconsistent")
    if session.get("decision_mode") != "preference-only" and session.get(
        "dimensions"
    ) != list(LEGACY_DIMENSIONS):
        raise ListeningImportError("Listening session decision mode is unsupported")
    return trials


def _validate_session_trial(
    root: Path,
    trial: JsonObject,
    trial_ids: set[str],
    audio: dict[Path, Path],
) -> int:
    trial_id = trial.get("trial_id")
    if not isinstance(trial_id, str) or not trial_id or trial_id in trial_ids:
        raise ListeningImportError("Listening session trial IDs are invalid")
    trial_ids.add(trial_id)
    _validate_trial_rating(trial, trial_id)
    _validate_trial_audio(root, trial, trial_id, audio)
    return int(trial.get("rating") is not None)


def _validate_trial_rating(trial: JsonObject, trial_id: str) -> None:
    rating = trial.get("rating")
    if rating is not None and (
        not isinstance(rating, dict)
        or rating.get("preference") not in {"a", "b", "tie"}
    ):
        raise ListeningImportError(f"Listening trial {trial_id!r} rating is invalid")


def _validate_trial_audio(
    root: Path,
    trial: JsonObject,
    trial_id: str,
    audio: dict[Path, Path],
) -> None:
    sides = trial.get("audio")
    if not isinstance(sides, dict) or set(sides) != {"a", "b"}:
        raise ListeningImportError(f"Listening trial {trial_id!r} audio is invalid")
    for side in ("a", "b"):
        relative = _safe_relative(sides[side], f"trial {trial_id!r} side {side}")
        if relative.suffix.casefold() != ".wav":
            raise ListeningImportError(
                f"Listening trial {trial_id!r} side {side} must be a WAV file"
            )
        source = _within(root, relative, f"trial {trial_id!r} side {side}")
        if not source.is_file():
            raise ListeningImportError(f"Listening audio is missing: {source}")
        if relative in audio:
            raise ListeningImportError(
                f"Listening audio path is reused by more than one side: {relative}"
            )
        audio[relative] = source


def _validate_key(
    session: JsonObject,
    key: JsonObject,
    key_path: Path,
    key_sha256: str,
    trials: Sequence[JsonObject],
    audio_sha256: dict[Path, str],
) -> tuple[SourceControl, ...]:
    _validate_key_identity(session, key, key_sha256)
    sources = _key_sources(key)
    source_controls = _validate_source_inventory(sources)
    _validate_source_digest(sources, session)
    models, assignments = _key_models_and_assignments(key)
    model_ids = _model_ids(models)
    _validate_key_assignments(
        assignments,
        trials,
        key_path,
        model_ids,
        audio_sha256,
        source_controls,
    )
    return tuple(sorted(source_controls.items(), key=lambda item: str(item[0])))


def _validate_key_identity(
    session: JsonObject, key: JsonObject, key_sha256: str
) -> None:
    if key_sha256 != session.get("blind_key_sha256"):
        raise ListeningImportError(
            "Blind-listening key is missing, changed, or mismatched"
        )
    for field in ("source_kind", "source_sha256"):
        if key.get(field) != session.get(field):
            raise ListeningImportError(
                f"Blind-listening key {field} does not match session"
            )


def _key_sources(key: JsonObject) -> list[object]:
    sources = key.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ListeningImportError("Blind-listening key source inventory is invalid")
    return sources


def _validate_source_inventory(sources: Sequence[object]) -> dict[Path, str]:
    source_controls: dict[Path, str] = {}
    for source in sources:
        if not _is_json_object(source) or not isinstance(source.get("path"), str):
            raise ListeningImportError(
                "Blind-listening key source inventory is invalid"
            )
        source_path = Path(_text_field(source, "path")).expanduser().resolve()
        source_sha256 = _require_sha256(
            source.get("sha256"), "blind-key source SHA-256"
        )
        if not source_path.is_file() or sha256_file(source_path) != source_sha256:
            raise ListeningImportError(
                f"Blind-listening source report is missing or changed: {source_path}"
            )
        source_controls[source_path] = source_sha256
    return source_controls


def _validate_source_digest(sources: Sequence[object], session: JsonObject) -> None:
    source_digest = hashlib.sha256(
        json.dumps(sources, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if source_digest != session.get("source_sha256"):
        raise ListeningImportError(
            "Blind-listening source inventory digest is inconsistent"
        )


def _key_models_and_assignments(key: JsonObject) -> tuple[list[object], list[object]]:
    models = key.get("models")
    assignments = key.get("assignments")
    if (
        not isinstance(models, list)
        or len(models) < 2
        or not isinstance(assignments, list)
    ):
        raise ListeningImportError("Blind-listening key models/assignments are invalid")
    return models, assignments


def _model_ids(models: Sequence[object]) -> list[str]:
    model_ids: list[str] = []
    for model in models:
        if not _is_json_object(model):
            raise ListeningImportError("Blind-listening key contains an invalid model")
        values = [
            _text_field(model, field) for field in ("model_id", "provider", "model")
        ]
        if not all(value.strip() for value in values):
            raise ListeningImportError("Blind-listening key contains an invalid model")
        model_ids.append(values[0])
    if len(model_ids) != len(set(model_ids)):
        raise ListeningImportError("Blind-listening key contains duplicate models")
    return model_ids


def _validate_key_assignments(
    assignments: Sequence[object],
    trials: Sequence[JsonObject],
    key_path: Path,
    model_ids: Sequence[str],
    audio_sha256: Mapping[Path, str],
    source_controls: dict[Path, str],
) -> None:
    expected_ids = {
        trial_id
        for trial in trials
        if isinstance(trial_id := trial.get("trial_id"), str)
    }
    trial_by_id = {
        trial_id: trial
        for trial in trials
        if isinstance(trial_id := trial.get("trial_id"), str)
    }
    seen: set[str] = set()
    for assignment in assignments:
        trial_id = _validate_assignment_header(assignment, expected_ids, seen)
        if not _is_json_object(assignment):
            raise ListeningImportError("Blind-listening assignment is invalid")
        _validate_assignment_sides(
            assignment,
            trial_id,
            trial_by_id[trial_id],
            key_path,
            model_ids,
            audio_sha256,
            source_controls,
        )
    if seen != expected_ids:
        raise ListeningImportError(
            "Blind-listening assignments do not cover every trial"
        )


def _validate_assignment_header(
    assignment: object, expected_ids: set[str], seen: set[str]
) -> str:
    if not _is_json_object(assignment) or set(assignment) != {"trial_id", "a", "b"}:
        raise ListeningImportError("Blind-listening assignment is invalid")
    trial_id = assignment["trial_id"]
    if trial_id not in expected_ids:
        raise ListeningImportError(
            f"Blind assignment references unknown trial {trial_id!r}"
        )
    if trial_id in seen:
        raise ListeningImportError(f"Duplicate blind assignment for {trial_id!r}")
    seen.add(trial_id)
    return trial_id


def _validate_assignment_sides(
    assignment: JsonObject,
    trial_id: str,
    trial: JsonObject,
    key_path: Path,
    model_ids: Sequence[str],
    audio_sha256: Mapping[Path, str],
    source_controls: dict[Path, str],
) -> None:
    sides: list[str] = []
    for side in ("a", "b"):
        value = assignment[side]
        if not isinstance(value, dict) or value.get("model_id") not in model_ids:
            raise ListeningImportError(
                f"Blind assignment {trial_id!r} side {side} has an unknown model"
            )
        if not isinstance(value.get("source"), str) or not value["source"].strip():
            raise ListeningImportError(
                f"Blind assignment {trial_id!r} side {side} has no source provenance"
            )
        provenance_audio = Path(value["source"]).expanduser().resolve()
        audio = _object_field(trial, "audio")
        relative = _safe_relative(audio.get(side), f"trial {trial_id!r} side {side}")
        _within(key_path.parent, relative, f"trial {trial_id!r} side {side}")
        if (
            not provenance_audio.is_file()
            or sha256_file(provenance_audio) != audio_sha256[relative]
        ):
            raise ListeningImportError(
                f"Blind assignment {trial_id!r} side {side} audio does not match its alias"
            )
        source_controls[provenance_audio] = audio_sha256[relative]
        sides.append(value["model_id"])
    if sides[0] == sides[1]:
        raise ListeningImportError(
            f"Blind assignment {trial_id!r} compares a model with itself"
        )


def _validate_report(
    session_path: Path, session: JsonObject, key: JsonObject, report: JsonObject
) -> None:
    configured_session = report.get("session")
    if (
        not isinstance(configured_session, str)
        or Path(configured_session).expanduser().resolve() != session_path
    ):
        raise ListeningImportError("Listening report points to a different session")
    expected = _expected_report(session, key)
    for field, value in expected.items():
        if report.get(field) != value:
            raise ListeningImportError(
                f"Listening report {field} is inconsistent with session ratings/key"
            )


def _expected_report(session: JsonObject, key: JsonObject) -> JsonObject:
    assignments = {
        _text_field(item, "trial_id"): item
        for item in _object_list(key.get("assignments"), "blind assignments")
    }
    stats: dict[str, _ReportStats] = {
        _text_field(item, "model_id"): {
            "model_id": _text_field(item, "model_id"),
            "provider": _text_field(item, "provider"),
            "model": _text_field(item, "model"),
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "reviewed_trials": 0,
        }
        for item in _object_list(key.get("models"), "blind models")
    }
    pairwise: defaultdict[tuple[str, str], _PairwiseStats] = defaultdict(
        lambda: {"trials": 0, "left_wins": 0, "right_wins": 0, "ties": 0}
    )
    for trial in _object_list(session.get("trials"), "listening trials"):
        rating = trial.get("rating")
        if rating is None:
            continue
        if not _is_json_object(rating):
            raise ListeningImportError("Listening trial rating is invalid")
        trial_id = _text_field(trial, "trial_id")
        assignment = assignments[trial_id]
        left_arm = _object_field(assignment, "a")
        right_arm = _object_field(assignment, "b")
        side_models = {
            "a": _text_field(left_arm, "model_id"),
            "b": _text_field(right_arm, "model_id"),
        }
        for model_id in side_models.values():
            stats[model_id]["reviewed_trials"] += 1
        preferred = _text_field(rating, "preference")
        if preferred == "tie":
            stats[side_models["a"]]["ties"] += 1
            stats[side_models["b"]]["ties"] += 1
        else:
            winner = side_models[preferred]
            loser = side_models["b" if preferred == "a" else "a"]
            stats[winner]["wins"] += 1
            stats[loser]["losses"] += 1
        left, right = sorted(side_models.values())
        comparison = pairwise[(left, right)]
        comparison["trials"] += 1
        if preferred == "tie":
            comparison["ties"] += 1
        elif side_models[preferred] == left:
            comparison["left_wins"] += 1
        else:
            comparison["right_wins"] += 1
    models: list[JsonObject] = []
    for value in stats.values():
        preference_trials = value["wins"] + value["losses"] + value["ties"]
        models.append(
            {
                "model_id": value["model_id"],
                "provider": value["provider"],
                "model": value["model"],
                "reviewed_trials": value["reviewed_trials"],
                "preference": {
                    "wins": value["wins"],
                    "losses": value["losses"],
                    "ties": value["ties"],
                    "rate": (
                        round(
                            (value["wins"] + 0.5 * value["ties"]) / preference_trials, 4
                        )
                        if preference_trials
                        else None
                    ),
                },
            }
        )
    models.sort(key=_model_sort_key)
    for rank, model in enumerate(models, start=1):
        model["rank"] = rank
    completed = _int_field(session, "completed_count")
    total = _int_field(session, "trial_count")
    return {
        "complete": completed == total,
        "completed_trials": completed,
        "pending_trials": total - completed,
        "manual_selection_required": True,
        "models": models,
        "pairwise": [
            {"left_model": left, "right_model": right, **values}
            for (left, right), values in sorted(pairwise.items())
        ],
    }


def _manifest(inspection: ListeningImportInspection, import_id: str) -> JsonObject:
    return {
        "schema": IMPORT_SCHEMA,
        "schema_version": IMPORT_SCHEMA_VERSION,
        "import_id": import_id,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "kind": "reverse1999-extractor-model-listening-session",
            "session_directory": str(inspection.session_directory),
            "logical_identity": inspection.logical_identity,
            "source_fingerprint": inspection.source_fingerprint,
        },
        "summary": {
            "trial_count": inspection.trial_count,
            "completed_count": inspection.completed_count,
            "audio_count": inspection.audio_count,
            "report_present": inspection.report_present,
        },
        "artifacts": [
            {
                "role": role,
                "source_path": str(source),
                "path": relative.as_posix(),
                "sha256": digest,
                **(
                    {"mode": inspection.key_mode}
                    if role == "blind_listening_key" and inspection.key_mode is not None
                    else {}
                ),
            }
            for role, source, relative, digest in inspection.artifacts
        ],
    }


def _validate_existing(
    destination: Path, inspection: ListeningImportInspection
) -> ListeningImportResult:
    manifest_path = destination / "import.json"
    manifest = _load_json(manifest_path, "existing listening import")
    source = manifest.get("source")
    if (
        manifest.get("schema") != IMPORT_SCHEMA
        or manifest.get("schema_version") != IMPORT_SCHEMA_VERSION
        or not isinstance(source, dict)
        or source.get("logical_identity") != inspection.logical_identity
    ):
        raise ListeningImportError(
            f"Listening import destination conflicts with existing data: {destination}"
        )
    if source.get("source_fingerprint") != inspection.source_fingerprint:
        raise ListeningImportError(
            "Listening session changed after import; existing application data was left untouched"
        )
    expected = _manifest(inspection, f"listening-{inspection.logical_identity[:24]}")
    expected["imported_at"] = manifest.get("imported_at")
    if manifest != expected:
        raise ListeningImportError(
            f"Existing listening import manifest was modified: {manifest_path}"
        )
    for artifact in _object_list(manifest.get("artifacts"), "imported artifacts"):
        if not _is_json_object(artifact):
            raise ListeningImportError(f"Malformed listening import: {manifest_path}")
        relative = _safe_relative(artifact.get("path"), "imported listening artifact")
        path = _within(destination, relative, "imported listening artifact")
        if not path.is_file() or sha256_file(path) != artifact.get("sha256"):
            raise ListeningImportError(f"Imported listening artifact changed: {path}")
        if "mode" in artifact and (path.stat().st_mode & 0o777) != artifact["mode"]:
            raise ListeningImportError(
                f"Imported listening artifact mode changed: {path}"
            )
    _verify_controls_unchanged(inspection)
    return ListeningImportResult(destination, manifest, False)


def _verify_controls_unchanged(inspection: ListeningImportInspection) -> None:
    for role, source, _relative, digest in inspection.artifacts:
        if not source.is_file() or sha256_file(source) != digest:
            raise ListeningImportError(
                "Listening source is active or changed during import; retry when idle. "
                "No application data was published."
            )
        if (
            role == "blind_listening_key"
            and inspection.key_mode is not None
            and (source.stat().st_mode & 0o777) != inspection.key_mode
        ):
            raise ListeningImportError(
                "Blind-listening key mode changed during import; retry when idle. "
                "No application data was published."
            )
    for source, digest in inspection.source_controls:
        if not source.is_file() or sha256_file(source) != digest:
            raise ListeningImportError(
                "Listening provenance source changed during import; retry when idle. "
                "No application data was published."
            )


def _load_schema_snapshot(
    path: PathInput, schema: str, description: str
) -> tuple[JsonObject, str]:
    path = Path(path)
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ListeningImportError(
            f"Unable to read {description} {path}: {error}"
        ) from error
    digest = hashlib.sha256(payload).hexdigest()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ListeningImportError(
            f"Unable to read {description} {path}: {error}"
        ) from error
    if not _is_json_object(value):
        raise ListeningImportError(f"{description.title()} must be a JSON object")
    if value.get("schema") != schema or value.get("schema_version") != SCHEMA_VERSION:
        raise ListeningImportError(
            f"Unsupported {description} schema; expected {schema!r} version {SCHEMA_VERSION}"
        )
    return value, digest


def _load_json(path: PathInput, description: str) -> JsonObject:
    value: object = load_json_object(path, description, error_type=ListeningImportError)
    if not _is_json_object(value):
        raise ListeningImportError(f"{description.title()} must be a JSON object")
    return value


def _safe_relative(value: object, label: str) -> Path:
    return safe_relative_path(value, label, error_type=ListeningImportError)


def _within(root: PathInput, relative: Path, label: str) -> Path:
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ListeningImportError(f"{label} leaves the session directory") from error
    return candidate


def _require_sha256(value: object, label: str) -> str:
    result: object = require_sha256(value, label, error_type=ListeningImportError)
    if not isinstance(result, str):
        raise ListeningImportError(f"{label} must be a full SHA-256")
    return result


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _is_json_object(value: object) -> TypeGuard[JsonObject]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _object_list(value: object, label: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(_is_json_object(item) for item in value):
        raise ListeningImportError(f"{label.title()} must be objects")
    return value


def _object_field(document: JsonObject, field: str) -> JsonObject:
    value = document.get(field)
    if not _is_json_object(value):
        raise ListeningImportError(f"Listening {field} is invalid")
    return value


def _text_field(document: JsonObject, field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str):
        raise ListeningImportError(f"Listening {field} is invalid")
    return value


def _int_field(document: JsonObject, field: str) -> int:
    value = document.get(field)
    if not isinstance(value, int):
        raise ListeningImportError(f"Listening {field} is invalid")
    return value


def _model_sort_key(model: JsonObject) -> tuple[float, int, str]:
    preference = _object_field(model, "preference")
    rate = preference.get("rate")
    wins = _int_field(preference, "wins")
    return (
        -(float(rate) if isinstance(rate, int | float) else -1),
        -wins,
        _text_field(model, "model_id"),
    )
