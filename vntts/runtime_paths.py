import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import TypeAlias

PathInput: TypeAlias = str | Path

_BUNDLED_SPEECH_RUNTIMES = frozenset(
    {"pocket-tts", "chatterbox-nano", "moss-tts", "moss-tts-delay"}
)

RUNTIME_ENVIRONMENT_VARIABLES = {
    "pocket-tts": "VNTTS_POCKET_TTS_RUNTIME",
    "chatterbox-nano": "VNTTS_CHATTERBOX_RUNTIME",
    "moss-tts": "VNTTS_MOSS_RUNTIME",
    "moss-tts-delay": "VNTTS_MOSS_DELAY_RUNTIME",
}


def source_runtime_project(backend: str) -> Path | None:
    if backend not in RUNTIME_ENVIRONMENT_VARIABLES or get_bundle_root() is not None:
        return None
    project = Path(__file__).resolve().parents[1] / "backends" / backend
    return (
        project
        if all((project / name).is_file() for name in ("pyproject.toml", "uv.lock"))
        else None
    )


def managed_runtime_location(backend: str) -> Path | None:
    """Bind app-owned environments to the shipped dependency recipe and host."""
    from vntts.application_directories import get_local_data_directory

    project = source_runtime_project(backend)
    if project is None:
        return None
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "uv.lock"):
        digest.update(name.encode() + b"\0")
        digest.update((project / name).read_bytes())
    digest.update(f"3.14:{sys.platform}:{platform.machine()}".encode())
    return get_local_data_directory() / "speech-runtimes" / backend / digest.hexdigest()


def find_managed_speech_runtime(backend: str) -> Path | None:
    location = managed_runtime_location(backend)
    if location is None:
        return None
    try:
        report = json.loads((location / "verified.json").read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if (
        isinstance(report, dict)
        and report.get("schema")
        in {
            "vntts.speech-runtime-installation-v1",
            "vntts.speech-runtime-installation-v2",
        }
        and report.get("backend") == backend
        and report.get("recipe") == location.name
    ):
        if report["schema"] == "vntts.speech-runtime-installation-v1":
            return location / "environment"
        from vntts.runtime_ownership import owned_generation

        generation = report.get("generation")
        if not isinstance(generation, str):
            return None
        runtime = location / "generations" / generation / "environment"
        if owned_generation(backend, runtime) is not None:
            return runtime
    return None


def default_source_speech_runtime(backend: str) -> Path:
    source = Path(__file__).resolve().parents[1] / "backends" / backend / ".venv"
    if source.exists():
        return source
    return find_managed_speech_runtime(backend) or source


def get_bundle_root() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    bundle_root = getattr(sys, "_MEIPASS", None)
    return Path(bundle_root).resolve() if bundle_root else None


def find_bundled_speech_runtime(
    backend: str, bundle_root: PathInput | None = None
) -> Path | None:
    if backend not in _BUNDLED_SPEECH_RUNTIMES:
        return None
    bundle_root = get_bundle_root() if bundle_root is None else Path(bundle_root)
    if bundle_root is None:
        return None
    runtime_root = (bundle_root / "speech-runtimes" / backend).resolve()
    return runtime_root if runtime_root.is_dir() else None


def find_bundled_espeak(
    bundle_root: PathInput | None = None,
) -> tuple[Path, Path] | None:
    bundle_root = get_bundle_root() if bundle_root is None else Path(bundle_root)
    if bundle_root is None:
        return None
    espeak_root = bundle_root / "espeak-ng"
    executables = [
        executable
        for name in ("espeak-ng.exe", "espeak-ng")
        for executable in espeak_root.rglob(name)
    ]
    data_directories = list(espeak_root.rglob("espeak-ng-data"))
    if not executables or not data_directories:
        return None
    return executables[0], data_directories[0]


def configure_bundled_dependencies(bundle_root: PathInput | None = None) -> Path | None:
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
    tesseract_executable = next(
        (
            candidate
            for name in ("tesseract.exe", "tesseract")
            if (candidate := tesseract_directory / name).is_file()
        ),
        None,
    )
    tessdata_directory = tesseract_directory / "tessdata"
    if tesseract_executable is None:
        return None
    if not (tessdata_directory / "eng.traineddata").is_file():
        return None

    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = str(tesseract_executable)
    os.environ["TESSDATA_PREFIX"] = str(tessdata_directory)
    return tesseract_executable
