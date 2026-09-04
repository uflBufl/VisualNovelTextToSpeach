"""Typed preparation and playback results shared by live speech backends."""

from dataclasses import dataclass
from enum import Enum
from typing import Any

from vntts.synthesis import SynthesisRequest


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
    source_sample_rate: int | None = None
    playback_sample_rate: int | None = None
    sample_count: int | None = None
    expected_playback_ms: float | None = None

    @property
    def successful(self) -> bool:
        return self.status in {
            PlaybackStatus.COMPLETED,
            PlaybackStatus.PASSTHROUGH_UNOBSERVED,
        }


def collect_synthesis(backend: Any, character: str, text: str) -> Any:
    return backend.render(
        SynthesisRequest(
            voice=character,
            text=text,
            generation_profile=backend.generation_profile,
        )
    ).collect()


def prepared_playback_from_render(
    backend: Any, character: str, text: str
) -> PreparedPlayback:
    rendered = collect_synthesis(backend, character, text)
    return PreparedPlayback(
        rendered.pcm.reshape(-1),
        rendered.timing.first_chunk_ms,
        None,
        rendered.diagnostics.cache_source,
        f"live:{backend.name}",
    )


def synthesized_mono_pcm(backend: Any, character: str, text: str) -> Any:
    return collect_synthesis(backend, character, text).pcm.reshape(-1)


def prepare_playback_payload(backend: Any, character: str, text: str) -> Any:
    prepared = backend.prepare_playback(character, text)
    backend.last_synthesis_ms = prepared.synthesis_ms
    backend.last_first_audio_ms = prepared.first_audio_ms
    return prepared.payload


def outcome_for_prepared(
    prepared: PreparedPlayback,
    status: PlaybackStatus,
    playback_ms: float | None,
    *,
    underflowed: bool = False,
    generation_limited: bool = False,
    first_audio_ms: float | None = None,
    error: str | None = None,
    error_type: type[Exception] | None = None,
) -> PlaybackOutcome:
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
