from dataclasses import dataclass
from pathlib import Path

from vntts.assets import ModelAssetManager
from vntts.hotkeys import HotkeyValidationError, validate_hotkey_assignments
from vntts.macos import get_macos_permission_status
from vntts.release_backends import packaged_speech_backend_available
from vntts.speech_backend import (
    activate_chatterbox_runtime,
    activate_moss_tts_runtime,
    activate_pocket_tts_runtime,
)
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
        if not packaged_speech_backend_available(settings.speech_backend):
            return DiagnosticResult(
                "Speech runtime",
                "error",
                f"{settings.speech_backend} is not included in this application "
                "package. Choose Pocket TTS or XTTS in Settings.",
                "settings",
            )
        isolated_runtime = {
            "pocket-tts": ("Pocket TTS runtime", activate_pocket_tts_runtime),
            "chatterbox-nano": (
                "Chatterbox Nano runtime",
                activate_chatterbox_runtime,
            ),
            "moss-tts": ("MOSS-TTS runtime", activate_moss_tts_runtime),
        }.get(settings.speech_backend)
        if isolated_runtime is not None:
            name, probe = isolated_runtime
            try:
                runtime = probe()
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
            if (
                settings.tts_model
                and "xtts" in settings.tts_model.casefold()
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
    if not settings.auto_advance_enabled:
        return False
    if settings.live_sequence_mode == "audio-auto":
        return bool(settings.story_index and settings.live_sequence_plan)
    return settings.live_sequence_mode != "audio-manual"


def get_model_cache_path(model_name):
    return ModelAssetManager().model_path(model_name)
