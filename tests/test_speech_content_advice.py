import unittest

from vntts.speech_content_advice import assess_speech_content, normalize_speech_text


class SpeechContentAdviceTest(unittest.TestCase):
    def test_case_punctuation_and_curly_apostrophes_preserve_raw_text(self):
        expected = "Wait, DON’T go!"
        observed = "wait don't go."
        advice = assess_speech_content(expected, observed)
        self.assertEqual(advice.status, "no_asr_mismatch")
        self.assertEqual(advice.issues, ())
        self.assertEqual(advice.expected_text, expected)
        self.assertEqual(advice.observed_text, observed)
        self.assertEqual(normalize_speech_text(expected), ("wait", "don't", "go"))

    def test_isolated_english_digits_match_words(self):
        for digit, word in zip(
            "0123456789",
            "zero one two three four five six seven eight nine".split(),
            strict=True,
        ):
            for expected, observed in ((digit, word), (word, digit)):
                with self.subTest(expected=expected, observed=observed):
                    self.assertEqual(
                        assess_speech_content(expected, observed).status,
                        "no_asr_mismatch",
                    )
        for text in ("Wait. 7", "Wait (7)", "Wait [7]", "seven keys six doors"):
            observed = "Wait seven" if text.startswith("Wait") else "7 keys 6 doors"
            self.assertEqual(
                assess_speech_content(text, observed).status, "no_asr_mismatch"
            )

    def test_research_controls_keep_review_categories_conservative(self):
        for expected, observed, status in (
            ("raincloud", "rain cloud", "boundary_variation"),
            ("selfsame", "self-same", "boundary_variation"),
            ("apart", "a part", "boundary_variation"),
            ("nowhere", "now here", "boundary_variation"),
            ("Aderyn", "Add her in", "uncertain_recognition"),
            ("Bechan", "B-chan", "uncertain_recognition"),
            ("Flamo Flurry", "flammoflurry", "uncertain_recognition"),
            ("Wait, what?", "Wait. What? Wine.", "review_possible_content_mismatch"),
            ("Wait what", "Wait what what", "review_possible_content_mismatch"),
            ("not ready", "ready", "review_possible_content_mismatch"),
            ("ready", "not ready", "review_possible_content_mismatch"),
            ("raincloud not ready", "rain cloud ready", "uncertain_recognition"),
            ("Wait what", "Wait which Wine", "uncertain_recognition"),
            ("not ready", "now very ready", "uncertain_recognition"),
            ("can", "can't", "uncertain_recognition"),
            ("closed", "close", "uncertain_recognition"),
            ("Don't", "Dont", "uncertain_recognition"),
            ("6", "7", "uncertain_recognition"),
            ("17", "7", "uncertain_recognition"),
            ("1 2", "12", "uncertain_recognition"),
            ("-7", "7", "uncertain_recognition"),
            ("6.7", "67", "uncertain_recognition"),
        ):
            with self.subTest(expected=expected, observed=observed):
                advice = assess_speech_content(expected, observed)
                self.assertEqual(advice.status, status)
                self.assertTrue(advice.issues)

    def test_issue_spans_show_insertions_deletions_and_mixed_replacements(self):
        added = assess_speech_content("Wait what", "Wait what Wine").issues
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].kind, "possible_added_content")
        self.assertEqual(added[0].expected_words, ())
        self.assertEqual(added[0].observed_words, ("wine",))
        missing = assess_speech_content("not ready", "ready").issues
        self.assertEqual(missing[0].kind, "possible_missing_content")
        self.assertEqual(missing[0].expected_words, ("not",))
        self.assertEqual(missing[0].observed_words, ())
        mixed = assess_speech_content("Wait what", "Wait which Wine").issues
        self.assertEqual(len(mixed), 1)
        self.assertEqual(mixed[0].kind, "uncertain_recognition")
        self.assertEqual(mixed[0].expected_words, ("what",))
        self.assertEqual(mixed[0].observed_words, ("which", "wine"))

    def test_numeric_symbols_do_not_disappear_into_a_clean_match(self):
        for expected, observed in (
            ("-7", "+7"),
            ("- 7", "7"),
            ("−7", "seven"),
            (".7", "7"),
            ("6.7", "6,7"),
            ("6.7", "6 7"),
            ("17", "seventeen"),
            ("1 2", "one two"),
            ("+7 (800) 123-45-67", "7 800 123 45 67"),
            ("+7 (800) 123-45-67", "-7 (800) 123-45-67"),
        ):
            with self.subTest(expected=expected, observed=observed):
                advice = assess_speech_content(expected, observed)
                self.assertEqual(advice.status, "uncertain_recognition")
                self.assertIn(
                    "uncertain_numeric_format", {issue.kind for issue in advice.issues}
                )
        self.assertEqual(
            assess_speech_content("-7 and 6.7", "-7 and 6.7").status,
            "no_asr_mismatch",
        )

    def test_empty_or_punctuation_only_transcripts_remain_uncertain(self):
        for expected, observed in (
            ("Wait", ""),
            ("", "Wait"),
            ("", ""),
            ("Wait", "..."),
            ("...", "!"),
        ):
            with self.subTest(expected=expected, observed=observed):
                advice = assess_speech_content(expected, observed)
                self.assertEqual(advice.status, "uncertain_recognition")
                self.assertEqual(advice.issues[0].kind, "empty_transcript")
