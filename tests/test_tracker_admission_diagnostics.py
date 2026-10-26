import unittest

from vntts.live_tracking import IncrementalDialogTracker, SpeechChunk


class TrackerAdmissionDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0

    def tracker(self, **kwargs):
        return IncrementalDialogTracker(clock=lambda: self.now, **kwargs)

    def test_stability_boundary_emission_and_unchanged(self):
        tracker = self.tracker(stability_frames=3)
        self.assertEqual(tracker.admission_reason, "empty")
        for _ in range(2):
            self.assertEqual(tracker.observe("Ada", "A partial line"), [])
            self.assertEqual(tracker.admission_reason, "waiting-stability")
        self.assertEqual(tracker.observe("Ada", "A partial line"), [])
        self.assertEqual(tracker.admission_reason, "waiting-boundary")

        self.now = 0.8
        chunks = tracker.observe("Ada", "A partial line")
        self.assertEqual(chunks, [SpeechChunk(1, "Ada", "A partial line")])
        self.assertEqual(chunks[0].ordinal, 1)
        self.assertEqual(tracker.admission_reason, "emitted")
        self.assertEqual(tracker.observe("Ada", "A partial line"), [])
        self.assertEqual(tracker.admission_reason, "unchanged")
        self.assertEqual(tracker.committed_text, "A partial line")

    def test_new_dialog_candidate_waits_without_changing_ownership(self):
        tracker = self.tracker()
        tracker.observe("Ada", "First line.")
        tracker.observe("Ada", "First line.")
        self.assertEqual(tracker.observe("Bea", "Next line."), [])
        self.assertEqual(tracker.admission_reason, "waiting-stability")
        self.assertEqual((tracker.generation, tracker.character), (1, "Ada"))
        self.assertEqual(tuple(tracker.pending_history), ("Next line.",))
        chunks = tracker.observe("Bea", "Next line.")
        self.assertEqual(chunks, [SpeechChunk(2, "Bea", "Next line.")])
        self.assertEqual(chunks[0].ordinal, 1)
        self.assertEqual(tracker.admission_reason, "emitted")
        self.assertEqual(tuple(tracker.pending_history), ())

    def test_committed_prefix_and_speaker_noise_preserve_committed_dialog(self):
        for text, observation, reason in (
            ("Hello, world.", ("Ada", "hello"), "committed-prefix"),
            ("... Hello.", ("Ada", "..."), "committed-prefix"),
            ("Hello, world.", ("Narrator", "Ada: Hello world!"), "speaker-noise"),
        ):
            with self.subTest(reason=reason, observation=observation):
                tracker = self.tracker()
                tracker.observe("Ada", text)
                tracker.observe("Ada", text)
                self.assertEqual(tracker.observe(*observation), [])
                self.assertEqual(tracker.admission_reason, reason)
                self.assertEqual(tracker.committed_text, text)
                self.assertEqual((tracker.generation, tracker.character), (1, "Ada"))
                self.assertFalse(tracker.conflicting_observation)

    def test_committed_conflict_also_updates_flush_reason(self):
        tracker = self.tracker()
        tracker.observe("Ada", "Yes, okay.")
        tracker.observe("Ada", "Yes, okay.")
        self.assertEqual(tracker.observe("Ada", "Yes, please."), [])
        self.assertEqual(tracker.admission_reason, "committed-conflict")
        self.assertTrue(tracker.conflicting_observation)
        self.assertEqual(tracker.flush(), [])
        self.assertEqual(tracker.admission_reason, "committed-conflict")
        self.assertEqual(tracker.committed_text, "Yes, okay.")

    def test_stable_prefix_conflict_keeps_existing_flush_semantics(self):
        tracker = self.tracker(stability_frames=3)
        tracker.observe("Ada", "Hello")
        tracker.observe("Ada", "Hello world.")
        self.assertEqual(tracker.flush(), [SpeechChunk(1, "Ada", "Hello world.")])
        self.assertEqual(tracker.admission_reason, "emitted")
        self.assertEqual(tracker.observe("Ada", "Hello world."), [])
        self.assertEqual(tracker.admission_reason, "committed-conflict")
        self.assertTrue(tracker.conflicting_observation)

    def test_complete_dialogue_waits_for_boundary_or_incomplete_source(self):
        tracker = self.tracker(
            complete_dialogue_only=True,
            incomplete_dialogue_probe=lambda character, text: True,
        )
        tracker.observe("Ada", "An unfinished source")
        self.assertEqual(tracker.observe("Ada", "An unfinished source"), [])
        self.assertEqual(tracker.admission_reason, "waiting-boundary")
        self.now = 0.8
        self.assertEqual(tracker.observe("Ada", "An unfinished source"), [])
        self.assertEqual(tracker.admission_reason, "incomplete-source-prefix")
        self.assertEqual(tracker.committed_text, "")
        self.assertEqual(
            tracker.flush(), [SpeechChunk(1, "Ada", "An unfinished source")]
        )
        self.assertEqual(tracker.admission_reason, "emitted")

    def test_early_resolver_emits_existing_canonical_text(self):
        tracker = self.tracker(
            complete_dialogue_only=True,
            early_dialogue_resolver=lambda character, text: "Canonical full line.",
        )
        tracker.observe("Ada", "Canonical")
        self.assertEqual(
            tracker.observe("Ada", "Canonical"),
            [SpeechChunk(1, "Ada", "Canonical full line.")],
        )
        self.assertEqual(tracker.admission_reason, "emitted")
        self.assertEqual(tracker.latest_text, "Canonical")

    def test_canonical_admission_hold_and_flush_never_leave_emitted_reason(self):
        tracker = self.tracker()
        chunks = tracker.observe_canonical("Ada", "Canonical line.", "line-1")
        self.assertEqual(tracker.admission_reason, "emitted")
        self.assertEqual((chunks[0].generation, chunks[0].ordinal), (1, 1))
        self.assertEqual(chunks[0].line_id, "line-1")
        self.assertEqual(
            tracker.observe_canonical("Ada", "Canonical line.", "line-1"), []
        )
        self.assertEqual(tracker.admission_reason, "canonical-held")
        self.assertEqual(tracker.observe("Ada", "Canonical line. OCR noise"), [])
        self.assertEqual(tracker.admission_reason, "canonical-held")
        self.assertEqual(tracker.flush(), [])
        self.assertEqual(tracker.admission_reason, "unchanged")
        self.assertEqual(tracker.generation, 1)
        self.assertEqual(tracker.canonical_line_id, "line-1")

    def test_silent_and_empty_events_reset_reason_without_extra_generations(self):
        tracker = self.tracker()
        self.assertTrue(tracker.observe_silent("silent-1"))
        self.assertEqual(tracker.admission_reason, "silent")
        self.assertFalse(tracker.observe_silent("silent-1"))
        self.assertEqual(tracker.admission_reason, "silent")
        self.assertEqual(tracker.generation, 1)
        self.assertEqual(tracker.flush(), [])
        self.assertEqual(tracker.admission_reason, "empty")
        tracker.observe("Ada", "Spoken line.")
        self.assertEqual(tracker.admission_reason, "waiting-stability")
        self.assertEqual(tracker.observe("Ada", ""), [])
        self.assertEqual(tracker.admission_reason, "empty")
        self.assertEqual(tracker.generation, 3)
        self.assertEqual(tracker.observe("Ada", ""), [])
        self.assertEqual(tracker.generation, 3)

    def test_punctuation_only_admission_is_silent_and_then_unchanged(self):
        tracker = self.tracker()
        tracker.observe("Ada", "...")
        self.assertEqual(tracker.observe("Ada", "..."), [])
        self.assertEqual(tracker.admission_reason, "silent")
        self.assertEqual(tracker.committed_text, "...")
        self.assertEqual(tracker.next_chunk_ordinal, 1)
        self.assertEqual(tracker.flush(), [])
        self.assertEqual(tracker.admission_reason, "unchanged")


if __name__ == "__main__":
    unittest.main()
