import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.generated_audio import load_generated_audio_document
from vntts_artifacts.story_index import (
    load_story_index_document,
    write_story_index_document,
)

from tests.authoring_fixtures import tree_hashes
from tests.pregeneration_fixtures import InProcessPocketGenerator
from tests.story_fixtures import semantic_evidence_document, semantic_evidence_entry
from vntts.document_identity import sha256_file
from vntts.game_pack import import_game_pack
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.pregeneration_pack import (
    OfflinePackError,
    OfflinePackPublisher,
    inspect_story_audio,
)
from vntts.pregeneration_queue import PregenerationInputStore
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import VoicePlanStore
from vntts.selected_story_snapshot import (
    _source_receipts,
    create_selected_story_snapshot,
)
from vntts.settings import AppSettings
from vntts.source_audio_semantics import (
    SEMANTIC_EVIDENCE_METHOD,
    load_source_audio_semantic_evidence,
)


class SelectedSnapshotPackTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "catalog" / "story-index.jsonl"
        self.source.parent.mkdir()
        _source_receipts.clear()
        self.addCleanup(_source_receipts.clear)
        self.records = [
            {
                "record_type": "line",
                "line_id": f"{kind}:{chapter}",
                "chapter": chapter,
                "sequence": sequence,
                "speaker": "Rhiannon",
                "text": f"{kind.title()} chapter {chapter}.",
                "text_sha256": hashlib.sha256(
                    f"{kind.title()} chapter {chapter}.".encode()
                ).hexdigest(),
                "collection_id": f"chapter-{chapter}",
                "source_audio_status": "available" if kind == "original" else "absent",
                **(
                    {
                        "source_media_ids": [int(chapter)],
                        "available_media_ids": [int(chapter)],
                    }
                    if kind == "original"
                    else {}
                ),
            }
            for chapter in ("1", "2")
            for sequence, kind in enumerate(("original", "generated"), start=1)
        ]
        write_story_index_document(
            self.source,
            {
                "game": "Synthetic Game",
                "game_version": "1.0",
                "language": "en",
                "collections": [
                    {
                        "collection_id": f"chapter-{chapter}",
                        "title": f"Chapter {chapter}",
                        "kind": "story",
                        "order": int(chapter),
                    }
                    for chapter in ("1", "2")
                ],
            },
            self.records,
        )
        self.source_sha256 = sha256_file(self.source)

    def prepare(self, name, chapters, *, model_sha256="b" * 64, first_duration=1.0):
        root = self.root / name
        line_ids = tuple(
            record["line_id"]
            for record in self.records
            if record["chapter"] in chapters
        )
        story_ids = tuple(f"chapter-{chapter}" for chapter in chapters)
        snapshot = create_selected_story_snapshot(
            self.source, self.source_sha256, line_ids, story_ids, root / "snapshot"
        )
        document = load_story_index_document(snapshot)
        records = [record.to_record() for record in document.records]
        entries = []
        for record in records:
            if record["source_audio_status"] != "available":
                continue
            media_sha256 = hashlib.sha256(record["line_id"].encode()).hexdigest()
            entry, entry_id = semantic_evidence_entry(
                line_id=record["line_id"],
                text=record["text"],
                displayed_text_sha256=hashlib.sha256(
                    record["text"].encode()
                ).hexdigest(),
                media_id=None,
                media_sha256=media_sha256,
                model_sha256=model_sha256,
            )
            entries.append(entry)
            duration = first_duration if record["chapter"] == "1" else 1.0
            record.update(
                source_audio_duration_seconds=duration,
                source_audio_duration_media_id=int(record["chapter"]),
                source_audio_duration_media_sha256=media_sha256,
                source_audio_duration_sample_rate=24000,
                source_audio_duration_sample_count=int(duration * 24000),
                source_audio_duration_decoder="synthetic",
                source_audio_completeness="full",
                source_audio_completeness_reason="exact-normalized-asr-transcript",
                source_audio_semantic_evidence_entry_id=entry_id,
            )
        entries.sort(
            key=lambda entry: (
                entry["locale"],
                entry["media_sha256"],
                entry["normalized_displayed_text_sha256"],
            )
        )
        evidence, evidence_id = semantic_evidence_document(
            entries,
            model_sha256=model_sha256,
            source_story_index_sha256=self.source_sha256,
        )
        classified = root / "classified" / "story-index.jsonl"
        classified.parent.mkdir()
        evidence_path = classified.with_name("source-audio-semantic-evidence.json")
        atomic_write_json(evidence_path, evidence, sort_keys=True)
        for record in records:
            if record.get("source_audio_semantic_evidence_entry_id"):
                record["source_audio_semantic_evidence_id"] = evidence_id
        metadata = document.metadata | {
            "source_audio_completion": "verified-media-duration-seconds",
            "source_audio_semantics": {
                "evidence_id": evidence_id,
                "evidence_sha256": sha256_file(evidence_path),
                "method": SEMANTIC_EVIDENCE_METHOD,
                "selected_chapters": list(chapters),
                "applied_count": len(entries),
            },
        }
        write_story_index_document(classified, metadata, records)
        jobs = PregenerationJobStore(root / "jobs")
        job = jobs.create_or_resume(inspect_story_index(classified), story_ids)
        plan = VoicePlanStore(jobs).create(job, AppSettings())
        prepared = PregenerationInputStore(jobs).materialize(job, plan)
        generated = InProcessPocketGenerator().generate(prepared, plan)
        return job, prepared, generated

    def test_independent_subsets_keep_both_semantic_routes_and_are_portable(self):
        first = self.prepare("first", ("1",))
        base = OfflinePackPublisher().publish(*first)
        base_hashes = tree_hashes(base.manifest.parent)
        second = self.prepare("second", ("2",))
        successor = OfflinePackPublisher(base_pack=base.manifest).publish(*second)
        document = load_story_index_document(successor.imported.story_index)
        self.assertEqual(
            tuple(record.line_id for record in document.records),
            ("original:1", "generated:1", "original:2", "generated:2"),
        )
        evidence = load_source_audio_semantic_evidence(
            successor.imported.source_audio_semantic_evidence, document
        )
        self.assertEqual(
            {
                line_id
                for entry in evidence["entries"]
                for line_id in entry["source_line_ids"]
            },
            {"original:1", "original:2"},
        )
        self.assertEqual(
            document.metadata["source_audio_semantics"]["applied_count"], 2
        )
        self.assertNotIn("selection_origin", document.metadata)
        library = GeneratedAudioLibrary.load_optional(
            successor.imported.generated_audio_manifest
        )
        self.assertEqual(
            {
                record.line_id
                for record in load_generated_audio_document(
                    successor.imported.generated_audio_manifest
                ).records
            },
            {"generated:1", "generated:2"},
        )
        for record in document.records:
            if record.line_id.startswith("generated:"):
                self.assertIsNotNone(library.find(record.line_id, record.text_sha256))
        content = inspect_story_index(self.source)
        for selection_id in ("chapter-1", "chapter-2"):
            coverage = inspect_story_audio(
                content,
                selection_id,
                PregenerationJobStore(self.root / "unused"),
                imported_pack=successor.imported,
            )
            self.assertEqual(
                (coverage.original, coverage.generated, coverage.missing), (1, 1, 0)
            )
        self.assertEqual(tree_hashes(base.manifest.parent), base_hashes)
        self.source.unlink()
        _source_receipts.clear()
        from vntts.game_pack import _validate_semantic_evidence

        _validate_semantic_evidence.cache_clear()
        self.assertEqual(
            import_game_pack(successor.manifest).story_index,
            successor.imported.story_index,
        )

    def test_current_subset_wins_overlapping_annotated_row(self):
        base = OfflinePackPublisher().publish(*self.prepare("first", ("1",)))
        current = self.prepare("current", ("1", "2"), first_duration=3.0)
        successor = OfflinePackPublisher(base_pack=base.manifest).publish(*current)
        document = load_story_index_document(successor.imported.story_index)
        self.assertEqual(
            document.find("original:1").document["source_audio_duration_seconds"], 3.0
        )
        self.assertEqual(len(document.records), 4)
        self.assertEqual(
            len(
                load_source_audio_semantic_evidence(
                    successor.imported.source_audio_semantic_evidence, document
                )["entries"]
            ),
            2,
        )

    def test_stale_catalog_refuses_successor_and_preserves_old_pack(self):
        base = OfflinePackPublisher().publish(*self.prepare("first", ("1",)))
        current = self.prepare("current", ("2",))
        before = tree_hashes(base.manifest.parent)
        self.source.write_bytes(self.source.read_bytes() + b"\n")
        with self.assertRaisesRegex(OfflinePackError, "source story.*changed"):
            OfflinePackPublisher(base_pack=base.manifest).publish(*current)
        self.assertEqual(tree_hashes(base.manifest.parent), before)
        self.assertFalse(any(current[1].directory.parent.glob("game-packs/*")))

    def test_stale_origin_refuses_first_selected_pack(self):
        current = self.prepare("current", ("2",))
        self.source.write_bytes(self.source.read_bytes() + b"\n")
        with self.assertRaisesRegex(OfflinePackError, "catalog|source story"):
            OfflinePackPublisher().publish(*current)
        self.assertFalse(any(current[1].directory.parent.glob("game-packs/*")))

    def test_catalog_race_after_story_merge_refuses_atomic_publication(self):
        base = OfflinePackPublisher().publish(*self.prepare("first", ("1",)))
        current = self.prepare("current", ("2",))
        before = tree_hashes(base.manifest.parent)

        def change_catalog(phase, *_args, **_kwargs):
            if phase == "story-and-voices":
                self.source.write_bytes(self.source.read_bytes() + b"\n")

        with patch(
            "vntts.pregeneration_pack._record_publication_phase",
            side_effect=change_catalog,
        ):
            with self.assertRaisesRegex(OfflinePackError, "catalog|source story"):
                OfflinePackPublisher(base_pack=base.manifest).publish(*current)
        self.assertEqual(tree_hashes(base.manifest.parent), before)
        self.assertFalse(any(current[1].directory.parent.glob("game-packs/*")))

    def test_incompatible_model_fails_atomically(self):
        base = OfflinePackPublisher().publish(*self.prepare("first", ("1",)))
        current = self.prepare("current", ("2",), model_sha256="d" * 64)
        before = tree_hashes(base.manifest.parent)
        with self.assertRaisesRegex(OfflinePackError, "incompatible models"):
            OfflinePackPublisher(base_pack=base.manifest).publish(*current)
        self.assertEqual(tree_hashes(base.manifest.parent), before)
        self.assertFalse(any(current[1].directory.parent.glob("game-packs/*")))

    def test_malformed_sidecar_fails_atomically(self):
        base = OfflinePackPublisher().publish(*self.prepare("first", ("1",)))
        job, prepared, generated = self.prepare("current", ("2",))
        before = tree_hashes(base.manifest.parent)
        evidence = prepared.source_audio_semantic_evidence
        evidence.write_text("{malformed")
        document = load_story_index_document(prepared.story_index)
        document.metadata["source_audio_semantics"]["evidence_sha256"] = sha256_file(
            evidence
        )
        write_story_index_document(
            prepared.story_index, document.metadata, document.records
        )
        prepared = replace(
            prepared,
            story_index_sha256=sha256_file(prepared.story_index),
            source_audio_semantic_evidence_sha256=sha256_file(evidence),
        )
        with self.assertRaisesRegex(OfflinePackError, "semantic evidence"):
            OfflinePackPublisher(base_pack=base.manifest).publish(
                job, prepared, generated
            )
        self.assertEqual(tree_hashes(base.manifest.parent), before)
        self.assertFalse(any(prepared.directory.parent.glob("game-packs/*")))


if __name__ == "__main__":
    unittest.main()
