"""Check every locked runtime or report its outdated direct dependencies."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY_PACKAGES = (
    "mlx",
    "numpy",
    "onnxruntime",
    "pyside6",
    "scipy",
    "torch",
    "torchaudio",
    "torchcodec",
    "torchvision",
)
WHEEL_PLATFORMS = {
    "moss-soundeffect-v2": {"linux"},
    "moss-tts": {"darwin"},
}


def projects() -> tuple[Path, ...]:
    return (
        ROOT,
        *(path.parent for path in sorted((ROOT / "backends").glob("*/pyproject.toml"))),
    )


def run(arguments: list[str]) -> None:
    subprocess.run(arguments, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--outdated",
        action="store_true",
        help="report outdated direct dependencies instead of validating locks and wheels",
    )
    outdated = parser.parse_args().outdated

    for project in projects():
        label = "." if project == ROOT else project.relative_to(ROOT).as_posix()
        print(f"\n== {label} ==", flush=True)
        if outdated:
            run(
                [
                    "uv",
                    "tree",
                    "--project",
                    str(project),
                    "--frozen",
                    "--outdated",
                    "--depth",
                    "1",
                ]
            )
            continue
        run(["uv", "lock", "--project", str(project), "--check", "--python", "3.14"])
        if allowed := WHEEL_PLATFORMS.get(project.name):
            if sys.platform not in allowed:
                print(f"wheel check skipped: unsupported on {sys.platform}", flush=True)
                continue
        command = [
            "uv",
            "sync",
            "--project",
            str(project),
            "--dry-run",
            "--python",
            "3.14",
        ]
        for package in BINARY_PACKAGES:
            command.extend(("--no-build-package", package))
        run(command)


if __name__ == "__main__":
    main()
