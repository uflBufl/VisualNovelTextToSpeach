import ast
import os
import runpy
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts import preview_content_worker as worker
from vntts.preview_content_advisor import (
    PreviewContentCheck,
    PreviewContentCheckCancelled,
    PreviewContentCheckError,
)
from vntts.speech_content_advice import assess_speech_content


def _successful_child(sender, root, _previews, _install, _cancel=None):
    (root / "worker-pid").write_text(str(os.getpid()))
    sender.send(
        (
            "ok",
            (
                PreviewContentCheck(
                    "voice",
                    "a" * 64,
                    "b" * 64,
                    "Hello",
                    0,
                    assess_speech_content("Hello", "Hello"),
                ),
            ),
        )
    )
    sender.close()


def _cancellable_child(sender, root, _previews, _install, cancelled):
    (root / "worker-pid").write_text(str(os.getpid()))
    if cancelled.wait(10):
        (root / "cancelled").write_text("cancelled before publication")
    else:
        (root / "published").write_text("unexpected publication")
    sender.close()


def _crashed_child(_sender, _root, _previews, _install, _cancel):
    os._exit(23)


class PreviewContentWorkerTest(unittest.TestCase):
    def setUp(self):
        self.context = Mock()
        self.receiver, self.sender = Mock(), Mock()
        self.context.Pipe.return_value = self.receiver, self.sender
        self.process = self.context.Process.return_value
        self.process.pid = 123
        self.process.exitcode = 7
        self.process.is_alive.return_value = False
        self.receiver.poll.return_value = True
        self.receiver.recv.return_value = ("ok", ())
        self.context_patch = patch.object(
            worker.multiprocessing, "get_context", return_value=self.context
        )
        self.get_context = self.context_patch.start()
        self.addCleanup(self.context_patch.stop)

    def run_worker(self, cancel_event=None):
        return worker.check_in_process(Path("previews"), (), False, cancel_event)

    def assert_closed(self):
        self.receiver.close.assert_called_once_with()
        self.sender.close.assert_called()
        self.process.close.assert_called_once_with()

    def test_spawn_uses_fixed_target_and_releases_resources_after_success(self):
        self.assertEqual(self.run_worker(), ())
        self.get_context.assert_called_once_with("spawn")
        self.assertIs(
            self.context.Process.call_args.kwargs["target"], worker._check_previews
        )
        self.process.start.assert_called_once_with()
        self.process.terminate.assert_not_called()
        self.context.Event.return_value.set.assert_called_once_with()
        self.assert_closed()

    def test_worker_errors_eof_and_invalid_messages_fail_without_results(self):
        for message in (("error", "model unavailable"), ("ok", ["invalid"]), None):
            with self.subTest(message=message):
                self.receiver.recv.return_value = message
                with self.assertRaises(PreviewContentCheckError):
                    self.run_worker()
        self.receiver.recv.side_effect = EOFError("worker closed pipe")
        with self.assertRaisesRegex(PreviewContentCheckError, "worker failed"):
            self.run_worker()
        self.assertEqual(self.process.close.call_count, 4)

    def test_crashed_worker_is_reported_and_closed(self):
        self.receiver.poll.return_value = False
        with self.assertRaisesRegex(PreviewContentCheckError, "unexpectedly.*7"):
            self.run_worker()
        self.assert_closed()

    def test_timeout_stops_running_worker(self):
        self.receiver.poll.return_value = False
        self.process.is_alive.side_effect = [True, True, False, False]
        with patch.object(worker, "monotonic", side_effect=[0, 301]):
            with self.assertRaisesRegex(PreviewContentCheckError, "timed out"):
                self.run_worker()
        self.process.terminate.assert_called_once_with()
        self.assert_closed()

    def test_cancellation_during_work_stops_worker_and_discards_results(self):
        cancelled = threading.Event()

        def receive(_timeout):
            cancelled.set()
            return True

        self.receiver.poll.side_effect = receive
        self.process.is_alive.side_effect = [True, False, False]
        with self.assertRaises(PreviewContentCheckCancelled):
            self.run_worker(cancelled)
        self.process.terminate.assert_called_once_with()
        self.assert_closed()

    def test_cancelled_request_does_not_allocate_process(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(PreviewContentCheckCancelled):
            self.run_worker(cancelled)
        self.get_context.assert_not_called()

    def test_start_failure_releases_pipes_and_unstarted_process(self):
        self.process.pid = None
        self.process.start.side_effect = OSError("cannot start")
        with self.assertRaisesRegex(PreviewContentCheckError, "cannot start"):
            self.run_worker()
        self.process.join.assert_not_called()
        self.assert_closed()

    def test_stubborn_worker_is_killed_after_termination(self):
        self.process.is_alive.side_effect = [True, True, False]
        worker._close_worker(self.process)
        self.process.terminate.assert_called_once_with()
        self.process.kill.assert_called_once_with()
        self.process.close.assert_called_once_with()

    def test_cleanup_failure_preserves_cancellation(self):
        cancelled = threading.Event()
        self.receiver.poll.side_effect = lambda _timeout: cancelled.set() or True
        self.process.join.side_effect = OSError("cleanup failed")
        with self.assertRaises(PreviewContentCheckCancelled) as caught:
            self.run_worker(cancelled)
        self.assertIn("cleanup failed", " ".join(caught.exception.__notes__))
        self.receiver.close.assert_called_once_with()
        self.process.terminate.assert_called_once_with()
        self.process.kill.assert_called_once_with()
        self.process.close.assert_called_once_with()
        self.context.Event.return_value.set.assert_called_once_with()

    def test_termination_failure_still_kills_reaps_and_closes_worker(self):
        self.process.is_alive.side_effect = [True, True, False]
        self.process.terminate.side_effect = OSError("cannot terminate")
        with self.assertRaisesRegex(RuntimeError, "cleanup failed") as caught:
            worker._close_worker(self.process)
        self.assertIn("cannot terminate", " ".join(caught.exception.__notes__))
        self.process.kill.assert_called_once_with()
        self.assertEqual(self.process.join.call_count, 3)
        self.process.close.assert_called_once_with()

    def test_child_limits_threads_and_keeps_model_work_inside_its_batch(self):
        torch = Mock()
        advisor = Mock()
        advisor._check_batch.return_value = ()
        with (
            patch.dict(sys.modules, {"torch": torch}),
            patch.dict(os.environ),
            patch(
                "vntts.preview_content_advisor.PreviewContentAdvisor",
                return_value=advisor,
            ) as create_advisor,
        ):
            worker._check_previews(
                self.sender, Path("previews"), (), True, self.context.Event.return_value
            )
            self.assertEqual(os.environ["OMP_NUM_THREADS"], "1")
        torch.set_num_threads.assert_called_once_with(1)
        torch.set_num_interop_threads.assert_called_once_with(1)
        create_advisor.assert_called_once_with(Path("previews"))
        advisor._check_batch.assert_called_once_with(
            (), True, self.context.Event.return_value
        )
        self.sender.send.assert_called_once_with(("ok", ()))
        self.sender.close.assert_called_once_with()

    def test_child_serializes_failures_as_text(self):
        with (
            patch.dict(sys.modules, {"torch": Mock()}),
            patch.dict(os.environ),
            patch(
                "vntts.preview_content_advisor.PreviewContentAdvisor",
                side_effect=RuntimeError("model failure"),
            ),
        ):
            worker._check_previews(self.sender, Path("previews"), (), False)
        self.sender.send.assert_called_once_with(("error", "model failure"))
        self.sender.close.assert_called_once_with()

    def test_real_spawn_returns_from_another_process_and_reaps_it(self):
        self.context_patch.stop()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            processes = []
            close_worker = worker._close_worker

            def close(process):
                processes.append(process)
                close_worker(process)

            with (
                patch.object(worker, "_check_previews", _successful_child),
                patch.object(worker, "_close_worker", side_effect=close),
            ):
                result = worker.check_in_process(root, (), False, None)
            self.assertEqual(result[0].source_id, "voice")
            self.assertNotEqual(int((root / "worker-pid").read_text()), os.getpid())
            self.assertEqual(len(processes), 1)
            with self.assertRaises(ValueError):
                processes[0].is_alive()

    def test_real_spawn_cancellation_reaches_child_before_cleanup_waits(self):
        self.context_patch.stop()
        with TemporaryDirectory() as directory:
            root = Path(directory)

            class CancelWhenStarted:
                def is_set(self):
                    return (root / "worker-pid").exists()

            with (
                patch.object(worker, "_check_previews", _cancellable_child),
                patch.object(worker, "_CHECK_TIMEOUT_SECONDS", 10),
                patch.object(
                    worker, "_close_worker", wraps=worker._close_worker
                ) as close,
            ):
                with self.assertRaises(PreviewContentCheckCancelled):
                    worker.check_in_process(root, (), False, CancelWhenStarted())
            self.assertTrue((root / "cancelled").exists())
            self.assertFalse((root / "published").exists())
            with self.assertRaises(ValueError):
                close.call_args.args[0].is_alive()

    def test_real_spawn_crash_is_reported_and_reaped(self):
        self.context_patch.stop()
        with TemporaryDirectory() as directory:
            with (
                patch.object(worker, "_check_previews", _crashed_child),
                patch.object(
                    worker, "_close_worker", wraps=worker._close_worker
                ) as close,
            ):
                with self.assertRaises(PreviewContentCheckError):
                    worker.check_in_process(Path(directory), (), False, None)
            with self.assertRaises(ValueError):
                close.call_args.args[0].is_alive()

    def test_frozen_entrypoint_dispatches_before_desktop_imports(self):
        source = Path(worker.__file__).with_name("app.py").read_text()
        statements = ast.parse(source).body
        dispatch = next(
            index
            for index, statement in enumerate(statements)
            if isinstance(statement, ast.If)
            and any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "freeze_support"
                for node in ast.walk(statement)
            )
        )
        self.assertTrue(
            all(
                not isinstance(statement, ast.ImportFrom)
                or not (statement.module or "").startswith(
                    ("PySide6", "pynput", "vntts")
                )
                for statement in statements[:dispatch]
            )
        )

        class WorkerDispatched(Exception):
            pass

        with (
            patch.object(sys, "frozen", True, create=True),
            patch(
                "multiprocessing.freeze_support", side_effect=WorkerDispatched
            ) as dispatch_worker,
        ):
            with self.assertRaises(WorkerDispatched):
                runpy.run_path(str(Path(worker.__file__).with_name("app.py")))
        dispatch_worker.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
