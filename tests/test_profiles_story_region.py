import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from unittest.mock import patch

from vntts.durable_settings import DurableSettingsMixin
from vntts.ocr import (
    CAPTURE_COORDINATE_SPACE,
    CaptureRegionNeedsCalibration,
    DialogRegion,
    OCRRegions,
    get_story_region,
    load_dialog_region,
    load_ocr_regions,
    save_dialog_region,
    save_story_region,
)
from vntts.profiles import GameProfile, GameProfileStore, resolve_story_region
from vntts.settings import AppSettings
from vntts.versioned_json import file_revision


class StoryRegionPersistenceTest(unittest.TestCase):
    dialogue = DialogRegion(0.1, 0.7, 0.8, 0.2)
    story = DialogRegion(0.05, 0.2, 0.9, 0.75)
    settings = AppSettings(capture_mode="window", game_window_title="Game")

    def test_containment_requires_the_whole_region(self):
        self.assertTrue(self.story.contains(self.dialogue))
        self.assertTrue(self.story.contains(self.story))
        self.assertFalse(self.dialogue.contains(self.story))
        self.assertFalse(self.story.contains(DialogRegion(0, 0.7, 0.8, 0.2)))

    def test_exact_edge_permission_survives_profile_and_global_reload(self):
        story = DialogRegion(80 / 1051, 220 / 816, 870 / 1051, 500 / 816)
        dialogue = DialogRegion(200 / 1051, 555 / 816, 750 / 1051, 160 / 816)
        for use_profile in (False, True):
            with self.subTest(profile=use_profile), TemporaryDirectory() as directory:
                root = Path(directory)
                capture = root / "capture.json"
                store = GameProfileStore(root / "profiles.json")
                if use_profile:
                    profile = store.create("Game", self.settings, region=dialogue)
                    store.update_story_region(profile.id, story)
                    settings = profile.apply(self.settings)
                    store = GameProfileStore.load(store.path)
                else:
                    settings = self.settings
                    save_dialog_region(dialogue, capture)
                    save_story_region(
                        story, capture, capture_mode="window", game_window_title="Game"
                    )
                with patch.dict(os.environ, {"VNTTS_DIALOG_REGION_FILE": str(capture)}):
                    self.assertEqual(resolve_story_region(settings, store), story)

    def test_profile_permission_round_trips_and_does_not_leak_to_new_profiles(self):
        with TemporaryDirectory() as directory:
            store = GameProfileStore(Path(directory) / "profiles.json")
            original = store.create("Original", self.settings, region=self.dialogue)
            approved = store.update_story_region(original.id, self.story)
            duplicate = store.duplicate(original.id, "Duplicate")
            created = store.create("New", approved.apply(self.settings))
            loaded = GameProfileStore.load(store.path)

            self.assertEqual(loaded.get(original.id).story_region, self.story)
            self.assertEqual(duplicate.story_region, self.story)
            self.assertIsNone(created.story_region)
            self.assertEqual(
                resolve_story_region(approved.apply(self.settings), loaded), self.story
            )

    def test_profile_unrelated_updates_and_dialogue_calibration_preserve_permission(
        self,
    ):
        with TemporaryDirectory() as directory:
            store = GameProfileStore(Path(directory) / "profiles.json")
            profile = store.create("Game", self.settings, region=self.dialogue)
            store.update_story_region(profile.id, self.story)
            updated = store.update_from_settings(
                profile.id, self.settings.updated(ocr_language="jpn")
            )
            self.assertEqual(updated.story_region, self.story)
            updated = store.update_region(profile.id, self.dialogue)
            self.assertEqual(updated.story_region, self.story)
            layout = OCRRegions(
                DialogRegion(0, 0, 0.4, 0.2), DialogRegion(0, 0.3, 1, 0.7)
            )
            updated = store.update_capture_regions(profile.id, self.dialogue, layout)
            self.assertEqual(updated.story_region, self.story)
            self.assertEqual(
                updated.updated_from_settings(
                    self.settings, region=self.dialogue
                ).story_region,
                self.story,
            )

    def test_profile_permission_is_cleared_when_either_binding_component_changes(self):
        original = replace(
            GameProfile.from_settings("Game", self.settings), story_region=self.story
        )
        for settings in (
            self.settings.updated(capture_mode="screen"),
            self.settings.updated(game_window_title="Another game"),
        ):
            with self.subTest(settings=settings):
                updated = original.updated_from_settings(settings)
                self.assertIsNone(updated.story_region)
                self.assertIsNone(
                    updated.updated_from_settings(self.settings).story_region
                )

    def test_profile_permission_never_uses_global_fallback_or_mismatched_binding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "capture.json"
            save_story_region(
                self.story, path, capture_mode="window", game_window_title="Game"
            )
            store = GameProfileStore(root / "profiles.json")
            profile = store.create("Game", self.settings, region=self.dialogue)
            with patch.dict(os.environ, {"VNTTS_DIALOG_REGION_FILE": str(path)}):
                active = profile.apply(self.settings)
                self.assertIsNone(resolve_story_region(active, store))
                self.assertIsNone(
                    resolve_story_region(
                        active.updated(active_profile_id="missing"), store
                    )
                )
                store.update_story_region(profile.id, self.story)
                self.assertIsNone(
                    resolve_story_region(
                        active.updated(game_window_title="Another game"), store
                    )
                )

    def test_effective_dialogue_must_remain_inside_saved_permission(self):
        with TemporaryDirectory() as directory:
            store = GameProfileStore(Path(directory) / "profiles.json")
            profile = store.create("Game", self.settings, region=self.dialogue)
            store.update_story_region(profile.id, self.story)
            active = profile.apply(self.settings)
            with patch.dict(os.environ, {"VNTTS_DIALOG_REGION": "0,0,1,1"}):
                self.assertIsNone(resolve_story_region(active, store))
            store.update_region(profile.id, DialogRegion(0, 0, 1, 1))
            self.assertEqual(store.get(profile.id).story_region, self.story)
            self.assertIsNone(resolve_story_region(active, store))

    def test_missing_legacy_profile_permission_is_absent_and_invalid_values_fail(self):
        values = GameProfile.from_settings("Game", self.settings).to_mapping()
        values.pop("story_region")
        self.assertIsNone(
            GameProfile.from_mapping(values, source_schema=11).story_region
        )
        for invalid in (
            {"left": 0, "top": 0, "width": 2, "height": 1},
            {"left": False, "top": 0, "width": 1, "height": 1},
            "full screen",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                GameProfile.from_mapping({**values, "story_region": invalid})

    def test_unknown_profile_coordinates_do_not_become_approved_after_calibration(self):
        with TemporaryDirectory() as directory:
            unknown = replace(
                GameProfile.from_settings("Game", self.settings, region=self.dialogue),
                coordinate_space="unknown",
                story_region=self.story,
            )
            store = GameProfileStore(Path(directory) / "profiles.json", [unknown])
            with self.assertRaises(CaptureRegionNeedsCalibration):
                resolve_story_region(unknown.apply(self.settings), store)
            self.assertIsNone(
                unknown.updated_from_settings(
                    self.settings, region=self.dialogue
                ).story_region
            )
            self.assertIsNone(
                store.update_region(unknown.id, self.dialogue).story_region
            )

    def test_global_story_save_preserves_dialogue_and_layout_and_calibration_preserves_story(
        self,
    ):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            layout = OCRRegions(
                DialogRegion(0, 0, 0.4, 0.2), DialogRegion(0, 0.3, 1, 0.7)
            )
            save_dialog_region(self.dialogue, path, ocr_regions=layout)
            save_story_region(
                self.story, path, capture_mode="window", game_window_title="Game"
            )
            self.assertEqual(load_dialog_region(path), self.dialogue)
            self.assertEqual(load_ocr_regions(path), layout)
            save_dialog_region(self.dialogue, path, ocr_regions=layout)
            with patch.dict(os.environ, {"VNTTS_DIALOG_REGION_FILE": str(path)}):
                self.assertEqual(
                    resolve_story_region(
                        self.settings,
                        GameProfileStore(Path(directory) / "profiles.json"),
                    ),
                    self.story,
                )
                self.assertEqual(
                    get_story_region(capture_mode="window", game_window_title="Game"),
                    self.story,
                )
                self.assertIsNone(
                    get_story_region(capture_mode="screen", game_window_title="Game")
                )
                self.assertIsNone(
                    get_story_region(capture_mode="window", game_window_title="Other")
                )
            save_story_region(
                None, path, capture_mode="window", game_window_title="Game"
            )
            self.assertEqual(load_dialog_region(path), self.dialogue)
            self.assertEqual(load_ocr_regions(path), layout)
            self.assertNotIn("story_region", json.loads(path.read_text()))

    def test_global_absent_unbound_and_invalid_permission_are_not_authorized(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            with patch.dict(os.environ, {"VNTTS_DIALOG_REGION_FILE": str(path)}):
                self.assertIsNone(
                    get_story_region(capture_mode="window", game_window_title="Game")
                )

                save_dialog_region(self.dialogue, path)
                for changes in (
                    {},
                    {"story_region": self.story.to_json()},
                    {
                        "story_region": {"left": 0, "top": 0, "width": 2, "height": 1},
                        "story_binding": {
                            "capture_mode": "window",
                            "game_window_title": "Game",
                        },
                    },
                ):
                    with self.subTest(changes=changes):
                        path.write_text(
                            json.dumps({**self.dialogue.to_json(), **changes})
                        )
                        self.assertIsNone(
                            get_story_region(
                                capture_mode="window", game_window_title="Game"
                            )
                        )

    def test_story_saved_with_environment_crop_is_rechecked_when_override_disappears(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "capture.json"
            store = GameProfileStore(root / "profiles.json")
            with patch.dict(os.environ, {"VNTTS_DIALOG_REGION_FILE": str(path)}):
                with patch.dict(os.environ, {"VNTTS_DIALOG_REGION": "0.1,0.7,0.8,0.2"}):
                    save_story_region(
                        self.story,
                        path,
                        capture_mode="window",
                        game_window_title="Game",
                    )
                    self.assertEqual(
                        resolve_story_region(self.settings, store), self.story
                    )
                self.assertIsNone(resolve_story_region(self.settings, store))

    def test_revoking_invalid_global_configuration_is_harmless(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            for contents in ("{broken", "null", "[]"):
                with self.subTest(contents=contents):
                    path.write_text(contents)
                    save_story_region(
                        None, path, capture_mode="window", game_window_title="Other"
                    )
                    self.assertEqual(path.read_text(), contents)

    def test_unknown_global_coordinates_require_recalibration_without_relabeling_story(
        self,
    ):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            save_story_region(
                self.story, path, capture_mode="window", game_window_title="Game"
            )
            value = json.loads(path.read_text())
            value["coordinate_space"] = "unknown"
            path.write_text(json.dumps(value))
            with patch.dict(os.environ, {"VNTTS_DIALOG_REGION_FILE": str(path)}):
                with self.assertRaises(CaptureRegionNeedsCalibration):
                    get_story_region(capture_mode="window", game_window_title="Game")
                save_dialog_region(self.dialogue, path)
                self.assertIsNone(
                    get_story_region(capture_mode="window", game_window_title="Game")
                )
                self.assertEqual(
                    json.loads(path.read_text())["coordinate_space"],
                    CAPTURE_COORDINATE_SPACE,
                )

    def test_global_binding_change_revokes_permission_even_after_switching_back(self):
        for changed in (
            self.settings.updated(capture_mode="screen"),
            self.settings.updated(game_window_title="Another game"),
        ):
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                root = Path(directory)
                capture_path = root / "capture.json"
                settings_path = root / "settings.json"
                self.settings.save(settings_path)
                owner = DurableSettingsMixin()
                owner.settings = self.settings
                owner._settings_path = settings_path
                owner._settings_revision = file_revision(settings_path)
                owner._settings_commit_lock = Lock()
                owner._last_saved_settings = self.settings
                save_story_region(
                    self.story,
                    capture_path,
                    capture_mode="window",
                    game_window_title="Game",
                )
                with patch.dict(
                    os.environ, {"VNTTS_DIALOG_REGION_FILE": str(capture_path)}
                ):
                    owner._save_settings_candidate(
                        self.settings.updated(ocr_language="jpn")
                    )
                    self.assertEqual(
                        get_story_region(
                            capture_mode="window", game_window_title="Game"
                        ),
                        self.story,
                    )
                    owner._save_settings_candidate(changed)
                    owner.settings = changed
                    owner._save_settings_candidate(self.settings)
                    self.assertIsNone(
                        get_story_region(
                            capture_mode="window", game_window_title="Game"
                        )
                    )


if __name__ == "__main__":
    unittest.main()
