import unittest
from dataclasses import replace

from vntts.live_tracking import IncrementalDialogTracker, SpeechChunk


class IdentifiedDialogTransitionsTest(unittest.TestCase):
    def test_exact_line_accepts_one_frame_and_skipped_same_speaker_line(self):
        tracker = IncrementalDialogTracker(stability_frames=4)

        first = tracker.observe_identified("Ada", "The first line.", "A", exact=True)
        third = tracker.observe_identified("Ada", "The third line.", "C", exact=True)

        self.assertEqual(
            [(c.generation, c.line_id) for c in first + third], [(1, "A"), (2, "C")]
        )
        self.assertEqual(
            tracker.observe_identified("Ada", "The third line.", "C", exact=True), []
        )
        self.assertEqual(
            tracker.observe_identified("Ada", "The third line.", "C", exact=False), []
        )
        self.assertEqual(tracker.generation, 2)

    def test_stale_preceding_canonical_frame_does_not_reopen_old_occurrence(self):
        tracker = IncrementalDialogTracker()
        tracker.observe_identified("Ada", "First.", "A", exact=True)
        tracker.observe_identified("Ada", "Third.", "C", exact=True)

        self.assertEqual(
            tracker.observe_identified("Ada", "First.", "A", exact=True), []
        )
        self.assertEqual(tracker.canonical_line_id, "C")
        self.assertEqual(tracker.generation, 2)

    def test_identical_text_with_different_identity_is_another_occurrence(self):
        tracker = IncrementalDialogTracker()

        first = tracker.observe_identified("Ada", "Yes.", "A", exact=True)
        second = tracker.observe_identified("Ada", "Yes.", "B", exact=True)

        self.assertEqual([c.text for c in first + second], ["Yes.", "Yes."])
        self.assertNotEqual(first[0].generation, second[0].generation)

    def test_approximate_identity_requires_consecutive_stable_observations(self):
        tracker = IncrementalDialogTracker()
        tracker.observe_identified("Ada", "First.", "A", exact=True)

        self.assertEqual(
            tracker.observe_identified("Ada", "Second.", "B", exact=False), []
        )
        self.assertEqual(
            tracker.observe_identified("Ada", "Third.", "C", exact=False), []
        )
        chunks = tracker.observe_identified("Ada", "Third.", "C", exact=False)

        self.assertEqual([(c.text, c.line_id) for c in chunks], [("Third.", "C")])
        self.assertEqual(tracker.generation, 2)

    def test_early_expanded_prefix_binds_identity_without_speaking_again(self):
        canonical = "These old ones are enough to carry everyone."
        tracker = IncrementalDialogTracker(
            complete_dialogue_only=True,
            early_dialogue_resolver=lambda *_: canonical,
        )
        tracker.observe("Ada", "These old ones are enough")
        chunks = tracker.observe("Ada", "These old ones are enough to")
        self.assertEqual([c.text for c in chunks], [canonical])

        self.assertEqual(
            tracker.observe_identified("Ada", canonical, "A", exact=True), []
        )
        self.assertEqual(tracker.canonical_line_id, "A")
        self.assertEqual(tracker.generation, 1)
        self.assertEqual(tracker.observe("Ada", canonical + " OCR junk"), [])
        self.assertEqual(tracker.flush(), [])

    def test_raw_full_text_binds_canonical_correction_without_replay(self):
        tracker = IncrementalDialogTracker()
        tracker.observe("Ada", "Hello worldd.")
        tracker.observe("Ada", "Hello worldd.")

        self.assertEqual(
            tracker.observe_identified("Ada", "Hello world.", "A", exact=True), []
        )
        self.assertEqual(tracker.canonical_line_id, "A")
        self.assertEqual(tracker.committed_text, "Hello world.")
        self.assertEqual(tracker.generation, 1)

    def test_binding_canonical_continuation_emits_only_unspoken_suffix(self):
        tracker = IncrementalDialogTracker()
        tracker.observe("Ada", "Hello. More")
        tracker.observe("Ada", "Hello. More")

        chunks = tracker.observe_identified(
            "Ada", "Hello. More follows.", "A", exact=True
        )

        self.assertEqual(
            [(c.text, c.line_id) for c in chunks], [("More follows.", "A")]
        )
        self.assertEqual(tracker.generation, 1)
        self.assertEqual(tracker.flush(), [])

    def test_distinct_raw_text_moves_forward_after_stability_without_key(self):
        for canonical in (False, True):
            with self.subTest(canonical=canonical):
                tracker = IncrementalDialogTracker()
                if canonical:
                    tracker.observe_identified(
                        "Ada", "First dialogue is complete.", "A", exact=True
                    )
                else:
                    tracker.observe("Ada", "First dialogue is complete.")
                    tracker.observe("Ada", "First dialogue is complete.")

                self.assertEqual(
                    tracker.observe("Ada", "Zebras wander beyond mountains."), []
                )
                chunks = tracker.observe("Ada", "Zebras wander beyond mountains.")

                self.assertEqual(
                    [c.text for c in chunks], ["Zebras wander beyond mountains."]
                )
                self.assertEqual(tracker.generation, 2)

    def test_small_raw_corrections_and_shrinking_prefixes_remain_blocked(self):
        for original, correction in (
            ("Hello worldd.", "Hello world."),
            ("Yes, okay.", "Yes, please."),
            ("Already spoken dialogue.", "Already spoken"),
        ):
            with self.subTest(correction=correction):
                tracker = IncrementalDialogTracker()
                tracker.observe("Ada", original)
                tracker.observe("Ada", original)

                self.assertEqual(tracker.observe("Ada", correction), [])
                self.assertEqual(tracker.observe("Ada", correction), [])
                self.assertEqual(tracker.flush(), [])
                self.assertEqual(tracker.generation, 1)

    def test_sequence_identity_does_not_change_chunk_identity_or_equality(self):
        chunk = SpeechChunk(1, "Ada", "Hello.", ordinal=1, explicit_replay=True)
        leased = replace(chunk, sequence_event_id="event", sequence_occurrence_id=2)

        self.assertEqual(chunk, leased)
        self.assertEqual(chunk.chunk_id, leased.chunk_id)
        self.assertTrue(leased.explicit_replay)


if __name__ == "__main__":
    unittest.main()
