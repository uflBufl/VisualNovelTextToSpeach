import itertools
import unicodedata
import unittest

from vntts.story_text_matching import (
    compare_text,
    fold_latin_diacritics,
    prepare_text,
)


class StoryTextMatchingTest(unittest.TestCase):
    def compare(self, canonical, observed, limit=4):
        return compare_text(prepare_text(observed), prepare_text(canonical), limit)

    def test_soft_punctuation_can_split_words_without_removing_actual_boundaries(self):
        for canonical, observed in (
            ("Remember Nowa Miedź?", "Remember Nowa Mied:z?"),
            ("Remember Nowa Miedź?", "Remember Nowa Mied: z?"),
            ("Remember Nowa Miedź?", "Remember Nowa Miedz?"),
            ("A well-known traveler came home.", "A wellknown traveler came home."),
            ("A well-known traveler came home.", "A well known traveler came home."),
            ("The quota is 5.", "The qu:ota is 5."),
            ("The quota is 5.", "The quo: ta is 5."),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                evidence = self.compare(canonical, observed)
                self.assertTrue(evidence.complete)
                self.assertEqual(evidence.distance, 0)
        for canonical, observed in (
            ("Café déjà vu.", "Cafedejavu."),
            ("Now here.", "Nowhere."),
            ("A real boundary.", "Areal boundary."),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                self.assertFalse(self.compare(canonical, observed).complete)

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

    def test_protected_spans_cannot_be_changed_or_partly_matched(self):
        for canonical, observed in (
            ("The quota is -5.", "The quota is 5."),
            ("The quota is +5.", "The quota is 5."),
            ("The quota is 5.", "The quota is 6."),
            ("The quota is 1.5.", "The quota is 1,5."),
            ("The quota is 5/10.", "The quota is 5 10."),
            ("The quota is 1e5.", "The quota is 1f5."),
            ("The quota is five.", "The quota is hive."),
            ("The quota is not met.", "The quota is now met."),
            ("We cannot leave home.", "We can leave home."),
            ("We can't leave home.", "We cant leave home."),
            ("Not now.", "Nót now."),
            ("The quota is 12345.", "The quota is 123"),
            ("The quota is not met.", "The quota is no"),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                evidence = self.compare(canonical, observed)
                self.assertFalse(evidence.complete)
                self.assertIsNone(evidence.prefix_distance)

    def test_protected_spans_allow_typography_and_keep_global_competitor_distance(self):
        for canonical, observed in (
            ("The quota is -5.", "The quota is −5."),
            ("We can't leave home.", "We can’t leave home."),
            ("The quota is not met.", "The qu:ota is not met."),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                evidence = self.compare(canonical, observed)
                self.assertTrue(evidence.complete)
                self.assertEqual(evidence.distance, 0)
        changed = self.compare("The quota is -5.", "The quota is 5.")
        self.assertEqual(changed.distance, 1)
        self.assertFalse(changed.complete)
        raw = prepare_text("Not the qu:ota of −5. We can’t leave.")
        self.assertEqual(raw.protected_content, ("not", "-5", "can't"))
        self.assertNotEqual(
            raw.protected_content,
            prepare_text("The quota of -5. We can't leave.").protected_content,
        )
        for canonical, observed in (
            (
                "The marker reads A 1 before the long description of the route.",
                "The marker reads 1 A before the long description of the route.",
            ),
            (
                "The first lamp is not on and the second lamp is now on.",
                "The first lamp is now on and the second lamp is not on.",
            ),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                self.assertFalse(self.compare(canonical, observed).complete)

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

    def test_empty_inputs_never_authorize_complete_or_prefix_matches(self):
        for canonical, observed in (("", ""), ("word", ""), ("", "word")):
            with self.subTest(canonical=canonical, observed=observed):
                evidence = self.compare(canonical, observed)
                self.assertFalse(evidence.complete)
                self.assertIsNone(evidence.prefix_distance)
        with self.assertRaises(ValueError):
            self.compare("word", "word", limit=-1)

    def test_banded_distance_matches_unbounded_optional_boundary_oracle(self):
        words = [
            "".join(letters)
            for length in range(4)
            for letters in itertools.product("ab :", repeat=length)
        ]
        for left, right in itertools.product(words, repeat=2):
            actual, source = prepare_text(left).key, prepare_text(right).key
            previous = [0]
            for unit in source:
                previous.append(previous[-1] + (unit != "\x01"))
            for unit in actual:
                current = [previous[0] + (unit != "\x01")]
                for j, other in enumerate(source, 1):
                    current.append(
                        min(
                            current[-1] + (other != "\x01"),
                            previous[j] + (unit != "\x01"),
                            previous[j - 1]
                            + (
                                unit != other
                                and not (unit in "\x00\x01" and other in "\x00\x01")
                            ),
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
