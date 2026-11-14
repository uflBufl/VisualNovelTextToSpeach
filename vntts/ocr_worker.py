"""Reusable, killable native OCR process; the parent retains parsing and routing."""

from __future__ import annotations

import atexit
import json
import multiprocessing
import os
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from time import monotonic
from typing import Any

from PIL import Image
from pytesseract import pytesseract as runtime

from vntts.ocr import (
    OcrData,
    OCRPreviewCancelled,
    OCRRegions,
    OCRResult,
    VoiceRegistry,
    _ocr_budget,
    default_minimum_ocr_confidence,
)
from vntts.ocr import (
    recognize_dialog_image_result as recognize_pipeline,
)
from vntts.reading_diagnostics import record_diagnostic_event
from vntts.tesseract_config import tesseract_config

_MAX_PIXELS = 32 * 1024 * 1024
_MAX_RESULT_BYTES = 16 * 1024 * 1024
_DEFAULT_TIMEOUT = 15.0


def _worker_main(
    connection: Connection,
    directory: str,
    executable: str,
    tessdata: str | None,
    library: str | None,
    sparse_model: str | None = None,
) -> None:
    # Set before loading native OpenMP; never throttle the application's TTS.
    os.environ["OMP_THREAD_LIMIT"] = "1"
    from vntts.tesseract_native import NativeTesseract

    root = Path(directory)
    engine = None
    language = None
    sparse_recognizer = None
    try:
        connection.send_bytes(b"ready")
        while connection.recv_bytes(16) == b"run":
            try:
                request = json.loads((root / "request.json").read_text())
                width, height = request["size"]
                if not (
                    isinstance(width, int)
                    and isinstance(height, int)
                    and width > 0
                    and height > 0
                    and width * height <= _MAX_PIXELS
                ):
                    raise ValueError("OCR image dimensions exceed worker limit")
                pixels = (root / "image.raw").read_bytes()
                mode = request.get("mode", "L")
                if mode not in {"L", "RGB"} or len(pixels) != width * height * len(
                    mode
                ):
                    raise ValueError("Invalid OCR image payload")
                if request.get("sparse"):
                    if sparse_model is None:
                        raise RuntimeError("Sparse dialogue model is not prepared")
                    if sparse_recognizer is None:
                        from vntts.sparse_ocr import SparseLineRecognizer

                        sparse_recognizer = SparseLineRecognizer(sparse_model)
                    text, confidence = sparse_recognizer.recognize(
                        Image.frombytes(mode, (width, height), pixels)
                    )
                    result = {"ok": True, "text": text, "confidence": confidence}
                else:
                    if engine is None or language != request["language"]:
                        if engine is not None:
                            engine.close()
                            engine = None
                        engine = NativeTesseract(
                            request["language"],
                            executable=executable,
                            tessdata_dir=tessdata,
                            library=library,
                        )
                        language = request["language"]
                    text, data = engine.recognize(
                        Image.frombytes("L", (width, height), pixels),
                        request["psm"],
                        text=request["text"],
                        data=request["data"],
                    )
                    result = {
                        "ok": True,
                        "text": text,
                        "data": data,
                        "pid": os.getpid(),
                    }
            except Exception as error:
                result = {"ok": False, "error": str(error), "pid": os.getpid()}
            payload = json.dumps(result).encode("utf-8")
            if len(payload) > _MAX_RESULT_BYTES:
                payload = b'{"ok": false, "error": "OCR result exceeds worker limit"}'
            (root / "result.json").write_bytes(payload)
            # Images/TSV travel through bounded files, so pipe writes cannot block
            # cancellation behind a large image or a partially written response.
            connection.send_bytes(b"done")
    except EOFError, BrokenPipeError, OSError:
        pass
    finally:
        if engine is not None:
            engine.close()
        connection.close()


class PersistentOCR:
    def __init__(self, sparse_model_path: str | Path | None = None) -> None:
        self._lock = Lock()
        self._state_lock = Lock()
        self._cancel = Event()
        self._closed = False
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._directory: TemporaryDirectory[str] | None = None
        self._configuration: tuple[str, str | None, str | None, str | None] | None = (
            None
        )
        self._sparse_model_path = str(sparse_model_path) if sparse_model_path else None

    def configure_sparse_model(self, path: str | Path | None) -> None:
        with self._lock:
            self._sparse_model_path = str(path) if path else None

    def warm_up_sparse(self, cancelled: Callable[[], bool] | None = None) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("OCR service is closed")
            cancellation = self._cancel
        check = _ocr_budget(
            _DEFAULT_TIMEOUT,
            lambda: cancellation.is_set() or (cancelled is not None and cancelled()),
        )
        while not self._lock.acquire(timeout=0.025):
            check()
        try:
            check()
            if self._sparse_model_path:
                self._run_sparse(Image.new("RGB", (32, 32), "white"), check)
        except BaseException:
            self._discard()
            raise
        finally:
            self._lock.release()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def cancel_pending(self) -> None:
        with self._state_lock:
            self._cancel.set()
            self._cancel = Event()

    def _discard(self) -> None:
        # A failed reap must never make the old response eligible for reuse.
        self._configuration = None
        process, connection, directory = (
            self._process,
            self._connection,
            self._directory,
        )
        if process is not None:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(0.5)
                if process.is_alive():
                    process.kill()
                    process.join(0.5)
                if process.is_alive():
                    raise RuntimeError("OCR worker could not be reaped")
            process.close()
        self._process = None
        self._connection = None
        self._directory = None
        self._configuration = None
        if connection is not None:
            connection.close()
        if directory is not None:
            directory.cleanup()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            self._cancel.set()
        if not self._lock.acquire(timeout=2):
            raise RuntimeError("OCR worker shutdown did not drain")
        try:
            self._discard()
        finally:
            self._lock.release()

    def _wait(self, token: bytes, check: Callable[[], float | None]) -> None:
        assert self._connection is not None and self._process is not None
        while True:
            remaining = check()
            if self._connection.poll(min(0.025, remaining or 0.025)):
                received = self._connection.recv_bytes(16)
                check()
                if received != token:
                    raise RuntimeError("Invalid OCR worker response")
                return
            if not self._process.is_alive():
                raise RuntimeError(f"OCR worker exited ({self._process.exitcode})")

    def _ensure_worker(self, check: Callable[[], float | None]) -> None:
        configuration = (
            str(runtime.tesseract_cmd),
            os.environ.get("TESSDATA_PREFIX"),
            os.environ.get("VNTTS_TESSERACT_LIBRARY"),
            self._sparse_model_path,
        )
        if (
            self._process is not None
            and self._process.is_alive()
            and self._configuration == configuration
        ):
            return
        self._discard()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self._connection = parent
        self._directory = TemporaryDirectory(prefix="vntts-ocr-")
        self._configuration = configuration
        self._process = context.Process(
            target=_worker_main,
            args=(child, self._directory.name, *configuration),
            name="vntts-ocr",
            daemon=True,
        )
        try:
            self._process.start()
        finally:
            child.close()
        self._wait(b"ready", check)
        try:
            record_diagnostic_event("ocr", "worker_started", pid=self.pid)
        except Exception:
            pass

    def _run_native(
        self,
        img: Image.Image,
        check: Callable[[], float | None],
        *,
        lang: str,
        config: str,
        text: bool,
        data: bool,
        **unused: object,
    ) -> tuple[str, OcrData]:
        check()
        modes = {tesseract_config(psm): psm for psm in (6, 7, 11)}
        if config not in modes or img.mode != "L":
            raise ValueError("Unsupported native OCR input")
        result = self._run_request(
            img,
            check,
            {"language": lang, "psm": modes[config], "text": text, "data": data},
        )
        if not isinstance(result.get("text"), str) or not isinstance(
            result.get("data"), dict
        ):
            raise RuntimeError("Invalid OCR worker result")
        return result["text"], result["data"]

    def _run_sparse(
        self, image: Image.Image, check: Callable[[], float | None]
    ) -> tuple[str, float]:
        result = self._run_request(image.convert("RGB"), check, {"sparse": True})
        if not isinstance(result.get("text"), str) or not isinstance(
            result.get("confidence"), (int, float)
        ):
            raise RuntimeError("Invalid sparse OCR worker result")
        return result["text"], float(result["confidence"])

    def _run_request(
        self,
        img: Image.Image,
        check: Callable[[], float | None],
        request: dict[str, object],
    ) -> dict[str, Any]:
        check()
        if img.width * img.height > _MAX_PIXELS:
            raise ValueError("OCR image exceeds worker limit")
        self._ensure_worker(check)
        assert self._directory is not None and self._connection is not None
        root = Path(self._directory.name)
        (root / "image.raw").write_bytes(img.tobytes())
        (root / "request.json").write_text(
            json.dumps({"size": img.size, "mode": img.mode, **request})
        )
        check()
        self._connection.send_bytes(b"run")
        self._wait(b"done", check)
        result_path = root / "result.json"
        if result_path.stat().st_size > _MAX_RESULT_BYTES:
            raise RuntimeError("OCR worker result exceeds limit")
        result = json.loads(result_path.read_text())
        check()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError(
                f"Native OCR failed: {result.get('error', 'invalid response') if isinstance(result, dict) else 'invalid response'}"
            )
        return result

    def recognize(
        self,
        image: Image.Image,
        voice_registry: VoiceRegistry | None = None,
        *,
        minimum_confidence: float = default_minimum_ocr_confidence,
        language: str = "eng",
        ocr_regions: OCRRegions | None = None,
        timeout: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> OCRResult:
        def is_cancelled() -> bool:
            return cancellation.is_set() or (cancelled is not None and cancelled())

        provenance = image.info.get("capture_provenance")
        identity = (
            {key: provenance.get(key) for key in ("run_id", "capture_id")}
            if isinstance(provenance, dict)
            else {}
        )
        queued_at: float | None = None
        worker_started: float | None = None
        completed: float | None = None
        outcome = "success"
        error_type: str | None = None
        request_pid: int | None = None
        try:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("OCR service is closed")
                cancellation = self._cancel
            check = _ocr_budget(
                _DEFAULT_TIMEOUT if timeout is None else timeout, is_cancelled
            )
            check()
            queued_at = monotonic()
            while not self._lock.acquire(timeout=0.025):
                check()
            worker_started = monotonic()
            check()
            result = recognize_pipeline(
                image,
                voice_registry,
                recognize_text=lambda img, **kw: self._run_native(
                    img, check, text=True, data=False, **kw
                )[0],
                recognize_data=lambda img, **kw: self._run_native(
                    img, check, text=False, data=True, **kw
                )[1],
                recognize_combined=lambda img, lang, remaining: self._run_native(
                    img,
                    check,
                    lang=lang,
                    config=tesseract_config(6),
                    text=True,
                    data=True,
                ),
                minimum_confidence=minimum_confidence,
                language=language,
                ocr_regions=ocr_regions,
                timeout=check(),
                cancelled=is_cancelled,
                recognize_sparse=self._run_sparse if self._sparse_model_path else None,
            )
            check()
            return result
        except BaseException as error:
            completed = monotonic()
            outcome = (
                "cancelled"
                if isinstance(error, OCRPreviewCancelled)
                else "timeout"
                if isinstance(error, TimeoutError)
                else "error"
            )
            error_type = type(error).__name__
            if worker_started is not None:
                request_pid = self.pid
                self._discard()
            raise
        finally:
            completed = monotonic() if completed is None else completed
            if worker_started is not None:
                if outcome == "success":
                    request_pid = self.pid
                self._lock.release()
            try:
                record_diagnostic_event(
                    "ocr",
                    "worker_request_completed",
                    **identity,
                    worker_pid=request_pid,
                    outcome=outcome,
                    error_type=error_type,
                    queue_wait_ms=round(
                        (
                            (completed if worker_started is None else worker_started)
                            - queued_at
                        )
                        * 1000,
                        3,
                    )
                    if queued_at is not None
                    else 0.0,
                    worker_call_ms=round((completed - worker_started) * 1000, 3)
                    if worker_started is not None
                    else 0.0,
                )
            except Exception:
                pass


_service_lock = Lock()
_service: PersistentOCR | None = None
_accepting = True
_sparse_model_path: str | Path | None = None


def configure_sparse_ocr(model_path: str | Path | None) -> None:
    global _sparse_model_path
    with _service_lock:
        _sparse_model_path = model_path
        service = _service if _accepting else None
    if service is not None:
        service.configure_sparse_model(model_path)


def warm_up_sparse_ocr(cancelled: Callable[[], bool] | None = None) -> None:
    global _service
    with _service_lock:
        if not _accepting:
            raise RuntimeError("OCR service is shutting down")
        if _service is None:
            _service = PersistentOCR(_sparse_model_path)
        service = _service
    service.warm_up_sparse(cancelled)


def recognize_dialog_image_result(
    image: Image.Image, voice_registry: VoiceRegistry | None = None, **options: Any
) -> OCRResult:
    global _service
    with _service_lock:
        if not _accepting:
            raise RuntimeError("OCR service is shutting down")
        if _service is None:
            _service = PersistentOCR(_sparse_model_path)
        service = _service
    return service.recognize(image, voice_registry, **options)


def cancel_pending_ocr() -> None:
    with _service_lock:
        if _service is not None:
            _service.cancel_pending()


def open_ocr_worker() -> None:
    """Reopen admission only at an explicit application startup."""
    global _service, _accepting
    with _service_lock:
        if not _accepting:
            if _service is not None and _service.pid is not None:
                raise RuntimeError("Previous OCR worker is still shutting down")
            _service = None
            _accepting = True


def close_ocr_worker() -> None:
    global _accepting
    with _service_lock:
        _accepting = False
        service = _service
    if service is not None:
        service.close()


atexit.register(close_ocr_worker)
