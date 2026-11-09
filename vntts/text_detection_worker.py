"""Persistent, killable screen-text inference with bounded file-based payloads."""

from __future__ import annotations

import json
import math
import multiprocessing
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from time import monotonic
from typing import TYPE_CHECKING

from PIL import Image

from vntts.ocr import OCRPreviewCancelled

if TYPE_CHECKING:
    from vntts.text_detection import TextLine

_MAX_PIXELS = 32 * 1024 * 1024
_MAX_RESULT_BYTES = 16 * 1024 * 1024


def _worker_main(
    connection: Connection, directory: str, detector_path: str, recognizer_path: str
) -> None:
    from vntts.text_detection import ScreenTextDetector

    root = Path(directory)
    try:
        detector = ScreenTextDetector(
            detector_path=detector_path, recognizer_path=recognizer_path
        )
        detector._load_models()
        connection.send_bytes(b"ready")
        while connection.recv_bytes(16) == b"run":
            try:
                request = json.loads((root / "request.json").read_text())
                width, height = request["size"]
                if (
                    type(width) is not int
                    or type(height) is not int
                    or width <= 0
                    or height <= 0
                    or width * height > _MAX_PIXELS
                ):
                    raise ValueError("Screen text image exceeds worker limit")
                with (root / "image.raw").open("rb") as source:
                    pixels = source.read(width * height * 3 + 1)
                if len(pixels) != width * height * 3:
                    raise ValueError("Invalid screen text image payload")
                lines = detector.detect(Image.frombytes("RGB", (width, height), pixels))
                result = {
                    "ok": True,
                    "lines": [
                        {
                            "box": line.box.to_json(),
                            "text": line.text,
                            "confidence": line.confidence,
                        }
                        for line in lines
                    ],
                }
            except Exception as error:
                result = {"ok": False, "error": str(error)}
            payload = json.dumps(result, allow_nan=False).encode("utf-8")
            if len(payload) > _MAX_RESULT_BYTES:
                payload = (
                    b'{"ok":false,"error":"Screen text result exceeds worker limit"}'
                )
            (root / "result.json").write_bytes(payload)
            connection.send_bytes(b"done")
    except EOFError, BrokenPipeError, OSError:
        pass
    finally:
        connection.close()


class TextDetectionWorker:
    """One child owns the native models; cancellation interrupts queued requests too."""

    def __init__(
        self,
        detector_path: str | Path,
        recognizer_path: str | Path,
        *,
        cancel_event: Event | None = None,
        timeout_seconds: float = 5.0,
        startup_timeout_seconds: float = 30.0,
    ) -> None:
        for timeout in (timeout_seconds, startup_timeout_seconds):
            if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError(
                    "Screen text worker timeouts must be finite and positive"
                )
        self.detector_path = str(detector_path)
        self.recognizer_path = str(recognizer_path)
        self.cancel_event = cancel_event
        self.timeout_seconds = timeout_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self._lock = Lock()
        self._state_lock = Lock()
        self._cancel = Event()
        self._closed = False
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._directory: TemporaryDirectory[str] | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def cancel_pending(self) -> None:
        with self._state_lock:
            self._cancel.set()
            self._cancel = Event()

    def _discard(self) -> None:
        process, connection, directory = (
            self._process,
            self._connection,
            self._directory,
        )
        # Detach before reaping so no late result can be reused after failure.
        self._process = self._connection = self._directory = None
        try:
            if process is not None:
                if process.pid is not None:
                    if process.is_alive():
                        process.terminate()
                    process.join(0.5)
                    if process.is_alive():
                        process.kill()
                        process.join(0.5)
                    if process.is_alive():
                        self._process = process
                        raise RuntimeError("Screen text worker could not be reaped")
                process.close()
        finally:
            if connection is not None:
                connection.close()
            if directory is not None:
                directory.cleanup()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            self._cancel.set()
        if not self._lock.acquire(timeout=2):
            raise RuntimeError("Screen text worker shutdown did not drain")
        try:
            self._discard()
        finally:
            self._lock.release()

    def _wait(self, token: bytes, check: Callable[[], float]) -> None:
        assert self._connection is not None and self._process is not None
        while True:
            remaining = check()
            if self._connection.poll(min(0.025, remaining)):
                response = self._connection.recv_bytes(16)
                check()
                if response != token:
                    raise RuntimeError("Invalid screen text worker response")
                return
            if not self._process.is_alive():
                raise RuntimeError(
                    f"Screen text worker exited ({self._process.exitcode})"
                )

    def _ensure_worker(self, cancelled: Callable[[], None]) -> None:
        if (
            self._process is not None
            and self._process.is_alive()
            and self._connection is not None
        ):
            return
        self._discard()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self._connection = parent
        self._directory = TemporaryDirectory(prefix="vntts-text-")
        self._process = context.Process(
            target=_worker_main,
            args=(
                child,
                self._directory.name,
                self.detector_path,
                self.recognizer_path,
            ),
            name="vntts-text-detection",
            daemon=True,
        )
        deadline = monotonic() + self.startup_timeout_seconds

        def check_startup() -> float:
            cancelled()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Screen text worker startup timed out")
            return remaining

        try:
            self._process.start()
        finally:
            child.close()
        self._wait(b"ready", check_startup)

    def detect(
        self, image: Image.Image, *, cancel_event: Event | None = None
    ) -> tuple[TextLine, ...]:
        from vntts.ocr import DialogRegion
        from vntts.text_detection import TextLine

        if (
            image.width <= 0
            or image.height <= 0
            or image.width * image.height > _MAX_PIXELS
        ):
            raise ValueError("Screen text image exceeds worker limit")
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Screen text worker is closed")
            cancellation = self._cancel

        def cancelled() -> None:
            if (
                cancellation.is_set()
                or (self.cancel_event is not None and self.cancel_event.is_set())
                or (cancel_event is not None and cancel_event.is_set())
            ):
                raise OCRPreviewCancelled("Screen text detection cancelled")

        deadline = monotonic() + self.timeout_seconds

        def check() -> float:
            cancelled()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Screen text detection timed out")
            return remaining

        acquired = False
        try:
            check()
            while not self._lock.acquire(timeout=min(0.025, check())):
                pass
            acquired = True
            check()
            self._ensure_worker(cancelled)
            deadline = monotonic() + self.timeout_seconds
            assert self._directory is not None and self._connection is not None
            root = Path(self._directory.name)
            (root / "image.raw").write_bytes(image.convert("RGB").tobytes())
            (root / "request.json").write_text(json.dumps({"size": image.size}))
            check()
            self._connection.send_bytes(b"run")
            self._wait(b"done", check)
            with (root / "result.json").open("rb") as source:
                payload = source.read(_MAX_RESULT_BYTES + 1)
            if len(payload) > _MAX_RESULT_BYTES:
                raise RuntimeError("Screen text worker result exceeds limit")
            result = json.loads(payload)
            check()
            if not isinstance(result, dict) or result.get("ok") is not True:
                detail = (
                    result.get("error", "invalid response")
                    if isinstance(result, dict)
                    else "invalid response"
                )
                raise RuntimeError(f"Screen text detection failed: {detail}")
            if not isinstance(result.get("lines"), list):
                raise RuntimeError("Invalid screen text worker result")
            lines = tuple(
                TextLine(
                    DialogRegion.from_mapping(line["box"]),
                    line["text"],
                    line["confidence"],
                )
                for line in result["lines"]
            )
            check()
            return lines
        except BaseException:
            if acquired:
                self._discard()
            raise
        finally:
            if acquired:
                self._lock.release()
