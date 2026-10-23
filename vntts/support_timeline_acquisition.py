"""Explicit, bounded acquisition of timeline evidence before in-memory eviction."""

import json
import math
import re
from datetime import datetime, timezone
from hashlib import sha256
from itertools import islice
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Mapping
from uuid import uuid4

from vntts.cleanup import attempt_cleanup

_IDENTITY_FIELDS = {
    "stage",
    "category",
    "operation",
    "backend",
    "provider",
    "completion",
    "cache_source",
    "stream_id",
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
    "bootstrap_id",
    "match_result",
    "phase",
    "worker_id",
}
# Reasons are application symbols, never user, OCR or exception text.
CONTINUATION_GATE_REASONS = frozenset(
    {
        "armed",
        "cancelled",
        "capture-failed",
        "timeout",
        "recorder-stopped",
        "ocr-uncertain",
        "clear-queue",
        "start-capture",
        "emergency-stop",
        "explicit-sequence-replacement",
        "indexed-current-recovery",
        "prefix-cursor-unavailable",
        "prefix-owner-changed",
        "prefix-render-not-quiet",
        "prefix-observation-unavailable",
        "schedule-precondition",
        "dispatch-precondition",
        "stale-auto-advance-timer",
        "ownership-changed-after-focus",
        "ownership-changed-after-render",
        "game-focus-not-owned",
        "owned-frame-not-visible-and-stable",
        "game-focus-lost-before-dispatch",
        "canonical-full-text-not-confirmed",
        "reading-control-barrier",
        "callback-blocked",
        "cursor-not-auto-advance-eligible",
        "event-route-not-terminal",
        "event-advance-already-dispatched",
        "dispatch-disabled",
        "cursor-dispatch-failed",
        "audio-preparation-failed",
        "owner-render-quiet",
        "context-enter",
        "context-exit",
        "caller-request",
        "native-probe-identity-changed",
        "native-probe-recovered",
        "qt-audio-outputs-changed",
        "output-stream-construction",
        "convenience-play",
        "convenience-stop",
        "convenience-replaced",
        "isolated-preloaded-pcm",
        "blocking-pcm-loop",
        "first-pcm-write-returned",
        "playback-started",
        "playback-completed",
        "playback-failed",
        "advance-dispatched",
        "visual-transition-confirmed",
        "transition-confirmed",
        "passive-transition-confirmed",
        "observation-anchor",
        "observation-successor",
        "observation-successor-chain",
    }
)
_NUMERIC_FIELDS = (
    frozenset(
        """
ocr_text_characters active_generation dialog_ready_generation pending_auto_advance_generation
failed_auto_advance_generation blocked_generation suppressed_generation auto_advance_enabled
already_dispatched timer_pending timer_serial current_timer_serial processed_frame_version
frame_equivalence_ms routed_frame_equivalent frame_route_epoch routed_latest_equal routed_frame_present latest_frame_present
observation_uncertain candidate_owner_present candidate_started_at stable_minimum_ms
render_owner_present render_changed_at render_quiet_minimum_ms current_chunk_present
speech_future_count deferred_chunk_present paused emergency_stopped shutting_down stop_requested
prefix_confirmation_pending prefix_owner_current cursor_present reader_present line_present
bind_frame_available capture_target_present render_quiet_ms

ocr_confidence minimum_confidence wait_ms
generation occurred_at monotonic_at clock_origin clock_elapsed_ms event_sequence run_started_at
worker_progress_age_ms worker_frame_version latest_frame_version frame_version started_monotonic
completed_monotonic window_id owner_pid success attempt underflowed generation_limited synthesis_ms
playback_ms first_audio_ms chunk_ordinal chunk_characters occurrence_id next_event_count visible
focused completion_cue recheck_interval_ms candidate_frames settled_ms ready prefetch_ms
from_text_visible_ms from_ocr_stable_ms from_generation_started_ms from_playback_started_ms
from_canonical_full_text_ms source_audio_lead_ms first_pcm_before_canonical_full_ms
source_sample_rate playback_sample_rate sample_count expected_playback_ms allowed_line_count
eligible_line_count prefix_matches speaker_matches reservation_ready normalized_text_characters
normalized_text_tokens speaker_candidate_count missing_identity_candidate_count
bounded_candidate_count best_bounded_similarity best_bounded_coverage observations
candidate_restarts fingerprint_changes ready_observations

worker_pid worker_startup_ms preload_ms frames_played callback_count elapsed_ms
first_callback_received_ms output_latency_seconds actual_sample_rate total_frames position_frames
audible_position_frames system_write_attempts system_write_failures device_reopens
max_output_lock_ms max_stream_lock_ms max_refresh_ms max_conversion_ms max_native_write_ms
sample_rate channels written_frames write_count underflow_count producer_starvation_count
first_underflow_frame max_guard_ms max_write_ms max_write_gap_ms source_samples timing_output_frames
timing_sample_rate process_id generation_owner_pid seed chunk_count first_chunk_ms
""".split()
    )
    | {
        f"{prefix}_{field}"
        for prefix in ("first_underflow", "max_write", "max_native_write")
        for field in (
            "write_index",
            "input_frame_offset",
            "input_frames",
            "sample_rate",
            "native_sample_rate",
            "native_frames",
            "started_monotonic",
            "ended_monotonic",
            "guard_ms",
            "write_gap_ms",
            "write_ms",
            "attempt",
            "output_lock_ms",
            "refresh_ms",
            "conversion_ms",
            "stream_lock_ms",
            "native_write_ms",
        )
    }
    | {
        f"{prefix}_{field}"
        for prefix in ("callback_timing", "dac_timing")
        for field in (
            "observation_count",
            "unavailable_count",
            "discontinuity_count",
            "max_gap_seconds",
            "max_offset_seconds",
            "max_offset_position_frames",
            "max_offset_output_frame",
        )
    }
)
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
        for key, value in islice(event.items(), 128):
            if key in _NUMERIC_FIELDS and isinstance(value, (bool, int)):
                if isinstance(value, bool) or value.bit_length() <= 64:
                    safe[key] = value
            elif (
                key in _NUMERIC_FIELDS
                and isinstance(value, float)
                and math.isfinite(value)
            ):
                safe[key] = value
            elif key == "reason" and isinstance(value, str):
                if value in CONTINUATION_GATE_REASONS:
                    safe[key] = value
            elif key == "source_sha256" and isinstance(value, str):
                if re.fullmatch(r"[a-f0-9]{64}", value):
                    safe[key] = value
            elif key == "fingerprint" and isinstance(value, str):
                if re.fullmatch(r"[a-f0-9]{16}", value):
                    safe[key] = value
            elif key == "owner" and isinstance(value, str) and len(value) <= 160:
                safe[key] = sha256(value.encode("utf-8", errors="replace")).hexdigest()[
                    :16
                ]
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
