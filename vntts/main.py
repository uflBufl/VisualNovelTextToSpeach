"""Compatibility facade and command-line entry point."""

# Re-exports preserve the historical vntts.main import surface.
# ruff: noqa: F401

import sys

from pynput import keyboard

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
    fingerprint_dialog_frame,
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
    get_clear_queue_hotkey,
    get_emergency_stop_hotkey,
    get_hotkey,
    get_live_configuration,
    get_live_hotkey,
    get_pause_hotkey,
    get_repeat_hotkey,
    get_skip_hotkey,
    get_tts_configuration,
    initialize_tts,
    initialize_voice_registry,
    initialize_voice_router,
)
from vntts.services.tts_engine import TTSEngine
from vntts.settings import load_app_settings
from vntts.window_capture import enable_windows_dpi_awareness


def listen_for_hotkeys(
    hotkey,
    live_hotkey,
    pause_hotkey,
    skip_hotkey,
    repeat_hotkey,
    clear_queue_hotkey,
    emergency_stop_hotkey,
    on_activate,
    on_live_toggle,
    on_pause_toggle,
    on_skip,
    on_repeat,
    on_clear_queue,
    on_emergency_stop,
):
    print(f"Press {hotkey} to read from screen once")
    print(f"Press {live_hotkey} to start or stop live reading")
    print(f"Press {pause_hotkey} to pause or resume speech")
    print(f"Press {skip_hotkey} to skip current speech")
    print(f"Press {repeat_hotkey} to repeat the last speech")
    print(f"Press {clear_queue_hotkey} to clear the speech queue")
    print(f"Press {emergency_stop_hotkey} for an emergency stop")
    with keyboard.GlobalHotKeys(
        {
            hotkey: on_activate,
            live_hotkey: on_live_toggle,
            pause_hotkey: on_pause_toggle,
            skip_hotkey: on_skip,
            repeat_hotkey: on_repeat,
            clear_queue_hotkey: on_clear_queue,
            emergency_stop_hotkey: on_emergency_stop,
        }
    ) as listener:
        listener.join()


def main(tts_factory=TTSEngine):
    enable_windows_dpi_awareness()
    settings = load_app_settings()
    controller = AppController(settings, tts_factory=tts_factory)
    if not controller.start():
        return 1

    hotkey = get_hotkey(settings)
    live_hotkey = get_live_hotkey(settings)
    pause_hotkey = get_pause_hotkey(settings)
    skip_hotkey = get_skip_hotkey(settings)
    repeat_hotkey = get_repeat_hotkey(settings)
    clear_queue_hotkey = get_clear_queue_hotkey(settings)
    emergency_stop_hotkey = get_emergency_stop_hotkey(settings)
    try:
        validate_hotkey_assignments(
            {
                "Read once": hotkey,
                "Live reading": live_hotkey,
                "Pause or resume": pause_hotkey,
                "Skip speech": skip_hotkey,
                "Repeat speech": repeat_hotkey,
                "Clear queue": clear_queue_hotkey,
                "Emergency stop": emergency_stop_hotkey,
            }
        )
    except HotkeyValidationError as error:
        print(f"Invalid hotkeys: {error}", file=sys.stderr)
        controller.shutdown()
        return 1

    try:
        listen_for_hotkeys(
            hotkey,
            live_hotkey,
            pause_hotkey,
            skip_hotkey,
            repeat_hotkey,
            clear_queue_hotkey,
            emergency_stop_hotkey,
            controller.read_once,
            controller.toggle_live,
            controller.toggle_speech_pause,
            controller.skip_current_speech,
            controller.repeat_last_speech,
            controller.clear_speech_queue,
            controller.emergency_stop,
        )
    finally:
        controller.shutdown()

    return 0
