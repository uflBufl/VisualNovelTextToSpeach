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
import stat
import struct
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts.atomic_io import atomic_output_path, atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.cli import cli_error, cli_success
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
            text = sample["text"]
            normalized = _normalized_text(text)
            audio = sample["resolved_audio"]
            text_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            sample_id = sample["id"]
            identity = sample_id
            queue_id = f"corpus:{identity}:{text_hash[:16]}"
            existing = audio_by_model[model_id].get(queue_id)
            audio_record = {"path": audio, "sha256": sample["audio_sha256"]}
            if existing is not None and existing != audio_record:
                raise ModelListeningError(
                    f"Model {model_id} has multiple outputs for sample {identity!r}"
                )
            audio_by_model[model_id][queue_id] = audio_record
            current = corpus_items.get(queue_id)
            item = {
                "queue_id": queue_id,
                "line_id": sample["line_id"],
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
                audio_by_model[model_id][item["queue_id"]]["path"],
                output_directory / aliases[side],
            )
            _verify_pcm_audio(
                output_directory / aliases[side],
                audio_by_model[model_id][item["queue_id"]]["sha256"],
                "blind audio alias",
            )
        trials.append(
            {
                "trial_id": trial_id,
                **item,
                "audio": {side: path.as_posix() for side, path in aliases.items()},
                "audio_sha256": {
                    side: audio_by_model[model_id][item["queue_id"]]["sha256"]
                    for side, model_id in zip(("a", "b"), sides, strict=True)
                },
                "rating": None,
            }
        )
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
    _atomic_write_private_json(key_path, key)
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
    current_schema = session.get("schema") == SESSION_SCHEMA
    legacy_audio_hashes = (
        {} if current_schema else _legacy_import_audio_hashes(path.parent)
    )
    trial_ids = [trial.get("trial_id") for trial in trials if isinstance(trial, dict)]
    if len(trial_ids) != len(trials) or len(set(trial_ids)) != len(trials):
        raise ModelListeningError("Listening session trial IDs are invalid")
    completed, _total = listening_progress(session)
    if session.get("completed_count") != completed:
        raise ModelListeningError("Listening session progress is inconsistent")
    for trial in trials:
        if current_schema:
            line_id = trial.get("line_id")
            text = trial.get("text")
            text_hash = trial.get("text_sha256")
            if not isinstance(line_id, str) or not line_id.strip():
                raise ModelListeningError(
                    f"Listening trial line identity is invalid: {trial['trial_id']}"
                )
            if not isinstance(text, str) or not text or not _is_sha256(text_hash):
                raise ModelListeningError(
                    f"Listening trial text identity is invalid: {trial['trial_id']}"
                )
            if (
                hashlib.sha256(_normalized_text(text).encode("utf-8")).hexdigest()
                != text_hash
            ):
                raise ModelListeningError(
                    f"Listening trial text hash changed: {trial['trial_id']}"
                )
        rating = trial.get("rating")
        if rating is not None and (
            not isinstance(rating, dict) or rating.get("preference") not in {"a", "b", "tie"}
        ):
            raise ModelListeningError(f"Listening trial rating is invalid: {trial['trial_id']}")
        audio = trial.get("audio")
        if not isinstance(audio, dict) or set(audio) != {"a", "b"}:
            raise ModelListeningError(f"Listening trial audio is invalid: {trial['trial_id']}")
        expected_hashes = trial.get("audio_sha256")
        if current_schema and (
            not isinstance(expected_hashes, dict) or set(expected_hashes) != {"a", "b"}
        ):
            raise ModelListeningError(
                f"Listening trial audio hashes are invalid: {trial['trial_id']}"
            )
        for side, relative in audio.items():
            candidate = _within(path.parent, relative, "listening trial audio")
            if not candidate.is_file():
                raise ModelListeningError(f"Listening trial audio is missing: {candidate}")
            expected_hash = (
                expected_hashes.get(side)
                if isinstance(expected_hashes, dict)
                else legacy_audio_hashes.get(relative)
            )
            _verify_pcm_audio(candidate, expected_hash, "listening trial audio")
    _load_blind_key(path, session)
    return session


def _load_blind_key(session_path, session):
    key_path = Path(session_path).expanduser().resolve().with_name(".blind-key.json")
    if key_path.is_file() and stat.S_IMODE(key_path.stat().st_mode) != 0o600:
        raise ModelListeningError("Listening session blind key mode must be 0600")
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
        trial = next(
            item
            for item in session["trials"]
            if item["trial_id"] == assignment["trial_id"]
        )
        sides = []
        for side in ("a", "b"):
            value = assignment.get(side)
            if not isinstance(value, dict) or value.get("model_id") not in model_ids:
                raise ModelListeningError("Listening session blind assignment is invalid")
            if session.get("schema") == SESSION_SCHEMA:
                expected_hash = value.get("audio_sha256")
                if not _is_sha256(expected_hash):
                    raise ModelListeningError(
                        "Listening session blind assignment audio hash is invalid"
                    )
                source = Path(str(value.get("source") or "")).expanduser()
                if source.is_file():
                    _verify_pcm_audio(source.resolve(), expected_hash, "blind source audio")
                if trial["audio_sha256"].get(side) != expected_hash:
                    raise ModelListeningError(
                        "Listening session alias and assignment hashes disagree"
                    )
            else:
                source = Path(str(value.get("source") or "")).expanduser()
                if source.is_file():
                    alias = _within(
                        key_path.parent,
                        trial["audio"][side],
                        "legacy blind audio alias",
                    )
                    _verify_pcm_audio(
                        source.resolve(), sha256_file(alias), "blind source audio"
                    )
            sides.append(value["model_id"])
        if sides[0] == sides[1]:
            raise ModelListeningError("Listening trial cannot compare a model with itself")
    return key


def next_pending_trial(session):
    return next((trial for trial in session["trials"] if trial.get("rating") is None), None)


def listening_progress(session):
    completed = sum(trial.get("rating") is not None for trial in session["trials"])
    return completed, len(session["trials"])


def record_trial_preference(
    session_path,
    trial_id,
    preference,
    *,
    overwrite=False,
    report_path=None,
):
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
    if report_path is not None:
        try:
            aggregate_listening_report(session_path, report_path)
        except (ModelListeningError, OSError) as error:
            raise ModelListeningError(
                "Preference was saved, but the listening report could not be updated; "
                "run `vntts-listen report` to recover it"
            ) from error
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


def _load_model_report(path):
    report = _load_schema(
        path, {MODEL_REPORT_SCHEMA, TTS_MODEL_REPORT_SCHEMA}, "model report"
    )
    backend = report.get("backend")
    model_id = report.get("model_id")
    samples = report.get("samples")
    if not isinstance(backend, str) or not backend.strip():
        raise ModelListeningError(f"Model report backend is invalid: {path}")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ModelListeningError(f"Model report model_id is invalid: {path}")
    if not isinstance(samples, list) or not samples:
        raise ModelListeningError(f"Model report samples are invalid: {path}")
    parsed = []
    seen_ids = set()
    report_root = Path(path).expanduser().resolve().parent
    for index, sample in enumerate(samples, start=1):
        if not isinstance(sample, dict):
            raise ModelListeningError(f"Model report sample {index} must be an object")
        sample_id = sample.get("id")
        line_id = sample.get("line_id")
        text = sample.get("text")
        text_hash = sample.get("text_sha256")
        audio_hash = sample.get("audio_sha256")
        if not isinstance(sample_id, str) or not sample_id.strip():
            raise ModelListeningError(f"Model report sample {index} id is invalid")
        if sample_id in seen_ids:
            raise ModelListeningError(f"Duplicate model report sample ID: {sample_id!r}")
        seen_ids.add(sample_id)
        if not isinstance(line_id, str) or not line_id.strip():
            raise ModelListeningError(f"Model report sample {index} line_id is invalid")
        if not isinstance(text, str) or not text:
            raise ModelListeningError(f"Model report sample {index} text is invalid")
        if not _is_sha256(text_hash) or hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest() != text_hash:
            raise ModelListeningError(
                f"Model report sample {index} text_sha256 does not match exact text"
            )
        if not _is_sha256(audio_hash):
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
        parsed.append(
            {
                **sample,
                "id": sample_id.strip(),
                "line_id": line_id.strip(),
                "text": text,
                "text_sha256": text_hash,
                "audio_sha256": audio_hash,
                "resolved_audio": audio,
            }
        )
    return {
        **report,
        "backend": backend.strip(),
        "model_id": model_id.strip(),
    }, parsed


def _verify_pcm_audio(path, expected_hash, label):
    path = Path(path)
    if not path.is_file():
        raise ModelListeningError(f"{label.title()} is missing: {path}")
    try:
        _probe_supported_wav(path)
    except (OSError, ValueError, struct.error) as error:
        raise ModelListeningError(f"{label.title()} is not a supported WAV: {path}") from error
    if expected_hash is not None and (
        not _is_sha256(expected_hash) or sha256_file(path) != expected_hash
    ):
        raise ModelListeningError(f"{label.title()} checksum changed: {path}")


def _probe_supported_wav(path):
    """Validate the PCM16 or legacy float32 WAV envelope without decoding."""
    with Path(path).open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] not in {b"RIFF", b"RF64"} or header[8:] != b"WAVE":
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
        format_tag, channels, sample_rate, _byte_rate, _block_align, bits = format_fields
        if channels not in {1, 2} or sample_rate <= 0 or (format_tag, bits) not in {
            (1, 16),
            (3, 32),
        }:
            raise ValueError("unsupported WAV encoding")


def _legacy_import_audio_hashes(root):
    manifest_path = Path(root) / "import.json"
    if not manifest_path.is_file():
        return {}
    manifest = _load_json(manifest_path, "listening import manifest")
    if (
        manifest.get("schema") != "vntts.authoring-listening-import"
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("artifacts"), list)
    ):
        raise ModelListeningError("Unsupported listening import manifest schema")
    result = {}
    for artifact in manifest["artifacts"]:
        if not isinstance(artifact, dict) or artifact.get("role") != "blind_audio":
            continue
        relative = artifact.get("path")
        digest = artifact.get("sha256")
        _within(root, relative, "imported blind audio")
        if not _is_sha256(digest):
            raise ModelListeningError("Imported blind audio hash is invalid")
        result[relative] = digest
    return result


def _atomic_write_private_json(path, value):
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


def _is_sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


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
                report_path=Path(options.session).resolve().with_name("report.json"),
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
