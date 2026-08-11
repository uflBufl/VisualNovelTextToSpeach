import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.reverse1999_batch import (
    load_state,
    map_speakers,
    new_state,
    save_state,
    stage_counts,
)


class Reverse1999BatchTest(unittest.TestCase):
    def test_state_checkpoint_is_atomic_and_resumable(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "state.json"
            state = new_state()
            state["unresolved_npc_ids"] = ["123"]

            save_state(state, path)
            loaded = load_state(path)

        self.assertEqual(loaded, state)

    def test_maps_catalog_and_assisted_speakers_and_reports_unresolved(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bank_index = root / "banks.json"
            catalog = root / "catalog.json"
            mappings = root / "mappings.json"
            bank_index.write_text(
                json.dumps(
                    {
                        "npc_banks": {
                            "520301": ["kamuta.bnk"],
                            "521001": ["selone.bnk"],
                            "999999": ["unknown.bnk"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            catalog.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "game": "Reverse: 1999",
                        "npcs": [
                            {
                                "id": "520301",
                                "display_name": "Kamuta",
                                "aliases": [],
                                "language": "en",
                                "game_versions": ["3.6.5"],
                                "banks": ["kamuta.bnk"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            mappings.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mappings": [
                            {
                                "display_name": "Selone",
                                "npc_id": "521001",
                                "bank": "selone.bnk",
                                "chapter": "24006",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = new_state()
            state["bank_index"] = str(bank_index)

            map_speakers(state, catalog_path=catalog, mapping_path=mappings)
            counts = stage_counts(state)

        self.assertEqual(
            [(item["speaker_name"], item["source"]) for item in state["mappings"]],
            [("Kamuta", "catalog"), ("Selone", "assisted")],
        )
        self.assertEqual(state["unresolved_npc_ids"], ["999999"])
        self.assertEqual(counts["mapped"], 2)
        self.assertEqual(counts["unresolved"], 1)

    def test_counts_resumable_clip_states(self):
        state = new_state()
        state["mappings"] = [{"speaker_name": "Selone"}]
        state["clips"] = [
            {"status": "scored"},
            {"status": "approved"},
            {"status": "rejected"},
            {"status": "imported"},
            {"status": "score-error"},
        ]

        counts = stage_counts(state)

        self.assertEqual(counts["scored"], 4)
        self.assertEqual(counts["pending_review"], 1)
        self.assertEqual(counts["approved"], 1)
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(counts["imported"], 1)
        self.assertEqual(counts["errors"], 1)


if __name__ == "__main__":
    unittest.main()
