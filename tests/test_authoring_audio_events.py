import hashlib
import unittest
from unittest.mock import patch

import vntts.authoring.audio_events as audio_events_module
from vntts.authoring.audio_events import (
    AUDIO_EVENT_PLAN_FIELD,
    STORY_AUDIO_CUES_FIELD,
    audio_event_plan_document,
    audio_event_plan_for_record,
    plan_inline_audio_events,
    requires_audio_event_composition,
    validate_story_audio_cues,
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

    def test_cues_are_validated_once_and_preserve_producer_extensions(self):
        cue = {**story_audio_cue(), "producer_metadata": {"revision": 3}}
        original = validate_story_audio_cues
        for text in ("Tsk!", "Hello *gasp*", "Ordinary dialogue."):
            with self.subTest(text=text):
                with patch.object(
                    audio_events_module, "validate_story_audio_cues", wraps=original
                ) as validate:
                    document = audio_event_plan_document(text, story_audio_cues=(cue,))
                self.assertEqual(validate.call_count, 1)
                if document is not None:
                    self.assertEqual(
                        document,
                        plan_inline_audio_events(text).to_document(
                            story_audio_cues=[cue]
                        ),
                    )
        self.assertEqual(cue["producer_metadata"], {"revision": 3})
        with self.assertRaisesRegex(ValueError, "source_audio_id is invalid"):
            plan_inline_audio_events("Tsk!").to_document(
                story_audio_cues=[{"cue_index": 1}]
            )

    def test_cue_indices_require_integers_and_scalars_have_domain_errors(self):
        for value in (True, 1.0):
            with self.subTest(index=value):
                cue = {**story_audio_cue(), "cue_index": value}
                with self.assertRaisesRegex(ValueError, "source-order indices"):
                    validate_story_audio_cues([cue])
        for field in (
            "localized_parameter_2",
            "scalar_parameter_4",
            "localized_parameter_5",
        ):
            for value in (10**1000, float("inf"), float("nan"), True):
                with self.subTest(field=field, value=str(value)[:20]):
                    cue = {**story_audio_cue(), field: value}
                    with self.assertRaisesRegex(ValueError, field):
                        validate_story_audio_cues([cue])

    def test_legacy_plan_without_story_audio_field_remains_byte_compatible(self):
        text = "N-No! *gurgle*"

        self.assertEqual(
            audio_event_plan_for_record({"text": text}),
            audio_event_plan_document(text),
        )


if __name__ == "__main__":
    unittest.main()
