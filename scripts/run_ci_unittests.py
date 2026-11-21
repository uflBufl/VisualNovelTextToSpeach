import ast
import builtins
import faulthandler
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from subprocess import run as run_process

from vntts.cleanup import cleanup_on_exit, temporary_directory
from vntts.cli import cli_message
from vntts.json_types import decode_json
from vntts.path_safety import read_regular_text

SHARD_TIMEOUTS = {
    "Darwin": {
        "qt-app": 180,
        "qt-app-2": 180,
        "qt-assets": 60,
        "qt-ocr": 60,
        "qt-pregeneration": 180,
        "qt-catalog": 300,
        "remainder": 900,
    },
    "Windows": {
        "qt-app": 300,
        "qt-app-2": 300,
        "qt-assets": 60,
        "qt-ocr": 60,
        "qt-pregeneration": 300,
        "qt-catalog": 300,
        "remainder-1": 900,
        "remainder": 900,
    },
}


def escape_workflow_command(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def workflow_failure_details(value: str) -> str:
    if len(value) <= 4_000:
        return value
    prefix = value[:2_500]
    tests = re.findall(r"^test_[^\r\n]*", value, flags=re.MULTILINE)
    last_test = f"\nLast test: {tests[-1][:300]}" if tests else ""
    tail_size = 3_950 - len(prefix) - len(last_test)
    return prefix + last_test + "\n... output truncated ...\n" + value[-tail_size:]


def workflow_failure_sections(value: str) -> tuple[str, ...]:
    return tuple(
        section.strip()
        for section in re.split(r"^={70}\r?$", value, flags=re.MULTILINE)
        if section.lstrip().startswith(("FAIL: ", "ERROR: "))
    )


def _flatten_suite(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for value in suite:
        if isinstance(value, unittest.TestSuite):
            yield from _flatten_suite(value)
        elif value is None:
            raise ValueError("Unittest discovery contains an empty test slot")
        else:
            yield value


type _TestIds = tuple[str, ...]


def partition_ui_test_ids(
    test_ids: Iterable[str], *, require_all: bool = True
) -> tuple[_TestIds, _TestIds, _TestIds, _TestIds]:
    """Assign every exact test once, isolating the crash-prone Qt app module."""
    values = list(test_ids)
    if len(values) != len(set(values)):
        raise ValueError("Unittest discovery contains duplicate test IDs")
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
    if require_all and not all((app, assets, ocr, remainder)):
        raise ValueError("UI unittest shards are incomplete or overlap")
    if sorted((*app, *assets, *ocr, *remainder)) != sorted(values):
        raise ValueError("UI unittest shards do not cover exact discovery")
    return app, assets, ocr, remainder


def _isolate_module(test_ids: Sequence[str], module: str) -> tuple[_TestIds, _TestIds]:
    isolated = tuple(value for value in test_ids if value.startswith(module + "."))
    isolated_set = set(isolated)
    return isolated, tuple(value for value in test_ids if value not in isolated_set)


def _write_safe_report(lines: Sequence[str]) -> None:
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    content = "\n".join(lines)[:16_000] + "\n"
    for variable in ("GITHUB_STEP_SUMMARY", "VNTTS_TEST_REPORT"):
        if destination := os.environ.get(variable):
            try:
                with Path(destination).open("a", encoding="utf-8") as output:
                    output.write(content)
            except Exception:
                # Reporting must preserve the original test outcome.
                pass


def _report_test_failures(
    result: unittest.TestResult,
    test_ids: Sequence[str],
    *,
    reported: set[str] | None = None,
) -> None:
    if reported is not None and len(reported) >= 40:
        return
    root = Path(__file__).resolve().parents[1]
    inventory = set(test_ids)
    lines: list[str] = []
    for kind, failures in (
        ("FAIL", result.failures),
        ("ERROR", result.errors),
        ("UNEXPECTED SUCCESS", ((test, "") for test in result.unexpectedSuccesses)),
    ):
        if len(lines) >= 40:
            break
        for test, traceback in failures:
            test = getattr(test, "test_case", test)
            if not isinstance(test, unittest.TestCase):
                continue
            test_id = test.id()
            if test_id not in inventory or not re.fullmatch(
                r"tests(?:\.[A-Za-z_][A-Za-z0-9_]*)+", test_id
            ):
                continue
            module, class_name, method = test_id.rsplit(".", 2)
            path = root / (module.replace(".", "/") + ".py")
            try:
                source = read_regular_text(path, encoding="utf-8")
                definition = next(
                    member
                    for node in ast.parse(source).body
                    if isinstance(node, ast.ClassDef) and node.name == class_name
                    for member in node.body
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and member.name == method
                )
            except OSError, UnicodeError, SyntaxError, StopIteration:
                continue
            line = definition.lineno
            source_lines = len(source.splitlines())
            for filename, number in re.findall(
                r'File "([^"\r\n]+)", line ([0-9]+)', traceback
            ):
                if filename == str(path) and 1 <= int(number) <= source_lines:
                    line = int(number)
            exception = ""
            for name in re.findall(
                r"^([A-Za-z_][A-Za-z0-9_]*)(?::|$)", traceback, flags=re.MULTILINE
            ):
                value = getattr(builtins, name, None)
                if isinstance(value, type) and issubclass(value, BaseException):
                    exception = f" {name}"
            record = f"- {kind} `{test_id}`{exception} `{path.relative_to(root).as_posix()}:{line}`"
            if record not in lines:
                lines.append(record)
            if len(lines) >= 40:
                break
    records = lines or ["- Unit tests failed; no source-validated test ID available."]
    pending = [line for line in records if reported is None or line not in reported]
    if reported is not None:
        pending = pending[: 40 - len(reported)]
    if pending:
        _write_safe_report(pending)
        if reported is not None:
            reported.update(pending)


def _run_exact_test_file(path: str | Path) -> int:
    try:
        test_ids = decode_json(read_regular_text(Path(path), encoding="utf-8"))
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
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(test_ids)
    if loader.errors:
        print(*loader.errors, sep="\n", file=sys.stderr)
        return 2
    if [test.id() for test in _flatten_suite(suite)] != test_ids:
        print("Exact test inventory must name individual test cases", file=sys.stderr)
        return 2
    reported: set[str] = set()
    previous = (0, 0, 0)

    def report(result: unittest.TestResult) -> None:
        nonlocal previous
        counts = (
            len(result.failures),
            len(result.errors),
            len(result.unexpectedSuccesses),
        )
        if counts == previous:
            return
        delta = unittest.TestResult()
        delta.failures = result.failures[previous[0] :]
        delta.errors = result.errors[previous[1] :]
        delta.unexpectedSuccesses = result.unexpectedSuccesses[previous[2] :]
        previous = counts
        try:
            _report_test_failures(delta, test_ids, reported=reported)
        except Exception:
            fallback = "- Unit tests failed; safe test report unavailable."
            if fallback not in reported:
                _write_safe_report((fallback,))
                reported.add(fallback)

    class ReportingTestResult(unittest.TextTestResult):
        def stopTest(self, test: unittest.TestCase) -> None:
            super().stopTest(test)
            report(self)

    class ReportingTestRunner(unittest.TextTestRunner):
        def _makeResult(self) -> ReportingTestResult:
            return ReportingTestResult(self.stream, self.descriptions, self.verbosity)

    result = ReportingTestRunner(verbosity=2).run(suite)
    report(result)
    if result.testsRun != len(test_ids):
        print(
            f"Exact test inventory expected {len(test_ids)} tests but ran "
            f"{result.testsRun}",
            file=sys.stderr,
        )
        return 2
    return 0 if result.wasSuccessful() else 1


def _run_shard(system: str, name: str, inventory: Path) -> int:
    command = [
        sys.executable,
        "-u",
        "-m",
        "scripts.run_ci_unittests",
        "--shard",
        name,
        str(inventory),
    ]
    transcript = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    with cleanup_on_exit(transcript.close, description="Unittest transcript cleanup"):
        try:
            completed = run_process(
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
                output or f"{system} unittest shard {name} exited without output.",
            ):
                print(
                    f"::error title={system} {name} tests failed::"
                    f"{escape_workflow_command(workflow_failure_details(details))}",
                    file=sys.stderr,
                )
        return completed.returncode
    return 0


def _run_sharded_full_discovery(
    system: str, selected_modules: Sequence[str] | None = None
) -> int:
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
    return _run_discovered_shards(system, test_ids, selected_modules)


def _run_local_module_shards(system: str, modules: Sequence[str]) -> int:
    loader = unittest.TestLoader()
    test_ids = []
    for module in sorted(modules):
        suite = loader.loadTestsFromName(module)
        if loader.errors:
            print(*loader.errors, sep="\n", file=sys.stderr)
            return 2
        ids = [test.id() for test in _flatten_suite(suite)]
        if not ids or any(not value.startswith(f"{module}.") for value in ids):
            print(f"Missing or foreign unittest cases in {module}", file=sys.stderr)
            return 2
        test_ids.extend(ids)
    return _run_discovered_shards(system, test_ids, require_all=False)


def _run_discovered_shards(
    system: str,
    test_ids: Sequence[str],
    selected_modules: Sequence[str] | None = None,
    *,
    require_all: bool = True,
) -> int:
    try:
        app_ids, asset_ids, ocr_ids, remainder_ids = partition_ui_test_ids(
            test_ids, require_all=require_all
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    with temporary_directory(prefix="vntts-unittest-shards-") as directory:
        root = Path(directory)
        midpoint = (len(app_ids) + 1) // 2
        app_shards = [("qt-app", app_ids[:midpoint])]
        if len(app_ids) > midpoint:
            app_shards.append(("qt-app-2", app_ids[midpoint:]))
        pregeneration_ids, remainder_ids = _isolate_module(
            remainder_ids, "tests.test_self_service_pregeneration"
        )
        catalog_ids, remainder_ids = _isolate_module(
            remainder_ids, "tests.test_ui_catalog"
        )
        early_ids: _TestIds = ()
        if system == "Windows" and require_all and remainder_ids:
            middle_module = remainder_ids[len(remainder_ids) // 2].rsplit(".", 2)[0]
            boundary = next(
                i
                for i, test in enumerate(remainder_ids)
                if test.rsplit(".", 2)[0] == middle_module
            )
            early_ids, remainder_ids = (
                remainder_ids[:boundary],
                remainder_ids[boundary:],
            )
        shards = (
            *app_shards,
            ("qt-assets", asset_ids),
            ("qt-ocr", ocr_ids),
            ("qt-pregeneration", pregeneration_ids),
            ("qt-catalog", catalog_ids),
            ("remainder-1", early_ids),
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
        early_status = 0
        for name, ids in shards:
            inventory = root / f"{name}.json"
            inventory.write_text(json.dumps(ids), encoding="utf-8")
            print(f"Running {system} unittest shard {name}: {len(ids)} tests")
            started = time.monotonic()
            status = _run_shard(system, name, inventory)
            elapsed = round(time.monotonic() - started)
            if status:
                _write_safe_report(
                    (
                        f"- {system} unittest shard {name} exited {status} after {elapsed} seconds.",
                    )
                )
                print(
                    f"{system} unittest shard {name} exited {status} after {elapsed} seconds"
                )
                if name == "remainder-1":
                    early_status = status
                    continue
                return status
            if (
                name in {"qt-catalog", "remainder-1", "remainder"}
                and require_all
                and selected_modules is None
                and os.environ.get("GITHUB_ACTIONS")
            ):
                print(
                    f"::notice title=Unittest timing::{system} {name} completed in {elapsed} seconds"
                )
    if early_status:
        return early_status
    count = sum(len(ids) for _, ids in shards)
    print(f"Ran {count} exact discovered tests once in {len(shards)} shards")
    return 0


def _run_child_shard(timeout: int, inventory: str) -> int:
    faulthandler.enable()
    faulthandler.dump_traceback_later(timeout * 0.8)
    try:
        return _run_exact_test_file(inventory)
    finally:
        # CLI shutdown can still block on surviving non-daemon workers.
        if __name__ != "__main__":
            faulthandler.cancel_dump_traceback_later()


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments[:1] == ["--shard"]:
        if len(arguments) != 3:
            return 2
        timeout = SHARD_TIMEOUTS.get(platform.system(), {}).get(arguments[1])
        if timeout is None:
            return 2
        return _run_child_shard(timeout, arguments[2])
    if arguments[:1] == ["--exact-test-ids-file"]:
        if len(arguments) != 2:
            return 2
        return _run_exact_test_file(arguments[1])
    sharded_selection: dict[str, Callable[[str, Sequence[str]], int]] = {
        "--selected": _run_sharded_full_discovery,
        "--local-selected": _run_local_module_shards,
    }
    if arguments and arguments[0] in sharded_selection:
        modules = arguments[1:]
        if not modules or any(not value.startswith("tests.test_") for value in modules):
            return 2
        modules = list(dict.fromkeys(modules))
        system = platform.system()
        if system in SHARD_TIMEOUTS:
            return sharded_selection[arguments[0]](system, modules)
        arguments = modules
    system = platform.system()
    if system in SHARD_TIMEOUTS and arguments == ["discover", "-s", "tests"]:
        return _run_sharded_full_discovery(system)
    completed = run_process(
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
