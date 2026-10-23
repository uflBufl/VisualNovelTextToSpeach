"""Experimental full-buffer playback through a locally built Rust process."""

from __future__ import annotations

import atexit
import ctypes.util
import importlib.util
import json
import os
import queue
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, RLock, Thread, current_thread
from time import monotonic
from typing import Any

import numpy as np

from vntts.audio_lifecycle import record_audio_lifecycle

MAX_PCM_BYTES = 128 * 1024 * 1024
MAX_EVENT_BYTES = 65536


def portaudio_library() -> str:
    """Resolve the installed public library, never sounddevice's CFFI internals."""
    explicit = os.environ.get("VNTTS_PORTAUDIO_LIBRARY")
    if explicit:
        return explicit
    found = ctypes.util.find_library("portaudio")
    if found:
        return found
    spec = importlib.util.find_spec("_sounddevice_data")
    if spec is not None and spec.submodule_search_locations:
        name = (
            "libportaudio.dylib"
            if sys.platform == "darwin"
            else f"libportaudio{struct.calcsize('P') * 8}bit.dll"
        )
        for location in spec.submodule_search_locations:
            candidates = list((Path(location) / "portaudio-binaries").rglob(name))
            if len(candidates) == 1:
                return str(candidates[0])
    raise RuntimeError("PortAudio not found; set VNTTS_PORTAUDIO_LIBRARY explicitly")


def worker_executable() -> Path:
    explicit = os.environ.get("VNTTS_AUDIO_WORKER")
    path = (
        Path(explicit).expanduser()
        if explicit
        else Path(__file__).resolve().parents[1]
        / "native/audio-worker/target/release"
        / (
            "vntts-audio-worker.exe"
            if sys.platform == "win32"
            else "vntts-audio-worker"
        )
    )
    if not path.is_file():
        raise RuntimeError(
            "Rust audio worker is missing. Run cargo build --release --locked "
            "--manifest-path native/audio-worker/Cargo.toml, or set VNTTS_AUDIO_WORKER"
        )
    return path


class RustAudioWorker:
    """One serialized clip owner; protocol reads never run on an audio callback."""

    def __init__(self, command: list[str] | None = None) -> None:
        self.lock = RLock()
        self.send_lock = Lock()
        self.events: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(64)
        self.device_changed = Event()
        self.closed = Event()
        self.next_id = 0
        self.active_id: int | None = None
        self.stderr = tempfile.TemporaryFile()
        started = monotonic()
        try:
            self.process = subprocess.Popen(
                command
                or [str(worker_executable()), "--portaudio", portaudio_library()],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr,
                bufsize=0,
            )
        except BaseException:
            self.stderr.close()
            raise
        self.reader = Thread(
            target=self._read_events, name="vntts-rust-events", daemon=True
        )
        self.reader.start()
        try:
            ready = self.receive(10)
            if ready.get("event") != "ready" or ready.get("protocol") != 1:
                raise RuntimeError("Rust audio worker protocol mismatch")
            self.startup_ms = (monotonic() - started) * 1000
            self.ready = ready
        except BaseException:
            self.close()
            raise

    def _read_events(self) -> None:
        assert self.process.stdout is not None
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    raise RuntimeError("Rust audio worker exited before completion")
                if len(line) > MAX_EVENT_BYTES or not line.endswith(b"\n"):
                    raise RuntimeError("Rust audio worker returned an oversized event")
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(
                    event.get("event"), str
                ):
                    raise RuntimeError("Rust audio worker returned an invalid event")
                self.events.put(event, timeout=1)
        except (Exception, queue.Full) as error:
            if not self.closed.is_set():
                try:
                    self.events.put_nowait(error)
                except queue.Full:
                    pass
                if self.process.poll() is None:
                    self.process.kill()

    def receive(self, timeout: float) -> dict[str, Any]:
        try:
            value = self.events.get(timeout=timeout)
        except queue.Empty:
            if self.closed.is_set() or self.process.poll() is not None:
                raise RuntimeError("Rust audio worker is unavailable") from None
            raise
        if isinstance(value, BaseException):
            raise RuntimeError(str(value)) from value
        return value

    def send(self, message: dict[str, Any], payload: bytes = b"") -> None:
        with self.send_lock:
            if self.closed.is_set():
                raise RuntimeError("Rust audio worker is closed")
            assert self.process.stdin is not None
            data = json.dumps(message, allow_nan=False).encode() + b"\n" + payload
            remaining = memoryview(data)
            while remaining:
                written = self.process.stdin.write(remaining)
                if written is None or written <= 0:
                    raise RuntimeError("Rust audio worker input closed")
                remaining = remaining[written:]

    def _send_bounded(
        self, message: dict[str, Any], payload: bytes, stop_requested: Event | None
    ) -> None:
        done = Event()
        failure: list[BaseException] = []

        def write() -> None:
            try:
                self.send(message, payload)
            except BaseException as error:
                failure.append(error)
            finally:
                done.set()

        writer = Thread(target=write, name="vntts-rust-upload", daemon=True)
        writer.start()
        deadline = monotonic() + 10
        while not done.wait(0.02):
            if monotonic() >= deadline or (
                stop_requested is not None and stop_requested.is_set()
            ):
                self.close()
                writer.join(timeout=1)
                raise RuntimeError("Rust PCM upload interrupted or timed out")
        if failure:
            raise RuntimeError("Rust PCM upload failed") from failure[0]

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        # Closing a pipe can wait for a concurrent writer; terminate its reader first.
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                pipe.close()
        if self.reader is not current_thread():
            self.reader.join(timeout=1)
        self.stderr.close()

    def play_buffer(
        self,
        samples: np.ndarray,
        sample_rate: int,
        channels: int,
        latency: object,
        cancelled: Any,
        first_write: Any,
        stop_requested: Event | None,
    ) -> tuple[bool, bool]:
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
                "Rust playback requires bounded finite mono/stereo float32 PCM"
            )
        with self.lock:
            if self.device_changed.is_set():
                raise RuntimeError("System audio device changed; retry playback")
            if cancelled():
                return False, False
            return _ClipPlayback(
                self,
                pcm,
                sample_rate,
                channels,
                latency,
                cancelled,
                first_write,
                stop_requested,
            ).run()

    def _expect(self, event: str, clip_id: int, timeout: float) -> dict[str, Any]:
        try:
            value = self.receive(timeout)
        except queue.Empty:
            raise RuntimeError(f"Rust audio {event} timed out") from None
        if value.get("event") != event or value.get("id") != clip_id:
            raise RuntimeError(
                str(value.get("error", f"Unexpected Rust audio event: {value}"))
            )
        return value


@dataclass
class _ClipPlayback:
    worker: RustAudioWorker
    pcm: np.ndarray
    sample_rate: int
    channels: int
    latency: object
    cancelled: Any
    first_write: Any
    stop_requested: Event | None
    clip_id: int = field(init=False)
    started: float = field(default_factory=monotonic, init=False)
    finished: Event = field(default_factory=Event, init=False)
    stop_sent: Event = field(default_factory=Event, init=False)
    preload_ms: float = field(default=0.0, init=False)
    first_reported: bool = field(default=False, init=False)
    first_callback_received_ms: float | None = field(default=None, init=False)

    def run(self) -> tuple[bool, bool]:
        self.worker.next_id += 1
        self.clip_id = self.worker.next_id
        self.worker.active_id = self.clip_id
        watcher: Thread | None = None
        try:
            self._load()
            if (
                self.stop_requested is not None and self.stop_requested.is_set()
            ) or self.cancelled():
                self.worker.send({"op": "stop", "id": self.clip_id})
                self.worker._expect("finished", self.clip_id, 3)
                return False, False
            self.worker.send({"op": "start", "id": self.clip_id})
            watcher = Thread(
                target=self._watch_stop, name="vntts-rust-stop", daemon=True
            )
            watcher.start()
            return self._wait_for_completion()
        except BaseException:
            # Protocol/guard failures cannot leave previously admitted audio playing.
            self.worker.close()
            raise
        finally:
            self.finished.set()
            if watcher is not None:
                watcher.join(timeout=1)
            self.worker.active_id = None

    def _load(self) -> None:
        self.worker._send_bounded(
            {
                "op": "load",
                "id": self.clip_id,
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "frames": len(self.pcm),
                "latency": self.latency,
            },
            self.pcm.tobytes(),
            self.stop_requested,
        )
        self.worker._expect("loaded", self.clip_id, 10)
        self.preload_ms = (monotonic() - self.started) * 1000

    def _watch_stop(self) -> None:
        while not self.finished.wait(0.01):
            if self.worker.device_changed.is_set() or (
                self.stop_requested is not None and self.stop_requested.is_set()
            ):
                self.stop_sent.set()
                try:
                    self.worker.send({"op": "stop", "id": self.clip_id})
                except Exception:
                    self.worker.close()
                    return
                if not self.finished.wait(3):
                    self.worker.close()
                return

    def _wait_for_completion(self) -> tuple[bool, bool]:
        deadline = monotonic() + len(self.pcm) / self.sample_rate + 10
        stop_deadline: float | None = None
        while True:
            if self.stop_sent.is_set() and stop_deadline is None:
                stop_deadline = monotonic() + 3
            if monotonic() >= min(deadline, stop_deadline or deadline):
                raise RuntimeError("Rust audio completion timed out")
            if not self.stop_sent.is_set() and self.cancelled():
                self.stop_sent.set()
                self.worker.send({"op": "stop", "id": self.clip_id})
            try:
                event = self.worker.receive(0.02)
            except queue.Empty:
                continue
            result = self._handle_event(event)
            if result is not None:
                return result

    def _handle_event(self, event: dict[str, Any]) -> tuple[bool, bool] | None:
        if event.get("id") != self.clip_id:
            raise RuntimeError("Rust audio returned an event for another clip")
        kind = event["event"]
        if kind == "error":
            raise RuntimeError(str(event.get("error", "Rust audio failed")))
        if kind == "first_callback" and not self.first_reported:
            self._first_callback(event)
        elif kind == "finished":
            return self._finish(event)
        elif kind not in {"started", "paused", "resumed", "first_callback"}:
            raise RuntimeError(f"Unknown Rust audio event: {kind}")
        return None

    def _first_callback(self, event: dict[str, Any]) -> None:
        frames = event.get("frames_written")
        if type(frames) is not int or not 0 <= frames <= len(self.pcm):
            raise RuntimeError("Rust audio returned an invalid first buffer")
        if frames:
            self.first_reported = True
            self.first_callback_received_ms = (monotonic() - self.started) * 1000
            if self.first_write is not None:
                self.first_write()

    def _validate_finished(self, event: dict[str, Any]) -> None:
        if event.get("outcome") not in {"complete", "interrupted", "failed"}:
            raise RuntimeError("Rust audio returned an invalid outcome")
        for name in ("frames_played", "underflow_count", "callback_count"):
            count = event.get(name)
            if type(count) is not int or count < 0:
                raise RuntimeError(f"Rust audio returned invalid {name}")
        if (
            event.get("sample_rate") != self.sample_rate
            or event.get("channels") != self.channels
        ):
            raise RuntimeError("Rust audio returned a different output format")

    def _finish(self, event: dict[str, Any]) -> tuple[bool, bool]:
        self._validate_finished(event)
        record_audio_lifecycle(
            "rust-pcm-summary",
            **{k: v for k, v in event.items() if k != "event"},
            reason="isolated-preloaded-pcm",
            preload_ms=round(self.preload_ms, 3),
            worker_startup_ms=round(self.worker.startup_ms, 3),
            worker_pid=self.worker.process.pid,
            first_callback_received_ms=self.first_callback_received_ms,
        )
        if self.worker.device_changed.is_set():
            raise RuntimeError("System audio device changed during Rust playback")
        if event.get("outcome") == "failed":
            raise RuntimeError(str(event.get("error", "Rust playback failed")))
        count = event.get("frames_played")
        if type(count) is not int or not 0 <= count <= len(self.pcm):
            raise RuntimeError("Rust audio returned an invalid frame count")
        complete = event.get("outcome") == "complete"
        if complete and count != len(self.pcm):
            raise RuntimeError("Rust audio completed without playing the whole buffer")
        return complete and not self.stop_sent.is_set(), event["underflow_count"] > 0


_worker: RustAudioWorker | None = None
_worker_lock = Lock()


def get_rust_audio_worker() -> RustAudioWorker:
    global _worker
    with _worker_lock:
        if _worker is not None and (
            _worker.closed.is_set() or _worker.device_changed.is_set()
        ):
            _worker.close()
            _worker = None
        if _worker is None:
            _worker = RustAudioWorker()
            atexit.register(_worker.close)
        return _worker


def notify_audio_device_changed() -> None:
    if _worker is not None:
        _worker.device_changed.set()


class RustPreparedStream:
    def __init__(
        self, *, samplerate: int, channels: int, dtype: str, latency: object
    ) -> None:
        if dtype != "float32":
            raise ValueError("Rust prepared output accepts float32 only")
        self.sample_rate = samplerate
        self.channels = channels
        self.latency = latency
        self.worker: RustAudioWorker | None = None

    def __enter__(self) -> RustPreparedStream:
        self.worker = get_rust_audio_worker()
        return self

    def __exit__(self, exc_type: object, *_args: object) -> None:
        if exc_type is not None and self.worker is not None:
            self.worker.close()

    def play_buffer(
        self,
        samples: np.ndarray,
        cancelled: Any,
        first_write: Any,
        stop_requested: Event | None,
    ) -> tuple[bool, bool]:
        if self.worker is None:
            raise RuntimeError("Rust prepared stream was not entered")
        return self.worker.play_buffer(
            samples,
            self.sample_rate,
            self.channels,
            self.latency,
            cancelled,
            first_write,
            stop_requested,
        )

    def write(self, _audio: object) -> object:
        raise RuntimeError("Rust prepared playback requires a full admitted buffer")

    def abort(self) -> None:
        if self.worker is not None:
            self.worker.close()

    def stop(self) -> None:
        self.abort()

    def close(self) -> None:
        self.abort()
