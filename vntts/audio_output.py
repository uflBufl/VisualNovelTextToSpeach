"""Small shared helpers for lazy audio output and playback status."""

import numpy as np


def resolve_audio_output(audio_output):
    """Return an injected output module or lazily import sounddevice."""
    if audio_output is None:
        import sounddevice

        return sounddevice
    return audio_output


def playback_underflowed(audio_output, playback_status=None):
    """Read a reliable output-underflow flag without requiring a live stream."""
    value = getattr(playback_status, "output_underflow", None)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    get_stream = getattr(audio_output, "get_stream", None)
    if not callable(get_stream):
        return False
    try:
        value = get_stream().status.output_underflow
    except AttributeError, RuntimeError:
        return False
    return bool(value) if isinstance(value, (bool, np.bool_)) else False


__all__ = ["playback_underflowed", "resolve_audio_output"]
