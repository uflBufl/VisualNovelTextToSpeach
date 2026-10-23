"""Explicit, bounded acquisition of timeline evidence before in-memory eviction."""

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Mapping
from uuid import uuid4

from vntts.cleanup import attempt_cleanup

_IDENTITY_FIELDS = {
    "stage",
    "action",
    "session_id",
    "run_id",
    "playback_id",
    "capture_id",
    "chunk_id",
    "line_id",
    "event_id",
    "previous_event_id",
    "target_event_id",
    "timing_provenance",
    "onset_provenance",
    "end_provenance",
    "measurement",
    "effective_source",
    "outcome",
    "worker_stage",
    "worker_state",
    "error_type",
}
_FINAL_RESERVE = 2048


def _line(value: dict[str, object]) -> bytes:
    return (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode()


class TimelineAcquisition:
    """One new file per opt-in acquisition; record never waits for disk I/O."""

    def __init__(
        self,
        path: str | Path,
        *,
        maximum_events: int = 20_000,
        maximum_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if (
            type(maximum_events) is not int
            or type(maximum_bytes) is not int
            or maximum_events < 1
            or maximum_bytes < 4096
            or maximum_events.bit_length() > 64
            or maximum_bytes.bit_length() > 64
        ):
            raise ValueError(
                "Acquisition limits must fit 64 bits, with at least one event and 4096 bytes"
            )
        self.acquisition_id = uuid4().hex
        self.maximum_events = maximum_events
        self.maximum_bytes = maximum_bytes
        self.lock = Lock()
        self.stop_requested = Event()
        self.queue: Queue[bytes] = Queue(maxsize=128)
        self.accepted = self.written = self.dropped = self.written_bytes = 0
        self.late_rejected = 0
        self.reserved_bytes = 0
        self.capped = False
        self.error_type: str | None = None
        self.file_closed = False
        self.file = Path(path).expanduser().open("xb", buffering=0)
        try:
            marker = _line(
                {
                    "kind": "start",
                    "version": 1,
                    "status": "recording",
                    "complete": False,
                    "acquisition_id": self.acquisition_id,
                    "scope": "explicit-acquisition-cutoff",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "maximum_events": maximum_events,
                    "maximum_bytes": maximum_bytes,
                }
            )
            self._write(marker)
            self.reserved_bytes = len(marker)
            self.thread = Thread(
                target=self._run, name="timeline-acquisition", daemon=True
            )
            self.thread.start()
        except BaseException as error:
            attempt_cleanup(
                self.file.close,
                description="Timeline journal close",
                primary_error=error,
            )
            raise

    def _write(self, payload: bytes) -> None:
        if self.file.write(payload) != len(payload):
            raise OSError("Incomplete acquisition write")

    def record(self, event: Mapping[str, object]) -> None:
        # Only bounded symbolic identities and numeric evidence, never free-text reasons.
        safe: dict[str, object] = {}
        for key, value in event.items():
            if isinstance(value, (bool, int)):
                if isinstance(value, bool) or value.bit_length() <= 64:
                    safe[key] = value
            elif isinstance(value, float) and math.isfinite(value):
                safe[key] = value
            elif (
                key in _IDENTITY_FIELDS
                and isinstance(value, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value)
            ):
                safe[key] = value
        payload = _line(
            {"kind": "event", "acquisition_id": self.acquisition_id, **safe}
        )
        with self.lock:
            if self.stop_requested.is_set():
                self.late_rejected += 1
                return
            if self.error_type is not None:
                self.dropped += 1
                return
            if (
                self.accepted >= self.maximum_events
                or self.reserved_bytes + len(payload) + _FINAL_RESERVE
                > self.maximum_bytes
            ):
                self.capped = True
                self.dropped += 1
                return
            try:
                self.queue.put_nowait(payload)
            except Full:
                self.dropped += 1
                return
            self.accepted += 1
            self.reserved_bytes += len(payload)

    def status(self) -> dict[str, object]:
        with self.lock:
            return {
                "acquisition_id": self.acquisition_id,
                "scope": "explicit-acquisition-cutoff",
                "maximum_events": self.maximum_events,
                "maximum_bytes": self.maximum_bytes,
                "accepted_events": self.accepted,
                "written_events": self.written,
                "pending_events": self.accepted - self.written,
                "dropped_events": self.dropped,
                "late_rejected_events": self.late_rejected,
                "written_event_bytes": self.written_bytes,
                "capped": self.capped,
                "error_type": self.error_type,
                "stop_requested": self.stop_requested.is_set(),
                "writer_running": self.thread.is_alive(),
                "file_closed": self.file_closed,
            }

    def stop(self, timeout: float = 1.0) -> dict[str, object]:
        with self.lock:
            self.stop_requested.set()
        self.thread.join(timeout=max(0.0, min(1.0, timeout)))
        return self.status()

    def _run(self) -> None:
        try:
            while True:
                try:
                    payload = self.queue.get(timeout=0.05)
                except Empty:
                    with self.lock:
                        if self.stop_requested.is_set() and self.queue.empty():
                            break
                    continue
                self._write(payload)
                with self.lock:
                    self.written += 1
                    self.written_bytes += len(payload)
            final = self.status()
            final.update(
                kind="stop",
                status="cutoff-written",
                complete=False,
                file_close="unobserved",
            )
            self._write(_line(final))
        except Exception as error:
            with self.lock:
                self.error_type = type(error).__name__
        finally:
            try:
                self.file.close()
                with self.lock:
                    self.file_closed = True
            except OSError as error:
                with self.lock:
                    self.error_type = type(error).__name__
