import json
import tempfile
import unittest
from pathlib import Path

from scripts.check_maintainability import check_repository


class MaintainabilityRatchetTest(unittest.TestCase):
    def _baseline(self, root: Path, **changes) -> Path:
        document = {
            "schema_version": 1,
            "thresholds": {
                "module_lines": 20,
                "function_lines": 8,
                "function_complexity": 3,
            },
            "private_imports": [],
            "module_lines": {},
            "function_lines": {},
            "function_complexity": {},
            **changes,
        }
        path = root / "baseline.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_new_private_import_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vntts"
            root.mkdir()
            (root / "consumer.py").write_text(
                "from vntts.owner import _private\n", encoding="utf-8"
            )

            failures = check_repository(root, self._baseline(root.parent))

        self.assertEqual(
            failures,
            ["new cross-module private import: vntts.consumer -> vntts.owner:_private"],
        )

    def test_package_relative_private_import_is_resolved_from_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vntts"
            root.mkdir()
            (root / "__init__.py").write_text(
                "from .owner import _private\n", encoding="utf-8"
            )

            failures = check_repository(root, self._baseline(root.parent))

        self.assertEqual(
            failures,
            ["new cross-module private import: vntts -> vntts.owner:_private"],
        )

    def test_baselined_debt_may_shrink_but_not_grow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vntts"
            root.mkdir()
            source = root / "large.py"
            source.write_text(
                "\n".join("value = 1" for _ in range(21)), encoding="utf-8"
            )
            baseline = self._baseline(
                root.parent,
                module_lines={"vntts.large": 21},
            )
            self.assertEqual(check_repository(root, baseline), [])
            source.write_text(
                "\n".join("value = 1" for _ in range(22)), encoding="utf-8"
            )

            failures = check_repository(root, baseline)

        self.assertEqual(
            failures,
            ["grown module_lines debt: vntts.large is 22 (baseline 21)"],
        )
