import hashlib
import os
import sqlite3
import unittest
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from vntts_artifacts.story_index import load_story_index_document

from tests.story_fixtures import write_story_index
from tests.symlink_support import symlink_or_skip
from vntts import story_catalog
from vntts.path_safety import open_regular_binary
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

    def test_restart_fetches_only_requested_source_locations_in_requested_order(self):
        storage, document = self.indexed_catalog()
        statements = []
        connect = sqlite3.connect

        def traced_connection(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        requested = ("reverse1999:3", "reverse1999:1", "reverse1999:3")
        with (
            patch("vntts.story_catalog.sqlite3.connect", side_effect=traced_connection),
            patch(
                "vntts.story_catalog.open_regular_binary",
                side_effect=AssertionError("source read during SQL lookup"),
            ),
        ):
            found = StoryCatalog(storage.path).record_locations(
                document["source_story_index_sha256"],
                document["source_audio_semantic_evidence_sha256"],
                requested,
            )
        raw = self.path.read_bytes()
        rows = raw.splitlines(keepends=True)
        offset = len(rows[0])
        expected = {}
        for record, row in zip(
            load_story_index_document(self.path).records, rows[1:], strict=True
        ):
            expected[record.line_id] = (
                record.line_id,
                offset,
                len(row),
                hashlib.sha256(row).hexdigest(),
            )
            offset += len(row)
        self.assertEqual(found, tuple(expected[line_id] for line_id in requested))
        queries = [value for value in statements if "FROM record_locations" in value]
        self.assertEqual(len(queries), 1)
        self.assertIn("WHERE line_id IN", queries[0])
        self.assertNotIn("reverse1999:2", queries[0])
        with closing(sqlite3.connect(storage.path)) as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT source_offset FROM record_locations WHERE line_id=?",
                (requested[0],),
            ).fetchall()
            self.assertTrue(any("SEARCH" in row[3] for row in plan))

    def test_record_locations_require_complete_matching_index_and_semantics(self):
        storage = StoryCatalog(self.catalog)
        document = self.document()
        identity = (
            document["source_story_index_sha256"],
            document["source_audio_semantic_evidence_sha256"],
        )
        storage.save(document)
        self.assertIsNone(storage.record_locations(*identity, ("reverse1999:3",)))
        storage.save(
            document,
            story=load_story_index_document(self.path),
            source_semantics_valid=False,
        )
        self.assertIsNone(storage.record_locations(*identity, ("reverse1999:3",)))
        with closing(sqlite3.connect(storage.path)) as connection:
            connection.execute("UPDATE record_source SET semantic_valid=1")
            connection.commit()
        with self.assertRaisesRegex(
            StoryCatalogError, "record-source metadata changed"
        ):
            storage.record_locations(*identity, ("reverse1999:3",))
        storage, _document = self.indexed_catalog()
        self.assertEqual(storage.record_locations(*identity, ()), ())
        self.assertIsNone(
            storage.record_locations("changed", identity[1], ("reverse1999:3",))
        )
        self.assertIsNone(
            storage.record_locations(identity[0], "changed", ("reverse1999:3",))
        )
        with closing(sqlite3.connect(storage.path)) as connection:
            connection.execute(
                "DELETE FROM record_locations WHERE line_id='reverse1999:3'"
            )
            connection.commit()
        self.assertIsNone(storage.record_locations(*identity, ("reverse1999:3",)))

    def test_record_locations_validate_bounds_digest_and_source_metadata(self):
        statements = (
            "UPDATE record_locations SET source_offset=-1 WHERE line_id='reverse1999:3'",
            "UPDATE record_locations SET source_length=0 WHERE line_id='reverse1999:3'",
            "UPDATE record_locations SET source_length=999999999 WHERE line_id='reverse1999:3'",
            "UPDATE record_locations SET row_sha256='invalid' WHERE line_id='reverse1999:3'",
            "UPDATE record_source SET header_length=0",
            "UPDATE record_source SET header_sha256='invalid'",
            "UPDATE record_source SET record_count=-1",
            "UPDATE record_source SET record_count=record_count+1",
            "UPDATE record_source SET source_size=source_size+1",
            "UPDATE record_source SET receipt_sha256='invalid'",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                storage, document = self.indexed_catalog()
                with closing(sqlite3.connect(storage.path)) as connection:
                    connection.execute(statement)
                    connection.commit()
                with self.assertRaises(StoryCatalogError):
                    storage.record_locations(
                        document["source_story_index_sha256"],
                        document["source_audio_semantic_evidence_sha256"],
                        ("reverse1999:3",),
                    )

    def test_index_build_requires_exact_source_sha_and_unpoisoned_parse(self):
        for mutation in ("sha", "header", "record"):
            with self.subTest(mutation=mutation):
                original = self.catalog.read_bytes()
                document = self.document()
                story = load_story_index_document(self.path)
                if mutation == "sha":
                    document["source_story_index_sha256"] = "a" * 64
                elif mutation == "header":
                    story.metadata["game"] = "poisoned"
                else:
                    story.records[0].document["text"] = "poisoned"
                with self.assertRaises(StoryCatalogError):
                    StoryCatalog(self.catalog).save(document, story=story)
                self.assertEqual(self.catalog.read_bytes(), original)
                self.assertEqual(list(self.catalog.parent.glob(".story-catalog-*")), [])

    def test_source_change_after_streaming_keeps_previous_complete_index(self):
        storage, document = self.indexed_catalog()
        original = storage.path.read_bytes()
        story = load_story_index_document(self.path)

        @contextmanager
        def change_source_after_read(path):
            with open_regular_binary(path) as stream:
                yield stream
            path.write_bytes(path.read_bytes() + b" ")

        with patch("vntts.story_catalog.open_regular_binary", change_source_after_read):
            with self.assertRaisesRegex(StoryCatalogError, "source changed"):
                storage.save(document, story=story)
        self.assertEqual(storage.path.read_bytes(), original)
        self.assertEqual(list(self.catalog.parent.glob(".story-catalog-*")), [])

    def test_stable_source_allows_different_fd_and_path_ctime(self):
        storage, document = self.indexed_catalog()
        story = load_story_index_document(self.path)
        original_fstat = story_catalog.os.fstat

        def different_ctime(descriptor):
            info = original_fstat(descriptor)
            values = {
                name: getattr(info, name)
                for name in dir(info)
                if name.startswith("st_")
            }
            values["st_ctime_ns"] += 1000
            return SimpleNamespace(**values)

        with patch("vntts.story_catalog.os.fstat", side_effect=different_ctime):
            storage.save(document, story=story)
        self.assertIsNotNone(
            storage.record_locations(
                document["source_story_index_sha256"],
                document["source_audio_semantic_evidence_sha256"],
                ("reverse1999:3",),
            )
        )

    def test_fd_ctime_change_preserves_previous_complete_index(self):
        storage, document = self.indexed_catalog()
        original = storage.path.read_bytes()
        story = load_story_index_document(self.path)
        original_fstat = story_catalog.os.fstat
        calls = 0

        def changed_ctime(descriptor):
            nonlocal calls
            info = original_fstat(descriptor)
            values = {
                name: getattr(info, name)
                for name in dir(info)
                if name.startswith("st_")
            }
            calls += 1
            if calls == 3:  # Regular-file admission, then before/after the source read.
                values["st_ctime_ns"] += 1000
            return SimpleNamespace(**values)

        with patch("vntts.story_catalog.os.fstat", side_effect=changed_ctime):
            with self.assertRaisesRegex(StoryCatalogError, "source changed"):
                storage.save(document, story=story)
        self.assertEqual(storage.path.read_bytes(), original)
        self.assertEqual(list(self.catalog.parent.glob(".story-catalog-*")), [])

    def test_same_content_source_replacement_preserves_previous_complete_index(self):
        storage, document = self.indexed_catalog()
        original = storage.path.read_bytes()
        story = load_story_index_document(self.path)

        @contextmanager
        def replace_source_after_read(path):
            with open_regular_binary(path) as stream:
                yield stream
            info = path.stat()
            replacement = path.with_name("replacement.jsonl")
            replacement.write_bytes(path.read_bytes())
            os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
            replacement.replace(path)

        with patch(
            "vntts.story_catalog.open_regular_binary", replace_source_after_read
        ):
            with self.assertRaisesRegex(StoryCatalogError, "source changed"):
                storage.save(document, story=story)
        self.assertEqual(storage.path.read_bytes(), original)
        self.assertEqual(list(self.catalog.parent.glob(".story-catalog-*")), [])

    def test_source_symlink_is_rejected_without_replacing_catalog(self):
        story = load_story_index_document(self.path)
        original = self.catalog.read_bytes()
        target = self.path.with_name("original.jsonl")
        self.path.rename(target)
        symlink_or_skip(self.path, target)
        with self.assertRaises(StoryCatalogError):
            StoryCatalog(self.catalog).save(self.document(), story=story)
        self.assertEqual(self.catalog.read_bytes(), original)

    def test_exact_v1_schema_is_a_miss_and_rebuilds_from_source(self):
        self.legacy_catalog()
        original = self.catalog.read_bytes()
        storage = StoryCatalog(self.catalog)
        document = self.document()
        with patch.object(
            StoryCatalog, "_selections", side_effect=AssertionError("legacy row read")
        ):
            self.assertIsNone(
                storage.load(
                    self.first.story_index_sha256,
                    document["source_audio_semantic_evidence_sha256"],
                    story_catalog_schema_version,
                )
            )
            self.assertIsNone(
                storage.record_locations(
                    self.first.story_index_sha256,
                    document["source_audio_semantic_evidence_sha256"],
                    ("reverse1999:3",),
                )
            )
        self.assertEqual(self.catalog.read_bytes(), original)
        storage.save(document, story=load_story_index_document(self.path))
        with closing(sqlite3.connect(self.catalog)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertIsNotNone(
            storage.record_locations(
                self.first.story_index_sha256,
                document["source_audio_semantic_evidence_sha256"],
                ("reverse1999:3",),
            )
        )

    def test_unknown_v1_schema_and_future_v1_cache_are_never_replaced(self):
        for future in (False, True):
            with self.subTest(future=future):
                self.legacy_catalog(
                    cache_version=99 if future else story_catalog_schema_version
                )
                if not future:
                    with closing(sqlite3.connect(self.catalog)) as connection:
                        connection.execute(
                            "ALTER TABLE metadata ADD COLUMN unexpected TEXT"
                        )
                        connection.commit()
                original = self.catalog.read_bytes()
                with self.assertRaises(StoryCatalogError):
                    StoryCatalog(self.catalog).save(
                        self.document(), story=load_story_index_document(self.path)
                    )
                self.assertEqual(self.catalog.read_bytes(), original)

    def test_record_lookup_rejects_linked_sql_sidecars(self):
        storage, document = self.indexed_catalog()
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                sidecar = self.catalog.with_name(self.catalog.name + suffix)
                symlink_or_skip(sidecar, self.path)
                try:
                    with self.assertRaises(StoryCatalogError):
                        storage.record_locations(
                            document["source_story_index_sha256"],
                            document["source_audio_semantic_evidence_sha256"],
                            ("reverse1999:3",),
                        )
                finally:
                    sidecar.unlink()

    def indexed_catalog(self):
        document = self.document()
        storage = StoryCatalog(self.catalog)
        storage.save(document, story=load_story_index_document(self.path))
        return storage, document

    def legacy_catalog(self, *, cache_version=story_catalog_schema_version):
        self.catalog.unlink()
        document = self.document()
        document["schema_version"] = cache_version
        with closing(sqlite3.connect(self.catalog)) as connection:
            for statement in story_catalog._LEGACY_SCHEMA:
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id={story_catalog._APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            StoryCatalog._write(connection, document)
            connection.commit()

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
