"""Display-only outcomes for optional prepared story order."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vntts.json_types import decode_json
from vntts.path_safety import read_regular_bytes


class SequenceUnavailableReason(StrEnum):
    MANUAL_BOUNDARY = "manual-boundary"
    NONLINEAR = "nonlinear"
    SOURCE_UNAVAILABLE = "source-unavailable"
    UNSUPPORTED_SOURCE = "unsupported-source"
    PUBLISHER_UNAVAILABLE = "publisher-unavailable"
    PREPARATION_FAILED = "preparation-failed"
    INVALID_PLAN = "invalid-plan"
    INCOMPLETE_COVERAGE = "incomplete-coverage"
    INCOMPATIBLE_SOURCE = "incompatible-source"
    OVERLAPPING_CHAPTERS = "overlapping-chapters"
    NOT_READY = "not-ready-at-handoff"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PreparedSequenceResult:
    plan: Path | None = None
    reason: SequenceUnavailableReason | None = None


_EXPLANATIONS = {
    SequenceUnavailableReason.MANUAL_BOUNDARY: (
        "This selection contains a story event whose automatic continuation "
        "is not supported."
    ),
    SequenceUnavailableReason.NONLINEAR: "This selection has no single supported story order.",
    SequenceUnavailableReason.SOURCE_UNAVAILABLE: "The story source needed to prepare its order is unavailable.",
    SequenceUnavailableReason.UNSUPPORTED_SOURCE: "Story-order preparation is not supported for this story source.",
    SequenceUnavailableReason.PUBLISHER_UNAVAILABLE: "The story-order preparation tool is unavailable.",
    SequenceUnavailableReason.PREPARATION_FAILED: "The story order could not be prepared.",
    SequenceUnavailableReason.INVALID_PLAN: "The prepared story order is invalid for this selection.",
    SequenceUnavailableReason.INCOMPLETE_COVERAGE: "The saved story order does not cover the whole selection.",
    SequenceUnavailableReason.INCOMPATIBLE_SOURCE: "The story-order data does not match the selected game data.",
    SequenceUnavailableReason.OVERLAPPING_CHAPTERS: "The story orders contain overlapping chapters.",
    SequenceUnavailableReason.NOT_READY: "The story order was not ready when this reading setup was created.",
    SequenceUnavailableReason.CANCELLED: "Story-order preparation was cancelled.",
}


def sequence_unavailable_notice(reason: SequenceUnavailableReason | None) -> str:
    if reason is None:
        return ""
    return (
        f"Reading matches lines from the screen. {_EXPLANATIONS[reason]} "
        "Prepared audio remains available."
    )


def reason_from_extension(extension: object) -> SequenceUnavailableReason | None:
    if not isinstance(extension, dict):
        return None
    value = extension.get("sequence_unavailable_reason")
    if not isinstance(value, str):
        return None
    try:
        return SequenceUnavailableReason(value)
    except ValueError:
        return None


def load_sequence_unavailable_reason(
    game_pack: str | Path | None,
) -> SequenceUnavailableReason | None:
    if not game_pack:
        return None
    try:
        document = decode_json(read_regular_bytes(Path(game_pack).expanduser()))
    except OSError, ValueError:
        return None
    return (
        reason_from_extension(document.get("vntts.self-service"))
        if isinstance(document, dict)
        else None
    )
