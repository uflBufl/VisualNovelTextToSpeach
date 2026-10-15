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


@unittest.skipUnless(POWERSHELL, "PowerShell is not installed")
class WindowsCiDiagnosticsTest(unittest.TestCase):
    def test_native_status_and_private_output_survive_workflow_capture(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
        job = yaml.safe_load(workflow)["jobs"]["windows-unit-tests"]
        powershell = [POWERSHELL, "-NoProfile", "-NonInteractive", "-File"]
        step = next(
            s for s in job["steps"] if s.get("name") == "Run Windows unit tests"
        )
        command = "uv run --frozen python scripts/run_ci_unittests.py discover -s tests"
        self.assertEqual(step["run"].count(command), 1)
        canary = "PRIVATE_CANARY_c31827"
        public = ROOT / "scripts/run_ci_unittests.py"
        invalid_line = len(public.read_text("utf-8").splitlines()) + 1
        tokens = set()
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            helper = temporary / "native.py"
            helper.write_text(
                "import os, sys\n"
                "print(os.environ['CI_DIAGNOSTIC_FIXTURE'], flush=True)\n"
                "status = int(sys.argv[1])\n"
                "if status == 19:\n    os._exit(status)\n"
                "sys.exit(status)\n",
                encoding="utf-8",
            )
            payload = "\n".join(
                [
                    f"::error::{canary}",
                    f"::stop-commands::{canary}",
                    f"::{canary}::",
                    f"ordinary output {canary}",
                    f"FAIL: test_{canary} (tests.{canary}.Case.test_{canary})",
                    f'  File "{public}", line 1, in {canary}',
                    f'  File "{public}", line 2 in {canary}',
                    f'  File "{temporary / canary}.py", line 1, in {canary}',
                    f'  File "{ROOT / canary}.py", line 1, in {canary}',
                    f'  File "{public}", line {"9" * 100}, in {canary}',
                    f'  File "{public}", line {invalid_line}, in {canary}',
                    f'  File "{public}", line 0, in {canary}',
                    f"AssertionError: {canary}",
                    "Fatal Python error: Aborted",
                ]
            )
            for status in (0, 1, 19):
                with self.subTest(status=status):
                    invocation = "& " + " ".join(
                        "'" + str(value).replace("'", "''") + "'"
                        for value in (sys.executable, helper, status)
                    )
                    script = temporary / "step.ps1"
                    script.write_text(
                        "$ErrorActionPreference = 'Stop'\n"
                        "$PSNativeCommandUseErrorActionPreference = $true\n"
                        + step["run"].replace(command, invocation),
                        encoding="utf-8",
                    )
                    fixture = payload if status != 19 else canary
                    result = subprocess.run(
                        [*powershell, str(script)],
                        env={
                            **os.environ,
                            **job["env"],
                            "RUNNER_TEMP": str(temporary),
                            "GITHUB_WORKSPACE": str(ROOT),
                            "CI_DIAGNOSTIC_FIXTURE": fixture,
                        },
                        capture_output=True,
                        encoding="utf-8",
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertEqual(result.stderr, "")
                    active, stops, resumes = [], [], []
                    token = None
                    for line in result.stdout.splitlines():
                        if token is not None:
                            if line == f"::{token}::":
                                resumes.append(token)
                                token = None
                        elif line.startswith("::stop-commands::"):
                            token = line.removeprefix("::stop-commands::")
                            stops.append(token)
                        elif line.startswith("::"):
                            self.assertEqual(len(resumes), 1)
                            active.append(line)
                    self.assertEqual(len(stops), 1)
                    self.assertRegex(stops[0], "^[0-9a-f]{32}$")
                    self.assertNotIn(stops[0], tokens)
                    tokens.add(stops[0])
                    self.assertEqual(resumes, stops)
                    self.assertIsNone(token)
                    self.assertIn(canary, result.stdout)
                    context = (
                        "scripts/run_ci_unittests.py:1; scripts/run_ci_unittests.py:2; "
                        "AssertionError; Aborted"
                        if status == 1
                        else "no structured failure context"
                    )
                    expected = (
                        [
                            "::error title=Windows unit tests failed::"
                            f"Runner exited with code {status}. {context}"
                        ]
                        if status
                        else []
                    )
                    self.assertEqual(active, expected)
