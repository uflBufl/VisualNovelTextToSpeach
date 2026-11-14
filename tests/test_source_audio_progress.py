import json
import unittest

from vntts.source_audio_progress import SourceAudioProgress, parse_source_audio_progress


class SourceAudioProgressTest(unittest.TestCase):
    def test_accepts_valid_counts_and_rejects_invalid_or_partial_snapshots(self):
        document = {
            "schema": "r1999.source-audio-progress",
            "schema_version": 1,
            "stage": "semantics",
            "processed": 3,
            "total": 8,
            "state": "analyzing",
        }
        self.assertEqual(
            parse_source_audio_progress(json.dumps(document)),
            SourceAudioProgress("semantics", 3, 8, "analyzing"),
        )
        self.assertEqual(
            parse_source_audio_progress(
                json.dumps(document | {"stage": "portraits", "state": "publishing"})
            ),
            SourceAudioProgress("portraits", 3, 8, "publishing"),
        )
        for changed in (
            {"schema_version": True},
            {"schema_version": 2},
            {"processed": True},
            {"processed": -1},
            {"processed": 9},
            {"total": 2**31},
            {"stage": "unknown"},
            {"state": "publishing"},
        ):
            with self.subTest(changed=changed):
                self.assertIsNone(
                    parse_source_audio_progress(json.dumps(document | changed))
                )
        for payload in ('{"processed":', "[]", "\ud800", " " * 4097):
            with self.subTest(payload=repr(payload)):
                self.assertIsNone(parse_source_audio_progress(payload))
