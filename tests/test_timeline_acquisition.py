import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch
from uuid import uuid4

from vntts.support import GenerationTimelineLog


def read_journal(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class TimelineAcquisitionTest(unittest.TestCase):
    def test_opt_out_and_early_evidence_survives_memory_eviction(self):
        log = GenerationTimelineLog(maximum_entries=1)
        self.assertNotIn("acquisition", log.report())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.jsonl"
            log.start_acquisition(path)
            for generation in range(1, 2002):
                log.record("capture", generation, float(generation))
            status = log.stop_acquisition()
            records = read_journal(path)
        self.assertFalse(status["writer_running"])
        self.assertEqual(log.snapshot()[0]["generation"], 2001)
        self.assertEqual(records[0]["kind"], "start")
        self.assertIs(records[0]["complete"], False)
        events = [entry for entry in records if entry["kind"] == "event"]
        self.assertEqual(events[0]["event_sequence"], 1)
        self.assertEqual(events[0]["generation"], 1)
        self.assertEqual(records[-1]["kind"], "stop")
        self.assertEqual(records[-1]["written_events"], len(events))
        self.assertEqual(status["accepted_events"] + status["dropped_events"], 2001)
        self.assertEqual(status["written_events"], status["accepted_events"])
        self.assertFalse(records[-1]["complete"])
        self.assertEqual(records[-1]["status"], "cutoff-written")
        self.assertTrue(status["file_closed"])

    def test_restart_requires_new_file_and_keeps_identity_and_privacy(self):
        session, run, playback, capture = [uuid4().hex for _ in range(4)]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "first.jsonl"
            log = GenerationTimelineLog()
            first = log.start_acquisition(path)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                log.start_acquisition(Path(directory) / "duplicate.jsonl")
            log.record(
                "route-decision",
                1,
                10.5,
                session_id=session,
                run_id=run,
                playback_id=playback,
                capture_id=capture,
                line_id="reverse1999:47",
                occurrence_id=7,
                text="PRIVATE dialogue",
                pixels=b"PRIVATE pixels",
                reason="PRIVATE reason",
                window_title="PRIVATE title",
            )
            log.stop_acquisition()
            original = path.read_bytes()
            restarted = GenerationTimelineLog()
            with self.assertRaises(FileExistsError):
                restarted.start_acquisition(path)
            self.assertEqual(path.read_bytes(), original)
            next_path = Path(directory) / "second.jsonl"
            second = restarted.start_acquisition(next_path)
            restarted.record("capture", 1, 20.0, session_id=session, run_id=uuid4().hex)
            restarted.stop_acquisition()
            event = read_journal(path)[1]
        self.assertNotEqual(first["acquisition_id"], second["acquisition_id"])
        self.assertNotIn(b"PRIVATE", original)
        self.assertEqual(event["session_id"], session)
        self.assertEqual(event["run_id"], run)
        self.assertEqual(event["playback_id"], playback)
        self.assertEqual(event["capture_id"], capture)
        self.assertEqual(event["occurrence_id"], 7)
        self.assertEqual(event["clock_origin"], 10.5)

    def test_event_and_byte_caps_preserve_prefix_and_report_drops(self):
        for limits in ({"maximum_events": 2}, {"maximum_bytes": 4096}):
            with self.subTest(limits=limits), TemporaryDirectory() as directory:
                path = Path(directory) / "journal.jsonl"
                log = GenerationTimelineLog()
                log.start_acquisition(path, **limits)
                for generation in range(1, 50):
                    log.record("capture", generation, float(generation))
                status = log.stop_acquisition()
                records = read_journal(path)
                self.assertLessEqual(
                    path.stat().st_size, limits.get("maximum_bytes", 8 * 1024 * 1024)
                )
            self.assertTrue(status["capped"])
            self.assertGreater(status["dropped_events"], 0)
            self.assertEqual(records[1]["event_sequence"], 1)
            self.assertFalse(records[-1]["complete"])
            if "maximum_events" in limits:
                self.assertEqual(status["written_events"], 2)

    def test_blocked_writer_never_blocks_record_or_bounded_stop(self):
        entered, release = Event(), Event()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            log = GenerationTimelineLog()
            log.start_acquisition(path)
            acquisition = log.acquisition
            original_write = acquisition._write

            def blocked(payload):
                entered.set()
                self.assertTrue(release.wait(5))
                original_write(payload)

            try:
                with patch.object(acquisition, "_write", side_effect=blocked):
                    log.record("capture", 1, 1.0)
                    self.assertTrue(entered.wait(1))
                    producer = Thread(
                        target=lambda: [
                            log.record("capture", 1, 2.0) for _ in range(130)
                        ]
                    )
                    producer.start()
                    producer.join(1)
                    self.assertFalse(producer.is_alive())
                    before = time.monotonic()
                    status = log.stop_acquisition(timeout=0.01)
                    self.assertLess(time.monotonic() - before, 0.5)
                    self.assertTrue(status["writer_running"])
                    self.assertGreater(status["pending_events"], 0)
                    self.assertGreater(status["dropped_events"], 0)
                    self.assertEqual(read_journal(path)[-1]["kind"], "start")
                    release.set()
                    log.stop_acquisition()
            finally:
                release.set()
                log.stop_acquisition()

    def test_stop_does_not_wait_for_existing_timeline_persistence(self):
        entered, release = Event(), Event()
        with TemporaryDirectory() as directory:
            log = GenerationTimelineLog()
            log.start_acquisition(Path(directory) / "journal.jsonl")

            def blocked():
                entered.set()
                self.assertTrue(release.wait(5))

            producer = Thread(target=lambda: log.record("capture", 1, 1.0))
            try:
                with patch.object(log, "_persist_locked", side_effect=blocked):
                    producer.start()
                    self.assertTrue(entered.wait(1))
                    before = time.monotonic()
                    log.stop_acquisition(timeout=0.01)
                    self.assertLess(time.monotonic() - before, 0.5)
                    self.assertTrue(producer.is_alive())
                    release.set()
                    producer.join(1)
                    self.assertFalse(producer.is_alive())
            finally:
                release.set()
                producer.join(1)
                log.stop_acquisition()

    def test_write_failure_stays_incomplete_and_does_not_abort_timeline(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            log = GenerationTimelineLog()
            log.start_acquisition(path)
            with patch.object(
                log.acquisition, "_write", side_effect=OSError("PRIVATE path")
            ):
                self.assertTrue(log.record("capture", 1, 1.0))
                status = log.stop_acquisition()
            self.assertEqual(status["error_type"], "OSError")
            self.assertFalse(status["writer_running"])
            self.assertEqual(status["pending_events"], 1)
            self.assertEqual(read_journal(path)[-1]["kind"], "start")
            self.assertNotIn("PRIVATE", repr(log.report()))
        self.assertEqual(log.snapshot()[0]["generation"], 1)

    def test_close_failure_cannot_make_standalone_file_claim_completion(self):
        from unittest.mock import Mock

        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            log = GenerationTimelineLog()
            log.start_acquisition(path)
            original = log.acquisition.file
            wrapped = Mock(wraps=original)
            wrapped.close.side_effect = OSError("PRIVATE close failure")
            log.acquisition.file = wrapped
            try:
                log.record("capture", 1, 1.0)
                status = log.stop_acquisition()
                final = read_journal(path)[-1]
                self.assertFalse(final["complete"])
                self.assertEqual(final["file_close"], "unobserved")
                self.assertEqual(status["error_type"], "OSError")
                self.assertFalse(status["file_closed"])
                self.assertNotIn("PRIVATE", repr(log.report()))
            finally:
                original.close()

    def test_stop_is_cutoff_and_late_events_are_visible_only_in_live_status(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            log = GenerationTimelineLog()
            log.start_acquisition(path)
            log.record("capture", 1, 1.0)
            log.stop_acquisition()
            stopped = path.read_bytes()
            log.record("capture", 2, 2.0)
            self.assertEqual(path.read_bytes(), stopped)
            self.assertEqual(log.report()["acquisition"]["late_rejected_events"], 1)
            self.assertEqual(
                read_journal(path)[-1]["scope"], "explicit-acquisition-cutoff"
            )

    def test_invalid_caps_cannot_disable_limits(self):
        log = GenerationTimelineLog()
        for limits in (
            {"maximum_events": 0},
            {"maximum_bytes": 4095},
            {"maximum_events": float("nan")},
            {"maximum_bytes": True},
            {"maximum_events": 10**400},
            {"maximum_bytes": 10**400},
        ):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                log.start_acquisition("unused.jsonl", **limits)


class TimelineAcquisitionEntrypointTest(unittest.TestCase):
    def run_main(self, arguments, *, fail_start=False):
        from unittest.mock import Mock

        from vntts import app

        application = Mock(spec=app.QApplication)
        application.exec.return_value = 17
        tray = Mock()
        if fail_start:
            tray.generation_timelines.start_acquisition.side_effect = FileExistsError(
                "existing"
            )
        with (
            patch.object(app, "configure_bundled_dependencies"),
            patch.object(app, "enable_windows_dpi_awareness"),
            patch.object(app.QApplication, "instance", return_value=application),
            patch("vntts.system_audio.initialize_audio_device_monitor"),
            patch.object(app, "TrayApplication", return_value=tray),
        ):
            if fail_start:
                with self.assertRaises(FileExistsError):
                    app.main(arguments)
            else:
                self.assertEqual(app.main(arguments), 17)
        return application, tray

    def test_cli_opt_in_starts_before_gui_and_stops_at_explicit_cutoff(self):
        application, tray = self.run_main(["--timeline-journal", "new.jsonl"])
        tray.generation_timelines.start_acquisition.assert_called_once_with("new.jsonl")
        tray.start.assert_called_once_with()
        application.exec.assert_called_once_with()
        tray.shutdown.assert_called_once_with()
        tray.generation_timelines.stop_acquisition.assert_called_once_with(timeout=0.25)

    def test_cli_defaults_to_no_acquisition(self):
        _, tray = self.run_main([])
        tray.generation_timelines.start_acquisition.assert_not_called()

    def test_acquisition_start_failure_cleans_constructed_application(self):
        application, tray = self.run_main(
            ["--timeline-journal", "existing.jsonl"], fail_start=True
        )
        tray.start.assert_not_called()
        application.exec.assert_not_called()
        tray.shutdown.assert_called_once_with()
        tray.generation_timelines.stop_acquisition.assert_called_once_with(timeout=0.25)

    def test_package_mode_never_starts_journal_or_desktop(self):
        from types import SimpleNamespace

        from vntts import app

        with (
            patch.object(app, "configure_bundled_dependencies"),
            patch.object(
                app, "run_package_self_test", return_value=SimpleNamespace(exit_code=0)
            ),
            patch.object(app, "TrayApplication") as tray,
        ):
            self.assertEqual(
                app.main(["--package-self-test", "--timeline-journal", "unused.jsonl"]),
                0,
            )
        tray.assert_not_called()
