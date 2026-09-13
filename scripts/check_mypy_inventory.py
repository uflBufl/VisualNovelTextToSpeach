#!/usr/bin/env python3
"""Fail when type-checking debt grows anywhere in production code."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPOSITORY_ROOT / "tests" / "fixtures" / "mypy-inventory-v1.json"
ERROR_PATTERN = re.compile(r"^(.*?\.py):\d+(?::\d+)?: error:")


def error_counts(output: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for line in output.splitlines():
        match = ERROR_PATTERN.match(line)
        if match:
            counts[match.group(1).replace("\\", "/")] += 1
    return counts


def baseline_counts(baseline: object) -> dict[str, int]:
    if not isinstance(baseline, dict) or baseline.get("schema_version") != 1:
        raise ValueError("Mypy inventory baseline schema is unsupported")
    files = baseline.get("maximum_errors_by_file")
    if not isinstance(files, dict) or any(
        not isinstance(path, str)
        or not path
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
        for path, count in files.items()
    ):
        raise ValueError("Mypy inventory baseline is malformed")
    return files


def check_counts(baseline: object, current: Counter[str]) -> list[str]:
    allowed = baseline_counts(baseline)
    return [
        f"mypy debt increased in {path}: {count} > {allowed.get(path, 0)}"
        for path, count in sorted(current.items())
        if count > allowed.get(path, 0)
    ]


def mypy_output(root: Path) -> str:
    completed = subprocess.run(
        ["mypy", "--no-error-summary", "--show-error-codes", "vntts"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        raise subprocess.CalledProcessError(
            completed.returncode,
            completed.args,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    arguments = parser.parse_args(argv)
    try:
        baseline = json.loads(arguments.baseline.read_text(encoding="utf-8"))
        failures = check_counts(baseline, error_counts(mypy_output(arguments.root)))
    except (
        OSError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(f"Mypy inventory ratchet failed: {error}")
        return 1
    if failures:
        print("Mypy inventory ratchet failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Mypy inventory ratchet passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
