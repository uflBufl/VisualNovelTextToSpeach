"""Replay retained dialogue PCM through the application's shared audio output."""

from collections.abc import Sequence
from threading import Event, Lock

import numpy as np

from vntts.audio_output import (
    AudioOutput,
    match_output_sample_rate,
    prepared_output_stream,
    resolve_audio_output,
    sanitize_playback_audio,
    write_pcm_chunks,
)
from vntts.playback import ReplayAudio
from vntts.speech_backend_runtime import validate_volume


class HistoryAudioPlayer:
    def __init__(self, audio_output: AudioOutput | None = None) -> None:
        self.audio_output = audio_output
        self._playback_lock = Lock()

    def play(
        self,
        clips: Sequence[ReplayAudio],
        *,
        volume: float,
        playback_latency: object,
        cancelled: Event,
    ) -> bool:
        """Play a whole entry; cancellation belongs only to this invocation.

        The caller may use observe_playback_start for its first audible buffer.
        Output failures propagate so the UI can distinguish them from Stop.
        """
        if cancelled.is_set():
            return False
        volume = validate_volume(volume)
        if not clips:
            return False
        with self._playback_lock:
            if cancelled.is_set():
                return False
            try:
                output = resolve_audio_output(self.audio_output)
                for clip in clips:
                    if cancelled.is_set():
                        return False
                    samples = np.array(clip.pcm, dtype=np.float32, copy=True)
                    sanitize_playback_audio(samples, volume)
                    samples, sample_rate = match_output_sample_rate(
                        output, samples, clip.sample_rate
                    )
                    if cancelled.is_set():
                        return False
                    with prepared_output_stream(
                        output,
                        samplerate=sample_rate,
                        channels=1 if samples.ndim == 1 else samples.shape[1],
                        dtype="float32",
                        latency=playback_latency,
                    ) as stream:
                        completed, _underflowed = write_pcm_chunks(
                            stream,
                            samples,
                            sample_rate,
                            cancelled.is_set,
                            stop_requested=cancelled,
                        )
                        if not completed:
                            stream.abort()
                            return False
                return not cancelled.is_set()
            except Exception:
                if cancelled.is_set():
                    return False
                raise
