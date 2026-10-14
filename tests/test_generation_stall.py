import json
import unittest
from threading import Event, Thread, get_ident
from unittest.mock import patch

from vntts.generation_stall import GenerationStallWatchdog


class GenerationStallWatchdogTest(unittest.TestCase):
    def test_blocked_worker_captures_locations_without_locals_and_stops(self):
        blocked = Event()
        release = Event()
        captured = Event()
        snapshots = []
        monitors = []

        def blocked_worker():
            private_dialogue = "PRIVATE dialogue token=SECRET"
            with GenerationStallWatchdog() as monitor:
                monitors.append(monitor)
                monitor.progress(
                    {
                        "active": {
                            "phase": "generating",
                            "attempt": 2,
                            "provider_attempt": 2,
                            "provider": "moss-tts",
                            "seed": 1,
                            "queue_id": private_dialogue,
                            "text": private_dialogue,
                        }
                    }
                )
                blocked.set()
                release.wait(3)

        def record(**details):
            snapshots.append(details)
            captured.set()

        with (
            patch.object(GenerationStallWatchdog, "STALL_SECONDS", 0.02),
            patch.object(GenerationStallWatchdog, "POLL_SECONDS", 0.005),
            patch("vntts.generation_stall.record_generation_stall", side_effect=record),
        ):
            worker = Thread(target=blocked_worker)
            worker.start()
            try:
                self.assertTrue(blocked.wait(2))
                self.assertTrue(captured.wait(2))
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(monitors[0]._thread.is_alive())
        worker_snapshot = next(t for t in snapshots[0]["threads"] if t["worker"])
        self.assertEqual(worker_snapshot["thread_id"], worker.ident)
        self.assertIn(
            "blocked_worker", [f["function"] for f in worker_snapshot["frames"]]
        )
        self.assertEqual(snapshots[0]["attempt"], 2)
        self.assertEqual(snapshots[0]["phase"], "generating")
        serialized = json.dumps(snapshots)
        self.assertNotIn("PRIVATE", serialized)
        self.assertNotIn("SECRET", serialized)
        for thread in snapshots[0]["threads"]:
            for frame in thread["frames"]:
                self.assertFalse(frame["file"].startswith("/"))
                self.assertNotIn("locals", frame)
                self.assertNotIn("source", frame)

    def test_progress_resets_stall_budget_and_capture_count_is_bounded(self):
        with (
            patch("vntts.generation_stall.monotonic", return_value=0) as clock,
            patch("vntts.generation_stall.record_generation_stall") as record,
        ):
            monitor = GenerationStallWatchdog()
            monitor._check()
            record.assert_not_called()
            for now in (120, 121, 150, 180, 210):
                clock.return_value = now
                monitor._check()
            self.assertEqual(record.call_count, 3)
            monitor.progress({"active": {"phase": "retrying", "attempt": 3}})
            clock.return_value = 329
            monitor._check()
            self.assertEqual(record.call_count, 3)
            clock.return_value = 330
            monitor._check()
            self.assertEqual(record.call_count, 4)
            self.assertEqual(record.call_args.kwargs["attempt"], 3)
            self.assertEqual(record.call_args.kwargs["capture_index"], 1)
            monitor.__exit__(None, None, None)
            clock.return_value = 999
            monitor._check()
            self.assertEqual(record.call_count, 4)

    def test_cancel_has_its_own_short_deadline_after_stall_budget_exhausted(self):
        cancel = Event()
        with (
            patch("vntts.generation_stall.monotonic", return_value=0) as clock,
            patch("vntts.generation_stall.record_generation_stall") as record,
        ):
            monitor = GenerationStallWatchdog(cancel)
            for now in (120, 150, 180):
                clock.return_value = now
                monitor._check()
            self.assertEqual(record.call_count, 3)
            monitor.progress()
            cancel.set()
            clock.return_value = 181
            monitor._check()
            self.assertEqual(record.call_count, 3)
            clock.return_value = 191
            monitor._check()
            self.assertEqual(record.call_count, 4)
            self.assertTrue(record.call_args.kwargs["cancellation_requested"])
            self.assertEqual(record.call_args.kwargs["worker_thread_id"], get_ident())

    def test_malformed_diagnostic_identity_does_not_interrupt_generation(self):
        with (
            patch("vntts.generation_stall.monotonic", return_value=0) as clock,
            patch("vntts.generation_stall.record_generation_stall") as record,
        ):
            monitor = GenerationStallWatchdog()
            monitor.progress({"active": {"queue_id": "\ud800"}})
            clock.return_value = 120
            monitor._check()
            record.assert_called_once()
            self.assertEqual(record.call_args.kwargs["phase"], "starting")

    def test_capture_failure_does_not_leak_watchdog_thread(self):
        failed = Event()

        def reject(**_details):
            failed.set()
            raise OSError("diagnostics folder unavailable")

        with (
            patch.object(GenerationStallWatchdog, "STALL_SECONDS", 0),
            patch.object(GenerationStallWatchdog, "POLL_SECONDS", 0.005),
            patch("vntts.generation_stall.record_generation_stall", side_effect=reject),
        ):
            with GenerationStallWatchdog() as monitor:
                self.assertTrue(failed.wait(2))
        self.assertFalse(monitor._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
