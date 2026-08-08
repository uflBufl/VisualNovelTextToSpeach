import pytesseract
import mss
import os
import sys

from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from datetime import datetime
from pathlib import Path
from threading import Lock
from pynput import keyboard

from vntts.dialog import is_empty, recognize_dialog, speak_dialog
from vntts.services.tts_engine import AudioPlaybackError, TTSEngine, TTSError

# Dialog box with speaker name included (on my 2560x1440 monitor)
dialog_height = 350

screenshot_path = 'logs/screenshots'
default_hotkey = '<ctrl>+<shift>+h'
# Create directory if not exist
Path(screenshot_path).mkdir(parents=True, exist_ok=True)


class ScreenCaptureError(RuntimeError):
    pass


class OCRError(RuntimeError):
    pass


def capture_dialog():
    try:
        with mss.mss() as sct:
            # Take first monitor sizes
            monitor = sct.monitors[1]

            # Dialog box on screen (only works if game in fullscreen)
            dialog_box = {
                'left': 0,
                'top': monitor['height'] - dialog_height,
                'width': monitor['width'],
                'height': dialog_height,
            }

            screenshot = sct.grab(dialog_box)

            image = Image.frombytes(
                'RGB',
                screenshot.size,
                screenshot.bgra,
                'raw',
                'BGRX',
            )

        now = datetime.now()
        formatted_date = now.strftime('%Y-%m-%d-%H-%M-%S')
        output = f'{screenshot_path}/dialog-{formatted_date}.png'
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


def read_dialog(tts):
    image, output = capture_dialog()
    character, text = recognize_screenshot(image)

    if is_empty(text):
        print(f'Screenshot {output} has no text')
    else:
        print(f'{character} is speaking now')
        print(f'Screenshot {output} with text:\n{text}')

        speak_dialog(text, tts.speak)


def read_dialog_safely(tts):
    try:
        read_dialog(tts)
    except ScreenCaptureError as error:
        print(f'Screen capture failed: {error}', file=sys.stderr)
    except OCRError as error:
        print(f'Tesseract OCR failed: {error}', file=sys.stderr)
    except TTSError as error:
        print(f'TTS model or synthesis failed: {error}', file=sys.stderr)
    except AudioPlaybackError as error:
        print(f'Audio playback failed: {error}', file=sys.stderr)
    except Exception as error:
        print(f'Unexpected dialog processing failure: {error}', file=sys.stderr)


def get_hotkey():
    hotkey = os.environ.get('VNTTS_HOTKEY', default_hotkey)
    try:
        keyboard.HotKey.parse(hotkey)
    except (TypeError, ValueError) as error:
        print(
            f'Invalid VNTTS_HOTKEY {hotkey!r}: {error}. '
            f'Using default {default_hotkey!r}'
        )
        return default_hotkey

    return hotkey


def create_dialog_read_scheduler(executor, tts):
    active_read = None
    active_read_lock = Lock()

    def schedule_dialog_read():
        nonlocal active_read

        with active_read_lock:
            if active_read is not None and not active_read.done():
                print('A dialog read is already in progress')
                return

            active_read = executor.submit(read_dialog_safely, tts)

    return schedule_dialog_read


def listen_for_hotkey(hotkey, on_activate):
    print(f'Press {hotkey} to read from screen once')
    with keyboard.GlobalHotKeys({hotkey: on_activate}) as listener:
        listener.join()


def initialize_tts(tts_factory=TTSEngine):
    print('Loading TTS model...')
    try:
        tts = tts_factory()
    except Exception as error:
        print(f'Unable to initialize TTS engine: {error}', file=sys.stderr)
        return None

    print('TTS model loaded')
    return tts


def main(tts_factory=TTSEngine):
    tts = initialize_tts(tts_factory)
    if tts is None:
        return 1

    hotkey = get_hotkey()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='dialog-reader') as executor:
        schedule_dialog_read = create_dialog_read_scheduler(executor, tts)
        listen_for_hotkey(hotkey, schedule_dialog_read)

    return 0
