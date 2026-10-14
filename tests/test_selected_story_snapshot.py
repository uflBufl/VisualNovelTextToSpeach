import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.story_index import StoryIndexError, write_story_index_document

from tests.story_fixtures import write_verified_source_story
from vntts.document_identity import sha256_file
from vntts.path_safety import read_regular_bytes
from vntts.selected_story_snapshot import (
    _source_receipts,
    create_selected_story_snapshot,
    selected_story_origin,
)
from vntts.story_index_snapshot import load_story_index_snapshot


class SelectedStorySnapshotTest(unittest.TestCase):
    def setUp(self):
        _source_receipts.clear()
        self.addCleanup(_source_receipts.clear)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "catalog" / "story-index.jsonl"
        self.source.parent.mkdir()
        self.directory = self.root / "selected"
        self.metadata = {
            "game": "Synthetic",
            "language": "en",
            "collections": [
                {
                    "collection_id": "first",
                    "title": "First",
                    "kind": "story",
                    "order": 1,
                },
                {
                    "collection_id": "later",
                    "title": "Later",
                    "kind": "story",
                    "order": 2,
                },
            ],
        }
        self.records = [
            {
                "record_type": "line",
                "line_id": line_id,
                "chapter": chapter,
                "sequence": index,
                "speaker": "Speaker",
                "text": f"Exact text {line_id}.",
                "collection_id": collection,
                "context": {"producer": [index]},
                "speakable": index != 2,
            }
            for index, (line_id, chapter, collection) in enumerate(
                (("one", "1", "first"), ("two", "1", "first"), ("later", "2", "later")),
                start=1,
            )
        ]
        write_story_index_document(self.source, self.metadata, self.records)
        self.checksum = sha256_file(self.source)

    def create(self):
        return create_selected_story_snapshot(
            self.source, self.checksum, ("one", "two"), ("first",), self.directory
        )

    def load(self, path):
        return load_story_index_snapshot(path, read_regular_bytes(path))

    def change_snapshot(self, change):
        path = self.directory / "story-index.jsonl"
        rows = [json.loads(row) for row in path.read_text().splitlines()]
        change(rows)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def test_keeps_exact_selected_records_and_ultimate_origin(self):
        selected = self.create()
        document = self.load(selected)
        self.assertEqual(
            tuple(record.line_id for record in document.records), ("one", "two")
        )
        self.assertEqual(document.records[1].speakable, False)
        self.assertEqual(document.records[0].context, {"producer": [1]})
        self.assertEqual(selected_story_origin(document), (self.source, self.checksum))
        self.assertEqual(document.metadata["line_count"], 2)
        self.assertEqual(self.create(), selected)

    def test_exact_annotated_snapshot_is_returned_unchanged(self):
        selected = self.create()
        self.change_snapshot(
            lambda rows: rows[1].update(
                source_audio_duration_seconds=1.25,
                source_audio_duration_media_sha256="a" * 64,
                source_audio_completeness="unknown",
            )
        )
        before = selected.read_bytes()
        reused = create_selected_story_snapshot(
            selected,
            sha256_file(selected),
            ("one", "two"),
            ("first",),
            self.root / "unused",
        )
        self.assertEqual(reused, selected)
        self.assertEqual(reused.read_bytes(), before)
        self.assertFalse((self.root / "unused").exists())

    def test_rejects_omitted_extra_duplicate_or_unknown_selection(self):
        for line_ids, story_ids in (
            (("one",), ("first",)),
            (("one", "two", "later"), ("first",)),
            (("one", "one"), ("first",)),
            (("missing",), ("first",)),
            (("one", "two"), ("missing",)),
        ):
            with self.subTest(line_ids=line_ids, story_ids=story_ids):
                with self.assertRaises(StoryIndexError):
                    create_selected_story_snapshot(
                        self.source, self.checksum, line_ids, story_ids, self.directory
                    )
                self.assertFalse(self.directory.exists())

    def test_cold_admission_rejects_invalid_unselected_row(self):
        self.records[-1]["sequence"] = True
        rows = [json.loads(row) for row in self.source.read_text().splitlines()]
        rows[-1]["sequence"] = True
        self.source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.checksum = sha256_file(self.source)
        with self.assertRaisesRegex(StoryIndexError, "sequence must be an integer"):
            self.create()

    def test_stale_unselected_original_is_rejected_on_every_reload(self):
        from vntts.pregeneration_setup import StoryContentChanged

        selected = self.create()
        rows = [json.loads(row) for row in self.source.read_text().splitlines()]
        rows[-1]["context"] = {"changed": True}
        self.source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaisesRegex(
            StoryIndexError, "origin checksum changed"
        ) as raised:
            self.load(selected)
        self.assertIsInstance(raised.exception.__cause__, StoryContentChanged)

    def test_disk_witnesses_do_not_replace_cold_source_validation(self):
        selected = self.create()
        _source_receipts.clear()
        from vntts_artifacts.story_index import load_story_index_document

        with patch(
            "vntts.story_index_snapshot.load_story_index_document",
            wraps=load_story_index_document,
        ) as strict:
            self.load(selected)
            self.assertEqual(strict.call_count, 2)
            self.load(selected)
            self.assertEqual(strict.call_count, 3)

    def test_mutable_document_cannot_change_later_admission(self):
        selected = self.create()
        document = self.load(selected)
        document.records[0].document["text"] = "Changed mutable cache"
        document.metadata["selection_origin"]["records"][0]["offset"] = 0
        self.assertEqual(self.load(selected).records[0].text, "Exact text one.")

    def test_warm_validation_and_factory_reuse_do_not_capture_whole_catalog(self):
        selected = self.create()
        from vntts.selected_story_snapshot import read_regular_bytes as read

        def refuse_catalog_capture(path):
            self.assertNotEqual(Path(path), self.source)
            return read(path)

        with patch(
            "vntts.selected_story_snapshot.read_regular_bytes",
            side_effect=refuse_catalog_capture,
        ):
            self.assertEqual(self.load(selected).records[0].line_id, "one")
            self.assertEqual(self.create(), selected)

    def test_warm_validation_rejects_late_source_mutation_after_record_checks(self):
        selected = self.create()
        from vntts.selected_story_snapshot import _record_digest

        def change_origin(record):
            digest = _record_digest(record)
            self.source.write_bytes(self.source.read_bytes() + b"\n")
            return digest

        with patch(
            "vntts.selected_story_snapshot._record_digest", side_effect=change_origin
        ):
            with self.assertRaisesRegex(StoryIndexError, "origin checksum changed"):
                self.load(selected)

    def test_warm_receipt_rows_cannot_be_mutated(self):
        self.create()
        receipt = next(iter(_source_receipts.values()))
        with self.assertRaises(TypeError):
            receipt.rows["one"] = (0, 1, "changed")

    def test_nested_selected_origin_is_rejected_before_recursive_parse(self):
        selected = self.create()
        selected_payload = selected.read_bytes()
        rows = [json.loads(row) for row in self.source.read_text().splitlines()]
        rows[0]["selection_origin"] = json.loads(selected_payload.splitlines()[0])[
            "selection_origin"
        ]
        self.source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        changed_checksum = sha256_file(self.source)
        self.change_snapshot(
            lambda selected_rows: selected_rows[0]["selection_origin"].update(
                story_index_sha256=changed_checksum
            )
        )
        _source_receipts.clear()
        with self.assertRaisesRegex(StoryIndexError, "complete catalog"):
            self.load(selected)

    def test_rejects_witness_or_immutable_record_changes(self):
        selected = self.create()
        original = selected.read_bytes()
        changes = (
            lambda rows: rows[0]["selection_origin"]["records"][0].update(offset=0),
            lambda rows: rows[0]["selection_origin"]["records"][0].update(length=1),
            lambda rows: rows[1].update(
                text="Changed", text_sha256=hashlib.sha256(b"Changed").hexdigest()
            ),
            lambda rows: rows[1].update(chapter="other"),
            lambda rows: rows[1].update(context={"producer": [999]}),
            lambda rows: rows[1].update(context={"producer": [True]}),
            lambda rows: rows[0].update(game="Changed"),
            lambda rows: rows.pop(),
            lambda rows: rows.append(dict(rows[1], line_id="extra")),
        )
        for change in changes:
            with self.subTest(change=change):
                selected.write_bytes(original)
                self.change_snapshot(change)
                with self.assertRaises(StoryIndexError):
                    self.load(selected)

    def test_origin_race_is_rejected_before_publication(self):
        from vntts.selected_story_snapshot import sha256_file as original_hash

        def changed_hash(path):
            if path == self.source:
                return "0" * 64
            return original_hash(path)

        with patch(
            "vntts.selected_story_snapshot.sha256_file", side_effect=changed_hash
        ):
            with self.assertRaisesRegex(StoryIndexError, "changed while read"):
                self.create()
        self.assertFalse(self.directory.exists())

    def semantic_source(self):
        write_verified_source_story(self.source)
        self.checksum = sha256_file(self.source)
        return create_selected_story_snapshot(
            self.source, self.checksum, ("test:0",), ("chapter:24006",), self.directory
        )

    def test_projects_and_validates_semantic_evidence(self):
        selected = self.semantic_source()
        document = self.load(selected)
        self.assertEqual(
            document.metadata["source_audio_semantics"]["applied_count"], 1
        )
        self.assertTrue(
            selected.with_name("source-audio-semantic-evidence.json").is_file()
        )
        original_evidence = self.source.with_name("source-audio-semantic-evidence.json")
        original_evidence.write_bytes(original_evidence.read_bytes() + b"\n")
        with self.assertRaisesRegex(
            StoryIndexError, "origin evidence checksum changed"
        ):
            self.load(selected)

    def test_selected_sidecar_tamper_is_rejected(self):
        selected = self.semantic_source()
        evidence = selected.with_name("source-audio-semantic-evidence.json")
        evidence.write_bytes(evidence.read_bytes() + b"\n")
        with self.assertRaisesRegex(StoryIndexError, "binding changed"):
            self.load(selected)

    def test_full_semantic_admission_checks_unselected_annotations(self):
        write_verified_source_story(self.source)
        rows = [json.loads(row) for row in self.source.read_text().splitlines()]
        rows.append(dict(rows[1], line_id="unselected", chapter="other"))
        rows[0]["line_count"] = 2
        self.source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.checksum = sha256_file(self.source)
        with self.assertRaisesRegex(StoryIndexError, "semantic evidence changed"):
            create_selected_story_snapshot(
                self.source,
                self.checksum,
                ("test:0",),
                ("chapter:24006",),
                self.directory,
            )
        self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
