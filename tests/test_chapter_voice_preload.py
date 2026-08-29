import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from vntts.chapter_voice_preload import (
    ChapterDialogue,
    ChapterMatch,
    ChapterVoicePreloader,
)


def dialogue_document():
    return {
        "dialogue": [
            {
                "chapter": "24006",
                "sequence": 10,
                "speaker_name": "Kamuta",
                "text": "These old ones are enough to carry everyone.",
            },
            {
                "chapter": "24006",
                "sequence": 20,
                "speaker_name": "Fatutu",
                "text": "Besides, brother, you will dive in after him!",
            },
            {
                "chapter": "24006",
                "sequence": 30,
                "speaker_name": "Selone",
                "text": "The tide is changing.",
            },
            {
                "chapter": "99001",
                "sequence": 10,
                "speaker_name": "Fatutu",
                "text": "A completely different scene.",
            },
        ]
    }


def story_index_document():
    records = [
        {
            "record_type": "metadata",
            "schema": "vntts.story-index",
            "schema_version": 1,
            "line_count": len(dialogue_document()["dialogue"]),
            "source_audio_completion": "duration-seconds",
        }
    ]
    for position, dialogue in enumerate(dialogue_document()["dialogue"]):
        record = {
            "record_type": "line",
            "line_id": f"test:{position}",
            "chapter": dialogue["chapter"],
            "sequence": dialogue["sequence"],
            "speaker": dialogue["speaker_name"],
            "text": dialogue["text"],
            "kind": "dialogue",
        }
        if position == 0:
            record.update(
                source_audio_status="available",
                source_audio_id="voice-7",
                source_audio_duration_seconds=2.75,
            )
        records.append(record)
    return "\n".join(json.dumps(record) for record in records) + "\n"


class ChapterVoicePreloaderTest(unittest.TestCase):
    def test_ranks_upcoming_unique_speakers_after_matching_partial_dialogue(self):
        preloader = ChapterVoicePreloader.from_document(dialogue_document())

        recommendations = preloader.recommend(
            "Kamuta",
            "These old ones are enough to carry",
        )

        self.assertEqual(recommendations, ("Fatutu", "Selone"))

    def test_retains_chapter_during_short_following_ocr_observation(self):
        preloader = ChapterVoicePreloader.from_document(dialogue_document())
        preloader.recommend("Kamuta", "These old ones are enough to carry")

        recommendations = preloader.recommend("Narrator", "A")

        self.assertEqual(recommendations, ("Kamuta", "Fatutu", "Selone"))

    def test_does_not_guess_from_ambiguous_speaker_without_enough_text(self):
        preloader = ChapterVoicePreloader.from_document(dialogue_document())

        self.assertEqual(preloader.recommend("Fatutu", "Hi"), ())

    def test_optional_loader_tolerates_missing_and_invalid_index(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "dialogue.json"
            self.assertEqual(ChapterVoicePreloader.load_optional(path).dialogue, ())
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(ChapterVoicePreloader.load_optional(path).dialogue, ())
            path.write_text(story_index_document(), encoding="utf-8")
            self.assertEqual(len(ChapterVoicePreloader.load_optional(path).dialogue), 4)

    def test_exact_resolution_returns_stable_line_identity_and_text_hash(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "story.jsonl"
            path.write_text(story_index_document(), encoding="utf-8")
            preloader = ChapterVoicePreloader.load_optional(path)

            line = preloader.resolve_exact(
                "KAMUTA",
                "These old ones are enough to carry everyone.",
            )

        self.assertEqual(line.line_id, "test:0")
        self.assertEqual(
            line.text_sha256,
            hashlib.sha256(line.text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(line.source_audio_status, "available")
        self.assertEqual(line.source_audio_id, "voice-7")
        self.assertEqual(line.source_audio_duration_seconds, 2.75)

    def test_normalized_exact_resolution_tolerates_punctuation_only_ocr_drift(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "story.jsonl"
            path.write_text(story_index_document(), encoding="utf-8")
            preloader = ChapterVoicePreloader.load_optional(path)

            line, result = preloader.resolve_exact_with_result(
                "Kamuta",
                "These old ones are enough to carry everyone",
            )

        self.assertEqual(line.line_id, "test:0")
        self.assertEqual(result, "normalized-exact")

    def test_expected_resolution_allows_one_text_only_nameplate_recovery(self):
        text = "Only the explicit branch candidate may match."
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": 1,
                        "line_id": "line-a",
                        "speaker_name": "Ada",
                        "text": text,
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    },
                    {
                        "chapter": "1",
                        "sequence": 2,
                        "line_id": "line-b",
                        "speaker_name": "Bea",
                        "text": "Another branch.",
                        "text_sha256": hashlib.sha256(b"Another branch.").hexdigest(),
                    },
                ]
            }
        )

        line, result = preloader.resolve_exact_among(
            "corrupted nameplate",
            text,
            ("line-a", "line-b"),
        )

        self.assertEqual(line.line_id, "line-a")
        self.assertEqual(result, "expected-text-only")

    def test_expected_resolution_rejects_ambiguous_repeated_text(self):
        text = "The same repeated line."
        digest = hashlib.sha256(text.encode()).hexdigest()
        preloader = ChapterVoicePreloader.from_document(
            {
                "dialogue": [
                    {
                        "chapter": "1",
                        "sequence": sequence,
                        "line_id": f"line-{sequence}",
                        "speaker_name": "Ada",
                        "text": text,
                        "text_sha256": digest,
                    }
                    for sequence in (1, 2)
                ]
            }
        )

        line, result = preloader.resolve_exact_among(
            "Ada",
            text,
            ("line-1", "line-2"),
        )

        self.assertIsNone(line)
        self.assertEqual(result, "expected-ambiguous")

    def test_unique_prefix_resolves_full_indexed_dialogue(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "story.jsonl"
            path.write_text(story_index_document(), encoding="utf-8")
            preloader = ChapterVoicePreloader.load_optional(path)

            line = preloader.resolve_unique_prefix(
                "Kamuta",
                "These old ones are enough",
                candidate_filter=lambda candidate: candidate.line_id == "test:0",
            )

        self.assertEqual(line.line_id, "test:0")
        self.assertEqual(line.text, "These old ones are enough to carry everyone.")

    def test_unique_prefix_checks_current_chapter_before_global_corpus(self):
        first = ChapterDialogue(
            "chapter-1:1",
            "chapter-1",
            1,
            "Narrator",
            "The shared opening phrase continues here.",
            "a" * 64,
        )
        second = ChapterDialogue(
            "chapter-2:1",
            "chapter-2",
            1,
            "Narrator",
            "The shared opening phrase ends differently.",
            "b" * 64,
        )
        preloader = ChapterVoicePreloader((first, second))
        preloader.current_match = ChapterMatch("chapter-2", 0, 1.0)
        inspected = []

        line = preloader.resolve_unique_prefix(
            "Narrator",
            "The shared opening phrase",
            candidate_filter=lambda candidate: inspected.append(candidate) or True,
        )

        self.assertEqual(line, second)
        self.assertEqual(inspected, [second])

    def test_unique_prefix_keeps_global_fallback_outside_current_chapter(self):
        current = ChapterDialogue(
            "chapter-1:1",
            "chapter-1",
            1,
            "Narrator",
            "An unrelated current line.",
            "a" * 64,
        )
        fallback = ChapterDialogue(
            "chapter-2:1",
            "chapter-2",
            1,
            "Narrator",
            "The globally unique fallback line.",
            "b" * 64,
        )
        preloader = ChapterVoicePreloader((current, fallback))
        preloader.current_match = ChapterMatch("chapter-1", 0, 1.0)

        line = preloader.resolve_unique_prefix(
            "Narrator",
            "The globally unique fallback",
        )

        self.assertEqual(line, fallback)

    def test_unique_text_prefix_recovers_speaker_lost_by_ocr(self):
        hotelier = ChapterDialogue(
            "chapter-1:20",
            "chapter-1",
            20,
            "Hotelier",
            "You know, we once had a guest who brought a water horse inside.",
            "b" * 64,
        )
        narrator = ChapterDialogue(
            "chapter-1:19",
            "chapter-1",
            19,
            "Narrator",
            "An unrelated line establishes the current chapter.",
            "a" * 64,
        )
        preloader = ChapterVoicePreloader((narrator, hotelier))
        preloader.current_match = ChapterMatch("chapter-1", 19, 1.0)

        line = preloader.resolve_unique_prefix_by_text(
            "You know, we once had a guest who brought"
        )

        self.assertEqual(line, hotelier)
        self.assertEqual(preloader.current_match.sequence, 20)

    def test_unique_text_prefix_rejects_ambiguous_current_chapter(self):
        first = ChapterDialogue(
            "chapter-1:1",
            "chapter-1",
            1,
            "Hotelier",
            "The shared long opening ends one way.",
            "a" * 64,
        )
        second = ChapterDialogue(
            "chapter-1:2",
            "chapter-1",
            2,
            "Rhiannon",
            "The shared long opening ends another way.",
            "b" * 64,
        )
        preloader = ChapterVoicePreloader((first, second))
        preloader.current_match = ChapterMatch("chapter-1", 0, 1.0)

        line = preloader.resolve_unique_prefix_by_text("The shared long opening")

        self.assertIsNone(line)
        self.assertEqual(preloader.current_match.sequence, 0)

    def test_unique_text_prefix_does_not_escape_established_chapter(self):
        current = ChapterDialogue(
            "chapter-1:1",
            "chapter-1",
            1,
            "Narrator",
            "The current chapter line.",
            "a" * 64,
        )
        elsewhere = ChapterDialogue(
            "chapter-2:1",
            "chapter-2",
            1,
            "Hotelier",
            "The globally unique dialogue belongs elsewhere.",
            "b" * 64,
        )
        preloader = ChapterVoicePreloader((current, elsewhere))
        preloader.current_match = ChapterMatch("chapter-1", 1, 1.0)

        line = preloader.resolve_unique_prefix_by_text(
            "The globally unique dialogue belongs"
        )

        self.assertIsNone(line)

    def test_short_or_ambiguous_prefix_does_not_guess_dialogue(self):
        document = story_index_document()
        extra = {
            "record_type": "line",
            "line_id": "test:extra",
            "chapter": "24006",
            "sequence": 11,
            "speaker": "Kamuta",
            "text": "These old ones are enough to carry two people.",
            "kind": "dialogue",
        }
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "story.jsonl"
            path.write_text(document + json.dumps(extra) + "\n", encoding="utf-8")
            preloader = ChapterVoicePreloader.load_optional(path)

            short = preloader.resolve_unique_prefix("Kamuta", "These old")
            ambiguous = preloader.resolve_unique_prefix(
                "Kamuta",
                "These old ones are enough to carry",
            )

        self.assertIsNone(short)
        self.assertIsNone(ambiguous)

    def test_unique_incomplete_prefix_waits_for_known_full_line(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "story.jsonl"
            path.write_text(story_index_document(), encoding="utf-8")
            preloader = ChapterVoicePreloader.load_optional(path)

            partial = preloader.is_unique_incomplete_prefix(
                "Kamuta",
                "These old ones are enough",
            )
            complete = preloader.is_unique_incomplete_prefix(
                "Kamuta",
                "These old ones are enough to carry everyone.",
            )

        self.assertTrue(partial)
        self.assertFalse(complete)

    def test_canonical_speaker_corrects_unique_high_confidence_ocr_drift(self):
        document = dialogue_document()
        document["dialogue"].append(
            {
                "chapter": "24006",
                "sequence": 40,
                "speaker_name": "Hotelier",
                "text": "Welcome.",
            }
        )
        preloader = ChapterVoicePreloader.from_document(document)

        self.assertEqual(preloader.canonical_speaker("Hoteller"), "Hotelier")
        self.assertEqual(preloader.canonical_speaker("Ada"), "Ada")

    def test_canonical_speaker_rejects_ambiguous_nearby_names(self):
        document = {
            "dialogue": [
                {
                    "chapter": "1",
                    "sequence": 1,
                    "speaker_name": "Annabel",
                    "text": "One.",
                },
                {
                    "chapter": "1",
                    "sequence": 2,
                    "speaker_name": "Annabelle",
                    "text": "Two.",
                },
            ]
        }
        preloader = ChapterVoicePreloader.from_document(document)

        self.assertEqual(preloader.canonical_speaker("Annabell"), "Annabell")

    def test_legacy_document_maps_installed_source_audio(self):
        document = dialogue_document()
        document["dialogue"][0].update(
            line_id="test:0",
            text_sha256=hashlib.sha256(
                document["dialogue"][0]["text"].encode("utf-8")
            ).hexdigest(),
            audio_status="installed",
            source_voice_id="legacy-7",
            display_seconds=1.0,
        )
        preloader = ChapterVoicePreloader.from_document(document)

        line = preloader.resolve_exact(
            "Kamuta",
            "These old ones are enough to carry everyone.",
        )

        self.assertEqual(line.source_audio_status, "available")
        self.assertEqual(line.source_audio_id, "legacy-7")
        self.assertIsNone(line.source_audio_duration_seconds)

    def test_loader_bridges_older_shared_contract_reader(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "story.jsonl"
            path.write_text(story_index_document(), encoding="utf-8")
            legacy_line = SimpleNamespace(
                line_id="test:0",
                chapter="24006",
                sequence=10,
                speaker="Kamuta",
                text="These old ones are enough to carry everyone.",
                text_sha256=hashlib.sha256(
                    b"These old ones are enough to carry everyone."
                ).hexdigest(),
            )
            with patch(
                "vntts.chapter_voice_preload.load_story_index",
                return_value=({}, (legacy_line,)),
            ):
                preloader = ChapterVoicePreloader.load_optional(path)

        self.assertEqual(preloader.dialogue[0].source_audio_status, "available")
        self.assertEqual(preloader.dialogue[0].source_audio_id, "voice-7")
        self.assertEqual(
            preloader.dialogue[0].source_audio_duration_seconds,
            2.75,
        )

    def test_invalid_source_audio_completion_duration_is_ignored(self):
        document = dialogue_document()
        document["dialogue"][0].update(
            line_id="test:0",
            text_sha256=hashlib.sha256(
                document["dialogue"][0]["text"].encode("utf-8")
            ).hexdigest(),
            source_audio_status="available",
            source_audio_duration_seconds=-1,
        )

        line = ChapterVoicePreloader.from_document(document).resolve_exact(
            "Kamuta",
            "These old ones are enough to carry everyone.",
        )

        self.assertIsNone(line.source_audio_duration_seconds)

    def test_exact_resolution_rejects_partial_or_ambiguous_text(self):
        document = dialogue_document()
        duplicate = dict(document["dialogue"][0])
        duplicate["chapter"] = "other"
        document["dialogue"].append(duplicate)
        for index, entry in enumerate(document["dialogue"]):
            entry["line_id"] = f"test:{index}"
            entry["text_sha256"] = hashlib.sha256(
                entry["text"].encode("utf-8")
            ).hexdigest()
        preloader = ChapterVoicePreloader.from_document(document)

        self.assertIsNone(
            preloader.resolve_exact("Kamuta", "These old ones are enough")
        )
        self.assertIsNone(
            preloader.resolve_exact(
                "Kamuta",
                "These old ones are enough to carry everyone.",
            )
        )
        line, result = preloader.resolve_exact_with_result(
            "Kamuta",
            "These old ones are enough to carry everyone.",
        )
        self.assertIsNone(line)
        self.assertEqual(result, "ambiguous")

        line, result = preloader.resolve_exact_with_result(
            "Kamuta",
            "This line is not indexed.",
        )
        self.assertIsNone(line)
        self.assertEqual(result, "no-match")


if __name__ == "__main__":
    unittest.main()
