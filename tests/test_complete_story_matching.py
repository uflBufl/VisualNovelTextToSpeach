import hashlib
import itertools
import unittest
from dataclasses import replace

from vntts.chapter_voice_preload import (
    ChapterDialogue,
    ChapterMatch,
    ChapterVoicePreloader,
    _bounded_levenshtein,
)


def dialogue(text, line_id="line", *, speaker="Ada", sequence=1):
    return ChapterDialogue(
        line_id,
        "chapter",
        sequence,
        speaker,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        story_order=sequence,
    )


class CompleteStoryMatchingTest(unittest.TestCase):
    def assert_resolves(self, canonical, observed, *, speaker="Ada"):
        row = dialogue(canonical)
        preloader = ChapterVoicePreloader([row])
        selected, result = preloader.resolve_complete_among(
            speaker, observed, [row.line_id]
        )
        self.assertIs(selected, row)
        self.assertIsInstance(result, str)
        self.assertTrue(result)
        return preloader

    def assert_rejects(self, canonical, observed):
        row = dialogue(canonical)
        preloader = ChapterVoicePreloader([row])
        previous = ChapterMatch("previous", 9, 1.0)
        preloader.current_match = previous
        selected, _result = preloader.resolve_complete_among(
            "Ada", observed, [row.line_id]
        )
        self.assertIsNone(selected)
        self.assertIs(preloader.current_match, previous)

    def test_complete_long_lines_accept_single_ocr_substitution(self):
        for text in (
            "I have been thinking about the old stories and all the people "
            "who came here before we finally arrived.",
            "I remember the promise we made together when we left the village "
            "and started our journey across the mountains.",
        ):
            with self.subTest(text=text):
                self.assert_resolves(text, "J" + text[1:])

    def test_normalized_text_only_accepts_hyphenated_line_with_unknown_speaker(self):
        self.assert_resolves(
            "The stack-finally arrangement will keep everything in its place.",
            "The stack finally arrangement will keep everything in its place.",
            speaker="Unknown speaker",
        )

    def test_short_lines_require_exact_text(self):
        self.assert_resolves("No.", "No.")
        self.assert_rejects("No.", "Go.")
        self.assert_rejects("Go.", "No.")

    def test_edit_budgets_use_canonical_alphanumeric_length(self):
        for length, budget in ((19, 0), (20, 1), (59, 1), (60, 2), (119, 2), (120, 3)):
            canonical = "a" * (length - 3) + " end"
            with self.subTest(length=length, edits=budget):
                self.assert_resolves(canonical, "b" * budget + canonical[budget:])
            with self.subTest(length=length, edits=budget + 1):
                self.assert_rejects(
                    canonical, "b" * (budget + 1) + canonical[budget + 1 :]
                )

    def test_internal_insertions_and_deletions_are_bounded_edits(self):
        canonical = "The lantern illuminated the forgotten passage beside our home."
        for observed in (
            canonical.replace("illuminated", "iluminated"),
            canonical.replace("illuminated", "illumminated"),
        ):
            with self.subTest(observed=observed):
                self.assert_resolves(canonical, observed)

    def test_numeric_expressions_cannot_change_even_when_normalization_hides_sign(self):
        for expected, observed in (
            ("10", "11"),
            ("four", "five"),
            ("-5", "5"),
            ("+5", "5"),
            ("1.5", "1,5"),
            ("1.5", "15"),
        ):
            with self.subTest(expected=expected, observed=observed):
                self.assert_rejects(
                    f"The remaining distance is {expected} miles before we reach home.",
                    f"The remaining distance is {observed} miles before we reach home.",
                )

    def test_negation_and_contractions_cannot_change(self):
        for expected, observed in (
            ("not", "now"),
            ("never", "lever"),
            ("no", "go"),
            ("can't", "can"),
            ("isn't", "isnt"),
        ):
            with self.subTest(expected=expected, observed=observed):
                self.assert_rejects(
                    f"The message says {expected} and we must remember it until morning.",
                    f"The message says {observed} and we must remember it until morning.",
                )
        self.assert_resolves(
            "I can't leave the lantern behind while we are still walking home.",
            "I can’t leave the lantern behind while we are still walking home.",
        )

    def test_negation_cannot_move_to_a_different_clause(self):
        self.assert_rejects(
            "The first lamp is not on and the second lamp is now on "
            "at the end of this forgotten passage.",
            "The first lamp is now on and the second lamp is not on "
            "at the end of this forgotten passage.",
        )

    def test_numeric_expression_cannot_move_to_a_different_word_position(self):
        self.assert_rejects(
            "The marker reads A 1 before the long description "
            "of the route across the forgotten passage.",
            "The marker reads 1 A before the long description "
            "of the route across the forgotten passage.",
        )

    def test_partial_prefix_suffix_and_changed_final_token_are_rejected(self):
        canonical = "The lantern illuminated the forgotten passage beside our home."
        for observed in (
            canonical[:-2],
            canonical.rsplit(" ", 1)[0],
            canonical.split(" ", 1)[1],
            canonical.replace("home", "hope"),
        ):
            with self.subTest(observed=observed):
                self.assert_rejects(canonical, observed)

    def test_exact_shorter_line_is_ambiguous_with_longer_completion(self):
        short = dialogue("The lantern illuminated the forgotten passage.", "short")
        long = dialogue(short.text[:-1] + " beside our home.", "long")
        preloader = ChapterVoicePreloader([short, long])
        selected, _result = preloader.resolve_complete_among(
            "Ada", short.text, [short.line_id, long.line_id]
        )
        self.assertIsNone(selected)
        self.assertIsNone(preloader.current_match)

    def test_exact_match_has_priority_over_neighboring_fuzzy_line(self):
        exact = dialogue(
            "The lantern illuminated the forgotten passage beside our home."
        )
        neighbor = dialogue(exact.text.replace("lantern", "lantarn"), "neighbor")
        preloader = ChapterVoicePreloader([exact, neighbor])
        selected, _result = preloader.resolve_complete_among(
            "Ada", exact.text, [exact.line_id, neighbor.line_id]
        )
        self.assertIs(selected, exact)

    def test_fuzzy_winner_requires_two_edit_margin(self):
        best = dialogue(
            "The lantern illuminated the forgotten passage beside our home."
        )
        neighbor = dialogue(best.text.replace("lantern", "lantorn"), "neighbor")
        preloader = ChapterVoicePreloader([best, neighbor])
        selected, _result = preloader.resolve_complete_among(
            "Ada",
            best.text.replace("lantern", "lantarn"),
            [best.line_id, neighbor.line_id],
        )
        self.assertIsNone(selected)

    def test_two_edit_margin_is_sufficient(self):
        best = dialogue("a" * 57 + " end")
        neighbor = dialogue("ccc" + "a" * 54 + " end", "neighbor")
        preloader = ChapterVoicePreloader([best, neighbor])
        selected, _result = preloader.resolve_complete_among(
            "Ada", "b" + "a" * 56 + " end", [best.line_id, neighbor.line_id]
        )
        self.assertIs(selected, best)

    def test_out_of_budget_neighbor_still_competes(self):
        best = dialogue("a" * 57 + " end")
        neighbor = dialogue("cc" + "a" * 54 + " end", "neighbor")
        preloader = ChapterVoicePreloader([best, neighbor])
        selected, _result = preloader.resolve_complete_among(
            "Ada", "bb" + "a" * 55 + " end", [best.line_id, neighbor.line_id]
        )
        self.assertIsNone(selected)

    def test_exact_speaker_can_disambiguate_duplicate_complete_text(self):
        first = dialogue("The lantern illuminated the forgotten passage.", "first")
        second = dialogue(first.text, "second", speaker="Bea")
        preloader = ChapterVoicePreloader([first, second])
        selected, _result = preloader.resolve_complete_among(
            "Ada", first.text, [first.line_id, second.line_id]
        )
        self.assertIs(selected, first)

    def test_duplicate_text_only_matches_remain_ambiguous_despite_context(self):
        first = dialogue("The stack-finally arrangement will hold.", "first")
        second = dialogue(first.text, "second", speaker="Bea", sequence=100)
        preloader = ChapterVoicePreloader([first, second])
        previous = ChapterMatch(first.chapter, first.sequence, 1.0)
        preloader.current_match = previous
        selected, _result = preloader.resolve_complete_among(
            "Unknown",
            first.text.replace("stack-finally", "stack finally"),
            [first.line_id, second.line_id],
        )
        self.assertIsNone(selected)
        self.assertIs(preloader.current_match, previous)

    def test_only_explicit_ids_with_checksum_identity_are_eligible(self):
        canonical = "The lantern illuminated the forgotten passage beside our home."
        outside = dialogue(canonical, "outside")
        missing_hash = replace(dialogue(canonical, "missing-hash"), text_sha256=None)
        missing_id = replace(dialogue(canonical), line_id=None)
        preloader = ChapterVoicePreloader([outside, missing_hash, missing_id])
        for allowed in ([], ["absent"], [missing_hash.line_id], [None]):
            with self.subTest(allowed=allowed):
                selected, _result = preloader.resolve_complete_among(
                    "Ada", canonical, allowed
                )
                self.assertIsNone(selected)
                self.assertIsNone(preloader.current_match)
        selected, _result = preloader.resolve_complete_among(
            "Ada", canonical, [outside.line_id, outside.line_id, "absent"]
        )
        self.assertIs(selected, outside)

    def test_unauthorized_exact_match_does_not_override_allowed_fuzzy_line(self):
        allowed = dialogue(
            "The lantern illuminated the forgotten passage beside our home."
        )
        outside = dialogue(allowed.text.replace("lantern", "lantarn"), "outside")
        preloader = ChapterVoicePreloader([allowed, outside])
        selected, _result = preloader.resolve_complete_among(
            "Ada", outside.text, [allowed.line_id]
        )
        self.assertIs(selected, allowed)

    def test_context_can_be_preserved_for_each_successful_matching_path(self):
        canonical = "The stack-finally arrangement will keep everything in its place."
        row = dialogue(canonical, sequence=7)
        for speaker, observed in (
            ("Ada", canonical),
            ("Unknown", canonical.replace("stack-finally", "stack finally")),
            ("Ada", canonical.replace("arrangement", "arrangament")),
        ):
            with self.subTest(speaker=speaker, observed=observed):
                preloader = ChapterVoicePreloader([row])
                previous = ChapterMatch("previous", 3, 1.0)
                preloader.current_match = previous
                selected, _result = preloader.resolve_complete_among(
                    speaker, observed, [row.line_id], update_context=False
                )
                self.assertIs(selected, row)
                self.assertIs(preloader.current_match, previous)
                selected, _result = preloader.resolve_complete_among(
                    speaker, observed, [row.line_id]
                )
                self.assertIs(selected, row)
                self.assertEqual(
                    preloader.current_match,
                    ChapterMatch(row.chapter, row.sequence, 1.0, row.story_order),
                )


class BoundedLevenshteinTest(unittest.TestCase):
    def test_matches_full_dynamic_programming_oracle_with_cutoffs(self):
        words = [
            "".join(letters)
            for length in range(4)
            for letters in itertools.product("ab", repeat=length)
        ]
        for left, right in itertools.product(words, repeat=2):
            previous = list(range(len(right) + 1))
            for i, left_character in enumerate(left, 1):
                current = [i]
                for j, right_character in enumerate(right, 1):
                    current.append(
                        min(
                            current[-1] + 1,
                            previous[j] + 1,
                            previous[j - 1] + (left_character != right_character),
                        )
                    )
                previous = current
            distance = previous[-1]
            for limit in range(4):
                with self.subTest(left=left, right=right, limit=limit):
                    self.assertEqual(
                        _bounded_levenshtein(left, right, limit),
                        min(distance, limit + 1),
                    )


if __name__ == "__main__":
    unittest.main()
