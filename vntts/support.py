import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import marshal
import math
import platform
import re
import shutil
import subprocess
import sys
import threading
import traceback
import zipfile
from collections import Counter, OrderedDict, deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, fields
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import mean
from threading import RLock
from types import TracebackType
from typing import TYPE_CHECKING, Any, Callable, Mapping, TypedDict
from uuid import UUID

from vntts_artifacts.atomic_io import atomic_output_path

from vntts.audio_lifecycle import audio_lifecycle_context
from vntts.diagnostics import macos_permission_warnings
from vntts.document_identity import is_lowercase_sha256, sha256_file
from vntts.json_types import decode_json, has_schema_version
from vntts.numeric_values import optional_finite_number
from vntts.ocr_review import OCR_REVIEW_SCHEMA_VERSION, OCRReviewMetadata
from vntts.onboarding import probe_audio_output, probe_tesseract
from vntts.path_safety import open_regular_binary
from vntts.settings import AppSettings
from vntts.support_timeline_acquisition import (
    PREROUTE_DIAGNOSTIC_REASONS,
    TimelineAcquisition,
)
from vntts.versioned_json import read_versioned_json
from vntts.voice_library import VoiceLibrary

if TYPE_CHECKING:
    from vntts.reading_diagnostics import (
        ReadingDiagnosticSession,
        SavedDiagnosticSession,
    )

SupportDocument = dict[str, object]
SupportEntries = list[SupportDocument]
SupportDetails = Mapping[str, object]


class _Timeline(TypedDict):
    generation: int
    session_id: str | None
    run_id: str | None
    dropped_events: int
    events: OrderedDict[str, SupportDocument]


audio_route_fields = (
    "session_id",
    "run_id",
    "playback_id",
    "generation",
    "effective_source",
    "match_result",
    "fallback_reason",
    "voice_reference_id",
    "line_id",
    "artifact_preflight_state",
    "chunk_id",
    "chunk_ordinal",
    "chunk_characters",
)

audio_lifecycle_fields = (
    "backend",
    "process_id",
    "generation_owner_pid",
    "source_sha256",
    "source_samples",
    "source_sample_rate",
    "source_audio_lead_ms",
    "callback_timing",
    "dac_timing",
    "timing_output_frames",
    "timing_sample_rate",
    "worker_pid",
    "worker_id",
    "worker_startup_ms",
    "wait_ms",
    "ready",
    "preload_ms",
    "frames_played",
    "callback_count",
    "elapsed_ms",
    "first_callback_received_ms",
    "output_latency_seconds",
    "actual_sample_rate",
    "total_frames",
    "position_frames",
    "audible_position_frames",
    "system_write_attempts",
    "system_write_failures",
    "device_reopens",
    "max_output_lock_ms",
    "max_stream_lock_ms",
    "max_refresh_ms",
    "max_conversion_ms",
    "max_native_write_ms",
    "player_id",
    "attempt_id",
    "run_id",
    "playback_id",
    "capture_id",
    "occurred_at",
    "timing_provenance",
    "session_id",
    "generation",
    "chunk_id",
    "stream_id",
    "operation",
    "outcome",
    "owner",
    "reason",
    "host_api",
    "device_name",
    "sample_rate",
    "channels",
    "dtype",
    "latency",
    "written_frames",
    "write_count",
    "underflow_count",
    "producer_starvation_count",
    "first_underflow_frame",
    "max_guard_ms",
    "max_write_ms",
    "max_write_gap_ms",
    *(
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
    ),
)

live_scope_fields = (
    "indexed_line_count",
    "indexed_chapter_count",
    "allowed_line_count",
    "eligible_line_count",
    "plan_speech_line_count",
    "live_sequence_mode",
    "normalized_text_characters",
    "normalized_text_tokens",
    "normalized_text_sha256",
    "normalized_speaker_sha256",
    "speaker_candidate_count",
    "missing_identity_candidate_count",
    "candidate_rejection_reason",
    "speaker_canonicalized",
    "ocr_confidence",
    "correction_count",
    "exact_speaker_candidate_count",
    "normalized_speaker_candidate_count",
    "text_only_candidate_count",
    "bounded_candidate_count",
    "best_candidate_line_id",
    "best_bounded_similarity",
    "best_bounded_coverage",
    "active_pack_identity",
    "active_pregeneration_job_id",
    "active_story_index_sha256",
    "active_source_queue_sha256",
    "active_source_state_sha256",
    "active_story_line_count",
    "active_approved_count",
    "active_live_fallback_count",
)

application_error_fields = (
    "logger",
    "operation",
    "exception_type",
    "traceback",
    "thread_name",
    "source_file",
    "source_line",
)
runtime_event_fields = (
    *audio_route_fields,
    *live_scope_fields,
    *application_error_fields,
)

preroute_timeline_stages = ("ocr-attempt", "tracker-admission")

generation_timeline_stages = (
    "capture",
    *preroute_timeline_stages,
    "reading-control",
    "ocr",
    "stable-text",
    "route-decision",
    "voice-resolution",
    "generation-start",
    "canonical-full-text",
    "first-pcm",
    "audio-onset-estimate",
    "playback-start",
    "playback-completion",
    "playback-outcome",
    "key-dispatch",
    "confirmed-next-dialogue",
    "auto-advance-withheld",
    "auto-advance-timeout",
    "duplicate-chunk-suppressed",
)

# The production controller reports both audio-generation stages and guarded
# sequence-control evidence through the same callback. Replay keeps the latter
# in a separate evidence stream, but the desktop recorder must still accept it:
# telemetry must never be able to abort dialog processing.
sequence_timeline_stages = (
    "stable-frame-gate",
    "canonical-prefix-visual-recheck",
    "late-chunk-suppressed",
    "queue-cancelled",
    "observation-invalidated",
    "sequence-candidate-miss",
    "sequence-prefix-gate",
    "sequence-shadow",
    "sequence-audio-manual",
    "sequence-audio-auto",
    "sequence-explicit-expected-selection",
    "sequence-explicit-user-resync",
    "sequence-visual-transition",
    "sequence-playback-state",
    "sequence-playback-suppressed",
    "sequence-key-dispatch-authorized",
    "sequence-successor-prefetch",
    "speaker-announcement-route",
    "speaker-announcement-outcome",
)

generation_timeline_detail_fields = (
    "ocr_confidence",
    "minimum_confidence",
    "action",
    "run_id",
    "run_started_at",
    "playback_id",
    "timing_provenance",
    "onset_provenance",
    "end_provenance",
    "measurement",
    "capture_id",
    "visibility_provenance",
    "worker_stage",
    "worker_state",
    "worker_error_type",
    "worker_progress_age_ms",
    "worker_frame_version",
    "latest_frame_version",
    "frame_version",
    "started_monotonic",
    "completed_monotonic",
    "frame_age_ms",
    "ocr_call_ms",
    "attempt_ms",
    "span_ms",
    "observations",
    "committed_characters",
    "latest_characters",
    "stable_characters",
    "transition_expected",
    "route_kind",
    "phase",
    "window_id",
    "owner_pid",
    "window_bounds",
    "capture_bounds",
    "coordinate_space",
    "session_id",
    "frame_sequence",
    "layout_epoch",
    "acquired_monotonic",
    "source_monotonic",
    "clock_uncertainty_ms",
    "output_dimensions",
    "success",
    "error_type",
    "mechanism",
    "scope",
    "effective_source",
    "match_result",
    "fallback_reason",
    "voice_reference_id",
    "line_id",
    "artifact_preflight_state",
    "attempt",
    "underflowed",
    "generation_limited",
    "outcome",
    "synthesis_ms",
    "playback_ms",
    "first_audio_ms",
    "cache_source",
    "chunk_id",
    "chunk_ordinal",
    "chunk_characters",
    "state",
    "previous_event_id",
    "event_id",
    "occurrence_id",
    "candidate_event_ids",
    "next_event_count",
    "reason",
    "route",
    "terminal_route",
    "fingerprint",
    "visible",
    "focused",
    "owner",
    "completion_cue",
    "recheck_interval_ms",
    "candidate_frames",
    "settled_ms",
    "ready",
    "target_event_id",
    "prefetch_ms",
    "from_text_visible_ms",
    "from_ocr_stable_ms",
    "from_generation_started_ms",
    "from_playback_started_ms",
    "from_canonical_full_text_ms",
    "source_audio_lead_ms",
    "first_pcm_before_canonical_full_ms",
    "source_sample_rate",
    "playback_sample_rate",
    "sample_count",
    "expected_playback_ms",
    "allowed_line_count",
    "eligible_line_count",
    "prefix_matches",
    "speaker_matches",
    "reservation_ready",
    "normalized_text_characters",
    "normalized_text_tokens",
    "normalized_text_sha256",
    "normalized_speaker_sha256",
    "speaker_candidate_count",
    "missing_identity_candidate_count",
    "candidate_rejection_reason",
    "bounded_candidate_count",
    "best_candidate_line_id",
    "best_bounded_similarity",
    "best_bounded_coverage",
    "generation_owner_pid",
    "active_generation",
    "dialog_ready_generation",
    "pending_auto_advance_generation",
    "failed_auto_advance_generation",
    "blocked_generation",
    "suppressed_generation",
    "auto_advance_enabled",
    "already_dispatched",
    "timer_pending",
    "timer_serial",
    "current_timer_serial",
    "processed_frame_version",
    "frame_route_epoch",
    "routed_latest_equal",
    "routed_frame_equivalent",
    "frame_equivalence_ms",
    "routed_frame_present",
    "latest_frame_present",
    "observation_uncertain",
    "candidate_owner_present",
    "candidate_started_at",
    "stable_minimum_ms",
    "render_owner_present",
    "render_changed_at",
    "render_quiet_minimum_ms",
    "current_chunk_present",
    "speech_future_count",
    "deferred_chunk_present",
    "paused",
    "emergency_stopped",
    "shutting_down",
    "stop_requested",
    "prefix_confirmation_pending",
    "prefix_owner_current",
    "cursor_present",
    "reader_present",
    "line_present",
    "bind_frame_available",
    "capture_target_present",
    "render_quiet_ms",
)

timeline_retention_bounds_note = (
    "Retained sequence bounds may contain gaps; they do not describe a continuous "
    "retained or lost interval."
)


def _timeline_uuid(value: object | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        return UUID(str(value)).hex
    except (ValueError, AttributeError) as error:
        raise ValueError(f"Timeline {name} ID must be a UUID") from error


def _record_stable_frame_gate(existing: SupportDetails, event: SupportDocument) -> None:
    observations = _event_counter(existing, "observations")
    candidate_frames = event.get("candidate_frames")
    event["observations"] = observations + 1
    event["candidate_restarts"] = _event_counter(existing, "candidate_restarts") + int(
        observations > 0 and candidate_frames == 1
    )
    event["fingerprint_changes"] = _event_counter(
        existing, "fingerprint_changes"
    ) + int(
        observations > 0 and event.get("fingerprint") != existing.get("fingerprint")
    )
    event["ready_observations"] = _event_counter(existing, "ready_observations") + int(
        event.get("ready") is True
    )


def _sanitize_preroute_details(details: SupportDetails) -> SupportDocument:
    safe: SupportDocument = {}
    for key in (
        "started_monotonic",
        "completed_monotonic",
        "frame_version",
        "frame_age_ms",
        "ocr_call_ms",
        "attempt_ms",
        "span_ms",
        "observations",
        "committed_characters",
        "latest_characters",
        "stable_characters",
        "ocr_confidence",
        "minimum_confidence",
    ):
        value = details.get(key)
        if optional_finite_number(value) is not None:
            safe[key] = value
    if isinstance(details.get("transition_expected"), bool):
        safe["transition_expected"] = details["transition_expected"]
    for key in ("capture_id", "run_id", "route_kind", "outcome", "error_type"):
        value = details.get(key)
        if isinstance(value, str) and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value
        ):
            safe[key] = value
    if details.get("phase") in ("started", "summary"):
        safe["phase"] = details["phase"]
    reason = details.get("reason")
    if isinstance(reason, str) and reason in PREROUTE_DIAGNOSTIC_REASONS:
        safe["reason"] = reason
    return safe


class GenerationTimelineLog:
    """Keep one bounded, privacy-safe pipeline timeline per generation."""

    def __init__(
        self, maximum_entries: int = 200, *, path: str | Path | None = None
    ) -> None:
        self.maximum_entries = max(1, int(maximum_entries))
        self.path = Path(path).expanduser() if path is not None else None
        self.timelines: OrderedDict[tuple[str | None, str | None, int], _Timeline] = (
            OrderedDict()
        )
        self.clock_origin: float | None = None
        self.event_sequence = 0
        self.timeline_instances = 0
        self.evicted_timelines = 0
        self.evicted_events = 0
        self.lock = RLock()
        self.acquisition: TimelineAcquisition | None = None

    def record(
        self,
        stage: str,
        generation: str | int | float,
        occurred_at: str | int | float,
        *,
        session_id: object | None = None,
        run_id: object | None = None,
        **details: object,
    ) -> bool:
        if stage not in generation_timeline_stages + sequence_timeline_stages:
            raise ValueError(f"Unknown generation timeline stage: {stage}")
        try:
            generation = int(generation)
            occurred_at = float(occurred_at)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(
                "Timeline generation and timestamp must be numeric"
            ) from error
        if not math.isfinite(occurred_at):
            raise ValueError("Timeline timestamp must be finite")
        if generation < 0 or generation == 0 and stage not in preroute_timeline_stages:
            return False
        session_id = _timeline_uuid(session_id, "session")
        run_id = _timeline_uuid(run_id, "run")
        with self.lock:
            if self.clock_origin is None:
                self.clock_origin = occurred_at
            self.event_sequence += 1
            identity = session_id, run_id, generation
            self.timeline_instances += int(identity not in self.timelines)
            timeline = self.timelines.setdefault(
                identity,
                {
                    "generation": generation,
                    "session_id": session_id,
                    "run_id": run_id,
                    "dropped_events": 0,
                    "events": OrderedDict(),
                },
            )
            existing = next(
                (
                    event
                    for event in reversed(timeline["events"].values())
                    if event.get("stage") == stage
                ),
                {},
            )
            event = {
                "stage": stage,
                "occurred_at": occurred_at,
                "monotonic_at": occurred_at,
                "clock_origin": self.clock_origin,
                "clock_elapsed_ms": _sanitize_event_value(
                    (occurred_at - self.clock_origin) * 1000
                ),
                "event_sequence": self.event_sequence,
                **(
                    _sanitize_preroute_details(details)
                    if stage in preroute_timeline_stages
                    else {
                        key: _sanitize_event_value(details[key])
                        for key in generation_timeline_detail_fields
                        if key in details and details[key] is not None
                    }
                ),
            }
            if stage == "stable-frame-gate":
                _record_stable_frame_gate(existing, event)
            if self.acquisition is not None:
                self.acquisition.record(
                    {
                        **event,
                        "generation": generation,
                        "session_id": session_id,
                        "run_id": run_id,
                    }
                )
            # Preserve repeats/retries; a later callback cannot rewrite evidence.
            timeline["events"][str(self.event_sequence)] = event
            while len(timeline["events"]) > 512:
                timeline["events"].popitem(last=False)
                timeline["dropped_events"] += 1
                self.evicted_events += 1
            self.timelines.move_to_end(identity)
            while (
                len(self.timelines) > self.maximum_entries
                or sum(len(t["events"]) for t in self.timelines.values()) > 2000
            ):
                _, evicted = self.timelines.popitem(last=False)
                self.evicted_timelines += 1
                self.evicted_events += len(evicted["events"])
            self._persist_locked()
        return True

    def start_acquisition(
        self,
        path: str | Path,
        *,
        maximum_events: int = 20_000,
        maximum_bytes: int = 8 * 1024 * 1024,
    ) -> SupportDocument:
        """Explicit opt-in. Use a new path; stop before shutdown or another acquisition."""
        with self.lock:
            if self.acquisition is not None and self.acquisition.thread.is_alive():
                raise RuntimeError("An acquisition is already running")
            self.acquisition = TimelineAcquisition(
                path,
                maximum_events=maximum_events,
                maximum_bytes=maximum_bytes,
            )
            return self.acquisition.status()

    def stop_acquisition(self, timeout: float = 1.0) -> SupportDocument:
        # Stop this selected owner without waiting for the timeline's disk persister.
        acquisition = self.acquisition
        return (
            acquisition.stop(timeout) if acquisition is not None else {"enabled": False}
        )

    def snapshot(self) -> list[SupportDocument]:
        with self.lock:
            return [
                self._serialize_timeline(value) for value in self.timelines.values()
            ]

    def retention_summary(self) -> SupportDocument:
        with self.lock:
            sequences = [
                int(sequence)
                for timeline in self.timelines.values()
                for sequence in timeline["events"]
            ]
            return {
                "recorded_events": self.event_sequence,
                "retained_events": len(sequences),
                "evicted_events": self.evicted_events,
                "created_timeline_instances": self.timeline_instances,
                "retained_timelines": len(self.timelines),
                "evicted_timelines": self.evicted_timelines,
                "first_retained_event_sequence": min(sequences, default=None),
                "last_retained_event_sequence": max(sequences, default=None),
                "clock_origin": self.clock_origin,
                "bounds_note": timeline_retention_bounds_note,
            }

    def report(self) -> SupportDocument:
        with self.lock:
            return {
                "version": 1,
                "timelines": self.snapshot(),
                "latency_summary": self.latency_summary(),
                "retention": self.retention_summary(),
                **(
                    {"acquisition": self.acquisition.status()}
                    if self.acquisition is not None
                    else {}
                ),
            }

    def latency_summary(self) -> SupportDocument:
        """Aggregate privacy-safe live latency components across retained lines."""
        fields = {
            "visible_to_first_pcm_ms": ("first-pcm", "from_text_visible_ms"),
            "ocr_stable_to_first_pcm_ms": ("first-pcm", "from_ocr_stable_ms"),
            "generation_to_first_pcm_ms": (
                "first-pcm",
                "from_generation_started_ms",
            ),
            "playback_to_first_pcm_ms": (
                "first-pcm",
                "from_playback_started_ms",
            ),
            "canonical_full_to_first_pcm_ms": (
                "first-pcm",
                "from_canonical_full_text_ms",
            ),
            "first_pcm_before_canonical_full_ms": (
                "canonical-full-text",
                "first_pcm_before_canonical_full_ms",
            ),
            "successor_preflight_ms": (
                "sequence-successor-prefetch",
                "prefetch_ms",
            ),
            "source_audio_lead_ms": ("playback-outcome", "source_audio_lead_ms"),
            "speaker_announcement_playback_ms": (
                "speaker-announcement-outcome",
                "playback_ms",
            ),
        }
        samples: dict[str, list[float]] = {name: [] for name in fields}
        cohorts: dict[tuple[str, str, str, bool], dict[str, list[float]]] = {}
        with self.lock:
            for timeline in self.timelines.values():
                attempts: dict[object, SupportDocument] = {}
                for event in timeline["events"].values():
                    playback_id = event.get("playback_id")
                    if playback_id is not None:
                        attempts.setdefault(playback_id, {}).update(event)
                for event in timeline["events"].values():
                    attempt = attempts.get(event.get("playback_id"), {})
                    source = str(
                        attempt.get("effective_source")
                        or event.get("effective_source")
                        or "unobserved"
                    )
                    cache = str(
                        attempt.get("cache_source")
                        or event.get("cache_source")
                        or "unobserved"
                    )
                    provenance = str(
                        event.get("timing_provenance")
                        or attempt.get("onset_provenance")
                        or attempt.get("timing_provenance")
                        or "unobserved"
                    )
                    lead = optional_finite_number(attempt.get("source_audio_lead_ms"))
                    identity = (
                        source,
                        cache,
                        provenance,
                        bool(lead is not None and lead > 0),
                    )
                    for name, (stage, field) in fields.items():
                        value = optional_finite_number(event.get(field))
                        if (
                            event.get("stage") == stage
                            and value is not None
                            and value >= 0
                        ):
                            samples[name].append(value)
                            cohorts.setdefault(identity, {}).setdefault(
                                name, []
                            ).append(value)

        def summarize(values: list[float]) -> SupportDocument:
            return {
                "samples": len(values),
                "p50_ms": _percentile(values, 0.50),
                "p95_ms": _percentile(values, 0.95),
            }

        result: SupportDocument = {
            name: summarize(values) for name, values in samples.items() if values
        }
        if cohorts:
            result["cohorts"] = [
                {
                    "effective_source": source,
                    "cache_source": cache,
                    "timing_provenance": provenance,
                    "has_source_lead": has_lead,
                    "model_warmth": "unobserved",
                    "metrics": {
                        name: summarize(values) for name, values in metrics.items()
                    },
                }
                for (source, cache, provenance, has_lead), metrics in sorted(
                    cohorts.items()
                )
            ]
        return result

    def _persist_locked(self) -> None:
        if self.path is None:
            return
        try:
            from durable_file import atomic_write_json

            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.path, self.report())
        except OSError:
            pass

    @staticmethod
    def _serialize_timeline(timeline: _Timeline) -> SupportDocument:
        events = list(timeline["events"].values())
        if not events:
            result = {"generation": timeline["generation"], "events": []}
            if timeline["session_id"] is not None:
                result["session_id"] = timeline["session_id"]
            if timeline["run_id"] is not None:
                result["run_id"] = timeline["run_id"]
            return result
        occurred_at = [
            value
            for event in events
            if isinstance(value := event.get("occurred_at"), float)
        ]
        if not occurred_at:
            return {"generation": timeline["generation"], "events": []}
        started_at = min(occurred_at)
        serialized: list[SupportDocument] = []
        for event in sorted(
            events,
            key=lambda value: (
                _timeline_occurred_at(value),
                (generation_timeline_stages + sequence_timeline_stages).index(
                    str(value.get("stage", ""))
                ),
            ),
        ):
            serialized.append(
                {key: value for key, value in event.items() if key != "occurred_at"}
                | {
                    "elapsed_ms": _sanitize_event_value(
                        round((_timeline_occurred_at(event) - started_at) * 1000, 3)
                    )
                }
            )
        result = {
            "generation": timeline["generation"],
            "events": serialized,
            "dropped_events": timeline["dropped_events"],
        }
        if timeline["session_id"] is not None:
            result["session_id"] = timeline["session_id"]
        if timeline["run_id"] is not None:
            result["run_id"] = timeline["run_id"]
        return result


def _timeline_occurred_at(event: SupportDetails) -> float:
    value = event.get("occurred_at")
    return value if isinstance(value, float) else 0.0


def _event_counter(event: SupportDetails, name: str) -> int:
    value = event.get(name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _percentile(values: list[float], quantile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


class RuntimeSupportLog:
    def __init__(
        self,
        maximum_entries: int = 200,
        *,
        maximum_bytes: int = 512 * 1024,
        clock: Callable[[], datetime] | None = None,
        path: str | Path | None = None,
        detail_fields: tuple[str, ...] = runtime_event_fields,
    ) -> None:
        self.entries: deque[SupportDocument] = deque(maxlen=maximum_entries)
        self.maximum_bytes = max(256, int(maximum_bytes))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = RLock()
        self.path = Path(path).expanduser() if path is not None else None
        self.detail_fields = tuple(detail_fields)
        self.persistence_error: str | None = None

    def add(self, level: object, message: object, **details: object) -> None:
        with self.lock:
            entry: SupportDocument = {
                "recorded_at": self.clock().isoformat(),
                "level": str(level),
                "message": self._bounded_message(message),
            }
            entry.update(
                (key, details[key]) for key in self.detail_fields if key in details
            )
            self.entries.append(entry)
            if self.path is not None:
                try:
                    self._persist_locked()
                except OSError as error:
                    self.persistence_error = _redact_game_import_text(error)

    def _bounded_message(self, message: object) -> str:
        value = str(message)
        maximum_characters = max(64, min(16_384, self.maximum_bytes // 2))
        if len(value) <= maximum_characters:
            return value
        return f"{value[: maximum_characters - 15]}... <truncated>"

    def _persist_locked(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = deque(self._json_line(entry) for entry in self.entries)
        payload_size = sum(map(len, lines))
        while payload_size > self.maximum_bytes and len(self.entries) > 1:
            self.entries.popleft()
            payload_size -= len(lines.popleft())
        if payload_size > self.maximum_bytes:
            entry = self.entries[-1]
            self.entries[-1] = {
                "recorded_at": entry["recorded_at"],
                "level": entry["level"],
                "message": "<runtime event exceeded persistent log limit>",
            }
            lines[-1] = self._json_line(self.entries[-1])
        with atomic_output_path(self.path) as temporary_path:
            temporary_path.write_bytes(b"".join(lines))

    @staticmethod
    def _json_line(entry: SupportDetails) -> bytes:
        return (json.dumps(sanitize_event(entry), ensure_ascii=False) + "\n").encode(
            "utf-8"
        )

    def snapshot(self) -> SupportEntries:
        with self.lock:
            return list(self.entries)


def _read_bounded_json_lines(
    path: Path | None,
    maximum_bytes: int,
    *,
    on_error: Callable[[OSError], None] | None = None,
) -> tuple[SupportDocument, ...]:
    if path is None:
        return ()
    try:
        with open_regular_binary(path) as source:
            source.seek(0, 2)
            offset = max(0, source.tell() - maximum_bytes)
            source.seek(offset)
            payload = source.read(maximum_bytes)
    except FileNotFoundError:
        return ()
    except OSError as error:
        if on_error is not None:
            on_error(error)
        return ()
    if offset:
        payload = payload.split(b"\n", 1)[-1]
    entries: SupportEntries = []
    for line in payload.splitlines():
        try:
            entry = decode_json(line)
        except TypeError, ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return tuple(entries)


application_error_log = RuntimeSupportLog(detail_fields=application_error_fields)
_application_error_handler: logging.Handler | None = None


class _ApplicationErrorHandler(logging.Handler):
    def __init__(self, event_log: RuntimeSupportLog) -> None:
        super().__init__(logging.WARNING)
        self.event_log = event_log
        self._emitting = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._emitting, "active", False):
            return
        self._emitting.active = True
        try:
            details: dict[str, object] = {
                "logger": record.name,
                "operation": record.funcName,
                "thread_name": record.threadName,
                "source_file": record.pathname,
                "source_line": record.lineno,
            }
            if record.exc_info and record.exc_info[0] is not None:
                details["exception_type"] = record.exc_info[0].__name__
                trace = "".join(traceback.format_exception(*record.exc_info))
                if len(trace) > 12_000:
                    trace = trace[:6000] + "\n... <truncated> ...\n" + trace[-5500:]
                details["traceback"] = _redact_game_import_text(trace)
            self.event_log.add(
                record.levelname.lower(),
                _redact_game_import_text(record.getMessage()),
                **details,
            )
        except Exception as error:
            # Reporting failure must never replace the application failure.
            self.event_log.persistence_error = _redact_game_import_text(error)
        finally:
            self._emitting.active = False


def configure_application_error_log(
    path: str | Path | None = None,
) -> RuntimeSupportLog:
    global application_error_log, _application_error_handler
    root = logging.getLogger()
    if _application_error_handler is not None:
        root.removeHandler(_application_error_handler)
        _application_error_handler.close()
    application_error_log = RuntimeSupportLog(
        path=path, detail_fields=application_error_fields
    )

    def retain_read_failure(error: OSError) -> None:
        application_error_log.persistence_error = _redact_game_import_text(
            f"Unable to restore application error log: {error}"
        )
        # Preserve unreadable evidence instead of overwriting it with this run.
        application_error_log.path = None

    for entry in _read_bounded_json_lines(
        application_error_log.path,
        application_error_log.maximum_bytes,
        on_error=retain_read_failure,
    ):
        if not isinstance(entry.get("level"), str) or entry["level"] not in {
            "warning",
            "error",
            "critical",
        }:
            continue
        application_error_log.entries.append(sanitize_event(entry))
    _application_error_handler = _ApplicationErrorHandler(application_error_log)
    root.addHandler(_application_error_handler)
    return application_error_log


@contextmanager
def capture_unhandled_errors() -> Iterator[None]:
    """Keep Python/Qt callbacks and worker-thread failures in the ordinary report."""
    previous_system = sys.excepthook
    previous_thread = threading.excepthook
    previous_unraisable = sys.unraisablehook
    logger = logging.getLogger("vntts.unhandled")

    def system_hook(
        error_type: type[BaseException],
        error: BaseException,
        trace: TracebackType | None,
    ) -> None:
        if not issubclass(error_type, (KeyboardInterrupt, SystemExit)):
            logger.critical(
                "Unhandled application exception", exc_info=(error_type, error, trace)
            )
        previous_system(error_type, error, trace)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if not issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            logger.critical(
                "Unhandled worker-thread exception",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
                if args.exc_value is not None
                else None,
            )
        previous_thread(args)

    def unraisable_hook(args: Any) -> None:
        logger.error(
            "Unraisable application exception",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        previous_unraisable(args)

    sys.excepthook = system_hook
    threading.excepthook = thread_hook
    sys.unraisablehook = unraisable_hook
    try:
        yield
    except Exception:
        logger.critical(
            "Application startup, event loop or shutdown failed", exc_info=True
        )
        raise
    finally:
        sys.excepthook = previous_system
        threading.excepthook = previous_thread
        sys.unraisablehook = previous_unraisable


generation_stall_phases = frozenset(
    {
        "starting",
        "generating",
        "retrying",
        "validating",
        "publishing",
        "finalizing",
        "subprocess",
        "unknown",
    }
)
generation_stall_providers = frozenset(
    {
        "moss-tts",
        "pocket-tts",
        "chatterbox-nano",
        "qwen-tts",
        "moss-tts-delay",
        "unknown",
    }
)


def _sanitize_generation_stall(details: object) -> SupportDocument:
    if not isinstance(details, dict):
        return {}
    safe: SupportDocument = {}
    for key, allowed in (
        ("phase", generation_stall_phases),
        ("provider", generation_stall_providers),
    ):
        value = details.get(key)
        safe[key] = value if isinstance(value, str) and value in allowed else "unknown"
    for key in (
        "attempt",
        "provider_attempt",
        "seed",
        "worker_thread_id",
        "process_id",
        "capture_index",
    ):
        value = details.get(key)
        if type(value) is int and 0 <= value < 2**64:
            if key != "capture_index" or 1 <= value <= 3:
                safe[key] = value
    if is_lowercase_sha256(details.get("queue_id_sha256")):
        safe["queue_id_sha256"] = details["queue_id_sha256"]
    elapsed = optional_finite_number(details.get("elapsed_seconds"))
    if elapsed is not None and elapsed >= 0:
        safe["elapsed_seconds"] = round(elapsed, 3)
    if type(details.get("cancellation_requested")) is bool:
        safe["cancellation_requested"] = details["cancellation_requested"]
    safe["threads"] = _generation_stall_threads(details.get("threads"))
    return safe


def _generation_stall_threads(raw_threads: object) -> list[SupportDocument]:
    threads: list[SupportDocument] = []
    for thread in raw_threads[:32] if isinstance(raw_threads, list) else ():
        if not isinstance(thread, dict):
            continue
        thread_id = thread.get("thread_id")
        if type(thread_id) is not int or not 0 <= thread_id < 2**64:
            continue
        raw_frames = thread.get("frames")
        frames = _generation_stall_frames(raw_frames)
        threads.append(
            {
                "thread_id": thread_id,
                "worker": thread.get("worker") is True,
                "frames": frames,
                "truncated": thread.get("truncated") is True
                or isinstance(raw_frames, list)
                and len(raw_frames) > 64,
            }
        )
    return threads


def _generation_stall_frames(raw_frames: object) -> list[SupportDocument]:
    frames: list[SupportDocument] = []
    for frame in raw_frames[:64] if isinstance(raw_frames, list) else ():
        if not isinstance(frame, dict):
            continue
        module, filename, function = (
            frame.get(key) for key in ("module", "file", "function")
        )
        line = frame.get("line")
        if (
            not isinstance(module, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,159}", module)
            or not isinstance(function, str)
            or not re.fullmatch(
                r"(?:[A-Za-z_][A-Za-z0-9_]{0,159}|"
                r"<(?:module|lambda|listcomp|dictcomp|setcomp|genexpr)>)",
                function,
            )
            or type(line) is not int
            or not 0 <= line < 2**31
        ):
            continue
        safe_frame: SupportDocument = {
            "module": module,
            "function": function,
            "line": line,
        }
        if (
            isinstance(filename, str)
            and len(filename) <= 240
            and re.fullmatch(
                r"(?:(?:vntts|tests|scripts)/(?:[A-Za-z_][A-Za-z0-9_]*/)*|)"
                r"[A-Za-z_][A-Za-z0-9_]*\.py",
                filename,
            )
        ):
            safe_frame["file"] = filename
        frames.append(safe_frame)
    return frames


class GenerationStallLog(RuntimeSupportLog):
    """Retain bounded Python stack locations across application restarts."""

    def __init__(
        self,
        maximum_entries: int = 8,
        *,
        maximum_bytes: int = 512 * 1024,
        path: str | Path | None = None,
    ) -> None:
        super().__init__(
            maximum_entries=max(1, min(8, maximum_entries)),
            maximum_bytes=min(512 * 1024, maximum_bytes),
            path=path,
        )
        try:
            for entry in _read_bounded_json_lines(self.path, self.maximum_bytes):
                if entry.get("level") != "generation-stall":
                    continue
                safe = _sanitize_generation_stall(entry)
                timestamp = entry.get("recorded_at")
                if not isinstance(timestamp, str):
                    continue
                try:
                    timestamp = datetime.fromisoformat(timestamp).isoformat()
                except ValueError:
                    continue
                self.entries.append(self._entry(safe, timestamp))
            self._persist_locked()
        except Exception:
            # Diagnostics must not prevent application startup.
            pass

    @staticmethod
    def _entry(details: SupportDocument, timestamp: str) -> SupportDocument:
        return {
            "recorded_at": timestamp,
            "level": "generation-stall",
            "message": "Generation stalled; captured Python thread stacks",
            **details,
        }

    def record(self, **details: object) -> None:
        with self.lock:
            self.entries.append(
                self._entry(
                    _sanitize_generation_stall(details), self.clock().isoformat()
                )
            )
            try:
                self._persist_locked()
            except OSError:
                pass

    def _persist_locked(self) -> None:
        if self.entries:
            newest = self.entries[-1]
            threads = newest.get("threads")
            if isinstance(threads, list):
                # Keep the worker stack when other thread locations exceed the budget.
                threads.sort(key=lambda thread: thread.get("worker") is not True)
                while (
                    len(threads) > 1
                    and len(self._json_line(newest)) > self.maximum_bytes
                ):
                    threads.pop()
        while (
            self.entries
            and sum(len(self._json_line(entry)) for entry in self.entries)
            > self.maximum_bytes
        ):
            self.entries.popleft()
        super()._persist_locked()

    @staticmethod
    def _json_line(entry: SupportDetails) -> bytes:
        return (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")

    def report(self) -> SupportDocument:
        return {
            "schema_version": 1,
            "events": self.snapshot(),
            "limitations": [
                "Python thread locations in the application process only; "
                "native code, child process stacks, and a native call holding the "
                "GIL may be unavailable."
            ],
        }


generation_stall_log = GenerationStallLog()


def configure_generation_stall_log(
    path: str | Path | None = None,
) -> GenerationStallLog:
    global generation_stall_log
    generation_stall_log = GenerationStallLog(path=path)
    return generation_stall_log


def record_generation_stall(**details: object) -> None:
    try:
        generation_stall_log.record(**details)
    except Exception:
        pass


performance_operation_id: ContextVar[str | None] = ContextVar(
    "performance_operation_id", default=None
)

performance_fields = (
    "operation_id",
    "parent_operation",
    "timing_scope",
    "count",
    "cache_hits",
    "cache_misses",
    "registry_builds",
    "reference_reads",
    "reference_preflights",
    "candidate_count",
    "group_count",
    "operation",
    "outcome",
    "elapsed_ms",
    "cpu_ms",
    "files_examined",
    "bytes_examined",
    "cache_state",
)


class PerformanceLog(RuntimeSupportLog):
    """Retain slow app-stage timings without user content or paths."""

    def __init__(
        self,
        maximum_entries: int = 200,
        *,
        maximum_bytes: int = 512 * 1024,
        clock: Callable[[], datetime] | None = None,
        path: str | Path | None = None,
    ) -> None:
        super().__init__(
            maximum_entries=maximum_entries,
            detail_fields=performance_fields,
            maximum_bytes=maximum_bytes,
            clock=clock,
            path=path,
        )

    def record(
        self, operation: str, elapsed_ms: float, outcome: str, **details: object
    ) -> None:
        number = optional_finite_number(elapsed_ms)
        if number is None:
            return
        if outcome == "complete" and number < 100 and not details.get("operation_id"):
            return
        self.add(
            "performance",
            f"Application operation: {operation}",
            operation=operation,
            outcome=outcome,
            elapsed_ms=round(elapsed_ms, 3),
            **{key: details[key] for key in performance_fields if key in details},
        )

    def report(self) -> SupportDocument:
        events = [sanitize_event(entry) for entry in self.snapshot()]
        summary: dict[str, dict[str, int | float]] = {}
        for event in events:
            operation = event.get("operation")
            elapsed_ms = event.get("elapsed_ms")
            if (
                not isinstance(operation, str)
                or not isinstance(elapsed_ms, (int, float))
                or optional_finite_number(elapsed_ms) is None
            ):
                continue
            aggregate = summary.setdefault(
                operation, {"count": 0, "total_ms": 0.0, "max_ms": 0.0}
            )
            aggregate["count"] += 1
            aggregate["total_ms"] = round(aggregate["total_ms"] + elapsed_ms, 3)
            aggregate["max_ms"] = max(aggregate["max_ms"], elapsed_ms)
            for name in ("files_examined", "bytes_examined"):
                if isinstance(
                    value := event.get(name), (int, float)
                ) and not isinstance(value, bool):
                    aggregate[f"max_{name}"] = max(
                        aggregate.get(f"max_{name}", 0), value
                    )
        return {
            "threshold_ms": 100,
            "correlated_threshold_ms": 0,
            "summary": {
                operation: {
                    **aggregate,
                    "total_ms": _sanitize_event_value(aggregate["total_ms"]),
                }
                for operation, aggregate in summary.items()
            },
            "events": events,
        }


performance_log = PerformanceLog()


def configure_performance_log(path: str | Path | None = None) -> PerformanceLog:
    global performance_log
    performance_log = PerformanceLog(path=path)
    return performance_log


def record_background_operation(
    operation: object, elapsed_ms: object, outcome: object, **details: object
) -> None:
    try:
        if not isinstance(elapsed_ms, (str, int, float)):
            return
        performance_log.record(
            str(operation), float(elapsed_ms), str(outcome), **details
        )
    except Exception:
        pass


game_import_fields = (
    "stage",
    "outcome",
    "operation_id",
    "index",
    "reason",
    "source_bundle",
    "path",
    "exists",
    "roots",
    "config_directory",
    "audio_directory",
    "resource_root",
    "executable",
    "command_kind",
    "package_version",
    "package_revision",
    "elapsed_ms",
    "exit_code",
    "stdout_tail",
    "stderr_tail",
    "traceback_tail",
    "cancelled",
    "characters",
    "references",
    "reference_bytes",
    "duration_seconds",
    "reference_sha256",
    "missing",
    "cache_state",
    "exception_type",
)
_game_import_path_fields = frozenset(
    "source_bundle path roots config_directory audio_directory resource_root executable".split()
)
_game_import_numeric_fields = frozenset(
    "elapsed_ms exit_code characters references reference_bytes duration_seconds".split()
)


class GameImportLog(RuntimeSupportLog):
    """Small, restart-safe import trace; content and configuration stay out."""

    def __init__(
        self,
        maximum_entries: int = 200,
        *,
        maximum_bytes: int = 512 * 1024,
        clock: Callable[[], datetime] | None = None,
        path: str | Path | None = None,
    ) -> None:
        super().__init__(
            maximum_entries=maximum_entries,
            detail_fields=game_import_fields,
            maximum_bytes=maximum_bytes,
            clock=clock,
            path=path,
        )
        try:
            self._load_previous()
        except Exception:
            # A support log must not make application startup fail.
            pass

    def record(self, stage: object, **details: object) -> None:
        safe_details = {
            key: _sanitize_game_import_value(key, value)
            for key, value in details.items()
            if key in game_import_fields and key != "stage"
        }
        safe_details = {
            key: value for key, value in safe_details.items() if value is not None
        }
        safe_details["stage"] = _sanitize_game_import_value("stage", stage)
        super().add(
            "game-import",
            f"Game import: {safe_details['stage']}",
            **safe_details,
        )

    def _load_previous(self) -> None:
        for entry in _read_bounded_json_lines(self.path, self.maximum_bytes):
            if entry.get("level") != "game-import":
                continue
            stage = entry.get("stage")
            if not isinstance(stage, str) or not stage:
                continue
            restored: SupportDocument = {
                "recorded_at": str(entry.get("recorded_at", "")),
                "level": "game-import",
                "message": self._bounded_message(
                    _redact_game_import_text(entry.get("message", ""))
                ),
            }
            restored.update(
                (key, safe_value)
                for key in game_import_fields
                if key in entry
                and (safe_value := _sanitize_game_import_value(key, entry[key]))
                is not None
            )
            self.entries.append(restored)


game_import_log = GameImportLog()


def configure_game_import_log(path: str | Path | None = None) -> GameImportLog:
    """Set the application-owned persistence target without affecting imports."""
    global game_import_log
    game_import_log = GameImportLog(path=path)
    return game_import_log


def record_game_import(stage: object, **details: object) -> None:
    """Record only privacy-safe technical import evidence; never affect import work."""
    try:
        game_import_log.record(stage, **details)
    except Exception:
        pass


native_speech_context: ContextVar[SupportDocument | None] = ContextVar(
    "native_speech_context", default=None
)
_native_fields = frozenset(
    "operation outcome cache server_pid server_load_s compute request_key "
    "reference_key reference_mode seed requested_seed frame_limit audio_frames "
    "max_audio_s audio_s request_s reference reference_encoding_s prefill_s "
    "gen_s decode_s logical_key attempt_id reason stage elapsed_ms backend profile "
    "cache_source text_characters text_words reference_s reference_sample_rate "
    "reference_channels sampling resources native_version model_key model_bytes "
    "codec_bytes gpu_layers aux_cpu local_gpu aux_cpu_threads device fallback_reason "
    "capability_version vulkan_available "
    "context_size http_status quality thresholds "
    "native_error_hint exit_code reference_prepare_s http_round_trip_s "
    "response_pcm_decode_s gen_backbone_s gen_frame_decoder_s "
    "gen_input_embedding_s".split()
)
_NATIVE_DROP = object()


class NativeSpeechLog(RuntimeSupportLog):
    """Bounded detail with non-rolling counts; independent of preview ownership."""

    def __init__(
        self, maximum_entries: int = 200, *, path: str | Path | None = None
    ) -> None:
        super().__init__(
            maximum_entries=maximum_entries,
            path=path,
            detail_fields=("native",),
        )
        self.started_at = self.clock().isoformat()
        self.total_events = 0
        self.outcomes: Counter[str] = Counter()
        self.request_seconds: dict[str, float] = {}
        self.latest_runtime: SupportDocument | None = None
        self.active_requests: OrderedDict[str, SupportDocument] = OrderedDict()
        try:
            self._load_previous()
        except Exception:
            pass

    def record(self, details: SupportDetails) -> None:
        details = _sanitize_native_details(details)
        with self.lock:
            self.add(
                "moss-native",
                "MOSS native: "
                + "; ".join(
                    f"{key}={value if value is not None else 'unavailable'}"
                    for key, value in details.items()
                    if key not in {"resources", "sampling"}
                ),
                native=details,
            )
            self._accumulate(details, self.entries[-1]["recorded_at"])

    def _load_previous(self) -> None:
        for entry in _read_bounded_json_lines(self.path, self.maximum_bytes):
            if entry.get("level") != "moss-native":
                continue
            details = _sanitize_native_details(entry.get("native"))
            if not details:
                continue
            restored: SupportDocument = {
                "recorded_at": str(entry.get("recorded_at", "")),
                "level": "moss-native",
                "message": self._bounded_message(
                    _redact_game_import_text(entry.get("message", ""))
                ),
                "native": details,
            }
            self.entries.append(restored)
            self._accumulate(details, restored["recorded_at"])
        if self.entries:
            self.started_at = str(self.entries[0]["recorded_at"])

    def _accumulate(self, details: SupportDocument, recorded_at: object) -> None:
        self.total_events += 1
        operation = details.get("operation", "unknown")
        outcome = details.get("outcome", "unknown")
        key = f"{operation}/{outcome}"
        # ponytail: fixed-size aggregate labels; details retain new labels.
        if key not in self.outcomes and len(self.outcomes) >= 64:
            key = "other"
        self.outcomes[key] += 1
        seconds = details.get("request_s")
        if (
            isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
            and abs(seconds) <= sys.float_info.max
        ):
            self.request_seconds[key] = self.request_seconds.get(key, 0.0) + max(
                0, float(seconds)
            )
        if operation == "server-start":
            self.latest_runtime = details
        attempt_id = details.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            return
        if operation == "request-start":
            self.active_requests[attempt_id] = {
                **details,
                "recorded_at": recorded_at,
            }
            if len(self.active_requests) > 64:
                self.active_requests.popitem(last=False)
        elif operation == "fresh-generation":
            self.active_requests.pop(attempt_id, None)

    def report(self) -> SupportDocument:
        with self.lock:
            events = self.snapshot()
            return {
                "schema_version": 3,
                "scope": "bounded recent application processes",
                "started_at": self.started_at,
                "total_events": self.total_events,
                "retained_events": len(events),
                "dropped_events": max(0, self.total_events - len(events)),
                "outcomes": dict(self.outcomes),
                "request_seconds": {
                    key: _sanitize_event_value(round(value, 3))
                    for key, value in self.request_seconds.items()
                },
                "latest_runtime": self.latest_runtime,
                "active_requests": list(self.active_requests.values()),
                "events": [sanitize_event(entry) for entry in events],
                "limitations": [
                    "Provider complete is not preview quality acceptance; use preview-outcome.",
                    "Audio/text are excluded: acoustic quality cannot be judged from this archive.",
                    "Native generation phases require the opt-in timing build; missing values are unavailable, not zero.",
                    "Generation phases exclude prefill/codec; frame decoder includes depth-transformer, sampling and cache work, not pure kernel time.",
                    "Natural EOS exactly at the frame limit cannot be distinguished from forced stop.",
                    "Missing reference timing is not a confirmed cache hit.",
                    "HTTP round trip includes native execution; client PCM conversion is separate from native codec decode.",
                    "Resource peaks are sampled during requests, not guaranteed absolute peaks.",
                    "Resource sampling covers native C++ requests, not MLX/Pocket/Torch workers.",
                    "NVIDIA utilization/VRAM are whole-device values, including other apps.",
                ],
            }


native_speech_log = NativeSpeechLog()


def configure_native_speech_log(path: str | Path | None = None) -> NativeSpeechLog:
    global native_speech_log
    native_speech_log = NativeSpeechLog(path=path)
    return native_speech_log


def preserve_previous_session(directory: str | Path) -> SupportDocument:
    """Snapshot bounded, sanitized diagnostics left by the previous process."""
    directory = Path(directory).expanduser()
    runtime_events = [
        sanitize_event(entry)
        for entry in _read_bounded_json_lines(directory / "runtime.log", 512 * 1024)
    ]
    performance_events = [
        sanitize_event(entry)
        for entry in _read_bounded_json_lines(directory / "performance.log", 512 * 1024)
    ]
    timeline_document = (
        _read_previous_generation_timelines(directory / "generation-timelines.json")
        or {}
    )
    timelines = _previous_generation_timelines(timeline_document)
    native = NativeSpeechLog(path=directory / "native-speech.log").report()
    audio = AudioLifecycleLog(
        path=directory / "audio-lifecycle.log", load_existing=True
    ).report()
    if not (
        runtime_events
        or performance_events
        or timelines
        or native["total_events"]
        or audio["events"]
    ):
        return {"available": False}
    snapshot = {
        "available": True,
        "schema_version": 1,
        "preserved_at": datetime.now(timezone.utc).isoformat(),
        "runtime_events": runtime_events,
        "performance_events": performance_events,
        "generation_timelines": timelines,
        "generation_timeline_retention": _sanitize_timeline_retention(
            timeline_document.get("retention")
        ),
        "native_speech": native,
        "audio_lifecycle": audio,
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with atomic_output_path(directory / "previous-session.json") as temporary_path:
            temporary_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    except OSError:
        pass
    return snapshot


def _previous_generation_timelines(document: SupportDocument) -> list[SupportDocument]:
    timelines = document.get("timelines", [])
    assert isinstance(timelines, list)
    return [
        sanitized
        for timeline in timelines[-200:]
        if (sanitized := _sanitize_previous_timeline(timeline)) is not None
    ]


def _sanitize_timeline_retention(value: object) -> SupportDocument:
    if not isinstance(value, dict):
        return {}
    result: SupportDocument = {
        key: number
        for key in (
            "recorded_events",
            "retained_events",
            "evicted_events",
            "created_timeline_instances",
            "retained_timelines",
            "evicted_timelines",
            "first_retained_event_sequence",
            "last_retained_event_sequence",
        )
        if isinstance(number := value.get(key), int)
        and not isinstance(number, bool)
        and number >= 0
    }
    result["clock_origin"] = optional_finite_number(value.get("clock_origin"))
    result["bounds_note"] = timeline_retention_bounds_note
    return result


def _read_previous_generation_timelines(path: Path) -> dict[str, object] | None:
    if path.is_symlink():
        return None
    limit = 8 * 1024 * 1024
    try:
        with open_regular_binary(path) as source:
            payload = source.read(limit + 1)
    except OSError:
        return None
    if len(payload) > limit:
        return None
    try:
        document = decode_json(payload)
    except UnicodeError, json.JSONDecodeError:
        return None
    if not isinstance(document, dict) or not isinstance(
        document.get("timelines"), list
    ):
        return None
    return document


def _sanitize_previous_timeline(timeline: object) -> SupportDocument | None:
    if not isinstance(timeline, dict):
        return None
    generation = timeline.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
    ):
        return None
    events = _sanitize_previous_events(timeline.get("events"))
    if generation == 0:
        events = [
            event for event in events if event["stage"] in preroute_timeline_stages
        ]
        if not events:
            return None
    sanitized: SupportDocument = {"generation": generation, "events": events}
    dropped = timeline.get("dropped_events")
    if isinstance(dropped, int) and not isinstance(dropped, bool) and dropped >= 0:
        sanitized["dropped_events"] = dropped
    for field in ("session_id", "run_id"):
        value = timeline.get(field)
        if value is not None:
            try:
                sanitized[field] = UUID(str(value)).hex
            except ValueError, AttributeError:
                return None
    return sanitized


def _sanitize_previous_events(raw_events: object) -> list[SupportDocument]:
    if not isinstance(raw_events, list):
        return []
    return [
        sanitized
        for event in raw_events[-512:]
        if (sanitized := _sanitize_previous_event(event)) is not None
    ]


def _sanitize_previous_event(event: object) -> SupportDocument | None:
    if not isinstance(event, dict) or event.get("stage") not in (
        generation_timeline_stages + sequence_timeline_stages
    ):
        return None
    details = (
        _sanitize_preroute_details(event)
        if event["stage"] in preroute_timeline_stages
        else {
            key: _sanitize_event_value(event[key])
            for key in generation_timeline_detail_fields
            if key in event and event[key] is not None
        }
    )
    return {
        "stage": event["stage"],
        **{
            key: _sanitize_event_value(event[key])
            for key in (
                "elapsed_ms",
                "monotonic_at",
                "clock_origin",
                "clock_elapsed_ms",
                "event_sequence",
            )
            if key in event and event[key] is not None
        },
        **details,
    }


def record_native_speech(**details: object) -> None:
    details = {**(native_speech_context.get() or {}), **details}
    try:
        native_speech_log.record(
            {key: value for key, value in details.items() if key in _native_fields}
        )
    except Exception:
        # Diagnostics must never turn a successful render into a failure.
        pass


class AudioLifecycleLog(RuntimeSupportLog):
    def __init__(
        self,
        maximum_entries: int = 400,
        *,
        path: str | Path | None = None,
        load_existing: bool = False,
    ) -> None:
        super().__init__(
            maximum_entries=maximum_entries,
            path=path,
            detail_fields=audio_lifecycle_fields,
        )
        self.persistence_initialized = self.path is None or bool(load_existing)
        if load_existing:
            for entry in _read_bounded_json_lines(self.path, self.maximum_bytes):
                if entry.get("level") == "audio-lifecycle":
                    self.entries.append(sanitize_event(entry))

    def _persist_locked(self) -> None:
        if self.path is None:
            return
        try:
            existing_bytes = self.path.stat().st_size
        except OSError:
            existing_bytes = 0
        latest = self._json_line(self.entries[-1])
        if (
            not self.persistence_initialized
            or existing_bytes + len(latest) > self.maximum_bytes
        ):
            super()._persist_locked()
            self.persistence_initialized = True
            return
        with self.path.open("ab") as output:
            output.write(latest)

    def record(self, operation: object, **details: object) -> None:
        fields = {**(audio_lifecycle_context.get() or {}), **details}
        fields["operation"] = str(operation)[:64]
        self.add(
            "audio-lifecycle",
            f"Audio stream: {fields['operation']}",
            **{
                key: _sanitize_audio_lifecycle_value(value, key)
                for key, value in fields.items()
                if key in audio_lifecycle_fields and value is not None
            },
        )

    def report(self) -> SupportDocument:
        return {"schema_version": 1, "events": self.snapshot()}


def _sanitize_audio_lifecycle_value(value: object, key: str | None = None) -> object:
    if key in {"callback_timing", "dac_timing"}:
        if not isinstance(value, Mapping):
            return None
        safe: SupportDocument = {}
        for field in (
            "observation_count",
            "unavailable_count",
            "discontinuity_count",
            "max_offset_position_frames",
            "max_offset_output_frame",
        ):
            number = value.get(field)
            if type(number) is int and 0 <= number < 2**64:
                safe[field] = number
        for field in ("max_gap_seconds", "max_offset_seconds"):
            number = value.get(field)
            if (
                isinstance(number, (int, float))
                and not isinstance(number, bool)
                and (not isinstance(number, int) or number.bit_length() <= 64)
                and math.isfinite(number)
                and (field != "max_gap_seconds" or number >= 0)
            ):
                safe[field] = round(number, 9)
        return safe
    if key == "source_sha256":
        return value if is_lowercase_sha256(value) else None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 9) if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return None
    return _redact_game_import_text(value)[:256]


audio_lifecycle_log = AudioLifecycleLog()


def configure_audio_lifecycle_log(
    path: str | Path | None = None,
) -> AudioLifecycleLog:
    global audio_lifecycle_log
    audio_lifecycle_log = AudioLifecycleLog(path=path)
    return audio_lifecycle_log


def record_audio_lifecycle(operation: object, **details: object) -> None:
    fields = {**(audio_lifecycle_context.get() or {}), **details}
    try:
        audio_lifecycle_log.record(operation, **fields)
    except Exception:
        pass
    try:
        from vntts.reading_diagnostics import record_diagnostic_event

        record_diagnostic_event("audio", str(operation)[:64], **fields)
    except Exception:
        # The optional journal must not interrupt playback or its ordinary log.
        pass


_PERSISTED_SUPPORT_READ_LIMIT = 2 * 1024 * 1024


class PregenerationSupportState:
    """Persist the latest bounded preparation failure for a later support export."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        self.lock = RLock()
        self.latest: SupportDocument | None = self._load()

    def record(
        self,
        operation: object,
        error: object,
        *,
        job: object | None = None,
        generation_input: object | None = None,
        voice_plan: object | None = None,
        state_path: str | Path | None = None,
    ) -> SupportDocument:
        snapshot: SupportDocument = {
            "schema_version": 1,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "operation": str(operation)[:160],
            "error": _redact_game_import_text(error),
            "job": _pregeneration_job_summary(job),
            "input": _pregeneration_input_summary(generation_input),
            "voice": _pregeneration_voice_summary(voice_plan),
            "generation_state": _pregeneration_state_summary(state_path),
        }
        with self.lock:
            self.latest = snapshot
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with atomic_output_path(self.path) as temporary_path:
                        temporary_path.write_text(
                            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                except OSError:
                    pass
        return snapshot

    def report(self) -> SupportDocument:
        with self.lock:
            return self.latest or {"available": False}

    def _load(self) -> SupportDocument | None:
        if self.path is None:
            return None
        if self.path.is_symlink():
            return None
        try:
            with open_regular_binary(self.path) as source:
                payload = source.read(_PERSISTED_SUPPORT_READ_LIMIT + 1)
        except OSError:
            return None
        if len(payload) > _PERSISTED_SUPPORT_READ_LIMIT:
            return None
        try:
            document = decode_json(payload)
        except UnicodeError, json.JSONDecodeError:
            return None
        return _loaded_pregeneration_support(document)


pregeneration_support = PregenerationSupportState()


def configure_pregeneration_support(
    path: str | Path | None = None,
) -> PregenerationSupportState:
    global pregeneration_support
    pregeneration_support = PregenerationSupportState(path)
    return pregeneration_support


def record_pregeneration_failure(
    *args: object, **kwargs: object
) -> SupportDocument | None:
    fields = {
        "operation",
        "error",
        "job",
        "generation_input",
        "voice_plan",
        "state_path",
    }
    if len(args) > 2 or set(kwargs) - fields:
        return None
    missing = object()
    operation = args[0] if args else kwargs.get("operation", missing)
    error = args[1] if len(args) == 2 else kwargs.get("error", missing)
    if (
        operation is missing
        or error is missing
        or (args and "operation" in kwargs)
        or (len(args) == 2 and "error" in kwargs)
    ):
        return None
    state_path = kwargs.get("state_path")
    if state_path is not None and not isinstance(state_path, (str, Path)):
        return None
    try:
        return pregeneration_support.record(
            operation,
            error,
            job=kwargs.get("job"),
            generation_input=kwargs.get("generation_input"),
            voice_plan=kwargs.get("voice_plan"),
            state_path=state_path,
        )
    except Exception:
        # Support evidence must never replace the user-facing preparation error.
        return None


def _pregeneration_job_summary(job: object | None) -> SupportDocument:
    if job is None:
        return {"available": False}
    selected_story_ids = tuple(getattr(job, "selected_story_ids", ()))
    return {
        "available": True,
        "job_id": _plain_support_value(getattr(job, "job_id", None)),
        "status": _plain_support_value(getattr(job, "status", None)),
        "provider_id": _plain_support_value(getattr(job, "provider_id", None)),
        "story_index_sha256": _sha256_support_value(
            getattr(job, "story_index_sha256", None)
        ),
        "selected_story_ids": [
            _plain_support_value(value) for value in selected_story_ids[:64]
        ],
        "selected_story_ids_truncated": max(0, len(selected_story_ids) - 64),
        "selected_line_count": len(tuple(getattr(job, "selected_line_ids", ()))),
    }


def _pregeneration_input_summary(
    generation_input: object | None,
) -> SupportDocument:
    if generation_input is None:
        return {"available": False}
    return {
        "available": True,
        "identity": _sha256_support_value(getattr(generation_input, "identity", None)),
        "queue_sha256": _sha256_support_value(
            getattr(generation_input, "queue_sha256", None)
        ),
        "queue_items": _nonnegative_support_int(
            getattr(generation_input, "queue_items", None)
        ),
        "ready_items": _nonnegative_support_int(
            getattr(generation_input, "ready_items", None)
        ),
    }


def _pregeneration_voice_summary(voice_plan: object | None) -> SupportDocument:
    if voice_plan is None:
        return {"available": False}
    return {
        "available": True,
        "backend": _plain_support_value(getattr(voice_plan, "synthesis_backend", None)),
        "model": _plain_support_value(getattr(voice_plan, "synthesis_model", None)),
        "profile": _plain_support_value(getattr(voice_plan, "synthesis_profile", None)),
        "controls_sha256": _sha256_support_value(
            getattr(voice_plan, "synthesis_controls_sha256", None)
        ),
    }


def _pregeneration_state_summary(path: str | Path | None) -> SupportDocument:
    if path is None:
        return {"available": False}
    path = Path(path)
    if path.is_symlink():
        return {"available": False, "reason": "state path is unsafe"}
    limit = 32 * 1024 * 1024
    try:
        with open_regular_binary(path) as source:
            payload = source.read(limit + 1)
    except FileNotFoundError:
        return {"available": False, "reason": "state is not present"}
    except OSError:
        return {"available": False, "reason": "state could not be read"}
    if len(payload) > limit:
        return {"available": False, "reason": "state exceeds support read limit"}
    try:
        document: object
        from vntts.generation_database import read_snapshot

        document = read_snapshot(payload)
    except UnicodeError, ValueError:
        return {"available": False, "reason": "state could not be read"}
    if not isinstance(document, dict):
        return {"available": False, "reason": "state items are invalid"}
    items = document.get("items")
    if not isinstance(items, dict):
        return {"available": False, "reason": "state items are invalid"}
    return {
        "available": True,
        "state_sha256": hashlib.sha256(payload).hexdigest(),
        "queue_sha256": _sha256_support_value(document.get("queue_sha256")),
        **_pregeneration_item_summary(items),
    }


def _pregeneration_item_summary(items: dict[str, object]) -> SupportDocument:
    statuses: Counter[str] = Counter()
    failures: list[SupportDocument] = []
    failed_item_count = 0
    for queue_id, item in sorted(items.items()):
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if isinstance(status, str):
            statuses[status] += 1
        if isinstance(queue_id, str) and status == "failed":
            failed_item_count += 1
            if len(failures) < 64:
                failures.append(_pregeneration_failure_summary(queue_id, item))
    return {
        "status_counts": dict(sorted(statuses.items())),
        "failed_items": failures,
        "failed_items_truncated": max(0, failed_item_count - 64),
    }


def _pregeneration_failure_summary(
    queue_id: str, item: SupportDocument
) -> SupportDocument:
    failure = _support_mapping(item.get("failure"))
    repair = _support_mapping(item.get("failure_repair"))
    source = _support_mapping(repair.get("source_failure"))
    carry = _support_mapping(item.get("carry_forward"))
    attempts = item.get("attempts_by_provider")
    return {
        "queue_id": _plain_support_value(queue_id),
        "line_id": _plain_support_value(item.get("line_id")),
        "speaker": _plain_support_value(item.get("speaker")),
        "requested_voice": _plain_support_value(item.get("requested_voice_character")),
        "effective_voice": _plain_support_value(item.get("voice_character")),
        "provider": _plain_support_value(item.get("provider")),
        "model": _plain_support_value(item.get("model")),
        "profile": _plain_support_value(item.get("generation_profile")),
        "attempts": _nonnegative_support_int(item.get("attempts")),
        "attempts_by_provider": {
            _plain_support_value(provider): _nonnegative_support_int(count)
            for provider, count in sorted(attempts.items())
            if isinstance(provider, str)
        }
        if isinstance(attempts, dict)
        else {},
        "failure_kind": _plain_support_value(failure.get("kind")),
        "failure_completion": _plain_support_value(failure.get("completion")),
        "failure_error_type": _plain_support_value(failure.get("error_type")),
        "repair_strategy": _plain_support_value(repair.get("strategy")),
        "source_provider": _plain_support_value(source.get("source_provider")),
        "source_failure_kind": _plain_support_value(source.get("source_failure_kind")),
        "source_repair_strategy": _plain_support_value(
            source.get("source_repair_strategy")
        ),
        "carry_source_provider": _plain_support_value(carry.get("source_provider")),
        "carry_source_failure_kind": _plain_support_value(
            carry.get("source_failure_kind")
        ),
    }


def _plain_support_value(value: object) -> str | None:
    if value is None:
        return None
    value = str(value)
    return (
        "<path>"
        if _looks_like_local_path(value)
        else _redact_game_import_text(value)[:1024]
    )


def _sha256_support_value(value: object) -> str | None:
    value = str(value or "")
    return value if is_lowercase_sha256(value) else None


def _nonnegative_support_int(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _support_mapping(value: object) -> SupportDocument:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _loaded_pregeneration_support(document: object) -> SupportDocument | None:
    source = _support_mapping(document)
    if not has_schema_version(source, 1):
        return None
    return {
        "schema_version": 1,
        "recorded_at": _plain_support_value(source.get("recorded_at")),
        "operation": _plain_support_value(source.get("operation")),
        "error": _plain_support_value(source.get("error")),
        "job": _loaded_pregeneration_section(
            source.get("job"),
            strings=("job_id", "status", "provider_id"),
            hashes=("story_index_sha256",),
            integers=("selected_line_count", "selected_story_ids_truncated"),
            lists=("selected_story_ids",),
        ),
        "input": _loaded_pregeneration_section(
            source.get("input"),
            hashes=("identity", "queue_sha256"),
            integers=("queue_items", "ready_items"),
        ),
        "voice": _loaded_pregeneration_section(
            source.get("voice"),
            strings=("backend", "model", "profile"),
            hashes=("controls_sha256",),
        ),
        "generation_state": _loaded_pregeneration_state(source.get("generation_state")),
    }


def _loaded_pregeneration_section(
    value: object,
    *,
    strings: tuple[str, ...] = (),
    hashes: tuple[str, ...] = (),
    integers: tuple[str, ...] = (),
    lists: tuple[str, ...] = (),
) -> SupportDocument:
    source = _support_mapping(value)
    if not source:
        return {"available": False}
    result: SupportDocument = {"available": source.get("available") is True}
    result.update({field: _plain_support_value(source.get(field)) for field in strings})
    result.update({field: _sha256_support_value(source.get(field)) for field in hashes})
    result.update(
        {field: _nonnegative_support_int(source.get(field)) for field in integers}
    )
    for field in lists:
        values = source.get(field)
        result[field] = (
            [_plain_support_value(item) for item in values[:64]]
            if isinstance(values, list)
            else []
        )
    return result


def _loaded_pregeneration_state(value: object) -> SupportDocument:
    source = _support_mapping(value)
    if not source:
        return {"available": False}
    result = _loaded_pregeneration_section(
        source,
        strings=("reason",),
        hashes=("state_sha256", "queue_sha256"),
        integers=("failed_items_truncated",),
    )
    raw_counts = source.get("status_counts")
    status_counts: dict[str, int] = {}
    for key, count in _support_mapping(raw_counts).items():
        safe_key = _plain_support_value(key)
        if (
            safe_key is not None
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
        ):
            status_counts[safe_key] = count
    result["status_counts"] = status_counts
    raw_failures = source.get("failed_items")
    result["failed_items"] = (
        [_loaded_pregeneration_failure(item) for item in raw_failures[:64]]
        if isinstance(raw_failures, list)
        else []
    )
    return result


def _loaded_pregeneration_failure(value: object) -> SupportDocument:
    source = _support_mapping(value)
    fields = (
        "queue_id",
        "line_id",
        "speaker",
        "requested_voice",
        "effective_voice",
        "provider",
        "model",
        "profile",
        "failure_kind",
        "failure_completion",
        "failure_error_type",
        "repair_strategy",
        "source_provider",
        "source_failure_kind",
        "source_repair_strategy",
        "carry_source_provider",
        "carry_source_failure_kind",
    )
    result: SupportDocument = {
        field: _plain_support_value(source.get(field)) for field in fields
    }
    result["attempts"] = _nonnegative_support_int(source.get("attempts"))
    raw_attempts = source.get("attempts_by_provider")
    result["attempts_by_provider"] = {
        _plain_support_value(provider): _nonnegative_support_int(count)
        for provider, count in _support_mapping(raw_attempts).items()
    }
    return result


def collect_active_content_identity(settings: AppSettings) -> SupportDocument:
    """Describe the active prepared content without exporting its local paths."""
    pack_path = settings.game_pack
    if pack_path:
        try:
            path = Path(pack_path).expanduser()
            stat = path.stat()
            return _active_pack_identity(str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass
    story_path = settings.story_index
    if story_path:
        try:
            path = Path(story_path).expanduser()
            return {
                "available": True,
                "active_story_index_sha256": sha256_file(path),
            }
        except OSError:
            pass
    return {"available": False}


def correlate_active_preparation(
    active: SupportDocument, preparation: SupportDocument
) -> SupportDocument:
    """Explain whether the saved pack came from the preparation that failed."""
    job = preparation.get("job")
    generation_input = preparation.get("input")
    if not active.get("available") or not isinstance(job, dict):
        return {"classification": "insufficient-evidence"}
    selected_story_ids = job.get("selected_story_ids")
    active_story_ids = active.get("active_story_ids")
    generation_state = preparation.get("generation_state")
    selected_stories_present: bool | None = None
    if (
        isinstance(selected_story_ids, list)
        and selected_story_ids
        and all(isinstance(value, str) for value in selected_story_ids)
        and isinstance(active_story_ids, list)
        and active_story_ids
        and all(isinstance(value, str) for value in active_story_ids)
    ):
        selected_truncated = _nonnegative_support_int(
            job.get("selected_story_ids_truncated")
        )
        # Old reports did not distinguish exactly 64 selected stories from truncation.
        if selected_truncated is None and len(selected_story_ids) < 64:
            selected_truncated = 0
        if set(selected_story_ids).issubset(active_story_ids):
            if selected_truncated == 0:
                selected_stories_present = True
        elif _nonnegative_support_int(active.get("active_story_ids_truncated", 0)) == 0:
            selected_stories_present = False
    comparisons: dict[str, bool | None] = {
        "selected_stories_present": selected_stories_present,
        "same_job": (
            active.get("active_pregeneration_job_id") == job.get("job_id")
            if active.get("active_pregeneration_job_id") and job.get("job_id")
            else None
        ),
        "same_source_queue": (
            active.get("active_source_queue_sha256")
            == generation_input.get("queue_sha256")
            if isinstance(generation_input, dict)
            and active.get("active_source_queue_sha256")
            and generation_input.get("queue_sha256")
            else None
        ),
        "same_source_state": (
            active.get("active_source_state_sha256")
            == generation_state.get("state_sha256")
            if isinstance(generation_state, dict)
            and active.get("active_source_state_sha256")
            and generation_state.get("state_sha256")
            else None
        ),
    }
    known = tuple(value for value in comparisons.values() if value is not None)
    classification = (
        "same-preparation"
        if known and all(known)
        else "different-preparation"
        if False in known
        else "insufficient-evidence"
    )
    return {"classification": classification, **comparisons}


_ACTIVE_CONTENT_READ_LIMIT = 64 * 1024 * 1024


@lru_cache(maxsize=16)
def _active_pack_identity(path: str, _modified_ns: int, size: int) -> SupportDocument:
    if size > _ACTIVE_CONTENT_READ_LIMIT:
        return {
            "available": False,
            "reason": "pack manifest exceeds support read limit",
        }
    try:
        with open_regular_binary(path) as source:
            payload = source.read(_ACTIVE_CONTENT_READ_LIMIT + 1)
    except OSError, UnicodeError, json.JSONDecodeError:
        return {"available": False, "reason": "pack manifest could not be read"}
    if len(payload) > _ACTIVE_CONTENT_READ_LIMIT:
        return {
            "available": False,
            "reason": "pack manifest exceeds support read limit",
        }
    try:
        document = decode_json(payload)
    except UnicodeError, json.JSONDecodeError:
        return {"available": False, "reason": "pack manifest could not be read"}
    if not isinstance(document, dict):
        return {"available": False, "reason": "pack manifest is invalid"}
    extension = document.get("vntts.self-service")
    extension = extension if isinstance(extension, dict) else {}
    components = document.get("components")
    components = components if isinstance(components, dict) else {}
    story = components.get("story_index")
    story = story if isinstance(story, dict) else {}
    story_ids = _active_story_ids(Path(path).resolve().parent, story)
    return {
        "available": True,
        "active_pack_identity": _sha256_support_value(extension.get("identity")),
        "active_pregeneration_job_id": _plain_support_value(extension.get("job_id")),
        "active_story_index_sha256": _sha256_support_value(story.get("sha256")),
        "active_source_queue_sha256": _sha256_support_value(
            extension.get("source_queue_sha256")
        ),
        "active_source_state_sha256": _sha256_support_value(
            extension.get("source_state_sha256")
        ),
        "active_story_line_count": _nonnegative_support_int(
            extension.get("story_line_count")
        ),
        **story_ids,
        "active_approved_count": _nonnegative_support_int(
            extension.get("approved_count")
        ),
        "active_live_fallback_count": _nonnegative_support_int(
            extension.get("live_fallback_count")
        ),
    }


def _active_story_ids(root: Path, component: SupportDocument) -> SupportDocument:
    relative = component.get("path")
    if not isinstance(relative, str) or not relative:
        return {"active_story_ids_available": False}
    try:
        path = (root / relative).resolve()
        path.relative_to(root)
        if path.stat().st_size > _ACTIVE_CONTENT_READ_LIMIT:
            raise ValueError("story index exceeds support read limit")
        with open_regular_binary(path) as source:
            payload = source.read(_ACTIVE_CONTENT_READ_LIMIT + 1)
        if len(payload) > _ACTIVE_CONTENT_READ_LIMIT:
            raise ValueError("story index exceeds support read limit")
        story_ids: set[str] = set()
        for line in payload.splitlines():
            record: object = decode_json(line)
            if not isinstance(record, dict):
                continue
            collection_id = record.get("collection_id")
            if (
                record.get("record_type") != "metadata"
                and isinstance(collection_id, str)
                and collection_id
            ):
                story_ids.add(collection_id)
    except OSError, ValueError:
        return {"active_story_ids_available": False}
    values = sorted(story_ids)
    return {
        "active_story_ids_available": True,
        "active_story_ids": values[:256],
        "active_story_ids_truncated": max(0, len(values) - 256),
    }


class SupportBundleBuilder:
    def __init__(
        self,
        settings: AppSettings,
        event_log: RuntimeSupportLog,
        *,
        diagnostic: object | None = None,
        dependency_probe: Callable[[], SupportDocument] | None = None,
        generation_timelines: GenerationTimelineLog | None = None,
        game_import_log: GameImportLog | None = None,
        performance_log_value: PerformanceLog | None = None,
        previous_session: SupportDocument | None = None,
        audio_lifecycle: AudioLifecycleLog | None = None,
        voice_library: VoiceLibrary | None = None,
        diagnostic_session: "ReadingDiagnosticSession | SavedDiagnosticSession | None" = None,
    ) -> None:
        self.settings = settings
        self.event_log = event_log
        self.diagnostic = diagnostic
        self.dependency_probe = dependency_probe or collect_dependency_status
        self.generation_timelines = generation_timelines
        self.game_import_log = game_import_log
        self.performance_log = performance_log_value
        self.previous_session = previous_session or {"available": False}
        self.audio_lifecycle = audio_lifecycle or AudioLifecycleLog()
        self.voice_library = voice_library
        self.diagnostic_session = diagnostic_session

    def build(self, path: str | Path) -> Path:
        path = Path(path).expanduser()
        if path.suffix.casefold() != ".zip":
            path = path.with_suffix(".zip")
        path.parent.mkdir(parents=True, exist_ok=True)
        session_entries = (
            self.diagnostic_session.export_entries()
            if self.diagnostic_session is not None
            else ()
        )
        if session_entries:
            required = sum(source.stat().st_size for _, source in session_entries)
            if shutil.disk_usage(path.parent).free < required * 1.01 + 512 * 1024**2:
                raise OSError(
                    "Not enough free disk space to export diagnostic evidence"
                )
        active_content = collect_active_content_identity(self.settings)
        preparation = pregeneration_support.report()
        files = {
            "manifest.json": {
                "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "privacy": (
                    "Explicit diagnostic session evidence is included. Selected recordings "
                    "and source WAVs may contain game images, dialogue and voice audio. "
                    "If media recording was selected, evidence may also include recognized "
                    "text and matched story dialogue, even if media recording failed. "
                    "Model files and environment-variable values are excluded."
                    if self.diagnostic_session is not None
                    else "Screenshots, recognized dialogue, voice audio, model files, "
                    "and environment-variable values are excluded."
                ),
            },
            "sanitized-settings.json": sanitize_settings(self.settings),
            "active-content.json": active_content,
            "pregeneration.json": {
                **preparation,
                "active_content_correlation": correlate_active_preparation(
                    active_content, preparation
                ),
            },
            "runtime-events.json": {
                "events": [sanitize_event(entry) for entry in self.event_log.snapshot()]
            },
            "application-errors.json": {
                "events": [
                    sanitize_event(entry) for entry in application_error_log.snapshot()
                ],
                "persistence_error": application_error_log.persistence_error,
            },
            "game-import.json": {
                "events": [
                    sanitize_event(entry)
                    for entry in (self.game_import_log or game_import_log).snapshot()
                ]
            },
            "performance.json": (self.performance_log or performance_log).report(),
            "generation-stalls.json": generation_stall_log.report(),
            "native-speech.json": {
                **native_speech_log.report(),
                "timing_note": (
                    "Seconds; gen includes backbone and auxiliary depth decoder. "
                    "request_s covers the native request path through PCM, including "
                    "any server restart; it excludes preview validation and playback. "
                    "Preview elapsed_ms includes reference checks, startup and validation. "
                    "Unavailable is not zero. Cached WAV playback is not generation."
                    " Request/reference keys correlate inputs only within one backend "
                    "instance; they are salted and contain no text or paths. "
                    "Seed is the effective native seed (requested zero maps to one). "
                    "Registered references do not by themselves prove a cache hit. "
                    "Limited means the frame cap was reached, not an acoustic verdict."
                ),
            },
            "build.json": collect_build_identity(),
            "generation-timelines.json": (
                self.generation_timelines.report()
                if self.generation_timelines is not None
                else {
                    "version": 1,
                    "timelines": [],
                    "latency_summary": {},
                    "retention": {},
                }
            ),
            "previous-session.json": self.previous_session,
            "audio-lifecycle.json": self.audio_lifecycle.report(),
            "ocr-metrics.json": collect_ocr_metrics(
                self.settings.ocr_diagnostics_directory
            ),
            "diagnostics.json": sanitize_diagnostic(self.diagnostic),
            "voice-bindings.json": collect_voice_bindings(self.voice_library),
            "dependencies.json": self.dependency_probe(),
        }
        with atomic_output_path(path) as temporary_path:
            with zipfile.ZipFile(
                temporary_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for filename, payload in files.items():
                    archive.writestr(
                        filename,
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    )
                for filename, source in session_entries:
                    with (
                        open_regular_binary(source) as original,
                        archive.open(filename, "w", force_zip64=True) as output,
                    ):
                        shutil.copyfileobj(original, output)
        return path


def collect_voice_bindings(library: VoiceLibrary | None) -> SupportDocument:
    """Expose effective voice decisions without exporting paths or audio."""
    if library is None:
        return {"available": False}
    try:
        bindings = library.bindings()
    except (OSError, ValueError) as error:
        return {"available": False, "reason": _plain_support_value(error)}
    evidence_fields = {
        "resolution",
        "selected_in",
        "selected_character",
        "source_id",
        "source_character",
        "speaker",
        "decision_context_sha256",
    }
    return {
        "available": True,
        "bindings": [
            {
                "role": binding.role,
                "variant_key": binding.variant_key,
                "route": binding.route,
                "source_id": binding.source_id,
                "source_sha256s": list(binding.source_sha256s),
                "provenance": {
                    "method": binding.provenance.get("method"),
                    "algorithm": binding.provenance.get("algorithm"),
                    "timestamp": binding.provenance.get("timestamp"),
                    "evidence": {
                        key: value
                        for key, value in _support_mapping(
                            binding.provenance.get("evidence")
                        ).items()
                        if key in evidence_fields
                    },
                },
            }
            for binding in bindings
        ],
    }


def sanitize_settings(settings: AppSettings) -> SupportDocument:
    values = asdict(settings)
    for definition in fields(settings):
        sensitivity = definition.metadata.get("support_sensitivity")
        value = values.get(definition.name)
        if not value or sensitivity is None:
            continue
        if sensitivity == "path":
            values[definition.name] = "<path>"
        elif sensitivity == "path-or-id" and _looks_like_local_path(value):
            values[definition.name] = "<path>"
    return values


def _looks_like_local_path(value: object) -> bool:
    value = str(value).strip()
    return bool(
        value.startswith(("/", "\\", "~", "./", "../"))
        or re.match(r"(?i)^[a-z]:[\\/]", value)
    )


def sanitize_event(entry: SupportDetails) -> SupportDocument:
    sanitized = {
        "recorded_at": entry.get("recorded_at"),
        "level": entry.get("level"),
        "message": _redact_game_import_text(entry.get("message", "")),
    }
    sanitized.update(
        (key, _sanitize_event_value(entry[key]))
        for key in runtime_event_fields
        if key in entry
    )
    if entry.get("level") == "game-import":
        sanitized.update(
            (key, safe_value)
            for key in game_import_fields
            if key in entry
            and (safe_value := _sanitize_game_import_value(key, entry[key])) is not None
        )
    if entry.get("level") == "performance":
        sanitized.update(
            (key, _sanitize_event_value(entry[key]))
            for key in performance_fields
            if key in entry
        )
    if isinstance(entry.get("native"), dict):
        sanitized["native"] = _sanitize_native_details(entry["native"])
    if entry.get("level") == "audio-lifecycle":
        sanitized.update(
            (key, _sanitize_audio_lifecycle_value(entry[key], key))
            for key in audio_lifecycle_fields
            if key in entry and entry[key] is not None
        )
    return sanitized


def _sanitize_event_value(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_game_import_text(value)


def _sanitize_game_import_value(key: str, value: object) -> object:
    if key in _game_import_path_fields:
        if key == "roots" and isinstance(value, (list, tuple)):
            return [_redact_game_import_text(item) for item in value[:16]]
        return (
            _redact_game_import_text(value)
            if value is not None and value != ""
            else None
        )
    if key in _game_import_numeric_fields:
        if isinstance(value, bool):
            return None
        if not isinstance(value, (str, int, float)):
            return None
        try:
            number = float(value)
        except TypeError, ValueError, OverflowError:
            return None
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() else round(number, 3)
    if key in {"exists", "cancelled"}:
        return value if isinstance(value, bool) else None
    if key in {"cache_state", "missing", "reason"}:
        return _sanitize_game_import_structure(value)
    if isinstance(value, (dict, set, bytes, bytearray)):
        return None
    if isinstance(value, (list, tuple)):
        return None
    return _redact_game_import_text(value)


def _sanitize_game_import_structure(value: object, depth: int = 0) -> object:
    if depth >= 2:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [
            item
            for item in (
                _sanitize_game_import_structure(item, depth + 1) for item in value[:32]
            )
            if item is not None
        ]
    if isinstance(value, dict):
        return {
            _redact_game_import_text(key)[:80]: (
                "<redacted>" if _is_secret_name(key) else sanitized
            )
            for key, item in list(value.items())[:32]
            if (sanitized := _sanitize_game_import_structure(item, depth + 1))
            is not None
        }
    if isinstance(value, (set, bytes, bytearray)):
        return None
    return _redact_game_import_text(value)


def _sanitize_native_details(details: object) -> SupportDocument:
    if not isinstance(details, dict):
        return {}
    return {
        key: sanitized
        for key, value in details.items()
        if key in _native_fields
        and (
            sanitized := _sanitize_native_value(
                value, max_depth=4 if key == "resources" else 3
            )
        )
        is not _NATIVE_DROP
    }


def _sanitize_native_value(
    value: object, depth: int = 0, *, max_depth: int = 3
) -> object:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if _looks_like_local_path(value):
            return "<path>"
        return _redact_game_import_text(value)[:1024]
    if depth >= max_depth:
        return _NATIVE_DROP
    if isinstance(value, (list, tuple)):
        return [
            item
            for item in (
                _sanitize_native_value(item, depth + 1, max_depth=max_depth)
                for item in value[:32]
            )
            if item is not _NATIVE_DROP
        ]
    if isinstance(value, dict):
        return {
            str(key)[:80]: ("<redacted>" if _is_secret_name(key) else sanitized)
            for key, item in list(value.items())[:32]
            if (
                sanitized := _sanitize_native_value(
                    item, depth + 1, max_depth=max_depth
                )
            )
            is not _NATIVE_DROP
        }
    return _NATIVE_DROP


def _redact_game_import_text(value: object) -> str:
    value = redact_text(value)
    value = re.sub(
        r"(?i)\bauthorization\s*([=:])\s*bearer\s+[^\s,;]+",
        r"authorization\1<redacted>",
        value,
    )
    value = re.sub(
        r"(?i)([\"']?)(password|passwd|token|api[_-]?key|secret|authorization|cookie)"
        r"\1\s*([=:])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1\2\1\3<redacted>",
        value,
    )
    return value[:12_288]


def _is_secret_name(value: object) -> bool:
    return bool(
        re.search(
            r"(?i)(password|passwd|token|api[_-]?key|secret|authorization|cookie)",
            str(value),
        )
    )


def redact_text(value: object) -> str:
    value = str(value)
    home = str(Path.home())
    if home:
        value = value.replace(home, "<home>")
    value = re.sub(r"(?i)[a-z]:[\\/]+Users[\\/]+[^\\/]+", "<home>", value)
    value = re.sub(r"/(?:Users|home)/[^/]+", "<home>", value)
    return value


def sanitize_diagnostic(snapshot: object) -> SupportDocument:
    if snapshot is None:
        return {"available": False}
    values = {
        "available": True,
        "confidence": getattr(snapshot, "confidence", None),
        "preprocessing_profile": getattr(snapshot, "preprocessing_profile", None),
        "capture_ms": getattr(snapshot, "capture_ms", None),
        "ocr_ms": getattr(snapshot, "ocr_ms", None),
        "synthesis_ms": getattr(snapshot, "synthesis_ms", None),
        "playback_ms": getattr(snapshot, "playback_ms", None),
        "capture_interval_ms": getattr(snapshot, "capture_interval_ms", None),
        "game_focused": getattr(snapshot, "game_focused", None),
        "automatic_correction_count": len(getattr(snapshot, "corrections", ())),
        "speech_queue_depth": getattr(snapshot, "speech_queue_depth", None),
        "max_speech_queue_depth": getattr(snapshot, "max_speech_queue_depth", None),
        "last_first_audio_ms": getattr(snapshot, "last_first_audio_ms", None),
        "cache_source": getattr(snapshot, "cache_source", None),
    }
    result = {key: _sanitize_event_value(value) for key, value in values.items()}
    from vntts.capture_provenance import CaptureProvenance

    provenance = getattr(snapshot, "capture_provenance", None)
    if isinstance(provenance, Mapping):
        allowed = {field.name for field in fields(CaptureProvenance)}
        result["capture_provenance"] = {
            key: (
                _sanitize_capture_geometry(value)
                if key in {"window_bounds", "capture_bounds", "output_dimensions"}
                else _sanitize_event_value(value)
            )
            for key, value in provenance.items()
            if key in allowed
        }
    return result


def _sanitize_capture_geometry(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _sanitize_event_value(item)
            for key, item in value.items()
            if key in {"left", "top", "width", "height"}
            and isinstance(item, (int, float))
            and not isinstance(item, bool)
        }
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_event_value(item)
            for item in value[:2]
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        ]
    return None


def _ocr_metric_average(values: Sequence[int | float]) -> int | float | None:
    if not values:
        return None
    try:
        return round(mean(values), 2)
    except OverflowError:
        return None


def collect_ocr_metrics(directory: str | Path) -> SupportDocument:
    directory = Path(directory).expanduser()
    confidences: list[float] = []
    attempts: list[int] = []
    profiles: Counter[str] = Counter()
    resolved = 0
    invalid = 0
    if directory.is_dir():
        for path in directory.glob("uncertain-*.json"):
            try:
                payload = read_versioned_json(
                    path,
                    schema_version=OCR_REVIEW_SCHEMA_VERSION,
                    document_name="OCR review metadata",
                    allow_unversioned=True,
                )
                metadata = OCRReviewMetadata.from_payload(payload)
                confidences.append(metadata.confidence)
                attempts.append(metadata.attempts)
                profiles[metadata.preprocessing_profile] += 1
                resolved += payload.get("resolved") is True
            except OSError, TypeError, ValueError, OverflowError:
                invalid += 1
    return {
        "sample_count": len(confidences),
        "resolved_count": resolved,
        "pending_count": len(confidences) - resolved,
        "invalid_metadata_count": invalid,
        "average_confidence": _ocr_metric_average(confidences),
        "average_attempts": _ocr_metric_average(attempts),
        "preprocessing_profiles": dict(sorted(profiles.items())),
    }


def collect_build_identity() -> SupportDocument:
    """No model imports, filenames, branch names or remotes in the report."""
    versions: dict[str, str | None] = {}
    code_fingerprints: dict[str, str | None] = {}
    git_commit: str | None = None
    tracked_changes: bool | None = None
    # Loaded functions, not files on disk: pulling while the app runs must not
    # make an old process appear to run the new source. Also works when frozen.
    for module, attribute in (
        ("vntts.support", "record_native_speech"),
        ("vntts.moss_cpp_backend", "MossCppVoiceRouterBackend._generate"),
        ("vntts.pregeneration_audition", "VoiceAuditionPreviewService.generate"),
        ("vntts.native_resources", "NativeResourceSampler._run"),
    ):
        try:
            function: object = sys.modules.get(module)
            for name in attribute.split("."):
                function = getattr(function, name)
            code = getattr(function, "__code__", None)
            code_fingerprints[f"{module}.{attribute}"] = (
                hashlib.sha256(marshal.dumps(code)).hexdigest()
                if code is not None
                else None
            )
        except AttributeError:
            code_fingerprints[f"{module}.{attribute}"] = None
    for package in (
        "visual-novel-text-to-speech",
        "PySide6",
        "numpy",
        "soundfile",
        "psutil",
        "torch",
        "vntts-artifacts",
        "reverse1999-extractor",
    ):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    root = Path(__file__).resolve().parent.parent
    if not getattr(sys, "frozen", False) and (root / ".git").exists():
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                timeout=1,
                text=True,
                check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip()
            if re.fullmatch(r"[0-9a-f]{40,64}", commit):
                git_commit = commit
            diff = subprocess.run(
                [
                    "git",
                    "diff",
                    "--quiet",
                    "HEAD",
                    "--",
                    "vntts",
                    "pyproject.toml",
                    "uv.lock",
                ],
                cwd=root,
                capture_output=True,
                timeout=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if diff.returncode in {0, 1}:
                tracked_changes = diff.returncode == 1
        except OSError, subprocess.SubprocessError:
            pass
    return {
        "git_commit": git_commit,
        "tracked_changes": tracked_changes,
        "versions": versions,
        "code_fingerprints": code_fingerprints,
    }


def collect_dependency_status() -> SupportDocument:
    modules = (
        "PySide6",
        "PIL",
        "TTS",
        "pynput",
        "pytesseract",
        "sounddevice",
        "torch",
        "torchaudio",
    )
    status: SupportDocument = {
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "frozen_application": bool(getattr(sys, "frozen", False)),
        "python_modules": {
            module: importlib.util.find_spec(module) is not None for module in modules
        },
        "macos_permission_warnings": macos_permission_warnings(),
    }
    try:
        status["tesseract"] = {"available": True, "version": str(probe_tesseract())}
    except Exception as error:
        status["tesseract"] = {"available": False, "error": redact_text(error)}
    try:
        probe_audio_output()
        status["audio_output"] = {"available": True}
    except Exception as error:
        status["audio_output"] = {"available": False, "error": redact_text(error)}
    return status
