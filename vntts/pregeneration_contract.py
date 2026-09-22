"""Typed results shared by offline generation and pack publication."""

from dataclasses import dataclass
from pathlib import Path


class OfflineGenerationError(RuntimeError):
    """The private generation input could not reach a terminal worker state."""


class OfflineGenerationCancelled(OfflineGenerationError):
    """The player cancelled the exact owned generation worker."""


@dataclass(frozen=True)
class OfflineGenerationResult:
    output: Path
    state: Path
    manifest: Path
    generated: int
    failed: int
    other_terminal: int
    pending_review: int = 0

    @property
    def total(self) -> int:
        return self.generated + self.failed + self.other_terminal
