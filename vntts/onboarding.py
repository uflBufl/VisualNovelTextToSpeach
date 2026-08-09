from dataclasses import dataclass
from pathlib import Path

from vntts.assets import ModelAssetManager
from vntts.hotkeys import HotkeyValidationError, validate_hotkey_assignments
from vntts.voices import CharacterVoiceRegistry, VoiceManifestError


@dataclass(frozen=True)
class DiagnosticResult:
    name: str
    status: str
    message: str

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
    ):
        self.tesseract_probe = tesseract_probe or probe_tesseract
        self.audio_probe = audio_probe or probe_audio_output
        self.model_path_resolver = model_path_resolver or get_model_cache_path

    def run(self, settings):
        return (
            self._check_hotkeys(settings),
            self._check_capture_source(settings),
            self._check_tesseract(),
            self._check_audio(),
            self._check_model(settings),
            self._check_voice_manifest(settings),
        )

    def _check_hotkeys(self, settings):
        try:
            validate_hotkey_assignments(
                {
                    "Read once": settings.read_hotkey,
                    "Live reading": settings.live_hotkey,
                }
            )
        except HotkeyValidationError as error:
            return DiagnosticResult("Hotkeys", "error", str(error))
        return DiagnosticResult("Hotkeys", "ok", "Read and live hotkeys are valid")

    def _check_capture_source(self, settings):
        if settings.capture_mode == "window" and not settings.game_window_title:
            return DiagnosticResult(
                "Capture source",
                "error",
                "No game window has been selected",
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
        model_name = settings.tts_model
        if not model_name:
            return DiagnosticResult("Speech model", "error", "No model configured")
        try:
            model_path = self.model_path_resolver(model_name)
        except Exception as error:
            return DiagnosticResult("Speech model", "error", str(error))
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
                )
            return DiagnosticResult(
                "Character voices",
                "warning",
                "No voice pack selected; unknown speakers will use the narrator",
            )
        try:
            registry = CharacterVoiceRegistry.from_file(settings.voice_manifest)
        except VoiceManifestError as error:
            return DiagnosticResult("Character voices", "error", str(error))

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


def get_model_cache_path(model_name):
    return ModelAssetManager().model_path(model_name)
