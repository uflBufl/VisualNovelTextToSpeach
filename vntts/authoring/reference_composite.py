"""Build an experimental voice reference from one complete exact game bank."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

import numpy as np
from numpy.typing import NDArray
from vntts_artifacts import (
    VoiceGenerationQueue,
    VoiceGenerationQueueItem,
    expected_voice_generation_queue_id,
    write_voice_generation_queue,
)
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import load_voice_manifest, write_voice_manifest

from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.authoring.publication import rename_directory_no_replace, staged_directory
from vntts.authoring.source_reference_quality_records import (
    QUALITY_REVIEW_SCHEMA,
    QUALITY_REVIEW_VERSION,
    SourceReferenceQualityResult,
    _copy_audio,
    load_source_reference_quality_review,
)
from vntts.authoring.source_reference_review import FIXED_EVALUATION_CORPUS
from vntts.authoring.workspace_foundation import contained_regular_file
from vntts.cli import cli_error, cli_success
from vntts.document_identity import is_lowercase_sha256
from vntts.reference_quality import analyze_reference_bytes

COMPOSITE_SCHEMA = "vntts.authoring-exact-bank-reference-composite"
COMPOSITE_VERSION = 1
COMPOSITE_EVALUATION_SCHEMA = "vntts.authoring-exact-bank-composite-evaluation"
COMPOSITE_EVALUATION_VERSION = 1
SOURCE_REPORT_SCHEMA = "r1999.story-voice-reference-candidates"
SOURCE_REPORT_VERSION = 2
COMPLETE_BANK_SCOPE = "complete_exact_bank"

PathInput: TypeAlias = str | Path
JsonObject: TypeAlias = dict[str, object]
Snapshot: TypeAlias = tuple[Path, str]


class ReferenceCompositeError(RuntimeError):
    """A complete-bank reference composite cannot be published safely."""


@dataclass(frozen=True)
class ReferenceCompositeResult:
    directory: Path
    clips: int
    duration_seconds: float
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "clips": self.clips,
            "duration_seconds": self.duration_seconds,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class _CompositeReviewInputs:
    ledger_path: Path
    ledger: JsonObject
    ledger_sha256: str
    evaluation_path: Path
    evaluation: JsonObject
    evaluation_sha256: str
    queue_path: Path


@dataclass(frozen=True)
class _CompositeReviewSources:
    composite_source: Path
    composite_sha256: str
    clips: list[JsonObject]
    affected: int
    snapshots: list[Snapshot]


@dataclass(frozen=True)
class _CompositeSelection:
    report_payload: bytes
    report_sha256: str
    source_bank_sha256: str
    candidates: list[JsonObject]


@dataclass(frozen=True)
class _StagedCompositeClips:
    sample_rate: int
    records: list[JsonObject]
    trimmed: list[NDArray[np.float32]]
    snapshots: list[Snapshot]


@dataclass(frozen=True)
class _CompositeClipInput:
    media_id: int
    event_ids: list[int]
    candidate: JsonObject
    source: Path
    payload: bytes
    expected_sha256: str
    sample_rate: int
    samples: NDArray[np.float32]


def publish_composite_quality_review(
    composite_directory: PathInput,
    state_path: PathInput,
    output: PathInput,
) -> SourceReferenceQualityResult:
    """Publish one self-contained review card for an exact-bank composite run."""
    composite_directory = Path(composite_directory).expanduser().resolve()
    state_path = Path(state_path).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise ReferenceCompositeError(f"Composite quality output exists: {output}")
    inputs = _load_composite_review_inputs(composite_directory)
    try:
        queue = VoiceGenerationQueue.load(inputs.queue_path)
        state = load_generation_state(state_path, inputs.queue_path)
    except (BulkGenerationError, OSError, ValueError) as error:
        raise ReferenceCompositeError(str(error)) from error
    state_sha256 = sha256_file(state_path)
    state_items = state.get("items")
    if not isinstance(state_items, dict):
        raise ReferenceCompositeError("Composite generation state is malformed")
    queue_by_id = {item.queue_id: item for item in queue.items}
    declared_queue_ids = _declared_queue_ids(inputs.evaluation, queue_by_id)
    sources = _load_composite_review_sources(composite_directory, inputs)

    output.parent.mkdir(parents=True, exist_ok=True)
    snapshots = [
        (inputs.ledger_path, inputs.ledger_sha256),
        (inputs.evaluation_path, inputs.evaluation_sha256),
        (inputs.queue_path, sha256_file(inputs.queue_path)),
        (state_path, state_sha256),
        *sources.snapshots,
    ]
    with staged_directory(output.parent, prefix=f".{output.name}.staging-") as staging:
        reference_relative = Path("audio") / "hotel-composite" / "reference.wav"
        reference = _copy_audio(
            sources.composite_source,
            sources.composite_sha256,
            staging / reference_relative,
        )
        reference["audio"] = reference_relative.as_posix()
        generated, excluded = _copy_review_outcomes(
            declared_queue_ids,
            queue_by_id,
            state_items,
            state_path.parent,
            staging,
            snapshots,
        )
        session = _composite_quality_session(
            inputs,
            sources,
            state_sha256,
            reference,
            generated,
            excluded,
        )
        review_path = staging / "review.json"
        atomic_write_json(review_path, session, sort_keys=True)
        load_source_reference_quality_review(review_path)
        _verify_snapshots(snapshots, "Composite quality source changed")
        rename_directory_no_replace(staging, output)
        return SourceReferenceQualityResult(output, 1, len(generated), len(excluded))


def _load_composite_review_inputs(directory: Path) -> _CompositeReviewInputs:
    ledger_path = directory / "composite.json"
    evaluation_path = directory / "evaluation.json"
    queue_path = directory / "queue.jsonl"
    try:
        ledger_payload, ledger = _read_json(ledger_path)
        evaluation_payload, evaluation = _read_json(evaluation_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReferenceCompositeError(
            f"Unable to read composite inputs: {error}"
        ) from error
    ledger_sha256 = hashlib.sha256(ledger_payload).hexdigest()
    evaluation_sha256 = hashlib.sha256(evaluation_payload).hexdigest()
    if (
        not isinstance(ledger, dict)
        or ledger.get("schema") != COMPOSITE_SCHEMA
        or ledger.get("schema_version") != COMPOSITE_VERSION
        or not isinstance(evaluation, dict)
        or evaluation.get("schema") != COMPOSITE_EVALUATION_SCHEMA
        or evaluation.get("schema_version") != COMPOSITE_EVALUATION_VERSION
        or evaluation.get("source_composite_sha256") != ledger_sha256
        or evaluation.get("queue_sha256") != sha256_file(queue_path)
    ):
        raise ReferenceCompositeError("Composite evaluation identity is invalid")
    return _CompositeReviewInputs(
        ledger_path,
        ledger,
        ledger_sha256,
        evaluation_path,
        evaluation,
        evaluation_sha256,
        queue_path,
    )


def _declared_queue_ids(
    evaluation: JsonObject, queue_by_id: dict[str, VoiceGenerationQueueItem]
) -> list[str]:
    declared = evaluation.get("fixed_queue_ids")
    if (
        not isinstance(declared, list)
        or len(declared) != len(queue_by_id)
        or any(not isinstance(queue_id, str) for queue_id in declared)
        or set(declared) != set(queue_by_id)
    ):
        raise ReferenceCompositeError("Composite fixed queue inventory changed")
    return declared


def _load_composite_review_sources(
    directory: Path, inputs: _CompositeReviewInputs
) -> _CompositeReviewSources:
    composite_record = inputs.ledger.get("composite")
    clips = inputs.ledger.get("clips")
    if (
        not isinstance(composite_record, dict)
        or not isinstance(clips, list)
        or any(not isinstance(clip, dict) for clip in clips)
    ):
        raise ReferenceCompositeError("Composite ledger inventory is invalid")
    composite_source = _contained_file(directory, composite_record.get("path"))
    composite_sha256 = _sha256(composite_record.get("sha256"), "Composite WAV hash")
    if sha256_file(composite_source) != composite_sha256:
        raise ReferenceCompositeError("Composite WAV changed")
    report_path = (
        Path(
            _text(
                inputs.ledger.get("source_candidate_report"), "Source candidate report"
            )
        )
        .expanduser()
        .resolve()
    )
    report_sha256 = _sha256(
        inputs.ledger.get("source_candidate_report_sha256"), "Source report hash"
    )
    try:
        report_payload, report = _read_json(report_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReferenceCompositeError(
            f"Unable to read source report: {error}"
        ) from error
    if hashlib.sha256(report_payload).hexdigest() != report_sha256:
        raise ReferenceCompositeError("Source candidate report changed")
    group = _matching_group(report, _ledger_identity(inputs.ledger))
    affected = group.get("affected_portrait_line_count") if group else None
    if isinstance(affected, bool) or not isinstance(affected, int) or affected <= 0:
        raise ReferenceCompositeError("Composite affected story-line count is invalid")
    return _CompositeReviewSources(
        composite_source,
        composite_sha256,
        clips,
        affected,
        [(composite_source, composite_sha256), (report_path, report_sha256)],
    )


def _copy_review_outcomes(
    queue_ids: list[str],
    queue_by_id: dict[str, VoiceGenerationQueueItem],
    state_items: dict[object, object],
    state_directory: Path,
    staging: Path,
    snapshots: list[Snapshot],
) -> tuple[list[JsonObject], list[JsonObject]]:
    generated: list[JsonObject] = []
    excluded: list[JsonObject] = []
    for index, queue_id in enumerate(queue_ids, start=1):
        item = queue_by_id[queue_id]
        result = state_items.get(queue_id)
        status = result.get("status") if isinstance(result, dict) else "pending"
        common = {
            "queue_id": queue_id,
            "evaluation_kind": item.document.get("evaluation_kind"),
            "text": item.text,
            "text_sha256": item.text_sha256,
        }
        if status in {"generated", "approved"}:
            if not isinstance(result, dict):
                raise ReferenceCompositeError(
                    f"Generated composite result is malformed: {queue_id}"
                )
            source = _contained_file(
                state_directory,
                _text(result.get("path"), f"Generated sample {queue_id} path"),
            )
            digest = _sha256(
                result.get("file_sha256"), f"Generated sample {queue_id} hash"
            )
            if sha256_file(source) != digest:
                raise ReferenceCompositeError(
                    f"Generated composite sample changed: {queue_id}"
                )
            relative = Path("audio") / "hotel-composite" / f"generated-{index}.wav"
            copied = _copy_audio(source, digest, staging / relative)
            generated.append({**common, "audio": relative.as_posix(), **copied})
            snapshots.append((source, digest))
        else:
            failure = result.get("failure", {}) if isinstance(result, dict) else {}
            excluded.append(
                {
                    **common,
                    "status": status,
                    "attempts": result.get("attempts", 0)
                    if isinstance(result, dict)
                    else 0,
                    "error": result.get("last_error")
                    if isinstance(result, dict)
                    else None,
                    "completion": failure.get("completion")
                    if isinstance(failure, dict)
                    else None,
                    "failure_kind": failure.get("kind")
                    if isinstance(failure, dict)
                    else None,
                }
            )
    return generated, excluded


def _composite_quality_session(
    inputs: _CompositeReviewInputs,
    sources: _CompositeReviewSources,
    state_sha256: str,
    reference: JsonObject,
    generated: list[JsonObject],
    excluded: list[JsonObject],
) -> JsonObject:
    now = datetime.now(timezone.utc).isoformat()
    variant_id = f"exact-bank-composite:{sources.composite_sha256}"
    return {
        "schema": QUALITY_REVIEW_SCHEMA,
        "schema_version": QUALITY_REVIEW_VERSION,
        "created_at": now,
        "updated_at": now,
        "source_reference_plan_sha256": inputs.ledger_sha256,
        "source_reference_evaluation_sha256": inputs.evaluation_sha256,
        "generation_state_sha256": state_sha256,
        "variant_count": 1,
        "completed_count": 0,
        "variants": [
            {
                "variant_id": variant_id,
                "cluster_id": variant_id,
                "character": inputs.ledger["character"],
                "portrait": inputs.ledger["portrait"],
                "portrait_image": None,
                "source_bank": inputs.ledger["source_bank"],
                "reference_kind": "exact_bank_composite",
                "media_ids": [clip["media_id"] for clip in sources.clips],
                "affected_queue_item_count": sources.affected,
                "reference": reference,
                "generated_samples": generated,
                "excluded_results": excluded,
                "decision": None,
            }
        ],
        "authority": (
            "Composite quality decision only. This review is not a source-reference "
            "plan and cannot be consumed as a voice binding without a dedicated gate."
        ),
    }


def _read_json(path: Path) -> tuple[bytes, object]:
    payload = path.read_bytes()
    value = json.loads(payload.decode("utf-8"))
    return payload, value


def _ledger_identity(ledger: JsonObject) -> tuple[object, object, object]:
    return (
        ledger.get("character"),
        ledger.get("portrait"),
        ledger.get("source_bank"),
    )


def _matching_group(
    report: object, identity: tuple[object, object, object]
) -> JsonObject | None:
    if not isinstance(report, dict):
        raise AttributeError(f"'{type(report).__name__}' object has no attribute 'get'")
    groups = report.get("groups", [])
    if not isinstance(groups, list):
        return None
    return next(
        (
            value
            for value in groups
            if isinstance(value, dict)
            and (
                value.get("character"),
                value.get("portrait"),
                value.get("source_bank"),
            )
            == identity
        ),
        None,
    )


def _verify_snapshots(snapshots: list[Snapshot], error_prefix: str) -> None:
    for source, digest in snapshots:
        if sha256_file(source) != digest:
            raise ReferenceCompositeError(f"{error_prefix}: {source.name}")


def publish_exact_bank_reference_composite(
    report_path: PathInput,
    character: object,
    portrait: object,
    source_bank: object,
    output: PathInput,
    *,
    gap_ms: int = 120,
    silence_dbfs: float = -40.0,
    trim_trigger_ms: int = 80,
    trim_padding_ms: int = 20,
) -> ReferenceCompositeResult:
    """Publish all clips for one exact complete-bank identity plus a composite."""
    report_path = Path(report_path).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    character = _text(character, "Character")
    portrait = _text(portrait, "Portrait")
    source_bank = _text(source_bank, "Source bank")
    if output.exists() or output.is_symlink():
        raise ReferenceCompositeError(f"Composite output exists: {output}")
    if (
        isinstance(gap_ms, bool)
        or not isinstance(gap_ms, int)
        or not 0 <= gap_ms <= 500
    ):
        raise ReferenceCompositeError("Composite gap must be 0..500 ms")
    if not 0 <= trim_padding_ms < trim_trigger_ms <= 500:
        raise ReferenceCompositeError("Composite edge-trim timing is invalid")
    selection = _load_composite_selection(
        report_path, (character, portrait, source_bank)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with staged_directory(output.parent, prefix=f".{output.name}.staging-") as staging:
        staged = _stage_composite_clips(
            selection.candidates,
            report_path.parent,
            staging,
            silence_dbfs=silence_dbfs,
            trim_trigger_ms=trim_trigger_ms,
            trim_padding_ms=trim_padding_ms,
        )
        result = _write_composite_artifacts(
            staging,
            report_path,
            selection,
            character,
            portrait,
            source_bank,
            gap_ms,
            silence_dbfs,
            trim_trigger_ms,
            trim_padding_ms,
            staged,
        )
        if (
            hashlib.sha256(report_path.read_bytes()).hexdigest()
            != selection.report_sha256
        ):
            raise ReferenceCompositeError(
                "Candidate report changed during composite publication"
            )
        _verify_snapshots(
            staged.snapshots, "Candidate reference changed during publication"
        )
        rename_directory_no_replace(staging, output)
        return ReferenceCompositeResult(
            output, result.clips, result.duration_seconds, result.sha256
        )


def _load_composite_selection(
    report_path: Path, identity: tuple[str, str, str]
) -> _CompositeSelection:
    try:
        report_payload, report = _read_json(report_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReferenceCompositeError(
            f"Unable to read candidate report {report_path}: {error}"
        ) from error
    if (
        not isinstance(report, dict)
        or report.get("schema") != SOURCE_REPORT_SCHEMA
        or report.get("schema_version") != SOURCE_REPORT_VERSION
        or report.get("bank_inventory_scope") != COMPLETE_BANK_SCOPE
    ):
        raise ReferenceCompositeError(
            "Composite requires extractor candidate-report v2 with a complete exact-bank inventory"
        )
    candidates = report.get("candidates")
    groups = report.get("groups")
    if not isinstance(candidates, list) or not isinstance(groups, list):
        raise ReferenceCompositeError("Candidate report inventory is invalid")
    selected = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and (
            candidate.get("character"),
            candidate.get("portrait"),
            candidate.get("source_bank"),
        )
        == identity
    ]
    matching_groups = [
        group
        for group in groups
        if isinstance(group, dict)
        and (group.get("character"), group.get("portrait"), group.get("source_bank"))
        == identity
    ]
    if len(matching_groups) != 1 or len(selected) < 2:
        raise ReferenceCompositeError(
            "Composite identity must have one group and at least two exact clips"
        )
    if matching_groups[0].get("candidate_count") != len(selected):
        raise ReferenceCompositeError(
            "Composite group candidate inventory is inconsistent"
        )
    media_ids = [candidate.get("media_id") for candidate in selected]
    if any(
        isinstance(media_id, bool) or not isinstance(media_id, int) or media_id < 0
        for media_id in media_ids
    ) or len(media_ids) != len(set(media_ids)):
        raise ReferenceCompositeError("Composite media inventory is invalid")
    selected.sort(key=lambda candidate: _media_id(candidate["media_id"]))
    report_sha256 = hashlib.sha256(report_payload).hexdigest()
    source_bank_sha256s = {
        _sha256(candidate.get("source_bank_sha256"), "Source bank hash")
        for candidate in selected
    }
    if len(source_bank_sha256s) != 1:
        raise ReferenceCompositeError("Composite clips disagree on source bank bytes")
    return _CompositeSelection(
        report_payload,
        report_sha256,
        next(iter(source_bank_sha256s)),
        selected,
    )


def _stage_composite_clips(
    candidates: list[JsonObject],
    report_directory: Path,
    staging: Path,
    *,
    silence_dbfs: float,
    trim_trigger_ms: int,
    trim_padding_ms: int,
) -> _StagedCompositeClips:
    sample_rate: int | None = None
    records: list[JsonObject] = []
    trimmed: list[NDArray[np.float32]] = []
    snapshots: list[Snapshot] = []
    for candidate in candidates:
        clip = _read_composite_clip(candidate, report_directory)
        if sample_rate is None:
            sample_rate = clip.sample_rate
        elif clip.sample_rate != sample_rate:
            raise ReferenceCompositeError("Composite clips must use one sample rate")
        record, samples, snapshot = _stage_composite_clip(
            clip,
            staging,
            silence_dbfs=silence_dbfs,
            trim_trigger_ms=trim_trigger_ms,
            trim_padding_ms=trim_padding_ms,
        )
        records.append(record)
        trimmed.append(samples)
        snapshots.append(snapshot)
    if sample_rate is None:
        raise ReferenceCompositeError("Composite has no clips")
    return _StagedCompositeClips(sample_rate, records, trimmed, snapshots)


def _read_composite_clip(
    candidate: JsonObject,
    report_directory: Path,
) -> _CompositeClipInput:
    media_id = _media_id(candidate.get("media_id"))
    event_ids = candidate.get("source_event_ids")
    if (
        not isinstance(event_ids, list)
        or not event_ids
        or any(
            isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 0
            for event_id in event_ids
        )
    ):
        raise ReferenceCompositeError(
            f"Composite media {media_id} has no exact event IDs"
        )
    source = _contained_file(
        report_directory, _text(candidate.get("reference"), "Candidate reference")
    )
    payload = source.read_bytes()
    expected_sha256 = _sha256(
        candidate.get("reference_sha256"), "Candidate reference hash"
    )
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ReferenceCompositeError(
            f"Composite candidate reference checksum changed: {media_id}"
        )
    sample_rate, samples = _read_pcm16_mono(payload, media_id)
    return _CompositeClipInput(
        media_id,
        event_ids,
        candidate,
        source,
        payload,
        expected_sha256,
        sample_rate,
        samples,
    )


def _stage_composite_clip(
    clip: _CompositeClipInput,
    staging: Path,
    *,
    silence_dbfs: float,
    trim_trigger_ms: int,
    trim_padding_ms: int,
) -> tuple[JsonObject, NDArray[np.float32], Snapshot]:
    trimmed, removed_start, removed_end = _trim_edges(
        clip.samples,
        clip.sample_rate,
        silence_dbfs=silence_dbfs,
        trigger_ms=trim_trigger_ms,
        padding_ms=trim_padding_ms,
    )
    if not trimmed.size:
        raise ReferenceCompositeError(
            f"Composite media {clip.media_id} is silent after bounded edge trim"
        )
    copied_relative = Path("clips") / f"{clip.media_id}.wav"
    copied = staging / copied_relative
    copied.parent.mkdir(parents=True, exist_ok=True)
    copied.write_bytes(clip.payload)
    if hashlib.sha256(copied.read_bytes()).hexdigest() != clip.expected_sha256:
        raise ReferenceCompositeError(f"Composite clip copy changed: {clip.media_id}")
    return (
        {
            "media_id": clip.media_id,
            "source_event_ids": sorted(clip.event_ids),
            "candidate_origin": clip.candidate.get("candidate_origin"),
            "source_sha256": _sha256(
                clip.candidate.get("source_sha256"), "Encoded media hash"
            ),
            "reference": copied_relative.as_posix(),
            "reference_sha256": clip.expected_sha256,
            "input_frames": int(len(clip.samples)),
            "composite_frames": int(len(trimmed)),
            "trimmed_leading_frames": removed_start,
            "trimmed_trailing_frames": removed_end,
        },
        trimmed,
        (clip.source, clip.expected_sha256),
    )


def _write_composite_artifacts(
    staging: Path,
    report_path: Path,
    selection: _CompositeSelection,
    character: str,
    portrait: str,
    source_bank: str,
    gap_ms: int,
    silence_dbfs: float,
    trim_trigger_ms: int,
    trim_padding_ms: int,
    staged: _StagedCompositeClips,
) -> ReferenceCompositeResult:
    composite, composite_path = _write_composite_wav(staging, staged, gap_ms)
    composite_payload = composite_path.read_bytes()
    composite_sha256 = hashlib.sha256(composite_payload).hexdigest()
    ledger_path = staging / "composite.json"
    atomic_write_json(
        ledger_path,
        _composite_ledger(
            report_path,
            selection,
            character,
            portrait,
            source_bank,
            gap_ms,
            silence_dbfs,
            trim_trigger_ms,
            trim_padding_ms,
            staged,
            composite,
            composite_path,
            composite_sha256,
        ),
    )
    _write_composite_evaluation_inputs(
        staging, ledger_path, composite_path, character, composite_sha256
    )
    return ReferenceCompositeResult(
        staging,
        len(staged.records),
        len(composite) / staged.sample_rate,
        composite_sha256,
    )


def _write_composite_wav(
    staging: Path, staged: _StagedCompositeClips, gap_ms: int
) -> tuple[NDArray[np.float32], Path]:
    gap_frames = round(staged.sample_rate * gap_ms / 1000)
    parts: list[NDArray[np.float32]] = []
    for index, samples in enumerate(staged.trimmed):
        if index:
            parts.append(np.zeros(gap_frames, dtype=np.float32))
        parts.append(samples)
    composite = np.concatenate(parts)
    composite_path = staging / "composite.wav"
    write_pcm16_wav(composite_path, composite, staged.sample_rate)
    return composite, composite_path


def _composite_ledger(
    report_path: Path,
    selection: _CompositeSelection,
    character: str,
    portrait: str,
    source_bank: str,
    gap_ms: int,
    silence_dbfs: float,
    trim_trigger_ms: int,
    trim_padding_ms: int,
    staged: _StagedCompositeClips,
    composite: NDArray[np.float32],
    composite_path: Path,
    composite_sha256: str,
) -> JsonObject:
    preflight = analyze_reference_bytes(
        composite_path.read_bytes(), path=composite_path
    )
    preflight["path"] = composite_path.name
    return {
        "schema": COMPOSITE_SCHEMA,
        "schema_version": COMPOSITE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_candidate_report": str(report_path),
        "source_candidate_report_sha256": selection.report_sha256,
        "character": character,
        "portrait": portrait,
        "source_bank": source_bank,
        "source_bank_sha256": selection.source_bank_sha256,
        "clip_count": len(staged.records),
        "clips": staged.records,
        "composition": {
            "ordering": "ascending_media_id",
            "gap_ms": gap_ms,
            "silence_dbfs": silence_dbfs,
            "trim_trigger_ms": trim_trigger_ms,
            "trim_padding_ms": trim_padding_ms,
        },
        "composite": {
            "path": composite_path.name,
            "sha256": composite_sha256,
            "sample_rate": staged.sample_rate,
            "frame_count": int(len(composite)),
            "duration_seconds": len(composite) / staged.sample_rate,
            "objective_preflight": preflight,
        },
        "authority": (
            "Experimental same-bank synthesis reference only. Exact bank identity "
            "does not replace generated-quality review or authorize a voice binding."
        ),
    }


def _write_composite_evaluation_inputs(
    staging: Path,
    ledger_path: Path,
    composite_path: Path,
    character: str,
    composite_sha256: str,
) -> None:
    ledger_sha256 = sha256_file(ledger_path)
    voice_character = f"Exact bank composite {character} {composite_sha256[:12]}"
    manifest_path = staging / "voice-manifest.json"
    write_voice_manifest(
        manifest_path,
        {
            "version": 2,
            "game": "Exact bank composite evaluation",
            "language": "en",
            "voices": [
                {
                    "character": voice_character,
                    "speaker": f"exact-bank-composite:{character}",
                    "references": [composite_path.name],
                }
            ],
            "vntts.authoring.exact_bank_composite_sha256": ledger_sha256,
        },
    )
    queue_path, queue_ids = _write_composite_queue(
        staging, character, voice_character, composite_sha256, ledger_sha256
    )
    atomic_write_json(
        staging / "evaluation.json",
        {
            "schema": COMPOSITE_EVALUATION_SCHEMA,
            "schema_version": COMPOSITE_EVALUATION_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_composite": ledger_path.name,
            "source_composite_sha256": ledger_sha256,
            "voice_manifest": manifest_path.name,
            "voice_manifest_sha256": sha256_file(manifest_path),
            "queue": queue_path.name,
            "queue_sha256": sha256_file(queue_path),
            "voice_character": voice_character,
            "fixed_queue_ids": queue_ids,
            "authority": (
                "Bounded fixed-corpus generation input only. Generated audio requires "
                "a separate quality decision before any Hotelier voice binding."
            ),
        },
    )
    load_voice_manifest(manifest_path, allow_legacy=False)
    VoiceGenerationQueue.load(queue_path)


def _write_composite_queue(
    staging: Path,
    character: str,
    voice_character: str,
    composite_sha256: str,
    ledger_sha256: str,
) -> tuple[Path, list[str]]:
    queue_items: list[JsonObject] = []
    queue_ids: list[str] = []
    for index, text in enumerate(FIXED_EVALUATION_CORPUS, start=1):
        text_sha256 = hashlib.sha256(text.encode()).hexdigest()
        line_id = f"exact-bank-composite:{composite_sha256}:fixed-{index}"
        queue_id = expected_voice_generation_queue_id(line_id, text_sha256)
        queue_ids.append(queue_id)
        queue_items.append(
            {
                "record_type": "generation_item",
                "queue_id": queue_id,
                "line_id": line_id,
                "text": text,
                "text_sha256": text_sha256,
                "speaker": character,
                "voice_character": voice_character,
                "source_audio_status": "absent",
                "source_audio_reason": "exact_bank_composite_evaluation",
                "source_kind": "authoring_evaluation",
                "action": "generate",
                "state": "pending",
                "evaluation_kind": f"fixed-{index}",
                "source_composite_sha256": ledger_sha256,
            }
        )
    queue_path = staging / "queue.jsonl"
    write_voice_generation_queue(
        queue_path,
        {
            "game": "Exact bank composite evaluation",
            "language": "en",
            "source_composite_sha256": ledger_sha256,
        },
        queue_items,
    )
    return queue_path, queue_ids


def _read_pcm16_mono(
    payload: bytes, media_id: object
) -> tuple[int, NDArray[np.float32]]:
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            channels = source.getnchannels()
            width = source.getsampwidth()
            rate = source.getframerate()
            frames = source.getnframes()
            raw = source.readframes(frames)
    except (EOFError, OSError, wave.Error) as error:
        raise ReferenceCompositeError(
            f"Composite media {media_id} is not a readable WAV: {error}"
        ) from error
    if channels != 1 or width != 2 or rate <= 0 or frames <= 0:
        raise ReferenceCompositeError(
            f"Composite media {media_id} must be non-empty PCM16 mono"
        )
    return rate, np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _trim_edges(
    samples: NDArray[np.float32],
    sample_rate: int,
    *,
    silence_dbfs: float,
    trigger_ms: int,
    padding_ms: int,
) -> tuple[NDArray[np.float32], int, int]:
    threshold = 10.0 ** (float(silence_dbfs) / 20.0)
    active = np.flatnonzero(np.abs(samples) > threshold)
    if not active.size:
        return samples[:0], 0, len(samples)
    trigger = round(sample_rate * trigger_ms / 1000)
    padding = round(sample_rate * padding_ms / 1000)
    first = int(active[0])
    last = int(active[-1])
    removed_start = max(0, first - padding) if first > trigger else 0
    trailing = len(samples) - last - 1
    removed_end = max(0, trailing - padding) if trailing > trigger else 0
    end = len(samples) - removed_end if removed_end else len(samples)
    return samples[removed_start:end].copy(), removed_start, removed_end


def _media_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReferenceCompositeError("Composite media inventory is invalid")
    return value


def _contained_file(root: Path, relative: object) -> Path:
    relative = _text(relative, "Reference path")
    return contained_regular_file(
        root, relative, "reference path", error_type=ReferenceCompositeError
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReferenceCompositeError(f"{label} must be non-empty text")
    return value.strip()


def _sha256(value: object, label: str) -> str:
    value = _text(value, label)
    if not is_lowercase_sha256(value):
        raise ReferenceCompositeError(f"{label} must be lowercase SHA-256")
    return value


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build one experimental reference from a complete exact game bank"
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--character", required=True)
    parser.add_argument("--portrait", required=True)
    parser.add_argument("--source-bank", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gap-ms", type=int, default=120)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = create_parser().parse_args(argv)
    try:
        result = publish_exact_bank_reference_composite(
            options.report,
            options.character,
            options.portrait,
            options.source_bank,
            options.output,
            gap_ms=options.gap_ms,
        )
    except (OSError, ReferenceCompositeError, ValueError) as error:
        return cli_error(error)
    return cli_success(json.dumps(result.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
