import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.reverse1999_batch import (
    Reverse1999BatchError,
    discover_auto_mappings,
    load_state,
    map_speakers,
    merge_clip_reviews,
    new_state,
    preselect_auto_references,
    save_state,
    stage_counts,
    update_catalog_from_imports,
)
from vntts.reverse1999_catalog import Reverse1999NpcCatalog


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

    def test_auto_discovery_accepts_only_stable_unique_speaker_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bank_index = root / "banks.json"
            dialogue_index = root / "dialogue.json"
            catalog = root / "catalog.json"
            npc_banks = {
                npc_id: [f"story_npc{npc_id}.bnk"]
                for npc_id in ("100", "200", "300", "400", "500", "600")
            }
            bank_index.write_text(
                json.dumps(
                    {
                        "npc_banks": npc_banks,
                        "banks": [
                            {
                                "filename": banks[0],
                                "path": banks[0],
                                "npc_ids": [npc_id],
                                "events": [{"media_ids": [1, 2, 3, 4]}],
                            }
                            for npc_id, banks in npc_banks.items()
                        ],
                    }
                ),
                encoding="utf-8",
            )
            dialogue_index.write_text(
                json.dumps(
                    {
                        "dialogue": [
                            {
                                "speaker_id": "100",
                                "speaker_name": "Safe",
                                "chapter": "1",
                            },
                            {
                                "speaker_id": "100",
                                "speaker_name": "Safe",
                                "chapter": "1",
                            },
                            {
                                "speaker_id": "200",
                                "speaker_name": "Alpha",
                                "chapter": "2",
                            },
                            {
                                "speaker_id": "200",
                                "speaker_name": "Beta",
                                "chapter": "2",
                            },
                            {
                                "speaker_id": "300",
                                "speaker_name": "Shared",
                                "chapter": "3",
                            },
                            {
                                "speaker_id": "300",
                                "speaker_name": "Shared",
                                "chapter": "3",
                            },
                            {
                                "speaker_id": "400",
                                "speaker_name": "Shared",
                                "chapter": "4",
                            },
                            {
                                "speaker_id": "400",
                                "speaker_name": "Shared",
                                "chapter": "4",
                            },
                            {
                                "speaker_id": "500",
                                "speaker_name": "Brief",
                                "chapter": "5",
                            },
                            {
                                "speaker_id": "600",
                                "speaker_name": "Slouch Hat",
                                "chapter": "6",
                            },
                            {
                                "speaker_id": "600",
                                "speaker_name": "Slouch Hat",
                                "chapter": "6",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog.write_text(
                json.dumps({"version": 1, "game": "Reverse: 1999", "npcs": []}),
                encoding="utf-8",
            )
            state = new_state()
            state["bank_index"] = str(bank_index)
            state["dialogue_index"] = str(dialogue_index)

            discover_auto_mappings(state, catalog_path=catalog)

        self.assertEqual(
            [(item["npc_id"], item["speaker_name"]) for item in state["mappings"]],
            [("100", "Safe")],
        )
        reasons = {
            item["npc_id"]: item["reasons"] for item in state["mapping_review_queue"]
        }
        self.assertIn("conflicting-speaker-names", reasons["200"])
        self.assertIn("speaker-name-shared-by-multiple-ids", reasons["300"])
        self.assertIn("speaker-name-shared-by-multiple-ids", reasons["400"])
        self.assertIn("insufficient-dialogue-evidence", reasons["500"])
        self.assertEqual(
            state["alias_resolutions"],
            [
                {
                    "npc_id": "600",
                    "observed_name": "Slouch Hat",
                    "canonical_name": "Brimley",
                    "dialogue_lines": 2,
                }
            ],
        )

    def test_auto_preselection_writes_pending_review_queue(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue = root / "queue.json"
            state = new_state()
            dialogue_index = root / "dialogue.json"
            dialogue_index.write_text(
                json.dumps(
                    {
                        "dialogue": [
                            {
                                "speaker_id": "100",
                                "speaker_name": "Safe",
                                "text": "One clean sentence.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state["dialogue_index"] = str(dialogue_index)
            state["mappings"] = [
                {
                    "speaker_name": "Safe",
                    "npc_id": "100",
                    "chapter": "1",
                    "banks": ["safe.bnk"],
                }
            ]
            durations = (5.0, 5.5, 6.0, 4.0, 5.0)
            state["clips"] = [
                {
                    "speaker_name": "Safe",
                    "npc_id": "100",
                    "bank": "safe.bnk",
                    "media_id": index,
                    "wav": str(root / f"{index}.wav"),
                    "status": "scored",
                    "metrics": {
                        "duration_seconds": seconds,
                        "quality_score": 100,
                        "technical_flags": [],
                    },
                }
                for index, seconds in enumerate(durations, start=1)
            ]

            preselect_auto_references(
                state,
                review_queue_path=queue,
                transcriber=lambda path: (
                    "[music]" if Path(path).stem == "3" else "One clean sentence."
                ),
            )
            document = json.loads(queue.read_text(encoding="utf-8"))
            document["clips"][0]["music_or_sfx"] = False
            document["clips"][0]["multiple_speakers"] = False
            document["clips"][0]["matches_expected_speaker"] = True
            queue.write_text(json.dumps(document), encoding="utf-8")
            preselect_auto_references(state, review_queue_path=queue)
            resumed = json.loads(queue.read_text(encoding="utf-8"))

        self.assertEqual(len(state["auto_selections"]), 1)
        self.assertEqual(len(state["auto_selections"][0]["clips"]), 3)
        self.assertNotIn(
            3,
            [item["media_id"] for item in state["auto_selections"][0]["clips"]],
        )
        self.assertEqual(len(document["clips"]), 3)
        self.assertTrue(all(item["approved"] is None for item in document["clips"]))
        self.assertIn("never imported", document["review_note"])
        self.assertFalse(resumed["clips"][0]["music_or_sfx"])
        self.assertFalse(resumed["clips"][0]["multiple_speakers"])
        self.assertTrue(resumed["clips"][0]["matches_expected_speaker"])

    def test_scene_audio_bank_is_quarantined_even_with_clean_clips(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue = root / "queue.json"
            dialogue_index = root / "dialogue.json"
            dialogue_index.write_text(
                json.dumps(
                    {
                        "dialogue": [
                            {
                                "speaker_id": "505701",
                                "speaker_name": "Professor",
                                "text": "Knowledge has no divisions.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state = new_state()
            state["dialogue_index"] = str(dialogue_index)
            state["mappings"] = [
                {
                    "speaker_name": "Professor",
                    "npc_id": "505701",
                    "chapter": "20",
                    "banks": ["activityvoc_story_npc505701_feichi.bnk"],
                }
            ]
            state["clips"] = [
                {
                    "speaker_name": "Professor",
                    "npc_id": "505701",
                    "bank": "activityvoc_story_npc505701_feichi.bnk",
                    "media_id": media_id,
                    "wav": str(root / f"{media_id}.wav"),
                    "status": "scored",
                    "metrics": {
                        "duration_seconds": 5.0,
                        "quality_score": 100,
                        "technical_flags": [],
                    },
                }
                for media_id in (176156650, 251690830, 392147139, 682090872)
            ]

            preselect_auto_references(state, review_queue_path=queue)

        self.assertEqual(state["auto_selections"], [])
        self.assertEqual(
            {tuple(clip["identity_flags"]) for clip in state["clips"]},
            {("scene-audio-bank",)},
        )

    def test_transcript_must_match_expected_character_dialogue(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue = root / "queue.json"
            dialogue_index = root / "dialogue.json"
            dialogue_index.write_text(
                json.dumps(
                    {
                        "dialogue": [
                            {
                                "speaker_id": "505701",
                                "speaker_name": "Professor",
                                "text": "Knowledge has no divisions.",
                            },
                            {
                                "speaker_id": "999",
                                "speaker_name": "Television",
                                "text": "Good for you, Bianca!",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            state = new_state()
            state["dialogue_index"] = str(dialogue_index)
            state["mappings"] = [
                {
                    "speaker_name": "Professor",
                    "npc_id": "505701",
                    "chapter": "20",
                    "banks": ["activityvoc_npc505701_2_4.bnk"],
                }
            ]
            state["clips"] = [
                {
                    "speaker_name": "Professor",
                    "npc_id": "505701",
                    "bank": "activityvoc_npc505701_2_4.bnk",
                    "media_id": media_id,
                    "wav": str(root / f"{media_id}.wav"),
                    "status": "scored",
                    "metrics": {
                        "duration_seconds": 5.0,
                        "quality_score": 100,
                        "technical_flags": [],
                    },
                }
                for media_id in (1, 2, 3)
            ]

            preselect_auto_references(
                state,
                review_queue_path=queue,
                transcriber=lambda _path: "Good for you, Bianca!",
            )

        self.assertEqual(state["auto_selections"], [])
        self.assertTrue(
            all(
                clip["identity_flags"] == ["dialogue-speaker-mismatch"]
                for clip in state["clips"]
            )
        )

    def test_speaker_embeddings_keep_only_one_consistent_cluster(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue = root / "queue.json"
            state = new_state()
            state["mappings"] = [
                {
                    "speaker_name": "Safe",
                    "npc_id": "100",
                    "chapter": "1",
                    "banks": ["safe.bnk"],
                }
            ]
            state["clips"] = [
                {
                    "speaker_name": "Safe",
                    "npc_id": "100",
                    "bank": "safe.bnk",
                    "media_id": media_id,
                    "wav": str(root / f"{media_id}.wav"),
                    "status": "scored",
                    "metrics": {
                        "duration_seconds": 5.0,
                        "quality_score": 100,
                        "technical_flags": [],
                    },
                }
                for media_id in (1, 2, 3, 4)
            ]
            embeddings = {
                "1": [1.0, 0.0],
                "2": [0.99, 0.01],
                "3": [0.98, 0.02],
                "4": [0.0, 1.0],
            }

            preselect_auto_references(
                state,
                review_queue_path=queue,
                speaker_embedder=lambda path: embeddings[Path(path).stem],
            )

        selected = state["auto_selections"][0]["clips"]
        self.assertEqual([clip["media_id"] for clip in selected], [1, 2, 3])
        self.assertEqual(
            state["clips"][3]["identity_flags"],
            ["inconsistent-speaker-embedding"],
        )

    def test_known_speaker_anchor_rejects_a_consistent_wrong_voice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue = root / "queue.json"
            state = new_state()
            state["mappings"] = [
                {
                    "speaker_name": "Professor",
                    "npc_id": "505701",
                    "chapter": "20",
                    "banks": ["activityvoc_npc505701_2_4.bnk"],
                }
            ]
            state["clips"] = [
                {
                    "speaker_name": "Professor",
                    "npc_id": "505701",
                    "bank": "activityvoc_npc505701_2_4.bnk",
                    "media_id": media_id,
                    "wav": str(root / f"{media_id}.wav"),
                    "status": "scored",
                    "metrics": {
                        "duration_seconds": 5.0,
                        "quality_score": 100,
                        "technical_flags": [],
                    },
                }
                for media_id in (1, 2, 3)
            ]

            preselect_auto_references(
                state,
                review_queue_path=queue,
                speaker_embedder=lambda _path: [0.0, 1.0],
                speaker_anchors={"505701": [1.0, 0.0]},
            )

        self.assertEqual(state["auto_selections"], [])
        self.assertTrue(
            all(
                clip["identity_flags"] == ["known-speaker-mismatch"]
                for clip in state["clips"]
            )
        )

    def test_pending_auto_review_is_ignored_until_content_flags_are_set(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "reviews.json"
            review = {
                "speaker_name": "Safe",
                "npc_id": "100",
                "bank": "safe.bnk",
                "media_id": 1,
                "approved": None,
                "music_or_sfx": None,
                "multiple_speakers": None,
                "matches_expected_speaker": None,
                "metrics": {"technical_flags": []},
            }
            path.write_text(
                json.dumps({"version": 1, "clips": [review]}), encoding="utf-8"
            )
            state = new_state()
            state["clips"] = [
                {
                    "bank": "safe.bnk",
                    "media_id": 1,
                    "status": "scored",
                }
            ]

            merge_clip_reviews(state, review_path=path)
            self.assertEqual(state["clips"][0]["status"], "scored")

            review["music_or_sfx"] = False
            review["multiple_speakers"] = False
            review["matches_expected_speaker"] = True
            path.write_text(
                json.dumps({"version": 1, "clips": [review]}), encoding="utf-8"
            )
            merge_clip_reviews(state, review_path=path)

        self.assertEqual(state["clips"][0]["status"], "approved")

    def test_approved_shortcut_cannot_bypass_identity_review(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "reviews.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "clips": [
                            {
                                "bank": "scene.bnk",
                                "media_id": 1,
                                "approved": True,
                                "music_or_sfx": False,
                                "multiple_speakers": False,
                                "matches_expected_speaker": None,
                                "metrics": {"technical_flags": []},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = new_state()
            state["clips"] = [
                {"bank": "scene.bnk", "media_id": 1, "status": "scored"}
            ]

            merge_clip_reviews(state, review_path=path)

        self.assertEqual(state["clips"][0]["status"], "scored")

    def test_catalog_update_is_validated_atomic_and_idempotent(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            reference_root = root / "data"
            reference = reference_root / "voices" / "safe.wav"
            reference.parent.mkdir(parents=True)
            reference.write_bytes(b"voice")
            checksum = hashlib.sha256(b"voice").hexdigest()
            catalog_path = root / "catalog.json"
            catalog_path.write_text(
                json.dumps({"version": 1, "game": "Reverse: 1999", "npcs": []}),
                encoding="utf-8",
            )
            state = new_state()
            state["imports"] = [
                {
                    "speaker_name": "Safe",
                    "npc_id": "100",
                    "banks": ["safe.bnk"],
                    "references": [
                        {
                            "path": str(reference),
                            "bank": "safe.bnk",
                            "media_id": 42,
                            "source_sha256": "a" * 64,
                            "reference_sha256": checksum,
                        }
                    ],
                }
            ]

            update_catalog_from_imports(
                state,
                catalog_path=catalog_path,
                reference_root=reference_root,
                game_version="3.7.0",
            )
            catalog = Reverse1999NpcCatalog.load(catalog_path)
            update_catalog_from_imports(
                state,
                catalog_path=catalog_path,
                reference_root=reference_root,
            )

        self.assertEqual(catalog.get("100").display_name, "Safe")
        self.assertEqual(catalog.get("100").game_versions, ("3.7.0",))
        self.assertEqual(catalog.get("100").approved_references[0].media_id, 42)
        self.assertEqual(state["catalog_updates"], [])

    def test_invalid_catalog_update_does_not_replace_existing_catalog(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            reference_root = root / "data"
            reference = reference_root / "voices" / "safe.wav"
            reference.parent.mkdir(parents=True)
            reference.write_bytes(b"voice")
            catalog_path = root / "catalog.json"
            original = '{"version": 1, "game": "Reverse: 1999", "npcs": []}\n'
            catalog_path.write_text(original, encoding="utf-8")
            state = new_state()
            state["imports"] = [
                {
                    "speaker_name": "Safe",
                    "npc_id": "100",
                    "banks": ["safe.bnk"],
                    "references": [
                        {
                            "path": str(reference),
                            "bank": "safe.bnk",
                            "media_id": 42,
                            "source_sha256": "a" * 64,
                            "reference_sha256": "b" * 64,
                        }
                    ],
                }
            ]

            with self.assertRaisesRegex(
                Reverse1999BatchError, "checksum does not match"
            ):
                update_catalog_from_imports(
                    state,
                    catalog_path=catalog_path,
                    reference_root=reference_root,
                )

            self.assertEqual(catalog_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
