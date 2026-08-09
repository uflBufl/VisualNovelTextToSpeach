import importlib
import json
import sys
from pathlib import Path

from vntts.onboarding import probe_tesseract
from vntts.runtime_paths import configure_bundled_dependencies, get_bundle_root
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


def run_package_self_test(
    report_path=None, *, import_module=None, tesseract_probe=None
):
    import_module = import_module or importlib.import_module
    tesseract_probe = tesseract_probe or probe_tesseract
    bundled_tesseract = configure_bundled_dependencies()
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
                "message": "Bundled tesseract.exe or English language data is missing",
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

    successful = all(check["status"] == "ok" for check in checks)
    report = {
        "success": successful,
        "frozen": frozen,
        "bundle_root": str(get_bundle_root() or ""),
        "checks": checks,
    }
    report_path = (
        get_local_data_directory() / "package-self-test.json"
        if report_path is None
        else Path(report_path).expanduser()
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = report_path.with_suffix(f"{report_path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(report_path)
    return successful, report_path
