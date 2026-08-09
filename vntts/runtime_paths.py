import os
import sys
from pathlib import Path


def get_bundle_root():
    if not getattr(sys, "frozen", False):
        return None
    bundle_root = getattr(sys, "_MEIPASS", None)
    return Path(bundle_root).resolve() if bundle_root else None


def find_bundled_espeak(bundle_root=None):
    bundle_root = get_bundle_root() if bundle_root is None else Path(bundle_root)
    if bundle_root is None:
        return None
    espeak_root = bundle_root / "espeak-ng"
    executables = list(espeak_root.rglob("espeak-ng.exe"))
    data_directories = list(espeak_root.rglob("espeak-ng-data"))
    if not executables or not data_directories:
        return None
    return executables[0], data_directories[0]


def configure_bundled_dependencies(bundle_root=None):
    bundle_root = get_bundle_root() if bundle_root is None else Path(bundle_root)
    if bundle_root is None:
        return None

    bundled_espeak = find_bundled_espeak(bundle_root)
    if bundled_espeak is not None:
        espeak_executable, espeak_data = bundled_espeak
        current_path = os.environ.get("PATH", "")
        path_entries = [str(espeak_executable.parent)]
        if current_path:
            path_entries.append(current_path)
        os.environ["PATH"] = os.pathsep.join(path_entries)
        os.environ["ESPEAK_DATA_PATH"] = str(espeak_data)

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
