import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.ocr import DialogRegion  # noqa: E402
from vntts.profiles import (  # noqa: E402
    GameProfile,
    GameProfileStore,
    profiles_schema_version,
)
from vntts.profiles_ui import GameProfilesDialog  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402


class GameProfileStoreTest(unittest.TestCase):
    def test_profile_round_trips_all_game_specific_settings(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "profiles.json"
            region = DialogRegion(0.1, 0.6, 0.8, 0.3)
            settings = AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                ocr_language="eng+jpn",
                voice_manifest="voices/reverse-1999.json",
                story_index="story/reverse-1999.jsonl",
                live_sequence_plan="story/live-sequence.json",
                live_sequence_mode="shadow",
                generated_audio_manifest="audio/generated.json",
                audio_source_policy="prefer-generated",
                voice_assignments={"Narrator": "preset:alba"},
                force_live_narrator=False,
            )
            store = GameProfileStore(path)

            profile = store.create("Reverse: 1999", settings, region=region)
            loaded = GameProfileStore.load(path)

        self.assertEqual(loaded.get(profile.id), profile)
        applied = profile.apply(AppSettings())
        self.assertEqual(applied.active_profile_id, profile.id)
        self.assertEqual(applied.game_window_title, "Reverse: 1999")
        self.assertEqual(applied.ocr_language, "eng+jpn")
        self.assertEqual(applied.voice_manifest, "voices/reverse-1999.json")
        self.assertEqual(applied.story_index, "story/reverse-1999.jsonl")
        self.assertEqual(applied.live_sequence_plan, "story/live-sequence.json")
        self.assertEqual(applied.live_sequence_mode, "shadow")
        self.assertEqual(applied.generated_audio_manifest, "audio/generated.json")
        self.assertEqual(applied.audio_source_policy, "prefer-generated")
        self.assertEqual(applied.voice_assignments, {"Narrator": "preset:alba"})
        self.assertFalse(applied.force_live_narrator)

    def test_profiles_can_be_duplicated_renamed_and_removed(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            original = store.create("Game", AppSettings())

            duplicate = store.duplicate(original.id, "Game copy")
            renamed = store.rename(duplicate.id, "Second game")
            removed = store.remove(original.id)

        self.assertEqual(renamed.name, "Second game")
        self.assertEqual(removed, original)
        self.assertEqual(store.profiles, [renamed])

    def test_profile_persists_and_preflights_game_pack_on_activation(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "profiles.json"
            store = GameProfileStore(path)
            profile = store.create(
                "Packaged game",
                AppSettings(game_pack="packs/game-pack.json"),
            )
            loaded = GameProfileStore.load(path).get(profile.id)
            resolved = AppSettings(game_pack="/resolved/game-pack.json")

            with patch(
                "vntts.game_pack.apply_game_pack",
                return_value=resolved,
            ) as preflight:
                applied = loaded.apply(AppSettings())

        self.assertEqual(loaded.game_pack, "packs/game-pack.json")
        self.assertEqual(applied, resolved)
        self.assertEqual(
            preflight.call_args.args[0].game_pack,
            "packs/game-pack.json",
        )

    def test_legacy_profile_without_audio_policy_migrates_to_live_tts(self):
        profile = GameProfile.from_mapping(
            {
                "id": "legacy",
                "name": "Legacy game",
                "capture_mode": "screen",
                "dialog_region": {
                    "left": 0.1,
                    "top": 0.6,
                    "width": 0.8,
                    "height": 0.3,
                },
            }
        )

        self.assertEqual(profile.audio_source_policy, "live-tts-only")

    def test_legacy_profile_preserves_narrator_force_live_routing(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 4,
                        "profiles": [
                            {
                                "id": "legacy",
                                "name": "Legacy game",
                                "capture_mode": "screen",
                                "dialog_region": {
                                    "left": 0.1,
                                    "top": 0.6,
                                    "width": 0.8,
                                    "height": 0.3,
                                },
                                "voice_assignments": {
                                    "Narrator": "reverse-1999-centurion-game-v1"
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            profile = GameProfileStore.load(path).get("legacy")

        self.assertTrue(profile.force_live_narrator)

    def test_duplicate_profile_names_are_rejected_case_insensitively(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            store.create("Game", AppSettings())

            with self.assertRaisesRegex(ValueError, "already exists"):
                store.create("game", AppSettings())

    def test_future_profile_schema_falls_back_to_empty_store(self):
        warnings = []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "profiles.json"
            path.write_text(
                json.dumps(
                    {"schema_version": profiles_schema_version + 1, "profiles": []}
                ),
                encoding="utf-8",
            )

            store = GameProfileStore.load(path, warn=warnings.append)

        self.assertEqual(store.profiles, [])
        self.assertIn("unsupported game profiles schema version", warnings[0])


class GameProfilesDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_using_profile_activates_region_and_settings(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            region = DialogRegion(0.05, 0.65, 0.9, 0.3)
            profile = store.create(
                "Reverse: 1999",
                AppSettings(
                    capture_mode="window",
                    game_window_title="Reverse: 1999",
                    ocr_language="eng",
                    voice_manifest="voices.json",
                ),
                region=region,
            )
            dialog = GameProfilesDialog(AppSettings(), store)
            dialog.refresh_profiles(profile.id)

            with patch("vntts.profiles_ui.save_dialog_region") as save_region:
                dialog.use_profile()

        save_region.assert_called_once()
        self.assertEqual(save_region.call_args.args[0], region)
        self.assertEqual(dialog.settings().active_profile_id, profile.id)
        self.assertEqual(dialog.settings().game_window_title, "Reverse: 1999")

    def test_active_and_selected_profiles_have_distinct_actions(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            first = store.create("Active game", AppSettings())
            second = store.create("Other game", AppSettings())
            dialog = GameProfilesDialog(
                AppSettings(active_profile_id=first.id),
                store,
            )

            self.assertEqual(dialog.active_status.text(), "Active game")
            self.assertIn("(active)", dialog.summary.text())
            self.assertFalse(dialog.use_button.isEnabled())
            self.assertEqual(dialog.use_button.text(), "Already active")
            self.assertFalse(dialog.remove_button.isEnabled())
            self.assertIn("Activate another", dialog.remove_button.toolTip())

            dialog.profiles.setCurrentIndex(dialog.profiles.findData(second.id))

            self.assertEqual(dialog.active_status.text(), "Active game")
            self.assertIn("Other game (not active)", dialog.summary.text())
            self.assertTrue(dialog.use_button.isEnabled())
            self.assertEqual(dialog.use_button.text(), "Use selected profile")
            self.assertTrue(dialog.remove_button.isEnabled())
            self.assertEqual(
                dialog.use_button.accessibleName(),
                "Activate selected game profile",
            )
            dialog.close()
            dialog.deleteLater()

    def test_empty_profile_manager_explains_its_only_available_action(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            dialog = GameProfilesDialog(AppSettings(), store)

            self.assertEqual(
                dialog.active_status.text(),
                "No stored profile is active",
            )
            self.assertIn("No profiles yet", dialog.summary.text())
            self.assertTrue(dialog.create_button.isEnabled())
            self.assertFalse(dialog.duplicate_button.isEnabled())
            self.assertFalse(dialog.rename_button.isEnabled())
            self.assertFalse(dialog.remove_button.isEnabled())
            self.assertFalse(dialog.use_button.isEnabled())
            dialog.close()
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
