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
    _isolate_pregeneration_tests,
    _run_exact_test_file,
    _run_shard,
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

    def test_pregeneration_shard_preserves_every_exact_test_once(self):
        values = (
            "tests.test_settings.SettingsTest.test_value",
            "tests.test_self_service_pregeneration.JourneyTest.test_first",
            "tests.test_self_service_pregeneration.JourneyTest.test_last",
        )
        isolated, remaining = _isolate_pregeneration_tests(values)
        self.assertEqual(isolated, values[1:])
        self.assertEqual(remaining, values[:1])
        self.assertEqual(sorted((*isolated, *remaining)), sorted(values))
        self.assertEqual(len(set((*isolated, *remaining))), len(values))

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
                patch(
                    "scripts.run_ci_unittests._flatten_suite",
                    return_value=(
                        Mock(id=Mock(return_value=value)) for value in test_ids
                    ),
                ),
                patch(
                    "scripts.run_ci_unittests.subprocess.run",
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
                patch(
                    "scripts.run_ci_unittests.unittest.defaultTestLoader.loadTestsFromNames"
                ) as load,
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
            self.assertEqual(_run_exact_test_file(path), 0)

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
            patch(
                "scripts.run_ci_unittests._flatten_suite",
                return_value=(Mock(id=Mock(return_value="test-id")),),
            ),
            patch(
                "scripts.run_ci_unittests.partition_ui_test_ids",
                return_value=(("app-id",), (), (), ()),
            ),
            patch("scripts.run_ci_unittests.subprocess.run", side_effect=primary),
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
                    patch("scripts.run_ci_unittests.subprocess.run", side_effect=run),
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

    def test_selected_module_runs_only_its_macos_shard(self):
        test_ids = (
            "tests.test_app.AppTest.test_ui",
            "tests.test_asset_ui.AssetTest.test_ui",
            "tests.test_ocr_review.OcrTest.test_ui",
            "tests.test_settings.SettingsTest.test_value",
        )
        with (
            patch(
                "scripts.run_ci_unittests._flatten_suite",
                return_value=(Mock(id=Mock(return_value=value)) for value in test_ids),
            ),
            patch(
                "scripts.run_ci_unittests.subprocess.run",
                return_value=Mock(returncode=0),
            ) as run,
        ):
            self.assertEqual(
                _run_sharded_full_discovery("Darwin", ["tests.test_app"]), 0
            )
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-3:-1], ["--shard", "qt-app"])

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
                    "scripts.run_ci_unittests.subprocess.run",
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
                patch(
                    "scripts.run_ci_unittests._flatten_suite",
                    return_value=(
                        Mock(id=Mock(return_value=value)) for value in test_ids
                    ),
                ),
                patch(
                    "scripts.run_ci_unittests.subprocess.run",
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
                "        faulthandler.dump_traceback_later(0.2)\n",
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
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                subprocess.run(
                    [sys.executable, "-u", "-c", script, str(inventory)],
                    cwd=Path(__file__).resolve().parents[1],
                    env={**os.environ, "PYTHONPATH": str(root), "PYTHONUTF8": "1"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=10,
                    check=False,
                )
        output = caught.exception.stdout.decode("utf-8")
        self.assertIn("SHARD_MAIN_RETURNED: 0", output)
        self.assertIn("OK", output.splitlines())
        self.assertIn("Timeout (", output)
        self.assertIn("blocked_worker", output)
        self.assertIn("_shutdown", output)


if __name__ == "__main__":
    unittest.main()
