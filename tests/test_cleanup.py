import unittest
from unittest.mock import Mock

from vntts.cleanup import attempt_cleanup, cleanup_on_exit


class CleanupTest(unittest.TestCase):
    def test_explicit_primary_error_keeps_identity_and_cleanup_runs_once(self):
        for error_type in (OSError, KeyboardInterrupt, SystemExit):
            with self.subTest(error_type=error_type):
                primary = ValueError("operation failed")
                cleanup = Mock(side_effect=error_type("cleanup failed"))
                with self.assertRaises(ValueError) as raised:
                    with cleanup_on_exit(cleanup, description="Resource cleanup"):
                        raise primary
                self.assertIs(raised.exception, primary)
                self.assertEqual(
                    primary.__notes__, ["Resource cleanup failed: cleanup failed"]
                )
                cleanup.assert_called_once_with()

    def test_standalone_failure_propagates_even_during_unrelated_handled_error(self):
        for error_type in (OSError, KeyboardInterrupt, SystemExit):
            with self.subTest(error_type=error_type):
                failure = error_type("cleanup failed")
                cleanup = Mock(side_effect=failure)
                try:
                    raise ValueError("unrelated handled failure")
                except ValueError:
                    with self.assertRaises(error_type) as raised:
                        with cleanup_on_exit(cleanup, description="Resource cleanup"):
                            pass
                self.assertIs(raised.exception, failure)
                cleanup.assert_called_once_with()

    def test_outer_owner_keeps_notes_from_nested_resource_cleanup(self):
        primary = ValueError("render failed")
        release_error = OSError("log close failed")
        release_error.add_note("Directory cleanup failed: access denied")
        cleanup = Mock(side_effect=release_error)
        attempt_cleanup(cleanup, description="Backend shutdown", primary_error=primary)
        self.assertEqual(
            primary.__notes__,
            [
                "Backend shutdown failed: log close failed",
                "Directory cleanup failed: access denied",
            ],
        )
        cleanup.assert_called_once_with()

    def test_attempt_reports_success_and_failure_without_owning_another_cleanup(self):
        cleanup = Mock()
        self.assertTrue(attempt_cleanup(cleanup, description="Resource cleanup"))
        cleanup.assert_called_once_with()
        primary = ValueError("operation failed")
        failed = Mock(side_effect=OSError("cleanup failed"))
        self.assertFalse(
            attempt_cleanup(
                failed, description="Resource cleanup", primary_error=primary
            )
        )
        failed.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
