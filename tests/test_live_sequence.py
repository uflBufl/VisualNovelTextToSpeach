import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.story_index import write_story_index

from vntts.live_sequence import (
    LIVE_SEQUENCE_SCHEMA,
    LIVE_SEQUENCE_SCHEMA_VERSION,
    LiveSequencePlanError,
    StoryCursor,
    StoryCursorError,
    StoryCursorState,
    load_live_sequence_plan,
    write_live_sequence_plan,
)


def write_story(path):
    write_story_index(
        path,
        {"game": "Synthetic", "language": "en"},
        [
            {
                "record_type": "line",
                "line_id": "synthetic:chapter-1:1",
                "chapter": "chapter-1",
                "sequence": 1,
                "speaker": "Ada",
                "text": "First line.",
                "kind": "dialogue",
                "source_audio_status": "absent",
            },
            {
                "record_type": "line",
                "line_id": "synthetic:chapter-1:3",
                "chapter": "chapter-1",
                "sequence": 3,
                "speaker": "Bea",
                "text": "Third line after one silent box.",
                "kind": "dialogue",
                "source_audio_status": "absent",
            },
        ],
    )


def plan_input():
    return {
        "game_id": "synthetic",
        "producer": {"name": "synthetic-extractor", "version": "1.0"},
        "source_extract_sha256": "1" * 64,
        "chapters": [
            {
                "chapter": "chapter-1",
                "entry_event_ids": ["event-1"],
                "events": [
                    {
                        "event_id": "event-1",
                        "sequence": 1,
                        "kind": "speech",
                        "line_id": "synthetic:chapter-1:1",
                        "control": "automatic",
                        "successors": ["event-2"],
                    },
                    {
                        "event_id": "event-2",
                        "sequence": 2,
                        "kind": "silent",
                        "control": "automatic",
                        "successors": ["event-transition"],
                    },
                    {
                        "event_id": "event-transition",
                        "sequence": 2,
                        "kind": "transition",
                        "control": "passive",
                        "successors": ["event-3"],
                    },
                    {
                        "event_id": "event-3",
                        "sequence": 3,
                        "kind": "speech",
                        "line_id": "synthetic:chapter-1:3",
                        "control": "terminal",
                        "successors": [],
                    },
                ],
            }
        ],
    }


class LiveSequencePlanTest(unittest.TestCase):
    def create_plan(self, root):
        story = root / "story-index.jsonl"
        plan_path = root / "live-sequence-plan.json"
        write_story(story)
        plan = write_live_sequence_plan(plan_path, plan_input(), story)
        return story, plan_path, plan

    def test_writer_binds_plan_to_exact_story_bytes_and_loads_silent_events(self):
        with TemporaryDirectory() as directory:
            story, plan_path, plan = self.create_plan(Path(directory))
            document = json.loads(plan_path.read_text(encoding="utf-8"))

        self.assertEqual(document["schema"], LIVE_SEQUENCE_SCHEMA)
        self.assertEqual(document["schema_version"], LIVE_SEQUENCE_SCHEMA_VERSION)
        self.assertEqual(plan.story_index_path, story.resolve())
        self.assertEqual(plan.events["event-2"].kind, "silent")
        self.assertEqual(
            plan.event_for_line("synthetic:chapter-1:3").event_id,
            "event-3",
        )

    def test_changed_story_bytes_invalidate_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, plan_path, _plan = self.create_plan(root)
            story.write_text(story.read_text(encoding="utf-8") + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                LiveSequencePlanError, "different story-index bytes"
            ):
                load_live_sequence_plan(plan_path, story)

    def test_speech_event_must_bind_an_existing_line_in_the_same_chapter(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            write_story(story)
            document = plan_input()
            document["chapters"][0]["events"][0]["line_id"] = "missing"

            output = root / "plan.json"
            with self.assertRaisesRegex(LiveSequencePlanError, "unknown story line"):
                write_live_sequence_plan(output, document, story)

            self.assertFalse(output.exists())

    def test_dangling_successor_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            write_story(story)
            document = plan_input()
            document["chapters"][0]["events"][1]["successors"] = ["missing"]

            with self.assertRaisesRegex(LiveSequencePlanError, "missing successor"):
                write_live_sequence_plan(root / "plan.json", document, story)

    def test_unguarded_automatic_cycle_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            write_story(story)
            document = plan_input()
            transition = document["chapters"][0]["events"][2]
            transition["kind"] = "silent"
            transition["control"] = "automatic"
            final = document["chapters"][0]["events"][3]
            final["control"] = "automatic"
            final["successors"] = ["event-1"]

            with self.assertRaisesRegex(LiveSequencePlanError, "automatic cycle"):
                write_live_sequence_plan(root / "plan.json", document, story)

    def test_choice_requires_manual_control(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            write_story(story)
            document = plan_input()
            middle = document["chapters"][0]["events"][1]
            middle["kind"] = "choice"
            middle["control"] = "automatic"

            with self.assertRaisesRegex(
                LiveSequencePlanError, "choice requires manual control"
            ):
                write_live_sequence_plan(root / "plan.json", document, story)


class StoryCursorTest(unittest.TestCase):
    def create_cursor(self):
        temporary = TemporaryDirectory()
        root = Path(temporary.name)
        story = root / "story-index.jsonl"
        write_story(story)
        plan = write_live_sequence_plan(root / "plan.json", plan_input(), story)
        return temporary, StoryCursor(plan)

    def test_playback_and_one_confirmed_transition_are_explicit_states(self):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)

        cursor.anchor_line("synthetic:chapter-1:1")
        self.assertEqual(cursor.state, StoryCursorState.LOCKED)
        self.assertTrue(cursor.can_auto_advance)

        cursor.begin_playback()
        self.assertEqual(cursor.state, StoryCursorState.PLAYING)
        cursor.finish_playback()
        cursor.dispatch_advance()
        self.assertEqual(cursor.state, StoryCursorState.WAITING_TRANSITION)

        cursor.confirm_transition("event-2")
        self.assertEqual(cursor.state, StoryCursorState.LOCKED)
        self.assertEqual(cursor.current_event_id, "event-2")

    def test_unexpected_transition_fails_closed(self):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)
        cursor.anchor_event("event-1")
        cursor.dispatch_advance()

        snapshot = cursor.confirm_transition("event-3")

        self.assertEqual(snapshot.state, StoryCursorState.DESYNCHRONIZED)
        self.assertFalse(cursor.can_auto_advance)
        self.assertIn("unexpected-transition", snapshot.reason)

    def test_visual_transition_requires_successful_playback_and_stops_on_silent_box(
        self,
    ):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)
        cursor.anchor_event("event-1")

        self.assertIsNone(cursor.confirm_visual_transition())
        cursor.begin_playback()
        cursor.finish_playback()

        candidate = cursor.confirm_visual_transition()

        self.assertEqual(candidate.event_id, "event-2")
        self.assertEqual(cursor.current_event_id, "event-2")
        self.assertTrue(cursor.can_confirm_visual_transition)

    def test_visual_transition_skips_one_deterministic_passive_path(self):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)
        cursor.anchor_event("event-2", "visual-transition-confirmed")

        candidate = cursor.confirm_visual_transition()

        self.assertEqual(candidate.event_id, "event-3")
        self.assertEqual(cursor.current_event_id, "event-3")

    def test_failed_playback_never_opens_visual_transition(self):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)
        cursor.anchor_event("event-1")
        cursor.begin_playback()
        cursor.finish_playback(successful=False)

        self.assertFalse(cursor.can_confirm_visual_transition)
        self.assertIsNone(cursor.confirm_visual_transition())
        self.assertEqual(cursor.current_event_id, "event-1")

    def test_shadow_observations_follow_an_explicit_silent_successor_chain(self):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)

        first = cursor.observe_line("synthetic:chapter-1:1")
        repeated = cursor.observe_line("synthetic:chapter-1:1")
        successor = cursor.observe_line("synthetic:chapter-1:3")

        self.assertEqual(first.current_event_id, "event-1")
        self.assertEqual(repeated.reason, "observation-current-event")
        self.assertEqual(successor.state, StoryCursorState.LOCKED)
        self.assertEqual(successor.reason, "observation-successor-chain")

    def test_shadow_observation_of_unplanned_line_fails_closed(self):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)
        cursor.observe_line("synthetic:chapter-1:1")

        snapshot = cursor.observe_line("not-in-plan")

        self.assertEqual(snapshot.state, StoryCursorState.DESYNCHRONIZED)

    def test_bounded_visible_successors_cross_only_explicit_graph_edges(self):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)
        cursor.anchor_event("event-1")

        candidates = cursor.bounded_visible_successors()

        self.assertEqual(
            [candidate.event_id for candidate in candidates],
            ["event-2", "event-3"],
        )

    def test_bounded_observation_can_recover_a_declared_lookahead_line(self):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)
        cursor.anchor_event("event-1")
        cursor.desynchronize("test-recovery")

        snapshot = cursor.observe_bounded_line(
            "synthetic:chapter-1:3",
            ("event-2", "event-3"),
        )

        self.assertEqual(snapshot.current_event_id, "event-3")
        self.assertEqual(snapshot.state, StoryCursorState.LOCKED)
        self.assertEqual(snapshot.reason, "observation-bounded-lookahead")

    def test_shadow_observation_cannot_implicitly_recover_after_desync(self):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)
        cursor.observe_line("synthetic:chapter-1:1")
        failed = cursor.observe_line("not-in-plan")

        repeated = cursor.observe_line("synthetic:chapter-1:1")
        successor = cursor.observe_line("synthetic:chapter-1:3")

        self.assertEqual(repeated, failed)
        self.assertEqual(successor, failed)
        self.assertEqual(cursor.state, StoryCursorState.DESYNCHRONIZED)

    def test_manual_and_silent_events_cannot_start_speech_playback(self):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)
        cursor.anchor_event("event-2")

        with self.assertRaisesRegex(StoryCursorError, "not speakable"):
            cursor.begin_playback()

    def test_passive_transition_never_becomes_auto_advanceable(self):
        temporary, cursor = self.create_cursor()
        self.addCleanup(temporary.cleanup)
        cursor.anchor_event("event-transition")

        self.assertFalse(cursor.can_auto_advance)
        snapshot = cursor.confirm_passive_transition("event-3")

        self.assertEqual(snapshot.current_event_id, "event-3")
        self.assertEqual(snapshot.reason, "passive-transition-confirmed")


if __name__ == "__main__":
    unittest.main()
