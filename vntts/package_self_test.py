import importlib
import subprocess
import sys
import traceback
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json

from vntts.cli import CLIReportResult
from vntts.onboarding import probe_tesseract
from vntts.runtime_paths import (
    configure_bundled_dependencies,
    find_bundled_espeak,
    get_bundle_root,
)
from vntts.settings import get_local_data_directory

required_modules = (
    "PySide6",
    "PIL",
    "TTS.api",
    "TTS.tts.configs.xtts_config",
    "mss",
    "pynput",
    "pytesseract",
    "sounddevice",
    "torch",
    "torchaudio",
)


def probe_espeak(executable):
    completed = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        check=True,
        text=True,
        timeout=15,
    )
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0] if output else "available"


def run_package_self_test(
    report_path=None,
    *,
    import_module=None,
    tesseract_probe=None,
    espeak_probe=None,
):
    import_module = import_module or importlib.import_module
    tesseract_probe = tesseract_probe or probe_tesseract
    espeak_probe = espeak_probe or probe_espeak
    bundled_tesseract = configure_bundled_dependencies()
    bundled_espeak = find_bundled_espeak()
    checks = []

    for module_name in required_modules:
        try:
            import_module(module_name)
        except Exception as error:
            checks.append(
                {
                    "name": f"Import {module_name}",
                    "status": "error",
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                }
            )
        else:
            checks.append(
                {
                    "name": f"Import {module_name}",
                    "status": "ok",
                    "message": "available",
                }
            )

    try:
        version = str(tesseract_probe())
    except Exception as error:
        checks.append(
            {
                "name": "Tesseract OCR",
                "status": "error",
                "message": str(error),
            }
        )
    else:
        checks.append(
            {
                "name": "Tesseract OCR",
                "status": "ok",
                "message": version,
            }
        )

    frozen = bool(getattr(sys, "frozen", False))
    if frozen and bundled_tesseract is None:
        checks.append(
            {
                "name": "Bundled Tesseract",
                "status": "error",
                "message": "Bundled Tesseract executable or English language data is missing",
            }
        )
    elif frozen:
        checks.append(
            {
                "name": "Bundled Tesseract",
                "status": "ok",
                "message": str(bundled_tesseract),
            }
        )
    if frozen and bundled_espeak is None:
        checks.append(
            {
                "name": "Bundled eSpeak-NG",
                "status": "error",
                "message": "Bundled eSpeak-NG executable or voice data is missing",
            }
        )
    elif frozen:
        try:
            espeak_version = espeak_probe(bundled_espeak[0])
        except Exception as error:
            checks.append(
                {
                    "name": "Bundled eSpeak-NG",
                    "status": "error",
                    "message": str(error),
                }
            )
        else:
            checks.append(
                {
                    "name": "Bundled eSpeak-NG",
                    "status": "ok",
                    "message": espeak_version,
                }
            )

    successful = all(check["status"] == "ok" for check in checks)
    report = {
        "success": successful,
        "frozen": frozen,
        "python_executable": str(Path(sys.executable).resolve()),
        "bundle_root": str(get_bundle_root() or ""),
        "checks": checks,
    }
    report_path = (
        get_local_data_directory() / "package-self-test.json"
        if report_path is None
        else Path(report_path).expanduser()
    )
    atomic_write_json(report_path, report)
    return CLIReportResult(successful, report_path)
