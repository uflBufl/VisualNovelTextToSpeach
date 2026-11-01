import unittest
from unittest.mock import Mock

from vntts.dialog import ocr_texts_equivalent
from vntts.dialog_capture import DialogFrameEquivalence
from vntts.ocr import OCRResult


def result(text="Hello, world!", speaker="Ada", confidence=95):
    return OCRResult(speaker, text, confidence, "sauvola", 1)


class OCRTextEquivalenceTest(unittest.TestCase):
    def test_normalization_and_single_edit_budget(self):
        for left, right in (
            ("Hello  world", "hello\nworld"),
            ("Ａda", "ada"),
            ("Hello world", "Hello wor1d"),
            ("Ada", "Adas"),
        ):
            with self.subTest(left=left, right=right):
                self.assertTrue(ocr_texts_equivalent(left, right))
                self.assertTrue(ocr_texts_equivalent(right, left))
        self.assertFalse(ocr_texts_equivalent("Al", "Ali"))
        self.assertFalse(ocr_texts_equivalent("Hello world", "Hello w0r1d"))

    def test_punctuation_is_supplemental_and_preserves_symbol_only_replies(self):
        self.assertFalse(ocr_texts_equivalent("cluttered,.room:", "cluttered, room. ."))
        self.assertTrue(
            ocr_texts_equivalent(
                "cluttered,.room:", "cluttered, room. .", punctuation_fallback=True
            )
        )
        self.assertTrue(
            ocr_texts_equivalent(
                "| will wait here.", "! will wait here.", punctuation_fallback=True
            )
        )
        for a, b in (("...", "?!"), ("♪", "?"), ("I can't go.", "I can go.")):
            with self.subTest(a=a, b=b):
                self.assertFalse(ocr_texts_equivalent(a, b, punctuation_fallback=True))

    def test_accepted_punctuation_identity_tradeoff(self):
        self.assertTrue(
            ocr_texts_equivalent("Really?!", "Really...", punctuation_fallback=True)
        )
        self.assertTrue(
            ocr_texts_equivalent(
                "Read section 1.2.3", "Read section 1,2,3", punctuation_fallback=True
            )
        )


class OCRFrameEquivalenceTest(unittest.TestCase):
    def setUp(self):
        self.context = ("run", "owner")
        self.compare = DialogFrameEquivalence(60, lambda: self.context)
        self.before, self.after = object(), object()

    def record(self, before, after):
        self.compare.record(self.before, before, self.context)
        self.compare.record(self.after, after, self.context)

    def test_confident_raw_pair_uses_text_without_mutation(self):
        before, after = result("cluttered,.room:"), result("cluttered, room. .")
        self.record(before, after)
        self.assertTrue(self.compare(self.before, self.after))
        self.assertEqual(before.text, "cluttered,.room:")
        self.assertEqual(after.text, "cluttered, room. .")

    def test_confident_different_text_or_name_rejects_evidence(self):
        for after in (result("A different phrase."), result(speaker="Beatrice")):
            with self.subTest(after=after):
                self.record(result(), after)
                self.assertFalse(self.compare(self.before, self.after))

    def test_missing_or_uncertain_ocr_never_authorizes_equivalence(self):
        for after in (
            None,
            result(confidence=59),
            result(text=""),
            result(text="???", confidence=0),
        ):
            with self.subTest(after=after):
                self.compare = DialogFrameEquivalence(60, lambda: self.context)
                self.compare.record(self.before, result(), self.context)
                if after is not None:
                    self.compare.record(self.after, after, self.context)
                self.assertFalse(self.compare(self.before, self.after))

    def test_changed_run_owner_or_missing_context_rejects_cached_evidence(self):
        self.record(result(), result())
        for context in (("other-run", "owner"), ("run", "other-owner"), None):
            with self.subTest(context=context):
                self.context = context
                self.assertFalse(self.compare(self.before, self.after))

    def test_cache_is_bounded_and_does_not_slide_the_original_anchor(self):
        self.compare.record(self.before, result("Mira"), self.context)
        related = object()
        self.compare.record(related, result("Mila"), self.context)
        self.assertTrue(self.compare(self.before, related))
        for _ in range(24):
            frame = object()
            self.compare.record(frame, result("Milo"), self.context)
            self.assertFalse(self.compare(self.before, frame))
        stale = frame
        for _ in range(8):
            self.compare.record(object(), result(), self.context)
        self.assertFalse(self.compare(self.before, stale))

    def test_context_change_during_semantic_comparison_invalidates_it(self):
        self.record(result(), result())
        self.compare.context = Mock(side_effect=[self.context, ("new-run", "owner")])
        self.assertFalse(self.compare(self.before, self.after))

    def test_confidence_setting_is_respected(self):
        compare = DialogFrameEquivalence(97, Mock(return_value=self.context))
        compare.record(self.before, result(), self.context)
        compare.record(self.after, result(), self.context)
        self.assertFalse(compare(self.before, self.after))
