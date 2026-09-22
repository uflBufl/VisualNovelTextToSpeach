"""Automatic acceptance of technically validated self-service WAVs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import Event
from time import perf_counter, process_time

from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    generation_review_authorities,
    load_generation_state,
    review_generation_cohort,
)
from vntts.pregeneration_generation import (
    OfflineGenerationCancelled,
    OfflineGenerationError,
    OfflineGenerationResult,
    OfflineGenerationWorker,
    validate_offline_generation_result,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.support import record_background_operation


class OfflineAcceptanceError(OfflineGenerationError):
    """Validated generated audio could not be accepted atomically."""


@dataclass(frozen=True)
class OfflineAcceptanceResult:
    generation: OfflineGenerationResult
    approved: int


class OfflineAcceptanceWorker:
    def __init__(self, generator: object | None = None) -> None:
        self.generator = generator or OfflineGenerationWorker()

    def accept(
        self,
        generation_input: PregenerationInput,
        generation_result: OfflineGenerationResult,
        cancel_event: Event | None = None,
    ) -> OfflineAcceptanceResult:
        phase_started, cpu_started = perf_counter(), process_time()
        validate_offline_generation_result(
            generation_input,
            generation_result,
            "acceptance",
            error_type=OfflineAcceptanceError,
        )
        _record_acceptance_phase("validation", phase_started, cpu_started)
        _raise_if_cancelled(cancel_event)
        if generation_result.pending_review == 0:
            return OfflineAcceptanceResult(generation_result, 0)
        phase_started, cpu_started = perf_counter(), process_time()
        try:
            state = load_generation_state(
                generation_result.state,
                generation_input.queue,
            )
        except (BulkGenerationError, OSError, ValueError) as error:
            raise OfflineAcceptanceError(
                f"Unable to inspect generated audio: {error}"
            ) from error
        _record_acceptance_phase("state-load", phase_started, cpu_started)
        items = state.get("items")
        if not isinstance(items, dict):
            raise OfflineAcceptanceError("Offline generation state is invalid")
        pending = tuple(
            sorted(
                queue_id
                for queue_id, item in items.items()
                if isinstance(queue_id, str)
                and isinstance(item, dict)
                and (item.get("status"), item.get("review_status"))
                == ("generated", "pending_review")
            )
        )
        if not pending:
            return OfflineAcceptanceResult(generation_result, 0)
        _raise_if_cancelled(cancel_event)
        try:
            phase_started, cpu_started = perf_counter(), process_time()
            authorities = generation_review_authorities(
                generation_result.state,
                pending,
            )
            _record_acceptance_phase(
                "authority-snapshot",
                phase_started,
                cpu_started,
                item_count=len(pending),
            )
            _raise_if_cancelled(cancel_event)
            phase_started, cpu_started = perf_counter(), process_time()
            review_generation_cohort(
                generation_result.state,
                generation_input.queue,
                authorities,
                "approved",
                provenance={
                    "schema": "vntts.self-service-automatic-acceptance",
                    "schema_version": 1,
                    "decision_source": "generation-technical-gates",
                    "human_reviewed": False,
                },
            )
            _record_acceptance_phase(
                "decision-commit",
                phase_started,
                cpu_started,
                item_count=len(pending),
            )
        except OfflineGenerationCancelled:
            raise
        except (BulkGenerationError, OSError, ValueError) as error:
            raise OfflineAcceptanceError(
                f"Unable to accept generated audio: {error}"
            ) from error
        return OfflineAcceptanceResult(
            replace(generation_result, pending_review=0),
            len(pending),
        )


def _raise_if_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OfflineGenerationCancelled("Automatic audio acceptance was cancelled")


def _record_acceptance_phase(
    name: str, started: float, cpu_started: float, **details: object
) -> None:
    record_background_operation(
        f"pregeneration-acceptance-{name}",
        (perf_counter() - started) * 1000,
        "complete",
        cpu_ms=(process_time() - cpu_started) * 1000,
        **details,
    )


__all__ = [
    "OfflineAcceptanceError",
    "OfflineAcceptanceResult",
    "OfflineAcceptanceWorker",
]
