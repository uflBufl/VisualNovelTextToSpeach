"""Shared generated-audio playback output double."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
from numpy.typing import ArrayLike, NDArray

from vntts.authoring.pcm_playback import _CallbackTimeInfo, _OutputDevice


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


class FakeStatus:
    def __init__(self, *, output_underflow: bool = False) -> None:
        self.output_underflow = output_underflow


class FakeOutputStream:
    def __init__(self, module: FakeAudioModule, **options: object) -> None:
        self.module = module
        self.options = options
        callback = options["callback"]
        samplerate = options["samplerate"]
        channels = options["channels"]
        assert callable(callback)
        assert isinstance(samplerate, (int, float))
        assert isinstance(channels, int)
        self.callback: Callable[
            [NDArray[np.float32], int, _CallbackTimeInfo, object], None
        ] = callback
        self.samplerate = samplerate
        self.channels = channels
        self.time = 0.0
        self.started = False
        self.aborted = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True

    def pump(self, frames: int, *, underflow: bool = False) -> NDArray[np.float32]:
        output = np.empty((frames, self.channels), dtype=np.float32)
        timing = SimpleNamespace(
            outputBufferDacTime=self.time + self.module.latency,
            currentTime=self.time,
        )
        self.callback(
            output,
            frames,
            timing,
            FakeStatus(output_underflow=underflow),
        )
        self.time += frames / self.samplerate
        return output


class FakeAudioModule:
    latency = 0.1

    def __init__(self, *, sample_rate: int = 48_000, channels: int = 2) -> None:
        self.device: _OutputDevice = {
            "default_samplerate": sample_rate,
            "max_output_channels": channels,
        }
        self.stream: FakeOutputStream | None = None

    def query_devices(self, *, kind: str) -> _OutputDevice:
        if kind != "output":
            raise AssertionError(kind)
        return self.device

    def OutputStream(self, **options: object) -> FakeOutputStream:
        self.stream = FakeOutputStream(self, **options)
        return self.stream
