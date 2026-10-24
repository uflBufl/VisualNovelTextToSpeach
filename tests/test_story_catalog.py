import hashlib
import sqlite3
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.story_fixtures import write_story_index
from vntts.pregeneration_setup import (
    PregenerationJobStore,
    StoryContentChanged,
    _cached_story_index_document,
    _source_audio_evidence_payload,
    inspect_story_index,
    story_catalog_schema_version,
)
from vntts.story_catalog import LazyLineIds, StoryCatalog, StoryCatalogError
from vntts.versioned_json import write_versioned_json


class StoryCatalogTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = write_story_index(self.root / "content")
        self.catalog = self.root / "cache" / "catalog.sqlite3"
        self.minimum = patch("vntts.pregeneration_setup.story_catalog_minimum_bytes", 0)
        self.location = patch(
            "vntts.pregeneration_setup._story_catalog_path", return_value=self.catalog
        )
        self.minimum.start()
        self.location.start()
        self.addCleanup(self.minimum.stop)
        self.addCleanup(self.location.stop)
        self.addCleanup(_cached_story_index_document.cache_clear)
        self.first = inspect_story_index(self.path)
        _cached_story_index_document.cache_clear()

    def test_restart_loads_summaries_and_only_selected_line_ids(self):
        original = StoryCatalog.line_ids
        with (
            patch(
                "vntts.pregeneration_setup._cached_story_index_document",
                side_effect=AssertionError("canonical parse on cache hit"),
            ),
            patch.object(
                StoryCatalog, "line_ids", autospec=True, side_effect=original
            ) as fetch,
        ):
            warm = inspect_story_index(self.path)
            self.assertEqual([value.line_count for value in warm.selections], [2, 1])
            self.assertEqual([len(value.line_ids) for value in warm.selections], [2, 1])
            fetch.assert_not_called()
            job = PregenerationJobStore(self.root / "jobs").create_or_resume(
                warm, ("rhiannon",)
            )
            self.assertEqual(job.selected_line_ids, ("reverse1999:3",))
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[1], "rhiannon")
            self.assertIsNone(warm.selections[0].line_ids._values)

    def test_lazy_ids_keep_tuple_equality_hash_slice_and_asdict_contract(self):
        selection = inspect_story_index(self.path).selections[0]
        expected = self.first.selections[0].line_ids
        self.assertIsInstance(selection.line_ids, LazyLineIds)
        self.assertEqual(selection.line_ids, expected)
        self.assertEqual(expected, selection.line_ids)
        self.assertEqual(hash(selection.line_ids), hash(expected))
        self.assertEqual(selection.line_ids[:1], expected[:1])
        self.assertIsInstance(asdict(selection)["line_ids"], tuple)
        self.assertEqual(selection, self.first.selections[0])

    def test_old_json_cache_is_ignored_and_preserved_while_source_is_rebuilt(self):
        self.catalog.unlink()
        legacy = self.catalog.with_suffix(".json")
        document = self.document()
        document["game"] = "Untrusted old summary"
        write_versioned_json(legacy, story_catalog_schema_version, document)
        original = legacy.read_bytes()
        rebuilt = inspect_story_index(self.path)
        self.assertEqual(rebuilt.game, self.first.game)
        self.assertEqual(rebuilt.selections, self.first.selections)
        self.assertEqual(legacy.read_bytes(), original)
        self.assertTrue(self.catalog.exists())

    def test_corrupt_and_future_sql_are_preserved_while_canonical_content_rebuilds(
        self,
    ):
        for future in (False, True):
            with self.subTest(future=future):
                if future:
                    self.catalog.unlink()
                    StoryCatalog(self.catalog).save(self.document())
                    with closing(sqlite3.connect(self.catalog)) as connection:
                        connection.execute("PRAGMA user_version=99")
                else:
                    self.catalog.write_bytes(b"broken database")
                original = self.catalog.read_bytes()
                _cached_story_index_document.cache_clear()
                rebuilt = inspect_story_index(self.path)
                self.assertEqual(rebuilt.selections, self.first.selections)
                self.assertEqual(self.catalog.read_bytes(), original)

    def test_source_and_evidence_identity_are_checked_without_line_fetch(self):
        storage = StoryCatalog(self.catalog)
        document = self.document()
        with patch.object(
            StoryCatalog, "line_ids", side_effect=AssertionError("line fetch")
        ):
            self.assertIsNone(
                storage.load(
                    "changed", document["source_audio_semantic_evidence_sha256"], 8
                )
            )
            self.assertIsNone(storage.load(self.first.story_index_sha256, "changed", 8))
            self.assertIsNotNone(
                storage.load(
                    self.first.story_index_sha256,
                    document["source_audio_semantic_evidence_sha256"],
                    8,
                )
            )

    def test_invalid_and_future_legacy_cache_are_preserved(self):
        for future in (False, True):
            with self.subTest(future=future):
                self.catalog.unlink()
                legacy = self.catalog.with_suffix(".json")
                if future:
                    document = self.document()
                    document["schema_version"] = 99
                    write_versioned_json(legacy, 99, document)
                else:
                    legacy.write_bytes(b"broken JSON")
                original = legacy.read_bytes()
                _cached_story_index_document.cache_clear()
                rebuilt = inspect_story_index(self.path)
                self.assertEqual(rebuilt.selections, self.first.selections)
                self.assertEqual(legacy.read_bytes(), original)
                self.assertTrue(self.catalog.exists())

    def test_late_deleted_lines_recover_from_verified_source(self):
        warm = inspect_story_index(self.path)
        self.delete_lines("rhiannon")
        self.assertEqual(tuple(warm.selections[1].line_ids), ("reverse1999:3",))

    def test_late_corruption_with_changed_source_or_evidence_requires_refresh(self):
        for evidence_changed in (False, True):
            with self.subTest(evidence_changed=evidence_changed):
                self.catalog.unlink()
                StoryCatalog(self.catalog).save(self.document())
                warm = inspect_story_index(self.path)
                self.delete_lines("rhiannon")
                changed = (
                    self.path.parent / "source-audio-semantic-evidence.json"
                    if evidence_changed
                    else self.path
                )
                original = changed.read_bytes()
                changed.write_bytes(original + b" ")
                try:
                    with self.assertRaises(StoryContentChanged):
                        tuple(warm.selections[1].line_ids)
                finally:
                    changed.write_bytes(original)

    def test_interrupted_replacement_keeps_previous_complete_cache(self):
        original = self.catalog.read_bytes()
        with patch.object(
            StoryCatalog, "_write", side_effect=RuntimeError("interrupted")
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                StoryCatalog(self.catalog).save(self.document())
        self.assertEqual(self.catalog.read_bytes(), original)
        self.assertEqual(list(self.catalog.parent.glob(".story-catalog-*")), [])

    def test_missing_read_creates_nothing_and_linked_sidecar_is_rejected(self):
        missing = self.root / "absent" / "catalog.sqlite3"
        self.assertIsNone(StoryCatalog(missing).load("a", "b", 8))
        self.assertFalse(missing.parent.exists())
        sidecar = self.catalog.with_name(self.catalog.name + "-journal")
        sidecar.symlink_to(self.path)
        with self.assertRaises(StoryCatalogError):
            StoryCatalog(self.catalog).load("a", "b", 8)

    def delete_lines(self, selection_id):
        with closing(sqlite3.connect(self.catalog)) as connection:
            connection.execute(
                "DELETE FROM selection_lines WHERE selection_ordinal="
                "(SELECT ordinal FROM selections WHERE selection_id=?)",
                (selection_id,),
            )
            connection.commit()

    def document(self):
        return {
            "schema_version": story_catalog_schema_version,
            "source_story_index_sha256": self.first.story_index_sha256,
            "source_audio_semantic_evidence_sha256": hashlib.sha256(
                _source_audio_evidence_payload(self.path)
            ).hexdigest(),
            "game": self.first.game,
            "game_id": self.first.game_id,
            "game_version": self.first.game_version,
            "selections": [asdict(value) for value in self.first.selections],
        }
