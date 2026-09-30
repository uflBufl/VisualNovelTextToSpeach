import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.live_sequence import write_live_sequence_plan

from vntts.authoring.authority import write_json_document_no_replace
from vntts.live_replay_coverage import (
    LiveReplayCoverageError,
    audit_live_replay_coverage,
)
from vntts.live_sequence import LiveSequencePlan


class LiveReplayCoverageTest(unittest.TestCase):
    @staticmethod
    def _fixture(root):
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

        return (
            story,
            plan,
            review("first.json", ["event-1"], True),
            review("second.json", ["event-2", "event-3"], False),
        )

    def test_unions_checksum_bound_sealed_segments(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, plan, first, second = self._fixture(root)

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

            original_review = first.read_bytes()
            for version in (True, 1.0):
                with self.subTest(version=version):
                    forged = json.loads(original_review)
                    forged["schema_version"] = version
                    first.write_text(json.dumps(forged), encoding="utf-8")
                    with self.assertRaisesRegex(
                        LiveReplayCoverageError, "not successful sealed replay"
                    ):
                        audit_live_replay_coverage(
                            root / f"invalid-{type(version).__name__}.json",
                            story_index=story,
                            sequence_plan=plan,
                            reviews=(first, second),
                        )
            first.write_bytes(original_review)

            for competitor in ("file", "symlink"):
                with self.subTest(competitor=competitor):
                    destination = root / f"coverage-{competitor}.json"
                    outside = root / f"outside-{competitor}.json"

                    def publish_after_competitor(path, document, label, *, error_type):
                        if competitor == "file":
                            destination.write_bytes(b"concurrent report")
                        else:
                            destination.symlink_to(outside)
                        return write_json_document_no_replace(
                            path, document, label, error_type=error_type
                        )

                    with (
                        patch(
                            "vntts.live_replay_coverage.write_json_document_no_replace",
                            side_effect=publish_after_competitor,
                        ),
                        self.assertRaises(LiveReplayCoverageError),
                    ):
                        audit_live_replay_coverage(
                            destination,
                            story_index=story,
                            sequence_plan=plan,
                            reviews=(first, second),
                        )
                    if competitor == "file":
                        self.assertEqual(destination.read_bytes(), b"concurrent report")
                    else:
                        self.assertTrue(destination.is_symlink())
                        self.assertFalse(outside.exists())

    def test_rejects_malformed_review_with_coverage_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, plan, first, second = self._fixture(root)
            first.write_bytes(b"{")
            with self.assertRaises(LiveReplayCoverageError):
                audit_live_replay_coverage(
                    root / "invalid-json.json",
                    story_index=story,
                    sequence_plan=plan,
                    reviews=(first, second),
                )

    def test_rejects_missing_review_with_coverage_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, plan, _first, _second = self._fixture(root)
            with self.assertRaises(LiveReplayCoverageError):
                audit_live_replay_coverage(
                    root / "missing-review.json",
                    story_index=story,
                    sequence_plan=plan,
                    reviews=(root / "missing-review-input.json",),
                )

    def test_rejects_unhashable_event_kind_with_coverage_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, plan, first, second = self._fixture(root)
            forged = json.loads(first.read_bytes())
            forged["mappings"][0]["event_kind"] = []
            first.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaises(LiveReplayCoverageError):
                audit_live_replay_coverage(
                    root / "invalid-event-kind.json",
                    story_index=story,
                    sequence_plan=plan,
                    reviews=(first, second),
                )

    def test_loads_story_snapshot_if_source_mutates_during_plan_load(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            story, plan, first, second = self._fixture(root)
            original_story = story.read_bytes()
            real_load = LiveSequencePlan.load
            mutated = False

            def load_after_mutation(plan_path, story_path):
                nonlocal mutated
                if not mutated and story_path == story:
                    story.write_bytes(b"mutated after snapshot")
                    mutated = True
                return real_load(plan_path, story_path)

            with patch(
                "vntts.live_replay_coverage.LiveSequencePlan.load",
                side_effect=load_after_mutation,
            ):
                _path, report = audit_live_replay_coverage(
                    root / "mutation-safe.json",
                    story_index=story,
                    sequence_plan=plan,
                    reviews=(first, second),
                )
            self.assertTrue(report["technical_coverage_complete"])
            self.assertFalse(mutated)
            story.write_bytes(original_story)


if __name__ == "__main__":
    unittest.main()
