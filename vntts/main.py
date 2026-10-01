"""Compatibility facade and command-line entry point."""

# Re-exports preserve the historical vntts.main import surface.
# ruff: noqa: F401

from collections.abc import Callable

from pynput import keyboard

from vntts.cli import cli_error
from vntts.controller import (
    AppController,
    create_dialog_read_scheduler,
    create_live_toggle,
    read_live_snapshot,
    speak_live_chunk,
)
from vntts.dialog_capture import (
    CapturedDialogFrame,
    OCRError,
    OCRUncertainError,
    ScreenCaptureError,
    TTSInitializationError,
    analyze_dialog_snapshot,
    capture_dialog,
    capture_live_frame,
    create_screenshot_path,
    dialog_completion_cue_visible,
    dialog_glyphs_visible,
    fingerprint_dialog_frame,
    fingerprint_dialog_render_activity,
    format_runtime_error,
    get_screenshot_directory,
    read_dialog,
    read_dialog_safely,
    recognize_live_frame,
    recognize_screenshot,
    recognize_screenshot_result,
    report_runtime_error,
)
from vntts.hotkeys import HotkeyValidationError, validate_hotkey_assignments
from vntts.runtime_config import (
    get_hotkey,
    get_live_configuration,
    get_live_hotkey,
    get_tts_configuration,
    initialize_tts,
    initialize_voice_registry,
    initialize_voice_router,
)
from vntts.services.tts_engine import TTSEngine
from vntts.settings import load_app_settings
from vntts.window_capture import enable_windows_dpi_awareness


def listen_for_hotkeys(
    hotkey: str,
    live_hotkey: str,
    on_activate: Callable[[], object],
    on_live_toggle: Callable[[], object],
) -> None:
    print(f"Press {hotkey} to read from screen once")
    print(
        f"Press {live_hotkey} to start reading or immediately stop reading and speech"
    )
    listener = keyboard.GlobalHotKeys(
        {
            hotkey: on_activate,
            live_hotkey: on_live_toggle,
        }
    )
    try:
        listener.start()
        listener.wait()
        listener.join()
    finally:
        listener.stop()


def main(tts_factory: Callable[..., object] = TTSEngine) -> int:
    enable_windows_dpi_awareness()
    settings = load_app_settings()
    controller = AppController(settings, tts_factory=tts_factory)
    if not controller.start():
        return 1

    hotkey = get_hotkey(settings)
    live_hotkey = get_live_hotkey(settings)
    try:
        validate_hotkey_assignments(
            {
                "Read once": hotkey,
                "Live reading": live_hotkey,
            }
        )
    except HotkeyValidationError as error:
        controller.shutdown()
        return cli_error(f"Invalid hotkeys: {error}")

    def reading_active() -> bool:
        reader = controller.live_reader
        snapshot = reader.runtime_control_snapshot() if reader is not None else {}
        return bool(
            controller.is_live_running
            or controller.is_one_shot_read_running is True
            or any(snapshot.get(key) for key in ("paused", "speaking", "queued"))
        )

    def read_once() -> None:
        if not reading_active():
            controller.read_once()

    def toggle_reading() -> None:
        if reading_active():
            controller.emergency_stop()
        else:
            controller.toggle_live()

    try:
        listen_for_hotkeys(
            hotkey,
            live_hotkey,
            read_once,
            toggle_reading,
        )
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError) as error:
        return cli_error(f"Unable to listen for hotkeys: {error}")
    finally:
        controller.shutdown()

    return 0
