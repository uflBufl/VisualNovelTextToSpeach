"""Sealing admits timing before reads and reuses it across both replay probes."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import TIMEOUT_MAX
from unittest.mock import patch

import tests.test_live_replay_sequence_seal as seal_tests
import tests.test_replay_configuration as configuration_tests
from vntts.live_replay_sequence_seal import seal_sequence_replay


class ReplaySealConfigurationTest(unittest.TestCase):
    def test_invalid_timing_is_refused_before_capture_acquisition(self):
        with patch(
            "vntts.live_replay_sequence_seal._read_regular_file",
            side_effect=AssertionError("capture read reached"),
        ) as read:
            for field, name, values in (
                ("interval_seconds", "interval", (0, 1.0, float("nan"), 10**400)),
                (
                    "timeout_seconds",
                    "timeout",
                    (0, TIMEOUT_MAX + 1, float("inf"), 10**400),
                ),
            ):
                for value in values:
                    with self.subTest(field=field, value_type=type(value).__name__):
                        with self.assertRaisesRegex(ValueError, "Live replay " + name):
                            seal_sequence_replay(
                                "unused-capture.json",
                                "unused-output",
                                story_index="unused-story.jsonl",
                                sequence_plan="unused-plan.json",
                                **{field: value},
                            )
            read.assert_not_called()

    def test_user_numeric_callbacks_run_once_across_actual_replay_probes(self):
        fixture = seal_tests.LiveReplaySequenceSealTest()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = fixture.write_story(
                root,
                [
                    {
                        "line_id": "story:1",
                        "chapter": "1",
                        "sequence": 1,
                        "speaker": "Ada",
                        "text": "Start.",
                        "source_audio_status": "absent",
                    }
                ],
            )
            plan = fixture.write_plan(
                root,
                story,
                [
                    {
                        "event_id": "speech:1",
                        "sequence": 1,
                        "kind": "speech",
                        "control": "terminal",
                        "successors": [],
                        "line_id": "story:1",
                    }
                ],
            )
            capture, recognize = fixture.capture(root, story, [("Ada", "Start.")])
            interval = configuration_tests.FloatOnce(0.002)
            timeout = configuration_tests.FloatOnce(5.0)
            result = seal_sequence_replay(
                capture.corpus,
                root / "sealed",
                story_index=story,
                sequence_plan=plan,
                recognizer=recognize,
                interval_seconds=interval,
                timeout_seconds=timeout,
            )
            self.assertEqual((interval.calls, timeout.calls), (1, 1))
            self.assertEqual(result.dialogue_count, 1)
            report = json.loads(result.replay_report.read_text(encoding="utf-8"))
            self.assertTrue(report["successful"])
