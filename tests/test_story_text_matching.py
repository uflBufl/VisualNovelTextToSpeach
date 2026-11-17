import itertools
import unicodedata
import unittest

from vntts.story_text_matching import (
    MatchingText,
    compare_text,
    fold_latin_diacritics,
    prepare_text,
)


class StoryTextMatchingTest(unittest.TestCase):
    def compare(self, canonical, observed, limit=4):
        return compare_text(prepare_text(observed), prepare_text(canonical), limit)

    def test_punctuation_is_a_collapsed_word_separator(self):
        for text in (
            "  Café,--déjà\tvu! ",
            "Ｃａｆé déjà vu",
            "Cafe de\u0301ja\u0300 vu",
        ):
            with self.subTest(text=text):
                self.assertEqual(prepare_text(text), MatchingText("cafe deja vu", 10))
        for canonical, observed, distance in (
            ("A well-known traveler came home.", "A well known traveler came home.", 0),
            ("A well-known traveler came home.", "A wellknown traveler came home.", 1),
            ("The quota is 5.", "The qu:ota is 5.", 1),
            ("Now here.", "Nowhere.", 1),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                evidence = self.compare(canonical, observed)
                self.assertTrue(evidence.complete)
                self.assertEqual(evidence.distance, distance)

    def test_edit_evidence_does_not_depend_on_word_position(self):
        canonical = "The lantern illuminated the forgotten passage beside our home."
        for observed in (
            "J" + canonical[1:],
            canonical.replace("illuminated", "iluminated"),
            canonical.replace("illuminated", "illumminated"),
            canonical.replace("home", "homc"),
        ):
            with self.subTest(observed=observed):
                evidence = self.compare(canonical, observed)
                self.assertTrue(evidence.complete)
                self.assertEqual(evidence.distance, 1)
        for canonical, observed in (("Cat.", "Ct."), ("Cat.", "Caat.")):
            with self.subTest(canonical=canonical, observed=observed):
                self.assertTrue(self.compare(canonical, observed).complete)

    def test_missing_source_ends_and_equal_cost_uncertainty_are_not_complete(self):
        for canonical, observed in (
            ("Cats.", "Cat."),
            ("Cats.", "ats."),
            ("abb", "ab"),
            ("bba", "ba"),
            ("Nowa Miedź?", "Nowa Mied"),
            ("The lantern is home.", "lantern is home."),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                self.assertFalse(self.compare(canonical, observed).complete)

    def test_owned_prefix_can_have_an_internal_typo(self):
        evidence = self.compare("Remember Nowa Miedź?", "Rememher Nowa Mied")
        self.assertFalse(evidence.complete)
        self.assertEqual(evidence.prefix_distance, 1)
        self.assertEqual(evidence.covered_characters, len("RememberNowaMied"))
        self.assertFalse(evidence.prefix_ends_at_boundary)
        whole_word = self.compare("The lantern stays home.", "The lantern")
        self.assertEqual(whole_word.prefix_distance, 0)
        self.assertTrue(whole_word.prefix_ends_at_boundary)
        self.assertEqual(self.compare("Caterpillar.", "Cat.").prefix_distance, 0)
        self.assertFalse(self.compare("Caterpillar.", "Cat.").prefix_ends_at_boundary)

    def test_numbers_negation_and_contractions_use_ordinary_edits(self):
        for canonical, observed, distance in (
            ("The quota is -5.", "The quota is 5.", 0),
            ("The quota is +5.", "The quota is 5.", 0),
            ("The quota is 5.", "The quota is 6.", 1),
            ("The quota is 1.5.", "The quota is 1,5.", 0),
            ("The quota is 5/10.", "The quota is 5 10.", 0),
            ("The quota is 1e5.", "The quota is 1f5.", 1),
            ("The quota is five.", "The quota is hive.", 1),
            ("The quota is not met.", "The quota is now met.", 1),
            ("We cannot leave home.", "We can leave home.", 3),
            ("We can't leave home.", "We cant leave home.", 1),
            ("Not now.", "Nót now.", 0),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                evidence = self.compare(canonical, observed)
                self.assertTrue(evidence.complete)
                self.assertEqual(evidence.distance, distance)
        evidence = self.compare("The quota is 12345.", "The quota is 123")
        self.assertFalse(evidence.complete)
        self.assertEqual(evidence.prefix_distance, 0)

    def test_prefixes_reject_missing_source_beginning_and_trailing_ocr_insertion(self):
        for canonical, observed in (
            ("The lantern shines brightly.", "he lantern"),
            ("The lantern shines brightly.", "The lanternx"),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                self.assertIsNone(self.compare(canonical, observed).prefix_distance)

    def test_prefix_budget_depends_on_visible_text_not_the_unseen_remainder(self):
        canonical = "The lantern shines " + "along the forgotten passage " * 20
        self.assertEqual(
            self.compare(canonical, "The lantcrn shines", limit=20).prefix_distance, 1
        )
        self.assertIsNone(
            self.compare(canonical, "The lantcrn shincs", limit=20).prefix_distance
        )

    def test_unicode_uses_latin_folding_and_preserves_other_scripts(self):
        for text in ("Café déjà vu. Miedź!", "The q\u0307uota is 5."):
            folded = fold_latin_diacritics(text)
            for variant in (text, unicodedata.normalize("NFD", text), folded):
                with self.subTest(text=text, variant=variant):
                    self.assertTrue(self.compare(text, variant).complete)
        for text in ("й", "ά", "が", "हिन्दी"):
            with self.subTest(text=text):
                self.assertEqual(fold_latin_diacritics(text), text)
                self.assertEqual(
                    prepare_text(text), prepare_text(unicodedata.normalize("NFD", text))
                )
        self.assertEqual(prepare_text("Ｆｕｌｌ").key, prepare_text("Full").key)
        self.assertEqual(prepare_text("Café déjà").alphanumeric_length, 8)

    def test_combining_marks_keep_their_surrounding_script_context(self):
        expected = MatchingText("\u0307 q б\u0307\u0308 \u0307", 2)
        self.assertEqual(
            prepare_text("\u0307 q\u0307\u0308 Б\u0307\u0308 .\u0307"), expected
        )
        self.assertEqual(prepare_text("q\u0307").key, "q")
        self.assertEqual(prepare_text("\u0307").key, "\u0307")

    def test_empty_inputs_never_authorize_complete_or_prefix_matches(self):
        for canonical, observed in (("", ""), ("word", ""), ("", "word")):
            with self.subTest(canonical=canonical, observed=observed):
                evidence = self.compare(canonical, observed)
                self.assertFalse(evidence.complete)
                self.assertIsNone(evidence.prefix_distance)
        with self.assertRaises(ValueError):
            self.compare("word", "word", limit=-1)

    def test_distance_matches_standard_levenshtein_oracle_with_cutoffs(self):
        words = [
            "".join(letters)
            for length in range(4)
            for letters in itertools.product("ab :", repeat=length)
        ]
        for left, right in itertools.product(words, repeat=2):
            actual, source = prepare_text(left).key, prepare_text(right).key
            previous = list(range(len(source) + 1))
            for i, unit in enumerate(actual, 1):
                current = [i]
                for j, other in enumerate(source, 1):
                    current.append(
                        min(
                            current[-1] + 1,
                            previous[j] + 1,
                            previous[j - 1] + (unit != other),
                        )
                    )
                previous = current
            for limit in range(3):
                self.assertEqual(
                    self.compare(right, left, limit).distance,
                    min(previous[-1], limit + 1),
                    (left, right, limit),
                )


if __name__ == "__main__":
    unittest.main()
