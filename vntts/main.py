import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

import mss
import pytesseract
from PIL import Image
from pynput import keyboard

from vntts.dialog import is_empty, recognize_dialog, speak_dialog
from vntts.services.tts_engine import AudioPlaybackError, TTSEngine, TTSError

# Dialog box with speaker name included (on my 2560x1440 monitor)
dialog_height = 350

default_screenshot_directory = Path("logs/screenshots")
default_hotkey = "<ctrl>+<shift>+h"


class ScreenCaptureError(RuntimeError):
    pass


class OCRError(RuntimeError):
    pass


def get_screenshot_directory():
    configured_directory = os.environ.get("VNTTS_SCREENSHOT_DIR")
    if not configured_directory:
        return default_screenshot_directory

    return Path(configured_directory)


def create_screenshot_path(screenshot_directory):
    screenshot_directory = Path(screenshot_directory)
    formatted_date = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return screenshot_directory / f"dialog-{formatted_date}-{uuid4().hex}.png"


def capture_dialog(screenshot_directory=None):
    try:
        if screenshot_directory is None:
            screenshot_directory = get_screenshot_directory()
        screenshot_directory = Path(screenshot_directory)
        screenshot_directory.mkdir(parents=True, exist_ok=True)

        with mss.mss() as sct:
            # Take first monitor sizes
            monitor = sct.monitors[1]

            # Dialog box on screen (only works if game in fullscreen)
            dialog_box = {
                "left": 0,
                "top": monitor["height"] - dialog_height,
                "width": monitor["width"],
                "height": dialog_height,
            }

            screenshot = sct.grab(dialog_box)

            image = Image.frombytes(
                "RGB",
                screenshot.size,
                screenshot.bgra,
                "raw",
                "BGRX",
            )

        output = create_screenshot_path(screenshot_directory)
        image.save(output)
        return image, output
    except Exception as error:
        raise ScreenCaptureError(str(error)) from error


def recognize_screenshot(image):
    try:
        character, text = recognize_dialog(
            image,
            pytesseract.image_to_string,
        )
    except Exception as error:
        raise OCRError(str(error)) from error

    return character, text


def read_dialog(tts, screenshot_directory):
    image, output = capture_dialog(screenshot_directory)
    character, text = recognize_screenshot(image)

    if is_empty(text):
        print(f"Screenshot {output} has no text")
    else:
        print(f"{character} is speaking now")
        print(f"Screenshot {output} with text:\n{text}")

        speak_dialog(text, tts.speak)


def read_dialog_safely(tts, screenshot_directory):
    try:
        read_dialog(tts, screenshot_directory)
    except ScreenCaptureError as error:
        print(f"Screen capture failed: {error}", file=sys.stderr)
    except OCRError as error:
        print(f"Tesseract OCR failed: {error}", file=sys.stderr)
    except TTSError as error:
        print(f"TTS model or synthesis failed: {error}", file=sys.stderr)
    except AudioPlaybackError as error:
        print(f"Audio playback failed: {error}", file=sys.stderr)
    except Exception as error:
        print(f"Unexpected dialog processing failure: {error}", file=sys.stderr)


def get_hotkey():
    hotkey = os.environ.get("VNTTS_HOTKEY", default_hotkey)
    try:
        keyboard.HotKey.parse(hotkey)
    except (TypeError, ValueError) as error:
        print(
            f"Invalid VNTTS_HOTKEY {hotkey!r}: {error}. "
            f"Using default {default_hotkey!r}"
        )
        return default_hotkey

    return hotkey


def create_dialog_read_scheduler(executor, tts, screenshot_directory):
    active_read = None
    active_read_lock = Lock()

    def schedule_dialog_read():
        nonlocal active_read

        with active_read_lock:
            if active_read is not None and not active_read.done():
                print("A dialog read is already in progress")
                return

            active_read = executor.submit(
                read_dialog_safely,
                tts,
                screenshot_directory,
            )

    return schedule_dialog_read


def listen_for_hotkey(hotkey, on_activate):
    print(f"Press {hotkey} to read from screen once")
    with keyboard.GlobalHotKeys({hotkey: on_activate}) as listener:
        listener.join()


def initialize_tts(tts_factory=TTSEngine):
    print("Loading TTS model...")
    try:
        tts = tts_factory()
    except Exception as error:
        print(f"Unable to initialize TTS engine: {error}", file=sys.stderr)
        return None

    print("TTS model loaded")
    return tts


def main(tts_factory=TTSEngine):
    tts = initialize_tts(tts_factory)
    if tts is None:
        return 1

    hotkey = get_hotkey()
    screenshot_directory = get_screenshot_directory()
    print(f"Screenshots will be stored in {screenshot_directory}")
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="dialog-reader"
    ) as executor:
        schedule_dialog_read = create_dialog_read_scheduler(
            executor,
            tts,
            screenshot_directory,
        )
        listen_for_hotkey(hotkey, schedule_dialog_read)

    return 0
