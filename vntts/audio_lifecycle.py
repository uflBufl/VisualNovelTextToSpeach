"""Playback identity shared with lazy audio-device diagnostics."""

from contextvars import ContextVar

audio_lifecycle_context = ContextVar("audio_lifecycle_context", default=None)


def record_audio_lifecycle(operation, **details):
    # Lazy import keeps the low-level audio module out of the support UI's import cycle.
    from vntts.support import record_audio_lifecycle as record

    record(operation, **details)
