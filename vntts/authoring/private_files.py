import stat
import sys
from pathlib import Path


def private_file_is_restricted(path, *, platform=None):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return False
    if (platform or sys.platform) == "win32":
        return True
    return stat.S_IMODE(path.stat().st_mode) == 0o600
