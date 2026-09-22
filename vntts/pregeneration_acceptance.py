"""Read-only validation of prepared audio from older callers."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from time import perf_counter, process_time

from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
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
        approved = sum(
            (item.get("status"), item.get("review_status")) == ("approved", "approved")
            for item in items.values()
            if isinstance(item, dict)
        )
        if any(
            not isinstance(item, dict)
            or (item.get("status"), item.get("review_status"))
            not in {("approved", "approved"), ("live_fallback", "live_fallback")}
            for item in items.values()
        ):
            raise OfflineAcceptanceError("Prepared audio has unfinished items")
        _raise_if_cancelled(cancel_event)
        return OfflineAcceptanceResult(generation_result, approved)


def _raise_if_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OfflineGenerationCancelled("Prepared audio validation was cancelled")


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
