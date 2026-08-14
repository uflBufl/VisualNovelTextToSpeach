import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.chapter_voice_preload import ChapterVoicePreloader


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
        }
    ]
    for position, dialogue in enumerate(dialogue_document()["dialogue"]):
        records.append(
            {
                "record_type": "line",
                "line_id": f"test:{position}",
                "chapter": dialogue["chapter"],
                "sequence": dialogue["sequence"],
                "speaker": dialogue["speaker_name"],
                "text": dialogue["text"],
                "kind": "dialogue",
            }
        )
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


if __name__ == "__main__":
    unittest.main()
