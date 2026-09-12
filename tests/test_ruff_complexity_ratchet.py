import tempfile
import unittest
from pathlib import Path

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
