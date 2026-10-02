import json
import subprocess
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import dependency_audit
from scripts.dependency_audit import locked_git_commits, query_osv


class _Response(BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class DependencyAuditTests(unittest.TestCase):
    @patch("scripts.dependency_audit.query_osv")
    @patch("scripts.dependency_audit.locked_git_commits")
    @patch("scripts.dependency_audit.projects")
    @patch("scripts.dependency_audit.subprocess.run")
    def test_main_audits_projects_and_git_for_all_results(
        self,
        run: MagicMock,
        resolved_projects: MagicMock,
        git_commits: MagicMock,
        osv: MagicMock,
    ) -> None:
        root = dependency_audit.ROOT
        backend = root / "backends/pocket-tts"
        lock_paths = (root / "uv.lock", backend / "uv.lock")
        commits = ("a" * 40, "b" * 40)
        resolved_projects.return_value = (root, backend)
        git_commits.return_value = commits
        root_command = [
            "uv",
            "audit",
            "--project",
            str(root),
            "--frozen",
            "--preview-features",
            "audit-command",
            *(
                item
                for advisory in dependency_audit.IGNORES["."]
                for item in ("--ignore", advisory)
            ),
        ]
        for name, package_result, git_result in (
            (
                "package and git findings",
                [subprocess.CalledProcessError(1, "uv"), None],
                {commits[0]: ("OSV-1",)},
            ),
            ("git-only finding", [None, None], {commits[0]: ("OSV-1",)}),
            ("clean result", [None, None], {}),
        ):
            with self.subTest(name=name):
                run.reset_mock()
                git_commits.reset_mock()
                osv.reset_mock()
                run.side_effect = package_result
                osv.return_value = git_result
                with patch("builtins.print") as print_output:
                    if name == "clean result":
                        dependency_audit.main()
                        print_output.assert_called_once_with(
                            "Audited 2 locks and 2 Git revisions"
                        )
                    else:
                        with self.assertRaisesRegex(
                            SystemExit, "Dependency audit failed"
                        ) as raised:
                            dependency_audit.main()
                        if name == "package and git findings":
                            self.assertIn(
                                "Package audit failures: . (exit status 1)",
                                str(raised.exception),
                            )
                        self.assertEqual(
                            "Vulnerable Git dependencies: " + commits[0]
                            in str(raised.exception),
                            bool(git_result),
                        )
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args_list[0].args[0], root_command)
                self.assertEqual(run.call_args_list[1].args[0][3], str(backend))
                git_commits.assert_called_once_with(lock_paths)
                osv.assert_called_once_with(commits)

    def test_requires_an_exact_git_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "uv.lock"
            lock.write_text(
                'package = [{ source = { git = "https://example.test/repo?rev=main#main" } }]'
            )
            with self.assertRaisesRegex(ValueError, "not locked to a commit"):
                locked_git_commits((lock,))

    @patch("scripts.dependency_audit.urllib.request.urlopen")
    def test_reports_osv_findings_by_commit(self, urlopen: MagicMock) -> None:
        commits = ("a" * 40, "b" * 40)
        urlopen.return_value = _Response(
            json.dumps({"results": [{}, {"vulns": [{"id": "OSV-1"}]}]}).encode()
        )

        self.assertEqual(query_osv(commits), {"b" * 40: ("OSV-1",)})


if __name__ == "__main__":
    unittest.main()
