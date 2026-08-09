import os
import sys
from pathlib import Path


def get_bundle_root():
    if not getattr(sys, "frozen", False):
        return None
    bundle_root = getattr(sys, "_MEIPASS", None)
    return Path(bundle_root).resolve() if bundle_root else None


def configure_bundled_dependencies(bundle_root=None):
    bundle_root = get_bundle_root() if bundle_root is None else Path(bundle_root)
    if bundle_root is None:
        return None

    tesseract_directory = bundle_root / "tesseract"
    tesseract_executable = tesseract_directory / "tesseract.exe"
    tessdata_directory = tesseract_directory / "tessdata"
    if not tesseract_executable.is_file():
        return None
    if not (tessdata_directory / "eng.traineddata").is_file():
        return None

    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = str(tesseract_executable)
    os.environ["TESSDATA_PREFIX"] = str(tessdata_directory)
    return tesseract_executable
