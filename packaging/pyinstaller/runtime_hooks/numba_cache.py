"""Keep Numba's generated cache outside the signed application bundle."""

import os
import sys
from pathlib import Path

if sys.platform == "darwin":
    cache_root = Path.home() / "Library" / "Caches"
elif sys.platform == "win32":
    cache_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
else:
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))

os.environ.setdefault(
    "NUMBA_CACHE_DIR",
    str(cache_root / "VisualNovelTextToSpeech" / "numba"),
)
