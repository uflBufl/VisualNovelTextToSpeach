import pytesseract
import numpy
import mss
import os

from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from datetime import datetime
from pathlib import Path
from threading import Lock
from pynput import keyboard

from vntts.services.tts_engine import TTSEngine

# Dialog box with speaker name included (on my 2560x1440 monitor)
dialog_height = 350

screenshot_path = 'logs/screenshots'
default_hotkey = '<ctrl>+<shift>+h'
# Create directory if not exist
Path(screenshot_path).mkdir(parents=True, exist_ok=True)

tts = TTSEngine()

def read_dialog():
    with mss.mss() as sct:
        # Take first monitor sizes
        monitor = sct.monitors[1]

        # Dialog box on screen (only works if game in fullscreen)
        dialog_box = {
            'left': 0,
            'top': monitor['height'] - dialog_height,
            'width': monitor['width'],
            'height': dialog_height
        }

        screenshot = sct.grab(dialog_box)

        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

        now = datetime.now()
        formatted_date = now.strftime("%Y-%m-%d-%H-%M-%S")
        output = f'{screenshot_path}/dialog-{formatted_date}.png'

        img.save(output)

        screenshot_bytes = numpy.asarray(screenshot)
        text = pytesseract.image_to_string(screenshot_bytes)

        lines = text.split('\n')
        character = 'Narrator'
        if len(lines) > 3 and is_empty(lines[1]):
            character = lines[0].strip()
            lines = lines[2:]
        text = ' '.join(line.strip() for line in lines)

        if is_empty(text):
            print(f'Screenshot {output} has no text')
        else:
            print(f'{character} is speaking now')
            print(f'Screenshot {output} with text:\n{text}')
            
            tts.speak(text)

def is_empty(text):
    return text is None or text == "" or text.isspace()

def read_dialog_safely():
    try:
        read_dialog()
    except Exception as error:
        print(f'Unable to read dialog: {error}')

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

def create_dialog_read_scheduler(executor):
    active_read = None
    active_read_lock = Lock()

    def schedule_dialog_read():
        nonlocal active_read

        with active_read_lock:
            if active_read is not None and not active_read.done():
                print('A dialog read is already in progress')
                return

            active_read = executor.submit(read_dialog_safely)

    return schedule_dialog_read

def listen_for_hotkey(hotkey, on_activate):
    print(f'Press {hotkey} to read from screen once')
    with keyboard.GlobalHotKeys({hotkey: on_activate}) as listener:
        listener.join()

def main():
    hotkey = get_hotkey()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='dialog-reader') as executor:
        schedule_dialog_read = create_dialog_read_scheduler(executor)
        listen_for_hotkey(hotkey, schedule_dialog_read)
