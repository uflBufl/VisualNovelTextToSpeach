import gc
import shutil
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from vntts import live_replay


class ReplaySnapshotLifetimeTest(unittest.TestCase):
    def test_pending_release_keeps_directory_until_drain(self):
        owner = live_replay._ReplaySnapshots()
        completed = Event()
        removed = Event()
        directory = owner.create_directory("replay-test-")
        Path(directory, "payload").write_text("kept", encoding="utf-8")
        started = []
        original_thread = live_replay.Thread
        original_remove = owner._remove_directory

        def make_thread(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            started.append(thread)
            return thread

        def remove(path):
            original_remove(path)
            removed.set()

        owner.wait_for = completed
        with patch.object(live_replay, "Thread", side_effect=make_thread):
            with patch.object(owner, "_remove_directory", side_effect=remove):
                owner.close()
                try:
                    self.assertTrue(Path(directory).exists())
                    completed.set()
                    self.assertTrue(removed.wait(1))
                    self.assertEqual(owner.directories, [])
                finally:
                    completed.set()
                    started[0].join(1)
                    self.assertFalse(started[0].is_alive())

        self.assertFalse(Path(directory).exists())

    def test_release_attempts_all_directories_and_preserves_primary_failure(self):
        owner = live_replay._ReplaySnapshots()
        first = owner.create_directory("replay-first-")
        second = owner.create_directory("replay-second-")
        primary = KeyboardInterrupt("first cleanup failed")
        calls = []

        def remove(path):
            calls.append(path)
            if path == first:
                raise primary
            raise OSError("second cleanup failed")

        try:
            with patch.object(live_replay.shutil, "rmtree", side_effect=remove):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    owner.close()
            self.assertIs(caught.exception, primary)
            self.assertEqual(calls, [first, second])
            self.assertEqual(owner.directories, [first, second])
            self.assertTrue(
                any("second cleanup failed" in note for note in primary.__notes__)
            )
        finally:
            for directory in (first, second):
                shutil.rmtree(directory, ignore_errors=True)

    def test_thread_start_failure_retains_directories_with_recovery_note(self):
        owner = live_replay._ReplaySnapshots()
        directory = owner.create_directory("replay-start-failure-")
        start_error = RuntimeError("drain thread unavailable")

        try:
            with patch.object(live_replay.Thread, "start", side_effect=start_error):
                with self.assertRaises(RuntimeError) as caught:
                    owner.wait_for = Event()
                    owner.close()
            self.assertIs(caught.exception, start_error)
            self.assertEqual(owner.directories, [directory])
            self.assertTrue(Path(directory).exists())
            self.assertTrue(
                any(
                    "Replay snapshots retained" in note
                    for note in start_error.__notes__
                )
            )
            del owner
            gc.collect()
            self.assertTrue(Path(directory).exists())
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_join_failure_attempts_every_executor_and_keeps_router_pending(self):
        calls = []
        targets = []
        completed = Event()
        wait_error = RuntimeError("reader wait failed")
        join_primary = KeyboardInterrupt("primary join failed")
        join_later = OSError("later join failed")
        frame_source = Mock()
        reader = Mock(shutdown_started=False, stop_event=Event())
        reader.wait.side_effect = wait_error

        def seal_reader():
            reader.shutdown_started = True
            reader.stop_event.set()
            calls.append("reader.shutdown")

        reader.shutdown.side_effect = seal_reader
        router = Mock()
        deferred_thread = Mock()

        def capture_thread(*, target, **_kwargs):
            targets.append(target)
            return deferred_thread

        def executor(name, error):
            item = Mock()

            def shutdown(*, wait, cancel_futures):
                calls.append((name, wait, cancel_futures))
                if wait and error is not None:
                    raise error

            item.shutdown.side_effect = shutdown
            return item

        with patch.object(live_replay, "Thread", side_effect=capture_thread):
            with self.assertRaisesRegex(RuntimeError, "reader wait failed"):
                live_replay._shutdown_legacy_replay(
                    frame_source,
                    reader,
                    [executor("primary", join_primary), executor("later", join_later)],
                    router,
                    completed,
                )

        self.assertTrue(targets)
        self.assertIn(("primary", False, True), calls)
        self.assertIn(("later", False, True), calls)
        with self.assertRaises(KeyboardInterrupt) as joined:
            targets[0]()
        self.assertIs(joined.exception, join_primary)
        self.assertIn(("primary", True, False), calls)
        self.assertIn(("later", True, False), calls)
        self.assertTrue(
            any("later join failed" in note for note in join_primary.__notes__)
        )
        router.stop.assert_not_called()
        self.assertFalse(completed.is_set())

    def test_reader_wait_failure_seals_reader_and_defers_safe_finish(self):
        calls = []
        targets = []
        completed = Event()
        wait_error = RuntimeError("reader wait failed")
        cancel_error = OSError("cancel failed")
        frame_source = Mock()
        reader = Mock(shutdown_started=False, stop_event=Event())
        reader.wait.side_effect = wait_error

        def seal_reader():
            reader.shutdown_started = True
            reader.stop_event.set()
            calls.append("reader.shutdown")

        reader.shutdown.side_effect = seal_reader
        router = Mock()
        deferred_thread = Mock()

        def capture_thread(*, target, **_kwargs):
            targets.append(target)
            return deferred_thread

        def executor(name, cancel_fails=False):
            item = Mock()

            def shutdown(*, wait, cancel_futures):
                calls.append((name, wait, cancel_futures))
                if not wait and cancel_fails:
                    raise cancel_error

            item.shutdown.side_effect = shutdown
            return item

        with patch.object(live_replay, "Thread", side_effect=capture_thread):
            with self.assertRaises(RuntimeError) as caught:
                live_replay._shutdown_legacy_replay(
                    frame_source,
                    reader,
                    [executor("cancel", cancel_fails=True), executor("safe")],
                    router,
                    completed,
                )

        self.assertIs(caught.exception, wait_error)
        self.assertTrue(reader.shutdown_started)
        self.assertTrue(reader.stop_event.is_set())
        self.assertIn(("cancel", False, True), calls)
        self.assertIn(("safe", False, True), calls)
        self.assertTrue(targets)
        targets[0]()
        self.assertIn(("cancel", True, False), calls)
        self.assertIn(("safe", True, False), calls)
        router.stop.assert_called_once_with()
        self.assertTrue(completed.is_set())
        self.assertTrue(any("cancel failed" in note for note in wait_error.__notes__))


if __name__ == "__main__":
    unittest.main()
