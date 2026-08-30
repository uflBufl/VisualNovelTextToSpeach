import importlib.util
import json
import platform
import re
import sys
import zipfile
from collections import Counter, OrderedDict, deque
from dataclasses import asdict
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
    "candidate_event_ids",
    "next_event_count",
    "reason",
    "route",
    "fingerprint",
    "visible",
    "focused",
    "owner",
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
    def __init__(self, maximum_entries=200, *, clock=None, path=None):
        self.entries = deque(maxlen=maximum_entries)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = RLock()
        self.path = Path(path).expanduser() if path is not None else None

    def add(self, level, message, **details):
        with self.lock:
            entry = {
                "recorded_at": self.clock().isoformat(),
                "level": str(level),
                "message": str(message),
            }
            entry.update(
                (key, details[key]) for key in audio_route_fields if key in details
            )
            self.entries.append(entry)
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as output:
                        output.write(
                            json.dumps(sanitize_event(entry), ensure_ascii=False) + "\n"
                        )
                except OSError:
                    pass

    def snapshot(self):
        with self.lock:
            return list(self.entries)


class SupportBundleBuilder:
    def __init__(
        self,
        settings,
        event_log,
        *,
        diagnostic=None,
        dependency_probe=None,
        generation_timelines=None,
    ):
        self.settings = settings
        self.event_log = event_log
        self.diagnostic = diagnostic
        self.dependency_probe = dependency_probe or collect_dependency_status
        self.generation_timelines = generation_timelines

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
    for key in (
        "screenshot_directory",
        "ocr_diagnostics_directory",
        "voice_manifest",
        "tts_speaker_wav",
    ):
        value = values.get(key)
        if value:
            values[key] = redact_text(value)
    return values


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
    return sanitized


def _sanitize_event_value(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)


def redact_text(value):
    value = str(value)
    home = str(Path.home())
    if home:
        value = value.replace(home, "<home>")
    value = re.sub(r"(?i)[a-z]:\\Users\\[^\\]+", "<home>", value)
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
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
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
