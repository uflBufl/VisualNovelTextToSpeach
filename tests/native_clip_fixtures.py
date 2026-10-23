"""Native clip-controller double with separate submission and DAC progress."""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

from vntts.rust_clip_player import ClipStatus


class FakeClipController:
    def __init__(self, *, sample_rate: int = 48_000, channels: int = 2) -> None:
        self.device: dict[str, Any] = {
            "name": "Native test output",
            "default_samplerate": sample_rate,
            "max_output_channels": channels,
        }
        self.device_changed = False
        self.closed = False
        self.load_count = self.reset_count = self.stop_count = 0
        self.samples = np.empty((0, channels), np.float32)
        self.sample_rate = sample_rate
        self.channels = channels
        self.time = 0.0
        self.latency = 0.1
        self._first_dac: float | None = None
        self._audible_origin = 0
        self._running = False
        self.state: ClipStatus = {
            "position_frames": 0,
            "audible_position_frames": 0,
            "total_frames": 0,
            "started": False,
            "playing": False,
            "paused": False,
            "finished": False,
            "underflow_count": 0,
            "producer_starvation_count": 0,
            "error": None,
        }

    def query_device(self) -> dict[str, Any]:
        return self.device

    def load(
        self,
        samples: NDArray[np.float32],
        sample_rate: int,
        channels: int,
        latency: float,
    ) -> None:
        self.samples = samples.copy()
        self.sample_rate, self.channels, self.latency = sample_rate, channels, latency
        self.load_count += 1
        self._first_dac = None
        self._audible_origin = 0
        self._running = False
        self.state = {
            "position_frames": 0,
            "audible_position_frames": 0,
            "total_frames": len(samples),
            "started": False,
            "playing": False,
            "paused": False,
            "finished": False,
            "underflow_count": 0,
            "producer_starvation_count": 0,
            "error": None,
        }

    def start(self) -> None:
        self._running = True
        self.state["playing"] = True

    def pause(self) -> None:
        self.status()
        self._running = False
        self.state["paused"] = True
        self.state["playing"] = False
        self._first_dac = None

    def resume(self) -> bool:
        self.state["started"] = self.state["finished"] = self.state["paused"] = False
        self.start()
        return True

    def seek(self, position_frames: int) -> bool:
        self.state["position_frames"] = self.state["audible_position_frames"] = (
            position_frames
        )
        self.state["started"] = self.state["finished"] = False
        self._first_dac = None
        self._audible_origin = position_frames
        return True

    def pump(self, frames: int, *, underflow: bool = False) -> NDArray[np.float32]:
        output = np.zeros((frames, self.channels), np.float32)
        if self._running:
            position = self.state["position_frames"]
            count = min(frames, len(self.samples) - position)
            output[:count] = self.samples[position : position + count]
            if count:
                if self._first_dac is None:
                    self._first_dac = self.time + self.latency
                    self._audible_origin = position
                self.state["position_frames"] += count
                self.state["started"] = True
            self.state["underflow_count"] += int(underflow)
        self.time += frames / self.sample_rate
        return output

    def status(self) -> ClipStatus:
        if self._first_dac is not None:
            self.state["audible_position_frames"] = min(
                self.state["position_frames"],
                self._audible_origin
                + round(max(0, self.time - self._first_dac) * self.sample_rate),
            )
            if self.state["audible_position_frames"] == len(self.samples):
                self.state["playing"] = self._running = False
                self.state["finished"] = True
        return self.state.copy()

    def stop(self) -> None:
        self.stop_count += 1
        self._running = False
        self.state["playing"] = False
        self.state["finished"] = True

    def reset(self) -> None:
        self.reset_count += 1
        self.device_changed = self._running = False

    def close(self) -> None:
        self.closed = True
        self._running = False
