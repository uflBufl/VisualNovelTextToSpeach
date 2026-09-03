import sys
from dataclasses import dataclass

from PIL import Image

from vntts.voices import is_narrator


@dataclass(frozen=True)
class DiagnosticSnapshot:
    image: Image.Image | None
    character: str = "Narrator"
    text: str = ""
    confidence: float = 0.0
    preprocessing_profile: str = "none"
    voice: str = "Default narrator"
    capture_ms: float | None = None
    ocr_ms: float | None = None
    synthesis_ms: float | None = None
    playback_ms: float | None = None
    capture_interval_ms: float | None = None
    game_focused: bool | None = None
    corrections: tuple[str, ...] = ()
    speech_queue_depth: int = 0
    max_speech_queue_depth: int = 0
    last_first_audio_ms: float | None = None
    cache_source: str | None = None
    audio_source: str = "Not selected"


def resolve_voice_label(voice_router, character):
    if voice_router is None:
        return "Not loaded"

    voice = voice_router.registry.resolve(character)
    if is_narrator(character) or voice is None:
        return voice_router.narrator_speaker or "Default narrator"
    return f"{voice.character} ({voice.speaker})"


def macos_permission_warnings(
    *,
    platform=None,
    screen_capture_trusted=None,
    accessibility_trusted=None,
):
    if (platform or sys.platform) != "darwin":
        return []

    if screen_capture_trusted is None:
        try:
            from Quartz import CGPreflightScreenCaptureAccess

            screen_capture_trusted = CGPreflightScreenCaptureAccess
        except ImportError, AttributeError:
            screen_capture_trusted = None
    if accessibility_trusted is None:
        try:
            from ApplicationServices import AXIsProcessTrusted

            accessibility_trusted = AXIsProcessTrusted
        except ImportError, AttributeError:
            accessibility_trusted = None

    warnings = []
    if screen_capture_trusted is not None and not screen_capture_trusted():
        warnings.append(
            "Screen capture permission is missing. Open System Settings -> "
            "Privacy & Security -> Screen & System Audio Recording, allow this "
            "application, and restart it."
        )
    if accessibility_trusted is not None and not accessibility_trusted():
        warnings.append(
            "Accessibility permission is missing, so auto advance will not work. "
            "Open System Settings -> Privacy & Security -> Accessibility, allow "
            "this application, and restart it."
        )
    return warnings


def diagnostic_error_guidance(error, *, platform=None):
    message = str(error).strip() or error.__class__.__name__
    normalized = message.casefold()
    if "window" in normalized and any(
        phrase in normalized
        for phrase in ("not found", "unavailable", "minimized", "closed")
    ):
        return (
            "The selected game window is unavailable. Start or restore the game, "
            "use windowed or borderless mode, and select it again in Settings. "
            f"Details: {message}"
        )
    if (platform or sys.platform) == "darwin" and any(
        phrase in normalized
        for phrase in ("capture", "display", "screen", "permission", "denied")
    ):
        return (
            "macOS could not capture the dialog region. Check System Settings -> "
            "Privacy & Security -> Screen & System Audio Recording, allow this "
            f"application, and restart it. Details: {message}"
        )
    return message


def diagnostic_remediation(message, *, platform=None):
    """Return the one recovery action appropriate for a diagnostics warning."""
    normalized = str(message).casefold()
    if (platform or sys.platform) == "darwin" and any(
        phrase in normalized
        for phrase in (
            "permission",
            "privacy & security",
            "screen & system audio recording",
            "accessibility",
        )
    ):
        return "macos-permissions", "Open macOS permissions"
    if "window" in normalized and any(
        phrase in normalized
        for phrase in ("not found", "unavailable", "minimized", "closed", "select")
    ):
        return "settings", "Open Settings"
    return None
