import hashlib
import itertools
import unicodedata
import unittest
from dataclasses import replace

from vntts.chapter_voice_preload import (
    ChapterDialogue,
    ChapterMatch,
    ChapterVoicePreloader,
    _bounded_levenshtein,
    _normalize_without_latin_diacritics,
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

    def test_one_or_two_character_lines_require_exact_text(self):
        self.assert_resolves("No.", "No.")
        self.assert_rejects("No.", "Go.")
        self.assert_rejects("Go.", "No.")
        self.assert_rejects("Go.", "Gc.")
        self.assert_rejects("I", "J")

    def test_single_word_typos_are_allowed_from_three_characters(self):
        for canonical, observed in (
            ("Yes!", "Yez!"),
            ("Cat.", "Cut."),
            ("Cat.", "Caat."),
            ("Cat.", "Ct."),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                self.assert_resolves(canonical, observed)
        self.assert_resolves("Go home.", "Gc home.")
        self.assert_rejects("Cat.", "Bbt.")
        for observed in ("Cat.", "Ats."):
            self.assert_rejects("Cats.", observed)

    def test_short_word_typos_keep_ambiguity_and_protected_content_guards(self):
        first = dialogue("Cat.", "first")
        second = dialogue("Hat.", "second")
        preloader = ChapterVoicePreloader([first, second])
        selected, result = preloader.resolve_complete_among(
            "Ada", "Bat.", [first.line_id, second.line_id]
        )
        self.assertIsNone(selected)
        self.assertEqual(result, "expected-ambiguous")
        for canonical, observed in (("Not.", "Now."), ("100", "101")):
            self.assert_rejects(canonical, observed)

    def test_edit_budgets_use_canonical_alphanumeric_length(self):
        for length, budget in (
            (2, 0),
            (3, 1),
            (19, 1),
            (20, 1),
            (59, 1),
            (60, 2),
            (119, 2),
            (120, 3),
        ):
            canonical = "a" * (length - 1) + "z"
            with self.subTest(length=length, edits=budget):
                self.assert_resolves(canonical, "b" * budget + canonical[budget:])
            with self.subTest(length=length, edits=budget + 1):
                self.assert_rejects(
                    canonical, "b" * (budget + 1) + canonical[budget + 1 :]
                )

    def test_latin_diacritics_do_not_spend_the_edit_budget(self):
        for canonical, observed in (
            ("Remember Nowa Miedź?", "Remember Nowa Miedz?"),
            ("Miedź", "Miedz"),
            ("é", "e"),
            ("Café.", "Cufe."),
            ("Café déjà vu.", "Cufe deja vu."),
            ("Remember Nowa Miedź?", "Remembcr Nowa Miedz?"),
            ("Café déjà vu.", "Cafe\u0301 de\u0300ja\u0300 vu."),
            ("Cafe\u0301 de\u0301ja\u0300 vu.", "Cafe deja vu."),
            ("The q\u0307uota is 5.", "The quota is 5."),
            ("The q\u0307uota is not met.", "The quota is not met."),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                self.assert_resolves(canonical, observed)
        self.assert_rejects("Café.", "Buxe.")

    def test_folded_candidates_are_ambiguous_despite_speaker_and_position(self):
        first = dialogue("Café.", "first")
        second = dialogue("Cafè.", "second", speaker="Bea", sequence=100)
        preloader = ChapterVoicePreloader([first, second])
        previous = ChapterMatch(first.chapter, first.sequence, 1.0)
        preloader.current_match = previous
        selected, result = preloader.resolve_complete_among(
            "Ada", "Cafe.", [first.line_id, second.line_id]
        )
        self.assertIsNone(selected)
        self.assertEqual(result, "expected-ambiguous")
        self.assertIs(preloader.current_match, previous)

    def test_strict_match_has_priority_over_folded_equal_candidates(self):
        first = dialogue("Café.", "first")
        second = dialogue("Cafe.", "second")
        preloader = ChapterVoicePreloader([first, second])
        selected, result = preloader.resolve_complete_among(
            "Ada", second.text, [first.line_id, second.line_id]
        )
        self.assertIs(selected, second)
        self.assertEqual(result, "expected-exact")

    def test_diacritic_folding_cannot_hide_longer_completions(self):
        for short_text, long_text in (
            ("Café.", "Cafe noir."),
            ("Cafe.", "Café noir."),
        ):
            with self.subTest(short=short_text):
                short = dialogue(short_text, "short")
                long = dialogue(long_text, "long")
                preloader = ChapterVoicePreloader([short, long])
                selected, result = preloader.resolve_complete_among(
                    "Ada", "Cafe.", [short.line_id, long.line_id]
                )
                self.assertIsNone(selected)
                self.assertEqual(result, "expected-incomplete")

    def test_folded_fuzzy_competitors_keep_the_two_edit_margin(self):
        first = dialogue("Café.", "first")
        second = dialogue("Côté.", "second")
        preloader = ChapterVoicePreloader([first, second])
        selected, result = preloader.resolve_complete_among(
            "Ada", "Cufe.", [first.line_id, second.line_id]
        )
        self.assertIsNone(selected)
        self.assertEqual(result, "expected-ambiguous")

    def test_folded_matching_preserves_protected_content_and_word_boundaries(self):
        for canonical, observed in (
            ("Café costs -5.", "Cafe costs 5."),
            ("Café not now.", "Cafe now now."),
            ("Not now.", "Nót now."),
            ("Café déjà vu.", "Cafedejavu."),
            ("Café déjà vu.", "Cafe deja v."),
            ("The q\u0307uota is 5.", "The quota is 6."),
            ("The q\u0307uota is not met.", "The quota is now met."),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                self.assert_rejects(canonical, observed)

    def test_folded_matching_keeps_allowed_scope_identity_and_context(self):
        row = dialogue("Café.")
        missing_hash = replace(dialogue("Cafè.", "missing"), text_sha256=None)
        preloader = ChapterVoicePreloader([row, missing_hash])
        previous = ChapterMatch("previous", 7, 1.0)
        preloader.current_match = previous
        for allowed in ([], ["absent"], [missing_hash.line_id]):
            with self.subTest(allowed=allowed):
                selected, _ = preloader.resolve_complete_among("Ada", "Cafe.", allowed)
                self.assertIsNone(selected)
                self.assertIs(preloader.current_match, previous)
        selected, result = preloader.resolve_complete_among(
            "Ada", "Cafe.", [row.line_id, missing_hash.line_id], update_context=False
        )
        self.assertIs(selected, row)
        self.assertEqual(result, "expected-complete-diacritic-fold")
        self.assertIs(preloader.current_match, previous)
        preloader.resolve_complete_among("Ada", "Cafe.", [row.line_id])
        self.assertEqual(preloader.current_match.chapter, row.chapter)

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


class LatinDiacriticNormalizationTest(unittest.TestCase):
    def test_latin_marks_are_removed_without_losing_word_boundaries(self):
        for text in (
            "Café déjà vu. Miedź!",
            "Cafe\u0301 de\u0301ja\u0300 vu. Miedz\u0301!",
        ):
            self.assertEqual(
                _normalize_without_latin_diacritics(text), "cafe deja vu miedz"
            )

    def test_non_latin_diacritics_and_compatibility_characters_are_preserved(self):
        for text in ("й", "ά", "が", "²"):
            with self.subTest(text=text):
                self.assertEqual(_normalize_without_latin_diacritics(text), text)
                self.assertEqual(
                    _normalize_without_latin_diacritics(
                        unicodedata.normalize("NFD", text)
                    ),
                    text,
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
