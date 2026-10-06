import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts.cleanup import attempt_cleanup, cleanup_on_exit, temporary_directory


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

    def test_temporary_directory_preserves_path_contract_and_cleanup_errors(self):
        for operation_failed in (False, True):
            with self.subTest(operation_failed=operation_failed):
                primary = ValueError("operation failed")
                cleanup_error = OSError("directory removal failed")
                original_cleanup = TemporaryDirectory.cleanup

                def remove_then_fail(directory):
                    original_cleanup(directory)
                    raise cleanup_error

                with TemporaryDirectory() as parent:
                    with patch.object(TemporaryDirectory, "cleanup", remove_then_fail):
                        expected = ValueError if operation_failed else OSError
                        with self.assertRaises(expected) as raised:
                            with temporary_directory(
                                dir=parent, prefix="owned-"
                            ) as directory:
                                self.assertIsInstance(directory, str)
                                self.assertEqual(Path(directory).parent, Path(parent))
                                self.assertTrue(
                                    Path(directory).name.startswith("owned-")
                                )
                                self.assertTrue(Path(directory).is_dir())
                                if operation_failed:
                                    raise primary
                    self.assertIs(
                        raised.exception, primary if operation_failed else cleanup_error
                    )
                    self.assertFalse(Path(directory).exists())
                    if operation_failed:
                        self.assertEqual(
                            primary.__notes__,
                            [
                                "Temporary directory cleanup failed: directory removal failed"
                            ],
                        )

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
