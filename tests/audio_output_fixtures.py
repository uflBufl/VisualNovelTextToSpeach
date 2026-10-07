"""Shared generated-audio playback output double."""

from unittest.mock import Mock

import numpy as np
from numpy.typing import ArrayLike, NDArray


class FakeAudioOutput:
    def __init__(self) -> None:
        self.plays: list[tuple[NDArray[np.generic], int, dict[str, object]]] = []
        self.stopped = False

    def query_devices(
        self, _device: object = None, _kind: object = None
    ) -> dict[str, int]:
        return {"default_samplerate": 24_000}

    def play(self, samples: ArrayLike, sample_rate: int, **options: object) -> None:
        self.plays.append((np.asarray(samples), sample_rate, options))

    def wait(self) -> Mock:
        return Mock(output_underflow=False)

    def stop(self) -> None:
        self.stopped = True
