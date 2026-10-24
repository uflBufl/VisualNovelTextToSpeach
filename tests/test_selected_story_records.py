import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.story_fixtures import write_story_index, write_verified_source_story
from vntts.chapter_voice_preload import _validated_source_audio_line_ids
from vntts.document_identity import sha256_file
from vntts.path_safety import open_regular_binary
from vntts.pregeneration_pack import StoryAudioInspector
from vntts.pregeneration_setup import (
    PregenerationJobStore,
    StoryContentChanged,
    _cached_story_index_document,
    _read_selected_story_rows,
    inspect_story_index,
    load_verified_story_index_document,
    load_verified_story_records,
)
from vntts.story_index_snapshot import load_story_index_snapshot


class SelectedStoryRecordsTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = write_story_index(self.root / "content")
        self.catalog = self.root / "catalog.sqlite3"
        self.minimum = patch("vntts.pregeneration_setup.story_catalog_minimum_bytes", 0)
        self.location = patch(
            "vntts.pregeneration_setup._story_catalog_path", return_value=self.catalog
        )
        self.minimum.start()
        self.location.start()
        self.addCleanup(self.minimum.stop)
        self.addCleanup(self.location.stop)
        self.addCleanup(_cached_story_index_document.cache_clear)
        self.content = inspect_story_index(self.path)
        self.checksum = self.content.story_index_sha256
        self.full = load_verified_story_index_document(self.path, self.checksum)
        self.selected = (self.full.records[-1].line_id,)
        _cached_story_index_document.cache_clear()

    def test_restart_parses_only_requested_original_records(self):
        with (
            patch(
                "vntts.pregeneration_setup.load_verified_story_index_document",
                side_effect=AssertionError("full parse on cache hit"),
            ),
            patch(
                "vntts.pregeneration_setup.load_story_index_snapshot",
                wraps=load_story_index_snapshot,
            ) as parser,
        ):
            selected = load_verified_story_records(
                self.path, self.checksum, self.selected
            )
        self.assertEqual(
            tuple(record.line_id for record in selected.records), self.selected
        )
        self.assertEqual(
            selected.records[0].to_record(), self.full.records[-1].to_record()
        )
        self.assertEqual(selected.metadata["line_count"], 1)
        self.assertEqual(len(parser.call_args.args[1].splitlines()), 2)

    def test_fresh_process_uses_persistent_locations_without_full_parse(self):
        script = """
import json
from pathlib import Path
import sys
from unittest.mock import patch
from vntts.pregeneration_setup import load_verified_story_records
source, catalog, checksum, line = sys.argv[1:]
with patch("vntts.pregeneration_setup.story_catalog_minimum_bytes", 0), patch("vntts.pregeneration_setup._story_catalog_path", return_value=Path(catalog)), patch("vntts.pregeneration_setup.load_verified_story_index_document", side_effect=AssertionError("full parse after restart")):
    document = load_verified_story_records(source, checksum, (line,))
print(json.dumps([record.line_id for record in document.records]))
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(self.path),
                str(self.catalog),
                self.checksum,
                self.selected[0],
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), list(self.selected))

    def test_source_order_uses_verified_offsets_and_default_keeps_request_order(self):
        requested = tuple(record.line_id for record in reversed(self.full.records))
        with patch(
            "vntts.pregeneration_setup.load_verified_story_index_document",
            side_effect=AssertionError("full parse for ordering"),
        ):
            requested_view = load_verified_story_records(
                self.path, self.checksum, requested
            )
            source_view = load_verified_story_records(
                self.path, self.checksum, requested, source_order=True
            )
        self.assertEqual(
            tuple(record.line_id for record in requested_view.records), requested
        )
        self.assertEqual(source_view.records, self.full.records)

    def test_source_order_fallback_retains_strict_source_order(self):
        requested = tuple(record.line_id for record in reversed(self.full.records))
        with closing(sqlite3.connect(self.catalog)) as connection, connection:
            connection.execute("DELETE FROM record_locations")
        with patch(
            "vntts.pregeneration_setup.load_verified_story_index_document",
            wraps=load_verified_story_index_document,
        ) as canonical:
            source_view = load_verified_story_records(
                self.path, self.checksum, requested, source_order=True
            )
        self.assertGreaterEqual(canonical.call_count, 1)
        self.assertEqual(source_view.records, self.full.records)

    def test_inspector_loads_only_records_for_selected_story(self):
        inspector = StoryAudioInspector(
            self.content, PregenerationJobStore(self.root / "jobs")
        )
        selection = self.content.selections[-1]
        with patch(
            "vntts.pregeneration_setup.load_verified_story_index_document",
            side_effect=AssertionError("full parse for readiness"),
        ):
            coverage = inspector.inspect(selection.selection_id)
        self.assertEqual(set(inspector._source_records), set(selection.line_ids))
        self.assertEqual(
            coverage.original
            + coverage.generated
            + coverage.live
            + coverage.omitted
            + coverage.non_spoken
            + coverage.missing,
            selection.line_count,
        )

    def test_batch_reads_only_the_union_of_requested_stories_once(self):
        ids = tuple(selection.selection_id for selection in self.content.selections)
        inspector = StoryAudioInspector(
            self.content, PregenerationJobStore(self.root / "jobs"), selection_ids=ids
        )
        with patch(
            "vntts.pregeneration_pack.load_verified_story_records",
            wraps=load_verified_story_records,
        ) as read:
            for selection_id in ids:
                inspector.inspect(selection_id)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(
            set(read.call_args.args[2]),
            {record.line_id for record in self.full.records},
        )

    def test_changed_source_is_rejected_even_when_size_and_mtime_are_restored(self):
        before = self.path.stat()
        original = self.path.read_bytes()
        self.path.write_bytes(original.replace(b"Reverse", b"reverse", 1))
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(StoryContentChanged):
            load_verified_story_records(self.path, self.checksum, self.selected)

    def test_evidence_changed_during_selected_parse_requires_refresh(self):
        evidence = self.path.with_name("source-audio-semantic-evidence.json")
        original = evidence.read_bytes()

        def changed(*args):
            result = _read_selected_story_rows(*args)
            evidence.write_bytes(original + b" ")
            return result

        with patch(
            "vntts.pregeneration_setup._read_selected_story_rows", side_effect=changed
        ):
            with self.assertRaisesRegex(StoryContentChanged, "evidence changed"):
                load_verified_story_records(self.path, self.checksum, self.selected)

    def test_corrupt_row_hint_uses_strict_canonical_admission(self):
        with closing(sqlite3.connect(self.catalog)) as connection, connection:
            connection.execute("DELETE FROM record_locations")
        with patch(
            "vntts.pregeneration_setup.load_verified_story_index_document",
            wraps=load_verified_story_index_document,
        ) as canonical:
            selected = load_verified_story_records(
                self.path, self.checksum, self.selected
            )
        self.assertGreaterEqual(canonical.call_count, 1)
        self.assertEqual(
            selected.find(self.selected[0]).to_record(),
            self.full.records[-1].to_record(),
        )

    def test_source_or_evidence_change_during_index_rebuild_requires_refresh(self):
        for evidence_changed in (False, True):
            with self.subTest(evidence_changed=evidence_changed):
                with closing(sqlite3.connect(self.catalog)) as connection, connection:
                    connection.execute("DELETE FROM record_locations")
                changed = (
                    self.path.with_name("source-audio-semantic-evidence.json")
                    if evidence_changed
                    else self.path
                )
                original = changed.read_bytes()

                def rebuild_then_change(*args):
                    result = inspect_story_index(*args)
                    changed.write_bytes(original + b" ")
                    return result

                try:
                    with patch(
                        "vntts.pregeneration_setup.inspect_story_index",
                        side_effect=rebuild_then_change,
                    ):
                        with self.assertRaises(StoryContentChanged):
                            load_verified_story_records(
                                self.path, self.checksum, self.selected
                            )
                finally:
                    changed.write_bytes(original)
                    _cached_story_index_document.cache_clear()

    def test_damaged_row_hint_cannot_admit_a_transient_source_edit(self):
        original = self.path.read_bytes()
        changed = original.replace(b"A child line.", b"B child line.")
        changed_row = changed.splitlines(keepends=True)[-1]
        with closing(sqlite3.connect(self.catalog)) as connection, connection:
            connection.execute(
                "UPDATE record_locations SET row_sha256=? WHERE line_id=?",
                (hashlib.sha256(changed_row).hexdigest(), self.selected[0]),
            )
        source = self.path

        class TransientEditStream:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def __iter__(self):
                return iter(self.stream)

            def seek(self, offset):
                source.write_bytes(changed)
                return self.stream.seek(offset)

            def read(self, size=-1):
                value = self.stream.read(size)
                if size == len(changed_row):
                    source.write_bytes(original)
                return value

        @contextmanager
        def transient_edit(path):
            with open_regular_binary(path) as stream:
                yield TransientEditStream(stream)

        with patch("vntts.pregeneration_setup.open_regular_binary", transient_edit):
            selected = load_verified_story_records(
                self.path, self.checksum, self.selected
            )
        self.assertEqual(
            selected.find(self.selected[0]).to_record(),
            self.full.records[-1].to_record(),
        )
        self.assertEqual(sha256_file(self.path), self.checksum)

    def test_partial_semantic_view_matches_full_source_readiness(self):
        source = self.root / "verified" / "story-index.jsonl"
        write_verified_source_story(source)
        content = inspect_story_index(source)
        whole = load_verified_story_index_document(source, content.story_index_sha256)
        expected = _validated_source_audio_line_ids(source, whole)
        line_ids = (whole.records[0].line_id,)
        _cached_story_index_document.cache_clear()
        with patch(
            "vntts.pregeneration_setup.load_verified_story_index_document",
            side_effect=AssertionError("full semantic parse"),
        ):
            selected = load_verified_story_records(
                source, content.story_index_sha256, line_ids
            )
        self.assertEqual(
            _validated_source_audio_line_ids(source, selected),
            expected.intersection(line_ids),
        )

    def test_invalid_unselected_semantics_cannot_be_hidden_by_a_partial_view(self):
        source = self.root / "invalid-semantics" / "story-index.jsonl"
        write_verified_source_story(source)
        rows = [json.loads(row) for row in source.read_text().splitlines()]
        rows.append({**rows[1], "line_id": "unselected", "chapter": "other"})
        rows[0]["line_count"] = 2
        rows[0]["source_audio_semantics"]["applied_count"] = 2
        source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        content = inspect_story_index(source)
        _cached_story_index_document.cache_clear()
        with patch(
            "vntts.pregeneration_setup.load_verified_story_index_document",
            wraps=load_verified_story_index_document,
        ) as canonical:
            selected = load_verified_story_records(
                source, content.story_index_sha256, (rows[1]["line_id"],)
            )
        self.assertGreaterEqual(canonical.call_count, 1)
        self.assertEqual(
            _validated_source_audio_line_ids(source, selected), frozenset()
        )


if __name__ == "__main__":
    unittest.main()
