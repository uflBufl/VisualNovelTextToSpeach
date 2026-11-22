"""Typed results shared by offline generation and pack publication."""

from dataclasses import dataclass
from pathlib import Path

# Schema v8 stores the failed source engine separately from the live destination.
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


TERMINAL_LINE_FAILURE_KINDS = frozenset(
    {
        "speech_silence",
        "missed_eos_audio_limit",
        "reference_unavailable",
        "backend_error",
    }
)


@dataclass(frozen=True)
class PlannedLiveFallback:
    queue_id: str
    action: str
    source_result_sha256: str


def is_terminal_line_failure(provider: object, kind: object) -> bool:
    return (
        isinstance(provider, str)
        and bool(provider.strip())
        and isinstance(kind, str)
        and kind in TERMINAL_LINE_FAILURE_KINDS
    )


def supports_generation_seed(provider: object) -> bool:
    """Pocket owns its sampling and does not accept a requested seed."""
    return provider != "pocket-tts"


def use_automatic_live_fallback(action: str, provider: object, kind: object) -> bool:
    if action == "bounded_seed_retry" and supports_generation_seed(provider):
        return False
    return (
        action in AUTOMATIC_RECOVERY_LIVE_FALLBACK_ACTIONS
        and is_terminal_line_failure(provider, kind)
    )


class OfflineGenerationError(RuntimeError):
    """The private generation input could not reach a terminal worker state."""


class OfflineGenerationCancelled(OfflineGenerationError):
    """The player cancelled the exact owned generation worker."""


@dataclass(frozen=True)
class OfflineGenerationResult:
    output: Path
    state: Path
    generated: int
    failed: int
    other_terminal: int
    pending_review: int = 0
    cancelled: int = 0

    @property
    def total(self) -> int:
        return self.generated + self.failed + self.other_terminal
