import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from vntts.assets import ModelAssetManager
from vntts.auto_advance_policy import auto_advance_allowed
from vntts.hotkeys import HotkeyValidationError, validate_hotkey_assignments
from vntts.macos import get_macos_permission_status
from vntts.release_backends import packaged_speech_backend_available
from vntts.speech_worker import resolve_speech_runtime_paths
from vntts.voices import CharacterVoiceRegistry, VoiceManifestError


@dataclass(frozen=True)
class DiagnosticResult:
    name: str
    status: str
    message: str
    remediation: str | None = None

    @property
    def passed(self):
        return self.status != "error"


class OnboardingDiagnostics:
    def __init__(
        self,
        *,
        tesseract_probe=None,
        audio_probe=None,
        model_path_resolver=None,
        permission_status_provider=None,
    ):
        self.tesseract_probe = tesseract_probe or probe_tesseract
        self.audio_probe = audio_probe or probe_audio_output
        self.model_path_resolver = model_path_resolver or get_model_cache_path
        self.permission_status_provider = (
            permission_status_provider or get_macos_permission_status
        )

    def run(self, settings):
        results = [
            self._check_hotkeys(settings),
            self._check_capture_source(settings),
            self._check_tesseract(),
            self._check_audio(),
            self._check_model(settings),
            self._check_voice_manifest(settings),
        ]
        permission_result = self._check_platform_permissions(settings)
        if permission_result is not None:
            results.insert(2, permission_result)
        return tuple(results)

    def moss_installation_space(self, settings):
        if (
            settings.speech_backend != "moss-tts"
            or sys.platform != "win32"
            or platform.machine().casefold() not in {"amd64", "x86_64"}
        ):
            return None
        from vntts.moss_cpp_backend import moss_cpp_requested
        from vntts.moss_cpp_installation import managed_download_space

        if not moss_cpp_requested(settings.tts_model):
            return None
        remaining, required, free = managed_download_space(settings.tts_model)
        return (remaining, required, free) if remaining else None

    def prepare_and_run(
        self,
        settings,
        *,
        cancellation,
        progress,
        allow_moss_download=False,
    ):
        """Only the setup journey provisions dependencies; ordinary probes stay read-only."""
        from vntts.moss_cpp_backend import moss_cpp_requested
        from vntts.runtime_installation import ensure_speech_runtime

        if settings.speech_backend == "moss-tts" and moss_cpp_requested(
            settings.tts_model
        ):
            from vntts.moss_cpp_installation import ensure_moss_cpp

            ensure_moss_cpp(
                settings.tts_model,
                cancellation=cancellation,
                progress=progress,
                allow_download=allow_moss_download,
            )

        if settings.speech_backend in {"pocket-tts", "moss-tts"} and not (
            settings.speech_backend == "moss-tts"
            and moss_cpp_requested(settings.tts_model)
        ):
            ensure_speech_runtime(
                settings.speech_backend,
                cancellation=cancellation,
                progress=progress,
            )
        progress("Checking OCR, audio, permissions, and speech assets...")
        return self.run(settings)

    def _check_platform_permissions(self, settings):
        status = self.permission_status_provider()
        screen_capture = status.get("screen_capture")
        accessibility = status.get("accessibility")
        needs_accessibility = _auto_advance_requires_accessibility(settings)
        if screen_capture is None and accessibility is None:
            return None
        missing = []
        if screen_capture is False:
            missing.append("Screen Recording for game capture")
        if accessibility is False and needs_accessibility:
            missing.append("Accessibility for auto advance")
        if missing:
            return DiagnosticResult(
                "macOS permissions",
                "error",
                f"Missing {', '.join(missing)}. Allow the terminal or VNTTS "
                "under System Settings -> Privacy & Security, then restart it.",
                "permissions",
            )
        if screen_capture is None or (needs_accessibility and accessibility is None):
            return DiagnosticResult(
                "macOS permissions",
                "warning",
                "One or more permission states could not be checked",
                "permissions",
            )
        message = "Screen Recording is granted"
        if needs_accessibility:
            message += " and Accessibility is granted"
        return DiagnosticResult("macOS permissions", "ok", message)

    def _check_hotkeys(self, settings):
        try:
            validate_hotkey_assignments(
                {
                    "Read once": settings.read_hotkey,
                    "Live reading": settings.live_hotkey,
                }
            )
        except HotkeyValidationError as error:
            return DiagnosticResult("Hotkeys", "error", str(error), "settings")
        return DiagnosticResult("Hotkeys", "ok", "Read and live hotkeys are valid")

    def _check_capture_source(self, settings):
        if settings.capture_mode == "window" and not settings.game_window_title:
            return DiagnosticResult(
                "Capture source",
                "error",
                "No game window has been selected",
                "settings",
            )
        description = (
            settings.game_window_title
            if settings.capture_mode == "window"
            else "Calibrated screen region"
        )
        return DiagnosticResult("Capture source", "ok", description)

    def _check_tesseract(self):
        try:
            version = self.tesseract_probe()
        except Exception as error:
            return DiagnosticResult("Tesseract OCR", "error", str(error))
        return DiagnosticResult("Tesseract OCR", "ok", f"Version {version}")

    def _check_audio(self):
        try:
            device = self.audio_probe()
        except Exception as error:
            return DiagnosticResult("Audio output", "error", str(error))
        return DiagnosticResult("Audio output", "ok", str(device))

    def _check_model(self, settings):
        from vntts.moss_cpp_backend import moss_cpp_paths, moss_cpp_requested

        if settings.speech_backend == "moss-tts" and moss_cpp_requested(
            settings.tts_model
        ):
            try:
                executable, model, _sidecar = moss_cpp_paths(settings.tts_model)
            except Exception as error:
                return DiagnosticResult(
                    "MOSS C++ runtime", "error", str(error), "settings"
                )
            return DiagnosticResult(
                "MOSS C++ runtime",
                "warning",
                f"Found {executable.name} and {model.name}; render test still required",
            )
        if not packaged_speech_backend_available(settings.speech_backend):
            return DiagnosticResult(
                "Speech runtime",
                "error",
                f"{settings.speech_backend} is not included in this application "
                "package. Choose Pocket TTS or XTTS in Settings.",
                "settings",
            )
        isolated_runtime = {
            "pocket-tts": "Pocket TTS runtime",
            "chatterbox-nano": "Chatterbox Nano runtime",
            "moss-tts": "MOSS-TTS runtime",
        }.get(settings.speech_backend)
        if isolated_runtime is not None:
            name = isolated_runtime
            try:
                runtime, _interpreter, _site = resolve_speech_runtime_paths(
                    settings.speech_backend
                )
            except Exception as error:
                return DiagnosticResult(name, "error", str(error), "settings")
            return DiagnosticResult(name, "ok", f"Installed at {runtime}")

        model_name = settings.tts_model
        if not model_name:
            return DiagnosticResult(
                "Speech model", "error", "No model configured", "settings"
            )
        try:
            model_path = self.model_path_resolver(model_name)
        except Exception as error:
            return DiagnosticResult("Speech model", "error", str(error), "settings")
        if Path(model_path).is_dir():
            return DiagnosticResult("Speech model", "ok", f"Cached at {model_path}")
        return DiagnosticResult(
            "Speech model",
            "warning",
            "Not cached yet; it will be downloaded before the final test",
        )

    def _check_voice_manifest(self, settings):
        if not settings.voice_manifest:
            if settings.speech_backend == "pocket-tts":
                return DiagnosticResult(
                    "Character voices",
                    "ok",
                    "Pocket's built-in Alba voice will narrate and cover unknown speakers",
                )
            if (
                settings.speech_backend == "coqui-xtts"
                and not settings.narrator_speaker
            ):
                return DiagnosticResult(
                    "Character voices",
                    "error",
                    "XTTS requires a narrator speaker or a voice pack",
                    "settings",
                )
            return DiagnosticResult(
                "Character voices",
                "warning",
                "No voice pack selected; unknown speakers will use the narrator",
                "settings",
            )
        try:
            registry = CharacterVoiceRegistry.from_file(settings.voice_manifest)
        except VoiceManifestError as error:
            return DiagnosticResult("Character voices", "error", str(error), "settings")

        voices = {id(voice): voice for voice in registry.voices.values()}.values()
        missing = [
            reference
            for voice in voices
            for reference in voice.references
            if not reference.is_file()
        ]
        if missing:
            return DiagnosticResult(
                "Character voices",
                "error",
                f"Missing voice reference: {missing[0]}",
                "voices",
            )
        return DiagnosticResult(
            "Character voices",
            "ok",
            f"Loaded {len(list(voices))} character voices",
        )


def probe_tesseract():
    import pytesseract

    return pytesseract.get_tesseract_version()


def probe_audio_output():
    import sounddevice

    device = sounddevice.query_devices(kind="output")
    if not device:
        raise RuntimeError("No default audio output device is available")
    if isinstance(device, dict):
        return device.get("name") or "Default output device"
    return getattr(device, "name", None) or str(device)


def _auto_advance_requires_accessibility(settings):
    if not settings.auto_advance_enabled or not auto_advance_allowed(
        settings.capture_mode,
        settings.live_sequence_mode,
    ):
        return False
    if settings.live_sequence_mode == "audio-auto":
        return bool(settings.story_index and settings.live_sequence_plan)
    return settings.live_sequence_mode != "audio-manual"


def get_model_cache_path(model_name):
    return ModelAssetManager().model_path(model_name)
