"""Generic blind, resumable same-text A/B model listening."""

from __future__ import annotations

import hashlib
import importlib
import itertools
import json
import os
import random
import re
import shutil
import struct
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal, NotRequired, Protocol, TypedDict, TypeGuard

from vntts_artifacts.atomic_io import atomic_output_path, atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.authoring.private_files import private_file_is_restricted
from vntts.authoring.workspace_foundation import load_json_object
from vntts.document_identity import is_lowercase_sha256
from vntts.settings import get_local_data_directory

SESSION_SCHEMA = "vntts.model-listening-session"
KEY_SCHEMA = "vntts.model-listening-key"
REPORT_SCHEMA = "vntts.model-listening-report"
MODEL_REPORT_SCHEMA = "vntts.voice-model-report"
TTS_MODEL_REPORT_SCHEMA = "vntts.tts-benchmark-report"
LEGACY_SESSION_SCHEMA = "r1999.model-listening-session"
LEGACY_KEY_SCHEMA = "r1999.model-listening-key"
LEGACY_REPORT_SCHEMA = "r1999.model-listening-report"
SCHEMA_VERSION = 1
LEGACY_DIMENSIONS = ("timbre", "accent", "naturalness", "pronunciation")
default_session_directory = get_local_data_directory() / "authoring" / "model-listening"

PathInput = str | Path
Preference = Literal["a", "b", "tie", "neither"]
RecordedPreference = Literal["a", "b", "tie"]


class TrialAudio(TypedDict):
    a: str
    b: str


class TrialRating(TypedDict):
    preference: RecordedPreference


class StoredTrialRating(TrialRating, total=False):
    acceptability: Literal["neither"]
    reviewed_at: str


class _ValidatedListeningTrial(TypedDict):
    trial_id: str
    queue_id: str
    line_id: str | None
    text_sha256: str | None
    text: str | None
    audio: TrialAudio
    rating: StoredTrialRating | None
    audio_sha256: NotRequired[TrialAudio]


class _ValidatedListeningSession(TypedDict):
    schema: str
    schema_version: int
    source_kind: str
    source_sha256: str
    blind_key_sha256: str
    decision_mode: str
    trial_count: int
    completed_count: int
    trials: list[_ValidatedListeningTrial]
    updated_at: str
    dimensions: NotRequired[list[str]]


class ListeningModel(TypedDict):
    model_id: str
    provider: str
    model: str
    reports: NotRequired[list[str]]


class AudioRecord(TypedDict):
    path: Path
    sha256: str


class CorpusItem(TypedDict):
    queue_id: str
    line_id: str
    text_sha256: str
    text: str


class SourceRecord(TypedDict):
    path: str
    sha256: str


class AssignmentArm(TypedDict):
    model_id: str
    source: str
    audio_sha256: NotRequired[str]


class BlindAssignment(TypedDict):
    trial_id: str
    a: AssignmentArm
    b: AssignmentArm


class ListeningKey(TypedDict):
    source_kind: str
    source_sha256: str
    models: list[ListeningModel]
    assignments: list[BlindAssignment]


class ModelReport(TypedDict):
    backend: str
    model_id: str
    provider: NotRequired[object]
    model: NotRequired[object]


class ModelReportSample(TypedDict):
    id: str
    line_id: str
    text: str
    text_sha256: str
    audio_sha256: str
    resolved_audio: Path


class ModelStats(TypedDict):
    model_id: str
    provider: str
    model: str
    wins: int
    losses: int
    ties: int
    rejections: int
    reviewed_trials: int


class PreferenceStats(TypedDict):
    wins: int
    losses: int
    ties: int
    rate: float | None
    rejections: NotRequired[int]


class ReportModel(TypedDict):
    model_id: str
    provider: str
    model: str
    reviewed_trials: int
    preference: PreferenceStats
    rank: NotRequired[int]


class PairwiseStats(TypedDict):
    trials: int
    left_wins: int
    right_wins: int
    ties: int
    neither_acceptable: int


class ReportFields(TypedDict):
    complete: bool
    completed_trials: int
    pending_trials: int
    manual_selection_required: bool
    models: list[ReportModel]
    pairwise: list[dict[str, object]]


class ListeningTrial(TypedDict):
    trial_id: str
    queue_id: str
    audio: TrialAudio
    line_id: NotRequired[str]
    text: NotRequired[str]


class ListeningSession(TypedDict):
    trials: list[ListeningTrial]


class ListeningReportModel(TypedDict):
    model_id: str


class ListeningReport(TypedDict):
    models: list[ListeningReportModel]


class _ListeningCli(Protocol):
    def main(self, argv: Sequence[str] | None = None) -> int: ...


class ModelListeningError(RuntimeError):
    """A listening session is invalid or cannot be updated safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_text(text: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(text)).replace("…", "...")
    return re.sub(r"\s+", " ", normalized).strip()


def _source_digest(paths: Iterable[PathInput]) -> tuple[list[SourceRecord], str]:
    sources: list[SourceRecord] = []
    for path in paths:
        resolved = Path(path).expanduser().resolve()
        sources.append({"path": str(resolved), "sha256": sha256_file(resolved)})
    payload = json.dumps(sources, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return sources, hashlib.sha256(payload).hexdigest()


def _link_blind_audio(source: PathInput, destination: PathInput) -> None:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def create_listening_session_from_reports(
    report_paths: Iterable[PathInput],
    output_directory: PathInput,
    *,
    seed: int = 0,
    sample_ids: Iterable[str] | None = None,
) -> Path:
    """Create blind trials from two or more generic per-model reports."""
    resolved_paths = [Path(path).expanduser().resolve() for path in report_paths]
    if len(resolved_paths) < 2:
        raise ModelListeningError("At least two model reports are required")
    selected_ids = _selected_model_report_ids(sample_ids)
    model_metadata: dict[str, ListeningModel] = {}
    audio_by_model: defaultdict[str, dict[str, AudioRecord]] = defaultdict(dict)
    corpus_items: dict[str, CorpusItem] = {}
    for report_path in resolved_paths:
        _collect_model_report_samples(
            report_path,
            selected_ids,
            model_metadata,
            audio_by_model,
            corpus_items,
        )
    if selected_ids is not None:
        _validate_selected_samples(
            selected_ids, model_metadata, audio_by_model, corpus_items
        )
    sources, source_sha256 = _source_digest(resolved_paths)
    return _write_listening_session(
        output_directory,
        list(model_metadata.values()),
        dict(audio_by_model),
        list(corpus_items.values()),
        sources=sources,
        source_sha256=source_sha256,
        seed=seed,
    )


def _selected_model_report_ids(
    sample_ids: Iterable[str] | None,
) -> frozenset[str] | None:
    if sample_ids is None:
        return None
    raw_selected_ids = tuple(sample_ids)
    if not raw_selected_ids or any(
        not isinstance(value, str) or not value.strip() for value in raw_selected_ids
    ):
        raise ModelListeningError(
            "Selected model-report sample IDs must be non-empty text"
        )
    if len(raw_selected_ids) != len(set(raw_selected_ids)):
        raise ModelListeningError("Selected model-report sample IDs are duplicated")
    return frozenset(raw_selected_ids)


def _collect_model_report_samples(
    report_path: Path,
    selected_ids: frozenset[str] | None,
    model_metadata: dict[str, ListeningModel],
    audio_by_model: defaultdict[str, dict[str, AudioRecord]],
    corpus_items: dict[str, CorpusItem],
) -> None:
    report, samples = _load_model_report(report_path)
    backend = report["backend"]
    model_id = report["model_id"]
    metadata = model_metadata.setdefault(
        model_id,
        {
            "model_id": model_id,
            "provider": str(report.get("provider") or backend),
            "model": str(report.get("model") or model_id),
            "reports": [],
        },
    )
    report_name = str(report_path)
    if report_name not in metadata["reports"]:
        metadata["reports"].append(report_name)
    for sample in samples:
        if selected_ids is None or sample["id"] in selected_ids:
            _collect_model_sample(model_id, sample, audio_by_model, corpus_items)


def _collect_model_sample(
    model_id: str,
    sample: ModelReportSample,
    audio_by_model: defaultdict[str, dict[str, AudioRecord]],
    corpus_items: dict[str, CorpusItem],
) -> None:
    text = sample["text"]
    normalized = _normalized_text(text)
    text_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    identity = sample["id"]
    queue_id = f"corpus:{identity}:{text_hash[:16]}"
    audio_record: AudioRecord = {
        "path": sample["resolved_audio"],
        "sha256": sample["audio_sha256"],
    }
    existing = audio_by_model[model_id].get(queue_id)
    if existing is not None and existing != audio_record:
        raise ModelListeningError(
            f"Model {model_id} has multiple outputs for sample {identity!r}"
        )
    audio_by_model[model_id][queue_id] = audio_record
    item: CorpusItem = {
        "queue_id": queue_id,
        "line_id": sample["line_id"],
        "text_sha256": text_hash,
        "text": text,
    }
    current = corpus_items.get(queue_id)
    if current is not None and (
        current["text_sha256"] != text_hash
        or _normalized_text(current["text"]) != normalized
    ):
        raise ModelListeningError(
            f"Model reports disagree on shared sample {identity!r}"
        )
    corpus_items.setdefault(queue_id, item)


def _validate_selected_samples(
    selected_ids: frozenset[str],
    model_metadata: dict[str, ListeningModel],
    audio_by_model: defaultdict[str, dict[str, AudioRecord]],
    corpus_items: dict[str, CorpusItem],
) -> None:
    shared_ids = {
        item["queue_id"].removeprefix("corpus:").rsplit(":", 1)[0]
        for item in corpus_items.values()
        if sum(
            item["queue_id"] in audio_by_model[model_id] for model_id in model_metadata
        )
        >= 2
    }
    missing = sorted(selected_ids - shared_ids)
    if missing:
        raise ModelListeningError(
            "Selected samples do not have complete audio from two models: "
            + ", ".join(missing)
        )


def create_listening_session(
    benchmark_path: PathInput, output_directory: PathInput, *, seed: int = 0
) -> Path:
    """Create a session from a VNTTS multi-model benchmark aggregate."""
    benchmark_path = Path(benchmark_path).expanduser().resolve()
    benchmark = _load_schema(
        benchmark_path,
        {"vntts.voice-model-benchmark"},
        "model benchmark",
    )
    reports = benchmark.get("reports")
    if not isinstance(reports, list) or len(reports) < 2:
        raise ModelListeningError("Model benchmark must reference at least two reports")
    resolved: list[Path] = []
    for value in reports:
        if not isinstance(value, str) or not value.strip():
            raise ModelListeningError("Model benchmark report paths are invalid")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = benchmark_path.parent / path
        resolved.append(path.resolve())
    return create_listening_session_from_reports(resolved, output_directory, seed=seed)


def _write_listening_session(
    output_directory: PathInput,
    models: Sequence[ListeningModel],
    audio_by_model: dict[str, dict[str, AudioRecord]],
    corpus_items: Sequence[CorpusItem],
    *,
    sources: list[SourceRecord],
    source_sha256: str,
    seed: int,
) -> Path:
    output_directory = Path(output_directory).expanduser().resolve()
    session_path = output_directory / "session.json"
    if session_path.exists() or (
        output_directory.exists() and any(output_directory.iterdir())
    ):
        raise ModelListeningError(
            f"Listening session directory is not empty: {output_directory}"
        )
    model_ids = [model["model_id"] for model in models]
    if len(model_ids) != len(set(model_ids)):
        raise ModelListeningError("Model reports contain duplicate model IDs")
    pairs: list[tuple[CorpusItem, str, str]] = []
    for item in corpus_items:
        queue_id = item["queue_id"]
        available = [
            model_id for model_id in model_ids if queue_id in audio_by_model[model_id]
        ]
        for left, right in itertools.combinations(available, 2):
            pairs.append((item, left, right))
    if not pairs:
        raise ModelListeningError("No same-text samples are shared by two models")
    generator = random.Random(seed)
    generator.shuffle(pairs)
    output_directory.mkdir(parents=True, exist_ok=True)
    trials: list[_ValidatedListeningTrial] = []
    assignments: list[BlindAssignment] = []
    for index, (item, left, right) in enumerate(pairs, start=1):
        sides = [left, right]
        generator.shuffle(sides)
        trial_id = f"trial-{index:04d}"
        aliases = {
            side: Path("audio") / f"{trial_id}-{side}.wav" for side in ("a", "b")
        }
        for side, model_id in zip(("a", "b"), sides, strict=True):
            _link_blind_audio(
                audio_by_model[model_id][item["queue_id"]]["path"],
                output_directory / aliases[side],
            )
            _verify_pcm_audio(
                output_directory / aliases[side],
                audio_by_model[model_id][item["queue_id"]]["sha256"],
                "blind audio alias",
            )
        trial: _ValidatedListeningTrial = {
            "trial_id": trial_id,
            **item,
            "audio": {
                "a": aliases["a"].as_posix(),
                "b": aliases["b"].as_posix(),
            },
            "audio_sha256": {
                "a": audio_by_model[sides[0]][item["queue_id"]]["sha256"],
                "b": audio_by_model[sides[1]][item["queue_id"]]["sha256"],
            },
            "rating": None,
        }
        trials.append(trial)
        assignments.append(
            {
                "trial_id": trial_id,
                "a": {
                    "model_id": sides[0],
                    "source": str(audio_by_model[sides[0]][item["queue_id"]]["path"]),
                    "audio_sha256": audio_by_model[sides[0]][item["queue_id"]][
                        "sha256"
                    ],
                },
                "b": {
                    "model_id": sides[1],
                    "source": str(audio_by_model[sides[1]][item["queue_id"]]["path"]),
                    "audio_sha256": audio_by_model[sides[1]][item["queue_id"]][
                        "sha256"
                    ],
                },
            }
        )
    key_path = output_directory / ".blind-key.json"
    key: dict[str, object] = {
        "schema": KEY_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_at": _utc_now(),
        "source_kind": "model-reports",
        "source_sha256": source_sha256,
        "sources": sources,
        "models": models,
        "assignments": assignments,
    }
    _atomic_write_private_json(key_path, key)
    session: dict[str, object] = {
        "schema": SESSION_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "source_kind": "model-reports",
        "source_sha256": source_sha256,
        "blind_key_sha256": sha256_file(key_path),
        "seed": seed,
        "decision_mode": "preference-only",
        "trial_count": len(trials),
        "completed_count": 0,
        "trials": trials,
    }
    atomic_write_json(session_path, session, sort_keys=True)
    return session_path


def load_listening_session(path: PathInput) -> ListeningSession:
    path = Path(path).expanduser().resolve()
    session = _load_schema(
        path,
        {SESSION_SCHEMA, LEGACY_SESSION_SCHEMA},
        "listening session",
    )
    trials = _session_trials(session)
    current_schema = session.get("schema") == SESSION_SCHEMA
    legacy_audio_hashes = (
        {} if current_schema else _legacy_import_audio_hashes(path.parent)
    )
    typed_trials = _typed_listening_trials(trials)
    _validate_listening_trial_ids(typed_trials, len(trials))
    _validate_listening_progress(session, typed_trials)
    for trial in typed_trials:
        _validate_listening_trial(path, trial, current_schema, legacy_audio_hashes)
    if not _is_listening_session(session):
        raise ModelListeningError("Listening session is invalid")
    _load_blind_key(path, session)
    return _public_session(session)


def _session_trials(session: dict[str, object]) -> list[object]:
    trials = _object_list(session.get("trials"))
    if trials is None or session.get("trial_count") != len(trials):
        raise ModelListeningError("Listening session trial count is invalid")
    if session.get("decision_mode") != "preference-only" and session.get(
        "dimensions"
    ) != list(LEGACY_DIMENSIONS):
        raise ModelListeningError("Listening session decision mode is invalid")
    return trials


def _typed_listening_trials(
    trials: Sequence[object],
) -> list[_ValidatedListeningTrial]:
    typed_trials: list[_ValidatedListeningTrial] = []
    for raw_trial in trials:
        if not _is_listening_trial(raw_trial):
            raise ModelListeningError("Listening session trial is invalid")
        typed_trials.append(raw_trial)
    return typed_trials


def _validate_listening_trial_ids(
    trials: Sequence[_ValidatedListeningTrial], trial_count: int
) -> None:
    trial_ids = [trial["trial_id"] for trial in trials]
    if len(trial_ids) != trial_count or len(set(trial_ids)) != trial_count:
        raise ModelListeningError("Listening session trial IDs are invalid")


def _validate_listening_progress(
    session: Mapping[str, object], trials: Sequence[_ValidatedListeningTrial]
) -> None:
    completed = sum(trial["rating"] is not None for trial in trials)
    if session.get("completed_count") != completed:
        raise ModelListeningError("Listening session progress is inconsistent")


def _validate_listening_trial(
    path: Path,
    trial: _ValidatedListeningTrial,
    current_schema: bool,
    legacy_audio_hashes: Mapping[str, str],
) -> None:
    if current_schema:
        _validate_current_trial_identity(trial)
    _validate_listening_trial_rating(trial)
    _validate_listening_trial_audio(path, trial, current_schema, legacy_audio_hashes)


def _validate_current_trial_identity(trial: _ValidatedListeningTrial) -> None:
    line_id = trial.get("line_id")
    text = trial.get("text")
    text_hash = trial.get("text_sha256")
    if not isinstance(line_id, str) or not line_id.strip():
        raise ModelListeningError(
            f"Listening trial line identity is invalid: {trial['trial_id']}"
        )
    if not isinstance(text, str) or not text or not is_lowercase_sha256(text_hash):
        raise ModelListeningError(
            f"Listening trial text identity is invalid: {trial['trial_id']}"
        )
    if hashlib.sha256(_normalized_text(text).encode("utf-8")).hexdigest() != text_hash:
        raise ModelListeningError(
            f"Listening trial text hash changed: {trial['trial_id']}"
        )


def _validate_listening_trial_rating(trial: _ValidatedListeningTrial) -> None:
    rating = trial.get("rating")
    if rating is not None and (
        not isinstance(rating, dict)
        or rating.get("preference") not in {"a", "b", "tie"}
        or rating.get("acceptability") not in {None, "neither"}
        or (
            rating.get("acceptability") == "neither"
            and rating.get("preference") != "tie"
        )
    ):
        raise ModelListeningError(
            f"Listening trial rating is invalid: {trial['trial_id']}"
        )


def _validate_listening_trial_audio(
    path: Path,
    trial: _ValidatedListeningTrial,
    current_schema: bool,
    legacy_audio_hashes: Mapping[str, str],
) -> None:
    audio = trial.get("audio")
    if not _is_trial_audio(audio):
        raise ModelListeningError(
            f"Listening trial audio is invalid: {trial['trial_id']}"
        )
    expected_hashes = trial.get("audio_sha256")
    if current_schema and not _is_trial_audio(expected_hashes):
        raise ModelListeningError(
            f"Listening trial audio hashes are invalid: {trial['trial_id']}"
        )
    audio_hashes = expected_hashes if _is_trial_audio(expected_hashes) else None
    audio_pairs: tuple[tuple[Literal["a"], str], tuple[Literal["b"], str]] = (
        ("a", audio["a"]),
        ("b", audio["b"]),
    )
    for side, relative in audio_pairs:
        candidate = _within(path.parent, relative, "listening trial audio")
        if not candidate.is_file():
            raise ModelListeningError(f"Listening trial audio is missing: {candidate}")
        expected_hash = (
            audio_hashes[side]
            if audio_hashes is not None
            else legacy_audio_hashes.get(relative)
        )
        _verify_pcm_audio(candidate, expected_hash, "listening trial audio")


def _load_blind_key(
    session_path: PathInput, session: _ValidatedListeningSession
) -> ListeningKey:
    key_path = Path(session_path).expanduser().resolve().with_name(".blind-key.json")
    _validate_blind_key_file(key_path, session)
    key = _load_schema(key_path, {_blind_key_schema(session)}, "listening key")
    _validate_blind_key_identity(key, session)
    if not _is_listening_key(key):
        raise ModelListeningError("Listening session blind key is invalid")
    models = key["models"]
    assignments = key["assignments"]
    model_ids = [model["model_id"] for model in models]
    if len(model_ids) != len(set(model_ids)):
        raise ModelListeningError("Listening session blind key models are invalid")
    _validate_assignment_coverage(assignments, session["trials"])
    _validate_blind_assignments(key_path, session, assignments, model_ids)
    return key


def _validate_blind_key_file(
    key_path: Path, session: _ValidatedListeningSession
) -> None:
    if key_path.is_file() and not private_file_is_restricted(key_path):
        raise ModelListeningError("Listening session blind key mode must be 0600")
    if not key_path.is_file() or sha256_file(key_path) != session.get(
        "blind_key_sha256"
    ):
        raise ModelListeningError("Listening session blind key is missing or changed")


def _blind_key_schema(session: _ValidatedListeningSession) -> str:
    return (
        LEGACY_KEY_SCHEMA
        if session.get("schema") == LEGACY_SESSION_SCHEMA
        else KEY_SCHEMA
    )


def _validate_blind_key_identity(
    key: Mapping[str, object], session: _ValidatedListeningSession
) -> None:
    if key.get("source_kind") != session.get("source_kind") or key.get(
        "source_sha256"
    ) != session.get("source_sha256"):
        raise ModelListeningError("Listening session source identity changed")


def _validate_assignment_coverage(
    assignments: Sequence[BlindAssignment], trials: Sequence[_ValidatedListeningTrial]
) -> None:
    assignment_ids = [item["trial_id"] for item in assignments]
    trial_ids = [trial["trial_id"] for trial in trials]
    if sorted(assignment_ids) != sorted(trial_ids):
        raise ModelListeningError("Listening session blind assignments are incomplete")


def _validate_blind_assignments(
    key_path: Path,
    session: _ValidatedListeningSession,
    assignments: Sequence[BlindAssignment],
    model_ids: Sequence[str],
) -> None:
    for assignment in assignments:
        trial = next(
            item
            for item in session["trials"]
            if item["trial_id"] == assignment["trial_id"]
        )
        sides = _validate_blind_assignment_sides(
            key_path, session, trial, assignment, model_ids
        )
        if sides[0] == sides[1]:
            raise ModelListeningError(
                "Listening trial cannot compare a model with itself"
            )


def _validate_blind_assignment_sides(
    key_path: Path,
    session: _ValidatedListeningSession,
    trial: _ValidatedListeningTrial,
    assignment: BlindAssignment,
    model_ids: Sequence[str],
) -> tuple[str, str]:
    values: list[str] = []
    for side in ("a", "b"):
        value = assignment[side]
        if value["model_id"] not in model_ids:
            raise ModelListeningError("Listening session blind assignment is invalid")
        _validate_blind_assignment_audio(key_path, session, trial, side, value)
        values.append(value["model_id"])
    return values[0], values[1]


def _validate_blind_assignment_audio(
    key_path: Path,
    session: _ValidatedListeningSession,
    trial: _ValidatedListeningTrial,
    side: Literal["a", "b"],
    value: AssignmentArm,
) -> None:
    source = Path(value["source"] or "").expanduser()
    if session.get("schema") == SESSION_SCHEMA:
        expected_hash = value.get("audio_sha256")
        if not is_lowercase_sha256(expected_hash):
            raise ModelListeningError(
                "Listening session blind assignment audio hash is invalid"
            )
        if source.is_file():
            _verify_pcm_audio(source.resolve(), expected_hash, "blind source audio")
        if trial["audio_sha256"].get(side) != expected_hash:
            raise ModelListeningError(
                "Listening session alias and assignment hashes disagree"
            )
        return
    if source.is_file():
        alias = _within(
            key_path.parent,
            trial["audio"][side],
            "legacy blind audio alias",
        )
        _verify_pcm_audio(source.resolve(), sha256_file(alias), "blind source audio")


def next_pending_trial(session: Mapping[str, object]) -> ListeningTrial | None:
    trials = _object_list(session.get("trials"))
    if trials is None:
        raise ModelListeningError("Listening session trial count is invalid")
    for trial in trials:
        if _is_listening_trial(trial) and trial["rating"] is None:
            return _public_trial(trial)
    return None


def listening_progress(session: Mapping[str, object]) -> tuple[int, int]:
    trials = _object_list(session.get("trials"))
    if trials is None:
        raise ModelListeningError("Listening session trial count is invalid")
    return sum(
        _is_listening_trial(trial) and trial["rating"] is not None for trial in trials
    ), len(trials)


def record_trial_preference(
    session_path: PathInput,
    trial_id: str,
    preference: Preference,
    *,
    overwrite: bool = False,
    report_path: PathInput | None = None,
) -> ListeningSession:
    if preference not in {"a", "b", "tie", "neither"}:
        raise ModelListeningError("Preference must be a, b, tie, or neither")
    session_path = Path(session_path).expanduser().resolve()
    guard_path = session_path.with_name(f".{session_path.name}.guard")
    with exclusive_advisory_lock(guard_path, blocking=True):
        session = load_listening_session(session_path)
        if not _is_listening_session(session):
            raise ModelListeningError("Listening session is invalid")
        _load_blind_key(session_path, session)
        trial = next(
            (item for item in session["trials"] if item.get("trial_id") == trial_id),
            None,
        )
        if trial is None:
            raise ModelListeningError(f"Unknown listening trial: {trial_id}")
        if trial.get("rating") is not None and not overwrite:
            raise ModelListeningError(f"Listening trial is already rated: {trial_id}")
        rating: StoredTrialRating = {
            "preference": "tie" if preference == "neither" else preference,
            "reviewed_at": _utc_now(),
        }
        if preference == "neither":
            rating["acceptability"] = "neither"
        trial["rating"] = rating
        session["completed_count"] = listening_progress(session)[0]
        session["updated_at"] = _utc_now()
        atomic_write_json(session_path, session, sort_keys=True)
        if report_path is not None:
            try:
                aggregate_listening_report(session_path, report_path)
            except (ModelListeningError, OSError) as error:
                raise ModelListeningError(
                    "Preference was saved, but the listening report could not be "
                    "updated; run `vntts-listen report` to recover it"
                ) from error
    return _public_session(session)


def aggregate_listening_report(
    session_path: PathInput, output_path: PathInput | None = None
) -> dict[str, object]:
    session_path = Path(session_path).expanduser().resolve()
    session = load_listening_session(session_path)
    if not _is_listening_session(session):
        raise ModelListeningError("Listening session is invalid")
    key = _load_blind_key(session_path, session)
    fields = _report_fields(session, key)
    report = {
        "schema": (
            LEGACY_REPORT_SCHEMA
            if session.get("schema") == LEGACY_SESSION_SCHEMA
            else REPORT_SCHEMA
        ),
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "session": str(session_path),
        **fields,
    }
    if output_path is not None:
        atomic_write_json(output_path, report, sort_keys=True)
    return report


def ensure_listening_report(
    session_path: PathInput, output_path: PathInput | None = None
) -> ListeningReport:
    """Return a current report without rewriting an equivalent legacy snapshot."""
    session_path = Path(session_path).expanduser().resolve()
    output_path = Path(output_path or session_path.with_name("report.json")).resolve()
    session = load_listening_session(session_path)
    if not _is_listening_session(session):
        raise ModelListeningError("Listening session is invalid")
    key = _load_blind_key(session_path, session)
    expected = _report_fields(session, key)
    expected_schema = (
        LEGACY_REPORT_SCHEMA
        if session.get("schema") == LEGACY_SESSION_SCHEMA
        else REPORT_SCHEMA
    )
    if output_path.is_file():
        try:
            current = _load_schema(
                output_path,
                {expected_schema},
                "listening report",
            )
        except ModelListeningError:
            current = None
        if current is not None and all(
            current.get(field) == value for field, value in expected.items()
        ):
            if session.get("schema") == LEGACY_SESSION_SCHEMA or current.get(
                "session"
            ) == str(session_path):
                return _public_report(current)
    return _public_report(aggregate_listening_report(session_path, output_path))


def _report_fields(
    session: _ValidatedListeningSession, key: ListeningKey
) -> ReportFields:
    supports_acceptability = session.get("schema") == SESSION_SCHEMA
    assignments = {item["trial_id"]: item for item in key["assignments"]}
    stats = _model_stats(key["models"])
    pairwise = _pairwise_stats()
    for trial in session["trials"]:
        _record_report_trial(trial, assignments, stats, pairwise)
    models = _report_models(stats, supports_acceptability)
    completed, total = listening_progress(session)
    return {
        "complete": completed == total,
        "completed_trials": completed,
        "pending_trials": total - completed,
        "manual_selection_required": True,
        "models": models,
        "pairwise": _report_pairwise(pairwise, supports_acceptability),
    }


def _model_stats(models: Sequence[ListeningModel]) -> dict[str, ModelStats]:
    return {
        model["model_id"]: {
            "model_id": model["model_id"],
            "provider": model["provider"],
            "model": model["model"],
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "rejections": 0,
            "reviewed_trials": 0,
        }
        for model in models
    }


def _pairwise_stats() -> defaultdict[tuple[str, str], PairwiseStats]:
    return defaultdict(
        lambda: {
            "trials": 0,
            "left_wins": 0,
            "right_wins": 0,
            "ties": 0,
            "neither_acceptable": 0,
        }
    )


def _record_report_trial(
    trial: _ValidatedListeningTrial,
    assignments: Mapping[str, BlindAssignment],
    stats: dict[str, ModelStats],
    pairwise: defaultdict[tuple[str, str], PairwiseStats],
) -> None:
    rating = trial.get("rating")
    if rating is None:
        return
    assignment = assignments.get(trial["trial_id"])
    if assignment is None:
        raise ModelListeningError(f"Blind key is missing {trial['trial_id']}")
    side_models = {side: assignment[side]["model_id"] for side in ("a", "b")}
    _record_model_preference(stats, side_models, rating)
    _record_pairwise_preference(pairwise, side_models, rating)


def _record_model_preference(
    stats: dict[str, ModelStats],
    side_models: Mapping[str, str],
    rating: StoredTrialRating,
) -> None:
    for model_id in side_models.values():
        if model_id not in stats:
            raise ModelListeningError(
                f"Blind key references unknown model {model_id!r}"
            )
        stats[model_id]["reviewed_trials"] += 1
    preferred = rating["preference"]
    if rating.get("acceptability") == "neither":
        stats[side_models["a"]]["rejections"] += 1
        stats[side_models["b"]]["rejections"] += 1
    elif preferred == "tie":
        stats[side_models["a"]]["ties"] += 1
        stats[side_models["b"]]["ties"] += 1
    else:
        stats[side_models[preferred]]["wins"] += 1
        stats[side_models["b" if preferred == "a" else "a"]]["losses"] += 1


def _record_pairwise_preference(
    pairwise: defaultdict[tuple[str, str], PairwiseStats],
    side_models: Mapping[str, str],
    rating: StoredTrialRating,
) -> None:
    left, right = sorted(side_models.values())
    comparison = pairwise[(left, right)]
    comparison["trials"] += 1
    preferred = rating["preference"]
    if rating.get("acceptability") == "neither":
        comparison["neither_acceptable"] += 1
    elif preferred == "tie":
        comparison["ties"] += 1
    elif side_models[preferred] == left:
        comparison["left_wins"] += 1
    else:
        comparison["right_wins"] += 1


def _report_models(
    stats: Mapping[str, ModelStats], supports_acceptability: bool
) -> list[ReportModel]:
    models = [_report_model(value, supports_acceptability) for value in stats.values()]
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
    return models


def _report_model(value: ModelStats, supports_acceptability: bool) -> ReportModel:
    total = value["wins"] + value["losses"] + value["ties"]
    preference: PreferenceStats = {
        "wins": value["wins"],
        "losses": value["losses"],
        "ties": value["ties"],
        "rate": round((value["wins"] + 0.5 * value["ties"]) / total, 4)
        if total
        else None,
    }
    if supports_acceptability:
        preference["rejections"] = value["rejections"]
    return {
        "model_id": value["model_id"],
        "provider": value["provider"],
        "model": value["model"],
        "reviewed_trials": value["reviewed_trials"],
        "preference": preference,
    }


def _report_pairwise(
    pairwise: Mapping[tuple[str, str], PairwiseStats], supports_acceptability: bool
) -> list[dict[str, object]]:
    return [
        {
            "left_model": left,
            "right_model": right,
            **(
                values
                if supports_acceptability
                else {
                    field: value
                    for field, value in values.items()
                    if field != "neither_acceptable"
                }
            ),
        }
        for (left, right), values in sorted(pairwise.items())
    ]


def _load_model_report(path: PathInput) -> tuple[ModelReport, list[ModelReportSample]]:
    report = _load_schema(
        path, {MODEL_REPORT_SCHEMA, TTS_MODEL_REPORT_SCHEMA}, "model report"
    )
    parsed_report = _model_report_metadata(report, path)
    samples = report.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ModelListeningError(f"Model report samples are invalid: {path}")
    parsed: list[ModelReportSample] = []
    seen_ids: set[str] = set()
    report_root = Path(path).expanduser().resolve().parent
    for index, sample in enumerate(samples, start=1):
        parsed_sample = _parse_model_report_sample(sample, index, seen_ids, report_root)
        if parsed_sample is not None:
            parsed.append(parsed_sample)
    return parsed_report, parsed


def _model_report_metadata(
    report: Mapping[str, object], path: PathInput
) -> ModelReport:
    backend = report.get("backend")
    model_id = report.get("model_id")
    if not isinstance(backend, str) or not backend.strip():
        raise ModelListeningError(f"Model report backend is invalid: {path}")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ModelListeningError(f"Model report model_id is invalid: {path}")
    return {"backend": backend.strip(), "model_id": model_id.strip()}


def _parse_model_report_sample(
    sample: object,
    index: int,
    seen_ids: set[str],
    report_root: Path,
) -> ModelReportSample | None:
    if not _is_json_object(sample):
        raise ModelListeningError(f"Model report sample {index} must be an object")
    sample_id = _model_report_sample_id(sample, index, seen_ids)
    text_fields = _model_report_sample_text(sample, index)
    if text_fields is None:
        return None
    line_id, text, text_hash = text_fields
    return _model_report_sample_audio(
        sample, index, report_root, sample_id, line_id, text, text_hash
    )


def _model_report_sample_id(
    sample: Mapping[str, object], index: int, seen_ids: set[str]
) -> str:
    sample_id = sample.get("id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        raise ModelListeningError(f"Model report sample {index} id is invalid")
    if sample_id in seen_ids:
        raise ModelListeningError(f"Duplicate model report sample ID: {sample_id!r}")
    seen_ids.add(sample_id)
    return sample_id


def _model_report_sample_text(
    sample: Mapping[str, object], index: int
) -> tuple[str, str, str] | None:
    line_id = sample.get("line_id")
    text = sample.get("text")
    text_hash = sample.get("text_sha256")
    if not isinstance(line_id, str) or not line_id.strip():
        raise ModelListeningError(f"Model report sample {index} line_id is invalid")
    if not isinstance(text, str) or not text:
        raise ModelListeningError(f"Model report sample {index} text is invalid")
    if (
        not is_lowercase_sha256(text_hash)
        or hashlib.sha256(text.encode("utf-8")).hexdigest() != text_hash
    ):
        raise ModelListeningError(
            f"Model report sample {index} text_sha256 does not match exact text"
        )
    outcome = sample.get("outcome", "complete")
    if outcome not in {"complete", "limited", "cancelled", "error"}:
        raise ModelListeningError(f"Model report sample {index} outcome is invalid")
    if outcome != "complete":
        return None
    return line_id, text, text_hash


def _model_report_sample_audio(
    sample: Mapping[str, object],
    index: int,
    report_root: Path,
    sample_id: str,
    line_id: str,
    text: str,
    text_hash: str,
) -> ModelReportSample:
    audio_hash = sample.get("audio_sha256")
    if not isinstance(audio_hash, str) or not is_lowercase_sha256(audio_hash):
        raise ModelListeningError(
            f"Model report sample {index} audio_sha256 is invalid"
        )
    raw_audio = sample.get("audio")
    if not isinstance(raw_audio, str) or not raw_audio.strip():
        raise ModelListeningError(f"Model report sample {index} audio is invalid")
    audio = Path(raw_audio).expanduser()
    if not audio.is_absolute():
        audio = report_root / audio
    audio = audio.resolve()
    _verify_pcm_audio(audio, audio_hash, "model report audio")
    return {
        "id": sample_id.strip(),
        "line_id": line_id.strip(),
        "text": text,
        "text_sha256": text_hash,
        "audio_sha256": audio_hash,
        "resolved_audio": audio,
    }


def _verify_pcm_audio(path: PathInput, expected_hash: object, label: str) -> None:
    path = Path(path)
    if not path.is_file():
        raise ModelListeningError(f"{label.title()} is missing: {path}")
    try:
        _probe_supported_wav(path)
    except (OSError, ValueError, struct.error) as error:
        raise ModelListeningError(
            f"{label.title()} is not a supported WAV: {path}"
        ) from error
    if expected_hash is not None and (
        not is_lowercase_sha256(expected_hash) or sha256_file(path) != expected_hash
    ):
        raise ModelListeningError(f"{label.title()} checksum changed: {path}")


def _probe_supported_wav(path: PathInput) -> None:
    """Validate the PCM16 or legacy float32 WAV envelope without decoding."""
    with Path(path).open("rb") as stream:
        header = stream.read(12)
        if (
            len(header) != 12
            or header[:4] not in {b"RIFF", b"RF64"}
            or header[8:] != b"WAVE"
        ):
            raise ValueError("missing RIFF/WAVE header")
        format_fields = None
        data_size = None
        while chunk := stream.read(8):
            if len(chunk) != 8:
                raise ValueError("truncated WAV chunk")
            chunk_id, chunk_size = struct.unpack("<4sI", chunk)
            payload = stream.read(chunk_size)
            if len(payload) != chunk_size:
                raise ValueError("truncated WAV payload")
            if chunk_size % 2:
                stream.read(1)
            if chunk_id == b"fmt " and chunk_size >= 16:
                format_fields = struct.unpack("<HHIIHH", payload[:16])
            elif chunk_id == b"data":
                data_size = chunk_size
        if format_fields is None or not data_size:
            raise ValueError("missing WAV format or audio data")
        format_tag, channels, sample_rate, _byte_rate, _block_align, bits = (
            format_fields
        )
        if (
            channels not in {1, 2}
            or sample_rate <= 0
            or (format_tag, bits)
            not in {
                (1, 16),
                (3, 32),
            }
        ):
            raise ValueError("unsupported WAV encoding")


def _legacy_import_audio_hashes(root: PathInput) -> dict[str, str]:
    manifest_path = Path(root) / "import.json"
    if not manifest_path.is_file():
        return {}
    manifest = _load_json(manifest_path, "listening import manifest")
    if (
        manifest.get("schema") != "vntts.authoring-listening-import"
        or manifest.get("schema_version") != 1
        or _object_list(manifest.get("artifacts")) is None
    ):
        raise ModelListeningError("Unsupported listening import manifest schema")
    result: dict[str, str] = {}
    artifacts = _object_list(manifest.get("artifacts"))
    if artifacts is None:
        raise ModelListeningError("Unsupported listening import manifest schema")
    for raw_artifact in artifacts:
        if (
            not _is_json_object(raw_artifact)
            or raw_artifact.get("role") != "blind_audio"
        ):
            continue
        relative = raw_artifact.get("path")
        digest = raw_artifact.get("sha256")
        _within(root, relative, "imported blind audio")
        if (
            not isinstance(relative, str)
            or not isinstance(digest, str)
            or not is_lowercase_sha256(digest)
        ):
            raise ModelListeningError("Imported blind audio hash is invalid")
        result[relative] = digest
    return result


def _atomic_write_private_json(path: PathInput, value: object) -> Path:
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with atomic_output_path(path) as temporary:
        temporary.chmod(0o600)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(rendered)
        temporary.chmod(0o600)
    return path


def _within(root: PathInput, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise ModelListeningError(f"{label} must be a POSIX-relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ModelListeningError(f"{label} leaves the session directory")
    root = Path(root).resolve()
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ModelListeningError(f"{label} leaves the session directory") from error
    return candidate


def _load_schema(
    path: PathInput, schemas: set[str], description: str
) -> dict[str, object]:
    value = _load_json(path, description)
    if (
        value.get("schema") not in schemas
        or value.get("schema_version") != SCHEMA_VERSION
    ):
        raise ModelListeningError(f"Unsupported {description} schema")
    return value


def _load_json(path: PathInput, description: str) -> dict[str, object]:
    value: object = load_json_object(path, description, error_type=ModelListeningError)
    if not _is_json_object(value):
        raise ModelListeningError(f"{description.title()} must be a JSON object")
    return value


def _is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _object_list(value: object) -> list[object] | None:
    return value if isinstance(value, list) else None


def _is_trial_audio(value: object) -> TypeGuard[TrialAudio]:
    return _is_json_object(value) and all(
        isinstance(value.get(side), str) for side in ("a", "b")
    )


def _is_trial_rating(value: object) -> TypeGuard[StoredTrialRating]:
    return _is_json_object(value) and value.get("preference") in {"a", "b", "tie"}


def _is_listening_trial(value: object) -> TypeGuard[_ValidatedListeningTrial]:
    if not _is_json_object(value):
        return False
    return (
        (
            isinstance(value.get("trial_id"), str)
            and isinstance(value.get("queue_id"), str)
            and value.get("line_id") is None
            or isinstance(value.get("line_id"), str)
        )
        and (value.get("text") is None or isinstance(value.get("text"), str))
        and (
            value.get("text_sha256") is None
            or isinstance(value.get("text_sha256"), str)
        )
        and _is_trial_audio(value.get("audio"))
        and (value.get("rating") is None or _is_trial_rating(value.get("rating")))
        and ("audio_sha256" not in value or _is_trial_audio(value["audio_sha256"]))
    )


def _is_listening_session(
    value: Mapping[str, object],
) -> TypeGuard[_ValidatedListeningSession]:
    trials = _object_list(value.get("trials"))
    return (
        isinstance(value.get("schema"), str)
        and isinstance(value.get("schema_version"), int)
        and isinstance(value.get("source_kind"), str)
        and isinstance(value.get("source_sha256"), str)
        and isinstance(value.get("blind_key_sha256"), str)
        and isinstance(value.get("decision_mode"), str)
        and isinstance(value.get("trial_count"), int)
        and isinstance(value.get("completed_count"), int)
        and isinstance(value.get("updated_at"), str)
        and trials is not None
        and all(_is_listening_trial(trial) for trial in trials)
        and (
            "dimensions" not in value
            or all(
                isinstance(item, str)
                for item in _object_list(value["dimensions"]) or []
            )
        )
    )


def _is_assignment_arm(value: object) -> TypeGuard[AssignmentArm]:
    return (
        _is_json_object(value)
        and isinstance(value.get("model_id"), str)
        and isinstance(value.get("source"), str)
        and ("audio_sha256" not in value or isinstance(value["audio_sha256"], str))
    )


def _is_listening_key(value: dict[str, object]) -> TypeGuard[ListeningKey]:
    models = _object_list(value.get("models"))
    assignments = _object_list(value.get("assignments"))
    return (
        isinstance(value.get("source_kind"), str)
        and isinstance(value.get("source_sha256"), str)
        and models is not None
        and assignments is not None
        and all(
            _is_json_object(model)
            and isinstance(model.get("model_id"), str)
            and isinstance(model.get("provider"), str)
            and isinstance(model.get("model"), str)
            for model in models
        )
        and all(
            _is_json_object(assignment)
            and isinstance(assignment.get("trial_id"), str)
            and _is_assignment_arm(assignment.get("a"))
            and _is_assignment_arm(assignment.get("b"))
            for assignment in assignments
        )
    )


def _is_listening_cli(value: object) -> TypeGuard[_ListeningCli]:
    return callable(getattr(value, "main", None))


def _is_public_trial(value: object) -> TypeGuard[ListeningTrial]:
    return (
        _is_json_object(value)
        and isinstance(value.get("trial_id"), str)
        and isinstance(value.get("queue_id"), str)
        and _is_trial_audio(value.get("audio"))
    )


def _public_trial(value: object) -> ListeningTrial:
    if not _is_public_trial(value):
        raise ModelListeningError("Listening trial is invalid")
    return value


def _is_public_session(value: object) -> TypeGuard[ListeningSession]:
    trials = _object_list(value.get("trials")) if _is_json_object(value) else None
    return (
        _is_json_object(value)
        and trials is not None
        and all(_is_public_trial(trial) for trial in trials)
    )


def _public_session(value: object) -> ListeningSession:
    if not _is_public_session(value):
        raise ModelListeningError("Listening session is invalid")
    return value


def _is_public_report(value: object) -> TypeGuard[ListeningReport]:
    models = _object_list(value.get("models")) if _is_json_object(value) else None
    return (
        _is_json_object(value)
        and models is not None
        and all(
            _is_json_object(model) and isinstance(model.get("model_id"), str)
            for model in models
        )
    )


def _public_report(value: object) -> ListeningReport:
    if not _is_public_report(value):
        raise ModelListeningError("Listening report is invalid")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    """Compatibility bridge for already-installed legacy entry points."""
    cli: object = importlib.import_module("vntts.authoring.listening_cli")
    if not _is_listening_cli(cli):
        raise ModelListeningError("Listening CLI is unavailable")
    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
