"""Build an experimental voice reference from one complete exact game bank."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from vntts_artifacts import (
    VoiceGenerationQueue,
    expected_voice_generation_queue_id,
    write_voice_generation_queue,
)
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import load_voice_manifest, write_voice_manifest

from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.authoring.game_pack import _rename_directory_no_replace
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
from vntts.reference_quality import analyze_reference_bytes

COMPOSITE_SCHEMA = "vntts.authoring-exact-bank-reference-composite"
COMPOSITE_VERSION = 1
COMPOSITE_EVALUATION_SCHEMA = "vntts.authoring-exact-bank-composite-evaluation"
COMPOSITE_EVALUATION_VERSION = 1
SOURCE_REPORT_SCHEMA = "r1999.story-voice-reference-candidates"
SOURCE_REPORT_VERSION = 2
COMPLETE_BANK_SCOPE = "complete_exact_bank"


class ReferenceCompositeError(RuntimeError):
    """A complete-bank reference composite cannot be published safely."""


@dataclass(frozen=True)
class ReferenceCompositeResult:
    directory: Path
    clips: int
    duration_seconds: float
    sha256: str

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "clips": self.clips,
            "duration_seconds": self.duration_seconds,
            "sha256": self.sha256,
        }


def publish_composite_quality_review(composite_directory, state_path, output):
    """Publish one self-contained review card for an exact-bank composite run."""
    composite_directory = Path(composite_directory).expanduser().resolve()
    state_path = Path(state_path).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise ReferenceCompositeError(f"Composite quality output exists: {output}")
    ledger_path = composite_directory / "composite.json"
    evaluation_path = composite_directory / "evaluation.json"
    queue_path = composite_directory / "queue.jsonl"
    try:
        ledger_payload = ledger_path.read_bytes()
        ledger = json.loads(ledger_payload.decode("utf-8"))
        evaluation_payload = evaluation_path.read_bytes()
        evaluation = json.loads(evaluation_payload.decode("utf-8"))
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
    try:
        queue = VoiceGenerationQueue.load(queue_path)
        state = load_generation_state(state_path, queue_path)
    except (BulkGenerationError, OSError, ValueError) as error:
        raise ReferenceCompositeError(str(error)) from error
    state_sha256 = sha256_file(state_path)
    queue_by_id = {item.queue_id: item for item in queue.items}
    declared_queue_ids = evaluation.get("fixed_queue_ids")
    if (
        not isinstance(declared_queue_ids, list)
        or len(declared_queue_ids) != len(queue.items)
        or set(declared_queue_ids) != set(queue_by_id)
    ):
        raise ReferenceCompositeError("Composite fixed queue inventory changed")
    composite_record = ledger.get("composite")
    clips = ledger.get("clips")
    if not isinstance(composite_record, dict) or not isinstance(clips, list):
        raise ReferenceCompositeError("Composite ledger inventory is invalid")
    composite_source = _contained_file(
        composite_directory, composite_record.get("path")
    )
    composite_sha256 = _sha256(composite_record.get("sha256"), "Composite WAV hash")
    if sha256_file(composite_source) != composite_sha256:
        raise ReferenceCompositeError("Composite WAV changed")
    report_path = (
        Path(_text(ledger.get("source_candidate_report"), "Source candidate report"))
        .expanduser()
        .resolve()
    )
    report_sha256 = _sha256(
        ledger.get("source_candidate_report_sha256"), "Source report hash"
    )
    try:
        report_payload = report_path.read_bytes()
        report = json.loads(report_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReferenceCompositeError(
            f"Unable to read source report: {error}"
        ) from error
    if hashlib.sha256(report_payload).hexdigest() != report_sha256:
        raise ReferenceCompositeError("Source candidate report changed")
    group = next(
        (
            value
            for value in report.get("groups", [])
            if isinstance(value, dict)
            and (
                value.get("character"),
                value.get("portrait"),
                value.get("source_bank"),
            )
            == (
                ledger.get("character"),
                ledger.get("portrait"),
                ledger.get("source_bank"),
            )
        ),
        None,
    )
    affected = group.get("affected_portrait_line_count") if group else None
    if isinstance(affected, bool) or not isinstance(affected, int) or affected <= 0:
        raise ReferenceCompositeError("Composite affected story-line count is invalid")

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    snapshots = [
        (ledger_path, ledger_sha256),
        (evaluation_path, evaluation_sha256),
        (queue_path, sha256_file(queue_path)),
        (state_path, state_sha256),
        (composite_source, composite_sha256),
        (report_path, report_sha256),
    ]
    try:
        reference_relative = Path("audio") / "hotel-composite" / "reference.wav"
        reference = _copy_audio(
            composite_source, composite_sha256, staging / reference_relative
        )
        reference["audio"] = reference_relative.as_posix()
        generated = []
        excluded = []
        for index, queue_id in enumerate(declared_queue_ids, start=1):
            item = queue_by_id[queue_id]
            result = state["items"].get(queue_id)
            status = result.get("status") if isinstance(result, dict) else "pending"
            common = {
                "queue_id": queue_id,
                "evaluation_kind": item.document.get("evaluation_kind"),
                "text": item.text,
                "text_sha256": item.text_sha256,
            }
            if status in {"generated", "approved"}:
                source = _contained_file(
                    state_path.parent,
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
        now = datetime.now(timezone.utc).isoformat()
        variant_id = f"exact-bank-composite:{composite_sha256}"
        session = {
            "schema": QUALITY_REVIEW_SCHEMA,
            "schema_version": QUALITY_REVIEW_VERSION,
            "created_at": now,
            "updated_at": now,
            "source_reference_plan_sha256": ledger_sha256,
            "source_reference_evaluation_sha256": evaluation_sha256,
            "generation_state_sha256": state_sha256,
            "variant_count": 1,
            "completed_count": 0,
            "variants": [
                {
                    "variant_id": variant_id,
                    "cluster_id": variant_id,
                    "character": ledger["character"],
                    "portrait": ledger["portrait"],
                    "portrait_image": None,
                    "source_bank": ledger["source_bank"],
                    "reference_kind": "exact_bank_composite",
                    "media_ids": [clip["media_id"] for clip in clips],
                    "affected_queue_item_count": affected,
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
        review_path = staging / "review.json"
        atomic_write_json(review_path, session, sort_keys=True)
        load_source_reference_quality_review(review_path)
        for source, digest in snapshots:
            if sha256_file(source) != digest:
                raise ReferenceCompositeError(
                    f"Composite quality source changed: {source.name}"
                )
        _rename_directory_no_replace(staging, output)
        return SourceReferenceQualityResult(output, 1, len(generated), len(excluded))
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def publish_exact_bank_reference_composite(
    report_path,
    character,
    portrait,
    source_bank,
    output,
    *,
    gap_ms=120,
    silence_dbfs=-40.0,
    trim_trigger_ms=80,
    trim_padding_ms=20,
):
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
    try:
        report_payload = report_path.read_bytes()
        report = json.loads(report_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReferenceCompositeError(
            f"Unable to read candidate report {report_path}: {error}"
        ) from error
    if not isinstance(report, dict) or (
        report.get("schema") != SOURCE_REPORT_SCHEMA
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
    identity = (character, portrait, source_bank)
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

    selected.sort(key=lambda candidate: candidate["media_id"])
    report_sha256 = hashlib.sha256(report_payload).hexdigest()
    source_bank_sha256s = {
        _sha256(candidate.get("source_bank_sha256"), "Source bank hash")
        for candidate in selected
    }
    if len(source_bank_sha256s) != 1:
        raise ReferenceCompositeError("Composite clips disagree on source bank bytes")
    source_bank_sha256 = next(iter(source_bank_sha256s))
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    snapshots = []
    try:
        sample_rate = None
        clip_records = []
        trimmed_clips = []
        for candidate in selected:
            media_id = candidate["media_id"]
            event_ids = candidate.get("source_event_ids")
            if (
                not isinstance(event_ids, list)
                or not event_ids
                or any(
                    isinstance(event_id, bool)
                    or not isinstance(event_id, int)
                    or event_id < 0
                    for event_id in event_ids
                )
            ):
                raise ReferenceCompositeError(
                    f"Composite media {media_id} has no exact event IDs"
                )
            relative = _text(candidate.get("reference"), "Candidate reference")
            source = _contained_file(report_path.parent, relative)
            payload = source.read_bytes()
            expected_sha256 = _sha256(
                candidate.get("reference_sha256"), "Candidate reference hash"
            )
            if hashlib.sha256(payload).hexdigest() != expected_sha256:
                raise ReferenceCompositeError(
                    f"Composite candidate reference checksum changed: {media_id}"
                )
            current_rate, samples = _read_pcm16_mono(payload, media_id)
            if sample_rate is None:
                sample_rate = current_rate
            elif current_rate != sample_rate:
                raise ReferenceCompositeError(
                    "Composite clips must use one sample rate"
                )
            trimmed, removed_start, removed_end = _trim_edges(
                samples,
                sample_rate,
                silence_dbfs=silence_dbfs,
                trigger_ms=trim_trigger_ms,
                padding_ms=trim_padding_ms,
            )
            if not trimmed.size:
                raise ReferenceCompositeError(
                    f"Composite media {media_id} is silent after bounded edge trim"
                )
            copied_relative = Path("clips") / f"{media_id}.wav"
            copied = staging / copied_relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            copied.write_bytes(payload)
            if hashlib.sha256(copied.read_bytes()).hexdigest() != expected_sha256:
                raise ReferenceCompositeError(
                    f"Composite clip copy changed: {media_id}"
                )
            snapshots.append((source, expected_sha256))
            trimmed_clips.append(trimmed)
            clip_records.append(
                {
                    "media_id": media_id,
                    "source_event_ids": sorted(event_ids),
                    "candidate_origin": candidate.get("candidate_origin"),
                    "source_sha256": _sha256(
                        candidate.get("source_sha256"), "Encoded media hash"
                    ),
                    "reference": copied_relative.as_posix(),
                    "reference_sha256": expected_sha256,
                    "input_frames": int(len(samples)),
                    "composite_frames": int(len(trimmed)),
                    "trimmed_leading_frames": removed_start,
                    "trimmed_trailing_frames": removed_end,
                }
            )
        assert sample_rate is not None
        gap_frames = round(sample_rate * gap_ms / 1000)
        parts = []
        for index, samples in enumerate(trimmed_clips):
            if index:
                parts.append(np.zeros(gap_frames, dtype=np.float32))
            parts.append(samples)
        composite = np.concatenate(parts)
        composite_path = staging / "composite.wav"
        write_pcm16_wav(composite_path, composite, sample_rate)
        composite_payload = composite_path.read_bytes()
        composite_sha256 = hashlib.sha256(composite_payload).hexdigest()
        preflight = analyze_reference_bytes(composite_payload, path=composite_path)
        preflight["path"] = composite_path.name
        ledger = {
            "schema": COMPOSITE_SCHEMA,
            "schema_version": COMPOSITE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_candidate_report": str(report_path),
            "source_candidate_report_sha256": report_sha256,
            "character": character,
            "portrait": portrait,
            "source_bank": source_bank,
            "source_bank_sha256": source_bank_sha256,
            "clip_count": len(clip_records),
            "clips": clip_records,
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
                "sample_rate": sample_rate,
                "frame_count": int(len(composite)),
                "duration_seconds": len(composite) / sample_rate,
                "objective_preflight": preflight,
            },
            "authority": (
                "Experimental same-bank synthesis reference only. Exact bank identity "
                "does not replace generated-quality review or authorize a voice binding."
            ),
        }
        ledger_path = staging / "composite.json"
        atomic_write_json(ledger_path, ledger)
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
        queue_items = []
        queue_ids = []
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
        evaluation = {
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
        }
        atomic_write_json(staging / "evaluation.json", evaluation)
        load_voice_manifest(manifest_path, allow_legacy=False)
        VoiceGenerationQueue.load(queue_path)
        if hashlib.sha256(report_path.read_bytes()).hexdigest() != report_sha256:
            raise ReferenceCompositeError(
                "Candidate report changed during composite publication"
            )
        for source, expected_sha256 in snapshots:
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha256:
                raise ReferenceCompositeError(
                    f"Candidate reference changed during publication: {source.name}"
                )
        _rename_directory_no_replace(staging, output)
        return ReferenceCompositeResult(
            output,
            len(clip_records),
            len(composite) / sample_rate,
            composite_sha256,
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _read_pcm16_mono(payload, media_id):
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


def _trim_edges(samples, sample_rate, *, silence_dbfs, trigger_ms, padding_ms):
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


def _contained_file(root, relative):
    relative = _text(relative, "Reference path")
    return contained_regular_file(
        root, relative, "reference path", error_type=ReferenceCompositeError
    )


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ReferenceCompositeError(f"{label} must be non-empty text")
    return value.strip()


def _sha256(value, label):
    value = _text(value, label)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ReferenceCompositeError(f"{label} must be lowercase SHA-256")
    return value


def create_parser():
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


def main(argv=None):
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
