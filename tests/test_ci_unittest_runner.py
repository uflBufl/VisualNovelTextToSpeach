import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.run_ci_unittests import (
    _run_exact_test_file,
    canonical_discovered_test_ids,
    partition_macos_test_ids,
)


class CiUnitTestRunnerTest(unittest.TestCase):
    def test_canonical_inventory_collapses_only_exact_discovery_aliases(self):
        values = [
            "tests.test_alpha.AlphaTest.test_one",
            "tests.test_zed.ZedTest.test_two",
            "tests.test_alpha.AlphaTest.test_one",
        ]
        self.assertEqual(
            canonical_discovered_test_ids(values),
            tuple(values[:2]),
        )

    def test_partition_assigns_every_test_exactly_once(self):
        values = [
            "tests.test_app.TrayApplicationTest.test_start",
            "tests.test_alpha.AlphaTest.test_one",
            "tests.test_zed.ZedTest.test_two",
        ]
        app, remainder = partition_macos_test_ids(values)
        self.assertEqual(app, (values[0],))
        self.assertEqual(remainder, tuple(values[1:]))
        self.assertEqual(sorted((*app, *remainder)), sorted(values))

    def test_partition_rejects_duplicates_and_missing_app_shard(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            partition_macos_test_ids(["tests.test_app.X.test_a"] * 2)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            partition_macos_test_ids(["tests.test_alpha.X.test_a"])

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


if __name__ == "__main__":
    unittest.main()
