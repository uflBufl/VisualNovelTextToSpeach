"""Bounded automatic recovery for player-owned offline generation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from threading import Event, Lock

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
    VoiceGenerationQueueItem,
)
from vntts_artifacts.voice_manifest import VoiceManifestError

from vntts.authoring.bulk_generation import (
    AUTOMATIC_RECOVERY_LIVE_FALLBACK_ACTIONS,
    BulkGenerationError,
    authorize_live_fallback,
    generation_failure_repair_plan,
    is_spoken_queue_item,
    load_generation_state,
)
from vntts.authoring.generation_state import (
    LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED,
)
from vntts.document_identity import is_lowercase_sha256
from vntts.pregeneration_generation import (
    OfflineGenerationCancelled,
    OfflineGenerationError,
    OfflineGenerationResult,
    OfflineGenerationWorker,
    validate_offline_generation_result,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_voices import VoicePlan
from vntts.voices import (
    CharacterVoiceRegistry,
    pocket_tts_preset_voices,
    synthesis_character_for_line,
)

AUTOMATIC_ACTION_ORDER = (
    "safe_resume",
    "sentence_boundary_segmentation",
    "edge_silence_trim",
    "bounded_seed_retry",
    "offline_fallback_backend",
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
    deferred_batches: tuple[OfflineRecoveryBatch, ...] = ()
    live_fallback_queue_ids: tuple[str, ...] = ()

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
    live_fallbacks: int = 0


def plan_automatic_recovery(generation_input, voice_plan, generation_result):
    """Derive exact safe batches from current checksum-bound failure evidence."""
    validate_offline_generation_result(
        generation_input,
        generation_result,
        "recovery",
        error_type=OfflineRecoveryError,
    )
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
    deferred_queue_ids = {}
    live_fallback_queue_ids = []
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
        provider = record.get("provider")
        if (
            provider == "pocket-tts"
            and action in AUTOMATIC_RECOVERY_LIVE_FALLBACK_ACTIONS
        ):
            deferred[action] += 1
            deferred_queue_ids.setdefault(action, []).append(queue_id)
            live_fallback_queue_ids.append(queue_id)
        elif action in grouped:
            grouped[action].append(queue_id)
        elif provider is not None and action != "provenance_recovery_or_regeneration":
            grouped["offline_fallback_backend"].append(queue_id)
        else:
            deferred[action] += 1
            deferred_queue_ids.setdefault(action, []).append(queue_id)
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
        deferred_batches=tuple(
            OfflineRecoveryBatch(action, tuple(sorted(queue_ids)))
            for action, queue_ids in sorted(deferred_queue_ids.items())
        ),
        live_fallback_queue_ids=tuple(sorted(live_fallback_queue_ids)),
    )


class OfflineRecoveryWorker:
    """Apply each safe queue/action pair at most once, replanning after changes."""

    def __init__(
        self,
        generator=None,
        *,
        planner=plan_automatic_recovery,
        terminalizer=None,
    ):
        self.generator = generator or OfflineGenerationWorker()
        self.planner = planner
        self.terminalizer = terminalizer or _terminalize_pocket_failures
        self._priority_lock = Lock()
        self._priority_line = None

    def prioritize_line(self, line_id: str, text_sha256: str) -> bool:
        if not all(
            isinstance(value, str) and value for value in (line_id, text_sha256)
        ):
            return False
        with self._priority_lock:
            self._priority_line = line_id, text_sha256
        return True

    def recover(
        self,
        generation_input,
        voice_plan,
        generation_result,
        cancel_event=None,
        *,
        queue_id=None,
    ):
        validate_offline_generation_result(
            generation_input,
            generation_result,
            "recovery",
            error_type=OfflineRecoveryError,
        )
        if not isinstance(voice_plan, VoicePlan):
            raise OfflineRecoveryError("Offline voice plan is invalid")
        if generation_result.failed == 0:
            return OfflineRecoveryResult(generation_result, 0, 0, 0, ())
        initial_failures = generation_result.failed
        current = generation_result
        applied = set()
        terminalized = 0
        terminalization_attempted = False
        scoped_initial_failures = None
        while True:
            plan = self.planner(generation_input, voice_plan, current)
            if queue_id is not None and scoped_initial_failures is None:
                scoped_initial_failures = int(_plan_contains(plan, queue_id))
                if not scoped_initial_failures:
                    return OfflineRecoveryResult(current, 0, 0, 0, ())
            next_batch = _next_recovery_batch(plan, applied, queue_id)
            if next_batch is None:
                terminal_queue_ids = tuple(
                    candidate
                    for candidate in plan.live_fallback_queue_ids
                    if queue_id is None or candidate == queue_id
                )
                if terminal_queue_ids and not terminalization_attempted:
                    current = self.terminalizer(
                        generation_input,
                        current,
                        terminal_queue_ids,
                        cancel_event,
                        generator=self.generator,
                    )
                    terminalized = len(terminal_queue_ids)
                    terminalization_attempted = True
                    continue
                remaining = (
                    Counter(dict(plan.deferred_action_counts))
                    if queue_id is None
                    else Counter(
                        batch.action
                        for batch in plan.deferred_batches
                        if queue_id in batch.queue_ids
                    )
                )
                for batch in plan.automatic_batches:
                    remaining[batch.action] += sum(
                        queue_id is None or candidate == queue_id
                        for candidate in batch.queue_ids
                    )
                remaining_failed = (
                    current.failed
                    if queue_id is None
                    else int(_plan_contains(plan, queue_id))
                )
                return OfflineRecoveryResult(
                    generation=current,
                    attempted_actions=len(applied),
                    recovered=max(
                        0,
                        (
                            initial_failures
                            if queue_id is None
                            else scoped_initial_failures or 0
                        )
                        - remaining_failed
                        - terminalized,
                    ),
                    remaining_failed=remaining_failed,
                    remaining_action_counts=tuple(sorted(remaining.items())),
                    live_fallbacks=terminalized,
                )
            repair_voice_plan = (
                replace(
                    voice_plan,
                    synthesis_backend="pocket-tts",
                    synthesis_model=None,
                    synthesis_profile="default",
                )
                if next_batch.action == "offline_fallback_backend"
                else voice_plan
            )
            current = self.generator.repair(
                generation_input,
                repair_voice_plan,
                current,
                action=next_batch.action,
                queue_ids=next_batch.queue_ids,
                cancel_event=cancel_event,
            )
            applied.update(
                (queue_id, next_batch.action) for queue_id in next_batch.queue_ids
            )

    def generate_and_recover(
        self,
        generation_input: PregenerationInput,
        voice_plan: VoicePlan,
        cancel_event: Event | None = None,
    ) -> OfflineRecoveryResult:
        """Finish safe recovery for each dialogue before starting the next one."""
        queue_ids, line_queue_ids = _ordered_generation_queue_ids(
            generation_input, voice_plan
        )
        if not queue_ids:
            generation = self.generator.generate(
                generation_input, voice_plan, cancel_event
            )
            return OfflineRecoveryResult(generation, 0, 0, generation.failed, ())
        try:
            current = self.generator.inspect(generation_input)
        except OfflineGenerationError:
            current = None
        statuses = (
            _generation_queue_statuses(current, generation_input)
            if current is not None
            else {}
        )
        attempted = recovered = live_fallbacks = 0
        remaining: Counter[str] = Counter()
        pending = list(queue_ids)
        while pending:
            queue_id = self._take_priority(line_queue_ids, pending) or pending[0]
            pending.remove(queue_id)
            if statuses.get(queue_id) in {
                "generated",
                "approved",
                "live_fallback",
                "omitted",
                "not_reproducible",
            }:
                continue
            if statuses.get(queue_id) != "failed":
                current = self.generator.generate(
                    generation_input,
                    voice_plan,
                    cancel_event,
                    queue_ids=(queue_id,),
                )
            result = self.recover(
                generation_input,
                voice_plan,
                current,
                cancel_event,
                queue_id=queue_id,
            )
            current = result.generation
            attempted += result.attempted_actions
            recovered += result.recovered
            live_fallbacks += result.live_fallbacks
            remaining.update(dict(result.remaining_action_counts))
            statuses[queue_id] = "failed" if result.remaining_failed else "generated"
        if current is None:
            raise OfflineRecoveryError("Offline generation produced no result")
        return OfflineRecoveryResult(
            current,
            attempted,
            recovered,
            current.failed,
            tuple(sorted(remaining.items())),
            live_fallbacks,
        )

    def _take_priority(
        self,
        line_queue_ids: Mapping[tuple[str, str], str],
        pending: list[str],
    ) -> str | None:
        with self._priority_lock:
            identity, self._priority_line = self._priority_line, None
        queue_id = line_queue_ids.get(identity)
        return queue_id if queue_id in pending else None


def _next_recovery_batch(
    plan: OfflineRecoveryPlan,
    applied: set[tuple[str, str]],
    queue_id: str | None,
) -> OfflineRecoveryBatch | None:
    for batch in plan.automatic_batches:
        fresh = tuple(
            candidate
            for candidate in batch.queue_ids
            if (queue_id is None or candidate == queue_id)
            and (candidate, batch.action) not in applied
        )
        if fresh:
            return OfflineRecoveryBatch(batch.action, fresh)
    return None


def _plan_contains(plan: OfflineRecoveryPlan, queue_id: str) -> bool:
    return queue_id in plan.live_fallback_queue_ids or any(
        queue_id in batch.queue_ids
        for batch in (*plan.automatic_batches, *plan.deferred_batches)
    )


def _ordered_generation_queue_ids(
    generation_input: PregenerationInput,
    voice_plan: VoicePlan,
) -> tuple[tuple[str, ...], dict[tuple[str, str], str]]:
    try:
        if sha256_file(generation_input.queue) != generation_input.queue_sha256:
            raise OfflineRecoveryError("Offline generation queue changed")
        queue = VoiceGenerationQueue.load(generation_input.queue)
        voices = CharacterVoiceRegistry.from_file(generation_input.voice_manifest)
        projections = set(generation_input.audio_event_projection_queue_ids)
        omissions = set(generation_input.audio_event_omission_queue_ids)
        narrator_roles = set(generation_input.narrator_fallback_roles)

        def has_voice(item: VoiceGenerationQueueItem) -> bool:
            requested = synthesis_character_for_line(item.speaker, item.voice_character)
            voice = voices.resolve(requested)
            return (
                requested in narrator_roles
                or voice is not None
                and (
                    bool(voice.references)
                    or voice_plan.synthesis_backend == "pocket-tts"
                    and voice.speaker in pocket_tts_preset_voices
                )
            )

        queue_ids = tuple(
            item.queue_id
            for item in queue.items
            if item.action == "generate"
            and item.queue_id not in omissions
            and (item.queue_id in projections or is_spoken_queue_item(item))
            and has_voice(item)
        )
    except (
        BulkGenerationError,
        OSError,
        VoiceGenerationQueueError,
        VoiceManifestError,
        ValueError,
    ) as error:
        raise OfflineRecoveryError(
            f"Unable to sequence offline generation: {error}"
        ) from error
    if len(queue_ids) != generation_input.ready_items:
        raise OfflineRecoveryError("Offline generation queue readiness changed")
    return queue_ids, {
        (item.line_id, item.text_sha256): item.queue_id
        for item in queue.items
        if item.queue_id in queue_ids
    }


def _generation_queue_statuses(
    generation_result: OfflineGenerationResult,
    generation_input: PregenerationInput,
) -> dict[object, str]:
    try:
        state = load_generation_state(
            generation_result.state,
            generation_input.queue,
        )
    except (BulkGenerationError, OSError, ValueError) as error:
        raise OfflineRecoveryError(
            f"Unable to resume offline generation: {error}"
        ) from error
    items = state.get("items")
    if not isinstance(items, dict):
        raise OfflineRecoveryError("Offline generation state is invalid")
    statuses: dict[object, str] = {}
    for queue_id, item in items.items():
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if isinstance(status, str):
            statuses[queue_id] = status
    return statuses


def _terminalize_pocket_failures(
    generation_input,
    generation_result,
    queue_ids,
    cancel_event,
    *,
    generator,
):
    validate_offline_generation_result(
        generation_input,
        generation_result,
        "recovery",
        error_type=OfflineRecoveryError,
    )
    for queue_id in sorted(set(queue_ids)):
        if cancel_event is not None and cancel_event.is_set():
            raise OfflineGenerationCancelled("Automatic recovery was cancelled")
        try:
            authorize_live_fallback(
                generation_result.state,
                generation_input.queue,
                queue_id,
                reason=LIVE_FALLBACK_AUTOMATIC_RECOVERY_EXHAUSTED,
                model="pocket-tts",
            )
        except (BulkGenerationError, OSError, ValueError) as error:
            raise OfflineRecoveryError(
                f"Unable to preserve live fallback for {queue_id!r}: {error}"
            ) from error
    return generator.inspect(generation_input)


def _sha256(value, label):
    if not is_lowercase_sha256(value):
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
