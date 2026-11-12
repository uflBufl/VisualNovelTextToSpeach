import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

from vntts.support import GenerationTimelineLog


class TimelinePersistenceTest(unittest.TestCase):
    def wait_for_failure(self, log):
        with log._persistence_condition:
            self.assertTrue(
                log._persistence_condition.wait_for(
                    lambda: log.persistence_status()["error_type"] is not None,
                    timeout=1,
                )
            )

    def test_blocked_write_keeps_record_report_and_acquisition_stop_responsive(self):
        entered, release = Event(), Event()
        recorded, reported = Event(), Event()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            log = GenerationTimelineLog(path=root / "snapshot.json")
            log.start_acquisition(root / "events.jsonl")
            original = log._write_snapshot

            def blocked(report):
                entered.set()
                self.assertTrue(release.wait(2))
                original(report)

            producer = Thread(
                target=lambda: (
                    log.record("capture", 1, 2.0, text="PRIVATE dialogue"),
                    recorded.set(),
                )
            )
            reader = Thread(target=lambda: (log.report(), reported.set()))
            try:
                with patch.object(log, "_write_snapshot", side_effect=blocked):
                    log.record("capture", 1, 1.0)
                    self.assertTrue(entered.wait(1))
                    producer.start()
                    reader.start()
                    self.assertTrue(recorded.wait(1))
                    self.assertTrue(reported.wait(1))
                    self.assertTrue(log.persistence_status()["pending"])
                    self.assertTrue(log.stop_acquisition()["file_closed"])
                    release.set()
                    status = log.close()
                    self.assertFalse(status["writer_running"])
                    self.assertFalse(status["pending"])
                    self.assertNotIn("PRIVATE", (root / "snapshot.json").read_text())
            finally:
                release.set()
                log.stop_acquisition()
                log.close()
                if producer.ident is not None:
                    producer.join(1)
                if reader.ident is not None:
                    reader.join(1)

    def test_blocked_snapshot_coalesces_records_to_one_latest_write(self):
        entered, release = Event(), Event()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            log = GenerationTimelineLog(path=path)
            original = log._write_snapshot
            writes = []

            def blocked(report):
                writes.append(report)
                if len(writes) == 1:
                    entered.set()
                    self.assertTrue(release.wait(2))
                original(report)

            try:
                with patch.object(log, "_write_snapshot", side_effect=blocked):
                    log.record("capture", 1, 1.0)
                    self.assertTrue(entered.wait(1))
                    writer = log._persistence_thread
                    for index in range(2, 1002):
                        log.record("capture", index, float(index))
                    self.assertIs(log._persistence_thread, writer)
                    release.set()
                    self.assertFalse(log.flush()["pending"])
                    self.assertEqual(len(writes), 2)
                    self.assertEqual(writes[0]["retention"]["recorded_events"], 1)
                    self.assertEqual(writes[1]["retention"]["recorded_events"], 1001)
                    self.assertEqual(json.loads(path.read_text()), log.report())
                    self.assertLessEqual(
                        log.report()["retention"]["retained_timelines"], 200
                    )
            finally:
                release.set()
                log.close()
            self.assertFalse(log.persistence_status()["writer_running"])

    def test_report_formatting_runs_without_holding_timeline_lock(self):
        entered, release, recorded = Event(), Event(), Event()
        with TemporaryDirectory() as directory:
            log = GenerationTimelineLog(path=Path(directory) / "snapshot.json")
            original = log._format_report
            first = True

            def blocked(*state):
                nonlocal first
                if first:
                    first = False
                    entered.set()
                    self.assertTrue(release.wait(2))
                return original(*state)

            producer = Thread(
                target=lambda: (log.record("capture", 1, 2.0), recorded.set())
            )
            try:
                with patch.object(log, "_format_report", side_effect=blocked):
                    log.record("capture", 1, 1.0)
                    self.assertTrue(entered.wait(1))
                    producer.start()
                    self.assertTrue(recorded.wait(1))
                    self.assertEqual(log.retention_summary()["recorded_events"], 2)
                    release.set()
                    self.assertFalse(log.flush()["pending"])
            finally:
                release.set()
                log.close()
                if producer.ident is not None:
                    producer.join(1)

    def test_failed_write_waits_for_new_record_or_explicit_flush(self):
        for retry in ("record", "flush"):
            with self.subTest(retry=retry), TemporaryDirectory() as directory:
                path = Path(directory) / "snapshot.json"
                path.write_text('{"previous": true}')
                log = GenerationTimelineLog(path=path)
                original = log._write_snapshot
                succeed = Event()
                calls = []

                def fail_until_retried(report):
                    calls.append(report)
                    if not succeed.is_set():
                        raise OSError("write failed")
                    original(report)

                try:
                    with patch.object(
                        log, "_write_snapshot", side_effect=fail_until_retried
                    ):
                        log.record("capture", 1, 1.0)
                        self.wait_for_failure(log)
                        self.assertEqual(
                            json.loads(path.read_text()), {"previous": True}
                        )
                        status = log.persistence_status()
                        self.assertTrue(status["pending"])
                        self.assertTrue(status["writer_running"])
                        self.assertEqual(status["error_type"], "OSError")
                        with log.lock:
                            self.assertFalse(log._persistence_requested)
                            self.assertIsNone(log._writing_sequence)
                            self.assertEqual(len(calls), 1)
                        succeed.set()
                        if retry == "record":
                            log.record("capture", 1, 2.0)
                        status = log.flush()
                        self.assertFalse(status["pending"])
                        self.assertIsNone(status["error_type"])
                        self.assertEqual(json.loads(path.read_text()), log.report())
                finally:
                    succeed.set()
                    log.close()
                self.assertFalse(log.persistence_status()["writer_running"])

    def test_close_reports_blocked_then_failed_write_and_never_restarts(self):
        entered, release = Event(), Event()
        with TemporaryDirectory() as directory:
            log = GenerationTimelineLog(path=Path(directory) / "snapshot.json")
            calls = []

            def blocked_failure(report):
                calls.append(report)
                entered.set()
                self.assertTrue(release.wait(2))
                raise OSError("write failed")

            try:
                with patch.object(log, "_write_snapshot", side_effect=blocked_failure):
                    log.record("capture", 1, 1.0)
                    self.assertTrue(entered.wait(1))
                    status = log.close(timeout=0)
                    self.assertTrue(status["closed"])
                    self.assertTrue(status["pending"])
                    self.assertTrue(status["writer_running"])
                    release.set()
                    status = log.close()
                    self.assertFalse(status["writer_running"])
                    self.assertTrue(status["pending"])
                    self.assertEqual(status["error_type"], "OSError")
                    writer = log._persistence_thread
                    log.record("capture", 1, 2.0)
                    self.assertIs(log._persistence_thread, writer)
                    self.assertEqual(log.retention_summary()["recorded_events"], 2)
                    self.assertEqual(len(calls), 1)
                    self.assertFalse(log.flush()["writer_running"])
            finally:
                release.set()
                log.close()

    def test_closed_successful_owner_keeps_late_records_in_memory(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            log = GenerationTimelineLog(path=path)
            try:
                log.record("capture", 1, 1.0)
                self.assertFalse(log.close()["pending"])
                saved = path.read_bytes()
                log.record("capture", 1, 2.0)
                self.assertEqual(log.retention_summary()["recorded_events"], 2)
                self.assertEqual(path.read_bytes(), saved)
                self.assertFalse(log.persistence_status()["writer_running"])
            finally:
                log.close()

    def test_no_path_or_no_records_starts_no_writer(self):
        for path in (None, Path("unused-snapshot.json")):
            with self.subTest(path=path):
                log = GenerationTimelineLog(path=path)
                with patch("vntts.support.threading.Thread") as thread:
                    if path is None:
                        log.record("capture", 1, 1.0)
                    self.assertFalse(log.flush()["pending"])
                    status = log.close()
                    self.assertFalse(status["writer_running"])
                    self.assertFalse(status["pending"])
                    self.assertEqual(status["enabled"], path is not None)
                    thread.assert_not_called()

    def test_unsupported_timeout_is_rejected_without_starting_writer(self):
        log = GenerationTimelineLog()
        for timeout in (-1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    log.flush(timeout)
                with self.assertRaises(ValueError):
                    log.close(timeout)
        self.assertIsNone(log._persistence_thread)
