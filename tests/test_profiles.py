import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

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
        self.assertFalse(applied.force_live_narrator)
        updated = profile.updated_from_settings(settings, region=region)
        self.assertEqual(
            updated.apply(AppSettings()).voice_manifest, settings.voice_manifest
        )

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
                        "vntts.versioned_json.write_versioned_json",
                        side_effect=OSError("disk full"),
                    ),
                    self.assertRaisesRegex(OSError, "disk full"),
                ):
                    operation(store, profile)

                self.assertEqual(store.profiles, before)

    def test_stale_store_cannot_overwrite_another_store(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            original = GameProfileStore(path)
            profile = original.create("Game", AppSettings())
            first = GameProfileStore.load(path)
            stale = GameProfileStore.load(path)
            first.rename(profile.id, "Renamed")

            with self.assertRaisesRegex(OSError, "changed on disk"):
                stale.remove(profile.id)
            with self.assertRaisesRegex(OSError, "changed on disk"):
                stale.save()

            self.assertEqual(stale.get(profile.id).name, "Game")
            self.assertEqual(
                GameProfileStore.load(path).get(profile.id).name, "Renamed"
            )

    def test_loaded_profiles_use_the_revision_of_the_decoded_snapshot(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            initial = GameProfileStore(path)
            initial.create("A", AppSettings())
            snapshot_a = path.read_bytes()
            external = GameProfileStore.load(path)
            external.create("B", AppSettings())
            snapshot_b = path.read_bytes()
            path.write_bytes(snapshot_a)

            from vntts.profiles import load_versioned_json as original_loader

            def load_b_then_restore_a(*args, **kwargs):
                path.write_bytes(snapshot_b)
                document = original_loader(*args, **kwargs)
                path.write_bytes(snapshot_a)
                return document

            with patch(
                "vntts.profiles.load_versioned_json",
                side_effect=load_b_then_restore_a,
            ):
                loaded = GameProfileStore.load(path)

            self.assertEqual([profile.name for profile in loaded.profiles], ["A", "B"])
            with self.assertRaisesRegex(OSError, "changed on disk"):
                loaded.create("C", AppSettings())

            self.assertEqual(
                [profile.name for profile in GameProfileStore.load(path).profiles],
                ["A"],
            )

    def test_restore_publishes_only_after_save_and_preserves_profile(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            store = GameProfileStore(path)
            remaining = store.create("Remaining", AppSettings())
            removed = store.create(
                "Removed",
                AppSettings(game_window_title="Game"),
                region=DialogRegion(0.2, 0.4, 0.7, 0.4),
            )
            store.remove(removed.id)
            before = path.read_bytes()
            with (
                patch(
                    "vntts.versioned_json.write_versioned_json",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                store.restore(removed)
            self.assertEqual(store.profiles, [remaining])
            self.assertEqual(path.read_bytes(), before)

            store.restore(removed)
            self.assertEqual(store.profiles, [remaining, removed])
            self.assertEqual(GameProfileStore.load(path).profiles, store.profiles)
            with self.assertRaisesRegex(ValueError, "profile IDs must be unique"):
                store.restore(removed)

    def test_malformed_profile_ids_are_rejected_without_rewriting_the_file(self):
        profile = GameProfile.from_settings("Game", AppSettings())
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            for invalid in (None, 0, False, [], {}, "", "   "):
                with self.subTest(profile_id=invalid):
                    values = {**profile.to_mapping(), "id": invalid}
                    with self.assertRaisesRegex(ValueError, "nonempty strings"):
                        GameProfile.from_mapping(values)
                    path.write_text(
                        json.dumps(
                            {
                                "schema_version": profiles_schema_version,
                                "profiles": [values],
                            }
                        ),
                        encoding="utf-8",
                    )
                    before = path.read_bytes()
                    warnings = []
                    loaded = GameProfileStore.load(path, warn=warnings.append)
                    self.assertEqual(loaded.profiles, [])
                    self.assertIn("nonempty strings", warnings[0])
                    self.assertEqual(path.read_bytes(), before)

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

    def test_invalid_optional_profile_fields_keep_saved_profile_usable(self):
        base = {
            "id": "saved",
            "name": "Saved game",
            "capture_mode": "screen",
            "dialog_region": {"left": 0.1, "top": 0.6, "width": 0.8, "height": 0.3},
        }
        cases = (
            ("capture_mode", [], "capture_mode", "screen"),
            ("live_sequence_mode", [], "live_sequence_mode", "off"),
            ("ocr_language", "  ", "ocr_language", "eng"),
            ("ocr_language", 42, "ocr_language", "eng"),
        )
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "profiles.json"
            for field, value, attribute, expected in cases:
                with self.subTest(field=field, value=value):
                    path.write_text(
                        json.dumps(
                            {
                                "schema_version": profiles_schema_version,
                                "profiles": [{**base, field: value}],
                            }
                        ),
                        encoding="utf-8",
                    )
                    warnings = []
                    loaded = GameProfileStore.load(path, warn=warnings.append)
                    profile = loaded.get("saved")

                    self.assertIsNotNone(profile)
                    self.assertEqual(
                        getattr(profile.apply(AppSettings()), attribute), expected
                    )
                    self.assertEqual(warnings, [])

    def test_legacy_profile_narrator_assignment_migrates_to_force_live_routing(self):
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
                                "voice_assignments": {"Narrator": "preset:alba"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            store = GameProfileStore.load(path)
            profile = store.get("legacy")
            store.save()
            saved_schema = json.loads(path.read_text(encoding="utf-8"))[
                "schema_version"
            ]

        self.assertTrue(profile.force_live_narrator)
        self.assertEqual(saved_schema, profiles_schema_version)

    def test_current_profile_ignores_obsolete_narrator_assignment(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": profiles_schema_version,
                        "profiles": [
                            {
                                "id": "current",
                                "name": "Current game",
                                "capture_mode": "screen",
                                "dialog_region": {
                                    "left": 0.1,
                                    "top": 0.6,
                                    "width": 0.8,
                                    "height": 0.3,
                                },
                                "voice_assignments": {"Narrator": "preset:alba"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            profile = GameProfileStore.load(path).get("current")

        self.assertFalse(profile.force_live_narrator)

    def test_duplicate_profile_names_are_rejected_case_insensitively(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            store.create("Game", AppSettings())

            with self.assertRaisesRegex(ValueError, "already exists"):
                store.create("game", AppSettings())

    def test_save_rejects_duplicate_profiles_before_publication(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "profiles.json"
            profiles = (
                GameProfile.from_settings("First", AppSettings(), profile_id="same"),
                GameProfile.from_settings("Second", AppSettings(), profile_id="same"),
            )

            with self.assertRaisesRegex(ValueError, "profile IDs must be unique"):
                GameProfileStore(path, profiles).save()

            self.assertFalse(path.exists())

    def test_duplicate_profile_ids_fall_back_to_empty_store(self):
        warnings = []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "profiles.json"
            profiles = (
                GameProfile.from_settings("First", AppSettings(), profile_id="same"),
                GameProfile.from_settings("Second", AppSettings(), profile_id="same"),
            )
            path.write_text(
                json.dumps(
                    {
                        "schema_version": profiles_schema_version,
                        "profiles": [profile.to_mapping() for profile in profiles],
                    }
                ),
                encoding="utf-8",
            )
            original = path.read_bytes()
            store = GameProfileStore.load(path, warn=warnings.append)
            with self.assertRaisesRegex(OSError, "changed on disk"):
                store.save()
            self.assertEqual(path.read_bytes(), original)

        self.assertEqual(store.profiles, [])
        self.assertIn("profile IDs must be unique", warnings[0])

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
            original = path.read_bytes()
            store = GameProfileStore.load(path, warn=warnings.append)
            with self.assertRaisesRegex(OSError, "changed on disk"):
                store.save()
            self.assertEqual(path.read_bytes(), original)

        self.assertEqual(store.profiles, [])
        self.assertIn("unsupported game profiles schema version", warnings[0])

    def test_unhashable_sequence_mode_preserves_saved_profile(self):
        warnings = []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": profiles_schema_version,
                        "profiles": [
                            {
                                "id": "damaged",
                                "name": "Damaged game",
                                "capture_mode": "screen",
                                "dialog_region": {
                                    "left": 0.1,
                                    "top": 0.6,
                                    "width": 0.8,
                                    "height": 0.3,
                                },
                                "live_sequence_mode": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            store = GameProfileStore.load(path, warn=warnings.append)

        self.assertEqual(len(store.profiles), 1)
        self.assertEqual(store.profiles[0].live_sequence_mode, "off")
        self.assertEqual(warnings, [])

    def test_incomplete_profile_region_is_reported_as_invalid(self):
        values = {
            "id": "damaged",
            "name": "Damaged game",
            "capture_mode": "screen",
            "dialog_region": {"left": 0.1},
        }
        with self.assertRaisesRegex(ValueError, "dialog_region top must be a number"):
            GameProfile.from_mapping(values)

        warnings = []
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "profiles.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": profiles_schema_version,
                        "profiles": [values],
                    }
                ),
                encoding="utf-8",
            )
            store = GameProfileStore.load(path, warn=warnings.append)

        self.assertEqual(store.profiles, [])
        self.assertIn("dialog_region top must be a number", warnings[0])


class GameProfilesDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_using_profile_selects_settings(self):
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

            dialog.use_profile()

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
            self.assertIn("Already active", dialog.summary.text())
            self.assertFalse(dialog.use_button.isEnabled())
            self.assertEqual(dialog.use_button.text(), "Already active")
            self.assertFalse(dialog.remove_button.isEnabled())
            self.assertIn("Activate another", dialog.remove_button.toolTip())
            with patch.object(QMessageBox, "exec") as prompt:
                dialog.remove_profile()
            prompt.assert_not_called()

            dialog.profiles.setCurrentIndex(dialog.profiles.findData(second.id))

            self.assertEqual(dialog.active_status.text(), "Active game")
            self.assertIn("Use selected profile to apply", dialog.summary.text())
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
                "Save current setup...",
            )
            self.assertTrue(
                any(
                    button.text() == "Close"
                    for button in dialog.findChildren(QPushButton)
                )
            )
            self.assertFalse(dialog.duplicate_button.isEnabled())
            self.assertFalse(dialog.rename_button.isEnabled())
            self.assertFalse(dialog.remove_button.isEnabled())
            self.assertFalse(dialog.use_button.isEnabled())
            dialog.close()
            dialog.deleteLater()

    def test_failed_correction_copy_removes_incomplete_duplicate(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            original = store.create("Original", AppSettings())
            corrections = Mock(profile_entries={original.id: {"name": "Name"}})
            corrections.copy_profile.side_effect = OSError("disk full")
            dialog = GameProfilesDialog(AppSettings(), store, corrections)
            with (
                patch.object(dialog, "_ask_name", return_value="Copy"),
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog.duplicate_profile()

            self.assertEqual([profile.id for profile in store.profiles], [original.id])
            self.assertIn("disk full", warning.call_args.args[-1])

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

    def test_failed_correction_cleanup_restores_profile_for_retry(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = GameProfileStore(root / "profiles.json")
            active = store.create("Active", AppSettings())
            removable = store.create("Other", AppSettings())
            corrections = OCRCorrectionStore(
                root / "corrections.json",
                profile_entries={removable.id: {"Vertln": "Vertin"}},
            )
            dialog = GameProfilesDialog(
                AppSettings(active_profile_id=active.id), store, corrections
            )
            dialog.profiles.setCurrentIndex(dialog.profiles.findData(removable.id))

            def confirm_prompt():
                prompt = self.application.activeModalWidget()
                next(
                    button
                    for button in prompt.buttons()
                    if button.text() == "Remove profile"
                ).click()

            QTimer.singleShot(0, confirm_prompt)
            with (
                patch.object(
                    corrections, "remove_profile", side_effect=OSError("disk full")
                ),
                patch.object(QMessageBox, "warning") as warning,
            ):
                dialog.remove_profile()

            self.assertIsNotNone(store.get(removable.id))
            self.assertEqual(
                corrections.profile_entries[removable.id], {"Vertln": "Vertin"}
            )
            self.assertIn("retry removal", warning.call_args.args[-1])


if __name__ == "__main__":
    unittest.main()
