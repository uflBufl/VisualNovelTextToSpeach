#!/usr/bin/env python3
"""Fail when the configured mypy scope drops below its versioned minimum."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPOSITORY_ROOT / "pyproject.toml"
DEFAULT_BASELINE = REPOSITORY_ROOT / "tests" / "fixtures" / "mypy-scope-v1.json"


def check_mypy_scope(config_path: Path, baseline_path: Path) -> list[str]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if baseline.get("schema_version") != 1:
        return ["Mypy scope baseline schema is unsupported"]
    minimum = baseline.get("minimum_files")
    if (
        not isinstance(minimum, list)
        or any(not isinstance(value, str) or not value for value in minimum)
        or len(minimum) != len(set(minimum))
    ):
        return ["Mypy scope baseline inventory is malformed"]
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    configured = config.get("tool", {}).get("mypy", {}).get("files")
    if not isinstance(configured, list) or any(
        not isinstance(value, str) or not value for value in configured
    ):
        return ["tool.mypy.files is missing or malformed"]
    missing = sorted(set(minimum) - set(configured))
    return [f"mypy scope removed required file: {value}" for value in missing]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    arguments = parser.parse_args(argv)
    failures = check_mypy_scope(arguments.config, arguments.baseline)
    if failures:
        print("Mypy scope ratchet failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Mypy scope ratchet passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
