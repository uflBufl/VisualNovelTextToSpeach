"""Main-story native speech is authoritative even with translated subtitles."""

import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from r1999extractor.source_audio_policy import MAIN_STORY_SOURCE_AUDIO_REASON
from vntts_artifacts.story_index import write_story_index_document

from tests.test_source_audio_semantics import publish_semantic_story, semantic_story
from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.game_content_importer import Reverse1999GameImporter
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.source_audio_semantics import (
    SourceAudioSemanticEvidenceError,
    load_source_audio_semantic_evidence,
    merge_source_audio_semantics,
    reset_stale_source_audio_semantics,
    source_audio_asr_needs_migration,
    source_audio_semantics_need_migration,
)


class MainStorySourceAudioPolicyTest(unittest.TestCase):
    def test_cumulative_pack_replaces_old_main_asr_without_requiring_asr_migration(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = semantic_story(root / "old", partial=True)
            records = [record.to_record() for record in story.records]
            for record in records:
                record["collection_kind"] = "main_story"
            story = write_story_index_document(story.path, story.metadata, records)
            self.assertFalse(source_audio_asr_needs_migration(story))
            metadata, merged, evidence_path = merge_source_audio_semantics(
                story, (story,), records, root / "pack"
            )
            self.assertNotIn("source_audio_semantics", metadata)
            self.assertIsNone(evidence_path)
            self.assertTrue(
                all(record["source_audio_completeness"] == "full" for record in merged)
            )

    def test_old_partial_main_story_skips_generation_without_asr(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = semantic_story(root / "old", current=True, partial=True)
            records = [record.to_record() for record in story.records]
            records[0]["collection_kind"] = "main_story"
            records[1].update(
                collection_kind="main_story", source_audio_status="absent"
            )
            story = write_story_index_document(
                story.path, {**story.metadata, "game": "Reverse: 1999"}, records
            )
            original = story.path.read_bytes()
            content = inspect_story_index(story.path, provider_id="reverse1999")
            self.assertEqual(content.selections[0].original_audio_lines, 1)
            self.assertEqual(content.selections[0].generation_lines, 1)
            preloader = ChapterVoicePreloader.load_optional(story.path)
            self.assertTrue(preloader.line_for_id("test:0").has_full_source_audio)
            self.assertFalse(
                preloader.line_for_id("test:0:native").has_full_source_audio
            )
            job = PregenerationJobStore(root / "jobs").create_or_resume(
                content, (content.selections[0].selection_id,)
            )
            importer = Reverse1999GameImporter(output_root=root / "imports")
            with (
                patch.object(importer, "_resolve_asr_model") as asr,
                patch.object(importer, "_prepare_timed_source_audio") as timing,
            ):
                successor = importer.prepare_source_audio_semantics(job)
            asr.assert_not_called()
            timing.assert_not_called()
            prepared = ChapterVoicePreloader.load_optional(successor.story_index)
            self.assertTrue(prepared.line_for_id("test:0").has_full_source_audio)
            self.assertEqual(story.path.read_bytes(), original)
            self.assertFalse(
                source_audio_semantics_need_migration(successor.story_index)
            )

    def test_policy_refresh_preserves_other_story_asr_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = semantic_story(root / "old", current=True, partial=True)
            evidence_path = story.path.with_name("source-audio-semantic-evidence.json")
            evidence = json.loads(evidence_path.read_bytes())
            records = [record.to_record() for record in story.records]
            records[0]["collection_kind"] = "main_story"
            records[1] = {
                **records[0],
                "line_id": "test:0:native",
                "sequence": 11,
                "collection_kind": "character_story",
            }
            evidence["entries"][0]["source_line_ids"].append("test:0:native")
            story, _ = publish_semantic_story(story, evidence, records)
            original = (story.path.read_bytes(), evidence_path.read_bytes())
            self.assertTrue(source_audio_semantics_need_migration(story))
            with self.assertRaisesRegex(
                SourceAudioSemanticEvidenceError, "source evidence directory"
            ):
                reset_stale_source_audio_semantics(
                    story, story.path.with_name("successor.jsonl")
                )
            self.assertEqual(
                (story.path.read_bytes(), evidence_path.read_bytes()), original
            )
            refreshed = reset_stale_source_audio_semantics(
                story, root / "new" / "story-index.jsonl"
            )
            main, character = (record.to_record() for record in refreshed.records)
            self.assertEqual(main["source_audio_completeness"], "full")
            self.assertEqual(
                main["source_audio_completeness_reason"], MAIN_STORY_SOURCE_AUDIO_REASON
            )
            self.assertNotIn("source_audio_semantic_evidence_entry_id", main)
            self.assertEqual(character["source_audio_completeness"], "partial")
            self.assertEqual(
                refreshed.metadata["source_audio_semantics"]["applied_count"], 1
            )
            load_source_audio_semantic_evidence(
                refreshed.path.with_name("source-audio-semantic-evidence.json"),
                refreshed,
            )
            self.assertEqual(
                (story.path.read_bytes(), evidence_path.read_bytes()), original
            )
            self.assertFalse(source_audio_semantics_need_migration(refreshed))
            self.assertIs(
                reset_stale_source_audio_semantics(refreshed, root / "unused.jsonl"),
                refreshed,
            )

    def test_main_original_still_requires_native_timing_for_playback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = semantic_story(root, current=True, partial=True)
            records = [record.to_record() for record in story.records]
            records[0]["collection_kind"] = "main_story"
            records[0].pop("source_audio_duration_sample_count")
            # Test the policy without an ASR sidecar masking the missing timing.
            for record in records:
                record.pop("source_audio_semantic_evidence_entry_id", None)
                record.pop("source_audio_semantic_evidence_id", None)
            metadata = deepcopy(story.metadata)
            metadata.pop("source_audio_semantics")
            story = write_story_index_document(story.path, metadata, records)
            preloader = ChapterVoicePreloader.load_optional(story.path)
            self.assertFalse(preloader.line_for_id("test:0").has_full_source_audio)
            self.assertFalse(
                Reverse1999GameImporter._semantic_evidence_is_current(
                    story.path, story, {"test:0"}
                )
            )
