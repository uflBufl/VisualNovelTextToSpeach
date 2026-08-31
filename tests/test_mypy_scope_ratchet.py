import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_mypy_scope import check_mypy_scope


class MypyScopeRatchetTest(unittest.TestCase):
    def _write(self, root, name, value):
        path = root / name
        path.write_text(value, encoding="utf-8")
        return path

    def test_configured_scope_must_be_a_superset_of_versioned_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = self._write(
                root,
                "baseline.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "minimum_files": ["vntts/a.py", "vntts/b.py"],
                    }
                ),
            )
            config = self._write(
                root,
                "pyproject.toml",
                '[tool.mypy]\nfiles = ["vntts/a.py", "vntts/extra.py"]\n',
            )

            failures = check_mypy_scope(config, baseline)

        self.assertEqual(
            failures,
            ["mypy scope removed required file: vntts/b.py"],
        )

    def test_scope_may_expand_without_baseline_churn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = self._write(
                root,
                "baseline.json",
                json.dumps(
                    {"schema_version": 1, "minimum_files": ["vntts/a.py"]}
                ),
            )
            config = self._write(
                root,
                "pyproject.toml",
                '[tool.mypy]\nfiles = ["vntts/a.py", "vntts/b.py"]\n',
            )

            self.assertEqual(check_mypy_scope(config, baseline), [])

    def test_malformed_or_duplicate_baseline_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._write(
                root,
                "pyproject.toml",
                '[tool.mypy]\nfiles = ["vntts/a.py"]\n',
            )
            baseline = self._write(
                root,
                "baseline.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "minimum_files": ["vntts/a.py", "vntts/a.py"],
                    }
                ),
            )

            failures = check_mypy_scope(config, baseline)

        self.assertEqual(failures, ["Mypy scope baseline inventory is malformed"])
