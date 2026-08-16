"""Non-destructive preservation import for Reverse: 1999 listening sessions."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.legacy_import import default_import_root

SESSION_SCHEMA = "r1999.model-listening-session"
KEY_SCHEMA = "r1999.model-listening-key"
REPORT_SCHEMA = "r1999.model-listening-report"
SCHEMA_VERSION = 1
IMPORT_SCHEMA = "vntts.authoring-listening-import"
IMPORT_SCHEMA_VERSION = 1
LEGACY_DIMENSIONS = ("timbre", "accent", "naturalness", "pronunciation")


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
    key_mode: int = 0o600


@dataclass(frozen=True)
class ListeningImportResult:
    destination: Path
    manifest: dict[str, object]
    created: bool


def inspect_listening_session(session_directory):
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
    key_mode = key_path.stat().st_mode & 0o777
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

    artifacts = [
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
        completed_count=int(session["completed_count"]),
        audio_count=len(audio),
        report_present=report_path.is_file(),
        artifacts=tuple(artifacts),
        source_controls=source_controls,
        key_mode=key_mode,
    )
    _verify_controls_unchanged(inspection)
    return inspection


def import_listening_session(session_directory, destination_root=None):
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

    staging = Path(tempfile.mkdtemp(prefix=f".{import_id}-", dir=destination_root))
    try:
        for role, source, relative, digest in inspection.artifacts:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256_file(target) != digest:
                raise ListeningImportError(
                    f"Listening artifact changed during import: {source}"
                )
            if role == "blind_listening_key" and (
                (target.stat().st_mode & 0o777) != inspection.key_mode
            ):
                raise ListeningImportError(
                    f"Blind-listening key mode changed during import: {source}"
                )
        atomic_write_json(staging / "import.json", manifest, sort_keys=True)
        _verify_controls_unchanged(inspection)
        try:
            staging.rename(destination)
        except OSError:
            if destination.exists():
                return _validate_existing(destination, inspection)
            raise
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return ListeningImportResult(destination, manifest, True)


def _validate_session(root, session):
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
    trial_ids = set()
    audio = {}
    completed = 0
    for index, trial in enumerate(trials):
        if not isinstance(trial, dict):
            raise ListeningImportError(f"Listening trial {index} must be an object")
        trial_id = trial.get("trial_id")
        if not isinstance(trial_id, str) or not trial_id or trial_id in trial_ids:
            raise ListeningImportError("Listening session trial IDs are invalid")
        trial_ids.add(trial_id)
        rating = trial.get("rating")
        if rating is not None:
            if not isinstance(rating, dict) or rating.get("preference") not in {
                "a",
                "b",
                "tie",
            }:
                raise ListeningImportError(
                    f"Listening trial {trial_id!r} rating is invalid"
                )
            completed += 1
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
    if session.get("completed_count") != completed:
        raise ListeningImportError("Listening session completed_count is inconsistent")
    return trials, audio


def _validate_key(session, key, key_path, key_sha256, trials, audio_sha256):
    if key_sha256 != session.get("blind_key_sha256"):
        raise ListeningImportError(
            "Blind-listening key is missing, changed, or mismatched"
        )
    for field in ("source_kind", "source_sha256"):
        if key.get(field) != session.get(field):
            raise ListeningImportError(
                f"Blind-listening key {field} does not match session"
            )
    sources = key.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ListeningImportError("Blind-listening key source inventory is invalid")
    source_controls = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            raise ListeningImportError(
                "Blind-listening key source inventory is invalid"
            )
        _require_sha256(source.get("sha256"), "blind-key source SHA-256")
        source_path = Path(source["path"]).expanduser().resolve()
        if not source_path.is_file() or sha256_file(source_path) != source["sha256"]:
            raise ListeningImportError(
                f"Blind-listening source report is missing or changed: {source_path}"
            )
        source_controls[source_path] = source["sha256"]
    source_digest = hashlib.sha256(
        json.dumps(sources, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if source_digest != session.get("source_sha256"):
        raise ListeningImportError(
            "Blind-listening source inventory digest is inconsistent"
        )
    models = key.get("models")
    assignments = key.get("assignments")
    if (
        not isinstance(models, list)
        or len(models) < 2
        or not isinstance(assignments, list)
    ):
        raise ListeningImportError("Blind-listening key models/assignments are invalid")
    model_ids = []
    for model in models:
        if not isinstance(model, dict) or not all(
            isinstance(model.get(field), str) and model[field].strip()
            for field in ("model_id", "provider", "model")
        ):
            raise ListeningImportError("Blind-listening key contains an invalid model")
        model_ids.append(model["model_id"])
    if len(model_ids) != len(set(model_ids)):
        raise ListeningImportError("Blind-listening key contains duplicate models")
    expected_ids = {trial["trial_id"] for trial in trials}
    trial_by_id = {trial["trial_id"]: trial for trial in trials}
    seen = set()
    for assignment in assignments:
        if not isinstance(assignment, dict) or set(assignment) != {
            "trial_id",
            "a",
            "b",
        }:
            raise ListeningImportError("Blind-listening assignment is invalid")
        trial_id = assignment["trial_id"]
        if trial_id not in expected_ids:
            raise ListeningImportError(
                f"Blind assignment references unknown trial {trial_id!r}"
            )
        if trial_id in seen:
            raise ListeningImportError(f"Duplicate blind assignment for {trial_id!r}")
        seen.add(trial_id)
        sides = []
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
            relative = _safe_relative(
                trial_by_id[trial_id]["audio"][side],
                f"trial {trial_id!r} side {side}",
            )
            _within(
                key_path.parent,
                relative,
                f"trial {trial_id!r} side {side}",
            )
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
    if seen != expected_ids:
        raise ListeningImportError(
            "Blind-listening assignments do not cover every trial"
        )
    return tuple(sorted(source_controls.items(), key=lambda item: str(item[0])))


def _validate_report(session_path, session, key, report):
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


def _expected_report(session, key):
    assignments = {item["trial_id"]: item for item in key["assignments"]}
    stats = {
        item["model_id"]: {
            "model_id": item["model_id"],
            "provider": item["provider"],
            "model": item["model"],
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "reviewed_trials": 0,
        }
        for item in key["models"]
    }
    pairwise = defaultdict(
        lambda: {"trials": 0, "left_wins": 0, "right_wins": 0, "ties": 0}
    )
    for trial in session["trials"]:
        rating = trial.get("rating")
        if rating is None:
            continue
        assignment = assignments[trial["trial_id"]]
        side_models = {side: assignment[side]["model_id"] for side in ("a", "b")}
        for model_id in side_models.values():
            stats[model_id]["reviewed_trials"] += 1
        preferred = rating["preference"]
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
    models = []
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
    models.sort(
        key=lambda item: (
            -(
                item["preference"]["rate"]
                if item["preference"]["rate"] is not None
                else -1
            ),
            -item["preference"]["wins"],
            item["model_id"],
        )
    )
    for rank, model in enumerate(models, start=1):
        model["rank"] = rank
    completed = int(session["completed_count"])
    total = int(session["trial_count"])
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


def _manifest(inspection, import_id):
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
                    if role == "blind_listening_key"
                    else {}
                ),
            }
            for role, source, relative, digest in inspection.artifacts
        ],
    }


def _validate_existing(destination, inspection):
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
    for artifact in manifest["artifacts"]:
        if not isinstance(artifact, dict):
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


def _verify_controls_unchanged(inspection):
    for role, source, _relative, digest in inspection.artifacts:
        if not source.is_file() or sha256_file(source) != digest:
            raise ListeningImportError(
                "Listening source is active or changed during import; retry when idle. "
                "No application data was published."
            )
        if (
            role == "blind_listening_key"
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


def _load_schema(path, schema, description):
    value = _load_json(path, description)
    if value.get("schema") != schema or value.get("schema_version") != SCHEMA_VERSION:
        raise ListeningImportError(
            f"Unsupported {description} schema; expected {schema!r} version {SCHEMA_VERSION}"
        )
    return value


def _load_schema_snapshot(path, schema, description):
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
    if not isinstance(value, dict):
        raise ListeningImportError(f"{description.title()} must be a JSON object")
    if value.get("schema") != schema or value.get("schema_version") != SCHEMA_VERSION:
        raise ListeningImportError(
            f"Unsupported {description} schema; expected {schema!r} version {SCHEMA_VERSION}"
        )
    return value, digest


def _load_json(path, description):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ListeningImportError(
            f"Unable to read {description} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ListeningImportError(f"{description.title()} must be a JSON object")
    return value


def _safe_relative(value, label):
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise ListeningImportError(f"{label} must be a non-empty POSIX-relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ListeningImportError(f"{label} must stay within the session directory")
    return Path(*pure.parts)


def _within(root, relative, label):
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ListeningImportError(f"{label} leaves the session directory") from error
    return candidate


def _require_sha256(value, label):
    if not isinstance(value, str) or len(value) != 64:
        raise ListeningImportError(f"{label} must be a full SHA-256")
    try:
        int(value, 16)
    except ValueError as error:
        raise ListeningImportError(f"{label} must be hexadecimal") from error


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
