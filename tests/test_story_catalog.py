import gc
import hashlib
import os
import sqlite3
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from vntts_artifacts.story_index import (
    load_story_index_document,
    write_story_index_document,
)

from tests.story_fixtures import write_story_index
from tests.symlink_support import symlink_or_skip
from vntts import story_catalog
from vntts.dialogue_search import projection_may_match, text_projection
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
                    "changed",
                    document["source_audio_semantic_evidence_sha256"],
                    story_catalog_schema_version,
                )
            )
            self.assertIsNone(
                storage.load(
                    self.first.story_index_sha256,
                    "changed",
                    story_catalog_schema_version,
                )
            )
            self.assertIsNotNone(
                storage.load(
                    self.first.story_index_sha256,
                    document["source_audio_semantic_evidence_sha256"],
                    story_catalog_schema_version,
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
        source_inode = self.path.stat().st_ino

        def changed_ctime(descriptor):
            nonlocal calls
            info = original_fstat(descriptor)
            values = {
                name: getattr(info, name)
                for name in dir(info)
                if name.startswith("st_")
            }
            if info.st_ino == source_inode:
                calls += 1
            if (
                info.st_ino == source_inode and calls == 3
            ):  # Regular-file admission, then before/after the source read.
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
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                story_catalog._VERSION,
            )
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

    def dialogue_projections(self, owner):
        return {
            row.line_id: text_projection(row.text)
            for row in owner.dialogues(owner.line_ids())
        }

    def dialogue_catalog(self):
        storage, document = self.indexed_catalog()
        owner = storage.open_dialogue(
            self.path,
            document["source_story_index_sha256"],
            document["source_audio_semantic_evidence_sha256"],
            cache_version=story_catalog_schema_version,
        )
        self.addCleanup(owner.close)
        return owner

    def test_dialogue_queries_reuse_owner_without_reopening_source_or_sql(self):
        from vntts.chapter_voice_preload import (
            ChapterVoicePreloader,
            _normalize,
            _normalize_exact_text,
        )
        from vntts.dialog import is_symbol_only_text

        expected = ChapterVoicePreloader.dialogue_from_story_index(
            self.path, load_story_index_document(self.path)
        )
        owner = self.dialogue_catalog()
        with (
            patch(
                "vntts.story_catalog.open_regular_binary",
                side_effect=AssertionError("source read"),
            ),
            patch(
                "vntts.story_catalog.sqlite3.connect",
                side_effect=AssertionError("SQL reopen"),
            ),
        ):
            self.assertEqual(owner.count, len(expected))
            self.assertEqual(
                owner.dialogues(tuple(row.line_id for row in expected)), expected
            )
            for line in expected:
                self.assertTrue(owner.line_exists(line.line_id))
                self.assertTrue(owner.chapter_exists(line.chapter))
                self.assertIn(line.line_id, owner.line_ids(line.chapter))
                self.assertIn(
                    line.line_id,
                    owner.exact_ids(
                        _normalize(line.speaker),
                        is_symbol_only_text(line.text),
                        _normalize_exact_text(line.text),
                    ),
                )
                self.assertIn(
                    line.line_id, owner.normalized_ids(None, _normalize(line.text))
                )
                self.assertIn(_normalize(line.speaker), owner.speakers(line.chapter))
            self.assertEqual(
                owner.has_original_speech(),
                any(row.has_original_speech for row in expected),
            )
            self.assertFalse(owner.line_exists("absent"))
            self.assertFalse(owner.chapter_exists("absent"))

    def test_fts_seeds_keep_every_full_and_prefix_edit_witness(self):
        owner = self.dialogue_catalog()
        keys = self.dialogue_projections(owner)
        for line_id, key in keys.items():
            for prefix, limit in ((False, 4), (True, 3)):
                source = key[: max(5, len(key) - 4)] if prefix else key
                for operation in ("unchanged", "delete", "insert", "replace"):
                    observed = source
                    if operation == "delete":
                        observed = observed[:1] + observed[2:]
                    elif operation == "insert":
                        observed = observed[:1] + "x" + observed[1:]
                    elif operation == "replace":
                        observed = "x" + observed[1:]
                    with self.subTest(
                        line_id=line_id, prefix=prefix, operation=operation
                    ):
                        self.assertIn(
                            line_id, dict(owner.iter_seed_rows(observed, limit, prefix))
                        )
        with self.assertRaises(ValueError):
            tuple(owner.iter_seed_rows("invalid", -1))

    def test_dialogue_owner_reads_admitted_rows_without_source_polling(
        self,
    ):
        owner = self.dialogue_catalog()
        selected = owner.line_ids()[-1:]
        document = owner.records(selected)
        self.assertEqual(tuple(row.line_id for row in document.records), selected)
        self.path.write_bytes(self.path.read_bytes() + b"\n")
        with patch(
            "vntts.content_revision.source_signature",
            side_effect=AssertionError("source polled"),
        ):
            self.assertEqual(
                tuple(row.line_id for row in owner.dialogues(selected)), selected
            )
            self.assertEqual(owner.records(selected).records, document.records)

    def test_dialogue_owner_pins_separate_read_connections_for_worker_threads(self):
        owner = self.dialogue_catalog()
        expected = owner.line_ids()
        with ThreadPoolExecutor(max_workers=1) as worker:
            self.assertEqual(worker.submit(owner.line_ids).result(), expected)
        self.assertEqual(len(owner._connections), 2)
        owner.close()
        with self.assertRaisesRegex(StoryCatalogError, "closed"):
            owner.line_ids()

    def test_dialogue_owner_uses_admitted_evidence_until_reloaded(self):
        owner = self.dialogue_catalog()
        evidence = self.path.with_name("source-audio-semantic-evidence.json")
        expected = owner.exact_ids(None, False, "generate me.")
        self.assertTrue(expected)
        evidence.write_bytes(evidence.read_bytes() + b"\n")
        with patch(
            "vntts.story_catalog._file_signature",
            side_effect=AssertionError("evidence polled"),
        ):
            self.assertEqual(owner.exact_ids(None, False, "generate me."), expected)

    def test_dialogue_query_cancellation_resets_sql_progress_handler(self):
        from vntts.chapter_voice_preload import StoryMatchingCancelled

        owner = self.dialogue_catalog()
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls > 1

        with self.assertRaises(StoryMatchingCancelled), owner.query(cancelled):
            owner._connection().execute(
                "WITH RECURSIVE values_(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM values_ WHERE n<100000) "
                "SELECT sum(n) FROM values_"
            ).fetchone()
        self.assertTrue(owner.line_exists("reverse1999:3"))
        self.assertEqual(owner.dialogues(("absent",)), ())

    def test_line_id_stream_is_lazy_and_selects_only_original_rows(self):
        owner = self.dialogue_catalog()
        expected = owner.line_ids()
        originals = tuple(
            row.line_id for row in owner.dialogues(expected) if row.has_original_speech
        )
        queries = []
        connection = owner._connection()
        connection.set_trace_callback(queries.append)
        self.addCleanup(connection.set_trace_callback, None)
        with patch.object(
            owner, "line_ids", side_effect=AssertionError("full identity tuple")
        ):
            stream = owner.iter_line_ids()
            self.assertEqual(queries, [])
            self.assertEqual(next(stream), expected[0])
            self.assertEqual(tuple(stream), expected[1:])
            self.assertEqual(tuple(owner.iter_line_ids(original_only=True)), originals)
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT line_id FROM dialogue WHERE original_speech=1 ORDER BY id"
        ).fetchall()
        self.assertTrue(any("dialogue_original" in row[3] for row in plan), plan)

    def test_prefix_range_keeps_all_matches_and_uses_projection_index(self):
        owner = self.dialogue_catalog()
        ids = owner.line_ids()
        projections = self.dialogue_projections(owner)
        for line_id, projection in projections.items():
            prefix = projection[:3]
            with self.subTest(line_id=line_id):
                expected = tuple(
                    key for key in ids if projections[key].startswith(prefix)
                )
                self.assertEqual(
                    tuple(key for key, _ in owner.iter_seed_rows(prefix, 0, True)),
                    expected,
                )
                chapter = owner.dialogues((line_id,))[0].chapter
                self.assertIn(
                    line_id, dict(owner.iter_seed_rows(prefix, 0, True, chapter))
                )
                self.assertEqual(
                    tuple(owner.iter_seed_rows(prefix, 0, True, "absent")), ()
                )
        self.assertEqual(tuple(owner.iter_seed_rows("\U0010ffff", 0, True)), ())
        plan = (
            owner._connection()
            .execute(
                "EXPLAIN QUERY PLAN SELECT line_id FROM dialogue WHERE projection>=? AND projection<? ORDER BY id",
                ("gen", "geo"),
            )
            .fetchall()
        )
        self.assertTrue(any("dialogue_projection" in row[3] for row in plan), plan)

    def test_candidate_lane_scopes_partition_every_unfiltered_result(self):
        owner = self.dialogue_catalog()
        ids = owner.line_ids()
        expected = {
            original: {
                row.line_id
                for row in owner.dialogues(ids)
                if row.has_original_speech == original
            }
            for original in (False, True)
        }
        queries = [("a", 4, True)]
        for projection in self.dialogue_projections(owner).values():
            queries.extend(
                (
                    (projection[:1], 0, True),
                    (projection, 4, False),
                    (projection, 0, False),
                    (projection, 4, True),
                )
            )
        for projection, distance, prefix in queries:
            unfiltered = set(dict(owner.iter_seed_rows(projection, distance, prefix)))
            lanes = {
                original: set(
                    dict(
                        owner.iter_seed_rows(
                            projection, distance, prefix, original=original
                        )
                    )
                )
                for original in (False, True)
            }
            self.assertEqual(lanes[False] | lanes[True], unfiltered)
            self.assertFalse(lanes[False] & lanes[True])
            for original in (False, True):
                self.assertEqual(lanes[original], unfiltered & expected[original])
        plan = (
            owner._connection()
            .execute(
                "EXPLAIN QUERY PLAN SELECT line_id FROM dialogue WHERE projection_length>=? AND original_speech=? ORDER BY id",
                (0, 0),
            )
            .fetchall()
        )
        self.assertTrue(any("dialogue_original" in row[3] for row in plan), plan)

    def test_positional_seeds_preserve_shifted_unicode_full_and_prefix_witnesses(self):
        story = load_story_index_document(self.path)
        base = story.records[-1].to_record()
        query = "abcdefghijklmnopqr"
        pairs = tuple(query[index : index + 2] for index in range(0, len(query), 2))
        split_pairs = (
            "".join(pair[0] + "x" + pair[1] for pair in pairs[:-1]) + pairs[-1]
        )
        texts = (
            "αβγδεζηθικλμνξοπρστυφχψω東京大阪名古屋abcdefghijklmnopqrstuv",
            "Старинный фонарь освещает забытый проход за старым домом.",
            split_pairs,
        )
        story = write_story_index_document(
            self.path,
            story.metadata,
            [
                {
                    **base,
                    "line_id": f"shift:{index}",
                    "sequence": index,
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
                for index, text in enumerate(texts)
            ],
        )
        document = {
            **self.document(),
            "source_story_index_sha256": hashlib.sha256(
                self.path.read_bytes()
            ).hexdigest(),
            "selections": [],
        }
        storage = StoryCatalog(self.catalog)
        storage.save(document, story=story)
        with storage.open_dialogue(
            self.path,
            document["source_story_index_sha256"],
            document["source_audio_semantic_evidence_sha256"],
            cache_version=story_catalog_schema_version,
        ) as owner:
            projections = self.dialogue_projections(owner)
            for line_id in ("shift:0", "shift:1"):
                key = projections[line_id]
                for distance in (3, 4):
                    for prefix in (False, True):
                        observed = key[:-8] if prefix else key
                        variants = (
                            "x" * distance + observed,
                            observed[distance:],
                            observed[:7] + "x" * distance + observed[7:],
                            observed[:7] + observed[7 + distance :],
                        )
                        for variant in variants:
                            with self.subTest(
                                line_id=line_id,
                                distance=distance,
                                prefix=prefix,
                                variant=variant,
                            ):
                                self.assertIn(
                                    line_id,
                                    dict(
                                        owner.iter_seed_rows(
                                            variant,
                                            max_distance=distance,
                                            prefix=prefix,
                                        )
                                    ),
                                )
            # Only the final pair survives eight internal insertions, beyond position 18.
            self.assertIn("shift:2", dict(owner.iter_seed_rows(query, max_distance=8)))

    def test_streamed_seed_rows_keep_all_scoped_projected_witnesses(self):
        owner = self.dialogue_catalog()
        rows = owner.dialogues(owner.line_ids())
        projections = self.dialogue_projections(owner)
        for projection in ("", "a", "gen", *projections.values()):
            for distance in (0, 4):
                for prefix in (False, True):
                    for chapter in (None, "1", "absent"):
                        for original in (None, False, True):
                            options = dict(
                                max_distance=distance,
                                prefix=prefix,
                                chapter=chapter,
                                original=original,
                            )
                            scope = {
                                row.line_id
                                for row in rows
                                if (chapter is None or row.chapter == chapter)
                                and (
                                    original is None
                                    or row.has_original_speech == original
                                )
                            }
                            witnesses = {
                                key
                                for key in scope
                                if projection_may_match(
                                    projection,
                                    projections[key],
                                    distance=distance,
                                    prefix=prefix,
                                )
                            }
                            with self.subTest(projection=projection, **options):
                                found = dict(
                                    owner.iter_seed_rows(projection, **options)
                                )
                                self.assertLessEqual(witnesses, found.keys())
                                self.assertLessEqual(found.keys(), scope)
                                self.assertEqual(
                                    found, {key: projections[key] for key in found}
                                )
                                if distance == 0:
                                    self.assertEqual(found.keys(), witnesses)

    def test_streamed_prefix_yields_before_consuming_the_whole_candidate_set(self):
        story = load_story_index_document(self.path)
        base = story.records[-1].to_record()
        text = "Shared opening for every voice."
        story = write_story_index_document(
            self.path,
            story.metadata,
            [
                {
                    **base,
                    "line_id": f"bulk:{index}",
                    "chapter": "bulk",
                    "sequence": index,
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
                for index in range(600)
            ],
        )
        document = {
            **self.document(),
            "source_story_index_sha256": hashlib.sha256(
                self.path.read_bytes()
            ).hexdigest(),
            "selections": [],
        }
        storage = StoryCatalog(self.catalog)
        storage.save(document, story=story)
        owner = storage.open_dialogue(
            self.path,
            document["source_story_index_sha256"],
            document["source_audio_semantic_evidence_sha256"],
            cache_version=story_catalog_schema_version,
        )
        self.addCleanup(owner.close)
        connection = owner._connection()
        consumed = []
        queries = []

        class TrackedCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchmany(self, size):
                rows = self.cursor.fetchmany(size)
                consumed.extend(rows)
                return rows

            def close(self):
                self.cursor.close()

        class TrackedConnection:
            def execute(self, sql, parameters):
                queries.append(sql)
                return TrackedCursor(connection.execute(sql, parameters))

        with patch.object(owner, "_connection", return_value=TrackedConnection()):
            candidates = owner.iter_seed_rows("shared", max_distance=0, prefix=True)
            self.assertEqual(consumed, [])
            first = next(candidates)
            self.assertEqual(first[0], "bulk:0")
            self.assertLess(len(consumed), owner.count)
            self.assertEqual(len(consumed), 256)
            rest = tuple(candidates)
            self.assertEqual(len(queries), 1)
            self.assertEqual(
                (first, *rest),
                tuple(
                    (f"bulk:{index}", "shared opening for every voice")
                    for index in range(600)
                ),
            )

    def test_unreferenced_owner_closes_its_retained_sql_connections(self):
        storage, document = self.indexed_catalog()
        owner = storage.open_dialogue(
            self.path,
            document["source_story_index_sha256"],
            document["source_audio_semantic_evidence_sha256"],
            cache_version=story_catalog_schema_version,
        )
        connection = owner._connection()
        reference = weakref.ref(owner)
        del owner
        gc.collect()
        self.assertIsNone(reference())
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_future_search_semantics_are_preserved_and_refused_by_dialogue_owner(self):
        storage, document = self.indexed_catalog()
        with closing(sqlite3.connect(self.catalog)) as connection:
            connection.execute(
                "UPDATE metadata SET cache_version=?",
                (story_catalog_schema_version + 1,),
            )
            connection.commit()
        original = self.catalog.read_bytes()
        with self.assertRaisesRegex(StoryCatalogError, "identity changed"):
            storage.open_dialogue(
                self.path,
                document["source_story_index_sha256"],
                document["source_audio_semantic_evidence_sha256"],
                cache_version=story_catalog_schema_version,
            )
        self.assertEqual(self.catalog.read_bytes(), original)
        with self.assertRaisesRegex(
            StoryCatalogError, "future story catalog cache version"
        ):
            storage.save(document, story=load_story_index_document(self.path))
        self.assertEqual(self.catalog.read_bytes(), original)

    def test_worker_owner_refuses_obsolete_search_semantics(self):
        storage, document = self.indexed_catalog()
        with closing(sqlite3.connect(self.catalog)) as connection:
            connection.execute("UPDATE metadata SET cache_version=0")
            connection.commit()
        original = self.catalog.read_bytes()
        with ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(
                storage.open_dialogue,
                self.path,
                document["source_story_index_sha256"],
                document["source_audio_semantic_evidence_sha256"],
                cache_version=story_catalog_schema_version,
            )
            with self.assertRaisesRegex(StoryCatalogError, "identity changed"):
                result.result()
        self.assertEqual(self.catalog.read_bytes(), original)

    def test_fts_shadow_schema_change_is_preserved_and_rejected(self):
        with closing(sqlite3.connect(self.catalog)) as connection:
            connection.execute(
                "ALTER TABLE dialogue_grams_config ADD COLUMN foreign_value TEXT"
            )
            connection.commit()
        original = self.catalog.read_bytes()
        with self.assertRaisesRegex(StoryCatalogError, "schema changed"):
            StoryCatalog(self.catalog).save(
                self.document(), story=load_story_index_document(self.path)
            )
        self.assertEqual(self.catalog.read_bytes(), original)

    def test_exact_v2_schema_rebuilds_without_reading_legacy_rows(self):
        self.catalog.unlink()
        document = self.document()
        with closing(sqlite3.connect(self.catalog)) as connection:
            for statement in story_catalog._V2_SCHEMA:
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id={story_catalog._APPLICATION_ID}")
            connection.execute("PRAGMA user_version=2")
            StoryCatalog._write(connection, document)
            connection.commit()
        storage = StoryCatalog(self.catalog)
        self.assertIsNone(
            storage.load(
                document["source_story_index_sha256"],
                document["source_audio_semantic_evidence_sha256"],
                document["schema_version"],
            )
        )
        storage.save(document, story=load_story_index_document(self.path))
        with storage.open_dialogue(
            self.path,
            document["source_story_index_sha256"],
            document["source_audio_semantic_evidence_sha256"],
            cache_version=story_catalog_schema_version,
        ) as owner:
            self.assertTrue(owner.line_exists("reverse1999:3"))

    def test_previous_matcher_keys_rebuild_from_source_before_dialogue_queries(self):
        storage, document = self.indexed_catalog()
        with closing(sqlite3.connect(self.catalog)) as connection:
            connection.execute("PRAGMA user_version=3")
            connection.execute(
                "UPDATE dialogue SET normalized_key='obsolete', projection='obsolete', "
                "projection_length=8"
            )
            connection.commit()
        previous = self.catalog.read_bytes()
        with patch.object(
            StoryCatalog, "_selections", side_effect=AssertionError("old keys read")
        ):
            self.assertIsNone(
                storage.load(
                    document["source_story_index_sha256"],
                    document["source_audio_semantic_evidence_sha256"],
                    document["schema_version"],
                )
            )
        with self.assertRaisesRegex(StoryCatalogError, "derived rebuild"):
            storage.open_dialogue(
                self.path,
                document["source_story_index_sha256"],
                document["source_audio_semantic_evidence_sha256"],
                cache_version=story_catalog_schema_version,
            )
        self.assertEqual(self.catalog.read_bytes(), previous)
        storage.save(document, story=load_story_index_document(self.path))
        with storage.open_dialogue(
            self.path,
            document["source_story_index_sha256"],
            document["source_audio_semantic_evidence_sha256"],
            cache_version=story_catalog_schema_version,
        ) as owner:
            self.assertEqual(
                dict(owner.iter_seed_rows("", max_distance=0, prefix=True)),
                self.dialogue_projections(owner),
            )
        with closing(sqlite3.connect(self.catalog)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                story_catalog._VERSION,
            )

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
