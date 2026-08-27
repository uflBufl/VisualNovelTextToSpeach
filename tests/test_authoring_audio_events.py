import hashlib
import unittest

from vntts.authoring.audio_events import (
    AUDIO_EVENT_PLAN_FIELD,
    STORY_AUDIO_CUES_FIELD,
    audio_event_plan_document,
    audio_event_plan_for_record,
    plan_inline_audio_events,
    requires_audio_event_composition,
)


def story_audio_cue(*, event="play_stream", status="configured_unavailable"):
    normalized = {
        "configured_unavailable": "unavailable",
        "installed": "available",
    }[status]
    media = [12] if status == "installed" else []
    return {
        "cue_index": 1,
        "source_audio_id": "25500117",
        "parameter_code_1": 0,
        "localized_parameter_2": 0.0,
        "parameter_code_3": 1,
        "scalar_parameter_4": 1.0,
        "localized_parameter_5": 0.0,
        "parameter_code_6": 1,
        "audio_status": status,
        "audio_reason": "resolved_local_media" if media else "bank_not_installed",
        "source_audio_status": normalized,
        "source_event": event,
        "source_bank": "story-sfx.bnk",
        "source_media_ids": media,
        "available_media_ids": media,
    }


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

    def test_story_audio_cues_are_validated_and_bound_without_semantic_assignment(self):
        cue = story_audio_cue()
        record = {
            "text": "Wh-What! *gasp*",
            STORY_AUDIO_CUES_FIELD: [cue],
        }

        plan = audio_event_plan_for_record(record)

        self.assertEqual(plan["story_audio_cue_count"], 1)
        self.assertEqual(len(plan["story_audio_cues_sha256"]), 64)
        self.assertNotIn("source_audio_id", plan["events"][0])
        record[AUDIO_EVENT_PLAN_FIELD] = plan
        self.assertTrue(requires_audio_event_composition(record))

        record[STORY_AUDIO_CUES_FIELD][0]["source_event"] = "changed"
        with self.assertRaisesRegex(ValueError, "does not match"):
            requires_audio_event_composition(record)

    def test_invalid_story_audio_cues_fail_even_when_text_has_no_event(self):
        record = {
            "text": "Ordinary dialogue.",
            STORY_AUDIO_CUES_FIELD: [{"cue_index": 1}],
        }

        with self.assertRaisesRegex(ValueError, "source_audio_id is invalid"):
            audio_event_plan_for_record(record)

    def test_legacy_plan_without_story_audio_field_remains_byte_compatible(self):
        text = "N-No! *gurgle*"

        self.assertEqual(
            audio_event_plan_for_record({"text": text}),
            audio_event_plan_document(text),
        )


if __name__ == "__main__":
    unittest.main()
