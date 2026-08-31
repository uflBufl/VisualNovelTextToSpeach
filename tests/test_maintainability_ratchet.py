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

    def test_module_alias_private_access_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vntts"
            root.mkdir()
            (root / "consumer.py").write_text(
                "import vntts.owner as owner\nowner._private()\n",
                encoding="utf-8",
            )

            failures = check_repository(root, self._baseline(root.parent))

        self.assertEqual(
            failures,
            ["new cross-module private import: vntts.consumer -> vntts.owner:_private"],
        )

    def test_except_star_branches_count_toward_complexity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vntts"
            root.mkdir()
            (root / "complex.py").write_text(
                "def run():\n"
                "    try:\n"
                "        work()\n"
                "    except* ValueError:\n"
                "        recover()\n"
                "    except* TypeError:\n"
                "        recover()\n"
                "    else:\n"
                "        finish()\n",
                encoding="utf-8",
            )

            failures = check_repository(
                root,
                self._baseline(
                    root.parent,
                    thresholds={
                        "module_lines": 20,
                        "function_lines": 20,
                        "function_complexity": 3,
                    },
                ),
            )

        self.assertEqual(
            failures,
            ["new function_complexity debt: vntts.complex.run is 4 (threshold 3)"],
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

    def test_baseline_ceiling_must_follow_shrinking_or_removed_debt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vntts"
            root.mkdir()
            source = root / "large.py"
            baseline = self._baseline(
                root.parent,
                module_lines={"vntts.large": 22},
            )
            source.write_text(
                "\n".join("value = 1" for _ in range(21)), encoding="utf-8"
            )
            shrunk = check_repository(root, baseline)
            source.write_text("value = 1\n", encoding="utf-8")
            removed = check_repository(root, baseline)

        self.assertEqual(
            shrunk,
            ["stale module_lines ceiling: vntts.large is 21 (baseline 22)"],
        )
        self.assertEqual(
            removed,
            [
                "stale module_lines ceiling: vntts.large no longer exceeds "
                "threshold 20 (baseline 22)"
            ],
        )
