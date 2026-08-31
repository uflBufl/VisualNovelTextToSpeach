"""Bounded automatic recovery for player-owned offline generation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    generation_failure_repair_plan,
)
from vntts.pregeneration_generation import (
    OfflineGenerationError,
    OfflineGenerationResult,
    OfflineGenerationWorker,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_voices import VoicePlan

AUTOMATIC_ACTION_ORDER = (
    "safe_resume",
    "sentence_boundary_segmentation",
    "edge_silence_trim",
    "bounded_seed_retry",
)


class OfflineRecoveryError(OfflineGenerationError):
    """The current generated output cannot be recovered safely."""


@dataclass(frozen=True)
class OfflineRecoveryBatch:
    action: str
    queue_ids: tuple[str, ...]


@dataclass(frozen=True)
class OfflineRecoveryPlan:
    state_sha256: str
    queue_sha256: str
    failure_count: int
    automatic_batches: tuple[OfflineRecoveryBatch, ...]
    deferred_action_counts: tuple[tuple[str, int], ...]

    @property
    def automatic_count(self):
        return sum(len(batch.queue_ids) for batch in self.automatic_batches)

    @property
    def deferred_count(self):
        return sum(count for _action, count in self.deferred_action_counts)


@dataclass(frozen=True)
class OfflineRecoveryResult:
    generation: OfflineGenerationResult
    attempted_actions: int
    recovered: int
    remaining_failed: int
    remaining_action_counts: tuple[tuple[str, int], ...]


def plan_automatic_recovery(generation_input, voice_plan, generation_result):
    """Derive exact safe batches from current checksum-bound failure evidence."""
    _validate_inputs(generation_input, generation_result)
    if not isinstance(voice_plan, VoicePlan):
        raise OfflineRecoveryError("Offline voice plan is invalid")
    try:
        document = generation_failure_repair_plan(
            generation_result.state,
            generation_input.queue,
        )
    except (BulkGenerationError, OSError, ValueError) as error:
        raise OfflineRecoveryError(
            f"Unable to inspect offline generation failures: {error}"
        ) from error
    records = document.get("records")
    if not isinstance(records, list):
        raise OfflineRecoveryError("Offline recovery plan is malformed")
    grouped = {action: [] for action in AUTOMATIC_ACTION_ORDER}
    deferred = Counter()
    seen_queue_ids = set()
    for record in records:
        if not isinstance(record, dict):
            raise OfflineRecoveryError("Offline recovery record is malformed")
        queue_id = record.get("queue_id")
        action = record.get("action")
        if (
            not isinstance(queue_id, str)
            or not queue_id.strip()
            or queue_id != queue_id.strip()
            or queue_id in seen_queue_ids
            or not isinstance(action, str)
            or not action
        ):
            raise OfflineRecoveryError("Offline recovery record is malformed")
        seen_queue_ids.add(queue_id)
        if action in grouped and not (
            action == "bounded_seed_retry"
            and voice_plan.synthesis_backend == "pocket-tts"
        ):
            grouped[action].append(queue_id)
        else:
            deferred[action] += 1
    failure_count = document.get("failure_count")
    if (
        not isinstance(failure_count, int)
        or isinstance(failure_count, bool)
        or failure_count < 0
        or failure_count != len(records)
    ):
        raise OfflineRecoveryError("Offline recovery failure count changed")
    return OfflineRecoveryPlan(
        state_sha256=_sha256(document.get("state_sha256"), "state"),
        queue_sha256=_sha256(document.get("queue_sha256"), "queue"),
        failure_count=failure_count,
        automatic_batches=tuple(
            OfflineRecoveryBatch(action, tuple(sorted(grouped[action])))
            for action in AUTOMATIC_ACTION_ORDER
            if grouped[action]
        ),
        deferred_action_counts=tuple(sorted(deferred.items())),
    )


class OfflineRecoveryWorker:
    """Apply each safe queue/action pair at most once, replanning after changes."""

    def __init__(self, generator=None, *, planner=plan_automatic_recovery):
        self.generator = generator or OfflineGenerationWorker()
        self.planner = planner

    def recover(
        self,
        generation_input,
        voice_plan,
        generation_result,
        cancel_event=None,
    ):
        _validate_inputs(generation_input, generation_result)
        if not isinstance(voice_plan, VoicePlan):
            raise OfflineRecoveryError("Offline voice plan is invalid")
        initial_failures = generation_result.failed
        current = generation_result
        applied = set()
        while True:
            plan = self.planner(generation_input, voice_plan, current)
            next_batch = None
            for batch in plan.automatic_batches:
                fresh = tuple(
                    queue_id
                    for queue_id in batch.queue_ids
                    if (queue_id, batch.action) not in applied
                )
                if fresh:
                    next_batch = OfflineRecoveryBatch(batch.action, fresh)
                    break
            if next_batch is None:
                remaining = Counter(dict(plan.deferred_action_counts))
                for batch in plan.automatic_batches:
                    remaining[batch.action] += len(batch.queue_ids)
                return OfflineRecoveryResult(
                    generation=current,
                    attempted_actions=len(applied),
                    recovered=max(0, initial_failures - current.failed),
                    remaining_failed=current.failed,
                    remaining_action_counts=tuple(sorted(remaining.items())),
                )
            current = self.generator.repair(
                generation_input,
                voice_plan,
                current,
                action=next_batch.action,
                queue_ids=next_batch.queue_ids,
                cancel_event=cancel_event,
            )
            applied.update(
                (queue_id, next_batch.action) for queue_id in next_batch.queue_ids
            )


def _validate_inputs(generation_input, generation_result):
    if not isinstance(generation_input, PregenerationInput):
        raise OfflineRecoveryError("Offline generation input is invalid")
    if not isinstance(generation_result, OfflineGenerationResult):
        raise OfflineRecoveryError("Offline generation result is invalid")
    expected_output = generation_input.directory.parent / (
        f"generation-output-{generation_input.identity[:16]}"
    )
    if generation_result.output.resolve() != expected_output.resolve():
        raise OfflineRecoveryError("Offline recovery output identity changed")


def _sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OfflineRecoveryError(f"Offline recovery {label} hash is invalid")
    return value


__all__ = [
    "AUTOMATIC_ACTION_ORDER",
    "OfflineRecoveryBatch",
    "OfflineRecoveryError",
    "OfflineRecoveryPlan",
    "OfflineRecoveryResult",
    "OfflineRecoveryWorker",
    "plan_automatic_recovery",
]
