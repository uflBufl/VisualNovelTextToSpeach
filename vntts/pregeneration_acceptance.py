"""Automatic acceptance of technically validated self-service WAVs."""

from __future__ import annotations

from dataclasses import dataclass

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
)
from vntts.pregeneration_queue import PregenerationInput


class OfflineAcceptanceError(OfflineGenerationError):
    """Validated generated audio could not be accepted atomically."""


@dataclass(frozen=True)
class OfflineAcceptanceResult:
    generation: OfflineGenerationResult
    approved: int


class OfflineAcceptanceWorker:
    def __init__(self, generator=None):
        self.generator = generator or OfflineGenerationWorker()

    def accept(self, generation_input, generation_result, cancel_event=None):
        _validate_inputs(generation_input, generation_result)
        _raise_if_cancelled(cancel_event)
        try:
            state = load_generation_state(
                generation_result.state,
                generation_input.queue,
            )
        except (BulkGenerationError, OSError, ValueError) as error:
            raise OfflineAcceptanceError(
                f"Unable to inspect generated audio: {error}"
            ) from error
        pending = tuple(
            sorted(
                queue_id
                for queue_id, item in state.get("items", {}).items()
                if isinstance(item, dict)
                and (item.get("status"), item.get("review_status"))
                == ("generated", "pending_review")
            )
        )
        if not pending:
            return OfflineAcceptanceResult(
                self.generator.inspect(generation_input),
                0,
            )
        _raise_if_cancelled(cancel_event)
        try:
            authorities = generation_review_authorities(
                generation_result.state,
                pending,
            )
            _raise_if_cancelled(cancel_event)
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
        except OfflineGenerationCancelled:
            raise
        except (BulkGenerationError, OSError, ValueError) as error:
            raise OfflineAcceptanceError(
                f"Unable to accept generated audio: {error}"
            ) from error
        return OfflineAcceptanceResult(
            self.generator.inspect(generation_input),
            len(pending),
        )


def _validate_inputs(generation_input, generation_result):
    if not isinstance(generation_input, PregenerationInput):
        raise OfflineAcceptanceError("Offline generation input is invalid")
    if not isinstance(generation_result, OfflineGenerationResult):
        raise OfflineAcceptanceError("Offline generation result is invalid")
    expected_output = generation_input.directory.parent / (
        f"generation-output-{generation_input.identity[:16]}"
    )
    if generation_result.output.resolve() != expected_output.resolve():
        raise OfflineAcceptanceError("Offline acceptance output identity changed")


def _raise_if_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise OfflineGenerationCancelled("Automatic audio acceptance was cancelled")


__all__ = [
    "OfflineAcceptanceError",
    "OfflineAcceptanceResult",
    "OfflineAcceptanceWorker",
]
