import os
import sys
from pathlib import Path

# Unlike the native Windows plugin, offscreen Qt uses FreeType and does not
# discover installed system fonts. Without this it measures missing-glyph boxes.
if sys.platform == "win32":
    os.environ.setdefault(
        "QT_QPA_FONTDIR",
        str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"),
    )
