import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh")
CANARY = "PRIVATE_CANARY_c31827"
CONTEXT = (
    "::error title=Windows unit tests failed::"
    "scripts/run_ci_unittests.py:1; scripts/run_ci_unittests.py:2; "
    "AssertionError; Aborted"
)


@unittest.skipUnless(POWERSHELL, "PowerShell is not installed")
class WindowsCiDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
        job = yaml.safe_load(workflow)["jobs"]["windows-unit-tests"]
        steps = {s.get("name"): s for s in job["steps"]}
        primary = steps["Run Windows unit tests"]
        collector = steps["Report Windows unit test failure"]
        self.assertEqual(collector["if"], "failure()")
        self.assertNotIn("continue-on-error", primary)
        self.command = (
            "uv run --frozen python scripts/run_ci_unittests.py discover -s tests"
        )
        self.primary = primary["run"]
        self.collector = collector["run"]
        self.assertEqual(self.primary.count(self.command), 1)
        self.temporary = Path(self.enterContext(TemporaryDirectory()))
        self.log = self.temporary / "windows-unit-tests.log"
        self.helper = self.temporary / "native.py"
        self.helper.write_text(
            "import os, sys\n"
            "print(os.environ['CI_DIAGNOSTIC_FIXTURE'], flush=True)\n"
            "print('stderr café ' + os.environ['CI_DIAGNOSTIC_FIXTURE'], file=sys.stderr, flush=True)\n"
            "status = int(sys.argv[1])\n"
            "if status == 19:\n    os._exit(status)\n"
            "sys.exit(status)\n",
            encoding="utf-8",
        )
        public = ROOT / "scripts/run_ci_unittests.py"
        invalid_line = len(public.read_text("utf-8").splitlines()) + 1
        self.payload = "\n".join(
            [
                f"::error::{CANARY}",
                f"::stop-commands::{CANARY}",
                f"::{CANARY}::",
                f"ordinary output {CANARY}",
                f"FAIL: test_{CANARY} (tests.{CANARY}.Case.test_{CANARY})",
                f'  File "{public}", line 1, in {CANARY}',
                f'  File "{public}", line 2 in {CANARY}',
                f'  File "{self.temporary / CANARY}.py", line 1, in {CANARY}',
                f'  File "{ROOT / CANARY}.py", line 1, in {CANARY}',
                f'  File "{public}", line {"9" * 100}, in {CANARY}',
                f'  File "{public}", line {invalid_line}, in {CANARY}',
                f'  File "{public}", line 0, in {CANARY}',
                f"AssertionError: {CANARY}",
                "Fatal Python error: Aborted",
            ]
        )
        self.env = {
            **os.environ,
            **job["env"],
            "RUNNER_TEMP": str(self.temporary),
            "GITHUB_WORKSPACE": str(ROOT),
            "CI_DIAGNOSTIC_FIXTURE": self.payload,
        }
        self.tokens = set()

    def _run(self, script):
        path = self.temporary / "step.ps1"
        path.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "$PSNativeCommandUseErrorActionPreference = $true\n" + script,
            encoding="utf-8",
        )
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(path)],
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(result.stderr, "")
        return result

    def _active_annotations(self, output, *, replayed=True):
        active, stops, resumes = [], [], []
        token = None
        for line in output.splitlines():
            if token is not None:
                if line == f"::{token}::":
                    resumes.append(token)
                    token = None
            elif line.startswith("::stop-commands::"):
                token = line.removeprefix("::stop-commands::")
                stops.append(token)
            elif line.startswith("::"):
                self.assertEqual(len(resumes), int(replayed))
                active.append(line)
        self.assertEqual(len(stops), int(replayed))
        if replayed:
            self.assertRegex(stops[0], "^[0-9a-f]{32}$")
            self.assertNotIn(stops[0], self.tokens)
            self.tokens.add(stops[0])
            self.assertIn(CANARY, output)
        self.assertEqual(resumes, stops)
        self.assertIsNone(token)
        for annotation in active:
            self.assertNotIn(CANARY, annotation)
        return active

    def _active_annotation(self, output, *, replayed=True):
        active = self._active_annotations(output, replayed=replayed)
        self.assertEqual(len(active), 1)
        return active[0]

    def test_primary_preserves_native_status_and_both_streams(self):
        for status in (0, 1, 19):
            with self.subTest(status=status):
                invocation = "& " + " ".join(
                    "'" + str(value).replace("'", "''") + "'"
                    for value in (sys.executable, self.helper, status)
                )
                result = self._run(self.primary.replace(self.command, invocation))
                self.assertEqual(result.returncode, status)
                self.assertEqual(result.stdout, "")
                self.assertIn("stderr café", self.log.read_text("utf-8"))
                if status:
                    report = self._run(self.collector)
                    self.assertEqual(report.returncode, 0)
                    self.assertEqual(self._active_annotation(report.stdout), CONTEXT)
                    saved = (self.temporary / "windows-unit-test-report.txt").read_text(
                        "utf-8"
                    )
                    self.assertIn(f"Test runner exit code: {status}\n", saved)
                    self.assertIn(CONTEXT.split("::")[-1], saved)
                    self.assertNotIn(CANARY, saved)

    def test_primary_only_replays_fixed_success_timing_notice(self):
        notice = (
            "::notice title=Unittest timing::Windows qt-catalog completed in 46 seconds"
        )
        self.env["CI_DIAGNOSTIC_FIXTURE"] = "\n".join(
            (notice, f"{notice} {CANARY}", f"::notice::{CANARY}")
        )
        invocation = "& " + " ".join(
            "'" + str(value).replace("'", "''") + "'"
            for value in (sys.executable, self.helper, 0)
        )
        result = self._run(self.primary.replace(self.command, invocation))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), notice)

    def test_each_failure_excludes_expected_logs_and_buffered_stdout(self):
        public = ROOT / "scripts/run_ci_unittests.py"
        delimiter = "=" * 70
        self.log.write_text(
            f'  File "{public}", line 3, in expected_error\n'
            "OSError: expected fixture failure\n"
            f"{delimiter}\n"
            f"FAIL: test_{CANARY} (tests.{CANARY}.Case.test_{CANARY})\n"
            f'  File "{public}", line 1, in {CANARY}\n'
            f"AssertionError: {CANARY}\n"
            f"{delimiter}\n"
            f"ERROR: test_{CANARY} (tests.{CANARY}.Case.test_{CANARY})\n"
            f'  File "{public}", line 2, in {CANARY}\n'
            f"ValueError: {CANARY}\n"
            + "-"
            * 70
            + "\nRan 2 tests in 0.1s\n\nFAILED (failures=1, errors=1)\n"
            f'  File "{public}", line 4, in buffered_output\n'
            "RuntimeError: buffered fixture output\n",
            encoding="utf-8",
        )
        report = self._run(self.collector)
        self.assertEqual(report.returncode, 0)
        self.assertEqual(
            self._active_annotations(report.stdout),
            [
                "::error title=Windows unit tests failed::"
                "scripts/run_ci_unittests.py:1; AssertionError",
                "::error title=Windows unit tests failed::"
                "scripts/run_ci_unittests.py:2; ValueError",
            ],
        )

    def test_crlf_failure_section_retains_only_actual_traceback(self):
        public = ROOT / "scripts/run_ci_unittests.py"
        self.log.write_bytes(
            (
                f"OSError: {CANARY}\r\n" + "=" * 70 + "\r\n"
                f"FAIL: test_{CANARY} (tests.{CANARY}.Case.test_{CANARY})\r\n"
                f'  File "{public}", line 1, in {CANARY}\r\n'
                f"AssertionError: {CANARY}\r\n" + "-" * 70 + "\r\n"
                "Ran 1 test in 0.1s\r\n"
            ).encode("utf-8")
        )
        report = self._run(self.collector)
        self.assertEqual(report.returncode, 0)
        self.assertEqual(
            self._active_annotation(report.stdout),
            "::error title=Windows unit tests failed::"
            "scripts/run_ci_unittests.py:1; AssertionError",
        )

    def test_terminal_state_and_validated_last_test_precede_noisy_context(self):
        method = "test_primary_preserves_native_status_and_both_streams"
        module = "tests.test_windows_ci_diagnostics"
        noise = "".join(
            f'  File "{ROOT / "scripts/run_ci_unittests.py"}", line {line}, in noise\r\n'
            for line in range(1, 150)
        )
        self.log.write_bytes(
            (
                "Running Windows unittest shard qt-catalog: 10 tests\r\n"
                "Running Windows unittest shard remainder: 4000 tests\r\n"
                f"{method} ({module}.WindowsCiDiagnosticsTest.{method}) ... \r\n"
                f"test_{CANARY} ({module}.WindowsCiDiagnosticsTest.test_{CANARY}) ... \r\n"
                + noise
                + "Windows unittest shard remainder exceeded 900 seconds\r\n"
                "Windows unittest shard remainder exited 124 after 900 seconds\r\n"
                f"Windows unittest shard {CANARY} exited 1 after 1 seconds\r\n"
            ).encode("utf-8")
        )
        report = self._run(self.collector)
        self.assertEqual(report.returncode, 0)
        annotation = self._active_annotation(report.stdout)
        self.assertTrue(
            annotation.startswith(
                "::error title=Windows unit tests failed::"
                "Running Windows unittest shard remainder: 4000 tests; "
                "Windows unittest shard remainder exceeded 900 seconds; "
                "Windows unittest shard remainder exited 124 after 900 seconds; "
                f"last-test={module}.{method}; "
            ),
            annotation,
        )
        self.assertLessEqual(
            len(annotation), 3500 + len("::error title=Windows unit tests failed::")
        )

    def test_independent_collector_preserves_private_failure_context(self):
        self.log.write_text(self.payload, encoding="utf-8")
        primary = self.primary.replace(
            self.command, "throw 'Primary unexpectedly survived'"
        )
        result = self._run("Stop-Process -Id $PID -Force\n" + primary)
        self.assertNotEqual(result.returncode, 0)
        report = self._run(self.collector)
        self.assertEqual(report.returncode, 0)
        self.assertEqual(self._active_annotation(report.stdout), CONTEXT)

    def test_missing_log_reports_only_fixed_collector_failure(self):
        report = self._run(self.collector)
        self.assertEqual(report.returncode, 1)
        self.assertRegex(
            self._active_annotation(report.stdout, replayed=False),
            "^::error title=Windows unit test diagnostics failed::"
            "stage=read-log; script line=[1-9][0-9]*; category=ObjectNotFound$",
        )

    def test_timeout_without_traceback_reports_only_fixed_shard_labels(self):
        self.log.write_text(
            "Running Windows unittest shard qt-app: 100 tests\n"
            "Running Windows unittest shard remainder: 4000 tests\n"
            "Windows unittest shard remainder exceeded 900 seconds\n"
            f"Running Windows unittest shard {CANARY}: 1 tests\n"
            f"Windows unittest shard {CANARY} exceeded 900 seconds\n"
            f"Running Windows unittest shard qt-app: {CANARY} tests\n"
            f"Windows unittest shard remainder exceeded {CANARY} seconds\n",
            encoding="utf-8",
        )
        report = self._run(self.collector)
        self.assertEqual(report.returncode, 0)
        self.assertEqual(
            self._active_annotation(report.stdout),
            "::error title=Windows unit tests failed::"
            "Running Windows unittest shard remainder: 4000 tests; "
            "Windows unittest shard remainder exceeded 900 seconds",
        )

    def test_unreadable_source_reports_only_fixed_collector_failure(self):
        self.log.write_text(self.payload, encoding="utf-8")
        prefix = (
            "function Get-Content {\n"
            "param([string]$LiteralPath, [switch]$Raw)\n"
            "if ($LiteralPath.EndsWith('.py')) { throw '" + CANARY + "' }\n"
            "Microsoft.PowerShell.Management\\Get-Content @PSBoundParameters\n"
            "}\n"
        )
        report = self._run(prefix + self.collector)
        self.assertEqual(report.returncode, 1)
        self.assertRegex(
            self._active_annotation(report.stdout),
            "^::error title=Windows unit test diagnostics failed::"
            "stage=read-source; script line=[1-9][0-9]*; category=OperationStopped$",
        )
