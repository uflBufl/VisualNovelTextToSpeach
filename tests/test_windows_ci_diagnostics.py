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

    def _active_annotation(self, output, *, replayed=True):
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
        self.assertEqual(len(active), 1)
        self.assertNotIn(CANARY, active[0])
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
