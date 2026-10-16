import hashlib
import json
import math
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.story_index import (
    load_story_index_document,
    write_story_index_document,
)

from tests.story_fixtures import (
    semantic_evidence_document,
    semantic_evidence_entry,
    write_synthetic_game_pack,
    write_verified_source_story,
)
from vntts.source_audio_semantics import (
    SEMANTIC_EVIDENCE_PREPROCESSING,
    SourceAudioSemanticEvidenceError,
    canonical_document_sha256,
    load_source_audio_semantic_evidence,
    merge_source_audio_semantics,
    reset_stale_source_audio_semantics,
    semantic_preprocessing_is_current,
    semantic_text_sha256,
    source_audio_semantics_need_migration,
    validate_source_audio_semantic_evidence,
)


def semantic_story(root, *, current=False, partial=False, line_id="test:0"):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "story-index.jsonl"
    write_verified_source_story(path)
    story = load_story_index_document(path)
    evidence_path = root / "source-audio-semantic-evidence.json"
    evidence = json.loads(evidence_path.read_bytes())
    evidence["model"].pop("preprocessing", None)
    if current:
        evidence["model"]["preprocessing"] = SEMANTIC_EVIDENCE_PREPROCESSING
    entry = evidence["entries"][0]
    entry["source_line_ids"] = [line_id]
    if partial:
        entry.update(
            observed_transcript="Different spoken words",
            normalized_observed_text_sha256=semantic_text_sha256(
                "Different spoken words"
            ),
            verdict="partial",
            reason="asr-transcript-mismatch",
        )
    entry["entry_id"] = canonical_document_sha256(
        {k: v for k, v in entry.items() if k not in {"entry_id", "source_line_ids"}}
    )
    evidence["evidence_id"] = canonical_document_sha256(
        {k: v for k, v in evidence.items() if k not in {"evidence_id", "generated_at"}}
    )
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    metadata = deepcopy(story.metadata)
    metadata["source_audio_semantics"].update(
        evidence_id=evidence["evidence_id"],
        evidence_sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
    )
    record = story.records[0].to_record()
    record.update(
        line_id=line_id,
        source_audio_completeness=entry["verdict"],
        source_audio_completeness_reason=entry["reason"],
        source_audio_semantic_evidence_id=evidence["evidence_id"],
        source_audio_semantic_evidence_entry_id=entry["entry_id"],
    )
    native = {**record, "line_id": line_id + ":native", "sequence": 11}
    native.pop("source_audio_semantic_evidence_id")
    native.pop("source_audio_semantic_evidence_entry_id")
    native.update(
        source_audio_completeness="partial",
        source_audio_completeness_reason="short-cue",
    )
    return write_story_index_document(path, metadata, [record, native])


class SourceAudioSemanticEvidenceTest(unittest.TestCase):
    def test_punctuation_variants_keep_exact_evidence_through_merge(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = semantic_story(root / "source", current=True)
            records = []
            entries = []
            for index, text in enumerate(("I'm Cornerstone.", "I'm Cornerstone ...")):
                text_hash = hashlib.sha256(text.encode()).hexdigest()
                line_id = f"variant:{index}"
                entry, entry_id = semantic_evidence_entry(
                    line_id=line_id,
                    text=text,
                    displayed_text_sha256=text_hash,
                    media_id=7,
                )
                entries.append(entry)
                records.append(
                    {
                        **source.records[0].to_record(),
                        "line_id": line_id,
                        "sequence": index,
                        "text": text,
                        "text_sha256": text_hash,
                        "source_audio_semantic_evidence_entry_id": entry_id,
                    }
                )
            entries.sort(key=lambda entry: entry["displayed_text_sha256"])
            authority, evidence_id = semantic_evidence_document(entries)
            evidence_path = source.path.with_name("source-audio-semantic-evidence.json")
            evidence_path.write_text(json.dumps(authority), encoding="utf-8")
            metadata = deepcopy(source.metadata)
            metadata["source_audio_semantics"].update(
                evidence_id=evidence_id,
                evidence_sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                applied_count=2,
            )
            for record in records:
                record["source_audio_semantic_evidence_id"] = evidence_id
            source = write_story_index_document(source.path, metadata, records)
            self.assertEqual(
                len(
                    load_source_audio_semantic_evidence(evidence_path, source)[
                        "entries"
                    ]
                ),
                2,
            )
            records.sort(key=lambda record: record["text_sha256"], reverse=True)
            metadata, merged, merged_path = merge_source_audio_semantics(
                source, (source,), records, root / "merged"
            )
            published = write_story_index_document(
                root / "merged" / "story-index.jsonl", metadata, merged
            )
            self.assertEqual(
                load_source_audio_semantic_evidence(merged_path, published)["entries"],
                entries,
            )
            records[0]["text_sha256"] = records[1]["text_sha256"]
            records[0]["text"] = records[1]["text"]
            tampered = write_story_index_document(source.path, source.metadata, records)
            with self.assertRaisesRegex(SourceAudioSemanticEvidenceError, "changed"):
                load_source_audio_semantic_evidence(evidence_path, tampered)

    def test_reset_rejects_source_sidecar_symlink_and_hardlink_destinations(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = semantic_story(root / "legacy")
            evidence = story.path.parent / "source-audio-semantic-evidence.json"
            alias = root / "alias.jsonl"
            alias.hardlink_to(story.path)
            symlink = root / "symlink.jsonl"
            symlink.symlink_to(root / "new.jsonl")
            original = story.path.read_bytes(), evidence.read_bytes()
            for destination in (story.path, evidence, alias, symlink):
                with (
                    self.subTest(destination=destination),
                    self.assertRaises(SourceAudioSemanticEvidenceError),
                ):
                    reset_stale_source_audio_semantics(story, destination)
            self.assertEqual((story.path.read_bytes(), evidence.read_bytes()), original)

    def test_reset_rejects_source_change_during_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = semantic_story(root / "legacy")
            destination = root / "reset" / "story-index.jsonl"

            def change_source(*args):
                result = write_story_index_document(*args)
                story.path.write_bytes(story.path.read_bytes() + b"\n")
                return result

            with patch(
                "vntts.source_audio_semantics.write_story_index_document",
                side_effect=change_source,
            ):
                with self.assertRaisesRegex(
                    SourceAudioSemanticEvidenceError, "changed while read"
                ):
                    reset_stale_source_audio_semantics(story, destination)
            self.assertFalse(destination.exists())

    def test_cumulative_current_incompatible_models_still_fail(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old = semantic_story(root / "old", current=True)
            new = semantic_story(root / "new", current=True, line_id="test:new")
            evidence_path = new.path.parent / "source-audio-semantic-evidence.json"
            evidence = json.loads(evidence_path.read_bytes())
            evidence["model"]["snapshot"] = "another-model-snapshot"
            evidence["evidence_id"] = canonical_document_sha256(
                {
                    k: v
                    for k, v in evidence.items()
                    if k not in {"evidence_id", "generated_at"}
                }
            )
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            metadata = deepcopy(new.metadata)
            metadata["source_audio_semantics"].update(
                evidence_id=evidence["evidence_id"],
                evidence_sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            )
            records = [record.to_record() for record in new.records]
            records[0]["source_audio_semantic_evidence_id"] = evidence["evidence_id"]
            new = write_story_index_document(new.path, metadata, records)
            records = [r.to_record() for story in (old, new) for r in story.records]
            catalog = write_story_index_document(
                root / "catalog.jsonl",
                {"game": old.game, "language": old.language},
                records,
            )
            with self.assertRaisesRegex(
                SourceAudioSemanticEvidenceError, "incompatible models"
            ):
                merge_source_audio_semantics(
                    catalog, (old, new), records, root / "merged"
                )

    def test_legacy_reset_preserves_native_timing_and_is_idempotent(self):
        for partial in (False, True):
            with self.subTest(partial=partial), TemporaryDirectory() as directory:
                root = Path(directory)
                story = semantic_story(root / "legacy", partial=partial)
                original = story.path.read_bytes()
                self.assertTrue(source_audio_semantics_need_migration(story))
                self.assertFalse(
                    semantic_preprocessing_is_current(
                        load_source_audio_semantic_evidence(
                            story.path.parent / "source-audio-semantic-evidence.json",
                            story,
                        )
                    )
                )
                destination = root / "reset" / "story-index.jsonl"
                reset = reset_stale_source_audio_semantics(story, destination)
                expected = story.records[0].to_record()
                expected["source_audio_completeness"] = "unknown"
                for key in (
                    "source_audio_completeness_reason",
                    "source_audio_semantic_evidence_id",
                    "source_audio_semantic_evidence_entry_id",
                ):
                    expected.pop(key)
                self.assertEqual(reset.records[0].to_record(), expected)
                self.assertEqual(reset.records[1], story.records[1])
                self.assertNotIn("source_audio_semantics", reset.metadata)
                self.assertEqual(story.path.read_bytes(), original)
                self.assertFalse(source_audio_semantics_need_migration(reset))
                self.assertIs(
                    reset_stale_source_audio_semantics(reset, destination), reset
                )
                again = reset_stale_source_audio_semantics(story, destination)
                self.assertEqual(again.metadata, reset.metadata)
                self.assertEqual(again.records, reset.records)

    def test_current_evidence_passes_through_without_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = semantic_story(root / "current", current=True)
            destination = root / "unused.jsonl"
            self.assertFalse(source_audio_semantics_need_migration(story.path))
            self.assertIs(reset_stale_source_audio_semantics(story, destination), story)
            self.assertFalse(destination.exists())

    def test_tampered_sidecar_is_rejected_before_reset_or_migration_decision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = semantic_story(root / "legacy")
            evidence_path = story.path.parent / "source-audio-semantic-evidence.json"
            evidence_path.write_bytes(evidence_path.read_bytes() + b"\n")
            destination = root / "reset.jsonl"
            for operation in (
                lambda: source_audio_semantics_need_migration(story),
                lambda: reset_stale_source_audio_semantics(story, destination),
            ):
                with self.assertRaises(SourceAudioSemanticEvidenceError):
                    operation()
            self.assertFalse(destination.exists())

    def test_reset_preserves_and_validates_selected_origin(self):
        from vntts.document_identity import sha256_file
        from vntts.selected_story_snapshot import create_selected_story_snapshot
        from vntts.story_index_snapshot import load_story_index_snapshot

        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = semantic_story(root / "legacy")
            selected = create_selected_story_snapshot(
                story.path,
                sha256_file(story.path),
                tuple(record.line_id for record in story.records),
                ("chapter:24006",),
                root / "selected",
            )
            document = load_story_index_snapshot(selected, selected.read_bytes())
            reset = reset_stale_source_audio_semantics(
                document, root / "reset" / "story-index.jsonl"
            )
            self.assertEqual(
                reset.metadata["selection_origin"],
                document.metadata["selection_origin"],
            )
            validated = load_story_index_snapshot(reset.path, reset.path.read_bytes())
            self.assertEqual(validated.records, reset.records)

    def test_cumulative_merge_expires_legacy_and_retains_current_latest_owner(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old = semantic_story(root / "old")
            new = semantic_story(root / "new", current=True, line_id="test:new")
            records = [r.to_record() for story in (old, new) for r in story.records]
            original = deepcopy(records)
            catalog = write_story_index_document(
                root / "catalog.jsonl",
                {"game": old.game, "language": old.language},
                records,
            )
            metadata, merged, evidence_path = merge_source_audio_semantics(
                catalog, (old, new), records, root / "merged"
            )
            self.assertEqual(records, original)
            self.assertEqual(merged[0]["source_audio_completeness"], "unknown")
            self.assertNotIn("source_audio_semantic_evidence_entry_id", merged[0])
            self.assertEqual(merged[1], original[1])
            self.assertEqual(merged[2]["source_audio_completeness"], "full")
            self.assertEqual(merged[3], original[3])
            published = write_story_index_document(
                root / "merged" / "story-index.jsonl", metadata, merged
            )
            evidence = load_source_audio_semantic_evidence(evidence_path, published)
            self.assertTrue(semantic_preprocessing_is_current(evidence))
            self.assertEqual(len(evidence["entries"]), 1)
            # Replacing an old line uses the current owner's evidence.
            replacement = semantic_story(root / "replacement", current=True)
            _, replaced, _ = merge_source_audio_semantics(
                catalog,
                (old, replacement),
                [r.to_record() for r in replacement.records],
                root / "replaced",
            )
            self.assertEqual(replaced[0]["source_audio_completeness"], "full")

    def test_captured_empty_evidence_does_not_reopen_an_available_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "source-audio-semantic-evidence.json"
            path.write_text("replacement must not be read", encoding="utf-8")
            with patch("vntts.source_audio_semantics.read_regular_bytes") as read:
                with self.assertRaises(SourceAudioSemanticEvidenceError):
                    load_source_audio_semantic_evidence(path, payload=b"")
            read.assert_not_called()

    def test_invalid_extra_values_use_domain_error_for_objects_and_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_synthetic_game_pack(root, include_semantics=True)
            path = root / "source-audio-semantic-evidence.json"
            original = json.loads(path.read_text(encoding="utf-8"))
            for location in ("document", "entry"):
                for value in (math.nan, math.inf, -math.inf, {"not JSON"}):
                    with self.subTest(location=location, value=value):
                        document = json.loads(json.dumps(original))
                        target = (
                            document
                            if location == "document"
                            else document["entries"][0]
                        )
                        target["unexpected"] = value
                        with self.subTest(boundary="object"):
                            with self.assertRaises(SourceAudioSemanticEvidenceError):
                                validate_source_audio_semantic_evidence(document)
                        if isinstance(value, float):
                            path.write_text(json.dumps(document), encoding="utf-8")
                            with self.subTest(boundary="file"):
                                with self.assertRaises(
                                    SourceAudioSemanticEvidenceError
                                ):
                                    load_source_audio_semantic_evidence(path)

    def test_valid_extra_values_remain_lossless(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_synthetic_game_pack(root, include_semantics=True)
            path = root / "source-audio-semantic-evidence.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            entry = document["entries"][0]
            entry["extra"] = {"duration": 2.5, "labels": [None, True, "valid"]}
            entry["entry_id"] = canonical_document_sha256(
                {
                    key: value
                    for key, value in entry.items()
                    if key not in {"entry_id", "source_line_ids"}
                }
            )
            document["extra"] = [1, 2.5, False, None]
            document["evidence_id"] = canonical_document_sha256(
                {
                    key: value
                    for key, value in document.items()
                    if key not in {"evidence_id", "generated_at"}
                }
            )
            path.write_text(json.dumps(document), encoding="utf-8")
            self.assertEqual(load_source_audio_semantic_evidence(path), document)
