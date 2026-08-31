import importlib
import json
import subprocess
import sys
import traceback
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json

from vntts.cli import CLIReportResult
from vntts.onboarding import probe_tesseract
from vntts.release_runtime import PROBE_MODULES, _probe_script
from vntts.runtime_paths import (
    configure_bundled_dependencies,
    find_bundled_espeak,
    get_bundle_root,
)
from vntts.settings import get_local_data_directory
from vntts.speech_worker import _runtime_paths

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


def probe_bundled_pocket_runtime(bundle_root=None, runner=subprocess.run):
    bundle_root = get_bundle_root() if bundle_root is None else Path(bundle_root)
    if bundle_root is None:
        raise RuntimeError("Pocket runtime provenance requires a frozen bundle")
    allowed_root = (bundle_root / "speech-runtimes").resolve()
    runtime_root, interpreter, runtime_site = _runtime_paths("pocket-tts")
    if runtime_root != allowed_root / "pocket-tts":
        raise RuntimeError(
            f"Pocket runtime is outside the frozen bundle: {runtime_root}"
        )
    completed = runner(
        [str(interpreter), "-I", "-c", _probe_script()],
        capture_output=True,
        check=True,
        text=True,
        timeout=120,
    )
    report = json.loads(completed.stdout)
    origins = {
        "interpreter": report.get("executable"),
        "prefix": report.get("prefix"),
        "base_prefix": report.get("base_prefix"),
        **{
            f"module:{name}": report.get("modules", {}).get(name)
            for name in PROBE_MODULES
        },
    }
    missing = sorted(name for name, origin in origins.items() if not origin)
    escaped = {
        name: origin
        for name, origin in origins.items()
        if origin and not Path(origin).resolve().is_relative_to(allowed_root)
    }
    if missing or escaped:
        raise RuntimeError(
            "Bundled Pocket runtime provenance failed: "
            + json.dumps({"missing": missing, "escaped": escaped}, sort_keys=True)
        )
    if not Path(runtime_site).resolve().is_relative_to(runtime_root):
        raise RuntimeError("Pocket runtime site-packages path is inconsistent")
    return report


def run_package_self_test(
    report_path=None,
    *,
    import_module=None,
    tesseract_probe=None,
    espeak_probe=None,
    speech_runtime_probe=None,
):
    import_module = import_module or importlib.import_module
    tesseract_probe = tesseract_probe or probe_tesseract
    espeak_probe = espeak_probe or probe_espeak
    speech_runtime_probe = speech_runtime_probe or probe_bundled_pocket_runtime
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

    if frozen:
        try:
            runtime_report = speech_runtime_probe()
        except Exception as error:
            checks.append(
                {
                    "name": "Bundled Pocket TTS runtime",
                    "status": "error",
                    "message": str(error),
                }
            )
        else:
            checks.append(
                {
                    "name": "Bundled Pocket TTS runtime",
                    "status": "ok",
                    "message": (
                        f"{runtime_report['executable']}; "
                        f"{len(runtime_report['modules'])} modules contained"
                    ),
                    "details": runtime_report,
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
