"""Logging test fixtures preserve application state and contain their failures."""

import io
import logging
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.error_logging_fixtures import isolated_application_error_log
from vntts import support


class ErrorLoggingIsolationTest(unittest.TestCase):
    def setUp(self):
        self.log = self.enterContext(isolated_application_error_log())
        self.root = logging.getLogger()
        self.root.setLevel(logging.WARNING)
        self.logger = logging.getLogger(__name__)

    def test_nested_and_repeated_contexts_restore_their_own_state(self):
        original_handler = support._application_error_handler
        self.root.setLevel(logging.ERROR)
        for _ in range(2):
            with isolated_application_error_log() as outer:
                outer_handler = support._application_error_handler
                self.assertIsNone(outer.path)
                self.root.setLevel(logging.WARNING)
                self.logger.error("Outer failure")
                with isolated_application_error_log() as inner:
                    inner_handler = support._application_error_handler
                    self.assertIsNone(inner.path)
                    self.root.setLevel(logging.DEBUG)
                    self.logger.error("Inner failure")
                self.assertIs(support.application_error_log, outer)
                self.assertIs(support._application_error_handler, outer_handler)
                self.assertIn(outer_handler, self.root.handlers)
                self.assertTrue(inner_handler._closed)
                self.assertEqual(self.root.level, logging.WARNING)
                self.logger.error("Outer failure after nesting")
                self.assertEqual(
                    [entry["message"] for entry in outer.snapshot()],
                    ["Outer failure", "Outer failure after nesting"],
                )
                self.assertEqual(
                    [entry["message"] for entry in inner.snapshot()],
                    ["Inner failure"],
                )
            self.assertIs(support.application_error_log, self.log)
            self.assertIs(support._application_error_handler, original_handler)
            self.assertIn(original_handler, self.root.handlers)
            self.assertTrue(outer_handler._closed)
            self.assertEqual(self.root.level, logging.ERROR)
        self.assertEqual(self.log.snapshot(), [])

    def test_exception_exit_restores_state_and_preserves_original_failure(self):
        original_handler = support._application_error_handler
        self.root.setLevel(logging.ERROR)
        failure = RuntimeError("Intentional context failure")
        with self.assertRaises(RuntimeError) as caught:
            with isolated_application_error_log():
                temporary_handler = support._application_error_handler
                self.root.setLevel(logging.DEBUG)
                raise failure
        self.assertIs(caught.exception, failure)
        self.assertIs(support.application_error_log, self.log)
        self.assertIs(support._application_error_handler, original_handler)
        self.assertIn(original_handler, self.root.handlers)
        self.assertNotIn(temporary_handler, self.root.handlers)
        self.assertTrue(temporary_handler._closed)
        self.assertEqual(self.root.level, logging.ERROR)

    def test_previous_handler_is_not_closed_and_works_after_restoration(self):
        previous_handler = support._application_error_handler
        with patch.object(
            previous_handler, "close", wraps=previous_handler.close
        ) as close:
            with isolated_application_error_log():
                self.assertNotIn(previous_handler, self.root.handlers)
                self.logger.error("Isolated failure")
            close.assert_not_called()
            self.logger.error("Failure after restoration")
        self.assertFalse(previous_handler._closed)
        self.assertEqual(
            [entry["message"] for entry in self.log.snapshot()],
            ["Failure after restoration"],
        )

    def test_originally_detached_handler_remains_detached(self):
        previous_handler = support._application_error_handler
        self.root.removeHandler(previous_handler)
        with patch.object(
            previous_handler, "close", wraps=previous_handler.close
        ) as close:
            with isolated_application_error_log():
                self.logger.error("Isolated failure")
            self.assertIs(support.application_error_log, self.log)
            self.assertIs(support._application_error_handler, previous_handler)
            self.assertNotIn(previous_handler, self.root.handlers)
            close.assert_not_called()
        self.assertFalse(previous_handler._closed)
        self.assertEqual(self.log.snapshot(), [])

    def test_unrelated_handler_added_in_context_survives_teardown(self):
        stream = io.StringIO()
        unrelated = logging.StreamHandler(stream)
        self.addCleanup(unrelated.close)
        self.addCleanup(self.root.removeHandler, unrelated)
        with patch.object(unrelated, "close", wraps=unrelated.close) as close:
            with isolated_application_error_log():
                self.root.addHandler(unrelated)
                self.logger.error("Failure during context")
            self.assertIn(unrelated, self.root.handlers)
            close.assert_not_called()
            self.logger.error("Failure after context")
        self.assertEqual(
            stream.getvalue().splitlines(),
            ["Failure during context", "Failure after context"],
        )

    def test_temporary_persistent_log_cannot_receive_later_failures(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "errors.log"
            with isolated_application_error_log():
                log = support.configure_application_error_log(path)
                temporary_handler = support._application_error_handler
                try:
                    raise ValueError("Intentional persistent failure")
                except ValueError:
                    self.logger.exception("Persistent action failed")
                self.assertEqual(log.snapshot()[0]["exception_type"], "ValueError")
            self.assertTrue(temporary_handler._closed)
            self.assertNotIn(temporary_handler, self.root.handlers)
            original = path.read_bytes()
            self.assertIn(b"Intentional persistent failure", original)
            self.logger.error("Later failure must stay in memory")
            self.assertEqual(path.read_bytes(), original)
            path.unlink()
            path.parent.rmdir()
            self.logger.error("Later failure must not recreate the file")
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
            self.assertEqual(len(self.log.snapshot()), 2)

    def test_capture_failure_restores_all_hooks_and_original_exception(self):
        previous_system = sys.excepthook
        previous_thread = threading.excepthook
        previous_unraisable = sys.unraisablehook
        failure = RuntimeError("Intentional unhandled failure")
        with self.assertRaises(RuntimeError) as caught:
            with support.capture_unhandled_errors():
                self.assertIsNot(sys.excepthook, previous_system)
                self.assertIsNot(threading.excepthook, previous_thread)
                self.assertIsNot(sys.unraisablehook, previous_unraisable)
                raise failure
        self.assertIs(caught.exception, failure)
        self.assertIs(sys.excepthook, previous_system)
        self.assertIs(threading.excepthook, previous_thread)
        self.assertIs(sys.unraisablehook, previous_unraisable)
        event = self.log.snapshot()[0]
        self.assertEqual(event["exception_type"], "RuntimeError")
        self.assertIn(str(failure), event["traceback"])


if __name__ == "__main__":
    unittest.main()
