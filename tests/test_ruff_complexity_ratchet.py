import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_ruff_complexity import check_findings, finding_identity


class RuffComplexityRatchetTest(unittest.TestCase):
    def _write(self, root, name, value):
        path = root / name
        path.write_text(value, encoding="utf-8")
        return path

    @staticmethod
    def _finding(code, path, row):
        return {"code": code, "filename": str(path), "location": {"row": row}}

    def test_scope_identity_survives_line_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._write(root, "module.py", "\n\n\ndef accepted():\n    pass\n")

            before = finding_identity(root, self._finding("C901", source, 4))
            source.write_text("\n\n\n\n\ndef accepted():\n    pass\n", encoding="utf-8")
            after = finding_identity(root, self._finding("C901", source, 6))

        self.assertEqual(before, after)

    def test_new_location_fails_even_when_a_baseline_finding_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._write(root, "module.py", "def introduced():\n    pass\n")
            baseline = {
                "schema_version": 1,
                "findings": [
                    {
                        "code": "C901",
                        "path": "module.py",
                        "scope": "removed",
                        "count": 1,
                    }
                ],
            }

            failures = check_findings(
                root,
                baseline,
                [self._finding("C901", source, 1)],
            )

        self.assertEqual(
            failures,
            ["new Ruff complexity finding: C901 module.py introduced (1 > 0)"],
        )

    def test_extra_finding_at_an_existing_identity_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._write(root, "module.py", "def accepted():\n    pass\n")
            baseline = {
                "schema_version": 1,
                "findings": [
                    {
                        "code": "C901",
                        "path": "module.py",
                        "scope": "accepted",
                        "count": 1,
                    }
                ],
            }

            failures = check_findings(
                root,
                baseline,
                [self._finding("C901", source, 1)] * 2,
            )

        self.assertEqual(
            failures,
            ["new Ruff complexity finding: C901 module.py accepted (2 > 1)"],
        )

    def test_reuses_source_read_and_ast_parse_for_duplicate_findings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._write(root, "module.py", "def accepted():\n    pass\n")
            baseline = {
                "schema_version": 1,
                "findings": [
                    {
                        "code": "C901",
                        "path": "module.py",
                        "scope": "accepted",
                        "count": 1,
                    }
                ],
            }
            original_read_text = Path.read_text
            original_parse = ast.parse
            reads = 0
            parses = 0

            def read_text(path, *args, **kwargs):
                nonlocal reads
                reads += 1
                return original_read_text(path, *args, **kwargs)

            def parse(*args, **kwargs):
                nonlocal parses
                parses += 1
                return original_parse(*args, **kwargs)

            with (
                patch("scripts.check_ruff_complexity.Path.read_text", read_text),
                patch("scripts.check_ruff_complexity.ast.parse", parse),
            ):
                check_findings(root, baseline, [self._finding("C901", source, 1)] * 2)

        self.assertEqual(reads, 1)
        self.assertEqual(parses, 1)

    def test_removing_baseline_findings_is_allowed(self):
        baseline = {
            "schema_version": 1,
            "findings": [
                {
                    "code": "PLR0915",
                    "path": "module.py",
                    "scope": "removed",
                    "count": 1,
                }
            ],
        }
        self.assertEqual(check_findings(Path.cwd(), baseline, []), [])


if __name__ == "__main__":
    unittest.main()
