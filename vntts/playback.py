"""Typed preparation and playback results shared by live speech backends."""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class PlaybackStatus(str, Enum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    PASSTHROUGH_UNOBSERVED = "passthrough-unobserved"


@dataclass(frozen=True)
class PreparedPlayback:
    payload: Any
    synthesis_ms: float | None
    first_audio_ms: float | None
    cache_source: str | None
    audio_source: str
    generation_completed: bool = True


@dataclass(frozen=True)
class PlaybackOutcome:
    status: PlaybackStatus
    playback_ms: float | None
    underflowed: bool = False
    generation_limited: bool = False
    first_audio_ms: float | None = None
    error: str | None = None
    error_type: type[Exception] | None = None
    synthesis_ms: float | None = None
    cache_source: str | None = None
    audio_source: str | None = None

    @property
    def successful(self):
        return self.status in {
            PlaybackStatus.COMPLETED,
            PlaybackStatus.PASSTHROUGH_UNOBSERVED,
        }


def outcome_for_prepared(
    prepared,
    status,
    playback_ms,
    *,
    underflowed=False,
    generation_limited=False,
    first_audio_ms=None,
    error=None,
    error_type=None,
):
    return PlaybackOutcome(
        status=status,
        playback_ms=playback_ms,
        underflowed=underflowed,
        generation_limited=generation_limited,
        first_audio_ms=first_audio_ms,
        error=error,
        error_type=error_type,
        synthesis_ms=prepared.synthesis_ms,
        cache_source=prepared.cache_source,
        audio_source=prepared.audio_source,
    )
