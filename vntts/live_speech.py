"""Typed preparation/playback helpers shared by live speech entry points."""

from collections.abc import Callable
from typing import Protocol

from vntts.playback import PlaybackOutcome, PlaybackStatus, PreparedPlayback
from vntts.services.tts_engine import (
    AudioPlaybackError,
    TTSConfigurationError,
    TTSSynthesisError,
)


class TypedPlaybackBackend(Protocol):
    def prepare_playback(self, character: str, text: str) -> PreparedPlayback: ...

    def play_prepared(
        self,
        prepared: PreparedPlayback,
        *,
        playback_guard: Callable[[], bool] | None = None,
    ) -> PlaybackOutcome: ...


def play_typed_text(
    backend: TypedPlaybackBackend,
    character: str,
    text: str,
    playback_guard: Callable[[], bool] | None = None,
) -> bool:
    prepared = backend.prepare_playback(character, text)
    if not isinstance(prepared, PreparedPlayback):
        raise TypeError("Speech backend returned an untyped prepared playback")
    outcome = backend.play_prepared(
        prepared,
        playback_guard=playback_guard,
    )
    if not isinstance(outcome, PlaybackOutcome):
        raise TypeError("Speech backend returned an untyped playback outcome")
    if outcome.status is PlaybackStatus.FAILED:
        message = outcome.error or "Audio playback failed"
        error_type = outcome.error_type
        if isinstance(error_type, type) and issubclass(
            error_type,
            (TTSConfigurationError, TTSSynthesisError, AudioPlaybackError),
        ):
            raise error_type(message)
        raise AudioPlaybackError(message)
    return outcome.successful
