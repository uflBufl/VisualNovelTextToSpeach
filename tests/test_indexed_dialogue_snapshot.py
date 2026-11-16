import gc
import unittest
import weakref
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.story_fixtures import write_story_index, write_verified_source_story
from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.dialogue_search import IndexedDialogueStore
from vntts.document_identity import sha256_file
from vntts.pregeneration_setup import _cached_story_index_document, open_reading_catalog
from vntts.selected_story_snapshot import (
    _source_receipts,
    create_selected_story_snapshot,
)
from vntts.story_catalog import StoryCatalogError


class IndexedDialogueSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(
            patch("vntts.pregeneration_setup.story_catalog_minimum_bytes", 0)
        )
        self.enterContext(
            patch(
                "vntts.pregeneration_setup._story_catalog_path",
                side_effect=lambda checksum, evidence: (
                    self.root / "cache" / f"{checksum}-{evidence}.sqlite3"
                ),
            )
        )
        _cached_story_index_document.cache_clear()
        self.addCleanup(_cached_story_index_document.cache_clear)
        _source_receipts.clear()
        self.addCleanup(_source_receipts.clear)

    def test_snapshot_source_lives_until_the_last_matching_clone_is_released(self):
        path = write_story_index(self.root / "source")
        preloader = ChapterVoicePreloader.load_snapshot(path, path.read_bytes())
        self.assertIsInstance(preloader.store, IndexedDialogueStore)
        source = preloader.store.catalog.source_path
        owner = weakref.ref(preloader.store.catalog)
        self.assertTrue(source.is_file())
        self.assertNotEqual(source, path)
        clone = preloader.matching_snapshot()
        del preloader
        gc.collect()
        self.assertIsNotNone(owner())
        self.assertTrue(source.is_file())
        line = clone.line_for_id("reverse1999:2")
        self.assertEqual(
            clone.resolve_indexed_complete(line.speaker, line.text)[0], line
        )
        del clone
        gc.collect()
        self.assertIsNone(owner())
        self.assertFalse(source.parent.exists())

    def test_snapshot_copies_optional_evidence_and_missing_evidence_has_no_authority(
        self,
    ):
        path = self.root / "verified" / "story-index.jsonl"
        write_verified_source_story(path)
        evidence = path.with_name("source-audio-semantic-evidence.json")
        payload = path.read_bytes()
        captured = ChapterVoicePreloader.load_snapshot(path, payload)
        self.assertIsInstance(captured.store, IndexedDialogueStore)
        copied_evidence = captured.store.catalog.source_path.with_name(evidence.name)
        self.assertEqual(copied_evidence.read_bytes(), evidence.read_bytes())
        self.assertTrue(captured.line_for_id("test:0").has_full_source_audio)
        evidence.unlink()
        self.assertTrue(captured.line_for_id("test:0").has_full_source_audio)
        without_evidence = ChapterVoicePreloader.load_snapshot(path, payload)
        self.assertIsInstance(without_evidence.store, IndexedDialogueStore)
        self.assertFalse(without_evidence.line_for_id("test:0").has_full_source_audio)

    def test_regular_indexed_source_rejects_a_changed_source_after_a_cached_lookup(
        self,
    ):
        path = write_story_index(self.root / "source")
        preloader = ChapterVoicePreloader.load_optional(path)
        self.assertIsInstance(preloader.store, IndexedDialogueStore)
        self.assertTrue(preloader.line_for_id("reverse1999:1").has_full_source_audio)
        write_story_index(path.parent, generated_text="The source has changed.")
        with self.assertRaises(StoryCatalogError):
            preloader.line_for_id("reverse1999:1")

    def test_selected_snapshot_uses_its_own_index_and_never_admits_parent_rows(self):
        path = write_story_index(self.root / "source")
        parent_checksum = sha256_file(path)
        selected = create_selected_story_snapshot(
            path,
            parent_checksum,
            ("reverse1999:3",),
            ("rhiannon",),
            self.root / "selected",
        )
        catalog = open_reading_catalog(selected)
        self.assertIsInstance(catalog.store, IndexedDialogueStore)
        self.assertEqual(catalog.store.catalog.source_path, selected)
        self.assertEqual(catalog.store.catalog.source_sha256, sha256_file(selected))
        self.assertNotEqual(catalog.store.catalog.source_sha256, parent_checksum)
        self.assertEqual(
            tuple(row.line_id for row in catalog.store.iter_rows()),
            ("reverse1999:3",),
        )
        self.assertIsNone(catalog.store.line_for_id("reverse1999:1"))
        self.assertIsNone(catalog.store.line_for_id("reverse1999:2"))
        self.assertEqual(
            tuple(row.line_id for row in catalog.store.exact_rows("A child line.")),
            ("reverse1999:3",),
        )
