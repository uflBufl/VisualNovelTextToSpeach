import hashlib
import io
import json
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from r1999extractor.source_audio_policy import apply_main_story_source_audio_policy
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
        from vntts.pregeneration_setup import _cached_story_index_document

        _cached_story_index_document.cache_clear()
        self.addCleanup(_cached_story_index_document.cache_clear)
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

    def plotvoc_source(self):
        self.records[0].update(
            collection_kind="main_story",
            source_audio_status="absent",
            source_bank=None,
            source_media_ids=[],
            available_media_ids=[],
            story_audio_cues=[
                {
                    "cue_index": 1,
                    "source_audio_id": "610018168",
                    "source_audio_status": "available",
                    "source_bank": "plotvoc_npc512101chapter6_part05.bnk",
                    "source_event": "play_plotvoc_chap6_npc512101_11129",
                    "source_media_ids": [927465334],
                    "available_media_ids": [927465334],
                }
            ],
        )
        write_story_index_document(self.source, self.metadata, self.records)
        self.checksum = sha256_file(self.source)
        return self.create()

    def test_accepts_origin_bound_plotvoc_projection_and_timing_successor(self):
        from vntts.source_audio_semantics import reset_stale_source_audio_semantics

        selected = self.plotvoc_source()
        original = self.source.read_bytes()
        successor = reset_stale_source_audio_semantics(
            self.load(selected), self.root / "prepared" / "story-index.jsonl"
        )
        records = [record.to_record() for record in successor.records]
        records[0].update(
            source_audio_duration_seconds=1.0,
            source_audio_duration_media_id=927465334,
            source_audio_duration_media_sha256="a" * 64,
            source_audio_duration_sample_rate=24000,
            source_audio_duration_sample_count=24000,
            source_audio_duration_decoder="native",
            source_audio_wait_seconds=1.5,
        )
        write_story_index_document(
            successor.path,
            {
                **successor.metadata,
                "source_audio_completion": "verified-media-duration-seconds",
            },
            records,
        )
        verified = self.load(successor.path)
        self.assertEqual(verified.records[0].source_audio_status, "available")
        self.assertEqual(verified.records[0].source_audio_id, "610018168")
        self.assertEqual(verified.records[0].document["source_audio_cue_index"], 1)
        self.assertEqual(verified.records[0].document["source_audio_wait_seconds"], 1.5)
        self.assertEqual(self.source.read_bytes(), original)

    def test_plotvoc_projection_does_not_authorize_other_source_or_text_changes(self):
        selected = self.plotvoc_source()
        original = selected.read_bytes()
        for projected in (False, True):
            for field, value in (
                ("source_bank", "unrelated.bnk"),
                ("source_media_ids", [99]),
                ("available_media_ids", [99]),
                ("source", "unrelated-source"),
                ("text", "Unrelated text."),
            ):
                with self.subTest(projected=projected, field=field):
                    document = self.load(selected)
                    records = [record.to_record() for record in document.records]
                    metadata = dict(document.metadata)
                    if projected:
                        apply_main_story_source_audio_policy(metadata, records)
                    records[0][field] = value
                    write_story_index_document(selected, metadata, records)
                    with self.assertRaisesRegex(
                        StoryIndexError, "origin record changed"
                    ):
                        self.load(selected)
                    selected.write_bytes(original)

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

    def test_receipt_only_serializes_records_in_the_current_selection(self):
        from vntts.selected_story_snapshot import _record_digest

        with patch(
            "vntts.selected_story_snapshot._record_digest", wraps=_record_digest
        ) as digest:
            selected = self.create()
            self.load(selected)
            self.assertEqual(
                {call.args[0]["line_id"] for call in digest.call_args_list},
                {"one", "two"},
            )
            digest.reset_mock()
            later = create_selected_story_snapshot(
                self.source,
                self.checksum,
                ("later",),
                ("later",),
                self.root / "later-selection",
            )
            self.load(later)
            self.assertEqual(
                {call.args[0]["line_id"] for call in digest.call_args_list}, {"later"}
            )

    def test_transient_selected_row_swap_cannot_replace_admitted_bytes(self):
        from vntts.selected_story_snapshot import open_regular_binary

        selected = self.create()
        original = self.source.read_bytes()
        forged = original.replace(b'"producer": [1]', b'"producer": [9]', 1)
        self.assertNotEqual(forged, original)
        self.assertEqual(len(forged), len(original))
        self.change_snapshot(lambda rows: rows[1].update(context={"producer": [9]}))

        @contextmanager
        def transient_source(path):
            if Path(path) == self.source:
                with io.BytesIO(forged) as stream:
                    yield stream
            else:
                with open_regular_binary(path) as stream:
                    yield stream

        with patch(
            "vntts.selected_story_snapshot.open_regular_binary",
            side_effect=transient_source,
        ):
            with self.assertRaisesRegex(StoryIndexError, "origin checksum changed"):
                self.load(selected)
        self.assertEqual(self.source.read_bytes(), original)

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

    def test_inspection_seals_source_before_reusing_parse_for_selection(self):
        from vntts_artifacts.story_index import load_story_index_document

        from vntts.pregeneration_setup import (
            inspect_story_index,
            load_verified_story_index_document,
        )

        with patch(
            "vntts.story_index_snapshot.load_story_index_document",
            wraps=load_story_index_document,
        ) as strict:
            content = inspect_story_index(self.source)
            self.assertEqual(strict.call_count, 1)
            document = load_verified_story_index_document(self.source, self.checksum)
            document.metadata["game"] = "Poisoned mutable metadata"
            document.records[0].document["text"] = "Poisoned mutable record"
            selected = self.create()
            # Only the small selected source view requires another parse.
            self.assertEqual(strict.call_count, 2)
            actual = self.load(selected)
        self.assertEqual(actual.game, content.game)
        self.assertEqual(actual.records[0].text, "Exact text one.")

    def test_portable_inspection_without_semantic_evidence_cannot_admit_selection(self):
        from vntts.pregeneration_setup import inspect_story_index

        write_verified_source_story(self.source)
        self.source.with_name("source-audio-semantic-evidence.json").unlink()
        self.checksum = sha256_file(self.source)
        self.assertTrue(inspect_story_index(self.source).selections)
        self.assertFalse(_source_receipts)
        with self.assertRaisesRegex(StoryIndexError, "origin is invalid"):
            create_selected_story_snapshot(
                self.source,
                self.checksum,
                ("test:0",),
                ("chapter:24006",),
                self.directory,
            )
        self.assertFalse(self.directory.exists())

    def test_portable_inspection_with_unreadable_evidence_does_not_admit_source(self):
        from vntts.pregeneration_setup import inspect_story_index

        write_verified_source_story(self.source)
        evidence = self.source.with_name("source-audio-semantic-evidence.json")
        evidence.unlink()
        evidence.mkdir()
        self.assertTrue(inspect_story_index(self.source).selections)
        self.assertFalse(_source_receipts)

    def test_inspection_receipt_rejects_late_source_or_evidence_mutation(self):
        from vntts.pregeneration_setup import (
            PregenerationSetupError,
            StoryContentChanged,
            _cached_story_index_document,
            inspect_story_index,
        )
        from vntts.selected_story_snapshot import _build_receipt

        for evidence_changed in (False, True):
            with self.subTest(evidence_changed=evidence_changed):
                write_verified_source_story(self.source)
                self.checksum = sha256_file(self.source)
                changed = (
                    self.source.with_name("source-audio-semantic-evidence.json")
                    if evidence_changed
                    else self.source
                )

                def mutate_after_receipt(document, payload):
                    receipt = _build_receipt(document, payload)
                    changed.write_bytes(changed.read_bytes() + b"\n")
                    return receipt

                with patch(
                    "vntts.selected_story_snapshot._build_receipt",
                    side_effect=mutate_after_receipt,
                ):
                    with self.assertRaisesRegex(
                        PregenerationSetupError, "changed while read"
                    ) as caught:
                        inspect_story_index(self.source)
                self.assertIsInstance(
                    caught.exception.__cause__.__cause__, StoryContentChanged
                )
                self.assertFalse(_source_receipts)
                self.assertEqual(_cached_story_index_document.cache_info().currsize, 0)

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

    @unittest.skipUnless(os.name == "posix", "requires a POSIX change timestamp")
    def test_warm_origin_hash_survives_reopen_but_not_same_size_edit(self):
        selected = self.create()
        if next(iter(_source_receipts.values())).signature is None:
            self.skipTest("filesystem lacks a fine-grained change timestamp")
        with patch(
            "vntts.selected_story_snapshot.sha256_file", wraps=sha256_file
        ) as digest:
            self.load(selected)
            digest.assert_not_called()
            stat = self.source.stat()
            original = self.source.read_bytes()
            changed = original.replace(b"Exact text later", b"Other text later")
            self.assertNotEqual(original, changed)
            self.source.write_bytes(changed)
            os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            with self.assertRaisesRegex(StoryIndexError, "origin checksum changed"):
                self.load(selected)
            digest.assert_called_once_with(self.source)

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
        from vntts.pregeneration_setup import inspect_story_index

        write_verified_source_story(self.source)
        rows = [json.loads(row) for row in self.source.read_text().splitlines()]
        rows.append(dict(rows[1], line_id="unselected", chapter="other"))
        rows[0]["line_count"] = 2
        self.source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.checksum = sha256_file(self.source)
        self.assertTrue(inspect_story_index(self.source).selections)
        self.assertFalse(_source_receipts)
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
