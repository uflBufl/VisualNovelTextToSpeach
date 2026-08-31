"""Typed preparation/playback helpers shared by live speech entry points."""

from vntts.playback import PlaybackOutcome, PlaybackStatus
from vntts.services.tts_engine import AudioPlaybackError


def play_typed_text(backend, character, text, playback_guard=None):
    prepared = backend.prepare_playback(character, text)
    outcome = backend.play_prepared(
        prepared,
        **({"playback_guard": playback_guard} if playback_guard else {}),
    )
    if isinstance(outcome, PlaybackOutcome):
        if outcome.status is PlaybackStatus.FAILED:
            raise AudioPlaybackError(outcome.error or "Audio playback failed")
        return outcome.successful
    return bool(outcome)
