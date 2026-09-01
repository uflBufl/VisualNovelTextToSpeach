import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from scripts.run_ci_unittests import (
    _flatten_suite,
    _run_exact_test_file,
    _run_macos_full_discovery,
    partition_macos_test_ids,
    workflow_failure_details,
)


class CiUnitTestRunnerTest(unittest.TestCase):
    def test_repository_discovery_has_no_testcase_import_aliases(self):
        suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
        test_ids = [test.id() for test in _flatten_suite(suite)]
        self.assertEqual(len(test_ids), len(set(test_ids)))

    def test_partition_assigns_every_test_exactly_once(self):
        values = [
            "tests.test_app.TrayApplicationTest.test_start",
            "tests.test_alpha.AlphaTest.test_one",
            "tests.test_zed.ZedTest.test_two",
        ]
        values.insert(1, "tests.test_asset_ui.AssetTest.test_dialog")
        app, assets, remainder = partition_macos_test_ids(values)
        self.assertEqual(app, (values[0],))
        self.assertEqual(assets, (values[1],))
        self.assertEqual(remainder, tuple(values[2:]))
        self.assertEqual(sorted((*app, *assets, *remainder)), sorted(values))

    def test_partition_rejects_duplicates_and_missing_app_shard(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            partition_macos_test_ids(["tests.test_app.X.test_a"] * 2)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            partition_macos_test_ids(["tests.test_alpha.X.test_a"])

    def test_failure_details_keep_both_ends(self):
        value = "start" + "x" * 6_000 + "\nFAIL: test_windows\n" + "y" * 6_000 + "finish"
        details = workflow_failure_details(value)

        self.assertTrue(details.startswith("FAIL: test_windows"))
        self.assertTrue(details.endswith("finish"))
        self.assertLessEqual(len(details), 4_000)

    def test_exact_inventory_executes_each_named_test_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tests.json"
            names = [
                f"{__name__}.CiUnitTestRunnerTest."
                "test_partition_assigns_every_test_exactly_once",
                f"{__name__}.CiUnitTestRunnerTest."
                "test_partition_rejects_duplicates_and_missing_app_shard",
            ]
            path.write_text(json.dumps(names), encoding="utf-8")
            self.assertEqual(_run_exact_test_file(path), 0)

    def test_macos_shard_timeout_fails_instead_of_hanging(self):
        with (
            patch(
                "scripts.run_ci_unittests._flatten_suite",
                return_value=(Mock(id=Mock(return_value="test-id")),),
            ),
            patch(
                "scripts.run_ci_unittests.partition_macos_test_ids",
                return_value=(("app-id",), ("asset-id",), ("other-id",)),
            ),
            patch(
                "scripts.run_ci_unittests.subprocess.run",
                side_effect=subprocess.TimeoutExpired(("python",), 60),
            ),
        ):
            self.assertEqual(_run_macos_full_discovery(), 124)


if __name__ == "__main__":
    unittest.main()
