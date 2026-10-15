import unittest
from dataclasses import replace

from vntts.pregeneration_voices import VoiceCandidate
from vntts.preview_content_advisor import PreviewContentCheck
from vntts.reference_recommendation import recommended_reference
from vntts.speech_content_advice import assess_speech_content


def content_check(source, observed, *, text="Wait what", seed=0):
    return PreviewContentCheck(
        source_id=source,
        preview_identity=source * 64,
        audio_sha256="f" * 64,
        text=text,
        seed=seed,
        advice=assess_speech_content(text, observed),
    )


class ReferenceRecommendationTest(unittest.TestCase):
    def setUp(self):
        self.candidates = (
            VoiceCandidate("a", "Character", "one", ("1" * 64,), match_score=90),
            VoiceCandidate("b", "Character", "two", ("2" * 64,), match_score=90),
        )
        self.checks = {
            "a": content_check("a", "Wait what Wine"),
            "b": content_check("b", "Wait what"),
        }

    def test_comparable_match_suggests_alternative_without_mutating_candidates(self):
        self.assertEqual(recommended_reference(self.candidates, self.checks), "b")
        self.assertEqual(self.candidates[0].source_id, "a")

    def test_unknown_uncertain_boundary_and_mismatched_seed_or_text_keep_baseline(self):
        alternatives = (
            {},
            {"a": self.checks["a"]},
            {**self.checks, "b": content_check("b", "Wait which")},
            {**self.checks, "b": content_check("b", "rain cloud", text="raincloud")},
            {**self.checks, "b": replace(self.checks["b"], seed=1)},
            {**self.checks, "b": replace(self.checks["b"], text="Another phrase")},
            {**self.checks, "b": replace(self.checks["b"], source_id="wrong")},
        )
        for checks in alternatives:
            with self.subTest(checks=checks):
                self.assertEqual(recommended_reference(self.candidates, checks), "a")

    def test_saved_choice_and_stronger_character_identity_are_preserved(self):
        self.assertEqual(
            recommended_reference(self.candidates, self.checks, saved_source_id="a"),
            "a",
        )
        weaker = (self.candidates[0], replace(self.candidates[1], match_score=60))
        self.assertEqual(recommended_reference(weaker, self.checks), "a")

    def test_clean_or_uncertain_baseline_is_not_reordered(self):
        for observed in ("Wait what", "Wait which Wine"):
            checks = {**self.checks, "a": content_check("a", observed)}
            self.assertEqual(recommended_reference(self.candidates, checks), "a")
        self.assertIsNone(recommended_reference((), {}))
