"""Generic blind, resumable same-text A/B model listening."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import random
import re
import shutil
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.cli import cli_error, cli_success
from vntts.settings import get_local_data_directory

SESSION_SCHEMA = "vntts.model-listening-session"
KEY_SCHEMA = "vntts.model-listening-key"
REPORT_SCHEMA = "vntts.model-listening-report"
LEGACY_SESSION_SCHEMA = "r1999.model-listening-session"
LEGACY_KEY_SCHEMA = "r1999.model-listening-key"
LEGACY_REPORT_SCHEMA = "r1999.model-listening-report"
SCHEMA_VERSION = 1
LEGACY_DIMENSIONS = ("timbre", "accent", "naturalness", "pronunciation")
default_session_directory = (
    get_local_data_directory() / "authoring" / "model-listening"
)


class ModelListeningError(RuntimeError):
    """A listening session is invalid or cannot be updated safely."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _normalized_text(text):
    normalized = unicodedata.normalize("NFKC", str(text)).replace("…", "...")
    return re.sub(r"\s+", " ", normalized).strip()


def _source_digest(paths):
    sources = [
        {"path": str(Path(path).expanduser().resolve()), "sha256": sha256_file(path)}
        for path in paths
    ]
    payload = json.dumps(sources, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return sources, hashlib.sha256(payload).hexdigest()


def _link_blind_audio(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def create_listening_session_from_reports(report_paths, output_directory, *, seed=0):
    """Create blind trials from two or more generic per-model reports."""
    resolved_paths = [Path(path).expanduser().resolve() for path in report_paths]
    if len(resolved_paths) < 2:
        raise ModelListeningError("At least two model reports are required")
    model_metadata = {}
    audio_by_model = defaultdict(dict)
    corpus_items = {}
    for report_path in resolved_paths:
        report = _load_json(report_path, "model report")
        samples = report.get("samples")
        backend = str(report.get("backend") or report.get("provider") or "").strip()
        if not backend or not isinstance(samples, list) or not samples:
            raise ModelListeningError(
                f"Model report is missing backend samples: {report_path}"
            )
        model_id = str(report.get("model_id") or "").strip()
        if not model_id:
            label = str(report.get("label") or "").strip()
            language = str(report.get("language") or "").strip()
            variant = " / ".join(part for part in (backend, language, label) if part)
            model_id = f"legacy/{variant}"
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
        for index, sample in enumerate(samples, start=1):
            if not isinstance(sample, dict):
                raise ModelListeningError(f"Model report sample is invalid: {report_path}")
            text = str(sample.get("text") or "").strip()
            normalized = _normalized_text(text)
            audio = Path(str(sample.get("audio") or "")).expanduser().resolve()
            if not normalized or not audio.is_file():
                raise ModelListeningError(
                    f"Model report sample text or audio is missing: {report_path}"
                )
            text_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            sample_id = str(sample.get("id") or sample.get("line_id") or "").strip()
            identity = sample_id or text_hash
            queue_id = f"corpus:{identity}:{text_hash[:16]}"
            existing = audio_by_model[model_id].get(queue_id)
            if existing is not None and existing != audio:
                raise ModelListeningError(
                    f"Model {model_id} has multiple outputs for sample {identity!r}"
                )
            audio_by_model[model_id][queue_id] = audio
            current = corpus_items.get(queue_id)
            item = {
                "queue_id": queue_id,
                "line_id": sample_id or f"sample-{index}",
                "text_sha256": text_hash,
                "text": text,
            }
            if current is not None and (
                current["text_sha256"] != text_hash
                or _normalized_text(current["text"]) != normalized
            ):
                raise ModelListeningError(
                    f"Model reports disagree on shared sample {identity!r}"
                )
            corpus_items.setdefault(queue_id, item)
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


def create_listening_session(benchmark_path, output_directory, *, seed=0):
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
    resolved = []
    for value in reports:
        if not isinstance(value, str) or not value.strip():
            raise ModelListeningError("Model benchmark report paths are invalid")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = benchmark_path.parent / path
        resolved.append(path.resolve())
    return create_listening_session_from_reports(resolved, output_directory, seed=seed)


def _write_listening_session(
    output_directory,
    models,
    audio_by_model,
    corpus_items,
    *,
    sources,
    source_sha256,
    seed,
):
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
    pairs = []
    for item in corpus_items:
        queue_id = item["queue_id"]
        available = [model_id for model_id in model_ids if queue_id in audio_by_model[model_id]]
        for left, right in itertools.combinations(available, 2):
            pairs.append((item, left, right))
    if not pairs:
        raise ModelListeningError("No same-text samples are shared by two models")
    generator = random.Random(seed)
    generator.shuffle(pairs)
    output_directory.mkdir(parents=True, exist_ok=True)
    trials = []
    assignments = []
    for index, (item, left, right) in enumerate(pairs, start=1):
        sides = [left, right]
        generator.shuffle(sides)
        trial_id = f"trial-{index:04d}"
        aliases = {
            side: Path("audio") / f"{trial_id}-{side}.wav" for side in ("a", "b")
        }
        for side, model_id in zip(("a", "b"), sides, strict=True):
            _link_blind_audio(
                audio_by_model[model_id][item["queue_id"]],
                output_directory / aliases[side],
            )
        trials.append(
            {
                "trial_id": trial_id,
                **item,
                "audio": {side: path.as_posix() for side, path in aliases.items()},
                "rating": None,
            }
        )
        assignments.append(
            {
                "trial_id": trial_id,
                "a": {
                    "model_id": sides[0],
                    "source": str(audio_by_model[sides[0]][item["queue_id"]]),
                },
                "b": {
                    "model_id": sides[1],
                    "source": str(audio_by_model[sides[1]][item["queue_id"]]),
                },
            }
        )
    key_path = output_directory / ".blind-key.json"
    key = {
        "schema": KEY_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_at": _utc_now(),
        "source_kind": "model-reports",
        "source_sha256": source_sha256,
        "sources": sources,
        "models": models,
        "assignments": assignments,
    }
    atomic_write_json(key_path, key, sort_keys=True)
    key_path.chmod(0o600)
    session = {
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


def load_listening_session(path):
    path = Path(path).expanduser().resolve()
    session = _load_schema(
        path,
        {SESSION_SCHEMA, LEGACY_SESSION_SCHEMA},
        "listening session",
    )
    trials = session.get("trials")
    if not isinstance(trials, list) or session.get("trial_count") != len(trials):
        raise ModelListeningError("Listening session trial count is invalid")
    if session.get("decision_mode") != "preference-only" and session.get(
        "dimensions"
    ) != list(LEGACY_DIMENSIONS):
        raise ModelListeningError("Listening session decision mode is invalid")
    trial_ids = [trial.get("trial_id") for trial in trials if isinstance(trial, dict)]
    if len(trial_ids) != len(trials) or len(set(trial_ids)) != len(trials):
        raise ModelListeningError("Listening session trial IDs are invalid")
    completed, _total = listening_progress(session)
    if session.get("completed_count") != completed:
        raise ModelListeningError("Listening session progress is inconsistent")
    for trial in trials:
        rating = trial.get("rating")
        if rating is not None and (
            not isinstance(rating, dict) or rating.get("preference") not in {"a", "b", "tie"}
        ):
            raise ModelListeningError(f"Listening trial rating is invalid: {trial['trial_id']}")
        audio = trial.get("audio")
        if not isinstance(audio, dict) or set(audio) != {"a", "b"}:
            raise ModelListeningError(f"Listening trial audio is invalid: {trial['trial_id']}")
        for relative in audio.values():
            candidate = _within(path.parent, relative, "listening trial audio")
            if not candidate.is_file():
                raise ModelListeningError(f"Listening trial audio is missing: {candidate}")
    return session


def _load_blind_key(session_path, session):
    key_path = Path(session_path).expanduser().resolve().with_name(".blind-key.json")
    if not key_path.is_file() or sha256_file(key_path) != session.get("blind_key_sha256"):
        raise ModelListeningError("Listening session blind key is missing or changed")
    expected_schema = (
        LEGACY_KEY_SCHEMA if session.get("schema") == LEGACY_SESSION_SCHEMA else KEY_SCHEMA
    )
    key = _load_schema(key_path, {expected_schema}, "listening key")
    if key.get("source_kind") != session.get("source_kind") or key.get(
        "source_sha256"
    ) != session.get("source_sha256"):
        raise ModelListeningError("Listening session source identity changed")
    models = key.get("models")
    assignments = key.get("assignments")
    if not isinstance(models, list) or not isinstance(assignments, list):
        raise ModelListeningError("Listening session blind key is invalid")
    model_ids = [
        model.get("model_id") for model in models if isinstance(model, dict)
    ]
    if len(model_ids) != len(models) or len(model_ids) != len(set(model_ids)):
        raise ModelListeningError("Listening session blind key models are invalid")
    assignment_ids = [
        item.get("trial_id") for item in assignments if isinstance(item, dict)
    ]
    trial_ids = [trial["trial_id"] for trial in session["trials"]]
    if len(assignment_ids) != len(assignments) or sorted(assignment_ids) != sorted(trial_ids):
        raise ModelListeningError("Listening session blind assignments are incomplete")
    for assignment in assignments:
        sides = []
        for side in ("a", "b"):
            value = assignment.get(side)
            if not isinstance(value, dict) or value.get("model_id") not in model_ids:
                raise ModelListeningError("Listening session blind assignment is invalid")
            sides.append(value["model_id"])
        if sides[0] == sides[1]:
            raise ModelListeningError("Listening trial cannot compare a model with itself")
    return key


def next_pending_trial(session):
    return next((trial for trial in session["trials"] if trial.get("rating") is None), None)


def listening_progress(session):
    completed = sum(trial.get("rating") is not None for trial in session["trials"])
    return completed, len(session["trials"])


def record_trial_preference(session_path, trial_id, preference, *, overwrite=False):
    if preference not in {"a", "b", "tie"}:
        raise ModelListeningError("Preference must be a, b, or tie")
    session_path = Path(session_path).expanduser().resolve()
    session = load_listening_session(session_path)
    _load_blind_key(session_path, session)
    trial = next((item for item in session["trials"] if item.get("trial_id") == trial_id), None)
    if trial is None:
        raise ModelListeningError(f"Unknown listening trial: {trial_id}")
    if trial.get("rating") is not None and not overwrite:
        raise ModelListeningError(f"Listening trial is already rated: {trial_id}")
    trial["rating"] = {"preference": preference, "reviewed_at": _utc_now()}
    session["completed_count"] = listening_progress(session)[0]
    session["updated_at"] = _utc_now()
    atomic_write_json(session_path, session, sort_keys=True)
    return session


def aggregate_listening_report(session_path, output_path=None):
    session_path = Path(session_path).expanduser().resolve()
    session = load_listening_session(session_path)
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


def ensure_listening_report(session_path, output_path=None):
    """Return a current report without rewriting an equivalent legacy snapshot."""
    session_path = Path(session_path).expanduser().resolve()
    output_path = Path(output_path or session_path.with_name("report.json")).resolve()
    session = load_listening_session(session_path)
    key = _load_blind_key(session_path, session)
    expected = _report_fields(session, key)
    if output_path.is_file():
        try:
            current = _load_schema(
                output_path,
                {REPORT_SCHEMA, LEGACY_REPORT_SCHEMA},
                "listening report",
            )
        except ModelListeningError:
            current = None
        if current is not None and all(current.get(field) == value for field, value in expected.items()):
            return current
    return aggregate_listening_report(session_path, output_path)


def _report_fields(session, key):
    assignments = {item["trial_id"]: item for item in key["assignments"]}
    stats = {
        model["model_id"]: {
            "model_id": model["model_id"],
            "provider": model["provider"],
            "model": model["model"],
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "reviewed_trials": 0,
        }
        for model in key["models"]
    }
    pairwise = defaultdict(
        lambda: {"trials": 0, "left_wins": 0, "right_wins": 0, "ties": 0}
    )
    for trial in session["trials"]:
        rating = trial.get("rating")
        if rating is None:
            continue
        assignment = assignments.get(trial["trial_id"])
        if assignment is None:
            raise ModelListeningError(f"Blind key is missing {trial['trial_id']}")
        side_models = {side: assignment[side]["model_id"] for side in ("a", "b")}
        for model_id in side_models.values():
            if model_id not in stats:
                raise ModelListeningError(f"Blind key references unknown model {model_id!r}")
            stats[model_id]["reviewed_trials"] += 1
        preferred = rating["preference"]
        if preferred == "tie":
            stats[side_models["a"]]["ties"] += 1
            stats[side_models["b"]]["ties"] += 1
        else:
            stats[side_models[preferred]]["wins"] += 1
            stats[side_models["b" if preferred == "a" else "a"]]["losses"] += 1
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
        total = value["wins"] + value["losses"] + value["ties"]
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
                    "rate": round((value["wins"] + 0.5 * value["ties"]) / total, 4)
                    if total
                    else None,
                },
            }
        )
    models.sort(
        key=lambda item: (
            -(item["preference"]["rate"] if item["preference"]["rate"] is not None else -1),
            -item["preference"]["wins"],
            item["model_id"],
        )
    )
    for rank, model in enumerate(models, start=1):
        model["rank"] = rank
    completed, total = listening_progress(session)
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


def _within(root, value, label):
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


def _load_schema(path, schemas, description):
    value = _load_json(path, description)
    if value.get("schema") not in schemas or value.get("schema_version") != SCHEMA_VERSION:
        raise ModelListeningError(f"Unsupported {description} schema")
    return value


def _load_json(path, description):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelListeningError(f"Unable to read {description} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ModelListeningError(f"{description.title()} must be a JSON object")
    return value


def create_parser():
    parser = argparse.ArgumentParser(description="Run blind, resumable model listening")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--benchmark", type=Path, required=True)
    start.add_argument("--output", type=Path, default=default_session_directory)
    start.add_argument("--seed", type=int, default=0)
    start_reports = subparsers.add_parser("start-reports")
    start_reports.add_argument("--reports", type=Path, nargs="+", required=True)
    start_reports.add_argument("--output", type=Path, default=default_session_directory)
    start_reports.add_argument("--seed", type=int, default=0)
    for command in ("status", "next", "report", "ui"):
        child = subparsers.add_parser(command)
        child.add_argument(
            "--session", type=Path, default=default_session_directory / "session.json"
        )
        if command == "report":
            child.add_argument("--output", type=Path)
    score = subparsers.add_parser("score")
    score.add_argument("trial_id")
    score.add_argument(
        "--session", type=Path, default=default_session_directory / "session.json"
    )
    score.add_argument("--preference", choices=("a", "b", "tie"), required=True)
    score.add_argument("--overwrite", action="store_true")
    return parser


def main(argv=None):
    options = create_parser().parse_args(argv)
    try:
        if options.command == "start":
            path = create_listening_session(
                options.benchmark, options.output, seed=options.seed
            )
            return cli_success(f"Created blind listening session: {path}")
        if options.command == "start-reports":
            path = create_listening_session_from_reports(
                options.reports, options.output, seed=options.seed
            )
            return cli_success(f"Created blind listening session: {path}")
        if options.command == "ui":
            from vntts.authoring.listening_ui import launch_listening_workbench

            return launch_listening_workbench(options.session)
        session = load_listening_session(options.session)
        if options.command == "status":
            completed, total = listening_progress(session)
            return cli_success(f"Listening progress: {completed}/{total} trials")
        if options.command == "next":
            trial = next_pending_trial(session)
            if trial is None:
                return cli_success("Listening session is complete")
            print(json.dumps(trial, ensure_ascii=False, indent=2))
            return 0
        if options.command == "score":
            updated = record_trial_preference(
                options.session,
                options.trial_id,
                options.preference,
                overwrite=options.overwrite,
            )
            aggregate_listening_report(
                options.session, Path(options.session).resolve().with_name("report.json")
            )
            completed, total = listening_progress(updated)
            return cli_success(f"Saved {options.trial_id}; progress: {completed}/{total}")
        output = options.output or Path(options.session).resolve().with_name("report.json")
        report = aggregate_listening_report(options.session, output)
        return cli_success(
            f"Listening report: {output} ({report['completed_trials']} completed, "
            f"{report['pending_trials']} pending)"
        )
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("PySide6"):
            return cli_error("Qt UI is not installed")
        raise
    except (ModelListeningError, OSError, json.JSONDecodeError) as error:
        return cli_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
