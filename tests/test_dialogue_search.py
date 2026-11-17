"""Indexed retrieval must preserve the exhaustive semantic matcher result."""

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.story_index import write_story_index_document

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.dialogue_search import (
    IndexedDialogueStore,
    MemoryDialogueStore,
    text_projection,
)
from vntts.pregeneration_setup import (
    _cached_story_index_document,
    open_reading_catalog,
)
from vntts.story_text_matching import compare_text, edit_budget, prepare_text


def record(line_id, text, *, speaker="Ada", chapter="chapter", original=False):
    return {
        "record_type": "line",
        "line_id": line_id,
        "chapter": chapter,
        "speaker": speaker,
        "voice_character": speaker,
        "text": text,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "kind": "dialogue",
        "collection_id": "story",
        "collection_kind": "main_story",
        "source_audio_status": "available" if original else "absent",
        "speakable": True,
    }


class IndexedDialogueSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.addCleanup(_cached_story_index_document.cache_clear)

    def preloader(self, rows):
        path = self.root / "story-index.jsonl"
        write_story_index_document(
            path,
            {
                "game": "Synthetic Story",
                "game_id": "synthetic",
                "language": "en",
                "collections": [
                    {
                        "collection_id": "story",
                        "title": "Synthetic Story",
                        "kind": "main-story",
                        "order": 1,
                    }
                ],
            },
            [{**row, "sequence": index} for index, row in enumerate(rows, 1)],
        )
        with (
            patch("vntts.pregeneration_setup.story_catalog_minimum_bytes", 0),
            patch(
                "vntts.pregeneration_setup._story_catalog_path",
                side_effect=lambda checksum, evidence: (
                    self.root / "cache" / f"{checksum}-{evidence}.sqlite3"
                ),
            ),
        ):
            catalog = open_reading_catalog(path)
        self.assertIsInstance(catalog.store, IndexedDialogueStore)
        self.addCleanup(catalog.store.catalog.close)
        return ChapterVoicePreloader.from_store(catalog.store)

    def assert_same_resolution(self, preloader, speaker, text, **options):
        indexed = preloader.matching_snapshot()
        exhaustive = preloader.matching_snapshot()
        expected = exhaustive.resolve_observation_among(
            speaker, text, preloader.store.catalog.line_ids(), **options
        )
        actual = indexed.resolve_observation_among(speaker, text, None, **options)
        self.assertEqual(actual, expected)
        self.assertEqual(indexed.current_match, exhaustive.current_match)
        return actual

    def test_warm_match_uses_admitted_catalog_without_source_polling(self):
        preloader = self.preloader(
            [
                record(f"line:{i}", f"This is a distinct complete sentence number {i}.")
                for i in range(28)
            ]
        )
        rows = preloader.chapter_rows("chapter")
        preloader.select_line_id(rows[0].line_id)
        ids = [row.line_id for row in rows]
        snapshot = preloader.matching_snapshot()
        expected = snapshot.resolve_complete_among(
            rows[0].speaker, rows[0].text, ids, update_context=False
        )
        self.assertIsNotNone(expected)
        with (
            patch(
                "vntts.content_revision.source_signature",
                side_effect=AssertionError("source polled"),
            ),
            patch(
                "vntts.story_catalog._file_signature",
                side_effect=AssertionError("evidence polled"),
            ),
            patch.object(
                preloader.store.catalog,
                "dialogues",
                side_effect=AssertionError("cached rows rehydrated"),
            ),
        ):
            actual = snapshot.resolve_complete_among(
                rows[0].speaker, rows[0].text, ids, update_context=False
            )
        self.assertEqual(actual, expected)
        preloader.store.catalog.close()
        from vntts.story_catalog import StoryCatalogError

        with self.assertRaisesRegex(StoryCatalogError, "closed"):
            preloader.line_for_id(rows[0].line_id)

    def test_mixed_lane_raw_prefix_does_not_hide_original_crop_recovery(self):
        preloader = self.preloader(
            (
                record(
                    "synth", "Regulus Welcome home. Stay awhile.", speaker="Someone"
                ),
                record("original", "Welcome home.", speaker="Regulus", original=True),
            )
        )
        self.assertTrue(preloader.line_for_id("original").has_original_speech)
        for allow_prefix in (False, True):
            with self.subTest(allow_prefix=allow_prefix):
                match = self.assert_same_resolution(
                    preloader,
                    "Regulus",
                    "Regulus Welcome home.",
                    allow_prefix=allow_prefix,
                    recover_crop_noise=True,
                    prefer_synthesis=True,
                )
                if allow_prefix:
                    self.assertEqual(match.line.line_id, "original")
                    self.assertTrue(match.complete)

    def test_recovered_crop_prefix_is_incomplete_until_full_coverage(self):
        preloader = self.preloader(
            (record("line", "The lantern illuminated the forgotten passage."),)
        )
        for allow_prefix in (False, True):
            with self.subTest(allow_prefix=allow_prefix):
                match = self.assert_same_resolution(
                    preloader,
                    "Ada",
                    "Ada The lantern illuminated",
                    allow_prefix=allow_prefix,
                    recover_crop_noise=True,
                )
                self.assertFalse(match.complete)
                self.assertEqual(
                    match.method,
                    "expected-bounded-prefix"
                    if allow_prefix
                    else "expected-incomplete",
                )

    def test_full_and_crop_typos_preserve_semantic_resolution(self):
        text = "The lantern illuminated the forgotten passage."
        preloader = self.preloader((record("line", text),))
        typo = text.replace("illuminated", "illumlnated")
        for observed in (text, typo, f"Ada {text}", f"Ada {typo}", f"{text} EXTRA"):
            for allow_prefix in (False, True):
                with self.subTest(observed=observed, allow_prefix=allow_prefix):
                    match = self.assert_same_resolution(
                        preloader,
                        "Ada",
                        observed,
                        allow_prefix=allow_prefix,
                        recover_crop_noise=True,
                    )
                    self.assertEqual(match.line.line_id, "line")
                    self.assertTrue(match.complete)

    def test_short_common_and_symbol_text_keep_all_competitors(self):
        preloader = self.preloader(
            (
                record("ada", "Go."),
                record("bea", "Go.", speaker="Bea", chapter="other"),
                record("longer", "Go. Stay here.", speaker="Bea"),
                record("symbols", "..."),
            )
        )
        for speaker, text in (
            ("Ada", "Go."),
            ("", "Go."),
            ("Ada", "G"),
            ("Ada", "..."),
            ("Ada", "?!"),
        ):
            for allow_prefix in (False, True):
                with self.subTest(
                    speaker=speaker, text=text, allow_prefix=allow_prefix
                ):
                    self.assert_same_resolution(
                        preloader,
                        speaker,
                        text,
                        allow_prefix=allow_prefix,
                        recover_crop_noise=True,
                    )

    def test_short_and_unicode_full_prefix_and_crop_resolution(self):
        preloader = self.preloader(
            (
                record("short", "Go."),
                record("latin", "Read the café notice beside the gate."),
                record("chinese", "你好世界。"),
                record("cyrillic", "Старинный фонарь освещает забытый проход."),
            )
        )
        observations = (
            "Go.",
            "G",
            "Ada Go.",
            "Read the cafe notice beside the gate.",
            "Read the cafe notlce beside the gate.",
            "Read the cafe notlce",
            "Ada Read the café notlce",
            "Read the café notice beside the gate. EXTRA",
            "你好世界。",
            "你好",
            "Ada 你好世界。",
            "Старинный фонарь освещает забытый проход.",
            "Старинный фонарь освещает",
            "Ada Старинный фонарь освещает",
        )
        for observed in observations:
            for allow_prefix in (False, True):
                for prefer_synthesis in (False, True):
                    with self.subTest(
                        observed=observed,
                        allow_prefix=allow_prefix,
                        prefer_synthesis=prefer_synthesis,
                    ):
                        self.assert_same_resolution(
                            preloader,
                            "Ada",
                            observed,
                            allow_prefix=allow_prefix,
                            recover_crop_noise=True,
                            prefer_synthesis=prefer_synthesis,
                        )

    def test_memory_candidate_queries_reuse_source_projections(self):
        preloader = self.preloader(
            (
                record("latin", "Read the café notice beside the gate."),
                record("cyrillic", "Старинный фонарь освещает забытый проход."),
                record("original", "A different answer awaits us.", original=True),
            )
        )
        memory = MemoryDialogueStore(preloader.store.iter_rows())
        observations = (
            ("Read the cafe notice", {}),
            ("Старинный фонарь освещает", {"chapter": "chapter"}),
            ("A different answer awaits us", {"original": True, "prefix": False}),
        )
        expected = tuple(
            preloader.store.candidates(observed, **options)
            for observed, options in observations
        )
        with patch("vntts.dialogue_search.prepare_text", wraps=prepare_text) as prepare:
            actual = tuple(
                memory.candidates(observed, **options)
                for observed, options in observations
            )
        self.assertEqual(actual, expected)
        self.assertEqual(
            [call.args[0] for call in prepare.call_args_list],
            [observed for observed, _options in observations],
        )

    def test_exact_synthesis_keeps_full_original_outside_observed_edit_budget(self):
        preloader = self.preloader(
            (
                record("synth", "Go", speaker="Bea"),
                record("original", "Gao", original=True),
            )
        )
        observed = prepare_text("Go")
        source = prepare_text("Gao")
        evidence = compare_text(observed, source)
        self.assertTrue(evidence.complete)
        self.assertGreater(evidence.distance, edit_budget(observed))
        self.assertLessEqual(evidence.distance, edit_budget(source))
        candidates = preloader._indexed_candidates(
            "Go", allow_prefix=False, prefer_synthesis=True
        )
        self.assertIn("original", {row.line_id for row in candidates})
        match = self.assert_same_resolution(
            preloader, "Ada", "Go", prefer_synthesis=True
        )
        self.assertEqual(match.line.line_id, "synth")
        self.assertTrue(match.complete)

    def test_numeric_and_negation_edits_use_the_common_metric(self):
        preloader = self.preloader(
            (
                record("number", "The lantern needs -12 coins."),
                record("decimal", "The lantern needs 1.2 coins."),
                record("negation", "The lantern is not ready."),
            )
        )
        for line_id, text in (
            ("number", "The lantern needs 12 coins."),
            ("decimal", "The lantern needs 1,2 coins."),
            ("negation", "The lantern is now ready."),
        ):
            for observed in (text, f"Ada {text}"):
                with self.subTest(observed=observed):
                    match = self.assert_same_resolution(
                        preloader,
                        "Ada",
                        observed,
                        allow_prefix=True,
                        recover_crop_noise=True,
                    )
                    self.assertEqual(match.line.line_id, line_id)
                    self.assertTrue(match.complete)

    def test_real_marcus_ocr_recovers_the_same_indexed_line(self):
        preloader = self.preloader(
            (
                record(
                    "reverse1999:100631:57",
                    '"Even if I die, I\'ll still turn Kakania completely against Isolde."',
                    speaker="Marcus",
                ),
                record("other", "A different answer awaits us.", speaker="Marcus"),
            )
        )
        match = self.assert_same_resolution(
            preloader,
            "Marcus",
            "Even if I die, [1l still turn Kakania completely against Isolde.",
            allow_prefix=True,
        )
        self.assertEqual(match.line.line_id, "reverse1999:100631:57")
        self.assertTrue(match.complete)

    def test_long_full_and_prefix_edits_keep_default_candidate_recall(self):
        text = (
            "The librarian catalogued every forgotten volume beside the western "
            "window while distant thunder echoed through the deserted courtyard. "
            "An ancient map described the winding passage beneath the tower and "
            "the hidden chamber beyond its heavy iron gate."
        )
        preloader = self.preloader(
            (
                record("synth", text),
                record("original", text, speaker="Bea", chapter="other", original=True),
                record("unrelated", "A different answer awaits us."),
            )
        )
        rows = tuple(preloader.store.iter_rows())
        memory = MemoryDialogueStore(rows)
        for prefix in (False, True):
            base = text[:210] if prefix else text
            for operation in ("insert", "delete", "replace"):
                observed = base
                for position in reversed(range(15, 150, 20)):
                    observed = (
                        observed[:position]
                        + ("x" if operation != "delete" else "")
                        + observed[position + (operation != "insert") :]
                    )
                shape = prepare_text(observed)
                evidence = compare_text(shape, prepare_text(text))
                if prefix:
                    self.assertIsNotNone(evidence.prefix_distance)
                    self.assertGreater(evidence.prefix_distance, 4)
                    self.assertLessEqual(evidence.prefix_distance, edit_budget(shape))
                else:
                    self.assertTrue(evidence.complete)
                    self.assertGreater(evidence.distance, 4)
                    self.assertLessEqual(
                        evidence.distance, edit_budget(prepare_text(text))
                    )
                self.assertEqual(
                    preloader.store.candidates(observed, prefix=prefix, distance=4), ()
                )
                self.assertEqual(
                    preloader.store.candidates(observed, prefix=prefix, distance=8),
                    preloader.store.candidates(observed, prefix=prefix),
                )
                for chapter in (None, "chapter", "other"):
                    for original in (None, False, True):
                        options = dict(
                            prefix=prefix, chapter=chapter, original=original
                        )
                        with self.subTest(operation=operation, **options):
                            expected = memory.candidates(observed, **options)
                            actual = preloader.store.candidates(observed, **options)
                            self.assertEqual(actual, expected)
                            witnesses = {
                                row.line_id
                                for row in rows
                                if row.text == text
                                and (chapter is None or row.chapter == chapter)
                                and (
                                    original is None
                                    or row.has_original_speech == original
                                )
                            }
                            self.assertEqual({row.line_id for row in actual}, witnesses)
                match = self.assert_same_resolution(
                    preloader, "Ada", observed, allow_prefix=prefix
                )
                self.assertEqual(match.line.line_id, "synth")
                self.assertEqual(match.complete, not prefix)

    def test_projection_preserves_shared_word_boundaries(self):
        self.assertEqual(
            text_projection("Read the café-notice."), "read the cafe notice"
        )

    def test_full_coverage_outside_budget_keeps_the_valid_prefix(self):
        source = "z" + "a" * 36 + "bcd"
        observed = "z" + "a" * 35 + "d"
        preloader = self.preloader((record("line", source),))
        evidence = compare_text(prepare_text(observed), prepare_text(source))
        self.assertTrue(evidence.complete)
        self.assertGreater(evidence.distance, edit_budget(prepare_text(source)))
        self.assertEqual(evidence.prefix_distance, 1)
        match = self.assert_same_resolution(
            preloader, "Ada", observed, allow_prefix=True
        )
        self.assertEqual(match.line.line_id, "line")
        self.assertTrue(match.prefix)
        self.assertFalse(match.complete)

    def test_original_and_synthesis_lanes_preserve_named_ownership(self):
        text = "The bridge will hold until morning."
        preloader = self.preloader(
            (
                record("synth", text),
                record("original", text, speaker="Bea", original=True),
            )
        )
        for speaker, expected in (("Ada", "synth"), ("Bea", "original")):
            for allow_prefix in (False, True):
                for prefer_synthesis in (False, True):
                    with self.subTest(
                        speaker=speaker,
                        allow_prefix=allow_prefix,
                        prefer_synthesis=prefer_synthesis,
                    ):
                        match = self.assert_same_resolution(
                            preloader,
                            speaker,
                            text,
                            allow_prefix=allow_prefix,
                            recover_crop_noise=True,
                            prefer_synthesis=prefer_synthesis,
                        )
                        self.assertEqual(match.line.line_id, expected)
                        self.assertTrue(match.complete)

    def test_current_owner_and_full_observation_keep_context_guards(self):
        text = "Welcome home."
        preloader = self.preloader(
            (
                record("current", text),
                record("successor", "A separate message follows."),
                record("foreign", f"{text} Stay awhile.", chapter="foreign"),
            )
        )
        preloader.select_line_id("current")
        match = self.assert_same_resolution(
            preloader, "Ada", text, fully_observed_line_id="current"
        )
        self.assertEqual(match.line.line_id, "current")
        for observed in ("Welcome hom", "elcome home."):
            with self.subTest(observed=observed):
                self.assert_same_resolution(
                    preloader,
                    "Ada",
                    observed,
                    allow_prefix=True,
                    recover_crop_noise=True,
                    fully_observed_line_id="current",
                )

    def test_projected_full_and_prefix_candidates_keep_semantic_witnesses(self):
        texts = (
            "A.",
            "Go.",
            "Read the café notice beside the gate.",
            "你好，世界。",
            "Старинный фонарь освещает забытый проход.",
            "The lantern is not ready.",
            "The ticket needs -12 coins.",
        )
        preloader = self.preloader(
            tuple(record(f"line-{index}", text) for index, text in enumerate(texts))
        )
        rows = tuple(preloader.store.iter_rows())
        observations = {"cafe", "你好", "Read the cafe notice beside the gate."}
        for text in texts:
            for base in (text, text[: max(1, len(text) // 2)]):
                observations.add(base)
                for edits in range(1, 5):
                    for operation in ("insert", "delete", "replace"):
                        value = list(base)
                        for index in range(edits):
                            position = min(
                                len(value) - 1, (index + 1) * len(value) // (edits + 1)
                            )
                            if operation == "insert":
                                value.insert(max(0, position), "x")
                            elif value and operation == "delete":
                                del value[position]
                            elif value:
                                value[position] = "x"
                        observations.add("".join(value))
        for observed in sorted(observations):
            shape = prepare_text(observed)
            if not shape.alphanumeric_length:
                continue
            for prefix in (False, True):
                for distance in (0, 4):
                    expected = set()
                    for row in rows:
                        evidence = compare_text(
                            shape, prepare_text(row.text), limit=distance
                        )
                        if (evidence.complete and evidence.distance <= distance) or (
                            prefix
                            and evidence.prefix_distance is not None
                            and evidence.prefix_distance <= distance
                        ):
                            expected.add(row.line_id)
                    with self.subTest(
                        observed=observed, prefix=prefix, distance=distance
                    ):
                        found = {
                            row.line_id
                            for row in preloader.store.candidates(
                                observed, prefix=prefix, distance=distance
                            )
                        }
                        self.assertLessEqual(expected, found)


if __name__ == "__main__":
    unittest.main()
