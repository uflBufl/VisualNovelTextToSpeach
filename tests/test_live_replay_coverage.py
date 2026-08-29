import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.live_sequence import write_live_sequence_plan

from vntts.live_replay_coverage import audit_live_replay_coverage


class LiveReplayCoverageTest(unittest.TestCase):
    def test_unions_checksum_bound_sealed_segments(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story.jsonl"
            lines = [
                {
                    "record_type": "line",
                    "kind": "dialogue",
                    "chapter": "1",
                    "sequence": sequence,
                    "line_id": f"story:{sequence}",
                    "speaker": speaker,
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
                for sequence, speaker, text in (
                    (1, "Ada", "First line."),
                    (3, "Bea", "Last line."),
                )
            ]
            story.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in (
                        {
                            "record_type": "metadata",
                            "schema": "vntts.story-index",
                            "schema_version": 1,
                            "line_count": 2,
                        },
                        *lines,
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            plan = root / "plan.json"
            write_live_sequence_plan(
                plan,
                {
                    "game_id": "coverage-test",
                    "producer": {"name": "tests", "version": "1"},
                    "source_extract_sha256": hashlib.sha256(b"fixture").hexdigest(),
                    "chapters": [
                        {
                            "chapter": "1",
                            "entry_event_ids": ["event-1"],
                            "events": [
                                {
                                    "event_id": "event-1",
                                    "sequence": 1,
                                    "kind": "speech",
                                    "control": "automatic",
                                    "successors": ["event-2"],
                                    "line_id": "story:1",
                                },
                                {
                                    "event_id": "event-2",
                                    "sequence": 2,
                                    "kind": "silent",
                                    "control": "automatic",
                                    "successors": ["event-3"],
                                },
                                {
                                    "event_id": "event-3",
                                    "sequence": 3,
                                    "kind": "speech",
                                    "control": "terminal",
                                    "successors": [],
                                    "line_id": "story:3",
                                },
                            ],
                        }
                    ],
                },
                story,
            )
            story_sha = hashlib.sha256(story.read_bytes()).hexdigest()
            plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()

            def review(name, event_ids, accepted):
                path = root / name
                path.write_text(
                    json.dumps(
                        {
                            "schema": "vntts.sequence-replay-seal-review",
                            "schema_version": 1,
                            "sealed_replay_successful": True,
                            "human_acceptance_recorded": accepted,
                            "authority": {
                                "story_index_sha256": story_sha,
                                "sequence_plan_sha256": plan_sha,
                            },
                            "mappings": [
                                {
                                    "event_id": event_id,
                                    "event_kind": {
                                        "event-1": "speech",
                                        "event-2": "silent",
                                        "event-3": "speech",
                                    }[event_id],
                                    "line_id": {
                                        "event-1": "story:1",
                                        "event-2": None,
                                        "event-3": "story:3",
                                    }[event_id],
                                    "mapping_method": (
                                        "unique-silent-frontier"
                                        if event_id == "event-2"
                                        else "exact-line-id"
                                    ),
                                }
                                for event_id in event_ids
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return path

            first = review("first.json", ["event-1"], True)
            second = review("second.json", ["event-2", "event-3"], False)

            path, report = audit_live_replay_coverage(
                root / "coverage.json",
                story_index=story,
                sequence_plan=plan,
                reviews=(first, second),
            )

            self.assertTrue(path.is_file())
            self.assertTrue(report["technical_coverage_complete"])
            self.assertFalse(report["human_acceptance_complete"])
            self.assertEqual(report["expected_visible_event_count"], 3)
            self.assertEqual(report["covered_visible_event_count"], 3)
            self.assertEqual(
                report["human_acceptance_pending_event_ids"],
                ["event-2"],
            )


if __name__ == "__main__":
    unittest.main()
