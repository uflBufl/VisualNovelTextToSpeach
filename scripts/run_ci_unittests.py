import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vntts.cli import cli_message

MACOS_SHARD_TIMEOUTS = {"qt-app": 60, "remainder": 300}


def escape_workflow_command(value):
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def workflow_failure_details(value):
    if len(value) <= 12_000:
        return value
    return value[:4_000] + "\n... output truncated ...\n" + value[-7_950:]


def _flatten_suite(suite):
    for value in suite:
        if isinstance(value, unittest.TestSuite):
            yield from _flatten_suite(value)
        else:
            yield value


def partition_macos_test_ids(test_ids):
    """Assign every exact test once, isolating the crash-prone Qt app module."""
    values = list(test_ids)
    if len(values) != len(set(values)):
        raise ValueError("Full test discovery contains duplicate test IDs")
    app = tuple(value for value in values if value.startswith("tests.test_app."))
    remainder = tuple(value for value in values if value not in set(app))
    if not app or not remainder or set(app).intersection(remainder):
        raise ValueError("macOS unittest shards are incomplete or overlap")
    if sorted((*app, *remainder)) != sorted(values):
        raise ValueError("macOS unittest shards do not cover exact discovery")
    return app, remainder


def _run_exact_test_file(path):
    try:
        test_ids = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Unable to load exact test inventory: {error}", file=sys.stderr)
        return 2
    if (
        not isinstance(test_ids, list)
        or not test_ids
        or any(not isinstance(value, str) or not value for value in test_ids)
    ):
        print("Exact test inventory is malformed", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.loadTestsFromNames(test_ids)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if result.testsRun != len(test_ids):
        print(
            f"Exact test inventory expected {len(test_ids)} tests but ran "
            f"{result.testsRun}",
            file=sys.stderr,
        )
        return 2
    return 0 if result.wasSuccessful() else 1


def _run_macos_full_discovery():
    suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
    test_ids = tuple(value.id() for value in _flatten_suite(suite))
    try:
        app_ids, remainder_ids = partition_macos_test_ids(test_ids)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="vntts-unittest-shards-") as directory:
        root = Path(directory)
        shards = (("qt-app", app_ids), ("remainder", remainder_ids))
        for name, ids in shards:
            inventory = root / f"{name}.json"
            inventory.write_text(json.dumps(ids), encoding="utf-8")
            print(f"Running macOS unittest shard {name}: {len(ids)} tests")
            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "scripts.run_ci_unittests",
                        "--exact-test-ids-file",
                        str(inventory),
                    ],
                    timeout=MACOS_SHARD_TIMEOUTS[name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                print(
                    f"macOS unittest shard {name} exceeded "
                    f"{MACOS_SHARD_TIMEOUTS[name]} seconds",
                    file=sys.stderr,
                )
                return 124
            print(completed.stdout, end="")
            if completed.returncode:
                if os.environ.get("GITHUB_ACTIONS"):
                    details = workflow_failure_details(completed.stdout) or (
                        f"macOS unittest shard {name} exited without output."
                    )
                    print(
                        f"::error title=macOS {name} tests failed::"
                        f"{escape_workflow_command(details)}",
                        file=sys.stderr,
                    )
                return completed.returncode
    print(f"Ran all {len(test_ids)} exact discovered tests once in 2 shards")
    return 0


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments[:1] == ["--exact-test-ids-file"]:
        if len(arguments) != 2:
            return 2
        return _run_exact_test_file(arguments[1])
    if platform.system() == "Darwin" and arguments == ["discover", "-s", "tests"]:
        return _run_macos_full_discovery()
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(completed.stdout, end="")
    if completed.returncode and os.environ.get("GITHUB_ACTIONS"):
        details = (
            workflow_failure_details(completed.stdout)
            or "Unit tests exited without output."
        )
        return cli_message(
            f"::error title=Unit tests failed::{escape_workflow_command(details)}",
            exit_code=completed.returncode,
            error=True,
        )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
