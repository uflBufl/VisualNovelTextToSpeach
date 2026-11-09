import unicodedata
import unittest

from tests.test_complete_story_matching import dialogue
from vntts.chapter_voice_preload import ChapterMatch, ChapterVoicePreloader
from vntts.live_tracking import IncrementalDialogTracker
from vntts.story_text_matching import fold_latin_diacritics


class LatinDiacriticFoldingTest(unittest.TestCase):
    def test_latin_marks_are_removed_without_changing_case_or_word_boundaries(self):
        for text in (
            "Café déjà vu. Miedź!",
            "Cafe\u0301 de\u0301ja\u0300 vu. Miedz\u0301!",
        ):
            with self.subTest(text=text):
                self.assertEqual(fold_latin_diacritics(text), "Cafe deja vu. Miedz!")

    def test_non_latin_diacritics_and_compatibility_characters_are_preserved(self):
        for text in ("й", "ά", "が", "²"):
            with self.subTest(text=text):
                self.assertEqual(fold_latin_diacritics(text), text)
                self.assertEqual(
                    fold_latin_diacritics(unicodedata.normalize("NFD", text)), text
                )


class ProtectedLegacyResolverTest(unittest.TestCase):
    def test_all_prefix_entry_points_reject_changed_protected_content(self):
        for canonical, observed in (
            (
                "The lantern needs -5 coins before we can leave home.",
                "The lantern needs 5 coins",
            ),
            (
                "The lantern isn't ready for our long journey home.",
                "The lantern isn t ready",
            ),
            (
                "The lantern is not ready for our long journey home.",
                "The lantern is now ready",
            ),
            (
                "The lantern needs 123 coins before we can leave home.",
                "The lantern needs 12",
            ),
        ):
            for method in (
                "resolve_unique_prefix",
                "resolve_unique_prefix_by_text",
                "is_unique_incomplete_prefix",
            ):
                with self.subTest(method=method, observed=observed):
                    resolver = ChapterVoicePreloader([dialogue(canonical)])
                    args = (
                        (observed,) if method.endswith("by_text") else ("Ada", observed)
                    )
                    self.assertFalse(getattr(resolver, method)(*args))
                    self.assertIsNone(resolver.current_match)

    def test_all_prefix_entry_points_accept_safe_source_coverage(self):
        for canonical, observed in (
            (
                "The lantern needs -5 coins before we can leave home.",
                "The lantern needs -5 coins",
            ),
            (
                "The lantern isn't ready for our long journey home.",
                "The lantern isn’t ready",
            ),
            (
                "The café lantern glows brightly beside our home.",
                "The cafe lantern glows",
            ),
        ):
            for method in (
                "resolve_unique_prefix",
                "resolve_unique_prefix_by_text",
                "is_unique_incomplete_prefix",
            ):
                with self.subTest(method=method, observed=observed):
                    row = dialogue(canonical)
                    resolver = ChapterVoicePreloader([row])
                    args = (
                        (observed,) if method.endswith("by_text") else ("Ada", observed)
                    )
                    actual = getattr(resolver, method)(*args)
                    if method == "is_unique_incomplete_prefix":
                        self.assertIs(actual, True)
                    else:
                        self.assertIs(actual, row)

        resolver = ChapterVoicePreloader([dialogue("The lantern isn't ready.")])
        self.assertFalse(
            resolver.is_unique_incomplete_prefix("Ada", "The lantern isn’t ready!")
        )

    def test_exact_entry_points_keep_protection_and_existing_normalization_contract(
        self,
    ):
        for canonical, observed, accepted in (
            ("The lantern needs -5 coins.", "The lantern needs 5 coins.", False),
            ("The lantern needs -5 coins.", "The lantern needs +5 coins.", False),
            ("The lantern isn't ready.", "The lantern isn t ready.", False),
            ("The lantern isn't ready.", "The lantern isn’t ready.", True),
            ("The lantern needs -5 coins.", "The lantern needs −5 coins.", True),
            ("The stack-finally holds.", "The stack finally holds!", True),
            ("The lantern glows.", "Ｔｈｅ ｌａｎｔｅｒｎ ｇｌｏｗｓ．", True),
            ("The café lantern glows.", "The cafe\u0301 lantern glows.", True),
            ("The café lantern glows.", "The cafe lantern glows.", False),
        ):
            for method in ("resolve_exact_among", "resolve_exact_with_result"):
                with self.subTest(method=method, observed=observed):
                    row = dialogue(canonical)
                    resolver = ChapterVoicePreloader([row])
                    previous = ChapterMatch("previous", 1, 1.0)
                    resolver.current_match = previous
                    args = (
                        ("Ada", observed, [row.line_id])
                        if method == "resolve_exact_among"
                        else ("Ada", observed)
                    )
                    actual, _ = getattr(resolver, method)(*args)
                    self.assertIs(actual, row if accepted else None)
                    if not accepted:
                        self.assertIs(resolver.current_match, previous)

    def test_tracker_early_resolver_cannot_expand_a_changed_numeric_sign(self):
        row = dialogue("The lantern needs -5 coins before we can leave home.")
        for observed, accepted in (
            ("The lantern needs 5 coins", False),
            ("The lantern needs -5 coins", True),
        ):
            with self.subTest(observed=observed):
                resolver = ChapterVoicePreloader([row])

                tracker = IncrementalDialogTracker(
                    complete_dialogue_only=True,
                    early_dialogue_resolver=resolver.resolve_unique_prefix,
                    clock=lambda: 0,
                )
                self.assertEqual(tracker.observe("Ada", observed), [])
                chunks = tracker.observe("Ada", observed)
                self.assertEqual(
                    [chunk.text for chunk in chunks], [row.text] if accepted else []
                )


class ProtectedCropTest(unittest.TestCase):
    def test_crop_recovery_cannot_discard_protected_observed_content(self):
        row = dialogue("The quota is five.")
        resolver = ChapterVoicePreloader([row])
        for observed in (
            "not The quota is five.",
            "The quota is five. not",
            "The quota is five. 6",
            "The quota is five. with six dogs",
        ):
            with self.subTest(observed=observed):
                match = resolver.resolve_observation_among(
                    "Ada",
                    observed,
                    [row.line_id],
                    allow_prefix=True,
                    recover_crop_noise=True,
                )
                self.assertIsNone(match.line)
        for observed in ("ae The quota is five.", "Ada The quota is five."):
            with self.subTest(observed=observed):
                match = resolver.resolve_observation_among(
                    "Ada",
                    observed,
                    [row.line_id],
                    recover_crop_noise=True,
                )
                self.assertIs(match.line, row)
                self.assertTrue(match.complete)

    def test_number_in_decorative_position_needs_more_evidence_than_text(self):
        row = dialogue("The old woman raises a finger to her lips.")
        resolver = ChapterVoicePreloader([row])
        for prefix in ("7 r RPA ", "not "):
            match = resolver.resolve_observation_among(
                "Ada",
                prefix + "The old woman raises a fin",
                [row.line_id],
                allow_prefix=True,
                recover_crop_noise=True,
            )
            self.assertIsNone(match.line)


class CropSelectionTest(unittest.TestCase):
    def test_out_of_budget_candidate_cannot_disable_exact_crop_recovery(self):
        expected = dialogue("The cat stays home.", "expected")
        other = dialogue("UI The bct staxs home.", "other")
        resolver = ChapterVoicePreloader([expected, other])
        match = resolver.resolve_observation_among(
            "Ada",
            "UI The cat stays home.",
            [expected.line_id, other.line_id],
            recover_crop_noise=True,
        )
        self.assertIs(match.line, expected)
        self.assertTrue(match.complete)


if __name__ == "__main__":
    unittest.main()
