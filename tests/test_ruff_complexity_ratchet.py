import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.check_ruff_complexity import (
    baseline_allowances,
    check_findings,
    finding_identity,
    finding_metric,
)


class RuffComplexityRatchetTest(unittest.TestCase):
    def _write(self, root, name, value):
        path = root / name
        path.write_text(value, encoding="utf-8")
        return path

    @staticmethod
    def _finding(code, path, row, metric=None):
        threshold = {"C901": 10, "PLR0912": 12, "PLR0915": 50}[code]
        metric = threshold + 2 if metric is None else metric
        return {
            "code": code,
            "filename": str(path),
            "location": {"row": row},
            "message": f"Complexity finding ({metric} > {threshold})",
        }

    def test_existing_owner_cannot_grow_while_finding_count_stays_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._write(root, "module.py", "def accepted():\n    pass\n")
            for code, limit in (("C901", 10), ("PLR0912", 12), ("PLR0915", 50)):
                with self.subTest(code=code):
                    metric = limit + 2
                    baseline = {
                        "schema_version": 2,
                        "findings": [
                            {
                                "code": code,
                                "path": "module.py",
                                "scope": "accepted",
                                "count": 1,
                                "maximum": metric,
                            }
                        ],
                    }
                    finding = self._finding(code, source, 1)
                    finding["message"] = f"Existing owner ({metric + 1} > {limit})"
                    self.assertTrue(check_findings(root, baseline, [finding]))

    def test_reduced_measurement_requires_tightening_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._write(root, "module.py", "def accepted():\n    pass\n")
            baseline = {
                "schema_version": 2,
                "findings": [
                    {
                        "code": "C901",
                        "path": "module.py",
                        "scope": "accepted",
                        "count": 1,
                        "maximum": 13,
                    }
                ],
            }
            finding = self._finding("C901", source, 1, metric=12)
            self.assertEqual(
                check_findings(root, baseline, [finding]),
                [
                    "stale Ruff complexity metric allowance: C901 module.py accepted (13 > 12)"
                ],
            )
            baseline["findings"][0]["maximum"] = 12
            self.assertEqual(check_findings(root, baseline, [finding]), [])

    def test_rejects_malformed_metric_budgets_and_diagnostics(self):
        for value in (True, 12.0, 0, -1, None):
            with self.subTest(maximum=value):
                baseline = {
                    "schema_version": 2,
                    "findings": [
                        {
                            "code": "C901",
                            "path": "module.py",
                            "scope": "accepted",
                            "count": 1,
                            "maximum": value,
                        }
                    ],
                }
                with self.assertRaises(ValueError):
                    baseline_allowances(baseline)
        for message in (None, "changed diagnostic", "(10 > 10)", "(9 > 10)"):
            with self.subTest(message=message):
                with self.assertRaises(ValueError):
                    finding_metric({"code": "C901", "message": message})
        for version in (1, True, 2.0):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    baseline_allowances({"schema_version": version, "findings": []})

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
                "schema_version": 2,
                "findings": [
                    {
                        "code": "C901",
                        "path": "module.py",
                        "scope": "removed",
                        "count": 1,
                        "maximum": 12,
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
            [
                "new Ruff complexity finding: C901 module.py introduced (1 > 0)",
                "stale Ruff complexity allowance: C901 module.py removed (1 > 0)",
            ],
        )

    def test_extra_finding_at_an_existing_identity_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._write(root, "module.py", "def accepted():\n    pass\n")
            baseline = {
                "schema_version": 2,
                "findings": [
                    {
                        "code": "C901",
                        "path": "module.py",
                        "scope": "accepted",
                        "count": 1,
                        "maximum": 12,
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
                "schema_version": 2,
                "findings": [
                    {
                        "code": "C901",
                        "path": "module.py",
                        "scope": "accepted",
                        "count": 1,
                        "maximum": 12,
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

    def test_removed_finding_requires_retiring_baseline_allowance(self):
        baseline = {
            "schema_version": 2,
            "findings": [
                {
                    "code": "PLR0915",
                    "path": "module.py",
                    "scope": "removed",
                    "count": 1,
                    "maximum": 12,
                }
            ],
        }
        self.assertEqual(
            check_findings(Path.cwd(), baseline, []),
            ["stale Ruff complexity allowance: PLR0915 module.py removed (1 > 0)"],
        )


if __name__ == "__main__":
    unittest.main()
