import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.dependency_audit import locked_git_commits, query_osv


class _Response(BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class DependencyAuditTests(unittest.TestCase):
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
