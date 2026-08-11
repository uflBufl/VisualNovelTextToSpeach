import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.reverse1999_audition import (  # noqa: E402
    candidate_banks,
    chapter_tokens,
    filter_dialogue,
    save_speaker_mapping,
)
from vntts.reverse1999_audition_ui import Reverse1999AuditionDialog  # noqa: E402
from vntts.voice_reference_quality import VoiceReferenceMetrics  # noqa: E402


class Reverse1999AuditionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_filters_dialogue_by_chapter_and_selone_mention(self):
        dialogue = [
            {"chapter": "24006", "speaker_id": "310918", "text": "Selone!"},
            {"chapter": "24007", "speaker_id": "520301", "text": "Paddle out."},
        ]

        result = filter_dialogue(dialogue, query="selone", chapter="24006")

        self.assertEqual(result, [dialogue[0]])
        self.assertEqual(chapter_tokens("24006"), ("2_4", "2-4", "plot24"))

    def test_ranks_exact_npc_before_chapter_candidates(self):
        index = {
            "banks": [
                {
                    "path": "chapter.bnk",
                    "filename": "activityvoc_npc624901_2_4_part01.bnk",
                    "npc_ids": ["624901"],
                    "events": [{"media_ids": [12, 11]}],
                },
                {
                    "path": "exact.bnk",
                    "filename": "npc520301_other.bnk",
                    "npc_ids": ["520301"],
                    "events": [{"media_ids": [20]}],
                },
                {
                    "path": "unrelated.bnk",
                    "filename": "activityvoc_npc999999_3_1_part01.bnk",
                    "npc_ids": ["999999"],
                    "events": [{"media_ids": [30]}],
                },
            ]
        }

        result = candidate_banks(index, chapter="24006", speaker_id="520301")

        self.assertEqual(result[0].filename, "npc520301_other.bnk")
        self.assertEqual(result[1].media_ids, (11, 12))
        self.assertEqual(len(result), 2)

    def test_saves_mapping_atomically_and_replaces_same_speaker(self):
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "mappings.json"
            save_speaker_mapping(
                "Selone", "624901", "part01.bnk", "24006", path=output
            )
            save_speaker_mapping(
                "Selone", "624901", "part02.bnk", "24007", path=output
            )
            document = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            document["mappings"],
            [
                {
                    "display_name": "Selone",
                    "npc_id": "624901",
                    "bank": "part02.bnk",
                    "chapter": "24007",
                }
            ],
        )

    def test_dialog_selects_chapter_candidates_and_prefills_npc_id(self):
        dialogue_index = {
            "dialogue": [
                {
                    "chapter": "24006",
                    "sequence": 3,
                    "speaker_id": "310918",
                    "speaker_name": "Fatutu",
                    "text": "Selone! Take this.",
                }
            ]
        }
        bank_index = {
            "game_audio_directory": "/game",
            "banks": [
                {
                    "path": "selone.bnk",
                    "filename": "activityvoc_npc624901_2_4_part01.bnk",
                    "npc_ids": ["624901"],
                    "events": [{"media_ids": [42]}],
                }
            ],
        }
        dialog = Reverse1999AuditionDialog(dialogue_index, bank_index)
        dialog.search.setText("Selone")
        dialog.dialogue.selectRow(0)
        self.application.processEvents()

        self.assertEqual(dialog.banks.count(), 1)
        self.assertEqual(dialog.npc_id.text(), "624901")
        self.assertEqual(dialog.media.currentData(), 42)
        dialog.deleteLater()

    def test_dialog_records_reviewed_clip(self):
        dialogue_index = {
            "dialogue": [
                {
                    "chapter": "24006",
                    "sequence": 3,
                    "speaker_id": "521001",
                    "speaker_name": "Selone",
                    "text": "Here, I'll give you a hand!",
                }
            ]
        }
        bank_index = {
            "game_audio_directory": "/game",
            "banks": [
                {
                    "path": "selone.bnk",
                    "filename": "activityvoc_npc521001_2_4.bnk",
                    "npc_ids": ["521001"],
                    "events": [{"media_ids": [42]}],
                }
            ],
        }
        metrics = VoiceReferenceMetrics(
            path="/cache/42.wav",
            duration_seconds=5.0,
            peak_dbfs=-2.0,
            rms_dbfs=-18.0,
            silence_ratio=0.1,
            leading_silence_seconds=0.1,
            trailing_silence_seconds=0.1,
            clipping_ratio=0.0,
            quality_score=100,
            technical_flags=(),
        )
        recorded = []
        dialog = Reverse1999AuditionDialog(
            dialogue_index,
            bank_index,
            clip_preparer=lambda _bank, _media_id: Path("/cache/42.wav"),
            quality_analyzer=lambda _path: metrics,
            review_recorder=lambda reviewed, **metadata: recorded.append(
                (reviewed, metadata)
            )
            or Path("/reviews.json"),
        )
        dialog.dialogue.selectRow(0)
        self.application.processEvents()
        dialog.player = Mock()
        dialog.play_clip()
        dialog.music_or_sfx.setCurrentIndex(1)
        dialog.multiple_speakers.setCurrentIndex(1)

        dialog.save_clip_review()

        self.assertEqual(len(recorded), 1)
        self.assertTrue(recorded[0][0].approved)
        self.assertEqual(recorded[0][1]["media_id"], 42)
        self.assertIn("approved", dialog.status.text())
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
