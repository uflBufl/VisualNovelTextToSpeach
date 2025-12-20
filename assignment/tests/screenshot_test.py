import time

import pytesseract
import numpy
import mss

from PIL import Image
from datetime import datetime
from pathlib import Path



# Dialog box with speaker name included (on my 2560x1440 monitor)
dialog_height = 350

screenshot_path = 'logs/screenshots'
# Create directory if not exist
Path(screenshot_path).mkdir(parents=True, exist_ok=True)

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

    while True:
        screenshot = sct.grab(dialog_box)

        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

        now = datetime.now()
        formatted_date = now.strftime("%Y-%m-%d-%H-%M-%S")
        output = f'{screenshot_path}/dialog-{formatted_date}.png'

        img.save(output)

        screenshot_bytes = numpy.asarray(screenshot)
        text = pytesseract.image_to_string(screenshot_bytes)

        print(f'Screenshot {output} with text:\n{text}')

        # Wait 5 seconds
        time.sleep(5)