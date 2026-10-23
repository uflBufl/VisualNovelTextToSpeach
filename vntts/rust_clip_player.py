"""Control a dedicated native clip player without a Python audio callback."""

from __future__ import annotations

from collections.abc import Callable
from math import isfinite
from queue import Empty
from threading import RLock
from time import monotonic
from typing import Any, TypedDict, cast

import numpy as np
from numpy.typing import NDArray

from vntts.audio_lifecycle import audio_lifecycle_context, record_audio_lifecycle
from vntts.cleanup import attempt_cleanup
from vntts.rust_audio import MAX_PCM_BYTES, RustAudioWorker


class ClipStatus(TypedDict):
    position_frames: int
    audible_position_frames: int
    total_frames: int
    started: bool
    playing: bool
    paused: bool
    finished: bool
    underflow_count: int
    producer_starvation_count: int
    error: str | None


class RustClipController:
    """Serialize control IPC; PCM delivery and its timeline belong to Rust."""

    def __init__(self, worker_factory: Callable[[], RustAudioWorker] = RustAudioWorker):
        self._worker_factory = worker_factory
        self._worker: RustAudioWorker | None = None
        self._lock = RLock()
        self._clip_id: int | None = None
        self._last_id = 0
        self._terminal = False
        self._frames = self._sample_rate = self._channels = 0
        self._context: dict[str, object] = {}

    @property
    def device_changed(self) -> bool:
        return self._worker is not None and self._worker.device_changed.is_set()

    def _get_worker(self) -> RustAudioWorker:
        if self._worker is None:
            self._worker = self._worker_factory()
        return self._worker

    def _event_kind(self, event: dict[str, Any]) -> str:
        kind = event.get("event")
        if not isinstance(kind, str):
            raise RuntimeError("Rust clip returned an invalid event")
        if kind == "error":
            raise RuntimeError(str(event.get("error", "Rust clip playback failed")))
        if kind == "device":
            return kind
        if event.get("id") != self._clip_id:
            raise RuntimeError("Rust clip returned an event for another clip")
        if kind not in {
            "loaded",
            "started",
            "first_callback",
            "paused",
            "resumed",
            "seeked",
            "finished",
            "status",
        }:
            raise RuntimeError(f"Unknown Rust clip event: {kind}")
        if kind == "finished":
            self._record_finished(event)
            self._terminal = True
            self._get_worker().active_id = None
        return kind

    def _record_finished(self, event: dict[str, Any]) -> None:
        self._validate_status(event)
        if event.get("outcome") not in {"complete", "interrupted", "failed"}:
            raise RuntimeError("Rust clip returned an invalid outcome")
        if type(event.get("callback_count")) is not int or event["callback_count"] < 0:
            raise RuntimeError("Rust clip returned an invalid callback count")
        if (
            type(event.get("frames_played")) is not int
            or event["frames_played"] != event["position_frames"]
        ):
            raise RuntimeError("Rust clip returned an invalid played frame count")
        for name in ("elapsed_ms", "actual_sample_rate", "output_latency_seconds"):
            value = event.get(name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(value)
                or value < 0
            ):
                raise RuntimeError(f"Rust clip returned invalid {name}")
        if not self._terminal:
            record_audio_lifecycle(
                "rust-pcm-summary",
                **self._context,
                **{
                    key: event[key]
                    for key in (
                        "id",
                        "outcome",
                        "frames_played",
                        "position_frames",
                        "audible_position_frames",
                        "total_frames",
                        "underflow_count",
                        "producer_starvation_count",
                        "callback_count",
                        "sample_rate",
                        "channels",
                        "actual_sample_rate",
                        "output_latency_seconds",
                        "elapsed_ms",
                        "error",
                    )
                },
                reason="isolated-authoring-pcm",
                worker_pid=self._get_worker().process.pid,
            )

    def _receive(self, expected: str) -> dict[str, Any]:
        worker = self._get_worker()
        deadline = monotonic() + 10
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise RuntimeError(f"Rust clip {expected} timed out")
            try:
                event = worker.receive(remaining)
            except Empty:
                raise RuntimeError(f"Rust clip {expected} timed out") from None
            kind = self._event_kind(event)
            if kind == expected:
                return event
            if kind == "finished" and expected in {"loaded", "started"}:
                raise RuntimeError(
                    str(event.get("error", "Rust clip ended before starting"))
                )

    def _request(self, op: str, expected: str, **fields: object) -> dict[str, Any]:
        try:
            self._get_worker().send({"op": op, **fields})
            return self._receive(expected)
        except BaseException as error:
            attempt_cleanup(
                self.close, description="Native clip cleanup", primary_error=error
            )
            raise

    def query_device(self) -> dict[str, Any]:
        with self._lock:
            return self._request("query_device", "device")

    def load(
        self,
        samples: NDArray[np.float32],
        sample_rate: int,
        channels: int,
        latency: float,
    ) -> None:
        pcm = np.ascontiguousarray(samples, dtype="<f4")
        if (
            pcm.ndim != 2
            or pcm.shape[1] != channels
            or channels not in (1, 2)
            or not len(pcm)
            or pcm.nbytes > MAX_PCM_BYTES
            or not np.isfinite(pcm).all()
            or not 8000 <= sample_rate <= 384000
        ):
            raise ValueError(
                "Rust clip playback requires bounded finite mono/stereo float32 PCM"
            )
        with self._lock:
            self.stop()
            self._last_id += 1
            self._clip_id = self._last_id
            self._terminal = False
            self._context = dict(audio_lifecycle_context.get() or {})
            self._frames, self._sample_rate, self._channels = (
                len(pcm),
                sample_rate,
                channels,
            )
            try:
                self._get_worker().active_id = self._clip_id
                self._get_worker()._send_bounded(
                    {
                        "op": "load",
                        "id": self._clip_id,
                        "sample_rate": sample_rate,
                        "channels": channels,
                        "frames": len(pcm),
                        "latency": latency,
                    },
                    pcm.tobytes(),
                    None,
                )
                self._receive("loaded")
            except BaseException as error:
                attempt_cleanup(
                    self.close, description="Native clip cleanup", primary_error=error
                )
                raise

    def _control(self, op: str, expected: str, **fields: object) -> bool:
        with self._lock:
            if self._clip_id is None or self._terminal:
                return False
            self._request(op, expected, id=self._clip_id, **fields)
            return not self._terminal

    def start(self) -> None:
        self._control("start", "started")

    def pause(self) -> None:
        self._control("pause", "paused")

    def resume(self) -> bool:
        return self._control("resume", "resumed")

    def seek(self, position_frames: int) -> bool:
        return self._control("seek", "seeked", position_frames=position_frames)

    def status(self) -> ClipStatus:
        with self._lock:
            if self._clip_id is None:
                raise RuntimeError("Rust clip has not been loaded")
            value = self._request("status", "status", id=self._clip_id)
            try:
                return self._validate_status(value)
            except BaseException as error:
                attempt_cleanup(
                    self.close, description="Native clip cleanup", primary_error=error
                )
                raise

    def _validate_status(self, value: dict[str, Any]) -> ClipStatus:
        for name in (
            "position_frames",
            "audible_position_frames",
            "total_frames",
            "underflow_count",
            "producer_starvation_count",
        ):
            if type(value.get(name)) is not int or value[name] < 0:
                raise RuntimeError(f"Rust clip returned invalid {name}")
        if (
            not 0
            <= value["audible_position_frames"]
            <= value["position_frames"]
            <= value["total_frames"]
            == self._frames
        ):
            raise RuntimeError("Rust clip returned an invalid frame position")
        if (value.get("sample_rate"), value.get("channels")) != (
            self._sample_rate,
            self._channels,
        ):
            raise RuntimeError("Rust clip returned a different PCM format")
        for name in ("started", "playing", "paused", "finished"):
            if type(value.get(name)) is not bool:
                raise RuntimeError(f"Rust clip returned invalid {name}")
        if "error" not in value or (
            value["error"] is not None and not isinstance(value["error"], str)
        ):
            raise RuntimeError("Rust clip returned an invalid error")
        return cast(ClipStatus, value)

    def stop(self) -> None:
        self._control("stop", "finished")

    def reset(self) -> None:
        with self._lock:
            self.close()
            self._terminal = False
            self._clip_id = None

    def close(self) -> None:
        with self._lock:
            worker, self._worker = self._worker, None
            self._clip_id = None
            if worker is not None:
                worker.active_id = None
                worker.close()
