"""Bounded, killable transport for one native capture target.

The native monotonic clock is deliberately kept separate from Python's clock.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from threading import Event, Lock, RLock, Thread, current_thread
from time import monotonic
from typing import Any, TypeGuard
from uuid import uuid4

from PIL import Image

MAX_HEADER_BYTES = 64 * 1024
MAX_PIXELS = 32 * 1024 * 1024
MAX_PAYLOAD_BYTES = MAX_PIXELS * 4
BACKENDS = {"macos_screencapturekit", "windows_graphics_capture", "linux_x11_composite"}


class CaptureError(RuntimeError):
    """Capture failed without substituting another target or backend."""

    def __init__(self, message: str, *, code: str = "transport_error") -> None:
        super().__init__(message)
        self.code = code


def worker_executable() -> Path:
    """Resolve an installed worker; never compile or silently fall back."""
    explicit = os.environ.get("VNTTS_CAPTURE_WORKER")
    name = "vntts-capture-worker" + (".exe" if sys.platform == "win32" else "")
    root = Path(__file__).resolve().parents[1]
    if explicit:
        path = Path(explicit).expanduser()
    elif getattr(sys, "frozen", False):
        path = (
            Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            / "native-capture"
            / name
        )
    elif sys.platform == "darwin":
        path = root / "native/capture-worker/macos/build" / name
    elif sys.platform == "win32":
        path = root / "native/capture-worker/windows/target/release" / name
    else:
        raise CaptureError(
            "Native capture requires macOS or Windows.", code="unsupported_platform"
        )
    build = (
        "scripts/build-macos-capture.sh"
        if sys.platform == "darwin"
        else "powershell -File scripts/build-windows-capture.ps1"
    )
    if not path.is_file() or (sys.platform != "win32" and not os.access(path, os.X_OK)):
        raise CaptureError(
            "Native capture worker is missing or not executable. "
            + (
                "Check VNTTS_CAPTURE_WORKER."
                if explicit
                else f"Build it from the project directory: {build}"
            ),
            code="worker_unavailable",
        )
    return path


def worker_command() -> list[str]:
    if sys.platform.startswith("linux") and not os.environ.get("VNTTS_CAPTURE_WORKER"):
        from vntts.window_capture import ensure_screen_capture_supported

        ensure_screen_capture_supported()
        return (
            [sys.executable, "--x11-capture-worker"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "vntts.x11_capture_worker"]
        )
    return [str(worker_executable())]


def capture_cancelled(cancel: Event | Callable[[], bool] | None) -> bool:
    return cancel.is_set() if isinstance(cancel, Event) else bool(cancel and cancel())


def _integer(value: Any, minimum: int = 0) -> TypeGuard[int]:
    return type(value) is int and value >= minimum


def _time(value: Any) -> bool:
    return type(value) in (int, float) and isfinite(value) and value >= 0


def _rect(value: Any) -> TypeGuard[list[int]]:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(type(item) is int for item in value)
        and value[2] > 0
        and value[3] > 0
    )


@dataclass(frozen=True)
class NativeFrame:
    backend: str
    scope: str
    session_id: str
    window_id: int | None
    pid: int | None
    width: int
    height: int
    row_stride: int
    pixel_format: str
    content_rect: tuple[int, int, int, int]
    desktop_bounds: tuple[int, int, int, int]
    frame_sequence: int
    source_time: float | None
    acquired_at: float
    layout_epoch: int
    pixels: bytes

    def to_image(self) -> Image.Image:
        """Return an owned RGB image; content_rect stays in returned pixel space."""
        return Image.frombytes(
            "RGB",
            (self.width, self.height),
            self.pixels,
            "raw",
            "BGRX",
            self.row_stride,
        )


class NativeCaptureSession:
    """One worker and target. Cancellation discards the process, never a response."""

    def __init__(
        self,
        target: dict[str, Any],
        *,
        command: list[str] | None = None,
        max_fps: int = 10,
        startup_timeout: float = 10,
        cancel: Event | Callable[[], bool] | None = None,
    ) -> None:
        target = dict(target)
        if target.get("kind") == "window":
            valid = set(target) == {"kind", "window_id", "pid"} and all(
                _integer(target.get(key), 1) for key in ("window_id", "pid")
            )
        else:
            valid = (
                target.get("kind") == "display"
                and set(target) == {"kind", "display_id"}
                and _integer(target.get("display_id"))
            )
        if not valid or not _integer(max_fps, 1) or max_fps > 60:
            raise ValueError("Invalid capture target or max_fps (expected 1..60)")
        if not _time(startup_timeout) or startup_timeout == 0:
            raise ValueError("startup_timeout must be positive")
        self._target = target
        self.session_id = str(uuid4())
        self._lock = RLock()
        self._close_lock = Lock()
        self._closed = Event()
        self._responses: queue.Queue[tuple[dict[str, Any], bytes] | BaseException] = (
            queue.Queue(1)
        )
        self._next_id = 0
        self._previous: NativeFrame | None = None
        self._stderr = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                command or worker_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                bufsize=0,
            )
        except BaseException:
            self._stderr.close()
            raise
        self.reader = Thread(
            target=self._read, name="vntts-capture-reader", daemon=True
        )
        self.reader.start()
        try:
            opened_at = monotonic()
            result, payload = self._request(
                "open", startup_timeout, cancel, target=target, max_fps=max_fps
            )
            if (
                payload
                or result.get("backend") not in BACKENDS
                or result.get("session_id") != self.session_id
                or not _time(result.get("clock_now"))
            ):
                raise CaptureError("Invalid native capture open response")
            received_at = monotonic()
            self.backend = result["backend"]
            self.worker_clock_at_open = float(result["clock_now"])
            self.clock_offset = (
                opened_at + received_at
            ) / 2 - self.worker_clock_at_open
            self.clock_uncertainty = (received_at - opened_at) / 2
        except BaseException:
            self.close()
            raise

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while not self._closed.is_set():
                self._responses.put_nowait(self._read_response())
        except Exception as error:
            if not self._closed.is_set():
                try:
                    self._responses.put_nowait(error)
                except queue.Full:
                    pass
                if self.process.poll() is None:
                    self.process.kill()

    def _read_response(self) -> tuple[dict[str, Any], bytes]:
        assert self.process.stdout is not None
        line = self.process.stdout.readline(MAX_HEADER_BYTES + 1)
        if not line:
            raise CaptureError("Native capture worker exited before responding")
        if len(line) > MAX_HEADER_BYTES or not line.endswith(b"\n"):
            raise CaptureError("Native capture response header exceeds limit")
        header = json.loads(line)
        if (
            not isinstance(header, dict)
            or header.get("version") != 1
            or type(header.get("version")) is not int
            or not _integer(header.get("id"), 1)
            or type(header.get("ok")) is not bool
            or not _integer(header.get("payload_bytes"))
            or header["payload_bytes"] > MAX_PAYLOAD_BYTES
        ):
            raise CaptureError("Invalid native capture response envelope")
        size = header["payload_bytes"]
        if not header["ok"] and size:
            raise CaptureError("Native capture error included pixel payload")
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.process.stdout.read(min(size - len(chunks), 1024 * 1024))
            if not chunk:
                raise CaptureError("Truncated native capture pixel payload")
            chunks.extend(chunk)
        return header, bytes(chunks)

    def _request(
        self,
        op: str,
        timeout: float,
        cancel: Event | Callable[[], bool] | None = None,
        **fields: Any,
    ) -> tuple[dict[str, Any], bytes]:
        with self._lock:
            if self._closed.is_set():
                raise CaptureError("Native capture session is closed", code="closed")
            if capture_cancelled(cancel):
                raise CaptureError("Native capture cancelled", code="cancelled")
            self._next_id += 1
            request = {
                "version": 1,
                "id": self._next_id,
                "op": op,
                "session_id": self.session_id,
                **fields,
            }
            encoded = json.dumps(request, allow_nan=False).encode() + b"\n"
            if len(encoded) > MAX_HEADER_BYTES:
                raise ValueError("Native capture request exceeds header limit")
            deadline = monotonic() + timeout
            try:
                assert self.process.stdin is not None
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
                response = self._wait_response(deadline, cancel)
                if self._closed.is_set():
                    raise CaptureError(
                        "Native capture session is closed", code="closed"
                    )
                if capture_cancelled(cancel):
                    raise CaptureError("Native capture cancelled", code="cancelled")
                return self._response_result(response)
            except (OSError, ValueError) as error:
                self.close()
                raise CaptureError(f"Native capture pipe failed: {error}") from error
            except BaseException:
                self.close()
                raise

    def _response_result(
        self, response: tuple[dict[str, Any], bytes] | BaseException
    ) -> tuple[dict[str, Any], bytes]:
        if isinstance(response, BaseException):
            raise CaptureError(str(response)) from response
        header, payload = response
        if header["id"] != self._next_id:
            raise CaptureError("Native capture response request identity mismatch")
        if not header["ok"]:
            error = header.get("error")
            if (
                not isinstance(error, dict)
                or not isinstance(error.get("code"), str)
                or not error["code"]
                or not isinstance(error.get("message"), str)
            ):
                raise CaptureError("Invalid native capture error response")
            raise CaptureError(error["message"], code=error["code"])
        result = header.get("result")
        if not isinstance(result, dict):
            raise CaptureError("Invalid native capture result")
        return result, payload

    def _wait_response(
        self, deadline: float, cancel: Event | Callable[[], bool] | None
    ) -> tuple[dict[str, Any], bytes] | BaseException:
        while True:
            if self._closed.is_set():
                raise CaptureError("Native capture session is closed", code="closed")
            if capture_cancelled(cancel):
                raise CaptureError("Native capture cancelled", code="cancelled")
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise CaptureError("Native capture response timed out", code="timeout")
            try:
                response = self._responses.get(timeout=min(remaining, 0.025))
                return response
            except queue.Empty:
                continue

    def capture(
        self,
        *,
        timeout_ms: int = 2000,
        cancel: Event | Callable[[], bool] | None = None,
    ) -> NativeFrame:
        with self._lock:
            return self._capture(timeout_ms=timeout_ms, cancel=cancel)

    def _capture(
        self, *, timeout_ms: int, cancel: Event | Callable[[], bool] | None
    ) -> NativeFrame:
        if not _integer(timeout_ms, 1) or timeout_ms > 10000:
            raise ValueError("timeout_ms must be 1..10000")
        result, payload = self._request(
            "capture", timeout_ms / 1000 + 0.5, cancel, timeout_ms=timeout_ms
        )
        try:
            rect = self._validate_frame_metadata(result, payload)
            frame = NativeFrame(
                **{
                    key: result[key]
                    for key in NativeFrame.__dataclass_fields__
                    if key not in {"pixels", "content_rect", "desktop_bounds"}
                },
                content_rect=(rect[0], rect[1], rect[2], rect[3]),
                desktop_bounds=tuple(result["desktop_bounds"]),
                pixels=payload,
            )
            previous = self._previous
            if previous and (
                frame.frame_sequence < previous.frame_sequence
                or frame.layout_epoch < previous.layout_epoch
                or (
                    (frame.width, frame.height, frame.content_rect)
                    != (previous.width, previous.height, previous.content_rect)
                    and frame.layout_epoch == previous.layout_epoch
                )
            ):
                raise CaptureError("Native capture frame sequence or layout regressed")
            if self._closed.is_set():
                raise CaptureError("Native capture session is closed", code="closed")
            self._previous = frame
            return frame
        except BaseException:
            self.close()
            raise

    def _validate_frame_metadata(
        self, result: dict[str, Any], payload: bytes
    ) -> list[int]:
        required = set(NativeFrame.__dataclass_fields__) - {"pixels"}
        if not required.issubset(result):
            raise CaptureError("Native capture frame metadata is incomplete")
        width, height = result.get("width"), result.get("height")
        if (
            not _integer(width, 1)
            or not _integer(height, 1)
            or width * height > MAX_PIXELS
            or result.get("row_stride") != width * 4
            or type(result.get("row_stride")) is not int
            or result.get("pixel_format") != "BGRA8"
            or len(payload) != width * height * 4
        ):
            raise CaptureError("Invalid native capture frame dimensions or payload")
        rect = result.get("content_rect")
        if (
            not _rect(rect)
            or rect[0] < 0
            or rect[1] < 0
            or rect[0] + rect[2] > width
            or rect[1] + rect[3] > height
            or not _rect(result.get("desktop_bounds"))
        ):
            raise CaptureError("Invalid native capture frame rectangles")
        self._validate_frame_identity(result)
        source = result.get("source_time")
        if (
            not _integer(result.get("frame_sequence"), 1)
            or not _integer(result.get("layout_epoch"), 1)
            or not _time(result.get("acquired_at"))
            or (source is not None and not _time(source))
        ):
            raise CaptureError("Invalid native capture frame clock or sequence")
        return rect

    def _validate_frame_identity(self, result: dict[str, Any]) -> None:
        window = self._target["kind"] == "window"
        if window and (
            not _integer(result.get("window_id"), 1)
            or not _integer(result.get("pid"), 1)
        ):
            raise CaptureError("Invalid native capture window identity")
        if (
            result.get("backend") != self.backend
            or result.get("session_id") != self.session_id
            or result.get("scope") != ("selected_window" if window else "display")
            or result.get("window_id") != self._target.get("window_id")
            or result.get("pid") != self._target.get("pid")
        ):
            raise CaptureError("Native capture frame target identity mismatch")

    def close(self) -> None:
        with self._close_lock:
            self._closed.set()
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=2)
            if self.reader is not current_thread():
                self.reader.join(timeout=2)
                if self.reader.is_alive():
                    raise CaptureError("Native capture reader did not stop")
            for stream in (self.process.stdin, self.process.stdout):
                if stream is not None:
                    stream.close()
            self._stderr.close()

    def __enter__(self) -> NativeCaptureSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
