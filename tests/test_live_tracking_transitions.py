import unittest
from dataclasses import replace

from vntts.chapter_voice_preload import ChapterDialogue
from vntts.live_tracking import IncrementalDialogTracker, SpeechChunk


class IdentifiedDialogTransitionsTest(unittest.TestCase):
    def test_early_identity_survives_noisy_partial_speakers_and_full_confirmation(self):
        text = "These old accounts contradict the promise you just made."
        line = ChapterDialogue("A", "chapter", 1, "Ada", text, None)
        for observed_speaker in ("Ada", "Narrator"):
            with self.subTest(observed_speaker=observed_speaker):
                tracker = IncrementalDialogTracker(
                    complete_dialogue_only=True,
                    early_dialogue_resolver=lambda *_: line,
                    clock=lambda: 0,
                )
                tracker.observe(observed_speaker, "These old accounts contradict")
                chunks = tracker.observe(
                    observed_speaker, "These old accounts contradict the"
                )
                self.assertEqual(
                    [(c.generation, c.character, c.text, c.line_id) for c in chunks],
                    [(1, "Ada", text, "A")],
                )
                self.assertEqual(tracker.canonical_line_id, "A")
                self.assertIn("A", tracker.accepted_line_ids)
                for noisy in (
                    "These old accounts contradict the promisc",
                    "These old accounts contradict the promise yov",
                ):
                    self.assertEqual(tracker.observe("Narrator", noisy), [])
                    self.assertEqual(tracker.generation, 1)
                    self.assertEqual(tracker.character, "Ada")
                self.assertEqual(
                    tracker.observe_identified("Ada", text, "A", exact=True), []
                )
                self.assertEqual(tracker.generation, 1)
                self.assertTrue(tracker.is_idle_complete())
                for noisy in (
                    "These old accounts contradict the promisc",
                    "Ada: These old accounts contradict the promise yov",
                ):
                    self.assertEqual(tracker.observe("Narrator", noisy), [])
                    self.assertEqual(tracker.generation, 1)
                    self.assertEqual(tracker.canonical_line_id, "A")
                self.assertEqual(
                    tracker.observe_identified("Ada", text, "A", exact=True), []
                )
                self.assertTrue(tracker.is_idle_complete())
                next_chunks = tracker.observe_identified(
                    "Ada", "Another account tells a different story.", "B", exact=True
                )
                self.assertEqual(
                    [(c.generation, c.line_id) for c in next_chunks], [(2, "B")]
                )

    def test_accepted_early_line_cannot_replay_after_a_real_transition(self):
        text = "These old accounts contradict the promise you just made."
        line = ChapterDialogue("A", "chapter", 1, "Ada", text, None)
        now = [0.0]
        tracker = IncrementalDialogTracker(
            complete_dialogue_only=True,
            early_dialogue_resolver=lambda speaker, _text: (
                line if speaker == "Ada" else None
            ),
            clock=lambda: now[0],
        )
        tracker.observe("Ada", "These old accounts contradict")
        chunks = tracker.observe("Ada", "These old accounts contradict")
        self.assertEqual([c.line_id for c in chunks], ["A"])
        tracker.observe_identified("Bea", "A new reply.", "B", exact=True)

        for _ in range(4):
            self.assertEqual(tracker.observe("Ada", "These old accounts contradict"), [])
        now[0] = 1.0
        for _ in range(2):
            self.assertEqual(tracker.observe("Ada", "These old accounts contradict"), [])
        self.assertEqual(tracker.admission_reason, "canonical-held")
        next_chunks = tracker.observe_identified(
            "Bea", "Another new reply.", "C", exact=True
        )
        self.assertEqual([c.line_id for c in next_chunks], ["C"])

    def test_early_identity_is_bound_even_when_stability_arrives_after_idle(self):
        text = "These old accounts contradict the promise you just made."
        line = ChapterDialogue("A", "chapter", 1, "Ada", text, None)
        now = [0.0]
        tracker = IncrementalDialogTracker(
            complete_dialogue_only=True,
            early_dialogue_resolver=lambda *_: line,
            clock=lambda: now[0],
        )
        tracker.observe("Ada", "These old accounts contradict")
        now[0] = 1.0
        chunks = tracker.observe("Ada", "These old accounts contradict")
        self.assertEqual([(c.text, c.line_id) for c in chunks], [(text, "A")])
        self.assertEqual(
            tracker.observe_identified("Ada", text, "A", exact=True), []
        )

    def test_early_identity_does_not_hide_a_new_shorter_speaker_reply(self):
        line = ChapterDialogue("A", "chapter", 1, "Ada", "Please come inside.", None)
        tracker = IncrementalDialogTracker(
            early_dialogue_resolver=lambda *_: line,
            complete_dialogue_only=True,
            clock=lambda: 0,
        )
        tracker.observe("Ada", "Please come")
        tracker.observe("Ada", "Please come")

        self.assertEqual(tracker.observe("Bea", "Come inside."), [])
        self.assertEqual(tracker.generation, 1)
        chunks = tracker.observe_identified("Bea", "Come inside.", "B", exact=True)
        self.assertEqual([(c.character, c.line_id) for c in chunks], [("Bea", "B")])
        self.assertEqual(tracker.generation, 2)

    def test_expected_transition_releases_the_early_prefix_speaker_guard(self):
        text = "The shared introduction leads to the first ending."
        line = ChapterDialogue("A", "chapter", 1, "Ada", text, None)
        tracker = IncrementalDialogTracker(
            early_dialogue_resolver=lambda *_: line,
            complete_dialogue_only=True,
            clock=lambda: 0,
        )
        tracker.observe("Ada", "The shared introduction")
        tracker.observe("Ada", "The shared introduction")
        tracker.expect_new_dialog()

        next_text = "The shared introduction leads to another ending."
        self.assertEqual(tracker.observe("Bea", next_text), [])
        self.assertFalse(tracker.is_idle_complete())
        chunks = tracker.observe_identified("Bea", next_text, "B", exact=True)
        self.assertEqual([(c.generation, c.line_id) for c in chunks], [(2, "B")])

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
            early_dialogue_resolver=lambda speaker, _text: ChapterDialogue(
                "A", "chapter", 1, speaker, canonical, None
            ),
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

    def test_pending_new_raw_dialogue_withholds_previous_canonical_readiness(self):
        now = [0.0]
        tracker = IncrementalDialogTracker(clock=lambda: now[0])
        tracker.observe_identified(
            "Ada", "First dialogue is complete.", "A", exact=True
        )
        self.assertTrue(tracker.is_idle_complete())

        self.assertEqual(tracker.observe("Ada", "Zebras wander beyond mountains."), [])
        self.assertFalse(tracker.is_idle_complete())
        chunks = tracker.observe("Ada", "Zebras wander beyond mountains.")

        self.assertEqual(
            [chunk.text for chunk in chunks], ["Zebras wander beyond mountains."]
        )
        self.assertEqual(tracker.generation, 2)
        self.assertIsNone(tracker.canonical_line_id)
        now[0] = 0.8
        self.assertTrue(tracker.is_idle_complete())

    def test_current_canonical_evidence_clears_one_noisy_new_candidate(self):
        for route in ("raw", "identified", "canonical"):
            with self.subTest(route=route):
                tracker = IncrementalDialogTracker()
                text = "First dialogue is complete."
                tracker.observe_identified("Ada", text, "A", exact=True)
                tracker.observe("Ada", "Zebras wander beyond mountains.")
                self.assertFalse(tracker.is_idle_complete())

                if route == "raw":
                    chunks = tracker.observe("Ada", text)
                elif route == "identified":
                    chunks = tracker.observe_identified("Ada", text, "A", exact=True)
                else:
                    chunks = tracker.observe_canonical("Ada", text, "A")

                self.assertEqual(chunks, [])
                self.assertTrue(tracker.is_idle_complete())
                self.assertEqual(tuple(tracker.pending_history), ())
                self.assertEqual(tracker.generation, 1)
                tracker.observe("Ada", "Zebras wander beyond mountains.")
                self.assertFalse(tracker.is_idle_complete())
                self.assertEqual(tracker.generation, 1)

    def test_related_prefix_and_ocr_noise_preserve_current_canonical_readiness(self):
        for character, text in (
            ("Ada", "First dialogue"),
            ("Ada", "First dialogue is complete. OCR noise"),
            ("Ada", "First dialogue is complette."),
            ("Narrator", "Ada First dialogue is complete. background"),
        ):
            with self.subTest(character=character, text=text):
                tracker = IncrementalDialogTracker()
                tracker.observe_identified(
                    "Ada", "First dialogue is complete.", "A", exact=True
                )
                tracker.observe("Ada", "Zebras wander beyond mountains.")

                self.assertEqual(tracker.observe(character, text), [])

                self.assertTrue(tracker.is_idle_complete())
                self.assertEqual(tracker.generation, 1)
                self.assertEqual(tuple(tracker.pending_history), ())

    def test_conflict_withholds_canonical_readiness_until_current_evidence(self):
        tracker = IncrementalDialogTracker()
        tracker.observe_identified("Ada", "First.", "A", exact=True)
        tracker.conflicting_observation = True

        self.assertFalse(tracker.is_idle_complete())
        tracker.observe("Ada", "First.")

        self.assertFalse(tracker.conflicting_observation)
        self.assertTrue(tracker.is_idle_complete())

    def test_stale_accepted_identity_does_not_clear_pending_current_transition(self):
        tracker = IncrementalDialogTracker()
        tracker.observe_identified("Ada", "First.", "A", exact=True)
        tracker.observe_identified("Ada", "Third.", "C", exact=True)
        tracker.observe("Ada", "Zebras wander beyond mountains.")

        self.assertEqual(
            tracker.observe_identified("Ada", "First.", "A", exact=True), []
        )

        self.assertFalse(tracker.is_idle_complete())
        self.assertEqual(len(tracker.pending_history), 1)
        self.assertEqual(tracker.canonical_line_id, "C")

    def test_sequence_identity_does_not_change_chunk_identity_or_equality(self):
        chunk = SpeechChunk(1, "Ada", "Hello.", ordinal=1, explicit_replay=True)
        leased = replace(chunk, sequence_event_id="event", sequence_occurrence_id=2)

        self.assertEqual(chunk, leased)
        self.assertEqual(chunk.chunk_id, leased.chunk_id)
        self.assertTrue(leased.explicit_replay)


if __name__ == "__main__":
    unittest.main()
