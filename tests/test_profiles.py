import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.ocr import DialogRegion  # noqa: E402
from vntts.profiles import GameProfileStore  # noqa: E402
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
                generated_audio_manifest="audio/generated.json",
                voice_assignments={"Narrator": "preset:alba"},
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
        self.assertEqual(applied.generated_audio_manifest, "audio/generated.json")
        self.assertEqual(applied.voice_assignments, {"Narrator": "preset:alba"})

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
                json.dumps({"schema_version": 3, "profiles": []}),
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


if __name__ == "__main__":
    unittest.main()
