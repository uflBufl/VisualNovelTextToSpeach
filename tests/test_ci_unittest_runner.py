import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from scripts.run_ci_unittests import (
    _flatten_suite,
    _run_exact_test_file,
    _run_sharded_full_discovery,
    main,
    partition_ui_test_ids,
    workflow_failure_details,
    workflow_failure_sections,
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
        values.insert(2, "tests.test_ocr_review.OCRReviewDialogTest.test_dialog")
        app, assets, ocr, remainder = partition_ui_test_ids(values)
        self.assertEqual(app, (values[0],))
        self.assertEqual(assets, (values[1],))
        self.assertEqual(ocr, (values[2],))
        self.assertEqual(remainder, tuple(values[3:]))
        self.assertEqual(sorted((*app, *assets, *ocr, *remainder)), sorted(values))

    def test_partition_rejects_duplicates_and_missing_app_shard(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            partition_ui_test_ids(["tests.test_app.X.test_a"] * 2)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            partition_ui_test_ids(["tests.test_alpha.X.test_a"])

    def test_failure_details_keep_both_ends(self):
        value = (
            "FAIL: test_windows\nTraceback\nAssertionError: exact failure\n"
            + "x" * 12_000
            + "finish"
        )
        details = workflow_failure_details(value)

        self.assertTrue(details.startswith("FAIL: test_windows"))
        self.assertIn("AssertionError: exact failure", details)
        self.assertTrue(details.endswith("finish"))
        self.assertLessEqual(len(details), 4_000)

    def test_failure_sections_keep_each_unittest_traceback(self):
        divider = "=" * 70
        output = (
            f"dots\n{divider}\nFAIL: test_one\ntrace one\n"
            f"{divider}\nERROR: test_two\ntrace two\n{divider}\nsummary"
        )

        self.assertEqual(
            workflow_failure_sections(output),
            ("FAIL: test_one\ntrace one", "ERROR: test_two\ntrace two"),
        )

    def test_failure_section_excludes_buffered_stdout_after_summary(self):
        divider = "=" * 70
        output = (
            f"{divider}\nFAIL: test_one\ntrace one\n{divider}\n"
            "Ran 1 test\nFAILED\napplication output"
        )

        self.assertEqual(
            workflow_failure_sections(output), ("FAIL: test_one\ntrace one",)
        )

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
        run = Mock(side_effect=subprocess.TimeoutExpired(("python",), 60))
        with (
            patch(
                "scripts.run_ci_unittests._flatten_suite",
                return_value=(Mock(id=Mock(return_value="test-id")),),
            ),
            patch(
                "scripts.run_ci_unittests.partition_ui_test_ids",
                return_value=(
                    ("app-id",),
                    ("asset-id",),
                    ("ocr-id",),
                    ("other-id",),
                ),
            ),
            patch(
                "scripts.run_ci_unittests.subprocess.run",
                run,
            ),
        ):
            self.assertEqual(_run_sharded_full_discovery("Darwin"), 124)
        self.assertEqual(
            run.call_args.args[0][-3:-1],
            ["--shard", "qt-app"],
        )

    def test_shard_captures_stacks_before_deadline_and_cancels_timer(self):
        for result in (0, RuntimeError("test loading failed")):
            with (
                self.subTest(result=result),
                patch(
                    "scripts.run_ci_unittests.platform.system", return_value="Darwin"
                ),
                patch("scripts.run_ci_unittests.faulthandler") as faults,
                patch("scripts.run_ci_unittests._run_exact_test_file") as run,
            ):
                if isinstance(result, Exception):
                    run.side_effect = result
                    with self.assertRaisesRegex(RuntimeError, "test loading failed"):
                        main(["--shard", "qt-app", "inventory.json"])
                else:
                    run.return_value = result
                    self.assertEqual(main(["--shard", "qt-app", "inventory.json"]), 0)
                run.assert_called_once_with("inventory.json")
                faults.enable.assert_called_once_with()
                faults.dump_traceback_later.assert_called_once_with(144)
                faults.cancel_dump_traceback_later.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
