"""Automatic acceptance of technically validated self-service WAVs."""

from __future__ import annotations

from dataclasses import dataclass, replace

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
        validate_offline_generation_result(
            generation_input,
            generation_result,
            "acceptance",
            error_type=OfflineAcceptanceError,
        )
        _raise_if_cancelled(cancel_event)
        if generation_result.pending_review == 0:
            return OfflineAcceptanceResult(generation_result, 0)
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
            return OfflineAcceptanceResult(generation_result, 0)
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
            replace(generation_result, pending_review=0),
            len(pending),
        )


def _raise_if_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise OfflineGenerationCancelled("Automatic audio acceptance was cancelled")


__all__ = [
    "OfflineAcceptanceError",
    "OfflineAcceptanceResult",
    "OfflineAcceptanceWorker",
]
