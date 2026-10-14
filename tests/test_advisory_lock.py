import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.symlink_support import symlink_or_skip
from vntts.authoring import advisory_lock
from vntts.authoring.advisory_lock import exclusive_advisory_lock


class AdvisoryLockTest(unittest.TestCase):
    def test_operation_failure_survives_release_and_close_failures(self) -> None:
        for stage in ("acquisition", "cancellation", "body"):
            with self.subTest(stage=stage), TemporaryDirectory() as directory:
                primary = KeyboardInterrupt(stage)
                release_error = OSError("unlock failed")
                release_error.add_note("unlock detail")
                close_error = OSError("close failed")
                close_error.add_note("close detail")

                def check_cancelled() -> None:
                    if stage == "cancellation":
                        raise primary

                with (
                    patch.object(advisory_lock.os, "open", return_value=42),
                    patch.object(
                        advisory_lock,
                        "_acquire",
                        side_effect=primary if stage == "acquisition" else None,
                    ),
                    patch.object(
                        advisory_lock, "_release", side_effect=release_error
                    ) as release,
                    patch.object(
                        advisory_lock.os, "close", side_effect=close_error
                    ) as close,
                    self.assertRaises(KeyboardInterrupt) as caught,
                ):
                    with exclusive_advisory_lock(
                        Path(directory) / "setup.lock", check_cancelled=check_cancelled
                    ):
                        raise primary

                self.assertIs(caught.exception, primary)
                expected_notes = []
                if stage == "acquisition":
                    release.assert_not_called()
                else:
                    release.assert_called_once_with(42)
                    expected_notes += [
                        "Advisory lock release failed: unlock failed",
                        "unlock detail",
                    ]
                close.assert_called_once_with(42)
                self.assertEqual(
                    primary.__notes__,
                    expected_notes
                    + [
                        "Advisory lock descriptor close failed: close failed",
                        "close detail",
                    ],
                )

    def test_standalone_cleanup_failure_propagates(self) -> None:
        for fail_release in (True, False):
            with (
                self.subTest(fail_release=fail_release),
                TemporaryDirectory() as directory,
            ):
                release_error = OSError("unlock failed")
                close_error = OSError("close failed")
                primary = release_error if fail_release else close_error
                with (
                    patch.object(advisory_lock.os, "open", return_value=42),
                    patch.object(advisory_lock, "_acquire"),
                    patch.object(
                        advisory_lock,
                        "_release",
                        side_effect=release_error if fail_release else None,
                    ) as release,
                    patch.object(
                        advisory_lock.os, "close", side_effect=close_error
                    ) as close,
                    self.assertRaises(OSError) as caught,
                ):
                    with exclusive_advisory_lock(Path(directory) / "setup.lock"):
                        pass

                self.assertIs(caught.exception, primary)
                release.assert_called_once_with(42)
                close.assert_called_once_with(42)
                self.assertEqual(
                    getattr(primary, "__notes__", []),
                    ["Advisory lock descriptor close failed: close failed"]
                    if fail_release
                    else [],
                )

    def test_cancellation_after_acquisition_releases_persistent_guard(self) -> None:
        with TemporaryDirectory() as directory:
            guard = Path(directory) / "setup.lock"
            checks = 0

            def check_cancelled() -> None:
                nonlocal checks
                checks += 1
                if checks == 2:
                    raise InterruptedError("cancelled after acquisition")

            with self.assertRaises(InterruptedError):
                with exclusive_advisory_lock(
                    guard, blocking=True, check_cancelled=check_cancelled
                ):
                    self.fail("Cancelled guard must not enter the body")
            self.assertEqual(checks, 2)
            self.assertTrue(guard.is_file())
            with exclusive_advisory_lock(guard):
                pass

    def test_lock_does_not_follow_alias_to_unrelated_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            unrelated = root / "unrelated.txt"
            unrelated.write_bytes(b"")
            lock_path = root / "setup.lock"
            symlink_or_skip(lock_path, unrelated)

            with self.assertRaises(OSError), exclusive_advisory_lock(lock_path):
                pass

            self.assertEqual(unrelated.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
