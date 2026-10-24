"""Ordinary support exports retain actionable failures without manual probes."""

import json
import logging
import sys
import threading
import unittest
import zipfile
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts import support
from vntts.async_ui import _Task
from vntts.game_content_importer import GameContentImportCancelled
from vntts.pregeneration_setup import PregenerationJobStore
from vntts.settings import AppSettings


class ApplicationErrorLoggingTest(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        self.handlers = list(root.handlers)
        self.root_level = root.level
        self.error_log = support.application_error_log
        self.error_handler = support._application_error_handler
        root.setLevel(logging.WARNING)

    def tearDown(self):
        root = logging.getLogger()
        for handler in root.handlers:
            if handler not in self.handlers:
                handler.close()
        root.handlers = self.handlers
        root.setLevel(self.root_level)
        support.application_error_log = self.error_log
        support._application_error_handler = self.error_handler

    def test_catalog_failure_survives_discarded_result_restart_and_ordinary_export(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "jobs" / "legacy-job" / "job.json"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"legacy preparation")
            store = PregenerationJobStore(root / "jobs")
            path = root / "errors.log"
            support.configure_application_error_log(path)
            signals = Mock()
            task = _Task(1, store.source_story_indexes, signals)
            with patch("vntts.async_ui.isValid", return_value=False):
                task.run()
            signals.finished.emit.assert_not_called()
            self.assertFalse(store.catalog.path.exists())
            self.assertEqual(original.read_bytes(), b"legacy preparation")

            restored = support.configure_application_error_log(path)
            self.assertTrue(restored.snapshot())
            with patch("vntts.support.collect_build_identity", return_value={}):
                output = support.SupportBundleBuilder(
                    AppSettings(),
                    support.RuntimeSupportLog(),
                    dependency_probe=lambda: {},
                ).build(root / "support.zip")
            with zipfile.ZipFile(output) as archive:
                report = json.loads(archive.read("application-errors.json"))
            text = json.dumps(report)
            for expected in (
                "explicit legacy migration",
                "PreparationCatalogError",
                "PregenerationSetupError",
                "_check_missing_catalog",
                "_catalog_call",
                "Traceback",
            ):
                self.assertIn(expected, text)
            self.assertIsNone(report["persistence_error"])

    def test_exception_chain_and_notes_are_redacted_without_capturing_locals(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "errors.log"
            log = support.configure_application_error_log(path)
            private_local = "PRIVATE_LOCAL_CONTENT"
            try:
                try:
                    raise PermissionError(
                        r"C:\Users\PrivateUser\catalog.sqlite3 token=private-token"
                    )
                except PermissionError as cause:
                    error = RuntimeError("Unable to restore preparation")
                    error.add_note("Cleanup failed: password=private-password")
                    raise error from cause
            except RuntimeError:
                logging.getLogger("vntts.test").exception("Catalog restore failed")
            text = path.read_text(encoding="utf-8")
            for forbidden in (
                private_local,
                "PrivateUser",
                "private-token",
                "private-password",
            ):
                self.assertNotIn(forbidden, text)
            for expected in ("PermissionError", "RuntimeError", "Cleanup failed"):
                self.assertIn(expected, text)
            event = log.snapshot()[0]
            self.assertEqual(event["exception_type"], "RuntimeError")
            self.assertTrue(event["source_line"])
            self.assertEqual(event["logger"], "vntts.test")

    def test_unhandled_hooks_capture_callbacks_threads_and_unraisable_failures(self):
        log = support.configure_application_error_log()
        previous_system, previous_thread, previous_unraisable = Mock(), Mock(), Mock()
        with (
            patch.object(sys, "excepthook", previous_system),
            patch.object(threading, "excepthook", previous_thread),
            patch.object(sys, "unraisablehook", previous_unraisable),
        ):
            with support.capture_unhandled_errors():
                try:
                    raise ValueError("Qt callback failed")
                except ValueError as error:
                    sys.excepthook(type(error), error, error.__traceback__)
                    sys.unraisablehook(
                        SimpleNamespace(
                            exc_type=type(error),
                            exc_value=error,
                            exc_traceback=error.__traceback__,
                        )
                    )

                def fail():
                    raise OSError("Worker thread failed")

                thread = threading.Thread(target=fail)
                thread.start()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            self.assertIs(sys.excepthook, previous_system)
            self.assertIs(threading.excepthook, previous_thread)
            self.assertIs(sys.unraisablehook, previous_unraisable)
        previous_system.assert_called_once()
        previous_thread.assert_called_once()
        previous_unraisable.assert_called_once()
        text = json.dumps(log.snapshot())
        self.assertIn("Qt callback failed", text)
        self.assertIn("Worker thread failed", text)
        self.assertIn("Unraisable", text)

    def test_capture_preserves_propagated_startup_failure(self):
        log = support.configure_application_error_log()
        failure = RuntimeError("Initialization failed")
        with self.assertRaises(RuntimeError) as caught:
            with support.capture_unhandled_errors():
                raise failure
        self.assertIs(caught.exception, failure)
        self.assertIn("Initialization failed", log.snapshot()[0]["traceback"])

    def test_logging_failure_preserves_original_exception_in_memory(self):
        log = support.configure_application_error_log(Path("unused-errors.log"))
        with patch.object(
            log, "_persist_locked", side_effect=PermissionError("read only")
        ):
            try:
                raise ValueError("Original failure")
            except ValueError:
                logging.getLogger("vntts.test").exception("Action failed")
        self.assertIn("read only", log.persistence_error)
        self.assertIn("Original failure", log.snapshot()[0]["traceback"])

    def test_unreadable_previous_log_is_reported_and_preserved(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "errors.log"
            original = b"previous error evidence"
            path.write_bytes(original)
            with patch(
                "vntts.support.open_regular_binary",
                side_effect=PermissionError("log is unreadable"),
            ):
                log = support.configure_application_error_log(path)
            logging.getLogger("vntts.test").error("New error remains available")
            self.assertIn("log is unreadable", log.persistence_error)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(
                log.snapshot()[0]["message"], "New error remains available"
            )

    def test_retention_and_reconfiguration_remain_bounded_without_duplicate_handlers(
        self,
    ):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "errors.log"
            support.configure_application_error_log(path)
            log = support.configure_application_error_log(path)
            log.maximum_bytes = 1024
            log.entries = deque(maxlen=3)
            for index in range(8):
                logging.getLogger("vntts.test").error("Failure %s", index)
            self.assertLessEqual(len(path.read_bytes()), 1024)
            self.assertLessEqual(len(log.snapshot()), 3)
            self.assertEqual(
                sum(event["message"] == "Failure 7" for event in log.snapshot()), 1
            )

    def test_expected_task_cancellation_does_not_become_application_failure(self):
        log = support.configure_application_error_log()

        def cancel():
            raise GameContentImportCancelled("Import cancelled by user")

        signals = Mock()
        with patch("vntts.async_ui.isValid", return_value=True):
            _Task(1, cancel, signals).run()
        self.assertEqual(log.snapshot(), [])
        self.assertIsInstance(
            signals.finished.emit.call_args.args[2], GameContentImportCancelled
        )

    def test_early_bundled_dependency_failure_is_persistent(self):
        from vntts import app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("vntts.app.get_local_data_directory", return_value=root),
                patch(
                    "vntts.app.configure_bundled_dependencies",
                    side_effect=ImportError("Bundled OCR DLL failed"),
                ),
                self.assertRaises(ImportError),
            ):
                app.main([])
            text = (root / "errors.log").read_text(encoding="utf-8")
            self.assertIn("Bundled OCR DLL failed", text)
            self.assertIn("ImportError", text)


if __name__ == "__main__":
    unittest.main()
