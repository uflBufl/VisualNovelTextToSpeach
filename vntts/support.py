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
from pathlib import Path
from threading import RLock

from vntts_artifacts.atomic_io import atomic_output_path

from vntts.diagnostics import macos_permission_warnings
from vntts.ocr_review import OCR_REVIEW_SCHEMA_VERSION
from vntts.onboarding import probe_audio_output, probe_tesseract
from vntts.versioned_json import read_versioned_json

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
)


class GenerationTimelineLog:
    """Keep one bounded, privacy-safe pipeline timeline per generation."""

    def __init__(self, maximum_entries=200, *, path=None):
        self.maximum_entries = max(1, int(maximum_entries))
        self.path = Path(path).expanduser() if path is not None else None
        self.timelines = OrderedDict()
        self.lock = RLock()

    def record(self, stage, generation, occurred_at, **details):
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

        with self.lock:
            timeline = self.timelines.setdefault(
                generation,
                {"generation": generation, "events": OrderedDict()},
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
            self.timelines.move_to_end(generation)
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
            return {"generation": timeline["generation"], "events": []}
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
        return {"generation": timeline["generation"], "events": serialized}


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
        detail_fields=audio_route_fields,
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
        if self.path is None:
            return
        try:
            with self.path.open("rb") as source:
                source.seek(0, 2)
                offset = max(0, source.tell() - self.maximum_bytes)
                source.seek(offset)
                payload = source.read(self.maximum_bytes)
        except OSError:
            return
        if offset:
            payload = payload.split(b"\n", 1)[-1]
        for line in payload.splitlines():
            try:
                entry = json.loads(line)
            except TypeError, ValueError, json.JSONDecodeError:
                continue
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
    "codec_bytes gpu_layers aux_cpu local_gpu aux_cpu_threads fallback_reason "
    "capability_version vulkan_available "
    "context_size http_status quality thresholds "
    "native_error_hint exit_code reference_prepare_s http_round_trip_s "
    "response_pcm_decode_s gen_backbone_s gen_frame_decoder_s "
    "gen_input_embedding_s".split()
)


class NativeSpeechLog(RuntimeSupportLog):
    """Bounded detail with non-rolling counts; independent of preview ownership."""

    def __init__(self, maximum_entries=200):
        super().__init__(maximum_entries=maximum_entries)
        self.started_at = self.clock().isoformat()
        self.total_events = 0
        self.outcomes = Counter()
        self.request_seconds = Counter()
        self.latest_runtime = None
        self.active_requests = OrderedDict()

    def record(self, details):
        with self.lock:
            self.add(
                "moss-native",
                "MOSS native: "
                + "; ".join(
                    f"{key}={value if value is not None else 'unavailable'}"
                    for key, value in details.items()
                    if key not in {"resources", "sampling"}
                ),
            )
            self.entries[-1]["native"] = details
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
                    "recorded_at": self.entries[-1]["recorded_at"],
                }
                if len(self.active_requests) > 64:
                    self.active_requests.popitem(last=False)
            elif attempt_id and operation == "fresh-generation":
                self.active_requests.pop(attempt_id, None)

    def report(self):
        with self.lock:
            events = self.snapshot()
            return {
                "schema_version": 2,
                "scope": "current application process; export before exit",
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


def record_native_speech(**details):
    details = {**(native_speech_context.get() or {}), **details}
    try:
        native_speech_log.record(
            {key: value for key, value in details.items() if key in _native_fields}
        )
    except Exception:
        # Diagnostics must never turn a successful render into a failure.
        pass


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
    ):
        self.settings = settings
        self.event_log = event_log
        self.diagnostic = diagnostic
        self.dependency_probe = dependency_probe or collect_dependency_status
        self.generation_timelines = generation_timelines
        self.game_import_log = game_import_log

    def build(self, path):
        path = Path(path).expanduser()
        if path.suffix.casefold() != ".zip":
            path = path.with_suffix(".zip")
        path.parent.mkdir(parents=True, exist_ok=True)
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
            "runtime-events.json": {
                "events": [sanitize_event(entry) for entry in self.event_log.snapshot()]
            },
            "game-import.json": {
                "events": [
                    sanitize_event(entry)
                    for entry in (self.game_import_log or game_import_log).snapshot()
                ]
            },
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
            "ocr-metrics.json": collect_ocr_metrics(
                self.settings.ocr_diagnostics_directory
            ),
            "diagnostics.json": sanitize_diagnostic(self.diagnostic),
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
        for key in audio_route_fields
        if key in entry
    )
    if entry.get("level") == "game-import":
        sanitized.update(
            (key, _sanitize_game_import_value(key, entry[key]))
            for key in game_import_fields
            if key in entry and _sanitize_game_import_value(key, entry[key]) is not None
        )
    if isinstance(entry.get("native"), dict):
        sanitized["native"] = {
            key: value
            for key, value in entry["native"].items()
            if key in _native_fields
        }
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
