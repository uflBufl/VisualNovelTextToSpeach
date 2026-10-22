"""Typed preparation/playback helpers shared by live speech entry points."""

from collections.abc import Callable

from vntts.playback import PlaybackOutcome, PlaybackStatus, PreparedPlayback
from vntts.playback import TypedPlaybackBackend as TypedPlaybackBackend
from vntts.services.tts_engine import (
    AudioPlaybackError,
    TTSConfigurationError,
    TTSSynthesisError,
)


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
    raise_playback_failure(outcome)
    return outcome.successful


def raise_playback_failure(outcome: PlaybackOutcome) -> None:
    if outcome.status is PlaybackStatus.FAILED:
        message = outcome.error or "Audio playback failed"
        error_type = outcome.error_type
        if isinstance(error_type, type) and issubclass(
            error_type,
            (TTSConfigurationError, TTSSynthesisError, AudioPlaybackError),
        ):
            raise error_type(message)
        raise AudioPlaybackError(message)
