"""Capture bounded Python thread locations when offline generation stops progressing."""

from __future__ import annotations

import hashlib
import os
import sys
from threading import Event, Lock, Thread, get_ident
from time import monotonic
from types import FrameType, TracebackType
from typing import Self

from vntts.support import record_generation_stall
from vntts.synthesis import CancellationSignal


class GenerationStallWatchdog:
    STALL_SECONDS = 120.0
    CANCEL_SECONDS = 10.0
    CAPTURE_INTERVAL_SECONDS = 30.0
    MAX_CAPTURES = 3
    POLL_SECONDS = 1.0

    def __init__(self, cancel_event: CancellationSignal | None = None) -> None:
        self._cancel_event = cancel_event
        self._worker_id = get_ident()
        self._stop = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._progress_at = monotonic()
        self._cancelled_at: float | None = None
        self._last_capture_at: float | None = None
        self._captures = 0
        self._details: dict[str, object] = {"phase": "starting"}

    def __enter__(self) -> Self:
        try:
            self._thread = Thread(
                target=self._run, name="generation-stall-watchdog", daemon=True
            )
            self._thread.start()
        except Exception:
            # Diagnostic startup must not prevent generation.
            self._thread = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)

    def progress(self, state: object = None) -> None:
        try:
            active = state.get("active") if isinstance(state, dict) else None
            details: dict[str, object] = {"phase": "finalizing"}
            if isinstance(active, dict):
                details = {
                    key: active[key]
                    for key in (
                        "phase",
                        "attempt",
                        "provider_attempt",
                        "provider",
                        "seed",
                    )
                    if key in active
                }
                queue_id = active.get("queue_id")
                if isinstance(queue_id, str):
                    details["queue_id_sha256"] = hashlib.sha256(
                        queue_id.encode("utf-8")
                    ).hexdigest()
            with self._lock:
                self._details = details
                self._progress_at = monotonic()
                self._captures = 0
                self._last_capture_at = None
        except Exception:
            pass

    def _run(self) -> None:
        while not self._stop.wait(self.POLL_SECONDS):
            try:
                self._check()
            except Exception:
                # Even a failed diagnostic must leave cancellation/generation alone.
                pass

    def _check(self) -> None:
        now = monotonic()
        cancelled = self._cancel_event is not None and self._cancel_event.is_set()
        with self._lock:
            if cancelled and self._cancelled_at is None:
                self._cancelled_at = now
                self._captures = 0
                self._last_capture_at = None
            stalled = now - self._progress_at >= self.STALL_SECONDS
            cancel_stalled = (
                self._cancelled_at is not None
                and now - self._cancelled_at >= self.CANCEL_SECONDS
            )
            if (
                self._stop.is_set()
                or not (stalled or cancel_stalled)
                or self._captures >= self.MAX_CAPTURES
                or (
                    self._last_capture_at is not None
                    and now - self._last_capture_at < self.CAPTURE_INTERVAL_SECONDS
                )
            ):
                return
            self._captures += 1
            self._last_capture_at = now
            details = {
                **self._details,
                "elapsed_seconds": round(max(0.0, now - self._progress_at), 3),
                "cancellation_requested": cancelled,
                "capture_index": self._captures,
            }
        record_generation_stall(
            **details,
            process_id=os.getpid(),
            worker_thread_id=self._worker_id,
            threads=_thread_locations(self._worker_id),
        )


def _thread_locations(worker_id: int) -> list[dict[str, object]]:
    # Never format a traceback: it includes source lines and can expose literals.
    current = sys._current_frames()
    try:
        identifiers = sorted(current, key=lambda value: value != worker_id)
        threads: list[dict[str, object]] = []
        for identifier in identifiers[:32]:
            frame: FrameType | None = current[identifier]
            locations: list[dict[str, object]] = []
            while frame is not None and len(locations) < 64:
                filename = frame.f_code.co_filename.replace("\\", "/")
                parts = filename.split("/")
                relative = parts[-1]
                for package in ("vntts", "tests", "scripts"):
                    if package in parts:
                        relative = "/".join(parts[parts.index(package) :])
                        break
                locations.append(
                    {
                        "module": frame.f_globals.get("__name__", "unknown"),
                        "file": relative,
                        "function": frame.f_code.co_name,
                        "line": frame.f_lineno,
                    }
                )
                frame = frame.f_back
            threads.append(
                {
                    "thread_id": identifier,
                    "worker": identifier == worker_id,
                    "frames": list(reversed(locations)),
                    "truncated": frame is not None,
                }
            )
        return threads
    finally:
        # Frame references must not retain the blocked operation or its locals.
        del current
