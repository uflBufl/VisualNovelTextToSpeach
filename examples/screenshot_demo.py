import time
from datetime import datetime
from pathlib import Path

import mss
import numpy
import pytesseract
from PIL import Image


# Dialog box with speaker name included (on a 2560x1440 monitor).
dialog_height = 350
screenshot_path = 'logs/screenshots'


def main():
    # Create the screenshot directory if it does not exist.
    Path(screenshot_path).mkdir(parents=True, exist_ok=True)

    with mss.mss() as sct:
        # Use the first physical monitor. Index 0 is the combined virtual screen.
        monitor = sct.monitors[1]

        # Capture the dialog box at the bottom of a fullscreen game.
        dialog_box = {
            'left': 0,
            'top': monitor['height'] - dialog_height,
            'width': monitor['width'],
            'height': dialog_height,
        }

        while True:
            screenshot = sct.grab(dialog_box)
            image = Image.frombytes(
                'RGB',
                screenshot.size,
                screenshot.bgra,
                'raw',
                'BGRX',
            )
            timestamp = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
            output = f'{screenshot_path}/dialog-{timestamp}.png'

            image.save(output)
            text = pytesseract.image_to_string(numpy.asarray(screenshot))
            print(f'Screenshot {output} with text:\n{text}')

            # Wait five seconds before taking the next screenshot.
            time.sleep(5)


if __name__ == '__main__':
    main()
