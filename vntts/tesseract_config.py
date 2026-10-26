"""Shared Tesseract settings for native and subprocess OCR."""

import subprocess

SAUVOLA_PARAMETERS: tuple[tuple[str, str], ...] = (
    ("thresholding_method", "2"),
    ("thresholding_window_size", "0.33"),
    ("thresholding_kfactor", "0.5"),
)
_SAUVOLA_CLI_EXECUTABLES: set[str] = set()


def tesseract_config(psm: int) -> str:
    return f"--psm {psm}" + "".join(
        f" -c {name}={value}" for name, value in SAUVOLA_PARAMETERS
    )


def require_sauvola_cli(executable: str, timeout: float | None) -> None:
    if executable in _SAUVOLA_CLI_EXECUTABLES:
        return
    remedy = "Install Tesseract 5 or configure a matching Tesseract executable."
    try:
        result = subprocess.run(
            [executable, "--print-parameters"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10.0 if timeout is None else timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            f"Unable to verify Tesseract CLI Sauvola support: {error}. {remedy}"
        ) from error
    supported = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    missing = [name for name, _value in SAUVOLA_PARAMETERS if name not in supported]
    if missing:
        raise RuntimeError(
            f"Tesseract CLI lacks required Sauvola settings: {', '.join(missing)}. "
            f"{remedy}"
        )
    _SAUVOLA_CLI_EXECUTABLES.add(executable)
