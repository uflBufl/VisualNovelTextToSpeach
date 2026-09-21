import hashlib
import importlib.metadata
import importlib.util
import json
import marshal
import math
import platform
import re
import subprocess
import sys
import zipfile
from collections import Counter, OrderedDict, deque
from contextvars import ContextVar
from dataclasses import asdict, fields
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import RLock
from uuid import UUID

from vntts_artifacts.atomic_io import atomic_output_path

from vntts.audio_lifecycle import audio_lifecycle_context
from vntts.diagnostics import macos_permission_warnings
from vntts.ocr_review import OCR_REVIEW_SCHEMA_VERSION
from vntts.onboarding import probe_audio_output, probe_tesseract
from vntts.settings import AppSettings
from vntts.versioned_json import read_versioned_json
from vntts.voice_library import VoiceLibrary

SupportDocument = dict[str, object]

audio_route_fields = (
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

runtime_event_fields = (*audio_route_fields, *live_scope_fields)

generation_timeline_stages = (
    "capture",
    "ocr",
    "stable-text",
    "route-decision",
    "voice-resolution",
    "generation-start",
    "canonical-full-text",
    "first-pcm",
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
    "sequence-candidate-miss",
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
)


class GenerationTimelineLog:
    """Keep one bounded, privacy-safe pipeline timeline per generation."""

    def __init__(self, maximum_entries=200, *, path=None):
        self.maximum_entries = max(1, int(maximum_entries))
        self.path = Path(path).expanduser() if path is not None else None
        self.timelines = OrderedDict()
        self.lock = RLock()

    def record(self, stage, generation, occurred_at, *, session_id=None, **details):
        if stage not in generation_timeline_stages + sequence_timeline_stages:
            raise ValueError(f"Unknown generation timeline stage: {stage}")
        try:
            generation = int(generation)
            occurred_at = float(occurred_at)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Timeline generation and timestamp must be numeric"
            ) from error
        if generation < 1:
            return False
        if session_id is not None:
            try:
                session_id = UUID(str(session_id)).hex
            except (ValueError, AttributeError) as error:
                raise ValueError("Timeline session ID must be a UUID") from error

        with self.lock:
            identity = session_id, generation
            timeline = self.timelines.setdefault(
                identity,
                {
                    "generation": generation,
                    "session_id": session_id,
                    "events": OrderedDict(),
                },
            )
            chunk_id = details.get("chunk_id")
            event_key = f"{stage}:{chunk_id}" if chunk_id else stage
            existing = timeline["events"].get(event_key, {})
            event = {
                "stage": stage,
                "occurred_at": occurred_at,
                **{
                    key: _sanitize_event_value(details[key])
                    for key in generation_timeline_detail_fields
                    if key in details and details[key] is not None
                },
            }
            # A stage can be reported by both the controller and the reader.
            # Preserve richer source-specific details while updating its time.
            timeline["events"][event_key] = {**existing, **event}
            self.timelines.move_to_end(identity)
            while len(self.timelines) > self.maximum_entries:
                self.timelines.popitem(last=False)
            self._persist_locked()
        return True

    def snapshot(self):
        with self.lock:
            return [
                self._serialize_timeline(value) for value in self.timelines.values()
            ]

    def latency_summary(self):
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
        samples = {name: [] for name in fields}
        with self.lock:
            for timeline in self.timelines.values():
                for event in timeline["events"].values():
                    for name, (stage, field) in fields.items():
                        value = event.get(field)
                        if (
                            event.get("stage") == stage
                            and isinstance(value, (int, float))
                            and not isinstance(value, bool)
                            and value >= 0
                        ):
                            samples[name].append(float(value))
        return {
            name: {
                "samples": len(values),
                "p50_ms": _percentile(values, 0.50),
                "p95_ms": _percentile(values, 0.95),
            }
            for name, values in samples.items()
            if values
        }

    def _persist_locked(self):
        if self.path is None:
            return
        try:
            from vntts_artifacts.atomic_io import atomic_write_json

            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                self.path,
                {
                    "version": 1,
                    "timelines": self.snapshot(),
                    "latency_summary": self.latency_summary(),
                },
            )
        except OSError:
            pass

    @staticmethod
    def _serialize_timeline(timeline):
        events = list(timeline["events"].values())
        if not events:
            result = {"generation": timeline["generation"], "events": []}
            if timeline["session_id"] is not None:
                result["session_id"] = timeline["session_id"]
            return result
        started_at = min(event["occurred_at"] for event in events)
        serialized = []
        for event in sorted(
            events,
            key=lambda value: (
                value["occurred_at"],
                (generation_timeline_stages + sequence_timeline_stages).index(
                    value["stage"]
                ),
            ),
        ):
            serialized.append(
                {key: value for key, value in event.items() if key != "occurred_at"}
                | {
                    "elapsed_ms": round(
                        (event["occurred_at"] - started_at) * 1000,
                        3,
                    )
                }
            )
        result = {"generation": timeline["generation"], "events": serialized}
        if timeline["session_id"] is not None:
            result["session_id"] = timeline["session_id"]
        return result


def _percentile(values, quantile):
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
        maximum_entries=200,
        *,
        maximum_bytes=512 * 1024,
        clock=None,
        path=None,
        detail_fields=runtime_event_fields,
    ):
        self.entries = deque(maxlen=maximum_entries)
        self.maximum_bytes = max(256, int(maximum_bytes))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = RLock()
        self.path = Path(path).expanduser() if path is not None else None
        self.detail_fields = tuple(detail_fields)

    def add(self, level, message, **details):
        with self.lock:
            entry = {
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
                except OSError:
                    pass

    def _bounded_message(self, message):
        value = str(message)
        maximum_characters = max(64, min(16_384, self.maximum_bytes // 2))
        if len(value) <= maximum_characters:
            return value
        return f"{value[: maximum_characters - 15]}... <truncated>"

    def _persist_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._json_lines()
        while len(payload) > self.maximum_bytes and len(self.entries) > 1:
            self.entries.popleft()
            payload = self._json_lines()
        if len(payload) > self.maximum_bytes:
            entry = self.entries[-1]
            self.entries[-1] = {
                "recorded_at": entry["recorded_at"],
                "level": entry["level"],
                "message": "<runtime event exceeded persistent log limit>",
            }
            payload = self._json_lines()
        with atomic_output_path(self.path) as temporary_path:
            temporary_path.write_bytes(payload)

    def _json_lines(self):
        return b"".join(
            (json.dumps(sanitize_event(entry), ensure_ascii=False) + "\n").encode(
                "utf-8"
            )
            for entry in self.entries
        )

    def snapshot(self):
        with self.lock:
            return list(self.entries)


def _read_bounded_json_lines(path, maximum_bytes):
    if path is None:
        return ()
    try:
        with path.open("rb") as source:
            source.seek(0, 2)
            offset = max(0, source.tell() - maximum_bytes)
            source.seek(offset)
            payload = source.read(maximum_bytes)
    except OSError:
        return ()
    if offset:
        payload = payload.split(b"\n", 1)[-1]
    entries = []
    for line in payload.splitlines():
        try:
            entry = json.loads(line)
        except TypeError, ValueError, json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return tuple(entries)


performance_fields = (
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

    def __init__(self, maximum_entries=200, **kwargs):
        super().__init__(
            maximum_entries=maximum_entries,
            detail_fields=performance_fields,
            **kwargs,
        )

    def record(self, operation, elapsed_ms, outcome, **details):
        if outcome == "complete" and elapsed_ms < 100:
            return
        self.add(
            "performance",
            f"Application operation: {operation}",
            operation=operation,
            outcome=outcome,
            elapsed_ms=round(elapsed_ms, 3),
            **{key: details[key] for key in performance_fields if key in details},
        )

    def report(self):
        events = [sanitize_event(entry) for entry in self.snapshot()]
        summary = {}
        for event in events:
            operation = event["operation"]
            aggregate = summary.setdefault(
                operation, {"count": 0, "total_ms": 0.0, "max_ms": 0.0}
            )
            aggregate["count"] += 1
            aggregate["total_ms"] = round(
                aggregate["total_ms"] + event["elapsed_ms"], 3
            )
            aggregate["max_ms"] = max(aggregate["max_ms"], event["elapsed_ms"])
            for name in ("files_examined", "bytes_examined"):
                if name in event:
                    aggregate[f"max_{name}"] = max(
                        aggregate.get(f"max_{name}", 0), event[name]
                    )
        return {"threshold_ms": 100, "summary": summary, "events": events}


performance_log = PerformanceLog()


def configure_performance_log(path=None):
    global performance_log
    performance_log = PerformanceLog(path=path)
    return performance_log


def record_background_operation(operation, elapsed_ms, outcome, **details):
    try:
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

    def __init__(self, maximum_entries=200, **kwargs):
        super().__init__(
            maximum_entries=maximum_entries,
            detail_fields=game_import_fields,
            **kwargs,
        )
        try:
            self._load_previous()
        except Exception:
            # A support log must not make application startup fail.
            pass

    def record(self, stage, **details):
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

    def _load_previous(self):
        for entry in _read_bounded_json_lines(self.path, self.maximum_bytes):
            if not isinstance(entry, dict) or entry.get("level") != "game-import":
                continue
            stage = entry.get("stage")
            if not isinstance(stage, str) or not stage:
                continue
            restored = {
                "recorded_at": str(entry.get("recorded_at", "")),
                "level": "game-import",
                "message": self._bounded_message(
                    _redact_game_import_text(entry.get("message", ""))
                ),
            }
            restored.update(
                (key, _sanitize_game_import_value(key, entry[key]))
                for key in game_import_fields
                if key in entry
                and _sanitize_game_import_value(key, entry[key]) is not None
            )
            self.entries.append(restored)


game_import_log = GameImportLog()


def configure_game_import_log(path=None):
    """Set the application-owned persistence target without affecting imports."""
    global game_import_log
    game_import_log = GameImportLog(path=path)
    return game_import_log


def record_game_import(stage, **details):
    """Record only privacy-safe technical import evidence; never affect import work."""
    try:
        game_import_log.record(stage, **details)
    except Exception:
        pass


native_speech_context = ContextVar("native_speech_context", default=None)
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

    def __init__(self, maximum_entries=200, *, path=None):
        super().__init__(
            maximum_entries=maximum_entries,
            path=path,
            detail_fields=("native",),
        )
        self.started_at = self.clock().isoformat()
        self.total_events = 0
        self.outcomes = Counter()
        self.request_seconds = Counter()
        self.latest_runtime = None
        self.active_requests = OrderedDict()
        try:
            self._load_previous()
        except Exception:
            pass

    def record(self, details):
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

    def _load_previous(self):
        for entry in _read_bounded_json_lines(self.path, self.maximum_bytes):
            if entry.get("level") != "moss-native":
                continue
            details = _sanitize_native_details(entry.get("native"))
            if not details:
                continue
            restored = {
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
            self.started_at = self.entries[0]["recorded_at"]

    def _accumulate(self, details, recorded_at):
        self.total_events += 1
        operation = details.get("operation", "unknown")
        outcome = details.get("outcome", "unknown")
        key = f"{operation}/{outcome}"
        # ponytail: fixed-size aggregate labels; details retain new labels.
        if key not in self.outcomes and len(self.outcomes) >= 64:
            key = "other"
        self.outcomes[key] += 1
        seconds = details.get("request_s")
        if isinstance(seconds, (int, float)) and math.isfinite(seconds):
            self.request_seconds[key] += max(0, seconds)
        if operation == "server-start":
            self.latest_runtime = details
        attempt_id = details.get("attempt_id")
        if attempt_id and operation == "request-start":
            self.active_requests[attempt_id] = {
                **details,
                "recorded_at": recorded_at,
            }
            if len(self.active_requests) > 64:
                self.active_requests.popitem(last=False)
        elif attempt_id and operation == "fresh-generation":
            self.active_requests.pop(attempt_id, None)

    def report(self):
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
                    key: round(value, 3) for key, value in self.request_seconds.items()
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


def configure_native_speech_log(path=None):
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
    timelines = _previous_generation_timelines(directory / "generation-timelines.json")
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


def _previous_generation_timelines(path: Path) -> list[SupportDocument]:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return []
        document = json.loads(path.read_bytes())
    except OSError, UnicodeError, json.JSONDecodeError:
        return []
    if not isinstance(document, dict) or not isinstance(
        document.get("timelines"), list
    ):
        return []
    result = []
    for timeline in document["timelines"][-200:]:
        if not isinstance(timeline, dict):
            continue
        generation = timeline.get("generation")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            continue
        sanitized: SupportDocument = {"generation": generation, "events": []}
        session_id = timeline.get("session_id")
        if session_id is not None:
            try:
                sanitized["session_id"] = UUID(str(session_id)).hex
            except ValueError, AttributeError:
                continue
        events = timeline.get("events")
        if isinstance(events, list):
            for event in events[-100:]:
                if not isinstance(event, dict) or event.get("stage") not in (
                    generation_timeline_stages + sequence_timeline_stages
                ):
                    continue
                safe_event = {
                    "stage": event["stage"],
                    **{
                        key: _sanitize_event_value(event[key])
                        for key in ("elapsed_ms", *generation_timeline_detail_fields)
                        if key in event and event[key] is not None
                    },
                }
                sanitized["events"].append(safe_event)
        result.append(sanitized)
    return result


def record_native_speech(**details):
    details = {**(native_speech_context.get() or {}), **details}
    try:
        native_speech_log.record(
            {key: value for key, value in details.items() if key in _native_fields}
        )
    except Exception:
        # Diagnostics must never turn a successful render into a failure.
        pass


class AudioLifecycleLog(RuntimeSupportLog):
    def __init__(self, maximum_entries=400, *, path=None, load_existing=False):
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

    def _persist_locked(self):
        try:
            existing_bytes = self.path.stat().st_size
        except OSError:
            existing_bytes = 0
        latest = (
            json.dumps(sanitize_event(self.entries[-1]), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        if (
            not self.persistence_initialized
            or existing_bytes + len(latest) > self.maximum_bytes
        ):
            super()._persist_locked()
            self.persistence_initialized = True
            return
        with self.path.open("ab") as output:
            output.write(latest)

    def record(self, operation, **details):
        fields = {**(audio_lifecycle_context.get() or {}), **details}
        fields["operation"] = str(operation)[:64]
        self.add(
            "audio-lifecycle",
            f"Audio stream: {fields['operation']}",
            **{
                key: _sanitize_audio_lifecycle_value(value)
                for key, value in fields.items()
                if key in audio_lifecycle_fields and value is not None
            },
        )

    def report(self):
        return {"schema_version": 1, "events": self.snapshot()}


def _sanitize_audio_lifecycle_value(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 3) if math.isfinite(value) else None
    return _redact_game_import_text(value)[:256]


audio_lifecycle_log = AudioLifecycleLog()


def configure_audio_lifecycle_log(path=None):
    global audio_lifecycle_log
    audio_lifecycle_log = AudioLifecycleLog(path=path)
    return audio_lifecycle_log


def record_audio_lifecycle(operation, **details):
    try:
        audio_lifecycle_log.record(operation, **details)
    except Exception:
        pass


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
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, UnicodeError, json.JSONDecodeError:
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
    return {
        "available": True,
        "job_id": _plain_support_value(getattr(job, "job_id", None)),
        "status": _plain_support_value(getattr(job, "status", None)),
        "provider_id": _plain_support_value(getattr(job, "provider_id", None)),
        "story_index_sha256": _sha256_support_value(
            getattr(job, "story_index_sha256", None)
        ),
        "selected_story_ids": [
            _plain_support_value(value)
            for value in tuple(getattr(job, "selected_story_ids", ()))[:64]
        ],
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
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            return {"available": False, "reason": "state exceeds support read limit"}
        payload = path.read_bytes()
        document = json.loads(payload)
    except FileNotFoundError:
        return {"available": False, "reason": "state is not present"}
    except OSError, UnicodeError, json.JSONDecodeError:
        return {"available": False, "reason": "state could not be read"}
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, dict):
        return {"available": False, "reason": "state items are invalid"}
    statuses = Counter(
        item.get("status")
        for item in items.values()
        if isinstance(item, dict) and isinstance(item.get("status"), str)
    )
    failures = [
        _pregeneration_failure_summary(queue_id, item)
        for queue_id, item in sorted(items.items())
        if isinstance(queue_id, str)
        and isinstance(item, dict)
        and item.get("status") == "failed"
    ]
    return {
        "available": True,
        "state_sha256": hashlib.sha256(payload).hexdigest(),
        "queue_sha256": _sha256_support_value(document.get("queue_sha256")),
        "status_counts": dict(sorted(statuses.items())),
        "failed_items": failures[:64],
        "failed_items_truncated": max(0, len(failures) - 64),
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
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else None


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
    if source.get("schema_version") != 1:
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
            integers=("selected_line_count",),
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
    pack_path = getattr(settings, "game_pack", None)
    if pack_path:
        try:
            path = Path(pack_path).expanduser()
            stat = path.stat()
            return _active_pack_identity(str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass
    story_path = getattr(settings, "story_index", None)
    if story_path:
        try:
            path = Path(story_path).expanduser()
            return {
                "available": True,
                "active_story_index_sha256": _file_sha256(path),
            }
        except OSError:
            pass
    return {"available": False}


def correlate_active_preparation(
    active: SupportDocument, preparation: SupportDocument
) -> SupportDocument:
    """Explain whether the saved pack came from the preparation that failed."""
    job = preparation.get("job") if isinstance(preparation, dict) else None
    generation_input = (
        preparation.get("input") if isinstance(preparation, dict) else None
    )
    if not active.get("available") or not isinstance(job, dict):
        return {"classification": "insufficient-evidence"}
    selected_story_ids = job.get("selected_story_ids")
    active_story_ids = active.get("active_story_ids")
    generation_state = preparation.get("generation_state")
    comparisons: dict[str, bool | None] = {
        "selected_stories_present": (
            set(selected_story_ids).issubset(active_story_ids)
            if isinstance(selected_story_ids, list)
            and all(isinstance(value, str) for value in selected_story_ids)
            and isinstance(active_story_ids, list)
            and all(isinstance(value, str) for value in active_story_ids)
            and selected_story_ids
            and active_story_ids
            else None
        ),
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


@lru_cache(maxsize=16)
def _active_pack_identity(path: str, _modified_ns: int, size: int) -> SupportDocument:
    if size > 64 * 1024 * 1024:
        return {
            "available": False,
            "reason": "pack manifest exceeds support read limit",
        }
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        return {"available": False, "reason": "pack manifest could not be read"}
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
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("story index exceeds support read limit")
        story_ids: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            record: object = json.loads(line)
            if not isinstance(record, dict):
                continue
            collection_id = record.get("collection_id")
            if (
                record.get("record_type") != "metadata"
                and isinstance(collection_id, str)
                and collection_id
            ):
                story_ids.add(collection_id)
    except OSError, UnicodeError, json.JSONDecodeError, ValueError:
        return {"active_story_ids_available": False}
    values = sorted(story_ids)
    return {
        "active_story_ids_available": True,
        "active_story_ids": values[:256],
        "active_story_ids_truncated": max(0, len(values) - 256),
    }


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SupportBundleBuilder:
    def __init__(
        self,
        settings,
        event_log,
        *,
        diagnostic=None,
        dependency_probe=None,
        generation_timelines=None,
        game_import_log=None,
        performance_log_value=None,
        previous_session=None,
        audio_lifecycle=None,
        voice_library: VoiceLibrary | None = None,
    ):
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

    def build(self, path):
        path = Path(path).expanduser()
        if path.suffix.casefold() != ".zip":
            path = path.with_suffix(".zip")
        path.parent.mkdir(parents=True, exist_ok=True)
        active_content = collect_active_content_identity(self.settings)
        preparation = pregeneration_support.report()
        files = {
            "manifest.json": {
                "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "privacy": (
                    "Screenshots, recognized dialogue, voice audio, model files, "
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
            "game-import.json": {
                "events": [
                    sanitize_event(entry)
                    for entry in (self.game_import_log or game_import_log).snapshot()
                ]
            },
            "performance.json": (self.performance_log or performance_log).report(),
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
            "generation-timelines.json": {
                "version": 1,
                "timelines": (
                    self.generation_timelines.snapshot()
                    if self.generation_timelines is not None
                    else []
                ),
                "latency_summary": (
                    self.generation_timelines.latency_summary()
                    if self.generation_timelines is not None
                    else {}
                ),
            },
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


def sanitize_settings(settings):
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


def _looks_like_local_path(value):
    value = str(value).strip()
    return bool(
        value.startswith(("/", "\\", "~", "./", "../"))
        or re.match(r"(?i)^[a-z]:[\\/]", value)
    )


def sanitize_event(entry):
    sanitized = {
        "recorded_at": entry.get("recorded_at"),
        "level": entry.get("level"),
        "message": redact_text(entry.get("message", "")),
    }
    sanitized.update(
        (key, _sanitize_event_value(entry[key]))
        for key in runtime_event_fields
        if key in entry
    )
    if entry.get("level") == "game-import":
        sanitized.update(
            (key, _sanitize_game_import_value(key, entry[key]))
            for key in game_import_fields
            if key in entry and _sanitize_game_import_value(key, entry[key]) is not None
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
            (key, _sanitize_audio_lifecycle_value(entry[key]))
            for key in audio_lifecycle_fields
            if key in entry and entry[key] is not None
        )
    return sanitized


def _sanitize_event_value(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)


def _sanitize_game_import_value(key, value):
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
        try:
            number = float(value)
        except TypeError, ValueError:
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


def _sanitize_game_import_structure(value, depth=0):
    if depth >= 2:
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


def _sanitize_native_details(details):
    if not isinstance(details, dict):
        return {}
    return {
        key: sanitized
        for key, value in details.items()
        if key in _native_fields
        and (sanitized := _sanitize_native_value(value)) is not _NATIVE_DROP
    }


def _sanitize_native_value(value, depth=0):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if _looks_like_local_path(value):
            return "<path>"
        return _redact_game_import_text(value)[:1024]
    if depth >= 3:
        return _NATIVE_DROP
    if isinstance(value, (list, tuple)):
        return [
            item
            for item in (_sanitize_native_value(item, depth + 1) for item in value[:32])
            if item is not _NATIVE_DROP
        ]
    if isinstance(value, dict):
        return {
            str(key)[:80]: ("<redacted>" if _is_secret_name(key) else sanitized)
            for key, item in list(value.items())[:32]
            if (sanitized := _sanitize_native_value(item, depth + 1))
            is not _NATIVE_DROP
        }
    return _NATIVE_DROP


def _redact_game_import_text(value):
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


def _is_secret_name(value):
    return bool(
        re.search(
            r"(?i)(password|passwd|token|api[_-]?key|secret|authorization|cookie)",
            str(value),
        )
    )


def redact_text(value):
    value = str(value)
    home = str(Path.home())
    if home:
        value = value.replace(home, "<home>")
    value = re.sub(r"(?i)[a-z]:[\\/]+Users[\\/]+[^\\/]+", "<home>", value)
    value = re.sub(r"/(?:Users|home)/[^/]+", "<home>", value)
    return value


def sanitize_diagnostic(snapshot):
    if snapshot is None:
        return {"available": False}
    return {
        "available": True,
        "confidence": snapshot.confidence,
        "preprocessing_profile": snapshot.preprocessing_profile,
        "capture_ms": snapshot.capture_ms,
        "ocr_ms": snapshot.ocr_ms,
        "synthesis_ms": snapshot.synthesis_ms,
        "playback_ms": snapshot.playback_ms,
        "capture_interval_ms": snapshot.capture_interval_ms,
        "game_focused": snapshot.game_focused,
        "automatic_correction_count": len(snapshot.corrections),
        "speech_queue_depth": snapshot.speech_queue_depth,
        "max_speech_queue_depth": snapshot.max_speech_queue_depth,
        "last_first_audio_ms": snapshot.last_first_audio_ms,
        "cache_source": snapshot.cache_source,
    }


def collect_ocr_metrics(directory):
    directory = Path(directory).expanduser()
    confidences = []
    attempts = []
    profiles = Counter()
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
                confidences.append(float(payload.get("confidence", 0)))
                attempts.append(int(payload.get("attempts", 0)))
                profiles[str(payload.get("preprocessing_profile") or "unknown")] += 1
                resolved += payload.get("resolved") is True
            except OSError, TypeError, ValueError, json.JSONDecodeError:
                invalid += 1
    return {
        "sample_count": len(confidences),
        "resolved_count": resolved,
        "pending_count": len(confidences) - resolved,
        "invalid_metadata_count": invalid,
        "average_confidence": (
            round(sum(confidences) / len(confidences), 2) if confidences else None
        ),
        "average_attempts": (
            round(sum(attempts) / len(attempts), 2) if attempts else None
        ),
        "preprocessing_profiles": dict(sorted(profiles.items())),
    }


def collect_build_identity():
    """No model imports, filenames, branch names or remotes in the report."""
    status = {"git_commit": None, "tracked_changes": None, "versions": {}}
    status["code_fingerprints"] = {}
    # Loaded functions, not files on disk: pulling while the app runs must not
    # make an old process appear to run the new source. Also works when frozen.
    for module, attribute in (
        ("vntts.support", "record_native_speech"),
        ("vntts.moss_cpp_backend", "MossCppVoiceRouterBackend._generate"),
        ("vntts.pregeneration_audition", "VoiceAuditionPreviewService.generate"),
        ("vntts.native_resources", "NativeResourceSampler._run"),
    ):
        try:
            function = sys.modules.get(module)
            for name in attribute.split("."):
                function = getattr(function, name)
            status["code_fingerprints"][f"{module}.{attribute}"] = hashlib.sha256(
                marshal.dumps(function.__code__)
            ).hexdigest()
        except AttributeError:
            status["code_fingerprints"][f"{module}.{attribute}"] = None
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
            status["versions"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            status["versions"][package] = None
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
                status["git_commit"] = commit
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
                status["tracked_changes"] = diff.returncode == 1
        except OSError, subprocess.SubprocessError:
            pass
    return status


def collect_dependency_status():
    modules = (
        "PySide6",
        "PIL",
        "TTS",
        "mss",
        "pynput",
        "pytesseract",
        "sounddevice",
        "torch",
        "torchaudio",
    )
    status = {
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
