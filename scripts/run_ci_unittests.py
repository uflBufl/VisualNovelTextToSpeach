import faulthandler
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vntts.cleanup import cleanup_on_exit, temporary_directory
from vntts.cli import cli_message

SHARD_TIMEOUTS = {
    "Darwin": {
        "qt-app": 180,
        "qt-app-2": 180,
        "qt-assets": 60,
        "qt-ocr": 60,
        "qt-pregeneration": 180,
        "remainder": 900,
    },
    "Windows": {
        "qt-app": 300,
        "qt-app-2": 300,
        "qt-assets": 60,
        "qt-ocr": 60,
        "qt-pregeneration": 300,
        "remainder": 900,
    },
}


def escape_workflow_command(value):
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def workflow_failure_details(value):
    if len(value) <= 4_000:
        return value
    prefix = value[:2_500]
    tests = re.findall(r"^test_[^\r\n]*", value, flags=re.MULTILINE)
    last_test = f"\nLast test: {tests[-1][:300]}" if tests else ""
    tail_size = 3_950 - len(prefix) - len(last_test)
    return prefix + last_test + "\n... output truncated ...\n" + value[-tail_size:]


def workflow_failure_sections(value):
    return tuple(
        section.strip()
        for section in re.split(r"^={70}\r?$", value, flags=re.MULTILINE)
        if section.lstrip().startswith(("FAIL: ", "ERROR: "))
    )


def _flatten_suite(suite):
    for value in suite:
        if isinstance(value, unittest.TestSuite):
            yield from _flatten_suite(value)
        else:
            yield value


def partition_ui_test_ids(test_ids):
    """Assign every exact test once, isolating the crash-prone Qt app module."""
    values = list(test_ids)
    if len(values) != len(set(values)):
        raise ValueError("Full test discovery contains duplicate test IDs")
    app = tuple(value for value in values if value.startswith("tests.test_app."))
    assets = tuple(
        value for value in values if value.startswith("tests.test_asset_ui.")
    )
    ocr = tuple(
        value
        for value in values
        if value.startswith(("tests.test_ocr_corrections.", "tests.test_ocr_review."))
    )
    isolated = set((*app, *assets, *ocr))
    remainder = tuple(value for value in values if value not in isolated)
    if (
        not app
        or not assets
        or not ocr
        or not remainder
        or isolated.intersection(remainder)
    ):
        raise ValueError("UI unittest shards are incomplete or overlap")
    if sorted((*app, *assets, *ocr, *remainder)) != sorted(values):
        raise ValueError("UI unittest shards do not cover exact discovery")
    return app, assets, ocr, remainder


def _isolate_pregeneration_tests(test_ids):
    isolated = tuple(
        value
        for value in test_ids
        if value.startswith("tests.test_self_service_pregeneration.")
    )
    isolated_set = set(isolated)
    return isolated, tuple(value for value in test_ids if value not in isolated_set)


def _run_exact_test_file(path):
    try:
        test_ids = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"Unable to load exact test inventory: {error}", file=sys.stderr)
        return 2
    if (
        not isinstance(test_ids, list)
        or not test_ids
        or any(not isinstance(value, str) or not value for value in test_ids)
        or len(test_ids) != len(set(test_ids))
    ):
        print("Exact test inventory is malformed", file=sys.stderr)
        return 2
    suite = unittest.defaultTestLoader.loadTestsFromNames(test_ids)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun != len(test_ids):
        print(
            f"Exact test inventory expected {len(test_ids)} tests but ran "
            f"{result.testsRun}",
            file=sys.stderr,
        )
        return 2
    return 0 if result.wasSuccessful() else 1


def _run_sharded_full_discovery(system, selected_modules=None):
    suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
    test_ids = tuple(value.id() for value in _flatten_suite(suite))
    if selected_modules is not None:
        missing = [
            module
            for module in selected_modules
            if not any(value.startswith(f"{module}.") for value in test_ids)
        ]
        if missing:
            print(
                f"Requested unittest modules have no discovered tests: {', '.join(missing)}",
                file=sys.stderr,
            )
            return 2
    try:
        app_ids, asset_ids, ocr_ids, remainder_ids = partition_ui_test_ids(test_ids)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    with temporary_directory(prefix="vntts-unittest-shards-") as directory:
        root = Path(directory)
        midpoint = (len(app_ids) + 1) // 2
        app_shards = [("qt-app", app_ids[:midpoint])]
        if len(app_ids) > midpoint:
            app_shards.append(("qt-app-2", app_ids[midpoint:]))
        pregeneration_ids, remainder_ids = _isolate_pregeneration_tests(remainder_ids)
        shards = (
            *app_shards,
            ("qt-assets", asset_ids),
            ("qt-ocr", ocr_ids),
            ("qt-pregeneration", pregeneration_ids),
            ("remainder", remainder_ids),
        )
        if selected_modules is not None:
            prefixes = tuple(f"{module}." for module in selected_modules)
            shards = tuple(
                (name, tuple(value for value in ids if value.startswith(prefixes)))
                for name, ids in shards
            )
        shards = tuple((name, ids) for name, ids in shards if ids)
        if not shards:
            print("No selected unittest cases were discovered", file=sys.stderr)
            return 2
        for name, ids in shards:
            inventory = root / f"{name}.json"
            inventory.write_text(json.dumps(ids), encoding="utf-8")
            print(f"Running {system} unittest shard {name}: {len(ids)} tests")
            command = [
                sys.executable,
                "-u",
                "-m",
                "scripts.run_ci_unittests",
                "--shard",
                name,
                str(inventory),
            ]
            transcript = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            with cleanup_on_exit(
                transcript.close, description="Unittest transcript cleanup"
            ):
                try:
                    completed = subprocess.run(
                        command,
                        timeout=SHARD_TIMEOUTS[system][name],
                        stdout=transcript,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                except subprocess.TimeoutExpired:
                    transcript.seek(0)
                    output = transcript.read()
                    print(output, end="")
                    if os.environ.get("GITHUB_ACTIONS"):
                        print(
                            f"::error title={system} {name} tests timed out::"
                            f"{escape_workflow_command(workflow_failure_details(output))}",
                            file=sys.stderr,
                        )
                    print(
                        f"{system} unittest shard {name} exceeded "
                        f"{SHARD_TIMEOUTS[system][name]} seconds",
                        file=sys.stderr,
                    )
                    return 124
                transcript.seek(0)
                output = transcript.read()
            if completed.returncode:
                print(output, end="")
                if os.environ.get("GITHUB_ACTIONS"):
                    sections = workflow_failure_sections(output)
                    for details in sections or (
                        output
                        or f"{system} unittest shard {name} exited without output.",
                    ):
                        print(
                            f"::error title={system} {name} tests failed::"
                            f"{escape_workflow_command(workflow_failure_details(details))}",
                            file=sys.stderr,
                        )
                return completed.returncode
    count = sum(len(ids) for _, ids in shards)
    print(f"Ran {count} exact discovered tests once in {len(shards)} shards")
    return 0


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments[:1] == ["--shard"]:
        if len(arguments) != 3:
            return 2
        timeout = SHARD_TIMEOUTS.get(platform.system(), {}).get(arguments[1])
        if timeout is None:
            return 2
        faulthandler.enable()
        faulthandler.dump_traceback_later(timeout * 0.8)
        try:
            return _run_exact_test_file(arguments[2])
        finally:
            faulthandler.cancel_dump_traceback_later()
    if arguments[:1] == ["--exact-test-ids-file"]:
        if len(arguments) != 2:
            return 2
        return _run_exact_test_file(arguments[1])
    if arguments[:1] == ["--selected"]:
        modules = arguments[1:]
        if not modules or any(not value.startswith("tests.test_") for value in modules):
            return 2
        modules = list(dict.fromkeys(modules))
        system = platform.system()
        if system in SHARD_TIMEOUTS:
            return _run_sharded_full_discovery(system, modules)
        arguments = modules
    system = platform.system()
    if system in SHARD_TIMEOUTS and arguments == ["discover", "-s", "tests"]:
        return _run_sharded_full_discovery(system)
    completed = subprocess.run(
        [sys.executable, "-u", "-m", "unittest", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(completed.stdout, end="")
    if completed.returncode and os.environ.get("GITHUB_ACTIONS"):
        for section in workflow_failure_sections(completed.stdout):
            print(
                "::error title=Unittest failure::"
                f"{escape_workflow_command(workflow_failure_details(section))}",
                file=sys.stderr,
            )
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
