import hashlib
import unittest

from vntts.authoring.audio_events import (
    AUDIO_EVENT_PLAN_FIELD,
    audio_event_plan_document,
    plan_inline_audio_events,
    requires_audio_event_composition,
)


class AuthoringAudioEventTest(unittest.TestCase):
    def test_mixed_gurgle_keeps_canonical_text_and_separates_speech(self):
        text = "N-No! *gurgle*"

        plan = plan_inline_audio_events(text)
        document = plan.to_document()

        self.assertEqual(plan.canonical_text, text)
        self.assertEqual(plan.spoken_text, "N-No!")
        self.assertEqual(
            document["canonical_text_sha256"], hashlib.sha256(text.encode()).hexdigest()
        )
        self.assertEqual(document["event_count"], 1)
        self.assertEqual(document["events"][0]["kind"], "human-gurgle")
        self.assertEqual(
            document["events"][0]["synthesis_policy"],
            "sound-effect-model-candidate",
        )

    def test_gasp_preserves_order_and_unknown_marker_fails_closed(self):
        plan = plan_inline_audio_events("Wait *gasp* there *door closes*")

        self.assertEqual(plan.spoken_text, "Wait there")
        self.assertEqual(
            [value["kind"] for value in plan.events],
            ["human-gasp", "unsupported-stage-direction"],
        )
        self.assertEqual([value["event_index"] for value in plan.events], [1, 2])
        self.assertEqual(plan.events[1]["synthesis_policy"], "unsupported")

    def test_tsk_is_an_event_only_pronunciation_candidate(self):
        plan = plan_inline_audio_events("Tsk!")

        self.assertEqual(plan.spoken_text, "")
        self.assertEqual(plan.events[0]["kind"], "tongue-click")
        self.assertEqual(
            plan.events[0]["synthesis_policy"], "tts-pronunciation-candidate"
        )

    def test_ordinary_dialogue_has_no_additive_plan(self):
        self.assertIsNone(audio_event_plan_document("Ordinary dialogue."))
        self.assertFalse(requires_audio_event_composition("Ordinary dialogue."))

    def test_recorded_plan_must_match_exact_canonical_text(self):
        document = {
            "text": "Wh-What! *gasp*",
            AUDIO_EVENT_PLAN_FIELD: audio_event_plan_document("Wh-What! *gasp*"),
        }
        self.assertTrue(requires_audio_event_composition(document))
        document["text"] = "Changed"
        with self.assertRaisesRegex(ValueError, "does not match"):
            requires_audio_event_composition(document)


if __name__ == "__main__":
    unittest.main()
