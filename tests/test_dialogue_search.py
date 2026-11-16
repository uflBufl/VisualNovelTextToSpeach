"""Indexed retrieval must preserve the exhaustive semantic matcher result."""

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.story_index import write_story_index_document

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.dialogue_search import IndexedDialogueStore
from vntts.pregeneration_setup import (
    _cached_story_index_document,
    open_reading_catalog,
)
from vntts.story_text_matching import compare_text, prepare_text


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

    def test_numeric_and_negation_changes_never_gain_canonical_authority(self):
        preloader = self.preloader(
            (
                record("number", "The lantern needs -12 coins."),
                record("decimal", "The lantern needs 1.2 coins."),
                record("negation", "The lantern is not ready."),
            )
        )
        for text in (
            "The lantern needs 12 coins.",
            "The lantern needs 1,2 coins.",
            "The lantern is now ready.",
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
                    self.assertIsNone(match.line)
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
                            and evidence.prefix_distance <= min(distance, 3)
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
