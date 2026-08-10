import importlib.util
import json
import platform
import re
import sys
import zipfile
from collections import Counter, deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from vntts.diagnostics import macos_permission_warnings
from vntts.onboarding import probe_audio_output, probe_tesseract


class RuntimeSupportLog:
    def __init__(self, maximum_entries=200, *, clock=None):
        self.entries = deque(maxlen=maximum_entries)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = RLock()

    def add(self, level, message):
        with self.lock:
            self.entries.append(
                {
                    "recorded_at": self.clock().isoformat(),
                    "level": str(level),
                    "message": str(message),
                }
            )

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
    ):
        self.settings = settings
        self.event_log = event_log
        self.diagnostic = diagnostic
        self.dependency_probe = dependency_probe or collect_dependency_status

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
            "ocr-metrics.json": collect_ocr_metrics(
                self.settings.ocr_diagnostics_directory
            ),
            "diagnostics.json": sanitize_diagnostic(self.diagnostic),
            "dependencies.json": self.dependency_probe(),
        }
        temporary_path = path.with_suffix(f"{path.suffix}.tmp")
        try:
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
            temporary_path.replace(path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
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
    return {
        "recorded_at": entry.get("recorded_at"),
        "level": entry.get("level"),
        "message": redact_text(entry.get("message", "")),
    }


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
                payload = json.loads(path.read_text(encoding="utf-8"))
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
