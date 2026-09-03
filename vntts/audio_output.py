"""Small shared helpers for lazy audio output and playback status."""

from threading import Event

import numpy as np
from scipy.signal import resample_poly

from vntts.playback import PlaybackStatus, PreparedPlayback, outcome_for_prepared


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


def match_output_sample_rate(audio_output, audio, source_sample_rate):
    """Resample once in Python instead of relying on a live device converter."""
    query_devices = getattr(audio_output, "query_devices", None)
    if not callable(query_devices):
        return audio, source_sample_rate
    try:
        device = query_devices(kind="output")
        target_sample_rate = int(round(float(device["default_samplerate"])))
    except KeyError, TypeError, ValueError, RuntimeError:
        return audio, source_sample_rate
    if target_sample_rate <= 0 or target_sample_rate == source_sample_rate:
        return audio, source_sample_rate

    divisor = np.gcd(source_sample_rate, target_sample_rate)
    resampled = resample_poly(
        np.asarray(audio, dtype=np.float32),
        target_sample_rate // divisor,
        source_sample_rate // divisor,
        axis=0,
    ).astype(np.float32, copy=False)
    peak = float(np.max(np.abs(resampled))) if resampled.size else 0.0
    if peak > 0.95:
        resampled *= 0.95 / peak
    return resampled, target_sample_rate


class SynchronousPcmPlaybackMixin:
    """Shared locking, cancellation and metrics for blocking PCM output."""

    playback_configuration_error = ValueError
    invalid_playback_message = "Playback received an invalid payload"

    def play_prepared(self, prepared, *, playback_guard=None):
        if not isinstance(prepared, PreparedPlayback):
            raise self.playback_configuration_error(self.invalid_playback_message)
        if playback_guard is not None and not playback_guard():
            return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
            stop_requested = Event()
            started = self.clock()
            underflowed = False
            first_audio_ms = None
            try:
                with self.playback_state_lock:
                    self.playback_active = True
                    self.active_playback_stop = stop_requested
                audio_output = self._resolve_audio_output()
                audio, playback_sample_rate = match_output_sample_rate(
                    audio_output,
                    self._prepare_audio(prepared.payload),
                    self.sample_rate,
                )
                with self.playback_state_lock:
                    interrupted = stop_requested.is_set() or (
                        playback_guard is not None and not playback_guard()
                    )
                    if not interrupted:
                        audio_output.play(
                            audio,
                            playback_sample_rate,
                            latency=self.playback_latency,
                        )
                        first_audio_ms = (self.clock() - started) * 1000
                if not interrupted:
                    playback_status = audio_output.wait()
                    underflowed = self._playback_underflowed(playback_status)
                    interrupted = stop_requested.is_set() or (
                        playback_guard is not None and not playback_guard()
                    )
            except Exception as error:
                if stop_requested.is_set():
                    return outcome_for_prepared(
                        prepared,
                        PlaybackStatus.INTERRUPTED,
                        (self.clock() - started) * 1000,
                        first_audio_ms=first_audio_ms,
                    )
                return outcome_for_prepared(
                    prepared,
                    PlaybackStatus.FAILED,
                    (self.clock() - started) * 1000,
                    error=str(error),
                    error_type=type(error),
                )
            finally:
                with self.playback_state_lock:
                    self.playback_active = False
                    if self.active_playback_stop is stop_requested:
                        self.active_playback_stop = None
        return outcome_for_prepared(
            prepared,
            PlaybackStatus.INTERRUPTED if interrupted else PlaybackStatus.COMPLETED,
            (self.clock() - started) * 1000,
            underflowed=underflowed,
            first_audio_ms=first_audio_ms,
        )

    def stop(self):
        with self.playback_state_lock:
            was_playing = self.playback_active
            stop_requested = self.active_playback_stop
            if was_playing and stop_requested is not None:
                stop_requested.set()
            if was_playing and self.audio_output is not None:
                self.audio_output.stop()
        return was_playing

    def _resolve_audio_output(self):
        self.audio_output = resolve_audio_output(self.audio_output)
        return self.audio_output

    def _playback_underflowed(self, playback_status=None):
        return playback_underflowed(self.audio_output, playback_status)


__all__ = [
    "SynchronousPcmPlaybackMixin",
    "match_output_sample_rate",
    "playback_underflowed",
    "resolve_audio_output",
]
