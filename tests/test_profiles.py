import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from vntts.ocr import DialogRegion  # noqa: E402
from vntts.ocr_corrections import OCRCorrectionStore  # noqa: E402
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
                live_sequence_mode="audio-manual",
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
        self.assertEqual(applied.live_sequence_mode, "audio-manual")
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

    def test_profile_mutations_publish_memory_only_after_persistence(self):
        operations = (
            lambda store, profile: store.create("Other", AppSettings()),
            lambda store, profile: store.duplicate(profile.id, "Copy"),
            lambda store, profile: store.rename(profile.id, "Renamed"),
            lambda store, profile: store.remove(profile.id),
            lambda store, profile: store.update_from_settings(
                profile.id,
                AppSettings(game_window_title="Changed"),
            ),
            lambda store, profile: store.update_region(
                profile.id,
                DialogRegion(0.2, 0.2, 0.5, 0.5),
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation), TemporaryDirectory() as directory:
                store = GameProfileStore(Path(directory) / "profiles.json")
                profile = store.create("Game", AppSettings())
                before = list(store.profiles)
                with (
                    patch(
                        "vntts.profiles.write_versioned_json",
                        side_effect=OSError("disk full"),
                    ),
                    self.assertRaisesRegex(OSError, "disk full"),
                ):
                    operation(store, profile)

                self.assertEqual(store.profiles, before)

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
            self.assertIn("Activation applies", dialog.summary.text())
            self.assertIn("Capture:", dialog.summary.text())
            self.assertIn("Content:", dialog.summary.text())
            self.assertIn("Audio:", dialog.summary.text())
            self.assertIn("Voices:", dialog.summary.text())
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
            self.assertEqual(
                dialog.create_button.text(),
                "Save current setup as profile...",
            )
            self.assertFalse(dialog.duplicate_button.isEnabled())
            self.assertFalse(dialog.rename_button.isEnabled())
            self.assertFalse(dialog.remove_button.isEnabled())
            self.assertFalse(dialog.use_button.isEnabled())
            dialog.close()
            dialog.deleteLater()

    def test_remove_profile_escape_keeps_profile_and_corrections(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = GameProfileStore(root / "profiles.json")
            active = store.create("Active game", AppSettings())
            removable = store.create("Other game", AppSettings())
            correction_store = OCRCorrectionStore(
                root / "corrections.json",
                profile_entries={removable.id: {"Vertln": "Vertin"}},
            )
            dialog = GameProfilesDialog(
                AppSettings(active_profile_id=active.id),
                store,
                correction_store,
            )
            dialog.profiles.setCurrentIndex(dialog.profiles.findData(removable.id))
            prompt_evidence = {}

            def cancel_prompt():
                prompt = self.application.activeModalWidget()
                self.assertIsInstance(prompt, QMessageBox)
                prompt_evidence["text"] = prompt.text()
                prompt_evidence["details"] = prompt.informativeText()
                prompt_evidence["default"] = prompt.defaultButton().text()
                prompt_evidence["escape"] = prompt.escapeButton().text()
                QTest.keyClick(prompt, Qt.Key.Key_Escape)

            QTimer.singleShot(0, cancel_prompt)
            dialog.remove_profile()

            self.assertIn("Other game", prompt_evidence["text"])
            self.assertIn("1 profile-scoped OCR correction", prompt_evidence["details"])
            self.assertEqual(prompt_evidence["default"], "Cancel")
            self.assertEqual(prompt_evidence["escape"], "Cancel")
            self.assertIsNotNone(store.get(removable.id))
            self.assertEqual(
                correction_store.profile_entries[removable.id],
                {"Vertln": "Vertin"},
            )
            dialog.close()
            dialog.deleteLater()

    def test_remove_profile_requires_explicit_button_and_deletes_corrections(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = GameProfileStore(root / "profiles.json")
            active = store.create("Active game", AppSettings())
            removable = store.create("Other game", AppSettings())
            correction_store = OCRCorrectionStore(
                root / "corrections.json",
                profile_entries={
                    removable.id: {
                        "Vertln": "Vertin",
                        "mareus": "Ms. Marcus",
                    }
                },
            )
            dialog = GameProfilesDialog(
                AppSettings(active_profile_id=active.id),
                store,
                correction_store,
            )
            dialog.profiles.setCurrentIndex(dialog.profiles.findData(removable.id))
            prompt_evidence = {}

            def confirm_prompt():
                prompt = self.application.activeModalWidget()
                self.assertIsInstance(prompt, QMessageBox)
                prompt_evidence["details"] = prompt.informativeText()
                remove_button = next(
                    button
                    for button in prompt.buttons()
                    if button.text() == "Remove profile"
                )
                remove_button.click()

            QTimer.singleShot(0, confirm_prompt)
            dialog.remove_profile()

            self.assertIn(
                "2 profile-scoped OCR corrections", prompt_evidence["details"]
            )
            self.assertIsNone(store.get(removable.id))
            self.assertNotIn(removable.id, correction_store.profile_entries)
            dialog.close()
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
