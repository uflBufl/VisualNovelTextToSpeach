"""Leaf loading primitives for authoritative generation state inputs."""

from __future__ import annotations

import copy
import hashlib
import importlib
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Callable, TypeAlias

import numpy as np
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
    VoiceGenerationQueueItem,
)
from vntts_artifacts.voice_manifest import normalize_character_name

from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.failure_repair import (
    BOUNDED_SEED_RETRY,
    EDGE_SILENCE_TRIM,
    INLINE_PAUSE_MARKER,
    MAX_BOUNDED_TOTAL_ATTEMPTS,
    OFFLINE_FALLBACK_BACKEND,
    SENTENCE_BOUNDARY_SEGMENTATION,
    FailureRepairPolicy,
    FailureRepairPolicyError,
    inline_sentence_pause_prompt,
    safe_sentence_segments,
)
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.generation_manifest import (
    AudioQuality,
    contained_generation_path,
    safe_generation_relative_path,
    validate_success_file_with_samples,
)
from vntts.authoring.missing_voice_policy import (
    MissingVoicePolicy,
    MissingVoicePolicyError,
)
from vntts.authoring.speech_quality import (
    LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION,
    NOTABLE_SILENCE_SPAN_SECONDS,
    PAUSE_DIAGNOSIS_VERSION,
    SPEECH_QUALITY_ANALYSIS_VERSION,
    measure_generated_speech_samples,
)
from vntts.authoring.terminal_conflict_records import (
    TerminalConflictRecordError,
    validate_terminal_conflict_item_provenance,
)
from vntts.document_identity import is_lowercase_sha256
from vntts.synthesis import SynthesisCompletion
from vntts.voices import synthesis_character_for_line

STATE_SCHEMA = "vntts.authoring-generation-state"
STATE_VERSION = 1
LEGACY_STATE_SCHEMA = "r1999.bulk-generation-state"
LEGACY_STATE_VERSION = 1
LIVE_FALLBACK_SCHEMA = "vntts.authoring-live-fallback-decision"
LIVE_FALLBACK_VERSION = 1
LIVE_FALLBACK_EVIDENCE_VERSION = 2
LIVE_FALLBACK_REVIEW_EVIDENCE_VERSION = 3
LIVE_FALLBACK_MISSING_VOICE_EVIDENCE_VERSION = 4
LIVE_FALLBACK_KNOWN_ROLE_EVIDENCE_VERSION = 5
LIVE_FALLBACK_AUDIO_EVENT_PROJECTION_VERSION = 6
LIVE_FALLBACK_REVIEWED_REJECTION_VERSION = 7
LIVE_FALLBACK_AUTOMATIC_RECOVERY_VERSION = 8
LIVE_FALLBACK_EVIDENCE_SCHEMA = "vntts.authoring-live-fallback-evidence"
MISSING_VOICE_LIVE_FALLBACK_EVIDENCE_SCHEMA = (
    "vntts.authoring-missing-voice-live-fallback-evidence"
)
KNOWN_ROLE_LIVE_FALLBACK_EVIDENCE_SCHEMA = (
    "vntts.authoring-known-role-live-fallback-evidence"
)
AUDIO_EVENT_PROJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA = (
    "vntts.authoring-audio-event-projection-live-fallback-evidence"
)
REVIEWED_REJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA = (
    "vntts.authoring-reviewed-rejection-live-fallback-evidence"
)
AUTOMATIC_RECOVERY_LIVE_FALLBACK_EVIDENCE_SCHEMA = (
    "vntts.self-service-automatic-recovery-live-fallback-evidence"
)
AUDIO_EVENT_OMISSION_SCHEMA = "vntts.authoring-audio-event-omission"
AUDIO_EVENT_OMISSION_VERSION = 1
AUDIO_EVENT_OMISSION_REASON = "no_validated_source_or_supported_generator"
REVIEWED_WAVEFORM_PUBLICATION_SCHEMA = "vntts.authoring-reviewed-waveform-publication"
REVIEWED_WAVEFORM_PUBLICATION_VERSION = 1
REVIEWED_WAVEFORM_PUBLICATION_REASON = "legacy_control_inventory_unavailable"
LIVE_FALLBACK_HYPOTHESES_EXHAUSTED = "generation_hypotheses_exhausted"
LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED = "automatic_recovery_exhausted"
LIVE_FALLBACK_REASONS = frozenset(
    {
        "offline_fallback_exhausted",
        "reference_unavailable_after_audit",
        "generated_audio_rejected",
        LIVE_FALLBACK_HYPOTHESES_EXHAUSTED,
        LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED,
    }
)
FAILURE_KINDS = {
    "missed_eos_audio_limit",
    "speech_silence",
    "reference_unavailable",
    "backend_error",
    "cancelled",
    "interrupted",
}

StateObject: TypeAlias = dict[str, object]
QueueById: TypeAlias = dict[str, VoiceGenerationQueueItem]


def load_stable_generation_queue(
    queue_path: str | Path,
) -> tuple[VoiceGenerationQueue, str]:
    """Load a queue from one immutable byte snapshot and return its SHA-256."""
    queue_path = Path(queue_path)
    try:
        payload = queue_path.read_bytes()
    except OSError as error:
        raise BulkGenerationError(str(error)) from error
    digest = hashlib.sha256(payload).hexdigest()
    try:
        with TemporaryDirectory() as directory:
            snapshot = Path(directory) / "queue.jsonl"
            snapshot.write_bytes(payload)
            queue = VoiceGenerationQueue.load(snapshot)
    except (OSError, VoiceGenerationQueueError) as error:
        raise BulkGenerationError(str(error)) from error
    return queue, digest


def _validate_live_fallback_evidence(
    evidence: object, previous_result_sha256: object
) -> None:
    if isinstance(evidence, dict) and evidence.get("schema_version") == 2:
        return _validate_render_review_fallback_evidence(
            evidence, previous_result_sha256
        )
    if (
        not isinstance(evidence, dict)
        or set(evidence)
        != {
            "schema",
            "schema_version",
            "queue_sha256",
            "base_result_sha256",
            "hypotheses",
        }
        or evidence.get("schema") != LIVE_FALLBACK_EVIDENCE_SCHEMA
        or evidence.get("schema_version") != 1
        or evidence.get("base_result_sha256") != previous_result_sha256
    ):
        raise BulkGenerationError("Live fallback evidence authority is malformed")
    _required_sha256(evidence.get("queue_sha256"), "Live fallback evidence queue")
    _required_sha256(
        evidence.get("base_result_sha256"), "Live fallback evidence base result"
    )
    hypotheses = evidence.get("hypotheses")
    if not isinstance(hypotheses, list) or not hypotheses:
        raise BulkGenerationError("Live fallback evidence hypothesis ledger is empty")
    observed_order = []
    for hypothesis in hypotheses:
        if (
            not isinstance(hypothesis, dict)
            or set(hypothesis)
            != {
                "workspace_id",
                "workspace_sha256",
                "state_sha256",
                "queue_sha256",
                "result_sha256",
                "strategy",
                "result",
            }
            or hypothesis.get("strategy") != SENTENCE_BOUNDARY_SEGMENTATION
            or hypothesis.get("queue_sha256") != evidence["queue_sha256"]
            or not isinstance(hypothesis.get("workspace_id"), str)
            or not hypothesis["workspace_id"].startswith("resume-")
        ):
            raise BulkGenerationError("Live fallback evidence hypothesis is malformed")
        for field in ("workspace_sha256", "state_sha256", "result_sha256"):
            _required_sha256(hypothesis.get(field), f"Live fallback evidence {field}")
        result = hypothesis.get("result")
        repair = result.get("failure_repair") if isinstance(result, dict) else None
        carry = result.get("carry_forward") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or canonical_document_sha256(result) != hypothesis["result_sha256"]
            or result.get("status") != "failed"
            or not isinstance(result.get("failure"), dict)
            or not isinstance(repair, dict)
            or repair.get("strategy") != hypothesis["strategy"]
            or not isinstance(carry, dict)
            or carry.get("source_item_sha256") != evidence["base_result_sha256"]
        ):
            raise BulkGenerationError("Live fallback evidence result authority changed")
        observed_order.append((hypothesis["workspace_id"], hypothesis["result_sha256"]))
    if observed_order != sorted(observed_order) or len(observed_order) != len(
        set(observed_order)
    ):
        raise BulkGenerationError("Live fallback evidence hypotheses are not canonical")


def _validate_render_review_fallback_evidence(
    evidence: StateObject, previous_result_sha256: object
) -> None:
    if (
        set(evidence)
        != {
            "schema",
            "schema_version",
            "queue_sha256",
            "base_result_sha256",
            "hypotheses",
        }
        or evidence.get("schema") != LIVE_FALLBACK_EVIDENCE_SCHEMA
        or evidence.get("schema_version") != 2
        or evidence.get("base_result_sha256") != previous_result_sha256
    ):
        raise BulkGenerationError("Live fallback review evidence is malformed")
    _required_sha256(evidence.get("queue_sha256"), "Live fallback evidence queue")
    _required_sha256(
        evidence.get("base_result_sha256"), "Live fallback evidence base result"
    )
    hypotheses = evidence.get("hypotheses")
    if not isinstance(hypotheses, list) or not hypotheses:
        raise BulkGenerationError("Live fallback review evidence is empty")
    fields = {
        "kind",
        "review_id",
        "review_sha256",
        "review_document_sha256",
        "decision_sha256",
        "decision_document_sha256",
        "comparison_sha256",
        "arm_report_sha256",
        "reference_sha256",
        "result_sha256",
        "decision",
        "review",
        "decision_document",
    }
    order = []
    for hypothesis in hypotheses:
        if (
            not isinstance(hypothesis, dict)
            or set(hypothesis) != fields
            or hypothesis.get("kind") != "render_hypothesis_review"
            or hypothesis.get("decision") != "need_different"
            or not isinstance(hypothesis.get("review"), dict)
            or not isinstance(hypothesis.get("decision_document"), dict)
        ):
            raise BulkGenerationError(
                "Live fallback render-review hypothesis is malformed"
            )
        for field in fields - {
            "kind",
            "decision",
            "review",
            "decision_document",
        }:
            _required_sha256(
                hypothesis.get(field), f"Live fallback render review {field}"
            )
        review = hypothesis["review"]
        decision = hypothesis["decision_document"]
        if (
            canonical_document_sha256(review) != hypothesis["review_document_sha256"]
            or canonical_document_sha256(decision)
            != hypothesis["decision_document_sha256"]
            or review.get("review_id") != hypothesis["review_id"]
            or review.get("comparison_sha256") != hypothesis["comparison_sha256"]
            or review.get("arm_report_sha256") != hypothesis["arm_report_sha256"]
            or review.get("reference_sha256") != hypothesis["reference_sha256"]
            or review.get("result_sha256") != hypothesis["result_sha256"]
            or decision.get("schema") != "vntts.authoring-render-hypothesis-decision"
            or decision.get("schema_version") != 1
            or decision.get("review_id") != hypothesis["review_id"]
            or decision.get("review_sha256") != hypothesis["review_sha256"]
            or decision.get("reference_sha256") != hypothesis["reference_sha256"]
            or decision.get("result_sha256") != hypothesis["result_sha256"]
            or decision.get("decision") != hypothesis["decision"]
        ):
            raise BulkGenerationError("Live fallback render-review authority changed")
        order.append((hypothesis["kind"], hypothesis["review_id"]))
    if order != sorted(order) or len(order) != len(set(order)):
        raise BulkGenerationError(
            "Live fallback render-review hypotheses are not canonical"
        )


def _validate_state_document(
    state: StateObject,
    output_directory: Path,
    queue: VoiceGenerationQueue | None,
    queue_sha256: str | None,
) -> None:
    schema_pair = (state.get("schema"), state.get("schema_version"))
    if schema_pair not in {
        (STATE_SCHEMA, STATE_VERSION),
        (LEGACY_STATE_SCHEMA, LEGACY_STATE_VERSION),
    }:
        raise BulkGenerationError(
            f"Unsupported generation state schema: {schema_pair!r}"
        )
    if queue_sha256 is not None and state.get("queue_sha256") != queue_sha256:
        raise BulkGenerationError(
            "Generation queue changed; use a new output directory"
        )
    _required_sha256(state.get("queue_sha256"), "Generation state queue_sha256")
    _validate_state_metadata(state, queue)
    state_items = state.get("items")
    if not isinstance(state_items, dict):
        raise BulkGenerationError("Generation state items must be an object")
    _validate_synthesis_controls(state)
    queue_by_id = (
        None if queue is None else {item.queue_id: item for item in queue.items}
    )
    for queue_id, result in state_items.items():
        _validate_state_item(
            queue_id, result, output_directory, queue_by_id, state["schema"]
        )
    _validate_reviewed_waveform_publication(state, queue_by_id)
    _validate_state_active(state, queue_by_id)


def _validate_state_metadata(
    state: StateObject, queue: VoiceGenerationQueue | None
) -> None:
    for field in ("game", "language"):
        value = state.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise BulkGenerationError(
                f"Generation state {field} must be non-empty text or null"
            )
        if (
            queue is not None
            and not (state["schema"] == LEGACY_STATE_SCHEMA and value is None)
            and value != queue.metadata.get(field)
        ):
            raise BulkGenerationError(
                f"Generation state {field} does not match the queue metadata"
            )


def _validate_state_item(
    queue_id: object,
    result: object,
    output_directory: Path,
    queue_by_id: QueueById | None,
    state_schema: object,
) -> None:
    if not isinstance(queue_id, str) or (
        queue_by_id is not None and queue_id not in queue_by_id
    ):
        raise BulkGenerationError(
            f"Generation state references unknown queue_id {queue_id!r}"
        )
    if not isinstance(result, dict):
        raise BulkGenerationError(f"Generation state item {queue_id!r} is invalid")
    _validate_terminal_conflict_resolution(result)
    status = _validate_state_item_status(result, queue_id)
    _provider_attempts(
        result,
        _nonnegative_int(result.get("attempts", 0), f"Item {queue_id!r} attempts"),
    )
    queue_item = None if queue_by_id is None else queue_by_id[queue_id]
    _validate_live_fallback_decision(result, queue_id, queue_item)
    if status == "omitted":
        _validate_audio_event_omission(result, queue_id, queue_item)
        return
    if status in {"failed", "live_fallback"}:
        if "failure" in result:
            _validate_failure_record(result["failure"], queue_id, result=result)
        _validate_synthesis_identity(result, queue_id, queue_item)
    else:
        _validate_success_item(
            queue_id, result, output_directory, queue_item, state_schema=state_schema
        )
    _validate_failure_repair_record(result, queue_id, queue_item)
    _validate_seed_application(result, queue_id)


def _validate_terminal_conflict_resolution(result: StateObject) -> None:
    if "terminal_conflict_resolution" not in result:
        return
    try:
        validate_terminal_conflict_item_provenance(
            result["terminal_conflict_resolution"]
        )
    except TerminalConflictRecordError as error:
        raise BulkGenerationError(str(error)) from error


def _validate_state_item_status(result: StateObject, queue_id: str) -> object:
    valid: dict[object, set[object]] = {
        "failed": {None},
        "generated": {"pending_review", "rejected"},
        "approved": {"approved"},
        "live_fallback": {"live_fallback"},
        "omitted": {"omitted"},
    }
    status = result.get("status")
    review = result.get("review_status")
    if status not in valid or review not in valid[status]:
        raise BulkGenerationError(
            f"Generation state item {queue_id!r} has invalid {status!r}/{review!r} status"
        )
    return status


def _validate_state_active(state: StateObject, queue_by_id: QueueById | None) -> None:
    active = state.get("active")
    if active is not None and not isinstance(active, dict):
        raise BulkGenerationError(
            "Generation state active attempt must be an object or null"
        )
    if isinstance(active, dict):
        _validate_active_attempt(active, queue_by_id)


def _validate_failure_record(
    failure: object, queue_id: str, *, result: StateObject | None = None
) -> None:
    if not isinstance(failure, dict) or failure.get("schema_version") != 1:
        raise BulkGenerationError(f"State item {queue_id!r} typed failure is invalid")
    if failure.get("kind") not in FAILURE_KINDS:
        raise BulkGenerationError(f"State item {queue_id!r} failure kind is invalid")
    _required_text(
        failure.get("error_type"), f"State item {queue_id!r} failure error_type"
    )
    features = failure.get("text_features")
    expected_features = {
        "character_count",
        "word_count",
        "comma_count",
        "ellipsis_count",
        "sentence_boundary_count",
    }
    if not isinstance(features, dict) or set(features) != expected_features:
        raise BulkGenerationError(
            f"State item {queue_id!r} failure text features are invalid"
        )
    for name, value in features.items():
        _nonnegative_int(value, f"State item {queue_id!r} failure {name}")
    completion = failure.get("completion")
    if completion is not None and completion not in {
        value.value for value in SynthesisCompletion
    }:
        raise BulkGenerationError(
            f"State item {queue_id!r} failure completion is invalid"
        )
    diagnosis = failure.get("pause_diagnosis")
    if diagnosis is not None:
        _validate_pause_diagnosis(diagnosis, features, queue_id, result=result)


def _validate_pause_diagnosis(
    diagnosis: object,
    features: StateObject,
    queue_id: str,
    *,
    result: StateObject | None,
) -> None:
    expected_fields = {
        "schema_version",
        "analysis_version",
        "classification",
        "threshold_seconds",
        "sentence_boundary_count",
        "repairable_by_safe_segmentation",
        "spans",
        "attempt_binding",
    }
    if (
        not isinstance(diagnosis, dict)
        or set(diagnosis) != expected_fields
        or diagnosis.get("schema_version") != PAUSE_DIAGNOSIS_VERSION
        or diagnosis.get("analysis_version")
        not in {
            LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION,
            SPEECH_QUALITY_ANALYSIS_VERSION,
        }
        or diagnosis.get("classification")
        not in {"sentence_boundary_pause_candidate", "speech_silence"}
        or diagnosis.get("threshold_seconds") != NOTABLE_SILENCE_SPAN_SECONDS
        or diagnosis.get("sentence_boundary_count")
        != features["sentence_boundary_count"]
        or not isinstance(diagnosis.get("repairable_by_safe_segmentation"), bool)
        or not isinstance(diagnosis.get("spans"), list)
    ):
        raise BulkGenerationError(f"State item {queue_id!r} pause diagnosis is invalid")
    binding = diagnosis.get("attempt_binding")
    binding_fields = {
        "provider",
        "model",
        "generation_profile",
        "seed",
        "synthesis_provenance_sha256",
    }
    if not isinstance(binding, dict) or set(binding) != binding_fields:
        raise BulkGenerationError(
            f"State item {queue_id!r} pause diagnosis binding is invalid"
        )
    for field in ("provider", "model", "generation_profile"):
        _required_text(
            binding.get(field), f"State item {queue_id!r} pause diagnosis {field}"
        )
    _integer(binding.get("seed"), f"State item {queue_id!r} pause diagnosis seed")
    _required_sha256(
        binding.get("synthesis_provenance_sha256"),
        f"State item {queue_id!r} pause diagnosis synthesis provenance",
    )
    if result is None or any(
        binding.get(field) != result.get(field) for field in binding_fields
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} pause diagnosis binding changed"
        )
    previous_end = 0.0
    for span in diagnosis["spans"]:
        if (
            not isinstance(span, dict)
            or set(span) != {"kind", "start_seconds", "end_seconds", "duration_seconds"}
            or span.get("kind") not in {"leading", "internal", "trailing", "all_silent"}
        ):
            raise BulkGenerationError(f"State item {queue_id!r} pause span is invalid")
        values = [
            span.get("start_seconds"),
            span.get("end_seconds"),
            span.get("duration_seconds"),
        ]
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not np.isfinite(value)
            or value < 0
            for value in values
        ):
            raise BulkGenerationError(
                f"State item {queue_id!r} pause span timing is invalid"
            )
        start_value, end_value, duration_value = values
        assert isinstance(start_value, (int, float))
        assert isinstance(end_value, (int, float))
        assert isinstance(duration_value, (int, float))
        start, end, duration = (
            float(start_value),
            float(end_value),
            float(duration_value),
        )
        if (
            start < previous_end
            or end <= start
            or duration < NOTABLE_SILENCE_SPAN_SECONDS
            or abs((end - start) - duration) > 0.002
        ):
            raise BulkGenerationError(
                f"State item {queue_id!r} pause span timing is inconsistent"
            )
        previous_end = end


def _validate_live_fallback_decision(
    result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None,
) -> None:
    decision = result.get("live_fallback")
    if decision is None:
        if result.get("status") == "live_fallback":
            raise BulkGenerationError(
                f"State item {queue_id!r} live fallback decision is missing"
            )
        return
    if not isinstance(decision, dict):
        raise BulkGenerationError(
            f"State item {queue_id!r} live fallback decision is malformed"
        )
    version = _validate_live_fallback_structure(decision, queue_id)
    _validate_live_fallback_evidence_version(
        decision, result, queue_id, queue_item, version
    )
    _validate_live_fallback_common(decision, queue_id)
    _validate_live_fallback_identity(decision, result, queue_id, queue_item, version)


def _validate_live_fallback_structure(decision: StateObject, queue_id: str) -> object:
    common_fields = {
        "schema",
        "schema_version",
        "reason",
        "provider",
        "model",
        "generation_profile",
        "queue_id",
        "line_id",
        "text_sha256",
        "speaker",
        "requested_voice_character",
        "previous_result_sha256",
        "decided_at",
    }
    version = decision.get("schema_version")
    expected_fields = (
        common_fields | {"evidence"}
        if version
        in {
            LIVE_FALLBACK_EVIDENCE_VERSION,
            LIVE_FALLBACK_REVIEW_EVIDENCE_VERSION,
            LIVE_FALLBACK_MISSING_VOICE_EVIDENCE_VERSION,
            LIVE_FALLBACK_KNOWN_ROLE_EVIDENCE_VERSION,
            LIVE_FALLBACK_AUDIO_EVENT_PROJECTION_VERSION,
            LIVE_FALLBACK_REVIEWED_REJECTION_VERSION,
            LIVE_FALLBACK_AUTOMATIC_RECOVERY_VERSION,
        }
        else common_fields
    )
    if (
        set(decision) != expected_fields
        or decision.get("schema") != LIVE_FALLBACK_SCHEMA
        or version
        not in {
            LIVE_FALLBACK_VERSION,
            LIVE_FALLBACK_EVIDENCE_VERSION,
            LIVE_FALLBACK_REVIEW_EVIDENCE_VERSION,
            LIVE_FALLBACK_MISSING_VOICE_EVIDENCE_VERSION,
            LIVE_FALLBACK_KNOWN_ROLE_EVIDENCE_VERSION,
            LIVE_FALLBACK_AUDIO_EVENT_PROJECTION_VERSION,
            LIVE_FALLBACK_REVIEWED_REJECTION_VERSION,
            LIVE_FALLBACK_AUTOMATIC_RECOVERY_VERSION,
        }
        or decision.get("reason") not in LIVE_FALLBACK_REASONS
        or decision.get("provider") != "pocket-tts"
        or decision.get("model") != "pocket-tts"
        or decision.get("generation_profile") != "default"
        or decision.get("queue_id") != queue_id
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} live fallback decision is malformed"
        )
    return version


LiveFallbackEvidenceValidator: TypeAlias = Callable[
    [StateObject, StateObject, str, VoiceGenerationQueueItem | None], None
]


def _validate_live_fallback_evidence_version(
    decision: StateObject,
    result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None,
    version: object,
) -> None:
    if version == LIVE_FALLBACK_VERSION:
        if decision.get("reason") in {
            LIVE_FALLBACK_HYPOTHESES_EXHAUSTED,
            LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED,
        }:
            raise BulkGenerationError(
                f"State item {queue_id!r} live fallback evidence is missing"
            )
        return
    validator = _live_fallback_evidence_validators().get(version)
    if validator is None:
        raise BulkGenerationError(
            f"State item {queue_id!r} live fallback decision is malformed"
        )
    validator(decision, result, queue_id, queue_item)


def _live_fallback_evidence_validators() -> dict[object, LiveFallbackEvidenceValidator]:
    return {
        LIVE_FALLBACK_EVIDENCE_VERSION: _validate_hypotheses_fallback_evidence,
        LIVE_FALLBACK_REVIEW_EVIDENCE_VERSION: _validate_hypotheses_fallback_evidence,
        LIVE_FALLBACK_MISSING_VOICE_EVIDENCE_VERSION: _validate_missing_voice_fallback_evidence,
        LIVE_FALLBACK_KNOWN_ROLE_EVIDENCE_VERSION: _validate_known_role_fallback_evidence,
        LIVE_FALLBACK_AUDIO_EVENT_PROJECTION_VERSION: _validate_audio_event_fallback_evidence,
        LIVE_FALLBACK_REVIEWED_REJECTION_VERSION: _validate_reviewed_rejection_fallback_evidence,
        LIVE_FALLBACK_AUTOMATIC_RECOVERY_VERSION: _validate_automatic_recovery_fallback_evidence,
    }


def _require_live_fallback_reason(
    decision: StateObject, queue_id: str, expected: str
) -> None:
    if decision.get("reason") != expected:
        raise BulkGenerationError(
            f"State item {queue_id!r} live fallback reason is invalid"
        )


def _validate_hypotheses_fallback_evidence(
    decision: StateObject,
    _result: StateObject,
    queue_id: str,
    _queue_item: VoiceGenerationQueueItem | None,
) -> None:
    _require_live_fallback_reason(
        decision, queue_id, LIVE_FALLBACK_HYPOTHESES_EXHAUSTED
    )
    _validate_live_fallback_evidence(
        decision.get("evidence"), decision.get("previous_result_sha256")
    )


def _validate_missing_voice_fallback_evidence(
    decision: StateObject,
    _result: StateObject,
    queue_id: str,
    _queue_item: VoiceGenerationQueueItem | None,
) -> None:
    _require_live_fallback_reason(
        decision, queue_id, "reference_unavailable_after_audit"
    )
    _validate_missing_voice_live_fallback_evidence(
        decision.get("evidence"), queue_id, decision.get("requested_voice_character")
    )


def _validate_known_role_fallback_evidence(
    decision: StateObject,
    result: StateObject,
    queue_id: str,
    _queue_item: VoiceGenerationQueueItem | None,
) -> None:
    _require_live_fallback_reason(
        decision, queue_id, LIVE_FALLBACK_HYPOTHESES_EXHAUSTED
    )
    _validate_known_role_live_fallback_evidence(
        decision.get("evidence"),
        queue_id,
        result.get("requested_voice_character"),
        decision.get("requested_voice_character"),
        result.get("voice_character"),
    )


def _validate_audio_event_fallback_evidence(
    decision: StateObject,
    _result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None,
) -> None:
    _require_live_fallback_reason(decision, queue_id, "generated_audio_rejected")
    _validate_audio_event_projection_live_fallback_evidence(
        decision.get("evidence"),
        queue_id,
        decision.get("previous_result_sha256"),
        decision.get("requested_voice_character"),
        queue_item,
    )


def _validate_reviewed_rejection_fallback_evidence(
    decision: StateObject,
    _result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None,
) -> None:
    _require_live_fallback_reason(decision, queue_id, "generated_audio_rejected")
    _validate_reviewed_rejection_live_fallback_evidence(
        decision.get("evidence"),
        queue_id,
        decision.get("previous_result_sha256"),
        decision.get("requested_voice_character"),
        queue_item,
    )


def _validate_automatic_recovery_fallback_evidence(
    decision: StateObject,
    _result: StateObject,
    queue_id: str,
    _queue_item: VoiceGenerationQueueItem | None,
) -> None:
    _require_live_fallback_reason(
        decision, queue_id, LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED
    )
    _validate_automatic_recovery_live_fallback_evidence(
        decision.get("evidence"), queue_id, decision.get("previous_result_sha256")
    )


def _validate_live_fallback_common(decision: StateObject, queue_id: str) -> None:
    for field in ("model", "generation_profile", "decided_at"):
        _required_text(
            decision.get(field), f"State item {queue_id!r} live fallback {field}"
        )
    previous = decision.get("previous_result_sha256")
    if previous is not None:
        _required_sha256(previous, f"State item {queue_id!r} previous result SHA-256")


def _validate_live_fallback_identity(
    decision: StateObject,
    result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None,
    version: object,
) -> None:
    if queue_item is not None:
        expected = {
            "line_id": queue_item.line_id,
            "text_sha256": queue_item.text_sha256,
            "speaker": queue_item.speaker,
        }
        if version not in {
            LIVE_FALLBACK_KNOWN_ROLE_EVIDENCE_VERSION,
            LIVE_FALLBACK_AUDIO_EVENT_PROJECTION_VERSION,
            LIVE_FALLBACK_REVIEWED_REJECTION_VERSION,
        }:
            expected["requested_voice_character"] = synthesis_character_for_line(
                queue_item.speaker, queue_item.voice_character
            )
        if any(decision.get(field) != value for field, value in expected.items()):
            raise BulkGenerationError(
                f"State item {queue_id!r} live fallback identity changed"
            )
    status_pair = result.get("status"), result.get("review_status")
    if status_pair not in {
        ("generated", "rejected"),
        ("live_fallback", "live_fallback"),
    }:
        raise BulkGenerationError(
            f"State item {queue_id!r} live fallback has an invalid terminal state"
        )


def _validate_audio_event_omission(
    result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None,
) -> None:
    result_fields = {
        "status",
        "review_status",
        "attempts",
        "line_id",
        "text_sha256",
        "speaker",
        "audio_event_omission",
        "updated_at",
    }
    decision = result.get("audio_event_omission")
    fields = {
        "schema",
        "schema_version",
        "reason",
        "queue_id",
        "line_id",
        "text_sha256",
        "speaker",
        "plan_sha256",
        "spoken_text_sha256",
        "decided_at",
        "authority",
    }
    authority_fields = {
        "batch_id",
        "base_workspace_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
    }
    authority = decision.get("authority") if isinstance(decision, dict) else None
    if (
        set(result) != result_fields
        or result.get("status") != "omitted"
        or result.get("review_status") != "omitted"
        or result.get("attempts") != 0
        or not isinstance(decision, dict)
        or set(decision) != fields
        or decision.get("schema") != AUDIO_EVENT_OMISSION_SCHEMA
        or decision.get("schema_version") != AUDIO_EVENT_OMISSION_VERSION
        or decision.get("reason") != AUDIO_EVENT_OMISSION_REASON
        or decision.get("queue_id") != queue_id
        or not isinstance(authority, dict)
        or set(authority) != authority_fields
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} audio-event omission is malformed"
        )
    for field in ("plan_sha256", "spoken_text_sha256"):
        _required_sha256(
            decision.get(field), f"State item {queue_id!r} omission {field}"
        )
    for field in (
        "batch_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
    ):
        _required_sha256(
            authority.get(field), f"State item {queue_id!r} omission {field}"
        )
    for field in ("line_id", "text_sha256", "speaker", "decided_at"):
        _required_text(decision.get(field), f"State item {queue_id!r} omission {field}")
    _required_text(result.get("updated_at"), f"State item {queue_id!r} updated_at")
    _required_text(
        authority.get("base_workspace_id"),
        f"State item {queue_id!r} omission base workspace",
    )
    if queue_item is None:
        return
    try:
        plan = audio_event_plan_for_record(queue_item)
    except ValueError as error:
        raise BulkGenerationError(str(error)) from error
    if (
        not isinstance(plan, dict)
        or not plan.get("requires_composition")
        or plan.get("spoken_text") != ""
        or decision.get("line_id") != queue_item.line_id
        or decision.get("text_sha256") != queue_item.text_sha256
        or decision.get("speaker") != queue_item.speaker
        or decision.get("plan_sha256") != plan.get("plan_sha256")
        or decision.get("spoken_text_sha256") != plan.get("spoken_text_sha256")
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} omission is not bound to a pure audio event"
        )


def _validate_missing_voice_live_fallback_evidence(
    evidence: object, queue_id: str, requested_voice_character: object
) -> None:
    fields = {
        "schema",
        "schema_version",
        "authority_bundle_id",
        "authority_bundle_sha256",
        "authority_decision_id",
        "authority_decision_sha256",
        "plan_id",
        "source_workspace_id",
        "source_workspace_sha256",
        "cohort_id",
        "queue_id",
        "decision_origin",
        "requested_voice_character",
        "configured_narrator_character",
        "batch_id",
    }
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema") != MISSING_VOICE_LIVE_FALLBACK_EVIDENCE_SCHEMA
        or evidence.get("schema_version") != 1
        or evidence.get("queue_id") != queue_id
        or evidence.get("requested_voice_character") != requested_voice_character
        or evidence.get("decision_origin") != "automatic_no_complete_candidate"
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} missing-voice fallback evidence is malformed"
        )
    for field in (
        "authority_bundle_id",
        "authority_bundle_sha256",
        "authority_decision_id",
        "authority_decision_sha256",
        "plan_id",
        "source_workspace_sha256",
        "cohort_id",
        "batch_id",
    ):
        _required_sha256(
            evidence.get(field),
            f"State item {queue_id!r} missing-voice fallback {field}",
        )
    for field in (
        "source_workspace_id",
        "queue_id",
        "requested_voice_character",
        "configured_narrator_character",
    ):
        _required_text(
            evidence.get(field),
            f"State item {queue_id!r} missing-voice fallback {field}",
        )


def _validate_known_role_live_fallback_evidence(
    evidence: object,
    queue_id: str,
    source_character: object,
    requested_synthesis_character: object,
    effective_synthesis_character: object,
) -> None:
    fields = {
        "schema",
        "schema_version",
        "batch_id",
        "queue_id",
        "voice_manifest_sha256",
        "route_binding_sha256",
        "queue_voice_overrides_sha256",
        "source_character",
        "synthesis_character",
        "evidence_workspace_id",
        "evidence_workspace_sha256",
        "evidence_state_sha256",
        "evidence_item_sha256",
        "evidence_item",
    }
    item = evidence.get("evidence_item") if isinstance(evidence, dict) else None
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema") != KNOWN_ROLE_LIVE_FALLBACK_EVIDENCE_SCHEMA
        or evidence.get("schema_version") != 1
        or evidence.get("queue_id") != queue_id
        or evidence.get("source_character") != source_character
        or evidence.get("synthesis_character") != requested_synthesis_character
        or evidence.get("synthesis_character") != effective_synthesis_character
        or not isinstance(item, dict)
        or item.get("status") != "failed"
        or canonical_document_sha256(item) != evidence.get("evidence_item_sha256")
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} known-role fallback evidence is malformed"
        )
    for field in (
        "batch_id",
        "voice_manifest_sha256",
        "route_binding_sha256",
        "queue_voice_overrides_sha256",
        "evidence_workspace_sha256",
        "evidence_state_sha256",
        "evidence_item_sha256",
    ):
        _required_sha256(
            evidence.get(field),
            f"State item {queue_id!r} known-role fallback {field}",
        )
    for field in (
        "source_character",
        "synthesis_character",
        "evidence_workspace_id",
    ):
        _required_text(
            evidence.get(field),
            f"State item {queue_id!r} known-role fallback {field}",
        )


def _validate_audio_event_projection_live_fallback_evidence(
    evidence: object,
    queue_id: str,
    previous_result_sha256: object,
    requested_voice_character: object,
    queue_item: VoiceGenerationQueueItem | None,
) -> None:
    fields = {
        "schema",
        "schema_version",
        "batch_id",
        "base_workspace_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "queue_id",
        "base_result_sha256",
        "base_result",
        "plan_sha256",
        "spoken_text",
        "spoken_text_sha256",
        "source_character",
        "synthesis_character",
    }
    base_result = evidence.get("base_result") if isinstance(evidence, dict) else None
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema")
        != AUDIO_EVENT_PROJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA
        or evidence.get("schema_version") != 1
        or evidence.get("queue_id") != queue_id
        or evidence.get("base_result_sha256") != previous_result_sha256
        or evidence.get("synthesis_character") != requested_voice_character
        or requested_voice_character != "Narrator"
        or not isinstance(base_result, dict)
        or base_result.get("status") != "generated"
        or base_result.get("review_status") != "rejected"
        or isinstance(base_result.get("live_fallback"), dict)
        or canonical_document_sha256(base_result) != evidence.get("base_result_sha256")
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} audio-event projection evidence is malformed"
        )
    for field in (
        "batch_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "base_result_sha256",
        "plan_sha256",
        "spoken_text_sha256",
    ):
        _required_sha256(
            evidence.get(field),
            f"State item {queue_id!r} audio-event projection {field}",
        )
    for field in (
        "base_workspace_id",
        "spoken_text",
        "source_character",
        "synthesis_character",
    ):
        _required_text(
            evidence.get(field),
            f"State item {queue_id!r} audio-event projection {field}",
        )
    spoken_text = evidence["spoken_text"]
    if (
        hashlib.sha256(spoken_text.encode("utf-8")).hexdigest()
        != evidence["spoken_text_sha256"]
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} audio-event spoken projection changed"
        )
    if queue_item is None:
        return
    try:
        plan = audio_event_plan_for_record(queue_item)
    except ValueError as error:
        raise BulkGenerationError(str(error)) from error
    if (
        not isinstance(plan, dict)
        or not plan.get("requires_composition")
        or not plan.get("events")
        or not plan.get("spoken_text")
        or plan.get("spoken_text") != spoken_text
        or plan.get("plan_sha256") != evidence["plan_sha256"]
        or plan.get("spoken_text_sha256") != evidence["spoken_text_sha256"]
        or queue_item.speaker != evidence["source_character"]
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} fallback is not bound to a mixed audio event"
        )


def _validate_reviewed_rejection_live_fallback_evidence(
    evidence: object,
    queue_id: str,
    previous_result_sha256: object,
    requested_voice_character: object,
    queue_item: VoiceGenerationQueueItem | None,
) -> None:
    fields = {
        "schema",
        "schema_version",
        "batch_id",
        "base_workspace_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "voice_manifest_sha256",
        "queue_id",
        "base_result_sha256",
        "base_result",
        "source_character",
        "synthesis_character",
        "route_source",
        "route_reference_sha256s",
    }
    base_result = evidence.get("base_result") if isinstance(evidence, dict) else None
    references = (
        evidence.get("route_reference_sha256s") if isinstance(evidence, dict) else None
    )
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema") != REVIEWED_REJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA
        or evidence.get("schema_version") != 1
        or evidence.get("queue_id") != queue_id
        or evidence.get("base_result_sha256") != previous_result_sha256
        or evidence.get("synthesis_character") != requested_voice_character
        or not isinstance(base_result, dict)
        or base_result.get("status") != "generated"
        or base_result.get("review_status") != "rejected"
        or isinstance(base_result.get("live_fallback"), dict)
        or canonical_document_sha256(base_result) != evidence.get("base_result_sha256")
        or evidence.get("route_source") not in {"config_rebase", "voice_manifest"}
        or not isinstance(references, list)
        or not references
        or references != sorted(set(references))
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} reviewed-rejection evidence is malformed"
        )
    for field in (
        "batch_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "voice_manifest_sha256",
        "base_result_sha256",
    ):
        _required_sha256(
            evidence.get(field),
            f"State item {queue_id!r} reviewed-rejection {field}",
        )
    for field in (
        "base_workspace_id",
        "source_character",
        "synthesis_character",
    ):
        _required_text(
            evidence.get(field),
            f"State item {queue_id!r} reviewed-rejection {field}",
        )
    for digest in references:
        _required_sha256(
            digest, f"State item {queue_id!r} reviewed-rejection reference"
        )
    if evidence["route_source"] == "config_rebase":
        rebase = base_result.get("config_rebase")
        if (
            not isinstance(rebase, dict)
            or rebase.get("target_route_status") != "active"
            or rebase.get("target_effective_character")
            != evidence["synthesis_character"]
            or sorted(set(rebase.get("target_reference_sha256s", []))) != references
        ):
            raise BulkGenerationError(
                f"State item {queue_id!r} reviewed-rejection route changed"
            )
    elif base_result.get("voice_character") != evidence["synthesis_character"]:
        raise BulkGenerationError(
            f"State item {queue_id!r} reviewed-rejection recorded voice changed"
        )
    if queue_item is not None and (
        queue_item.speaker != evidence["source_character"]
        or queue_item.line_id != base_result.get("line_id")
        or queue_item.text_sha256 != base_result.get("text_sha256")
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} reviewed-rejection queue identity changed"
        )


def _validate_automatic_recovery_live_fallback_evidence(
    evidence: object, queue_id: str, previous_result_sha256: object
) -> None:
    fields = {
        "schema",
        "schema_version",
        "queue_sha256",
        "queue_id",
        "base_result_sha256",
        "base_result",
        "recovery_action",
        "failure_kind",
    }
    base_result = evidence.get("base_result") if isinstance(evidence, dict) else None
    failure = base_result.get("failure") if isinstance(base_result, dict) else None
    allowed_actions = {
        "bounded_seed_retry",
        "offline_fallback_backend",
        "inline_pause_marker_comparison",
        "reference_comparison",
        "reference_discovery",
        "backend_diagnosis",
        "provenance_recovery_or_regeneration",
    }
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema") != AUTOMATIC_RECOVERY_LIVE_FALLBACK_EVIDENCE_SCHEMA
        or evidence.get("schema_version") != 1
        or evidence.get("queue_id") != queue_id
        or evidence.get("base_result_sha256") != previous_result_sha256
        or not isinstance(base_result, dict)
        or base_result.get("status") != "failed"
        or not _automatic_recovery_source_backend(base_result, failure)
        or isinstance(base_result.get("live_fallback"), dict)
        or canonical_document_sha256(base_result) != evidence.get("base_result_sha256")
        or evidence.get("recovery_action") not in allowed_actions
        or not isinstance(failure, dict)
        or failure.get("kind") != evidence.get("failure_kind")
        or failure.get("kind") in {"cancelled", "interrupted"}
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} automatic-recovery evidence is malformed"
        )
    _required_sha256(
        evidence.get("queue_sha256"),
        f"State item {queue_id!r} automatic-recovery queue",
    )
    _required_sha256(
        evidence.get("base_result_sha256"),
        f"State item {queue_id!r} automatic-recovery base result",
    )
    _validate_failure_record(failure, queue_id, result=base_result)
    _validate_synthesis_identity(base_result, queue_id)
    _validate_seed_application(base_result, queue_id)


def _automatic_recovery_source_backend(result: StateObject, failure: object) -> bool:
    provider = result.get("provider")
    return bool(
        provider == "pocket-tts"
        and result.get("model") == "pocket-tts"
        and result.get("generation_profile") == "default"
        or provider == "moss-tts"
        and isinstance(failure, dict)
        and failure.get("kind") in {"missed_eos_audio_limit", "speech_silence"}
    )


def _validate_synthesis_identity(
    result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None = None,
) -> None:
    expected_requested = _validate_synthesis_speaker(result, queue_id, queue_item)
    requested = result.get("requested_voice_character")
    effective = result.get("voice_character")
    fallback = result.get("synthesis_fallback")
    source_binding = result.get("source_reference_binding")
    configuration = _validate_synthesis_configuration(result, queue_id)
    _validate_audio_event_spoken_projection(result, queue_id, queue_item, configuration)
    if requested is not None:
        requested = _required_text(
            requested, f"State item {queue_id!r} requested_voice_character"
        )
        if expected_requested is not None and requested != expected_requested:
            raise BulkGenerationError(
                f"State item {queue_id!r} requested voice does not match the queue"
            )
    if effective is not None:
        effective = _required_text(
            effective, f"State item {queue_id!r} voice_character"
        )
    changed = (
        requested is not None
        and effective is not None
        and normalize_character_name(requested) != normalize_character_name(effective)
    )
    if fallback is None:
        _validate_source_reference_binding(
            source_binding, configuration, requested, effective, changed, queue_id
        )
        if "narrator_character" in result:
            raise BulkGenerationError(
                f"State item {queue_id!r} has unbound narrator provenance"
            )
        return
    _validate_narrator_fallback(
        fallback, source_binding, configuration, requested, effective, result, queue_id
    )


def _validate_synthesis_speaker(
    result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None,
) -> str | None:
    if queue_item is None:
        return None
    if (
        "speaker" in result
        and _required_text(result.get("speaker"), f"State item {queue_id!r} speaker")
        != queue_item.speaker
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} speaker does not match the queue"
        )
    return _required_text(
        synthesis_character_for_line(queue_item.speaker, queue_item.voice_character),
        f"State item {queue_id!r} requested voice",
    )


def _validate_source_reference_binding(
    source_binding: object,
    configuration: StateObject | None,
    requested: object,
    effective: object,
    changed: bool,
    queue_id: str,
) -> None:
    if not changed:
        if source_binding is not None:
            raise BulkGenerationError(
                f"State item {queue_id!r} has an unnecessary source-reference binding"
            )
        return
    if configuration is None or not isinstance(source_binding, dict):
        raise BulkGenerationError(
            f"State item {queue_id!r} changed synthesis voice without provenance"
        )
    expected = {
        "schema_version": 1,
        "queue_id": queue_id,
        "source_voice_character": requested,
        "synthesis_voice_character": effective,
        "queue_voice_overrides_sha256": configuration.get(
            "queue_voice_overrides_sha256"
        ),
    }
    if set(source_binding) != set(expected) or any(
        source_binding.get(field) != value for field, value in expected.items()
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} source-reference binding conflicts"
        )


def _validate_narrator_fallback(
    fallback: object,
    source_binding: object,
    configuration: StateObject | None,
    requested: object,
    effective: object,
    result: StateObject,
    queue_id: str,
) -> None:
    if configuration is None:
        raise BulkGenerationError(
            f"State item {queue_id!r} fallback lacks its synthesis configuration"
        )
    if source_binding is not None:
        raise BulkGenerationError(
            f"State item {queue_id!r} mixes narrator and source-reference overrides"
        )
    if not isinstance(fallback, dict) or set(fallback) != {
        "schema_version",
        "kind",
        "policy",
        "source_voice_character",
        "synthesis_voice_character",
        "narrator_character",
    }:
        raise BulkGenerationError(
            f"State item {queue_id!r} synthesis fallback is malformed"
        )
    if (
        fallback.get("schema_version") != 1
        or fallback.get("kind") != "missing_voice_to_narrator"
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} synthesis fallback is unsupported"
        )
    try:
        policy = MissingVoicePolicy.from_document(fallback.get("policy"))
    except MissingVoicePolicyError as error:
        raise BulkGenerationError(str(error)) from error
    source = _required_text(
        fallback.get("source_voice_character"),
        f"State item {queue_id!r} fallback source voice",
    )
    synthesis = _required_text(
        fallback.get("synthesis_voice_character"),
        f"State item {queue_id!r} fallback synthesis voice",
    )
    narrator = _required_text(
        fallback.get("narrator_character"),
        f"State item {queue_id!r} fallback narrator character",
    )
    overrides = configuration.get("synthesis_character_overrides")
    assert isinstance(overrides, dict)
    if (
        requested != source
        or effective != synthesis
        or normalize_character_name(synthesis) != "narrator"
        or not policy.applies_to(source)
        or result.get("narrator_character") != narrator
        or configuration["missing_voice_policy"] != policy.to_document()
        or overrides.get(normalize_character_name(source)) != "Narrator"
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} synthesis fallback provenance conflicts"
        )


def _validate_synthesis_configuration(
    result: StateObject, queue_id: str
) -> StateObject | None:
    configuration = result.get("synthesis_configuration")
    if configuration is None:
        return None
    legacy_fields = {
        "missing_voice_policy",
        "synthesis_character_overrides",
    }
    current_fields = legacy_fields | {"failure_repair_policy"}
    projection_fields = current_fields | {"audio_event_spoken_projection_queue_ids"}
    binding_fields = current_fields | {"queue_voice_overrides_sha256"}
    projection_binding_fields = projection_fields | {"queue_voice_overrides_sha256"}
    if not isinstance(configuration, dict) or frozenset(configuration) not in {
        frozenset(legacy_fields),
        frozenset(current_fields),
        frozenset(projection_fields),
        frozenset(binding_fields),
        frozenset(projection_binding_fields),
    }:
        raise BulkGenerationError(
            f"State item {queue_id!r} synthesis configuration is malformed"
        )
    policy = _load_missing_voice_policy(configuration, queue_id)
    repair_policy = _load_failure_repair_policy(configuration, queue_id)
    canonical = _validate_synthesis_overrides(configuration, policy, queue_id)
    validated = {
        "missing_voice_policy": policy.to_document(),
        "synthesis_character_overrides": canonical,
        "failure_repair_policy": repair_policy.to_document(),
    }
    _add_synthesis_configuration_optionals(validated, configuration, queue_id)
    return validated


def _load_missing_voice_policy(
    configuration: StateObject, queue_id: str
) -> MissingVoicePolicy:
    try:
        return MissingVoicePolicy.from_document(
            configuration.get("missing_voice_policy")
        )
    except MissingVoicePolicyError as error:
        raise BulkGenerationError(str(error)) from error


def _load_failure_repair_policy(
    configuration: StateObject, queue_id: str
) -> FailureRepairPolicy:
    try:
        return FailureRepairPolicy.from_document(
            configuration.get("failure_repair_policy")
        )
    except FailureRepairPolicyError as error:
        raise BulkGenerationError(str(error)) from error


def _validate_synthesis_overrides(
    configuration: StateObject, policy: MissingVoicePolicy, queue_id: str
) -> StateObject:
    overrides = configuration.get("synthesis_character_overrides")
    if not isinstance(overrides, dict):
        raise BulkGenerationError(
            f"State item {queue_id!r} synthesis overrides are malformed"
        )
    canonical: StateObject = {}
    for source, effective in overrides.items():
        source = _required_text(
            source, f"State item {queue_id!r} synthesis override source"
        )
        key = _required_text(
            normalize_character_name(source),
            f"State item {queue_id!r} synthesis override source",
        )
        if key != source or key == "narrator":
            raise BulkGenerationError(
                f"State item {queue_id!r} synthesis override source is not canonical"
            )
        if effective != "Narrator" or not policy.applies_to(source):
            raise BulkGenerationError(
                f"State item {queue_id!r} synthesis override is unauthorized"
            )
        canonical[key] = "Narrator"
    if canonical != overrides:
        raise BulkGenerationError(
            f"State item {queue_id!r} synthesis overrides are inconsistent"
        )
    return canonical


def _add_synthesis_configuration_optionals(
    result: StateObject, configuration: StateObject, queue_id: str
) -> None:
    if "audio_event_spoken_projection_queue_ids" in configuration:
        projection_ids = configuration["audio_event_spoken_projection_queue_ids"]
        if (
            not isinstance(projection_ids, list)
            or any(
                not isinstance(value, str) or not value.strip()
                for value in projection_ids
            )
            or projection_ids != sorted(set(projection_ids))
            or not projection_ids
        ):
            raise BulkGenerationError(
                f"State item {queue_id!r} audio-event projections are malformed"
            )
        result["audio_event_spoken_projection_queue_ids"] = list(projection_ids)
    if "queue_voice_overrides_sha256" in configuration:
        result["queue_voice_overrides_sha256"] = _required_sha256(
            configuration.get("queue_voice_overrides_sha256"),
            f"State item {queue_id!r} queue voice override SHA-256",
        )
    return None


def _validate_audio_event_spoken_projection(
    result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None,
    configuration: StateObject | None,
) -> None:
    transform = result.get("text_transform")
    projection_ids = (
        configuration.get("audio_event_spoken_projection_queue_ids", ())
        if isinstance(configuration, dict)
        else ()
    )
    assert isinstance(projection_ids, (list, tuple))
    if queue_id not in projection_ids:
        if transform == "audio-event-spoken-projection-v1":
            raise BulkGenerationError(
                f"State item {queue_id!r} has an unauthorized audio-event projection"
            )
        return
    if queue_item is None:
        return
    try:
        plan = audio_event_plan_for_record(queue_item)
    except ValueError as error:
        raise BulkGenerationError(str(error)) from error
    if (
        transform != "audio-event-spoken-projection-v1"
        or not isinstance(plan, dict)
        or not plan.get("requires_composition")
        or not plan.get("events")
        or not plan.get("spoken_text")
        or result.get("synthesis_text_sha256") != plan.get("spoken_text_sha256")
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} audio-event spoken projection changed"
        )


def _validate_failure_repair_record(
    result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None,
) -> None:
    repair = result.get("failure_repair")
    if repair is None:
        return
    if not isinstance(repair, dict) or repair.get("schema_version") != 1:
        raise BulkGenerationError(
            f"State item {queue_id!r} failure repair is malformed"
        )
    configuration = _validate_synthesis_configuration(result, queue_id)
    if configuration is None:
        raise BulkGenerationError(
            f"State item {queue_id!r} failure repair lacks synthesis configuration"
        )
    try:
        policy = FailureRepairPolicy.from_document(
            configuration["failure_repair_policy"]
        )
    except FailureRepairPolicyError as error:
        raise BulkGenerationError(str(error)) from error
    strategy = policy.strategy_for(queue_id)
    if strategy is None or repair.get("strategy") != strategy:
        raise BulkGenerationError(
            f"State item {queue_id!r} failure repair is unauthorized"
        )
    _failure_repair_validators().get(strategy, _validate_bounded_seed_repair)(
        repair, result, queue_id, queue_item, policy
    )


FailureRepairValidator: TypeAlias = Callable[
    [
        StateObject,
        StateObject,
        str,
        VoiceGenerationQueueItem | None,
        FailureRepairPolicy,
    ],
    None,
]


def _failure_repair_validators() -> dict[str, FailureRepairValidator]:
    return {
        SENTENCE_BOUNDARY_SEGMENTATION: _validate_sentence_boundary_repair,
        INLINE_PAUSE_MARKER: _validate_inline_pause_repair,
        EDGE_SILENCE_TRIM: _validate_edge_silence_repair,
        OFFLINE_FALLBACK_BACKEND: _validate_offline_fallback_repair,
    }


def _validate_sentence_boundary_repair(
    repair: StateObject,
    result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None,
    policy: FailureRepairPolicy,
) -> None:
    if set(repair) != {
        "schema_version",
        "strategy",
        "segments",
        "segment_text_sha256",
        "planned_segment_seeds",
        "pause_ms",
    }:
        raise BulkGenerationError(
            f"State item {queue_id!r} sentence repair is malformed"
        )
    if queue_item is not None:
        expected = safe_sentence_segments(queue_item.text)
        if repair.get("segments") != list(expected):
            raise BulkGenerationError(
                f"State item {queue_id!r} sentence repair text changed"
            )
        expected_hashes = [
            hashlib.sha256(value.encode("utf-8")).hexdigest() for value in expected
        ]
        if repair.get("segment_text_sha256") != expected_hashes:
            raise BulkGenerationError(
                f"State item {queue_id!r} sentence repair hashes changed"
            )
    if repair.get("pause_ms") != policy.segment_pause_ms:
        raise BulkGenerationError(
            f"State item {queue_id!r} sentence repair pause conflicts"
        )
    seeds = repair.get("planned_segment_seeds")
    segments = repair.get("segments")
    if (
        not isinstance(seeds, list)
        or not isinstance(segments, list)
        or len(seeds) != len(segments)
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} sentence repair seeds are malformed"
        )
    for value in seeds:
        _integer(value, f"State item {queue_id!r} sentence repair seed")
    outer_seed = result.get("seed")
    if outer_seed is not None:
        assert isinstance(outer_seed, int)
    if outer_seed is not None and seeds != [
        outer_seed + index for index in range(len(seeds))
    ]:
        raise BulkGenerationError(
            f"State item {queue_id!r} sentence repair seeds conflict"
        )


def _validate_inline_pause_repair(
    repair: StateObject,
    result: StateObject,
    queue_id: str,
    queue_item: VoiceGenerationQueueItem | None,
    policy: FailureRepairPolicy,
) -> None:
    if set(repair) != {
        "schema_version",
        "strategy",
        "source_text_sha256",
        "derived_prompt_sha256",
        "pause_ms",
        "marker_count",
    }:
        raise BulkGenerationError(
            f"State item {queue_id!r} inline pause repair is malformed"
        )
    if repair.get("pause_ms") != policy.inline_pause_ms:
        raise BulkGenerationError(f"State item {queue_id!r} inline pause conflicts")
    marker_count = _nonnegative_int(
        repair.get("marker_count"), f"State item {queue_id!r} inline pause marker count"
    )
    if marker_count < 1:
        raise BulkGenerationError(
            f"State item {queue_id!r} inline pause marker count is invalid"
        )
    if queue_item is None:
        return
    prompt, marker_count = inline_sentence_pause_prompt(
        queue_item.text, pause_ms=policy.inline_pause_ms
    )
    expected_source = hashlib.sha256(queue_item.text.encode("utf-8")).hexdigest()
    expected_prompt = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    if (
        repair.get("source_text_sha256") != expected_source
        or repair.get("derived_prompt_sha256") != expected_prompt
        or repair.get("marker_count") != marker_count
        or result.get("synthesis_text_sha256") != expected_prompt
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} inline pause prompt changed"
        )


def _validate_edge_silence_repair(
    repair: StateObject,
    _result: StateObject,
    queue_id: str,
    _queue_item: VoiceGenerationQueueItem | None,
    _policy: FailureRepairPolicy,
) -> None:
    allowed = {
        "schema_version",
        "strategy",
        "leading_trimmed_samples",
        "trailing_trimmed_samples",
    }
    if not set(repair).issubset(allowed):
        raise BulkGenerationError(f"State item {queue_id!r} edge repair is malformed")
    for field in ("leading_trimmed_samples", "trailing_trimmed_samples"):
        if field in repair:
            _nonnegative_int(repair[field], f"State item {queue_id!r} repair {field}")


def _validate_bounded_seed_repair(
    repair: StateObject,
    _result: StateObject,
    queue_id: str,
    _queue_item: VoiceGenerationQueueItem | None,
    _policy: FailureRepairPolicy,
) -> None:
    if set(repair) != {"schema_version", "strategy"}:
        raise BulkGenerationError(
            f"State item {queue_id!r} bounded seed repair is malformed"
        )


def _validate_offline_fallback_repair(
    repair: StateObject,
    result: StateObject,
    queue_id: str,
    _queue_item: VoiceGenerationQueueItem | None,
    _policy: FailureRepairPolicy,
) -> None:
    if set(repair) != {"schema_version", "strategy", "source_failure"}:
        raise BulkGenerationError(
            f"State item {queue_id!r} offline fallback repair is malformed"
        )
    source = repair.get("source_failure")
    if not isinstance(source, dict):
        raise BulkGenerationError(
            f"State item {queue_id!r} offline fallback source is malformed"
        )
    _validate_offline_fallback_source_fields(source, queue_id)
    _validate_offline_fallback_authority(source, queue_id)
    _validate_offline_fallback_source_consistency(source, result, queue_id)
    _validate_offline_fallback_source_values(source, queue_id)


def _validate_offline_fallback_source_fields(
    source: StateObject, queue_id: str
) -> None:
    required = {
        "mode",
        "source_workspace_id",
        "source_state_sha256",
        "source_item_sha256",
        "character",
        "source_provider",
        "source_model",
        "source_generation_profile",
        "source_attempts",
        "source_seed",
        "source_failure_kind",
        "source_voice_reference",
    }
    optional = {
        "source_parent_carry_forward",
        "source_provider_attempts",
        "source_repair_strategy",
        "source_unresolved_authority",
    }
    if not required.issubset(source) or not set(source).issubset(required | optional):
        raise BulkGenerationError(
            f"State item {queue_id!r} offline fallback source is malformed"
        )


def _validate_offline_fallback_authority(source: StateObject, queue_id: str) -> None:
    authority = source.get("source_unresolved_authority")
    if authority is None:
        return
    expected = {
        "schema": "vntts.authoring-offline-fallback-authority-reference",
        "schema_version": 1,
        "kind": authority.get("kind") if isinstance(authority, dict) else None,
        "authority_id": authority.get("authority_id")
        if isinstance(authority, dict)
        else None,
        "source_sha256": authority.get("source_sha256")
        if isinstance(authority, dict)
        else None,
        "queue_id": queue_id,
        "source_item_sha256": source.get("source_item_sha256"),
    }
    if (
        not isinstance(authority, dict)
        or set(authority) != set(expected)
        or authority.get("kind") not in {"failed_voice_review", "failed_prompt_review"}
        or any(authority.get(field) != value for field, value in expected.items())
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} offline fallback authority is malformed"
        )
    _required_sha256(
        authority.get("authority_id"), f"State item {queue_id!r} fallback authority ID"
    )
    _required_sha256(
        authority.get("source_sha256"),
        f"State item {queue_id!r} fallback authority SHA-256",
    )


def _validate_offline_fallback_source_consistency(
    source: StateObject, result: StateObject, queue_id: str
) -> None:
    attempts = source.get("source_provider_attempts")
    exhausted = (
        source.get("source_failure_kind") == "speech_silence"
        and source.get("source_repair_strategy")
        in {None, BOUNDED_SEED_RETRY, INLINE_PAUSE_MARKER}
        and isinstance(attempts, int)
        and not isinstance(attempts, bool)
        and (
            source.get("source_unresolved_authority") is not None
            or attempts >= MAX_BOUNDED_TOTAL_ATTEMPTS
        )
    )
    if (
        source.get("mode") != "failed-outcome"
        or source.get("source_provider") == result.get("provider")
        or source.get("source_failure_kind")
        not in {"missed_eos_audio_limit", "speech_silence"}
        or (source.get("source_failure_kind") == "speech_silence" and not exhausted)
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} offline fallback source is inconsistent"
        )
    if attempts is not None and (
        _nonnegative_int(attempts, f"State item {queue_id!r} source provider attempts")
        < MAX_BOUNDED_TOTAL_ATTEMPTS
        and source.get("source_unresolved_authority") is None
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} offline fallback source attempts are not exhausted"
        )
    strategy = source.get("source_repair_strategy")
    if strategy is not None and strategy not in {
        BOUNDED_SEED_RETRY,
        INLINE_PAUSE_MARKER,
        SENTENCE_BOUNDARY_SEGMENTATION,
    }:
        raise BulkGenerationError(
            f"State item {queue_id!r} offline fallback source repair is invalid"
        )


def _validate_offline_fallback_source_values(
    source: StateObject, queue_id: str
) -> None:
    for field in ("source_state_sha256", "source_item_sha256"):
        _required_sha256(
            source.get(field),
            f"State item {queue_id!r} source {field.removeprefix('source_')} SHA-256",
        )
    for field in ("source_model", "source_generation_profile"):
        _required_text(
            source.get(field), f"State item {queue_id!r} {field.replace('_', ' ')}"
        )
    _nonnegative_int(
        source.get("source_attempts"), f"State item {queue_id!r} source attempts"
    )
    _integer(source.get("source_seed"), f"State item {queue_id!r} source seed")
    parent_carry = source.get("source_parent_carry_forward")
    if parent_carry is not None and not isinstance(parent_carry, dict):
        raise BulkGenerationError(
            f"State item {queue_id!r} parent carry-forward is malformed"
        )
    voice = source.get("source_voice_reference")
    if (
        not isinstance(voice, dict)
        or set(voice) != {"character", "speaker", "aliases", "references"}
        or not isinstance(voice.get("references"), list)
        or not voice["references"]
    ):
        raise BulkGenerationError(
            f"State item {queue_id!r} source voice reference is malformed"
        )
    for reference in voice["references"]:
        _required_sha256(reference, f"State item {queue_id!r} source reference SHA-256")


def _validate_seed_application(result: StateObject, queue_id: str) -> None:
    if "seed_applied" not in result:
        return
    applied = result.get("seed_applied")
    if not isinstance(applied, bool):
        raise BulkGenerationError(
            f"State item {queue_id!r} seed_applied must be boolean"
        )
    expected = result.get("provider") != "pocket-tts"
    if applied != expected:
        raise BulkGenerationError(
            f"State item {queue_id!r} seed application conflicts with its backend"
        )


def _validate_active_attempt(
    active: StateObject, queue_by_id: QueueById | None
) -> None:
    queue_id = active.get("queue_id")
    if not isinstance(queue_id, str) or not queue_id:
        raise BulkGenerationError("Active attempt queue_id must be non-empty text")
    queue_item = _validate_active_queue_item(active, queue_id, queue_by_id)
    _validate_active_phase(active)
    integers = _validate_active_counts(active)
    if "seed" in active:
        _integer(active["seed"], "Active attempt seed")
    _validate_active_provider_attempts(active, integers)
    _validate_active_timestamps(active)
    if active.get("last_error") is not None and not isinstance(
        active.get("last_error"), str
    ):
        raise BulkGenerationError("Active attempt last_error must be text or null")
    if queue_item is not None:
        _validate_active_synthesis(active, queue_id, queue_item)


def _validate_active_queue_item(
    active: StateObject, queue_id: str, queue_by_id: QueueById | None
) -> VoiceGenerationQueueItem | None:
    if queue_by_id is None:
        return None
    queue_item = queue_by_id.get(queue_id)
    if queue_item is None:
        raise BulkGenerationError("Active attempt references an unknown queue item")
    expected = {
        "line_id": queue_item.line_id,
        "text": queue_item.text,
        "text_sha256": queue_item.text_sha256,
        "speaker": queue_item.speaker,
        "voice_character": queue_item.voice_character,
    }
    for field, value in expected.items():
        if field in active and active[field] != value:
            raise BulkGenerationError(
                f"Active attempt {field} does not match queue item {queue_id!r}"
            )
    return queue_item


def _validate_active_phase(active: StateObject) -> None:
    phase = active.get("phase")
    if phase is not None and phase not in {
        "generating",
        "validating",
        "publishing",
        "retrying",
    }:
        raise BulkGenerationError(f"Active attempt phase is invalid: {phase!r}")


def _validate_active_counts(active: StateObject) -> dict[str, int]:
    integers: dict[str, int] = {
        field: _positive_active_count(active[field], field)
        for field in ("attempt", "attempt_limit", "total_attempts", "provider_attempt")
        if field in active
    }
    if integers.get("attempt", 0) > integers.get("attempt_limit", float("inf")):
        raise BulkGenerationError("Active attempt exceeds its attempt limit")
    if integers.get("total_attempts", float("inf")) < integers.get("attempt", 0):
        raise BulkGenerationError("Active cumulative attempts are inconsistent")
    return integers


def _positive_active_count(value: object, field: str) -> int:
    count = _nonnegative_int(value, f"Active attempt {field}")
    if count < 1:
        raise BulkGenerationError(f"Active attempt {field} must be positive")
    return count


def _validate_active_provider_attempts(
    active: StateObject, integers: dict[str, int]
) -> None:
    if "attempts_by_provider" not in active:
        return
    provider_attempts = _provider_attempts(
        active,
        integers.get("total_attempts", 0),
        default_provider=active.get("provider"),
    )
    provider = active.get("provider")
    if (
        isinstance(provider, str)
        and "provider_attempt" in integers
        and provider_attempts.get(provider) != integers["provider_attempt"]
    ):
        raise BulkGenerationError("Active provider attempt counter is inconsistent")


def _validate_active_timestamps(active: StateObject) -> None:
    for field in ("started_at", "updated_at"):
        timestamp = active.get(field)
        if field in active and (
            not isinstance(timestamp, str) or not timestamp.strip()
        ):
            raise BulkGenerationError(f"Active attempt {field} must be timestamp text")


def _validate_active_synthesis(
    active: StateObject, queue_id: str, queue_item: VoiceGenerationQueueItem
) -> None:
    active_result = {
        "requested_voice_character": active.get("requested_voice_character"),
        "voice_character": active.get("synthesis_voice_character"),
        "provider": active.get("provider"),
    }
    for field in (
        "synthesis_configuration",
        "source_reference_binding",
        "synthesis_fallback",
        "narrator_character",
        "failure_repair",
        "synthesis_text_sha256",
        "text_transform",
        "attempts",
        "attempts_by_provider",
        "seed_applied",
    ):
        if field in active:
            active_result[field] = active[field]
    _validate_synthesis_identity(active_result, queue_id, queue_item)
    active_result["seed"] = active.get("seed")
    _validate_failure_repair_record(active_result, queue_id, queue_item)
    _validate_seed_application(active_result, queue_id)


def _validate_synthesis_controls(state: StateObject) -> None:
    registry = state.get("synthesis_controls")
    if registry is None:
        return
    if not isinstance(registry, dict):
        raise BulkGenerationError(
            "Generation state synthesis_controls must be an object"
        )
    for provenance, controls in registry.items():
        _required_sha256(provenance, "Synthesis-control provenance key")
        _validate_synthesis_control_set(controls)


def _validate_synthesis_control_set(controls: object) -> None:
    if not isinstance(controls, list):
        raise BulkGenerationError("Synthesis-control set must be a list")
    roles: set[str] = set()
    for control in controls:
        role = _validate_synthesis_control(control)
        if role in roles:
            raise BulkGenerationError(f"Synthesis-control role is duplicated: {role!r}")
        roles.add(role)


def _validate_synthesis_control(control: object) -> str:
    if not isinstance(control, dict):
        raise BulkGenerationError("Synthesis control must be an object")
    role = _required_text(control.get("role"), "Synthesis control role")
    kind = control.get("kind")
    expected_fields = {"role", "kind", "path", "sha256"}
    if kind == "directory":
        expected_fields.add("files")
    elif kind != "file":
        raise BulkGenerationError(f"Synthesis control kind is invalid: {kind!r}")
    if set(control) != expected_fields:
        raise BulkGenerationError(f"Synthesis control {role!r} has unsupported fields")
    _required_text(control.get("path"), f"Synthesis control {role!r} path")
    _required_sha256(control.get("sha256"), f"Synthesis control {role!r} sha256")
    if kind == "directory":
        _validate_synthesis_control_directory(control, role)
    return role


def _validate_synthesis_control_directory(control: StateObject, role: str) -> None:
    files = control.get("files")
    if not isinstance(files, list):
        raise BulkGenerationError(f"Synthesis control {role!r} files must be a list")
    parsed_files: list[dict[str, str]] = []
    paths: set[str] = set()
    for file_record in files:
        parsed = _parse_synthesis_control_file(file_record, role)
        if parsed["path"] in paths:
            raise BulkGenerationError(
                f"Synthesis control {role!r} file path is duplicated"
            )
        paths.add(parsed["path"])
        parsed_files.append(parsed)
    if _control_directory_digest(parsed_files) != control["sha256"]:
        raise BulkGenerationError(
            f"Synthesis control {role!r} directory digest is inconsistent"
        )


def _parse_synthesis_control_file(file_record: object, role: str) -> dict[str, str]:
    if not isinstance(file_record, dict) or set(file_record) != {"path", "sha256"}:
        raise BulkGenerationError(f"Synthesis control {role!r} file record is invalid")
    relative = file_record.get("path")
    if not isinstance(relative, str) or "\\" in relative:
        raise BulkGenerationError(f"Synthesis control {role!r} file path is invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise BulkGenerationError(f"Synthesis control {role!r} file path is unsafe")
    return {
        "path": relative,
        "sha256": _required_sha256(
            file_record.get("sha256"), f"Synthesis control {role!r} file sha256"
        ),
    }


def _validate_reviewed_waveform_publication(
    state: StateObject, queue_by_id: QueueById | None
) -> None:
    publication = state.get("reviewed_waveform_publication")
    if publication is None:
        return
    if not isinstance(publication, dict):
        raise BulkGenerationError(
            "Reviewed-waveform publication authority is malformed"
        )
    _validate_reviewed_waveform_publication_structure(publication)
    _validate_reviewed_waveform_publication_metadata(publication)
    items = publication.get("items")
    if not isinstance(items, list) or not items:
        raise BulkGenerationError("Reviewed-waveform publication ledger is empty")
    observed = [
        _validate_reviewed_waveform_ledger_item(ledger, state, queue_by_id)
        for ledger in items
    ]
    if observed != sorted(set(observed)):
        raise BulkGenerationError(
            "Reviewed-waveform publication items are not canonical"
        )


def _validate_reviewed_waveform_publication_structure(
    publication: StateObject,
) -> None:
    fields = {
        "schema",
        "schema_version",
        "batch_id",
        "reason",
        "publication_scope",
        "synthesis_reproducibility",
        "base_workspace_id",
        "base_workspace_path",
        "base_workspace_sha256",
        "base_state_path",
        "base_state_sha256",
        "queue_sha256",
        "selected_story_index_sha256",
        "selected_voice_manifest_sha256",
        "narrator_character",
        "narrator_reference_sha256s",
        "items",
    }
    if (
        set(publication) != fields
        or publication.get("schema") != REVIEWED_WAVEFORM_PUBLICATION_SCHEMA
        or publication.get("schema_version") != REVIEWED_WAVEFORM_PUBLICATION_VERSION
        or publication.get("reason") != REVIEWED_WAVEFORM_PUBLICATION_REASON
        or publication.get("publication_scope") != "exact_reviewed_waveform"
        or publication.get("synthesis_reproducibility") is not False
        or publication.get("batch_id")
        != canonical_document_sha256(
            {key: value for key, value in publication.items() if key != "batch_id"}
        )
    ):
        raise BulkGenerationError(
            "Reviewed-waveform publication authority is malformed"
        )


def _validate_reviewed_waveform_publication_metadata(
    publication: StateObject,
) -> None:
    for field in (
        "batch_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "selected_story_index_sha256",
        "selected_voice_manifest_sha256",
    ):
        _required_sha256(
            publication.get(field), f"Reviewed-waveform publication {field}"
        )
    _required_text(
        publication.get("base_workspace_id"),
        "Reviewed-waveform publication base workspace ID",
    )
    _required_text(
        publication.get("base_workspace_path"),
        "Reviewed-waveform publication base workspace path",
    )
    _required_text(
        publication.get("base_state_path"),
        "Reviewed-waveform publication base state path",
    )
    _required_text(
        publication.get("narrator_character"),
        "Reviewed-waveform publication narrator character",
    )
    narrator_references = publication.get("narrator_reference_sha256s")
    if (
        not isinstance(narrator_references, list)
        or not narrator_references
        or narrator_references != sorted(set(narrator_references))
    ):
        raise BulkGenerationError(
            "Reviewed-waveform narrator references are not canonical"
        )
    for digest in narrator_references:
        _required_sha256(digest, "Reviewed-waveform narrator reference SHA-256")


def _validate_reviewed_waveform_ledger_item(
    ledger: object, state: StateObject, queue_by_id: QueueById | None
) -> str:
    fields = {
        "queue_id",
        "line_id",
        "text_sha256",
        "speaker",
        "path",
        "file_sha256",
        "base_result_sha256",
        "base_result",
        "route",
    }
    if not isinstance(ledger, dict) or set(ledger) != fields:
        raise BulkGenerationError("Reviewed-waveform publication item is malformed")
    queue_id = _required_text(ledger.get("queue_id"), "Reviewed-waveform queue ID")
    _validate_reviewed_waveform_base_result(ledger, state, queue_id)
    _validate_reviewed_waveform_route(ledger.get("route"), queue_id)
    _validate_reviewed_waveform_queue_identity(ledger, queue_id, queue_by_id)
    return queue_id


def _validate_reviewed_waveform_base_result(
    ledger: StateObject, state: StateObject, queue_id: str
) -> None:
    state_items = state["items"]
    assert isinstance(state_items, dict)
    result = state_items.get(queue_id)
    base_result = ledger.get("base_result")
    if (
        not isinstance(result, dict)
        or result.get("status") != "approved"
        or result.get("review_status") != "approved"
        or not isinstance(base_result, dict)
        or result != base_result
        or canonical_document_sha256(base_result) != ledger.get("base_result_sha256")
        or any(
            result.get(field) != ledger.get(field)
            for field in ("line_id", "text_sha256", "path", "file_sha256")
        )
    ):
        raise BulkGenerationError(
            f"Reviewed-waveform publication item changed for {queue_id!r}"
        )
    for field, label in (
        ("text_sha256", "text SHA-256"),
        ("file_sha256", "WAV SHA-256"),
        ("base_result_sha256", "base result SHA-256"),
    ):
        _required_sha256(ledger.get(field), f"Reviewed-waveform {queue_id!r} {label}")


def _validate_reviewed_waveform_route(route: object, queue_id: str) -> None:
    fields = {"source", "status", "effective_character", "reference_sha256s"}
    if (
        not isinstance(route, dict)
        or set(route) != fields
        or route.get("source") not in {"config_rebase", "historical_reviewed_waveform"}
        or route.get("status") not in {"active", "not_reproducible"}
    ):
        raise BulkGenerationError(
            f"Reviewed-waveform route is invalid for {queue_id!r}"
        )
    _required_text(
        route.get("effective_character"),
        f"Reviewed-waveform {queue_id!r} effective character",
    )
    references = route.get("reference_sha256s")
    if not isinstance(references, list) or references != sorted(set(references)):
        raise BulkGenerationError(
            f"Reviewed-waveform references are not canonical for {queue_id!r}"
        )
    valid_routes = {
        "config_rebase": ("active", True),
        "historical_reviewed_waveform": ("not_reproducible", False),
    }
    expected_status, references_required = valid_routes[route["source"]]
    if route["status"] != expected_status or bool(references) != references_required:
        raise BulkGenerationError(
            f"Reviewed-waveform route is inconsistent for {queue_id!r}"
        )
    for digest in references:
        _required_sha256(digest, f"Reviewed-waveform {queue_id!r} reference SHA-256")


def _validate_reviewed_waveform_queue_identity(
    ledger: StateObject, queue_id: str, queue_by_id: QueueById | None
) -> None:
    if queue_by_id is None:
        return
    queue_item = queue_by_id.get(queue_id)
    if (
        queue_item is None
        or queue_item.line_id != ledger.get("line_id")
        or queue_item.text_sha256 != ledger.get("text_sha256")
        or queue_item.speaker != ledger.get("speaker")
    ):
        raise BulkGenerationError(
            f"Reviewed-waveform queue identity changed for {queue_id!r}"
        )


def reviewed_waveform_publication_queue_ids(state: StateObject) -> frozenset[str]:
    """Return exact approved queue IDs covered by a validated migration."""
    publication = state.get("reviewed_waveform_publication")
    if not isinstance(publication, dict):
        return frozenset()
    return frozenset(item["queue_id"] for item in publication["items"])


def _validate_success_item(
    queue_id: str,
    result: StateObject,
    output_directory: Path,
    queue_item: VoiceGenerationQueueItem | None,
    *,
    state_schema: object,
) -> None:
    if queue_item is not None and (
        result.get("line_id") != queue_item.line_id
        or result.get("text_sha256") != queue_item.text_sha256
    ):
        raise BulkGenerationError(
            f"Generation state identity does not match queue item {queue_id!r}"
        )
    _validate_success_item_inputs(result, queue_id)
    _validate_synthesis_identity(result, queue_id, queue_item)
    if state_schema == STATE_SCHEMA:
        _validate_current_success_item(result, queue_id)
    relative = safe_generation_relative_path(
        result.get("path"), f"State item {queue_id!r} path"
    )
    audio = contained_generation_path(output_directory, relative, "Generated WAV")
    quality, samples = validate_success_file_with_samples(queue_id, result, audio)
    if state_schema == STATE_SCHEMA:
        _validate_success_speech_quality(result, queue_id, quality, samples)
    _validate_success_audio_event_composition(result, queue_id, output_directory)


def _validate_success_item_inputs(result: StateObject, queue_id: str) -> None:
    _required_text(result.get("provider"), f"State item {queue_id!r} provider")
    _required_text(result.get("model"), f"State item {queue_id!r} model")
    _required_sha256(
        result.get("prompt_sha256"), f"State item {queue_id!r} prompt_sha256"
    )
    _integer(result.get("seed"), f"State item {queue_id!r} seed")


def _validate_current_success_item(result: StateObject, queue_id: str) -> None:
    for field in ("generation_profile", "voice_character"):
        _required_text(result.get(field), f"State item {queue_id!r} {field}")
    for field in (
        "synthesis_provenance_sha256",
        "queue_annotations_sha256",
        "synthesis_text_sha256",
    ):
        _required_sha256(result.get(field), f"State item {queue_id!r} {field}")
    if result.get("text_transform") is not None:
        _required_text(
            result.get("text_transform"), f"State item {queue_id!r} text_transform"
        )
    if result.get("prompt_applied") is not False:
        raise BulkGenerationError(
            f"State item {queue_id!r} prompt_applied must be false"
        )


def _validate_success_speech_quality(
    result: StateObject, queue_id: str, quality: AudioQuality, samples: np.ndarray
) -> None:
    stored = result.get("speech_quality")
    if not isinstance(stored, dict):
        raise BulkGenerationError(
            f"Generated WAV speech quality is missing for {queue_id!r}"
        )
    version = stored.get("analysis_version", LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version
        not in {LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION, SPEECH_QUALITY_ANALYSIS_VERSION}
    ):
        raise BulkGenerationError(
            f"Generated WAV speech quality version is invalid for {queue_id!r}"
        )
    actual = asdict(
        measure_generated_speech_samples(
            samples,
            sample_rate=quality.sample_rate,
            duration_seconds=quality.duration_seconds,
            analysis_version=version,
        )
    )
    if version == LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION:
        actual.pop("analysis_version")
    if stored != actual:
        raise BulkGenerationError(
            f"Generated WAV speech quality mismatch for {queue_id!r}"
        )


def _validate_success_audio_event_composition(
    result: StateObject, queue_id: str, output_directory: Path
) -> None:
    if (
        result.get("provider") != "original-game-audio-event"
        and "audio_event_composition" not in result
    ):
        return
    module = importlib.import_module("vntts.authoring.audio_event_workspace")
    try:
        module.validate_audio_event_composition_state_item(
            output_directory.parent, queue_id, result
        )
    except module.AudioEventWorkspaceError as error:
        raise BulkGenerationError(str(error)) from error


def _control_directory_digest(records: list[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        relative = record["path"].encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(record["sha256"]))
    return digest.hexdigest()


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BulkGenerationError(f"{label} must be non-empty text")
    return value.strip()


def _required_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not is_lowercase_sha256(value):
        raise BulkGenerationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BulkGenerationError(f"{label} must be an integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    value = _integer(value, label)
    if value < 0:
        raise BulkGenerationError(f"{label} cannot be negative")
    return value


def _provider_attempts(
    result: StateObject, total_attempts: int, *, default_provider: object = None
) -> dict[str, int]:
    """Return validated per-provider counts, deriving old state losslessly."""
    value = result.get("attempts_by_provider")
    if value is None:
        previous = result.get("provider") or default_provider
        if total_attempts and isinstance(previous, str) and previous.strip():
            return {previous: total_attempts}
        return {}
    if not isinstance(value, dict):
        raise BulkGenerationError("Attempts by provider must be an object")
    canonical = {}
    for provider, count in value.items():
        if not isinstance(provider, str) or not provider.strip():
            raise BulkGenerationError(
                "Attempts by provider keys must be non-empty text"
            )
        canonical[provider] = _nonnegative_int(
            count, f"Attempts for provider {provider!r}"
        )
    if sum(canonical.values()) > total_attempts:
        raise BulkGenerationError("Attempts by provider exceed cumulative attempts")
    return canonical


# Public domain APIs; bulk_generation retains its historical local names as aliases.
validate_live_fallback_evidence = _validate_live_fallback_evidence
validate_render_review_fallback_evidence = _validate_render_review_fallback_evidence
validate_failure_record = _validate_failure_record
validate_pause_diagnosis = _validate_pause_diagnosis
validate_live_fallback_decision = _validate_live_fallback_decision
validate_missing_voice_live_fallback_evidence = (
    _validate_missing_voice_live_fallback_evidence
)
validate_synthesis_identity = _validate_synthesis_identity
validate_synthesis_configuration = _validate_synthesis_configuration
validate_failure_repair_record = _validate_failure_repair_record
validate_seed_application = _validate_seed_application
validate_active_attempt = _validate_active_attempt
validate_synthesis_controls = _validate_synthesis_controls
validate_success_item = _validate_success_item
safe_state_relative_path = safe_generation_relative_path
contained_state_path = contained_generation_path
control_directory_digest = _control_directory_digest
required_state_text = _required_text
required_state_sha256 = _required_sha256
state_integer = _integer
state_nonnegative_int = _nonnegative_int
provider_attempts = _provider_attempts


def validate_generation_state_document(
    document: object,
    output_directory: str | Path,
    queue: VoiceGenerationQueue | None,
    queue_sha256: str | None,
) -> StateObject:
    """Validate captured state semantics without reopening its JSON path."""
    state = copy.deepcopy(document)
    assert isinstance(state, dict)
    _validate_state_document(
        state,
        Path(output_directory).expanduser().resolve(),
        queue,
        queue_sha256,
    )
    return state


__all__ = [
    "AUTOMATIC_RECOVERY_LIVE_FALLBACK_EVIDENCE_SCHEMA",
    "AUDIO_EVENT_PROJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA",
    "AUDIO_EVENT_OMISSION_REASON",
    "AUDIO_EVENT_OMISSION_SCHEMA",
    "AUDIO_EVENT_OMISSION_VERSION",
    "FAILURE_KINDS",
    "LEGACY_STATE_SCHEMA",
    "LEGACY_STATE_VERSION",
    "LIVE_FALLBACK_EVIDENCE_SCHEMA",
    "LIVE_FALLBACK_EVIDENCE_VERSION",
    "LIVE_FALLBACK_AUDIO_EVENT_PROJECTION_VERSION",
    "LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED",
    "LIVE_FALLBACK_AUTOMATIC_RECOVERY_VERSION",
    "LIVE_FALLBACK_REVIEWED_REJECTION_VERSION",
    "LIVE_FALLBACK_KNOWN_ROLE_EVIDENCE_VERSION",
    "LIVE_FALLBACK_HYPOTHESES_EXHAUSTED",
    "LIVE_FALLBACK_MISSING_VOICE_EVIDENCE_VERSION",
    "LIVE_FALLBACK_REASONS",
    "LIVE_FALLBACK_REVIEW_EVIDENCE_VERSION",
    "LIVE_FALLBACK_SCHEMA",
    "LIVE_FALLBACK_VERSION",
    "MISSING_VOICE_LIVE_FALLBACK_EVIDENCE_SCHEMA",
    "KNOWN_ROLE_LIVE_FALLBACK_EVIDENCE_SCHEMA",
    "REVIEWED_WAVEFORM_PUBLICATION_REASON",
    "REVIEWED_WAVEFORM_PUBLICATION_SCHEMA",
    "REVIEWED_WAVEFORM_PUBLICATION_VERSION",
    "REVIEWED_REJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA",
    "STATE_SCHEMA",
    "STATE_VERSION",
    "contained_state_path",
    "control_directory_digest",
    "load_stable_generation_queue",
    "provider_attempts",
    "reviewed_waveform_publication_queue_ids",
    "required_state_sha256",
    "required_state_text",
    "safe_state_relative_path",
    "state_integer",
    "state_nonnegative_int",
    "validate_failure_record",
    "validate_failure_repair_record",
    "validate_generation_state_document",
    "validate_live_fallback_evidence",
    "validate_seed_application",
    "validate_success_item",
    "validate_synthesis_identity",
]
