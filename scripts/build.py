"""Prepare Python dependencies and native workers for running from source."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from subprocess import run as run_process

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vntts.path_safety import copy_regular_file  # noqa: E402


def _run(command: list[str]) -> None:
    print(f"Running: {subprocess.list2cmdline(command)}", flush=True)
    run_process(command, cwd=ROOT, check=True)


def _host_target() -> str:
    result = run_process(
        ["rustc", "-vV"], cwd=ROOT, check=True, stdout=subprocess.PIPE, text=True
    )
    for line in result.stdout.splitlines():
        if line.startswith("host: "):
            return line.removeprefix("host: ")
    raise RuntimeError("Rust did not report its host target. Check the Rust toolchain.")


def build() -> None:
    if sys.platform not in {"darwin", "win32"} and not sys.platform.startswith("linux"):
        raise RuntimeError(f"Unsupported source-build platform: {sys.platform}")
    capture_tool = "xcrun" if sys.platform == "darwin" else "powershell"
    tools = ["uv", "cargo", "rustc"]
    if not sys.platform.startswith("linux"):
        tools.append(capture_tool)
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(
            f"Missing build tools: {', '.join(missing)}. See Developer setup in README.md."
        )

    audio = ROOT / "native" / "audio-worker"
    host = _host_target()
    _run(["uv", "sync", "--frozen"])
    _run(
        [
            "cargo",
            "build",
            "--release",
            "--locked",
            "--manifest-path",
            str(audio / "Cargo.toml"),
            "--target-dir",
            str(audio / "target"),
            "--target",
            host,
        ]
    )
    name = "vntts-audio-worker" + (".exe" if sys.platform == "win32" else "")
    destination = audio / "target" / "release" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    copy_regular_file(
        audio / "target" / host / "release" / name, destination, preserve_metadata=True
    )
    if sys.platform == "darwin":
        _run(["bash", str(ROOT / "scripts" / "build-macos-capture.sh")])
    elif sys.platform == "win32":
        _run(
            [
                "powershell",
                "-NoProfile",
                "-File",
                str(ROOT / "scripts" / "build-windows-capture.ps1"),
            ]
        )
    print("Source build complete. Start the application with: uv run vntts-app")


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        build()
    except subprocess.CalledProcessError as error:
        print(f"Source build failed: {error}", file=sys.stderr)
        return error.returncode
    except (OSError, RuntimeError) as error:
        print(f"Source build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
