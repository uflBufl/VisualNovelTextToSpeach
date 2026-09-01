import sys
from pathlib import Path
from unittest import SkipTest


def symlink_or_skip(link, target, *, target_is_directory=False):
    try:
        Path(link).symlink_to(target, target_is_directory=target_is_directory)
    except OSError as error:
        if sys.platform == "win32" and getattr(error, "winerror", None) == 1314:
            raise SkipTest("Windows symlink privilege is unavailable") from error
        raise
