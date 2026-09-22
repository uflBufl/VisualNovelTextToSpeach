"""Checksum-bound blind comparison for experimental internal-silence compression."""

from __future__ import annotations

import hashlib
import io
import json
import math
import shutil
import wave
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.failure_repair import (
    DEFAULT_INTERNAL_SILENCE_TARGET_SECONDS,
    compress_single_sentence_boundary_silence,
)
from vntts.authoring.listening import (
    ModelListeningError,
    create_listening_session_from_reports,
)
from vntts.authoring.publication import rename_directory_no_replace, staged_directory
from vntts.authoring.workspace_foundation import contained_regular_file
from vntts.document_identity import is_lowercase_sha256

SILENCE_COMPARISON_SCHEMA = "vntts.authoring-silence-comparison"
SILENCE_COMPARISON_VERSION = 1
SILENCE_COMPARISON_INPUT_SCHEMA = "vntts.authoring-silence-comparison-input"
SILENCE_COMPARISON_INPUT_VERSION = 1


class SilenceComparisonError(RuntimeError):
    """A silence-compression comparison cannot be published or trusted."""


@dataclass(frozen=True)
class SilenceComparisonSample:
    queue_id: str
    line_id: str
    text: str
    raw_audio: Path
    segmented_audio: Path
    raw_audio_sha256: str | None = None
    segmented_audio_sha256: str | None = None


@dataclass(frozen=True)
class SilenceComparisonResult:
    directory: Path
    sample_count: int
    report_paths: tuple[Path, Path]


@dataclass(frozen=True)
class SilenceComparisonInputPlan:
    path: Path
    sha256: str
    samples: tuple[SilenceComparisonSample, ...]


def load_silence_comparison_input_plan(
    path: str | Path,
) -> SilenceComparisonInputPlan:
    """Load an exact, checksum-bound operator plan without changing its sources."""
    source, payload, document = _read_silence_comparison_input_plan(path)
    records = _validate_silence_comparison_input_document(document)
    samples = _silence_comparison_input_samples(records, source.parent)
    return SilenceComparisonInputPlan(
        source,
        hashlib.sha256(payload).hexdigest(),
        tuple(samples),
    )


def _read_silence_comparison_input_plan(
    path: str | Path,
) -> tuple[Path, bytes, object]:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise SilenceComparisonError("Silence comparison input plan is a symlink")
    source = source.resolve()
    try:
        payload = source.read_bytes()
        document = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise SilenceComparisonError(
            f"Unable to read silence comparison input plan: {error}"
        ) from error
    return source, payload, document


def _validate_silence_comparison_input_document(document: object) -> list[object]:
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "schema_version",
        "samples",
    }:
        raise SilenceComparisonError("Silence comparison input plan is malformed")
    if (
        document["schema"] != SILENCE_COMPARISON_INPUT_SCHEMA
        or not isinstance(document["schema_version"], int)
        or isinstance(document["schema_version"], bool)
        or document["schema_version"] != SILENCE_COMPARISON_INPUT_VERSION
    ):
        raise SilenceComparisonError("Unsupported silence comparison input schema")
    records = document["samples"]
    if not isinstance(records, list) or not records:
        raise SilenceComparisonError(
            "Silence comparison input plan requires at least one sample"
        )
    return records


def _silence_comparison_input_samples(
    records: Iterable[object], root: Path
) -> list[SilenceComparisonSample]:
    required_fields = {
        "queue_id",
        "line_id",
        "text",
        "text_sha256",
        "raw_audio",
        "raw_audio_sha256",
        "segmented_audio",
        "segmented_audio_sha256",
    }
    samples: list[SilenceComparisonSample] = []
    queue_ids = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != required_fields:
            raise SilenceComparisonError("Silence comparison input sample is malformed")
        text = record["text"]
        if (
            not isinstance(text, str)
            or not text
            or text != text.strip()
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != record["text_sha256"]
        ):
            raise SilenceComparisonError(
                "Silence comparison input text identity is invalid"
            )
        raw_path = _planned_audio_path(root, record["raw_audio"], "raw")
        segmented_path = _planned_audio_path(
            root, record["segmented_audio"], "segmented"
        )
        raw_sha256 = _validate_planned_audio(
            raw_path, record["raw_audio_sha256"], "raw"
        )
        segmented_sha256 = _validate_planned_audio(
            segmented_path, record["segmented_audio_sha256"], "segmented"
        )
        if raw_sha256 == segmented_sha256:
            raise SilenceComparisonError(
                "Raw and segmented comparison audio must be different"
            )
        sample = _validate_sample(
            SilenceComparisonSample(
                record["queue_id"],
                record["line_id"],
                text,
                raw_path,
                segmented_path,
                raw_sha256,
                segmented_sha256,
            )
        )
        if sample.queue_id in queue_ids:
            raise SilenceComparisonError(
                "Silence comparison input queue IDs must be unique"
            )
        queue_ids.add(sample.queue_id)
        samples.append(sample)
    return samples


def publish_silence_comparison(
    samples: Iterable[SilenceComparisonSample],
    output_directory: str | Path,
    *,
    target_seconds: float = DEFAULT_INTERNAL_SILENCE_TARGET_SECONDS,
    input_plan_sha256: str | None = None,
) -> SilenceComparisonResult:
    """Publish immutable segmentation/compression reports for later blind review."""
    values = _publishable_silence_comparison_samples(samples, input_plan_sha256)
    output = _silence_comparison_output_directory(output_directory)
    output.parent.mkdir(parents=True, exist_ok=True)
    with staged_directory(output.parent, prefix=f".{output.name}.staging-") as staging:
        staged = _stage_silence_comparison_samples(values, staging, target_seconds)
        reports = _write_silence_comparison_reports(staging, staged)
        document = _silence_comparison_document(
            staging, staged.records, reports, target_seconds, input_plan_sha256
        )
        atomic_write_json(staging / "comparison.json", document, sort_keys=True)
        _validate_silence_comparison_staging(reports, staging, staged.checked_sources)
        rename_directory_no_replace(staging, output)
        return SilenceComparisonResult(
            output,
            len(staged.records),
            (
                output / "reports/sentence-segmentation.json",
                output / "reports/silence-compression.json",
            ),
        )


@dataclass(frozen=True)
class _StagedSilenceComparison:
    records: list[dict[str, object]]
    segmented_report_samples: list[dict[str, object]]
    compressed_report_samples: list[dict[str, object]]
    checked_sources: list[tuple[Path, str, str]]


def _publishable_silence_comparison_samples(
    samples: Iterable[SilenceComparisonSample], input_plan_sha256: str | None
) -> tuple[SilenceComparisonSample, ...]:
    if input_plan_sha256 is not None and not is_lowercase_sha256(input_plan_sha256):
        raise SilenceComparisonError(
            "Silence comparison input plan checksum is invalid"
        )
    values = tuple(_validate_sample(value) for value in samples)
    if not values:
        raise SilenceComparisonError("Silence comparison requires at least one sample")
    queue_ids = [value.queue_id for value in values]
    if len(set(queue_ids)) != len(queue_ids):
        raise SilenceComparisonError("Silence comparison queue IDs must be unique")
    return values


def _silence_comparison_output_directory(output_directory: str | Path) -> Path:
    output = _new_directory(output_directory)
    if output.exists() or output.is_symlink():
        raise SilenceComparisonError(
            f"Silence comparison destination already exists: {output}"
        )
    return output


def _stage_silence_comparison_samples(
    values: Iterable[SilenceComparisonSample], staging: Path, target_seconds: float
) -> _StagedSilenceComparison:
    staged = _StagedSilenceComparison([], [], [], [])
    for value in values:
        record, segmented_report, compressed_report, checked_sources = (
            _stage_silence_comparison_sample(value, staging, target_seconds)
        )
        staged.records.append(record)
        staged.segmented_report_samples.append(segmented_report)
        staged.compressed_report_samples.append(compressed_report)
        staged.checked_sources.extend(checked_sources)
    return staged


def _stage_silence_comparison_sample(
    value: SilenceComparisonSample, staging: Path, target_seconds: float
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    tuple[tuple[Path, str, str], tuple[Path, str, str]],
]:
    raw_path, raw_payload, raw_sha256, raw_pcm, raw_rate = _read_source_wav(
        value.raw_audio, "raw comparison audio"
    )
    (
        segmented_path,
        segmented_payload,
        segmented_sha256,
        _segmented_pcm,
        segmented_rate,
    ) = _read_source_wav(value.segmented_audio, "segmented comparison audio")
    _validate_silence_comparison_sources(
        value, raw_sha256, segmented_sha256, raw_rate, segmented_rate
    )
    try:
        compression = compress_single_sentence_boundary_silence(
            raw_pcm, raw_rate, value.text, target_seconds=target_seconds
        )
    except ValueError as error:
        raise SilenceComparisonError(
            f"Unsafe silence-compression sample {value.queue_id}: {error}"
        ) from error
    raw_relative, segmented_relative, compressed_relative = _silence_comparison_paths(
        value.queue_id
    )
    _write_exact(staging / raw_relative, raw_payload)
    _write_exact(staging / segmented_relative, segmented_payload)
    write_pcm16_wav(staging / compressed_relative, compression.pcm, raw_rate)
    compressed_sha256 = sha256_file(staging / compressed_relative)
    text_sha256 = hashlib.sha256(value.text.encode("utf-8")).hexdigest()
    common = {
        "id": value.queue_id,
        "line_id": value.line_id,
        "text": value.text,
        "text_sha256": text_sha256,
    }
    return (
        {
            "queue_id": value.queue_id,
            "line_id": value.line_id,
            "text": value.text,
            "text_sha256": text_sha256,
            "raw_source": str(raw_path),
            "raw_source_sha256": raw_sha256,
            "raw_copy": raw_relative,
            "segmented_source": str(segmented_path),
            "segmented_source_sha256": segmented_sha256,
            "segmented_copy": segmented_relative,
            "compressed_audio": compressed_relative,
            "compressed_audio_sha256": compressed_sha256,
            "sample_rate": raw_rate,
            "transform": {
                key: result
                for key, result in asdict(compression).items()
                if key != "pcm"
            },
        },
        {
            **common,
            "audio": f"../{segmented_relative}",
            "audio_sha256": segmented_sha256,
        },
        {
            **common,
            "audio": f"../{compressed_relative}",
            "audio_sha256": compressed_sha256,
        },
        (
            (raw_path, raw_sha256, "raw comparison audio"),
            (segmented_path, segmented_sha256, "segmented comparison audio"),
        ),
    )


def _validate_silence_comparison_sources(
    value: SilenceComparisonSample,
    raw_sha256: str,
    segmented_sha256: str,
    raw_rate: int,
    segmented_rate: int,
) -> None:
    if value.raw_audio_sha256 is not None and raw_sha256 != value.raw_audio_sha256:
        raise SilenceComparisonError(
            f"Planned raw comparison audio changed for {value.queue_id}"
        )
    if (
        value.segmented_audio_sha256 is not None
        and segmented_sha256 != value.segmented_audio_sha256
    ):
        raise SilenceComparisonError(
            f"Planned segmented comparison audio changed for {value.queue_id}"
        )
    if segmented_rate != raw_rate:
        raise SilenceComparisonError(
            f"Comparison sample rates differ for {value.queue_id}"
        )


def _silence_comparison_paths(queue_id: str) -> tuple[str, str, str]:
    stem = hashlib.sha256(queue_id.encode("utf-8")).hexdigest()[:24]
    return (
        f"sources/{stem}-raw.wav",
        f"audio/{stem}-segmentation.wav",
        f"audio/{stem}-compression.wav",
    )


def _write_silence_comparison_reports(
    staging: Path, staged: _StagedSilenceComparison
) -> tuple[Path, Path]:
    reports = staging / "reports"
    reports.mkdir(parents=True)
    segmented_report = reports / "sentence-segmentation.json"
    compressed_report = reports / "silence-compression.json"
    atomic_write_json(
        segmented_report,
        _model_report(
            "sentence-segmentation",
            "independently rendered sentence segments",
            staged.segmented_report_samples,
        ),
        sort_keys=True,
    )
    atomic_write_json(
        compressed_report,
        _model_report(
            "silence-compression",
            "center-only compression of one verified silent span",
            staged.compressed_report_samples,
        ),
        sort_keys=True,
    )
    return segmented_report, compressed_report


def _silence_comparison_document(
    staging: Path,
    records: list[dict[str, object]],
    reports: tuple[Path, Path],
    target_seconds: float,
    input_plan_sha256: str | None,
) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": SILENCE_COMPARISON_SCHEMA,
        "schema_version": SILENCE_COMPARISON_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "kind": "single_sentence_boundary_silence_compression",
            "production_enabled": False,
            "requires_blind_review": True,
            "target_seconds": target_seconds,
        },
        "reports": [path.relative_to(staging).as_posix() for path in reports],
        "samples": records,
        "artifacts": _silence_comparison_artifacts(staging),
    }
    if input_plan_sha256 is not None:
        document["input_plan_sha256"] = input_plan_sha256
    return document


def _silence_comparison_artifacts(staging: Path) -> list[dict[str, str]]:
    return [
        {
            "path": path.relative_to(staging).as_posix(),
            "sha256": sha256_file(path),
        }
        for path in sorted(
            value
            for value in staging.rglob("*")
            if value.is_file() and value.name != "comparison.json"
        )
    ]


def _validate_silence_comparison_staging(
    reports: tuple[Path, Path],
    staging: Path,
    checked_sources: Iterable[tuple[Path, str, str]],
) -> None:
    validation = staging / ".validation-session"
    try:
        create_listening_session_from_reports(reports, validation, seed=0)
    except ModelListeningError as error:
        raise SilenceComparisonError(str(error)) from error
    shutil.rmtree(validation)
    for path, digest, label in checked_sources:
        if sha256_file(path) != digest:
            raise SilenceComparisonError(f"{label.title()} changed during staging")


def load_silence_comparison(directory: str | Path) -> dict[str, object]:
    """Validate a published comparison and every checksum-bound artifact."""
    root = Path(directory).expanduser().resolve()
    document = _read_silence_comparison_document(root)
    policy, reports, samples, artifacts = _validate_silence_comparison_document(
        document
    )
    seen = _validate_silence_comparison_artifacts(root, artifacts)
    _validate_silence_comparison_inventory(root, seen, reports)
    by_queue_id = _validate_silence_comparison_samples(root, samples, seen, policy)
    _validate_comparison_report(
        root,
        reports[0],
        "sentence-segmentation",
        "independently rendered sentence segments",
        "segmented_copy",
        "segmented_source_sha256",
        by_queue_id,
    )
    _validate_comparison_report(
        root,
        reports[1],
        "silence-compression",
        "center-only compression of one verified silent span",
        "compressed_audio",
        "compressed_audio_sha256",
        by_queue_id,
    )
    return document


def _read_silence_comparison_document(root: Path) -> dict[str, object]:
    try:
        document = json.loads((root / "comparison.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SilenceComparisonError(
            f"Unable to read silence comparison: {error}"
        ) from error
    if not isinstance(document, dict):
        raise SilenceComparisonError("Silence comparison document is malformed")
    return document


def _validate_silence_comparison_document(
    document: dict[str, object],
) -> tuple[dict[str, object], list[object], list[object], list[object]]:
    required_document_fields = {
        "schema",
        "schema_version",
        "created_at",
        "policy",
        "reports",
        "samples",
        "artifacts",
    }
    if frozenset(document) not in {
        frozenset(required_document_fields),
        frozenset((*required_document_fields, "input_plan_sha256")),
    }:
        raise SilenceComparisonError("Silence comparison document is malformed")
    if "input_plan_sha256" in document and not is_lowercase_sha256(
        document["input_plan_sha256"]
    ):
        raise SilenceComparisonError(
            "Silence comparison input plan checksum is invalid"
        )
    if (
        document["schema"] != SILENCE_COMPARISON_SCHEMA
        or not isinstance(document["schema_version"], int)
        or isinstance(document["schema_version"], bool)
        or document["schema_version"] != SILENCE_COMPARISON_VERSION
    ):
        raise SilenceComparisonError("Unsupported silence comparison schema")
    _silence_comparison_created_at(document["created_at"])
    policy = document["policy"]
    if (
        not isinstance(policy, dict)
        or set(policy)
        != {
            "kind",
            "production_enabled",
            "requires_blind_review",
            "target_seconds",
        }
        or policy["kind"] != "single_sentence_boundary_silence_compression"
        or policy["production_enabled"] is not False
        or policy["requires_blind_review"] is not True
        or not isinstance(policy["target_seconds"], (int, float))
        or isinstance(policy["target_seconds"], bool)
        or not math.isfinite(policy["target_seconds"])
        or policy["target_seconds"] <= 0
    ):
        raise SilenceComparisonError("Silence comparison policy is invalid")
    reports = document.get("reports")
    samples = document.get("samples")
    artifacts = document.get("artifacts")
    if (
        reports
        != [
            "reports/sentence-segmentation.json",
            "reports/silence-compression.json",
        ]
        or not isinstance(samples, list)
        or not samples
        or not isinstance(artifacts, list)
    ):
        raise SilenceComparisonError("Silence comparison inventory is malformed")
    return policy, reports, samples, artifacts


def _silence_comparison_created_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise SilenceComparisonError("Silence comparison creation timestamp is invalid")
    try:
        created_at = datetime.fromisoformat(value)
    except ValueError as error:
        raise SilenceComparisonError(
            "Silence comparison creation timestamp is invalid"
        ) from error
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise SilenceComparisonError(
            "Silence comparison creation timestamp must include a timezone"
        )
    return created_at


def _validate_silence_comparison_artifacts(
    root: Path, artifacts: Iterable[object]
) -> dict[object, object]:
    seen: dict[object, object] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise SilenceComparisonError("Silence comparison artifact is malformed")
        relative = artifact["path"]
        if relative in seen:
            raise SilenceComparisonError("Silence comparison artifact is duplicated")
        path = _contained_file(root, relative)
        digest = artifact["sha256"]
        if not is_lowercase_sha256(digest) or sha256_file(path) != digest:
            raise SilenceComparisonError(
                f"Silence comparison artifact checksum changed: {relative}"
            )
        seen[relative] = digest
    return seen


def _validate_silence_comparison_inventory(
    root: Path, seen: Mapping[object, object], reports: Iterable[object]
) -> None:
    actual_inventory = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SilenceComparisonError(
                "Silence comparison artifacts must not use symlinks"
            )
        if path.is_file() and path.name != "comparison.json":
            actual_inventory.add(path.relative_to(root).as_posix())
    if set(seen) != actual_inventory:
        raise SilenceComparisonError(
            "Silence comparison artifact inventory is not exact"
        )
    if set(reports) - set(seen):
        raise SilenceComparisonError(
            "Silence comparison report inventory is incomplete"
        )


def _validate_silence_comparison_samples(
    root: Path,
    samples: list[object],
    seen: Mapping[object, object],
    policy: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    required_sample_fields = {
        "queue_id",
        "line_id",
        "text",
        "text_sha256",
        "raw_source",
        "raw_source_sha256",
        "raw_copy",
        "segmented_source",
        "segmented_source_sha256",
        "segmented_copy",
        "compressed_audio",
        "compressed_audio_sha256",
        "sample_rate",
        "transform",
    }
    sample_ids = set()
    by_queue_id: dict[str, dict[str, object]] = {}
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != required_sample_fields:
            raise SilenceComparisonError("Silence comparison sample is malformed")
        queue_id = sample["queue_id"]
        if not isinstance(queue_id, str) or not queue_id or queue_id in sample_ids:
            raise SilenceComparisonError("Silence comparison sample ID is invalid")
        sample_ids.add(queue_id)
        _validate_silence_comparison_sample(root, sample, seen, policy)
        by_queue_id[queue_id] = sample
    return by_queue_id


def _validate_silence_comparison_sample(
    root: Path,
    sample: dict[str, object],
    seen: Mapping[object, object],
    policy: Mapping[str, object],
) -> None:
    if (
        not isinstance(sample["line_id"], str)
        or not sample["line_id"]
        or not isinstance(sample["text"], str)
        or not sample["text"]
        or hashlib.sha256(sample["text"].encode("utf-8")).hexdigest()
        != sample["text_sha256"]
    ):
        raise SilenceComparisonError("Silence comparison sample identity is invalid")
    _validate_silence_comparison_sample_artifacts(sample, seen)
    sample_rate = _silence_comparison_sample_rate(sample)
    audio = _silence_comparison_sample_audio(root, sample, sample_rate)
    target_seconds = _silence_comparison_policy_target(policy)
    try:
        compression = compress_single_sentence_boundary_silence(
            audio["raw_copy"],
            sample_rate,
            sample["text"],
            target_seconds=target_seconds,
        )
    except ValueError as error:
        raise SilenceComparisonError(
            "Silence comparison transform cannot be reproduced"
        ) from error
    expected_transform = {
        key: value for key, value in asdict(compression).items() if key != "pcm"
    }
    if sample["transform"] != expected_transform or not np.array_equal(
        audio["compressed_audio"], compression.pcm
    ):
        raise SilenceComparisonError(
            "Silence comparison transform ledger does not match its audio"
        )


def _validate_silence_comparison_sample_artifacts(
    sample: Mapping[str, object], seen: Mapping[object, object]
) -> None:
    for path_field, digest_field in (
        ("raw_copy", "raw_source_sha256"),
        ("segmented_copy", "segmented_source_sha256"),
        ("compressed_audio", "compressed_audio_sha256"),
    ):
        relative = sample[path_field]
        if (
            not is_lowercase_sha256(sample[digest_field])
            or seen.get(relative) != sample[digest_field]
        ):
            raise SilenceComparisonError(
                "Silence comparison sample is not bound to its artifact inventory"
            )


def _silence_comparison_sample_rate(sample: Mapping[str, object]) -> int:
    sample_rate = sample["sample_rate"]
    if (
        not isinstance(sample_rate, int)
        or isinstance(sample_rate, bool)
        or sample_rate <= 0
        or not isinstance(sample["transform"], dict)
    ):
        raise SilenceComparisonError(
            "Silence comparison sample audio metadata is invalid"
        )
    return sample_rate


def _silence_comparison_sample_audio(
    root: Path, sample: Mapping[str, object], sample_rate: int
) -> dict[str, np.ndarray]:
    audio = {}
    for path_field in ("raw_copy", "segmented_copy", "compressed_audio"):
        _path, _payload, _digest, pcm, rate = _read_source_wav(
            _contained_file(root, sample[path_field]),
            "published silence comparison audio",
        )
        if rate != sample_rate:
            raise SilenceComparisonError(
                "Silence comparison sample rate does not match its WAV"
            )
        audio[path_field] = pcm
    return audio


def _silence_comparison_policy_target(policy: Mapping[str, object]) -> float:
    target_seconds = policy["target_seconds"]
    if not isinstance(target_seconds, (int, float)) or isinstance(target_seconds, bool):
        raise SilenceComparisonError("Silence comparison policy is invalid")
    return target_seconds


def _validate_comparison_report(
    root: Path,
    relative: object,
    model_id: str,
    model: str,
    audio_field: str,
    digest_field: str,
    samples: dict[str, dict[str, object]],
) -> None:
    path = _contained_file(root, relative)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SilenceComparisonError(
            f"Unable to read silence comparison report: {error}"
        ) from error
    if (
        not isinstance(report, dict)
        or set(report)
        != {
            "schema",
            "schema_version",
            "model_id",
            "provider",
            "backend",
            "model",
            "samples",
        }
        or report["schema"] != "vntts.voice-model-report"
        or not isinstance(report["schema_version"], int)
        or isinstance(report["schema_version"], bool)
        or report["schema_version"] != 1
        or report["model_id"] != model_id
        or report["provider"] != "derived-comparison"
        or report["backend"] != "derived-comparison"
        or report["model"] != model
        or not isinstance(report["samples"], list)
        or len(report["samples"]) != len(samples)
    ):
        raise SilenceComparisonError("Silence comparison report is malformed")
    seen = set()
    for record in report["samples"]:
        if not isinstance(record, dict) or set(record) != {
            "id",
            "line_id",
            "text",
            "text_sha256",
            "audio",
            "audio_sha256",
        }:
            raise SilenceComparisonError(
                "Silence comparison report sample is malformed"
            )
        queue_id = record["id"]
        source = samples.get(queue_id)
        if source is None or queue_id in seen:
            raise SilenceComparisonError(
                "Silence comparison report sample identity is invalid"
            )
        seen.add(queue_id)
        expected_audio = f"../{source[audio_field]}"
        if record != {
            "id": queue_id,
            "line_id": source["line_id"],
            "text": source["text"],
            "text_sha256": source["text_sha256"],
            "audio": expected_audio,
            "audio_sha256": source[digest_field],
        }:
            raise SilenceComparisonError(
                "Silence comparison report diverges from its sample ledger"
            )
        report_audio = (path.parent / record["audio"]).resolve()
        expected_path = _contained_file(root, source[audio_field])
        if report_audio != expected_path:
            raise SilenceComparisonError(
                "Silence comparison report audio leaves its sample ledger"
            )
    if seen != set(samples):
        raise SilenceComparisonError(
            "Silence comparison report sample inventory is incomplete"
        )


def create_silence_comparison_session(
    comparison_directory: str | Path,
    output_directory: str | Path,
    *,
    seed: int = 0,
) -> Path:
    """Create a standard blind A/B session from one verified comparison bundle."""
    root = Path(comparison_directory).expanduser().resolve()
    document = load_silence_comparison(root)
    reports = document["reports"]
    if not isinstance(reports, list):
        raise SilenceComparisonError("Silence comparison report inventory is malformed")
    report_paths = tuple(_contained_file(root, value) for value in reports)
    try:
        return create_listening_session_from_reports(
            report_paths, output_directory, seed=seed
        )
    except ModelListeningError as error:
        raise SilenceComparisonError(str(error)) from error


def _validate_sample(value: object) -> SilenceComparisonSample:
    if not isinstance(value, SilenceComparisonSample):
        raise SilenceComparisonError(
            "Silence comparison samples must be SilenceComparisonSample values"
        )
    for field in ("queue_id", "line_id", "text"):
        text = getattr(value, field)
        if not isinstance(text, str) or not text or text != text.strip():
            raise SilenceComparisonError(f"Silence comparison {field} is invalid")
    for field in ("raw_audio_sha256", "segmented_audio_sha256"):
        digest = getattr(value, field)
        if digest is not None and not is_lowercase_sha256(digest):
            raise SilenceComparisonError(f"Silence comparison {field} is invalid")
    return value


def _planned_audio_path(root: Path, value: object, label: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise SilenceComparisonError(
            f"Silence comparison input {label} audio path is invalid"
        )
    path = Path(value).expanduser()
    if path.is_absolute():
        if path.is_symlink():
            raise SilenceComparisonError(
                f"Silence comparison input {label} audio is a symlink"
            )
        path = path.resolve()
        if not path.is_file():
            raise SilenceComparisonError(
                f"Silence comparison input {label} audio is missing"
            )
        return path
    return _contained_file(root, value)


def _validate_planned_audio(path: Path, expected_sha256: object, label: str) -> str:
    if not is_lowercase_sha256(expected_sha256):
        raise SilenceComparisonError(
            f"Silence comparison input {label} audio checksum is invalid"
        )
    _path, _payload, actual_sha256, _pcm, _rate = _read_source_wav(
        path, f"planned {label} comparison audio"
    )
    if actual_sha256 != expected_sha256:
        raise SilenceComparisonError(
            f"Silence comparison input {label} audio checksum changed"
        )
    return actual_sha256


def _read_source_wav(
    path: str | Path, label: str
) -> tuple[Path, bytes, str, np.ndarray, int]:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise SilenceComparisonError(f"{label.title()} must not be a symlink")
    source = source.resolve()
    try:
        payload = source.read_bytes()
        with wave.open(io.BytesIO(payload), "rb") as wav:
            if (
                wav.getcomptype() != "NONE"
                or wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getframerate() < 1
            ):
                raise Pcm16MonoWavError("expected mono 16-bit PCM WAV")
            rate = wav.getframerate()
            count = wav.getnframes()
            pcm_payload = wav.readframes(count)
        pcm: np.ndarray = np.frombuffer(pcm_payload, dtype="<i2")
        if len(pcm) != count:
            raise Pcm16MonoWavError("WAV sample data is incomplete")
    except (OSError, EOFError, wave.Error, Pcm16MonoWavError) as error:
        raise SilenceComparisonError(f"Unable to read {label}: {error}") from error
    return (
        source,
        payload,
        hashlib.sha256(payload).hexdigest(),
        pcm.astype(np.float32) / 32768.0,
        rate,
    )


def _model_report(
    model_id: str, model: str, samples: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    return {
        "schema": "vntts.voice-model-report",
        "schema_version": 1,
        "model_id": model_id,
        "provider": "derived-comparison",
        "backend": "derived-comparison",
        "model": model,
        "samples": samples,
    }


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _new_directory(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.name or path.name in {".", ".."}:
        raise SilenceComparisonError("Silence comparison requires a directory name")
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.parent.resolve() / path.name


def _contained_file(root: Path, relative: object) -> Path:
    return contained_regular_file(
        root,
        relative,
        "silence comparison artifact",
        error_type=SilenceComparisonError,
    )
