"""Rust-owned device output with bounded Python PCM ingress."""

from __future__ import annotations

import atexit
import queue
from collections.abc import Callable
from threading import Event, Lock, RLock, Thread
from time import monotonic
from typing import Any
from weakref import WeakSet

import numpy as np

from vntts.audio_lifecycle import record_audio_lifecycle
from vntts.rust_audio import RustAudioWorker


class RustAudioFormatError(RuntimeError):
    pass


class RustAudioModule:
    """Share device discovery and reuse idle workers, never Python device callbacks."""

    PortAudioError = RustAudioFormatError

    def __init__(self) -> None:
        self.lock = RLock()
        self.query_worker: RustAudioWorker | None = None
        self.idle: list[RustAudioWorker] = []
        self.streams: WeakSet[RustOutputStream] = WeakSet()
        self.convenience: Thread | None = None
        self.convenience_stop = Event()
        self.convenience_error: BaseException | None = None
        atexit.register(self._terminate)

    def _query(self, message: dict[str, Any], event: str) -> dict[str, Any]:
        with self.lock:
            if self.query_worker is not None and (
                self.query_worker.closed.is_set()
                or self.query_worker.device_changed.is_set()
            ):
                self.query_worker.close()
                self.query_worker = None
            if self.query_worker is None:
                self.query_worker = RustAudioWorker()
            self.query_worker.send(message)
            try:
                result = self.query_worker.receive(3)
            except queue.Empty:
                self.query_worker.close()
                operation = message["op"]
                details = (
                    f" ({message['sample_rate']} Hz, {message['channels']} channels)"
                    if operation == "check_format"
                    else ""
                )
                raise RuntimeError(
                    f"Rust audio {operation} timed out{details}"
                ) from None
            if result.get("event") != event:
                raise RuntimeError(
                    str(result.get("error", "Invalid Rust device response"))
                )
            return result

    def query_devices(self, *, kind: str) -> dict[str, Any]:
        if kind != "output":
            raise ValueError("Only output-device discovery is supported")
        return self._query({"op": "query_device"}, "device")

    def query_hostapis(self, index: int) -> dict[str, str]:
        device = self.query_devices(kind="output")
        if device["hostapi"] != index:
            raise RuntimeError("Default output host API changed")
        return {"name": str(device["host_api"])}

    def check_output_settings(
        self, *, samplerate: int, channels: int, dtype: str
    ) -> None:
        if dtype != "float32":
            raise RustAudioFormatError("Rust output requires float32", -9994)
        result = self._query(
            {"op": "check_format", "sample_rate": samplerate, "channels": channels},
            "format",
        )
        if not result.get("supported"):
            raise RustAudioFormatError(
                str(result.get("error", "Unsupported output format")),
                result.get("code"),
            )

    def acquire(self) -> RustAudioWorker:
        with self.lock:
            while self.idle:
                worker = self.idle.pop()
                if not worker.closed.is_set() and not worker.device_changed.is_set():
                    return worker
                worker.close()
        return RustAudioWorker()

    def release(self, worker: RustAudioWorker) -> None:
        with self.lock:
            if worker.closed.is_set() or worker.device_changed.is_set() or self.idle:
                worker.close()
            else:
                self.idle.append(worker)

    def OutputStream(self, **options: Any) -> RustOutputStream:
        if "callback" in options:
            raise TypeError(
                "Python audio callbacks were removed; use native clip control"
            )
        stream = RustOutputStream(self, **options)
        self.streams.add(stream)
        return stream

    def play(self, audio: object, sample_rate: int, *, latency: object) -> None:
        self.stop()
        cancelled = Event()
        self.convenience_stop = cancelled
        self.convenience_error = None

        def play() -> None:
            try:
                samples = np.asarray(audio, dtype=np.float32)
                channels = 1 if samples.ndim == 1 else samples.shape[1]
                if cancelled.is_set():
                    return
                with self.OutputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype="float32",
                    latency=latency,
                ) as stream:
                    stream.play_buffer(
                        samples.reshape(-1, channels), cancelled.is_set, None, cancelled
                    )
            except BaseException as error:
                if self.convenience_stop is cancelled:
                    self.convenience_error = error

        self.convenience = Thread(
            target=play, name="vntts-rust-convenience", daemon=True
        )
        self.convenience.start()

    def wait(self) -> None:
        if self.convenience is not None:
            self.convenience.join()
        if self.convenience_error is not None:
            raise RuntimeError(str(self.convenience_error)) from self.convenience_error

    def stop(self) -> None:
        self.convenience_stop.set()
        for stream in tuple(self.streams):
            stream.abort()

    def _terminate(self) -> None:
        self.stop()
        with self.lock:
            workers, self.idle = self.idle, []
            if self.query_worker is not None:
                workers.append(self.query_worker)
                self.query_worker = None
        for stream in tuple(self.streams):
            if stream.worker is not None:
                workers.append(stream.worker)
        for worker in workers:
            worker.close()

    def _initialize(self) -> None:
        self.query_devices(kind="output")


class RustOutputStream:
    """One native stream; write acknowledges bounded admission, stop drains EOS."""

    def __init__(
        self,
        module: RustAudioModule,
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        latency: object,
    ) -> None:
        if dtype != "float32" or channels not in (1, 2) or samplerate <= 0:
            raise ValueError("Rust output requires finite float32 mono/stereo PCM")
        self.module = module
        self.sample_rate, self.channels, self.latency = samplerate, channels, latency
        self.capacity = samplerate * 2
        self.worker: RustAudioWorker | None = None
        self.clip_id: int | None = None
        self.started = self.opened = self.first_callback = self.closed = False
        self.finished = Event()
        self.aborted = Event()
        self.write_lock = Lock()
        self.underflow_count = self.producer_starvation_count = 0
        self.admitted_frames = self.sent_frames = 0
        self.summary: dict[str, Any] | None = None

    def start(self) -> None:
        if self.closed or self.aborted.is_set():
            raise RuntimeError("Rust output stream is closed or interrupted")
        self.started = True

    def __enter__(self) -> RustOutputStream:
        self.start()
        return self

    def __exit__(self, error: object, *_args: object) -> None:
        try:
            if error is None:
                self.stop()
            else:
                self.abort()
        finally:
            self.close()

    def _worker(self) -> RustAudioWorker:
        if self.worker is None:
            self.worker = self.module.acquire()
        return self.worker

    def _open(self) -> None:
        if self.opened:
            return
        worker = self._worker()
        worker.next_id += 1
        self.clip_id = worker.next_id
        worker.active_id = self.clip_id
        if not worker._arm_recording(self.aborted.is_set) or self.aborted.is_set():
            worker.active_id = None
            raise RuntimeError("Rust output stream is interrupted")
        worker.send(
            {
                "op": "open_stream",
                "id": self.clip_id,
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "latency": self.latency,
                "capacity_frames": self.capacity,
            }
        )
        self._until("opened", 5)
        self.opened = True

    def _event(self, timeout: float) -> dict[str, Any]:
        worker = self._worker()
        event = worker.receive(timeout)
        if event.get("id") != self.clip_id:
            raise RuntimeError("Rust stream returned another owner's event")
        if event["event"] == "error":
            raise RuntimeError(str(event.get("error", "Rust stream failed")))
        for key in ("underflow_count", "producer_starvation_count"):
            if key in event:
                value = event[key]
                if type(value) is not int or value < 0:
                    raise RuntimeError("Rust stream returned invalid counters")
                setattr(self, key, max(getattr(self, key), value))
        if event["event"] == "first_callback":
            self.first_callback = int(event.get("frames_written", 0)) > 0
        if event["event"] == "finished":
            self._validate_finished(event)
            self.summary = event
            self.finished.set()
            worker.active_id = None
            record_audio_lifecycle(
                "rust-stream-summary",
                reason="bounded-native-ingress",
                worker_pid=worker.process.pid,
                **{k: v for k, v in event.items() if k != "event"},
            )
            if event.get("outcome") == "failed":
                raise RuntimeError(str(event.get("error", "Rust output failed")))
        return event

    def _validate_finished(self, event: dict[str, Any]) -> None:
        if event.get("outcome") not in {"complete", "interrupted", "failed"}:
            raise RuntimeError("Rust stream returned an invalid outcome")
        for name in (
            "frames_played",
            "total_frames",
            "callback_count",
            "underflow_count",
            "producer_starvation_count",
        ):
            if type(event.get(name)) is not int or event[name] < 0:
                raise RuntimeError(f"Rust stream returned invalid {name}")
        if (event.get("sample_rate"), event.get("channels")) != (
            self.sample_rate,
            self.channels,
        ):
            raise RuntimeError("Rust stream returned a different output format")
        if not 0 <= event["frames_played"] <= event["total_frames"] <= self.sent_frames:
            raise RuntimeError("Rust stream returned invalid frame counts")
        if (
            event["outcome"] == "complete"
            and event["frames_played"] != self.admitted_frames
        ):
            raise RuntimeError(
                "Rust stream completed without playing all admitted frames"
            )

    def _until(self, kind: str, timeout: float) -> dict[str, Any]:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            try:
                event = self._event(min(0.05, max(0.001, deadline - monotonic())))
            except queue.Empty:
                continue
            if event["event"] == kind:
                return event
            if event["event"] == "finished":
                raise RuntimeError("Rust audio was interrupted before admission")
        self._worker().close()
        raise RuntimeError(f"Rust stream {kind} timed out")

    def write(self, audio: object) -> bool:
        samples = np.ascontiguousarray(audio, dtype="<f4")
        if samples.ndim == 1:
            samples = samples.reshape(-1, 1)
        if (
            samples.ndim != 2
            or samples.shape[1] != self.channels
            or not np.isfinite(samples).all()
        ):
            raise ValueError("Invalid Rust stream PCM layout or samples")
        if not len(samples):
            return False
        with self.write_lock:
            if not self.started or self.closed or self.aborted.is_set():
                raise RuntimeError("Rust output stream is interrupted")
            self._open()
            worker = self._worker()
            before = self.underflow_count
            for offset in range(0, len(samples), self.capacity):
                if self.aborted.is_set() or worker.device_changed.is_set():
                    raise RuntimeError("Rust stream interrupted or device changed")
                chunk = samples[offset : offset + self.capacity]
                self.sent_frames += len(chunk)
                worker._send_bounded(
                    {"op": "append", "id": self.clip_id, "frames": len(chunk)},
                    chunk.tobytes(),
                    self.aborted,
                )
                ack = self._until("appended", len(chunk) / self.sample_rate + 5)
                if ack.get("frames") != len(chunk):
                    raise RuntimeError("Rust stream did not admit the complete chunk")
                self.admitted_frames += len(chunk)
                if not self.first_callback:
                    self._until("first_callback", 5)
            return self.underflow_count > before

    @property
    def underflowed(self) -> bool:
        return self.underflow_count > 0

    def play_buffer(
        self,
        samples: np.ndarray,
        cancelled: Callable[[], bool],
        first_write: Callable[[], None] | None,
        stop_requested: Event | None,
    ) -> tuple[bool, bool]:
        if self.opened:
            raise RuntimeError("Cannot replace an active PCM stream with a clip")
        worker = self._worker()
        try:
            return worker.play_buffer(
                samples,
                self.sample_rate,
                self.channels,
                self.latency,
                lambda: self.aborted.is_set() or cancelled(),
                first_write,
                stop_requested,
            )
        finally:
            self.finished.set()

    def abort(self) -> None:
        if self.aborted.is_set():
            return
        self.aborted.set()
        worker = self.worker
        if worker is not None:
            worker.cancel_active()

    def stop(self) -> None:
        if not self.opened or self.finished.is_set():
            return
        if self.aborted.is_set():
            return
        worker = self._worker()
        worker.send({"op": "end", "id": self.clip_id})
        self._until("ended", 3)
        self._until("finished", self.capacity / self.sample_rate + 5)
        if self.summary is None or self.summary.get("outcome") != "complete":
            raise RuntimeError("Rust PCM stream failed to drain")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        worker = self.worker
        if worker is not None:
            if worker.active_id is not None or (
                self.opened and not self.finished.is_set()
            ):
                self.abort()
                worker.close()
            self.module.release(worker)
        self.module.streams.discard(self)
