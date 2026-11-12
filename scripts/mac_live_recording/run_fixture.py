"""Record only an owned fixture: RGB phases, or a shorter resize/fullscreen check."""

from __future__ import annotations

import argparse
import json
import select
import subprocess
from pathlib import Path


def run(recorder: Path, fixture: Path, output: Path, *, resize: bool = False) -> None:
    subprocess.run([str(recorder), "--preflight"], check=True, timeout=5)
    process = subprocess.Popen(
        [str(fixture), *(["--resize"] if resize else [])],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        if not select.select([process.stdout], [], [], 5)[0]:
            raise TimeoutError(
                "Fixture did not report its explicit window within five seconds"
            )
        target = json.loads(process.stdout.readline())
        print(
            f"Capturing own generated fixture only: PID {target['pid']}, window {target['window_id']}",
            flush=True,
        )
        subprocess.run(
            [
                str(recorder),
                "--video-only",
                str(target["pid"]),
                str(target["window_id"]),
                "33" if resize else "65",
                str(output),
            ],
            check=True,
            timeout=83,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        if process.stdout is not None:
            if resize and output.is_dir():
                (output / "fixture.jsonl").write_text(process.stdout.read())
            process.stdout.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recorder", type=Path)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--resize", action="store_true")
    args = parser.parse_args()
    run(
        args.recorder.resolve(),
        args.fixture.resolve(),
        args.output.resolve(),
        resize=args.resize,
    )


if __name__ == "__main__":
    main()
