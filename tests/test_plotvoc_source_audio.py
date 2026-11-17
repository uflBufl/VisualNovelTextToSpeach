import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from r1999extractor.source_audio_policy import apply_main_story_source_audio_policy
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_manifest import write_voice_manifest

from vntts.authoring.queue_builder import plan_generation_queue
from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.game_content_importer import Reverse1999GameImporter
from vntts.pregeneration_setup import open_reading_catalog
from vntts.prepared_dialogue_store import PreparedDialogueStore
from vntts.source_audio_semantics import (
    reset_stale_source_audio_semantics,
    source_audio_semantics_need_migration,
)

QUOTE = '"What really scared me was not the threat to my life, but the possibility of dying ignorant."'


def plotvoc_story(root, *, timed=False, kind="main_story", wait=None):
    records = []
    for sequence, speaker, text in (
        (53, "Narrator", QUOTE),
        (58, "Marcus", "This was your plan, madam ...?"),
        (59, "Vertin", QUOTE),
        (60, "Marcus", "I know what to do now."),
    ):
        records.append(
            {
                "record_type": "line",
                "line_id": f"reverse1999:100631:{sequence}",
                "chapter": "100631",
                "sequence": sequence,
                "story_order": sequence,
                "speaker": speaker,
                "voice_character": speaker,
                "text": text,
                "kind": "narration" if speaker == "Narrator" else "dialogue",
                "speakable": True,
                "collection_id": "story",
                "collection_kind": kind,
                "source_audio_status": "absent",
                "source_audio_completeness": "unknown",
            }
        )
    records[2]["story_audio_cues"] = [
        {
            "cue_index": 1,
            "source_audio_id": "610018168",
            "source_audio_status": "available",
            "audio_status": "installed",
            "audio_reason": "resolved_local_media",
            "source_bank": "plotvoc_npc512101chapter6_part05.bnk",
            "source_event": "play_plotvoc_chap6_npc512101_11129",
            "source_media_ids": [927465334],
            "available_media_ids": [927465334],
            "localized_parameter_2": 0.0,
            "localized_parameter_5": 0.0,
            "parameter_code_1": 0,
            "parameter_code_3": 1,
            "parameter_code_6": 1,
            "scalar_parameter_4": 1.0,
        }
    ]
    metadata = {
        "game": "Reverse: 1999",
        "language": "en",
        "collections": [
            {"collection_id": "story", "kind": kind, "title": "Chapter 6", "order": 6}
        ],
    }
    if timed:
        apply_main_story_source_audio_policy(metadata, records)
        metadata["source_audio_completion"] = "verified-media-duration-seconds"
        records[2].update(
            source_audio_duration_seconds=7.253333,
            source_audio_duration_media_id=927465334,
            source_audio_duration_media_sha256="a" * 64,
            source_audio_duration_sample_rate=24000,
            source_audio_duration_sample_count=174080,
            source_audio_duration_decoder="native-decoder",
        )
        if wait is not None:
            records[2]["source_audio_wait_seconds"] = wait
    return write_story_index_document(root / "story-index.jsonl", metadata, records)


class PlotVocSourceAudioTests(unittest.TestCase):
    def test_old_untimed_catalog_excludes_main_plotvoc_before_duration_is_ready(self):
        with TemporaryDirectory() as directory:
            story = plotvoc_story(Path(directory))
            original = story.path.read_bytes()
            preloader = ChapterVoicePreloader.load_optional(story.path)
            line = preloader.line_for_id("reverse1999:100631:59")
            self.assertTrue(line.has_original_speech)
            self.assertFalse(line.has_full_source_audio)
            self.assertEqual(line.source_audio_id, "610018168")
            self.assertFalse(
                preloader.line_for_id("reverse1999:100631:53").has_original_speech
            )
            self.assertEqual(story.path.read_bytes(), original)

    def test_timed_main_plotvoc_ignores_old_generated_archive_authority(self):
        with TemporaryDirectory() as directory:
            story = plotvoc_story(Path(directory), timed=True)
            current = ChapterVoicePreloader.load_optional(story.path)
            original = current.line_for_id("reverse1999:100631:59")
            stale = replace(
                original,
                source_audio_status="absent",
                source_audio_id=None,
                source_audio_duration_seconds=None,
                source_audio_completeness="unknown",
                source_audio_authoritative=False,
                source_audio_main_story=False,
            )
            store = PreparedDialogueStore(
                (("reverse1999", ChapterVoicePreloader((stale,)).store),),
                (current.store,),
            )
            self.assertTrue(store.line_for_id(stale.line_id).has_full_source_audio)

    def test_source_projection_applies_to_sqlite_hydration_too(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = plotvoc_story(root)
            with (
                patch("vntts.pregeneration_setup.story_catalog_minimum_bytes", 1),
                patch(
                    "vntts.pregeneration_setup.get_local_data_directory",
                    return_value=root,
                ),
            ):
                catalog = open_reading_catalog(story.path, game_id="reverse1999")
                self.addCleanup(catalog.store.catalog.close)
                line = catalog.store.line_for_id("reverse1999:100631:59")
                self.assertTrue(line.has_original_speech)
                self.assertEqual(line.source_audio_id, "610018168")

    def test_side_story_plotvoc_requires_asr_before_excluding_tts(self):
        with TemporaryDirectory() as directory:
            story = plotvoc_story(Path(directory), timed=True, kind="character_story")
            line = ChapterVoicePreloader.load_optional(story.path).line_for_id(
                "reverse1999:100631:59"
            )
            self.assertEqual(line.source_audio_status, "available")
            self.assertFalse(line.has_original_speech)
            self.assertEqual(line.source_audio_completeness, "unknown")
            self.assertTrue(
                Reverse1999GameImporter()._timed_story_requires_asr(
                    story.path, {line.line_id}
                )
            )

    def test_queue_excludes_main_plotvoc_but_keeps_side_story_until_asr(self):
        for kind in ("main_story", "character_story"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                root = Path(directory)
                story = plotvoc_story(root, timed=True, kind=kind)
                manifest = root / "voices.json"
                write_voice_manifest(manifest, {"version": 2, "voices": []})
                queue = plan_generation_queue(story, (), manifest)
                line_ids = {row["line_id"] for row in queue.items}
                self.assertEqual(
                    "reverse1999:100631:59" in line_ids, kind != "main_story"
                )
                self.assertIn("reverse1999:100631:53", line_ids)
                self.assertEqual(
                    Reverse1999GameImporter()._timed_story_requires_asr(
                        story.path, {"reverse1999:100631:59"}
                    ),
                    kind != "main_story",
                )

    def test_cue_wait_preserves_exact_wem_duration_and_waits_for_scheduled_end(self):
        with TemporaryDirectory() as directory:
            story = plotvoc_story(Path(directory), timed=True, wait=9.5)
            self.assertEqual(
                story.records[2].document["source_audio_duration_seconds"], 7.253333
            )
            line = ChapterVoicePreloader.load_optional(story.path).line_for_id(
                "reverse1999:100631:59"
            )
            self.assertTrue(line.has_full_source_audio)
            self.assertEqual(line.source_audio_duration_seconds, 9.5)

    def test_preparation_migrates_nested_cue_without_rewriting_source(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = plotvoc_story(root)
            original = story.path.read_bytes()
            self.assertTrue(source_audio_semantics_need_migration(story))
            successor = reset_stale_source_audio_semantics(
                story, root / "prepared" / "story-index.jsonl"
            )
            self.assertEqual(successor.records[2].source_audio_status, "available")
            self.assertEqual(
                successor.records[2].document["source_audio_completeness"], "full"
            )
            self.assertEqual(story.path.read_bytes(), original)
