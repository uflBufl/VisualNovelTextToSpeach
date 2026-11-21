import importlib
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from unittest.mock import Mock, patch

from scripts.run_ci_unittests import (
    _flatten_suite,
    _isolate_module,
    _report_test_failures,
    _run_exact_test_file,
    _run_shard,
    _run_sharded_full_discovery,
    main,
    partition_ui_test_ids,
    workflow_failure_details,
    workflow_failure_sections,
)


class CiUnitTestRunnerTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": "", "VNTTS_TEST_REPORT": ""})
        )

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

    def test_catalog_and_pregeneration_shards_preserve_every_exact_test_once(self):
        values = (
            "tests.test_settings.SettingsTest.test_value",
            "tests.test_self_service_pregeneration.JourneyTest.test_first",
            "tests.test_self_service_pregeneration.JourneyTest.test_last",
            "tests.test_ui_catalog.CatalogTest.test_render",
            "tests.test_ui_catalog_schema.SchemaTest.test_field",
        )
        prep, remaining = _isolate_module(
            values, "tests.test_self_service_pregeneration"
        )
        catalog, remaining = _isolate_module(remaining, "tests.test_ui_catalog")
        self.assertEqual(prep, values[1:3])
        self.assertEqual(catalog, values[3:4])
        self.assertEqual(remaining, (values[0], values[4]))
        self.assertCountEqual((*prep, *catalog, *remaining), values)

    def test_windows_remainder_keeps_modules_together_and_runs_both_after_failure(self):
        grouped = (
            "tests.test_alpha.Alpha.test_a",
            "tests.test_beta.Beta.test_b",
            "tests.test_beta.Beta.test_c",
            "tests.test_zed.Zed.test_d",
        )
        inventory = [
            "tests.test_app.App.test_ui",
            "tests.test_asset_ui.Assets.test_ui",
            "tests.test_ocr_review.Review.test_ui",
            *grouped,
        ]
        ran = {}

        def run(_system, shard, path):
            ran[shard] = json.loads(path.read_text())
            return 1 if shard == "remainder-1" else 0

        with (
            patch.dict(os.environ, {"GITHUB_ACTIONS": ""}),
            patch.object(
                unittest.defaultTestLoader,
                "discover",
                return_value=[Mock(id=Mock(return_value=name)) for name in inventory],
            ),
            patch("scripts.run_ci_unittests._run_shard", side_effect=run),
        ):
            self.assertEqual(_run_sharded_full_discovery("Windows"), 1)
        self.assertEqual(ran["remainder-1"], list(grouped[:1]))
        self.assertEqual(ran["remainder"], list(grouped[1:]))

    def test_selected_pregeneration_runs_only_its_isolated_shard(self):
        test_ids = (
            "tests.test_app.AppTest.test_ui",
            "tests.test_asset_ui.AssetTest.test_ui",
            "tests.test_ocr_review.OcrTest.test_ui",
            "tests.test_self_service_pregeneration.JourneyTest.test_ui",
            "tests.test_settings.SettingsTest.test_value",
        )
        for system in ("Darwin", "Windows"):
            with (
                self.subTest(system=system),
                patch.object(
                    unittest.defaultTestLoader,
                    "discover",
                    return_value=(
                        Mock(id=Mock(return_value=value)) for value in test_ids
                    ),
                ),
                patch(
                    "scripts.run_ci_unittests.run_process",
                    return_value=Mock(returncode=0),
                ) as run,
            ):
                self.assertEqual(
                    _run_sharded_full_discovery(
                        system, ["tests.test_self_service_pregeneration"]
                    ),
                    0,
                )
                run.assert_called_once()
                self.assertEqual(
                    run.call_args.args[0][-3:-1], ["--shard", "qt-pregeneration"]
                )

    def test_partition_rejects_duplicates_and_missing_app_shard(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            partition_ui_test_ids(["tests.test_app.X.test_a"] * 2)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            partition_ui_test_ids(["tests.test_alpha.X.test_a"])

    def test_failure_details_keep_both_ends(self):
        value = (
            "FAIL: test_windows\nTraceback\nAssertionError: exact failure\n"
            + "x" * 12_000
            + "\ntest_last (tests.test_example.Case.test_last) ... "
            + "finish"
        )
        details = workflow_failure_details(value)

        self.assertTrue(details.startswith("FAIL: test_windows"))
        self.assertIn("AssertionError: exact failure", details)
        self.assertIn("Last test: test_last", details)
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

    def test_exact_inventory_encoding_error_returns_diagnostic_status(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tests.json"
            path.write_bytes(b"\xff")
            output = StringIO()
            with redirect_stderr(output):
                result = _run_exact_test_file(path)
        self.assertEqual(result, 2)
        self.assertIn("Unable to load exact test inventory", output.getvalue())

    def test_duplicate_exact_inventory_is_rejected_before_loading_or_running(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tests.json"
            name = f"{__name__}.CiUnitTestRunnerTest.test_partition_assigns_every_test_exactly_once"
            path.write_text(json.dumps([name, name]), encoding="utf-8")
            output = StringIO()
            with (
                patch("unittest.TestLoader.loadTestsFromNames") as load,
                patch("scripts.run_ci_unittests.unittest.TextTestRunner") as runner,
                redirect_stderr(output),
            ):
                self.assertEqual(main(["--exact-test-ids-file", str(path)]), 2)
            load.assert_not_called()
            runner.assert_not_called()
        self.assertIn("Exact test inventory is malformed", output.getvalue())

    def test_discovery_rejects_an_executed_suite_with_empty_slots(self):
        suite = unittest.TestSuite()
        suite._tests = [None]
        with self.assertRaisesRegex(ValueError, "empty test slot"):
            tuple(_flatten_suite(suite))

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
            with patch("scripts.run_ci_unittests._report_test_failures") as report:
                self.assertEqual(_run_exact_test_file(path), 0)
            report.assert_not_called()
            path.write_text(
                json.dumps([names[0], names[1].rsplit(".", 1)[0]]), encoding="utf-8"
            )
            with patch("unittest.TextTestRunner.run") as run:
                self.assertEqual(_run_exact_test_file(path), 2)
                run.assert_not_called()

    def test_failed_inventory_writes_only_safe_ids_and_locations_to_both_reports(self):
        canary = "PRIVATE_CANARY_ci_report"
        method = "test_partition_assigns_every_test_exactly_once"
        case_class = importlib.import_module(
            "tests.test_ci_unittest_runner"
        ).CiUnitTestRunnerTest
        test_id = f"{case_class.__module__}.CiUnitTestRunnerTest.{method}"

        def fail(case):
            for _ in range(2):
                with case.subTest(private=canary):
                    raise ValueError(canary)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            inventory.write_text(json.dumps([test_id]), encoding="utf-8")
            summary, report = root / "summary.md", root / "safe-report.txt"
            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_ACTIONS": "1",
                        "GITHUB_STEP_SUMMARY": str(summary),
                        "VNTTS_TEST_REPORT": str(report),
                    },
                ),
                patch.object(case_class, "setUp", lambda _case: None),
                patch.object(case_class, method, fail),
                redirect_stderr(StringIO()),
            ):
                self.assertEqual(_run_exact_test_file(inventory), 1)
            safe = report.read_text("utf-8")
            self.assertEqual(summary.read_text("utf-8"), safe)
            self.assertIn(f"`{test_id}` ValueError", safe)
            self.assertEqual(safe.count(f"`{test_id}`"), 1)
            self.assertRegex(safe, r"`tests/test_ci_unittest_runner\.py:[1-9][0-9]*`")
            self.assertNotIn(canary, safe)
            self.assertNotIn("private", safe)
            self.assertNotIn(str(root), safe)

    def test_safe_failure_report_exists_before_the_next_case_finishes(self):
        case_class = importlib.import_module(
            "tests.test_ci_unittest_runner"
        ).CiUnitTestRunnerTest
        first = "test_partition_assigns_every_test_exactly_once"
        following = "test_partition_rejects_duplicates_and_missing_app_shard"
        test_id = f"{case_class.__module__}.CiUnitTestRunnerTest.{first}"
        next_id = f"{case_class.__module__}.CiUnitTestRunnerTest.{following}"
        canary = "PRIVATE_CANARY_immediate_report"
        for outcome in ("failure", "error", "subtest", "unexpected-success"):
            with self.subTest(outcome=outcome), TemporaryDirectory() as directory:
                root = Path(directory)
                inventory, artifact = root / "inventory.json", root / "safe.txt"
                inventory.write_text(json.dumps([test_id, next_id]), encoding="utf-8")

                def fail(case):
                    if outcome == "failure":
                        case.fail(canary)
                    elif outcome == "error":
                        raise ValueError(canary)
                    elif outcome == "subtest":
                        with case.subTest(private=canary):
                            case.fail(canary)

                if outcome == "unexpected-success":
                    fail = unittest.expectedFailure(fail)

                def interrupt(_case):
                    safe = artifact.read_text("utf-8")
                    self.assertEqual(safe.count(f"`{test_id}`"), 1)
                    self.assertRegex(
                        safe, r"`tests/test_ci_unittest_runner\.py:[1-9][0-9]*`"
                    )
                    self.assertNotIn(canary, safe)
                    self.assertNotIn(str(root), safe)
                    raise KeyboardInterrupt

                with (
                    patch.dict(
                        os.environ,
                        {"GITHUB_ACTIONS": "1", "VNTTS_TEST_REPORT": str(artifact)},
                    ),
                    patch.object(case_class, "setUp", lambda _case: None),
                    patch.object(case_class, first, fail),
                    patch.object(case_class, following, interrupt),
                    redirect_stderr(StringIO()),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    _run_exact_test_file(inventory)

    def test_safe_report_rejects_unknown_test_objects_and_preserves_failure_status(
        self,
    ):
        method = "test_partition_assigns_every_test_exactly_once"
        case_class = importlib.import_module(
            "tests.test_ci_unittest_runner"
        ).CiUnitTestRunnerTest
        case = case_class(method)
        result = unittest.TestResult()
        result.failures.append(
            (Mock(id=Mock(return_value="PRIVATE_CANARY")), "PRIVATE_CANARY")
        )
        with TemporaryDirectory() as directory:
            report = Path(directory) / "report.txt"
            with patch.dict(
                os.environ,
                {
                    "GITHUB_ACTIONS": "1",
                    "GITHUB_STEP_SUMMARY": directory,
                    "VNTTS_TEST_REPORT": str(report),
                },
            ):
                _report_test_failures(result, (case.id(),))
            self.assertEqual(
                report.read_text("utf-8"),
                "- Unit tests failed; no source-validated test ID available.\n",
            )
            inventory = Path(directory) / "inventory.json"
            inventory.write_text(json.dumps([case.id()]), encoding="utf-8")

            def fail(case):
                raise AssertionError("PRIVATE_CANARY")

            with (
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_ACTIONS": "1",
                        "GITHUB_STEP_SUMMARY": directory,
                        "VNTTS_TEST_REPORT": directory,
                    },
                ),
                patch.object(case_class, "setUp", lambda _case: None),
                patch.object(case_class, method, fail),
                redirect_stderr(StringIO()),
            ):
                self.assertEqual(_run_exact_test_file(inventory), 1)

    def test_shard_spawn_failure_survives_transcript_and_inventory_cleanup(self):
        primary = OSError("shard launch failed")
        original_cleanup = TemporaryDirectory.cleanup
        transcripts = []

        def transcript(*args, **kwargs):
            value = TemporaryFile(*args, **kwargs)
            transcripts.append(value)
            original_close = value.close

            def close_then_fail():
                original_close()
                raise KeyboardInterrupt("transcript cleanup failed")

            value.close = close_then_fail
            return value

        def remove_then_fail(directory):
            original_cleanup(directory)
            if Path(directory.name).name.startswith("vntts-unittest-shards-"):
                raise OSError("inventory cleanup failed")

        with (
            patch.object(
                unittest.defaultTestLoader,
                "discover",
                return_value=(Mock(id=Mock(return_value="test-id")),),
            ),
            patch(
                "scripts.run_ci_unittests.partition_ui_test_ids",
                return_value=(("app-id",), (), (), ()),
            ),
            patch("scripts.run_ci_unittests.run_process", side_effect=primary),
            patch(
                "scripts.run_ci_unittests.tempfile.TemporaryFile",
                side_effect=transcript,
            ),
            patch.object(TemporaryDirectory, "cleanup", new=remove_then_fail),
            self.assertRaises(OSError) as caught,
        ):
            _run_sharded_full_discovery("Darwin")
        self.assertIs(caught.exception, primary)
        self.assertTrue(all(value.closed for value in transcripts))
        self.assertEqual(
            primary.__notes__,
            [
                "Unittest transcript cleanup failed: transcript cleanup failed",
                "Temporary directory cleanup failed: inventory cleanup failed",
            ],
        )

    def test_macos_shard_timeout_fails_instead_of_hanging(self):
        run = Mock(side_effect=subprocess.TimeoutExpired(("python",), 60))
        with (
            patch.object(
                unittest.defaultTestLoader,
                "discover",
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
                "scripts.run_ci_unittests.run_process",
                run,
            ),
        ):
            self.assertEqual(_run_sharded_full_discovery("Darwin"), 124)
        self.assertEqual(
            run.call_args.args[0][-3:-1],
            ["--shard", "qt-app"],
        )

    def test_non_utf8_shard_output_preserves_failure_status_and_annotation(self):
        for timed_out, status, outcome in (
            (False, 1, "failed"),
            (True, 124, "timed out"),
        ):
            with self.subTest(outcome=outcome):
                output = StringIO()
                errors = StringIO()

                def run(command, *, stdout, **_kwargs):
                    os.write(stdout.fileno(), b"FAIL: test_cp1252\ncaf\xe9\n")
                    if timed_out:
                        raise subprocess.TimeoutExpired(command, 60)
                    return subprocess.CompletedProcess(command, 1)

                with (
                    patch("scripts.run_ci_unittests.run_process", side_effect=run),
                    patch.dict(os.environ, {"GITHUB_ACTIONS": "1"}),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    self.assertEqual(
                        _run_shard("Windows", "qt-assets", Path("inventory.json")),
                        status,
                    )
                self.assertEqual(output.getvalue(), "FAIL: test_cp1252\ncaf\ufffd\n")
                self.assertIn(
                    f"::error title=Windows qt-assets tests {outcome}::",
                    errors.getvalue(),
                )
                self.assertIn("caf\ufffd", errors.getvalue())

    def test_local_selection_runs_only_the_requested_inventory(self):
        module = "tests.test_ci_unittest_runner"
        expected = [
            f"{module}.CiUnitTestRunnerTest.{name}"
            for name in unittest.TestLoader().getTestCaseNames(CiUnitTestRunnerTest)
        ]
        inventories = []

        def run(system, shard, inventory):
            inventories.append((shard, json.loads(inventory.read_text())))
            return 0

        with (
            patch("scripts.run_ci_unittests.platform.system", return_value="Darwin"),
            patch.object(unittest.TestLoader, "discover", side_effect=AssertionError),
            patch("scripts.run_ci_unittests._run_shard", side_effect=run),
        ):
            self.assertEqual(main(["--local-selected", module, module]), 0)
            self.assertEqual(inventories, [("remainder", expected)])

            with redirect_stderr(StringIO()) as errors:
                self.assertEqual(
                    main(["--local-selected", module, "tests.test_typo_missing"]), 2
                )
            self.assertIn("tests.test_typo_missing", errors.getvalue())
            self.assertEqual(inventories, [("remainder", expected)])

    def test_selected_modules_are_forwarded_once_in_first_seen_order(self):
        selected = ["tests.test_alpha", "tests.test_beta"]
        for system in ("Darwin", "Windows", "Linux"):
            with (
                self.subTest(system=system),
                patch("scripts.run_ci_unittests.platform.system", return_value=system),
                patch(
                    "scripts.run_ci_unittests._run_sharded_full_discovery",
                    return_value=0,
                ) as sharded,
                patch(
                    "scripts.run_ci_unittests.run_process",
                    return_value=Mock(returncode=0, stdout=""),
                ) as run,
            ):
                self.assertEqual(main(["--selected", *selected, selected[0]]), 0)
                if system == "Linux":
                    sharded.assert_not_called()
                    run.assert_called_once_with(
                        [sys.executable, "-u", "-m", "unittest", *selected],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                else:
                    sharded.assert_called_once_with(system, selected)
                    run.assert_not_called()

    def test_selected_modules_reject_unknown_names_before_running(self):
        test_ids = (
            "tests.test_app.AppTest.test_ui",
            "tests.test_asset_ui.AssetTest.test_ui",
            "tests.test_ocr_review.OcrTest.test_ui",
            "tests.test_settings.SettingsTest.test_value",
        )
        for system in ("Darwin", "Windows"):
            with (
                self.subTest(system=system),
                patch.object(
                    unittest.defaultTestLoader,
                    "discover",
                    return_value=(
                        Mock(id=Mock(return_value=value)) for value in test_ids
                    ),
                ),
                patch(
                    "scripts.run_ci_unittests.run_process",
                    return_value=Mock(returncode=0),
                ) as run,
            ):
                self.assertEqual(
                    _run_sharded_full_discovery(
                        system, ["tests.test_settings", "tests.test_typo_missing"]
                    ),
                    2,
                )
                run.assert_not_called()

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

    def test_cli_shard_watchdog_covers_interpreter_worker_shutdown(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shutdown_probe.py").write_text(
                "import faulthandler, threading, unittest\n"
                "def blocked_worker():\n    threading.Event().wait()\n"
                "class ShutdownProbe(unittest.TestCase):\n"
                "    def test_worker(self):\n"
                "        threading.Thread(target=blocked_worker, daemon=False).start()\n"
                "        faulthandler.dump_traceback_later(0.2, exit=True)\n",
                encoding="utf-8",
            )
            inventory = root / "tests.json"
            inventory.write_text(
                json.dumps(["shutdown_probe.ShutdownProbe.test_worker"]),
                encoding="utf-8",
            )
            script = (
                "import platform, runpy, sys\n"
                "platform.system = lambda: 'Windows'\n"
                "sys.argv = ['runner', '--shard', 'qt-assets', sys.argv[1]]\n"
                "try:\n"
                "    runpy.run_module('scripts.run_ci_unittests', run_name='__main__')\n"
                "except SystemExit as result:\n"
                "    print('SHARD_MAIN_RETURNED:', result.code, flush=True)\n"
            )
            completed = subprocess.run(
                [sys.executable, "-u", "-c", script, str(inventory)],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "PYTHONPATH": str(root), "PYTHONUTF8": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                timeout=10,
            )
        output = completed.stdout
        self.assertEqual(completed.returncode, 1, output)
        self.assertIn("SHARD_MAIN_RETURNED: 0", output)
        self.assertIn("OK", output.splitlines())
        self.assertIn("Timeout (", output)
        self.assertIn("blocked_worker", output)
        self.assertIn("_shutdown", output)


if __name__ == "__main__":
    unittest.main()
