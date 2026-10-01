import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION = PROJECT_ROOT / "scripts/run-windows-release-test.ps1"
POWERSHELL = shutil.which("pwsh")


@unittest.skipUnless(POWERSHELL, "PowerShell is not installed")
class WindowsReleaseQualificationTest(unittest.TestCase):
    def _powershell(self, script: str, *arguments: str) -> str:
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-CommandWithArgs",
                script,
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()

    def _ast_extent(
        self,
        selector: str,
        extent_expression: str = "$node.Extent.Text",
    ) -> str:
        script = f"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $args[0], [ref]$tokens, [ref]$errors
)
if ($errors.Count -ne 0) {{ throw ($errors | ForEach-Object Message) }}
$node = $ast.FindAll({{ param($n) {selector} }}, $true) |
    Select-Object -First 1
if ($null -eq $node) {{ throw "AST node was not found" }}
[Console]::Write({extent_expression})
"""
        return self._powershell(script, str(QUALIFICATION))

    def test_default_and_intermediate_reports_are_per_run(self):
        default_block = self._ast_extent(
            "$n -is [System.Management.Automation.Language.IfStatementAst] "
            "-and $n.Extent.Text.Contains('$EvidenceReport')"
        )
        smoke_assignment = self._ast_extent(
            "$n -is [System.Management.Automation.Language.AssignmentStatementAst] "
            "-and $n.Left.Extent.Text -eq '$SmokeEvidenceReport'"
        )

        def default_path(test_id: str) -> str:
            return self._powershell(
                "$ProjectRoot = $args[1]; $TestId = $args[0]; "
                "$EvidenceReport = $null; "
                f"{default_block}; $EvidenceReport",
                test_id,
                temporary_directory,
            )

        def smoke_path(test_id: str) -> str:
            return self._powershell(
                "$EvidenceDirectory = $args[1]; $TestId = $args[0]; "
                f"{smoke_assignment}; $SmokeEvidenceReport",
                test_id,
                temporary_directory,
            )

        with TemporaryDirectory() as temporary_directory:
            first = default_path("run-a")
            second = default_path("run-b")
            self.assertNotEqual(first, second)
            self.assertTrue(first.endswith("release-evidence-run-a.json"))
            self.assertTrue(second.endswith("release-evidence-run-b.json"))
            self.assertIn("run-a", first)
            self.assertIn("run-b", second)

            self.assertEqual(
                self._powershell(
                    "$ProjectRoot = $args[1]; $TestId = $args[0]; "
                    "$EvidenceReport = $null; "
                    f"{default_block}; "
                    "New-Item -ItemType Directory -Force (Split-Path $EvidenceReport) | Out-Null; "
                    "Set-Content $EvidenceReport first; "
                    "$first = $EvidenceReport; $TestId = 'run-b'; $EvidenceReport = $null; "
                    f"{default_block}; "
                    "Set-Content $EvidenceReport second; "
                    '"$first`n$EvidenceReport`n$(Get-Content $first -Raw)"',
                    "run-a",
                    temporary_directory,
                ).splitlines()[-1],
                "first",
            )
            explicit = self._powershell(
                "$ProjectRoot = $args[1]; $TestId = $args[0]; "
                "$EvidenceReport = Join-Path $args[1] 'explicit.json'; "
                f"{default_block}; $EvidenceReport",
                "run-a",
                temporary_directory,
            )
            self.assertEqual(explicit, str(Path(temporary_directory) / "explicit.json"))
            self.assertNotEqual(smoke_path("run-a"), smoke_path("run-b"))
            self.assertIn("installed-smoke-evidence-run-a.json", smoke_path("run-a"))

    def test_workflow_uploads_only_primary_evidence(self):
        workflow = (
            PROJECT_ROOT / ".github/workflows/windows-release-test.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("path: build/windows/release-evidence-*.json", workflow)
        self.assertNotIn("path: build/windows/release-evidence.json", workflow)


if __name__ == "__main__":
    unittest.main()
