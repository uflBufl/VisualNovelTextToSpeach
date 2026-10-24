"""Checksum-bound live-fallback evidence and leased decision publication."""

from __future__ import annotations

import copy
import hashlib
import os
import secrets
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias, TypeGuard

from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueItem,
)

from vntts.authoring.authority import canonical_document_sha256 as _canonical_sha256
from vntts.authoring.failure_repair import (
    OFFLINE_FALLBACK_BACKEND,
    SENTENCE_BOUNDARY_SEGMENTATION,
)
from vntts.authoring.generation_failures import generation_failure_repair_plan
from vntts.authoring.generation_lease import (
    BulkGenerationError,
    BulkGenerationSourceChangedError,
    GenerationLease,
    process_is_alive,
)
from vntts.authoring.generation_manifest import (
    RUNTIME_PROGRESS_MANIFEST_NAME,
    write_runtime_progress_manifest_from_state,
)
from vntts.authoring.generation_manifest import (
    approved_manifest_entries as _approved_manifest_entries,
)
from vntts.authoring.generation_manifest import (
    write_generated_manifest_from_state as _write_generated_manifest_from_state,
)
from vntts.authoring.generation_state import (
    AUTOMATIC_RECOVERY_LIVE_FALLBACK_EVIDENCE_SCHEMA,
    LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED,
    LIVE_FALLBACK_AUTOMATIC_RECOVERY_VERSION,
    LIVE_FALLBACK_EVIDENCE_SCHEMA,
    LIVE_FALLBACK_EVIDENCE_VERSION,
    LIVE_FALLBACK_HYPOTHESES_EXHAUSTED,
    LIVE_FALLBACK_REASONS,
    LIVE_FALLBACK_REVIEW_EVIDENCE_VERSION,
    LIVE_FALLBACK_VERSION,
    STATE_SCHEMA,
    STATE_VERSION,
    decode_generation_state_snapshot,
    generation_state_file,
    generation_state_snapshot,
    write_generation_state,
)
from vntts.authoring.generation_state import generation_state_items as _state_items
from vntts.authoring.generation_state import (
    live_fallback_decision as _live_fallback_decision,
)
from vntts.authoring.generation_state import (
    load_stable_generation_queue as _load_stable_queue,
)
from vntts.authoring.generation_state import required_state_text as _generation_text
from vntts.authoring.generation_state import required_state_text as _required_text
from vntts.authoring.generation_state import state_nonnegative_int as _nonnegative_int
from vntts.authoring.generation_state import (
    validate_failure_record as _validate_failure_record,
)
from vntts.authoring.generation_state import (
    validate_failure_repair_record as _validate_failure_repair_record,
)
from vntts.authoring.generation_state import (
    validate_generation_state_document as _validate_state_document,
)
from vntts.authoring.generation_state import (
    validate_live_fallback_evidence as _validate_live_fallback_evidence,
)
from vntts.authoring.generation_state import (
    validate_seed_application as _validate_seed_application,
)
from vntts.authoring.generation_state import (
    validate_synthesis_identity as _validate_synthesis_identity,
)
from vntts.authoring.publication import publication_errors
from vntts.document_identity import sha256_file
from vntts.json_types import decode_json, has_schema_version
from vntts.json_types import is_json_object as _is_json_document
from vntts.path_safety import read_regular_bytes
from vntts.voices import synthesis_character_for_line

JsonDocument: TypeAlias = dict[str, object]

_GenerationLease = GenerationLease


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


AUTOMATIC_RECOVERY_LIVE_FALLBACK_ACTIONS = frozenset(
    {
        "backend_diagnosis",
        "bounded_seed_retry",
        "inline_pause_marker_comparison",
        "offline_fallback_backend",
        "provenance_recovery_or_regeneration",
        "reference_comparison",
        "reference_discovery",
    }
)


def authorize_live_fallback(
    state_path: str | Path,
    queue_path: str | Path,
    queue_id: str,
    *,
    reason: str,
    provider: str = "pocket-tts",
    model: object,
    generation_profile: object = "default",
    evidence_workspaces: Sequence[str | Path] = (),
    evidence_reviews: Sequence[str | Path] = (),
) -> JsonDocument:
    """Record one exact terminal live-Pocket decision without publishing audio."""
    provider, model, generation_profile = _live_fallback_backend(
        reason, provider, model, generation_profile
    )
    state_path = generation_state_file(state_path)
    queue_path = Path(queue_path).expanduser().resolve()
    queue, queue_sha256 = _load_stable_queue(queue_path)
    queue_by_id = {item.queue_id: item for item in queue.items}
    queue_item = queue_by_id.get(queue_id)
    if queue_item is None:
        raise BulkGenerationError(f"Unknown live fallback queue item: {queue_id!r}")
    with _GenerationLease(
        state_path.parent,
        queue_sha256,
        process_checker=process_is_alive,
    ) as lease:
        state, state_sha256 = _live_fallback_state(state_path)
        _validate_state_document(state, state_path.parent, queue, queue_sha256)
        existing = _state_items(state).get(queue_id)
        _validate_live_fallback_source(existing, queue_item, reason)
        if (
            reason
            in {
                LIVE_FALLBACK_HYPOTHESES_EXHAUSTED,
                LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED,
            }
            and state.get("active") is not None
        ):
            raise BulkGenerationError(
                "Recovery fallback requires an inactive generation state"
            )
        previous_sha256 = None if existing is None else _canonical_sha256(existing)
        evidence, evidence_sources = _live_fallback_evidence(
            reason,
            state_path,
            queue_path,
            state_sha256,
            queue_sha256,
            queue_id,
            queue_item,
            existing,
            evidence_workspaces,
            evidence_reviews,
        )
        requested = synthesis_character_for_line(
            queue_item.speaker, queue_item.voice_character
        )
        decision: JsonDocument = _live_fallback_decision(
            schema_version=(
                LIVE_FALLBACK_AUTOMATIC_RECOVERY_VERSION
                if reason == LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED
                else (
                    LIVE_FALLBACK_REVIEW_EVIDENCE_VERSION
                    if isinstance(evidence, dict)
                    and evidence.get("schema_version") == 2
                    else (
                        LIVE_FALLBACK_EVIDENCE_VERSION
                        if evidence is not None
                        else LIVE_FALLBACK_VERSION
                    )
                )
            ),
            reason=reason,
            provider=provider,
            model=model,
            generation_profile=generation_profile,
            queue_id=queue_id,
            line_id=queue_item.line_id,
            text_sha256=queue_item.text_sha256,
            speaker=queue_item.speaker,
            requested_voice_character=requested,
            previous_result_sha256=previous_sha256,
            decided_at=datetime.now(timezone.utc).isoformat(),
            evidence=evidence,
        )
        proposed = _live_fallback_proposed_state(
            state, existing, queue_id, queue_item, requested, reason, decision
        )
        _validate_state_document(proposed, state_path.parent, queue, queue_sha256)
        entries = _approved_manifest_entries(proposed, state_path.parent)
        transaction_id = secrets.token_hex(16)
        staged_state = state_path.with_name(f".{state_path.name}.{transaction_id}.tmp")
        manifest_path = state_path.parent / "manifest.json"
        staged_manifest = manifest_path.with_name(
            f".{manifest_path.name}.{transaction_id}.tmp"
        )
        _commit_live_fallback(
            staged_state,
            staged_manifest,
            state_path,
            queue_path,
            queue_sha256,
            state_sha256,
            evidence_sources,
            proposed,
            queue,
            lease,
            entries,
        )
        progress_manifest = state_path.parent / RUNTIME_PROGRESS_MANIFEST_NAME
        if progress_manifest.is_file():
            write_runtime_progress_manifest_from_state(
                proposed,
                state_path.parent,
                progress_manifest,
                validate_files=False,
            )
    return decision


def _live_fallback_backend(
    reason: str, provider: str, model: object, profile: object
) -> tuple[str, str, str]:
    if reason not in LIVE_FALLBACK_REASONS:
        raise BulkGenerationError("Live fallback reason is unsupported")
    if provider != "pocket-tts":
        raise BulkGenerationError("Live fallback currently requires Pocket TTS")
    provider, model, profile = (
        _required_text(provider, "Live fallback provider"),
        _required_text(model, "Live fallback model"),
        _required_text(profile, "Live fallback generation profile"),
    )
    if model != "pocket-tts" or profile != "default":
        raise BulkGenerationError(
            "Live fallback requires the exact Pocket TTS default model/profile"
        )
    return provider, model, profile


def _live_fallback_state(path: Path) -> tuple[JsonDocument, str]:
    try:
        state, digest, _payload = generation_state_snapshot(path)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise BulkGenerationError(
            f"Unable to read generation state {path}: {error}"
        ) from error
    if not _is_json_document(state):
        raise BulkGenerationError("Generation state must be a JSON object")
    return state, digest


def _live_fallback_evidence(
    reason: str,
    state_path: Path,
    queue_path: Path,
    state_sha256: str,
    queue_sha256: str,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem,
    existing: JsonDocument | None,
    workspaces: Sequence[str | Path],
    reviews: Sequence[str | Path],
) -> tuple[JsonDocument | None, tuple[tuple[Path, str], ...]]:
    if reason == LIVE_FALLBACK_HYPOTHESES_EXHAUSTED:
        if tuple(workspaces) and tuple(reviews):
            raise BulkGenerationError(
                "Live fallback cannot mix repair-workspace and render-review evidence"
            )
        if tuple(reviews):
            return _capture_render_review_fallback_evidence(
                state_path, queue_path, queue_sha256, queue_item, existing, reviews
            )
        return _capture_live_fallback_evidence(
            state_path, queue_path, queue_sha256, queue_item, existing, workspaces
        )
    if reason == LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED:
        if tuple(workspaces) or tuple(reviews):
            raise BulkGenerationError(
                "Automatic-recovery fallback derives its evidence from the current generation state"
            )
        if existing is None:
            raise BulkGenerationError(
                "Automatic-recovery fallback requires an exact terminal Pocket failure"
            )
        return _automatic_recovery_fallback_evidence(
            state_path, queue_path, state_sha256, queue_sha256, queue_id, existing
        ), ()
    if tuple(workspaces) or tuple(reviews):
        raise BulkGenerationError(
            "Live fallback evidence sources require the generation_hypotheses_exhausted reason"
        )
    return None, ()


def _live_fallback_proposed_state(
    state: JsonDocument,
    existing: JsonDocument | None,
    queue_id: str,
    item: VoiceGenerationQueueItem,
    requested: str,
    reason: str,
    decision: JsonDocument,
) -> JsonDocument:
    proposed, proposed_item = (
        copy.deepcopy(state),
        copy.deepcopy(existing) if existing else {},
    )
    if reason == "generated_audio_rejected":
        proposed_item["live_fallback"], proposed_item["updated_at"] = (
            decision,
            datetime.now(timezone.utc).isoformat(),
        )
    else:
        for field in (
            "path",
            "file_sha256",
            "quality",
            "speech_quality",
            "review_status",
        ):
            proposed_item.pop(field, None)
        proposed_item.update(
            {
                "status": "live_fallback",
                "review_status": "live_fallback",
                "attempts": _nonnegative_int(
                    proposed_item.get("attempts", 0), "Live fallback attempts"
                ),
                "line_id": item.line_id,
                "text_sha256": item.text_sha256,
                "speaker": item.speaker,
                "requested_voice_character": requested,
                "voice_character": proposed_item.get("voice_character", requested),
                "live_fallback": decision,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    _state_items(proposed)[queue_id] = proposed_item
    return proposed


def _commit_live_fallback(
    staged_state: Path,
    staged_manifest: Path,
    state_path: Path,
    queue_path: Path,
    queue_sha256: str,
    state_sha256: str,
    evidence_sources: Sequence[tuple[Path, str]],
    proposed: JsonDocument,
    queue: VoiceGenerationQueue,
    lease: GenerationLease,
    entries: list[dict[str, object]],
) -> None:
    try:
        write_generation_state(staged_state, proposed)
        _write_generated_manifest_from_state(
            proposed, state_path.parent, staged_manifest, entries=entries
        )
        _assert_live_fallback_commit_sources(
            state_path, queue_path, state_sha256, queue_sha256, evidence_sources
        )
        _validate_state_document(proposed, state_path.parent, queue, queue_sha256)
        lease.assert_owned()
        os.replace(staged_state, state_path)
        try:
            lease.assert_owned()
        except BulkGenerationError as error:
            raise BulkGenerationError(
                f"Live fallback was saved, but manifest rebuild was blocked: {error}"
            ) from error
        os.replace(staged_manifest, state_path.parent / "manifest.json")
        lease.mark_committed()
    finally:
        for path in (staged_state, staged_manifest):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass


@publication_errors(BulkGenerationError)
def _assert_live_fallback_commit_sources(
    state_path: Path,
    queue_path: Path,
    state_sha256: str,
    queue_sha256: str,
    evidence_sources: Sequence[tuple[Path, str]],
) -> None:
    if sha256_file(queue_path) != queue_sha256:
        raise BulkGenerationSourceChangedError(
            "Generation queue changed before live fallback commit"
        )
    if sha256_file(state_path) != state_sha256:
        raise BulkGenerationSourceChangedError(
            "Generation state changed before live fallback commit"
        )
    for path, digest in evidence_sources:
        if not path.is_file() or sha256_file(path) != digest:
            raise BulkGenerationSourceChangedError(
                "Live fallback evidence changed before commit"
            )
        if path.name == "generation-state.sqlite3" and any(
            path.parent.rglob("*.partial.wav")
        ):
            raise BulkGenerationSourceChangedError(
                "Live fallback evidence became active before commit"
            )


def _validate_live_fallback_source(
    existing: JsonDocument | None,
    queue_item: VoiceGenerationQueueItem,
    reason: str,
) -> None:
    failure = existing.get("failure") if existing is not None else None
    validators = {
        LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED: _validate_automatic_recovery_fallback_source,
        "reference_unavailable_after_audit": _validate_reference_unavailable_fallback_source,
        LIVE_FALLBACK_HYPOTHESES_EXHAUSTED: _validate_hypotheses_fallback_source,
        "generated_audio_rejected": _validate_rejected_audio_fallback_source,
    }
    validator = validators.get(reason, _validate_offline_exhausted_fallback_source)
    validator(existing, failure)


def _validate_automatic_recovery_fallback_source(
    existing: JsonDocument | None, failure: object
) -> None:
    provider = existing.get("provider") if isinstance(existing, dict) else None
    if (
        not isinstance(existing, dict)
        or existing.get("status") != "failed"
        or not _is_json_document(failure)
        or failure.get("kind") in {"cancelled", "interrupted"}
        or provider == "pocket-tts"
        and (
            existing.get("model") != "pocket-tts"
            or existing.get("generation_profile") != "default"
        )
        or provider == "moss-tts"
        and failure.get("kind") not in {"missed_eos_audio_limit", "speech_silence"}
        or provider not in {"moss-tts", "pocket-tts"}
    ):
        raise BulkGenerationError(
            "Automatic-recovery fallback requires an exact terminal speech failure"
        )


def _validate_reference_unavailable_fallback_source(
    existing: JsonDocument | None, failure: object
) -> None:
    if existing is not None and (
        existing.get("status") != "failed"
        or not _is_json_document(failure)
        or failure.get("kind") != "reference_unavailable"
    ):
        raise BulkGenerationError(
            "Reference-unavailable fallback requires no result or an exact reference-unavailable failure"
        )


def _validate_hypotheses_fallback_source(
    existing: JsonDocument | None, failure: object
) -> None:
    if not isinstance(existing, dict):
        raise BulkGenerationError("Live fallback requires an existing outcome")
    if (
        existing.get("status") != "failed"
        or existing.get("provider") != "moss-tts"
        or not _is_json_document(failure)
        or failure.get("kind") not in {"missed_eos_audio_limit", "speech_silence"}
    ):
        raise BulkGenerationError(
            "Exhausted-hypothesis fallback requires an exact typed failed MOSS current-control outcome"
        )


def _validate_rejected_audio_fallback_source(
    existing: JsonDocument | None, _failure: object
) -> None:
    if not isinstance(existing, dict):
        raise BulkGenerationError("Live fallback requires an existing outcome")
    if (existing.get("status"), existing.get("review_status")) != (
        "generated",
        "rejected",
    ):
        raise BulkGenerationError(
            "Rejected-audio fallback requires a reviewed rejected WAV"
        )


def _validate_offline_exhausted_fallback_source(
    existing: JsonDocument | None, _failure: object
) -> None:
    if not isinstance(existing, dict):
        raise BulkGenerationError("Live fallback requires an existing outcome")
    repair = existing.get("failure_repair")
    if (
        existing.get("status") != "failed"
        or existing.get("provider") != "pocket-tts"
        or not isinstance(existing.get("failure"), dict)
        or not isinstance(repair, dict)
        or repair.get("strategy") != OFFLINE_FALLBACK_BACKEND
    ):
        raise BulkGenerationError(
            "Offline-exhausted live fallback requires a typed failed Pocket fallback"
        )


def _automatic_recovery_fallback_evidence(
    state_path: Path,
    queue_path: Path,
    state_sha256: str,
    queue_sha256: str,
    queue_id: str,
    existing: JsonDocument,
) -> JsonDocument:
    plan = generation_failure_repair_plan(state_path, queue_path)
    if (
        plan.get("state_sha256") != state_sha256
        or plan.get("queue_sha256") != queue_sha256
    ):
        raise BulkGenerationSourceChangedError(
            "Generation evidence changed while automatic fallback was planned"
        )
    records = plan.get("records")
    if not _is_object_list(records):
        raise BulkGenerationError("Automatic recovery evidence records are malformed")
    record = next(
        (
            value
            for value in records
            if _is_json_document(value) and value.get("queue_id") == queue_id
        ),
        None,
    )
    action = record.get("action") if record is not None else None
    if action not in AUTOMATIC_RECOVERY_LIVE_FALLBACK_ACTIONS:
        raise BulkGenerationError(
            "Automatic live fallback cannot replace a remaining safe repair"
        )
    existing_failure = existing.get("failure")
    if not _is_json_document(existing_failure):
        raise BulkGenerationError(
            "Automatic-recovery fallback requires an exact terminal speech failure"
        )
    return {
        "schema": AUTOMATIC_RECOVERY_LIVE_FALLBACK_EVIDENCE_SCHEMA,
        "schema_version": 1,
        "queue_sha256": queue_sha256,
        "queue_id": queue_id,
        "base_result_sha256": _canonical_sha256(existing),
        "base_result": copy.deepcopy(existing),
        "recovery_action": action,
        "failure_kind": _generation_text(existing_failure.get("kind"), "Failure kind"),
    }


def _capture_live_fallback_evidence(
    state_path: Path,
    queue_path: Path,
    queue_sha256: str,
    queue_item: VoiceGenerationQueueItem,
    existing: JsonDocument | None,
    evidence_workspaces: Sequence[str | Path],
) -> tuple[JsonDocument, tuple[tuple[Path, str], ...]]:
    if existing is None:
        raise BulkGenerationError("Live fallback requires an existing outcome")
    values = tuple(Path(value).expanduser().resolve() for value in evidence_workspaces)
    if not values:
        raise BulkGenerationError(
            "Exhausted-hypothesis fallback requires an evidence workspace"
        )
    if len(values) != len(set(values)):
        raise BulkGenerationError("Live fallback evidence workspace is duplicated")
    base_root = state_path.parent.parent
    base_workspace_path = base_root / "workspace.json"
    try:
        base_workspace_payload = read_regular_bytes(base_workspace_path)
        base_workspace = decode_json(base_workspace_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise BulkGenerationError(
            f"Unable to read live fallback base workspace: {error}"
        ) from error
    _base_workspace_id, base_import = _live_fallback_workspace_import(
        base_workspace, "base"
    )
    if queue_path != base_root / "queue.jsonl":
        raise BulkGenerationError(
            "Live fallback queue must belong to its base workspace"
        )
    base_result_sha256 = _canonical_sha256(existing)
    hypotheses: list[JsonDocument] = []
    snapshots: list[tuple[Path, str]] = []
    for directory in values:
        if directory == base_root:
            raise BulkGenerationError(
                "Live fallback evidence must differ from the current workspace"
            )
        hypothesis, source_snapshots = _live_fallback_workspace_hypothesis(
            directory,
            base_root,
            base_import,
            queue_sha256,
            queue_item,
            base_result_sha256,
        )
        hypotheses.append(hypothesis)
        snapshots.extend(source_snapshots)
    hypotheses.sort(
        key=lambda value: (
            _generation_text(value.get("workspace_id"), "Evidence workspace ID"),
            _generation_text(value.get("result_sha256"), "Evidence result SHA-256"),
        )
    )
    evidence: JsonDocument = {
        "schema": LIVE_FALLBACK_EVIDENCE_SCHEMA,
        "schema_version": 1,
        "queue_sha256": queue_sha256,
        "base_result_sha256": base_result_sha256,
        "hypotheses": hypotheses,
    }
    _validate_live_fallback_evidence(evidence, base_result_sha256)
    snapshots.extend(
        (
            (base_workspace_path, hashlib.sha256(base_workspace_payload).hexdigest()),
            (queue_path, queue_sha256),
        )
    )
    return evidence, tuple(snapshots)


def _live_fallback_workspace_hypothesis(
    directory: Path,
    base_root: Path,
    base_import: str,
    queue_sha256: str,
    queue_item: VoiceGenerationQueueItem,
    base_result_sha256: str,
) -> tuple[JsonDocument, tuple[tuple[Path, str], ...]]:
    workspace_path = directory / "workspace.json"
    source_queue_path = directory / "queue.jsonl"
    source_state_path = directory / "generated-audio" / "generation-state.sqlite3"
    try:
        workspace_payload = read_regular_bytes(workspace_path)
        source_queue_payload = read_regular_bytes(source_queue_path)
        source_state_payload = read_regular_bytes(source_state_path)
        workspace = decode_json(workspace_payload.decode("utf-8"))
        source_state = decode_generation_state_snapshot(source_state_payload)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise BulkGenerationError(
            f"Unable to read live fallback evidence workspace {directory}: {error}"
        ) from error
    workspace_id, source_import = _live_fallback_workspace_import(workspace, "evidence")
    if source_import != base_import:
        raise BulkGenerationError(
            "Live fallback evidence immutable import differs from its base"
        )
    source_queue_sha256 = hashlib.sha256(source_queue_payload).hexdigest()
    if source_queue_sha256 != queue_sha256:
        raise BulkGenerationError("Live fallback evidence queue differs from its base")
    source_queue, stable_queue_sha256 = _load_stable_queue(source_queue_path)
    if stable_queue_sha256 != source_queue_sha256:
        raise BulkGenerationSourceChangedError(
            "Live fallback evidence queue changed while it was read"
        )
    if not _is_json_document(source_state):
        raise BulkGenerationError(
            "Live fallback evidence generation state is malformed"
        )
    if (
        type(source_state.get("schema_version")) is not int
        or (source_state.get("schema"), source_state.get("schema_version"))
        != (STATE_SCHEMA, STATE_VERSION)
        or source_state.get("queue_sha256") != source_queue_sha256
        or not isinstance(source_state.get("items"), dict)
    ):
        raise BulkGenerationError(
            "Live fallback evidence generation state is malformed"
        )
    if source_state.get("active") is not None or any(
        source_state_path.parent.rglob("*.partial.wav")
    ):
        raise BulkGenerationError(
            "Live fallback evidence workspace is active or incomplete"
        )
    source_result = _state_items(source_state).get(queue_item.queue_id)
    if not isinstance(source_result, dict) or source_result.get("status") != "failed":
        raise BulkGenerationError(
            "Live fallback evidence must contain the exact failed queue item"
        )
    source_queue_item = next(
        item for item in source_queue.items if item.queue_id == queue_item.queue_id
    )
    _validate_failure_record(
        source_result.get("failure"),
        queue_item.queue_id,
        result=source_result,
    )
    _validate_synthesis_identity(source_result, queue_item.queue_id, source_queue_item)
    _validate_failure_repair_record(
        source_result, queue_item.queue_id, source_queue_item
    )
    _validate_seed_application(source_result, queue_item.queue_id)
    repair = source_result.get("failure_repair")
    carry = source_result.get("carry_forward")
    if (
        not isinstance(repair, dict)
        or repair.get("strategy") != SENTENCE_BOUNDARY_SEGMENTATION
        or not isinstance(carry, dict)
        or carry.get("source_item_sha256") != base_result_sha256
    ):
        raise BulkGenerationError(
            "Live fallback evidence must be a completed sentence-boundary "
            "repair carried from the exact current item"
        )
    workspace_sha256 = hashlib.sha256(workspace_payload).hexdigest()
    state_sha256 = hashlib.sha256(source_state_payload).hexdigest()
    result_sha256 = _canonical_sha256(source_result)
    return {
        "workspace_id": workspace_id,
        "workspace_sha256": workspace_sha256,
        "state_sha256": state_sha256,
        "queue_sha256": source_queue_sha256,
        "result_sha256": result_sha256,
        "strategy": SENTENCE_BOUNDARY_SEGMENTATION,
        "result": copy.deepcopy(source_result),
    }, (
        (workspace_path, workspace_sha256),
        (source_queue_path, source_queue_sha256),
        (source_state_path, state_sha256),
    )


def _capture_render_review_fallback_evidence(
    state_path: Path,
    queue_path: Path,
    queue_sha256: str,
    queue_item: VoiceGenerationQueueItem,
    existing: JsonDocument | None,
    evidence_reviews: Sequence[str | Path],
) -> tuple[JsonDocument, tuple[tuple[Path, str], ...]]:
    from vntts.authoring.render_hypothesis_records import (
        RenderHypothesisRecordError,
        load_render_hypothesis_record,
    )

    if existing is None:
        raise BulkGenerationError("Live fallback requires an existing outcome")
    values = tuple(Path(value).expanduser().resolve() for value in evidence_reviews)
    if not values:
        raise BulkGenerationError(
            "Exhausted-hypothesis fallback requires a render review"
        )
    if len(values) != len(set(values)):
        raise BulkGenerationError("Live fallback render review is duplicated")
    base_root = state_path.parent.parent
    if queue_path != base_root / "queue.jsonl":
        raise BulkGenerationError(
            "Live fallback queue must belong to its base workspace"
        )
    base_result_sha256 = _canonical_sha256(existing)
    hypotheses: list[JsonDocument] = []
    snapshots: list[tuple[Path, str]] = [(queue_path, queue_sha256)]
    for directory in values:
        try:
            record = load_render_hypothesis_record(directory)
        except RenderHypothesisRecordError as error:
            raise BulkGenerationError(
                f"Unable to read live fallback render review {directory}: {error}"
            ) from error
        review = record.review
        decision = record.decision
        decision_snapshot = record.decision_snapshot
        if (
            not isinstance(decision, dict)
            or decision_snapshot is None
            or decision.get("decision") != "need_different"
        ):
            raise BulkGenerationError(
                "Live fallback render review must be terminal need_different"
            )
        if (
            review.get("queue_id") != queue_item.queue_id
            or review.get("line_id") != queue_item.line_id
            or review.get("text_sha256") != queue_item.text_sha256
        ):
            raise BulkGenerationError(
                "Live fallback render review queue identity changed"
            )
        source_files = tuple(
            (snapshot.path, snapshot.sha256) for snapshot in record.snapshots
        )
        hypotheses.append(
            {
                "kind": "render_hypothesis_review",
                "review_id": review["review_id"],
                "review_sha256": record.review_snapshot.sha256,
                "review_document_sha256": _canonical_sha256(review),
                "decision_sha256": decision_snapshot.sha256,
                "decision_document_sha256": _canonical_sha256(decision),
                "comparison_sha256": review["comparison_sha256"],
                "arm_report_sha256": review["arm_report_sha256"],
                "reference_sha256": review["reference_sha256"],
                "result_sha256": review["result_sha256"],
                "decision": "need_different",
                "review": review,
                "decision_document": decision,
            }
        )
        snapshots.extend(source_files)
    hypotheses.sort(
        key=lambda value: (
            _generation_text(value.get("kind"), "Render review kind"),
            _generation_text(value.get("review_id"), "Render review ID"),
        )
    )
    evidence: JsonDocument = {
        "schema": LIVE_FALLBACK_EVIDENCE_SCHEMA,
        "schema_version": 2,
        "queue_sha256": queue_sha256,
        "base_result_sha256": base_result_sha256,
        "hypotheses": hypotheses,
    }
    _validate_live_fallback_evidence(evidence, base_result_sha256)
    return evidence, tuple(snapshots)


def _live_fallback_workspace_import(workspace: object, label: str) -> tuple[str, str]:
    if (
        not isinstance(workspace, dict)
        or workspace.get("schema") != "vntts.authoring-workspace"
        or not has_schema_version(workspace, 1)
        or not isinstance(workspace.get("workspace_id"), str)
        or not workspace["workspace_id"].startswith("resume-")
        or not isinstance(workspace.get("source"), dict)
        or not isinstance(workspace["source"].get("import_id"), str)
        or not workspace["source"]["import_id"]
    ):
        raise BulkGenerationError(
            f"Live fallback {label} workspace authority is malformed"
        )
    return workspace["workspace_id"], workspace["source"]["import_id"]
