import copy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.mac_live_recording.run_fixture import run
from scripts.mac_live_recording.verify import validate_events


class NativeRecordingEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.metadata = {
            "status": "succeeded",
            "errors": [],
            "window_id": 123,
            "pid": 456,
            "bounds": {"x": 0, "y": 0, "width": 640, "height": 400},
            "mechanism": "ScreenCaptureKit desktopIndependentWindow",
            "requested_seconds": 65,
        }
        self.events = [
            {"kind": "video", "written": True, "pts_s": 100 + second}
            for second in range(65)
        ]
        self.events.append({"kind": "finished", "video_drops": 0})

    def test_long_changing_window_returns_beginning_middle_end(self):
        self.assertEqual(validate_events(self.metadata, self.events), [2, 32, 62])

    def test_cleanup_success_cannot_hide_failure(self):
        for change in (
            {"status": "failed"},
            {"errors": ["Stop failed"]},
            {"status": "in_progress"},
            {"status": "incomplete", "native_status": "succeeded", "errors": []},
            {"stop_requested": True},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_events(self.metadata | change, self.events)
        with self.assertRaises(ValueError):
            validate_events(self.metadata, [{"kind": "error"}] + self.events)

    def test_four_frame_capture_is_rejected_despite_successful_finish(self):
        events = [
            {"kind": "video", "written": True, "pts_s": 100 + n / 30} for n in range(4)
        ]
        events.append({"kind": "finished", "video_drops": 0})
        with self.assertRaisesRegex(ValueError, "ended early"):
            validate_events(self.metadata, events)

    def test_target_provenance_drops_and_middle_gap_are_required(self):
        for change in (
            {"window_id": 0},
            {"pid": None},
            {"bounds": {}},
            {"mechanism": "desktop"},
            {"requested_seconds": 60},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_events(self.metadata | change, self.events)
        events = copy.deepcopy(self.events)
        events[-1]["video_drops"] = 1
        with self.assertRaisesRegex(ValueError, "dropped"):
            validate_events(self.metadata, events)
        with self.assertRaisesRegex(ValueError, "gap"):
            validate_events(self.metadata, self.events[:3] + self.events[8:])

    def test_fixture_cleanup_runs_on_recorder_failure(self):
        fixture = Mock()
        fixture.stdout.readline.return_value = '{"pid": 42, "window_id": 88}'
        with (
            patch(
                "scripts.mac_live_recording.run_fixture.select.select",
                return_value=([fixture.stdout], [], []),
            ),
            patch(
                "scripts.mac_live_recording.run_fixture.subprocess.Popen",
                return_value=fixture,
            ),
            patch(
                "scripts.mac_live_recording.run_fixture.subprocess.run",
                side_effect=[None, RuntimeError("failed")],
            ) as command,
        ):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                run(Path("/recorder"), Path("/fixture"), Path("/output"))
        self.assertEqual(
            command.call_args_list[0].args[0], ["/recorder", "--preflight"]
        )
        self.assertEqual(
            command.call_args.args[0],
            ["/recorder", "--video-only", "42", "88", "65", "/output"],
        )
        fixture.terminate.assert_called_once()
        fixture.wait.assert_called_once_with(timeout=3)

    def test_missing_permission_does_not_launch_fixture(self):
        with (
            patch(
                "scripts.mac_live_recording.run_fixture.subprocess.run",
                side_effect=RuntimeError("no permission"),
            ),
            patch("scripts.mac_live_recording.run_fixture.subprocess.Popen") as spawn,
        ):
            with self.assertRaisesRegex(RuntimeError, "no permission"):
                run(Path("/recorder"), Path("/fixture"), Path("/output"))
        spawn.assert_not_called()
