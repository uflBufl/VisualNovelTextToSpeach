"""Keep Numba's generated cache outside the signed application bundle."""

import os

from platformdirs import user_cache_path

os.environ.setdefault(
    "NUMBA_CACHE_DIR",
    str(user_cache_path("VisualNovelTextToSpeech", appauthor=False) / "numba"),
)
