"""Shared PCM WAV publication helpers."""

import wave
from pathlib import Path

import numpy as np

from vntts.atomic_io import atomic_output_path


def write_pcm16_wav(path, samples, sample_rate):
    destination = Path(path)
    values = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm = np.round(values * 32767.0).astype("<i2")
    with atomic_output_path(destination) as temporary:
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(int(sample_rate))
            output.writeframes(pcm.tobytes())
    return destination
