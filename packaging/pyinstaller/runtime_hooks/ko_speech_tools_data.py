import sys
from pathlib import Path

import ko_speech_tools

if getattr(sys, "frozen", False):
    package_directory = Path(sys._MEIPASS) / "ko_speech_tools"
    package_path = str(package_directory)
    if package_path not in ko_speech_tools.__path__:
        ko_speech_tools.__path__.append(package_path)
