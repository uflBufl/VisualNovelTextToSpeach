import hashlib
import unittest
from dataclasses import replace
from threading import Event
from unittest.mock import patch

from tests import test_story_recognition_concurrency as concurrency
from vntts.chapter_voice_preload import (
    ChapterDialogue,
    ChapterMatch,
    ChapterVoicePreloader,
    StoryMatchingCancelled,
)
from vntts.ocr import OCRResult
from vntts.story_text_matching import compare_text


class NoCatalogScan(tuple):
    def __iter__(self):
        raise AssertionError("Indexed discovery must not scan unrelated dialogue")


def dialogue(text, line_id="line", *, speaker="Ada", chapter="discovered"):
    return ChapterDialogue(
        line_id, chapter, 1, speaker, text, hashlib.sha256(text.encode()).hexdigest()
    )


class IndexedStoryDiscoveryTests(unittest.TestCase):
    def test_exact_and_normalized_discovery_keep_speaker_and_text_only_results(self):
        row = dialogue("The stack-finally arrangement will hold.")
        for speaker, text, method in (
            ("Ada", row.text, "expected-exact"),
            ("Unknown", row.text, "expected-text-only"),
            (
                "Ada",
                row.text.replace("stack-finally", "stack finally"),
                "expected-normalized-exact",
            ),
            (
                "",
                row.text.replace("stack-finally", "stack finally"),
                "expected-normalized-text-only",
            ),
        ):
            with self.subTest(speaker=speaker, text=text):
                preloader = ChapterVoicePreloader((row,))
                selected, result = preloader.resolve_indexed_complete(speaker, text)
                self.assertIs(selected, row)
                self.assertEqual(result, method)

    def test_normalized_keys_cannot_hide_changed_protected_content(self):
        for canonical, observed in (
            ("The lantern needs -12 coins.", "The lantern needs 12 coins."),
            ("The lantern needs 1.2 coins.", "The lantern needs 1,2 coins."),
            ("The lantern is not ready.", "The lantern is now ready."),
        ):
            with self.subTest(canonical=canonical, observed=observed):
                preloader = ChapterVoicePreloader((dialogue(canonical),))
                self.assertIsNone(
                    preloader.resolve_indexed_complete("Ada", observed)[0]
                )
                self.assertIsNone(preloader.current_match)

    def test_symbol_only_discovery_requires_the_exact_punctuation(self):
        row = dialogue("...")
        preloader = ChapterVoicePreloader((row,))
        self.assertIs(preloader.resolve_indexed_complete("Ada", "...")[0], row)
        for text in ("?!", "..", ""):
            with self.subTest(text=text):
                self.assertIsNone(preloader.resolve_indexed_complete("Ada", text)[0])

    def test_duplicate_positions_remain_ambiguous_across_chapters(self):
        first = dialogue("A complete discovery.", "first", chapter="first")
        second = replace(first, line_id="second", chapter="second")
        preloader = ChapterVoicePreloader((first, second))
        selected, result = preloader.resolve_indexed_complete("Ada", first.text)
        self.assertIsNone(selected)
        self.assertEqual(result, "expected-ambiguous")
        self.assertIsNone(preloader.current_match)

    def test_duplicate_text_prefers_the_current_position(self):
        first = dialogue("A complete discovery.", "first", chapter="first")
        second = replace(first, line_id="second", chapter="second")
        for current in (first, second):
            with self.subTest(current=current.line_id):
                preloader = ChapterVoicePreloader((first, second))
                preloader.select_line_id(current.line_id)
                previous = preloader.current_match
                selected, result = preloader.resolve_indexed_complete(
                    "Ada", first.text, update_context=False
                )
                self.assertIs(selected, current)
                self.assertEqual(result, "expected-exact")
                self.assertIs(preloader.current_match, previous)

    def test_named_speaker_disambiguates_complete_text(self):
        first = dialogue("A complete discovery.", "first")
        second = replace(first, line_id="second", speaker="Bea", chapter="other")
        preloader = ChapterVoicePreloader((first, second))
        self.assertIs(preloader.resolve_indexed_complete("Bea", first.text)[0], second)
        self.assertEqual(preloader.current_match.chapter, second.chapter)
        preloader.current_match = None
        self.assertIsNone(preloader.resolve_indexed_complete("", first.text)[0])
        self.assertIsNone(preloader.current_match)

    def test_discovery_keeps_synthesis_preference_with_a_current_position(self):
        generated = dialogue("A complete discovery.", "generated")
        original = replace(
            generated,
            line_id="original",
            chapter="other",
            source_audio_status="available",
            source_audio_id="recording",
            source_audio_authoritative=True,
            source_audio_completeness="full",
            source_audio_duration_seconds=1.0,
        )
        preloader = ChapterVoicePreloader((generated, original))
        preloader.select_line_id(original.line_id)
        selected, method = preloader.resolve_indexed_complete(
            "Ada", generated.text, prefer_synthesis=True
        )
        self.assertIs(selected, generated)
        self.assertEqual(method, "expected-exact")

    def test_discovery_checks_longer_continuations_in_the_candidate_chapter(self):
        short = dialogue("The lantern illuminated the forgotten passage.", "short")
        for speaker, expected in (("Ada", None), ("Bea", short)):
            with self.subTest(continuation_speaker=speaker):
                longer = dialogue(
                    short.text[:-1] + " beside our home.", "longer", speaker=speaker
                )
                preloader = ChapterVoicePreloader((short, longer))
                preloader.current_match = ChapterMatch("previous", 1, 1.0)
                selected, result = preloader.resolve_indexed_complete("Ada", short.text)
                self.assertIs(selected, expected)
                self.assertEqual(
                    result, "expected-exact" if expected else "expected-incomplete"
                )

    def test_incomplete_identity_is_not_a_discovery_candidate(self):
        for row in (
            replace(dialogue("A complete discovery."), line_id=None),
            replace(dialogue("A complete discovery."), text_sha256=None),
        ):
            with self.subTest(row=row):
                preloader = ChapterVoicePreloader((row,))
                self.assertIsNone(
                    preloader.resolve_indexed_complete(row.speaker, row.text)[0]
                )

    def test_original_only_line_remains_discoverable_when_preferring_synthesis(self):
        original = replace(
            dialogue("A complete original recording."),
            source_audio_status="available",
            source_audio_id="original",
            source_audio_authoritative=True,
            source_audio_completeness="full",
            source_audio_duration_seconds=1.0,
        )
        generated = dialogue("A different generated recording.", "generated")
        preloader = ChapterVoicePreloader((original, generated))
        selected, result = preloader.resolve_indexed_complete(
            original.speaker, original.text, prefer_synthesis=True
        )
        self.assertIs(selected, original)
        self.assertEqual(result, "expected-exact")

    def test_context_is_published_only_when_requested(self):
        row = dialogue("A complete discovery.")
        preloader = ChapterVoicePreloader((row,))
        previous = ChapterMatch("previous", 8, 1.0)
        preloader.current_match = previous
        self.assertIs(
            preloader.resolve_indexed_complete("Ada", row.text, update_context=False)[
                0
            ],
            row,
        )
        self.assertIs(preloader.current_match, previous)
        preloader.resolve_indexed_complete("Ada", row.text)
        self.assertEqual(preloader.current_match.chapter, row.chapter)

    def test_cancelled_discovery_does_not_publish_context(self):
        row = dialogue("A complete discovery.")
        preloader = ChapterVoicePreloader((row,))
        stopped = Event()
        snapshot = preloader.matching_snapshot(cancelled=stopped.is_set)

        def stop_during_comparison(*args, **kwargs):
            stopped.set()
            return compare_text(*args, **kwargs)

        with patch(
            "vntts.chapter_voice_preload.compare_text",
            side_effect=stop_during_comparison,
        ):
            with self.assertRaises(StoryMatchingCancelled):
                snapshot.resolve_indexed_complete("Ada", row.text)
        self.assertIsNone(preloader.current_match)
        self.assertIsNone(snapshot.current_match)
        self.assertEqual(preloader.last_resolution_diagnostics, {})

    def test_discovery_never_iterates_the_catalog_or_unrelated_chapters(self):
        row = dialogue("A complete discovery.")
        unrelated = dialogue("An unrelated catalog row.", "unrelated", chapter="other")
        preloader = ChapterVoicePreloader((row, unrelated))
        preloader.dialogue = NoCatalogScan(preloader.dialogue)
        chapter_rows = preloader.chapter_rows

        def bounded_chapter(chapter):
            self.assertNotEqual(chapter, unrelated.chapter)
            return chapter_rows(chapter)

        with patch.object(preloader, "chapter_rows", side_effect=bounded_chapter):
            self.assertIs(preloader.resolve_indexed_complete("Ada", row.text)[0], row)
            self.assertIsNone(
                preloader.resolve_indexed_complete("Ada", "A noisy incomplete prefix")[
                    0
                ]
            )

    def controller(self):
        fixture = concurrency.StoryRecognitionConcurrencyTests()
        fixture.setUp()
        return fixture, fixture.controller()

    def test_noisy_known_chapter_prefix_does_not_scan_the_catalog(self):
        fixture, controller = self.controller()
        preloader = controller.chapter_voice_preloader
        preloader.dialogue = NoCatalogScan(preloader.dialogue)
        raw = fixture.raw("Abh! Tosca ...! You're turnlng")
        resolution = {}
        self.assertIs(
            controller._resolve_story_ocr_result(raw, resolution=resolution), raw
        )
        self.assertIsNone(resolution["line"])
        self.assertEqual(preloader.current_match.sequence, fixture.previous.sequence)
        controller._request_original_speech.assert_not_called()

    def test_exact_foreign_line_is_discovered_without_catalog_iteration(self):
        fixture, controller = self.controller()
        preloader = controller.chapter_voice_preloader
        preloader.dialogue = NoCatalogScan(preloader.dialogue)
        resolution = {}
        controller._resolve_story_ocr_result(
            OCRResult("Other", fixture.foreign.text, 95, "fixture", 1),
            resolution=resolution,
        )
        self.assertIs(resolution["line"], fixture.foreign)
        self.assertEqual(preloader.current_match.chapter, fixture.foreign.chapter)

    def test_current_chapter_continuation_vetoes_exact_foreign_short_line(self):
        fixture, controller = self.controller()
        short = dialogue("Repeat.")
        longer = replace(
            dialogue("Repeat. This is the full message.", "longer"),
            chapter=fixture.previous.chapter,
            sequence=fixture.previous.sequence + 1,
        )
        preloader = ChapterVoicePreloader((fixture.previous, longer, short))
        preloader.select_line_id(fixture.previous.line_id)
        preloader.dialogue = NoCatalogScan(preloader.dialogue)
        controller.chapter_voice_preloader = preloader
        raw = OCRResult(short.speaker, short.text, 95, "fixture", 1)
        resolution = {}
        self.assertIs(
            controller._resolve_story_ocr_result(raw, resolution=resolution), raw
        )
        self.assertIsNone(resolution["line"])
        self.assertEqual(resolution["match_result"], "expected-incomplete")
        self.assertEqual(preloader.current_match.chapter, fixture.previous.chapter)

    def test_typo_only_foreign_line_waits_for_exact_text_but_startup_stays_fuzzy(self):
        fixture, controller = self.controller()
        observed = fixture.foreign.text.replace("other", "otber")
        raw = OCRResult("Other", observed, 95, "fixture", 1)
        resolution = {}
        self.assertIs(
            controller._resolve_story_ocr_result(raw, resolution=resolution), raw
        )
        self.assertIsNone(resolution["line"])
        self.assertEqual(
            controller.chapter_voice_preloader.current_match.chapter,
            fixture.previous.chapter,
        )
        controller.chapter_voice_preloader.current_match = None
        controller._resolve_story_ocr_result(raw, resolution=resolution)
        self.assertIs(resolution["line"], fixture.foreign)


if __name__ == "__main__":
    unittest.main()
