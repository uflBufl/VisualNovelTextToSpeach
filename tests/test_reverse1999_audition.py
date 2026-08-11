import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.reverse1999_audition import (  # noqa: E402
    candidate_banks,
    chapter_tokens,
    filter_dialogue,
    save_speaker_mapping,
)
from vntts.reverse1999_audition_ui import Reverse1999AuditionDialog  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
