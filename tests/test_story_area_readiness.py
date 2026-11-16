import json
import os
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts.ocr import DialogRegion, save_dialog_region, save_story_region
from vntts.onboarding import DiagnosticResult, OnboardingDiagnostics
from vntts.profiles import GameProfile, GameProfileStore
from vntts.settings import AppSettings
from vntts.support import (
    RuntimeSupportLog,
    SupportBundleBuilder,
    collect_capture_configuration,
)


class StoryAreaReadinessTest(unittest.TestCase):
    dialogue = DialogRegion(0.1, 0.7, 0.8, 0.2)
    story = DialogRegion(0.05, 0.1, 0.9, 0.8)

    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "VNTTS_DIALOG_REGION_FILE": str(self.directory / "region.json"),
                    "VNTTS_DIALOG_REGION": "",
                },
            )
        )
        save_dialog_region(self.dialogue, self.directory / "region.json")
        self.settings = AppSettings(
            capture_mode="window",
            game_window_title="PRIVATE TARGET",
            discover_text_layouts=True,
        )
        self.store = GameProfileStore(self.directory / "profiles.json")

    def diagnostics(self):
        return OnboardingDiagnostics(profile_store_loader=lambda: self.store)

    def global_story_area(self):
        save_story_region(
            self.story,
            self.directory / "region.json",
            capture_mode="window",
            game_window_title="PRIVATE TARGET",
        )

    def profile(self, *, target="PRIVATE TARGET", story=None):
        profile = GameProfile.from_settings(
            "PRIVATE PROFILE",
            self.settings.updated(game_window_title=target),
            region=self.dialogue,
            profile_id="fixture",
        )
        self.store.profiles = [replace(profile, story_region=story)]
        self.settings = self.settings.updated(active_profile_id="fixture")

    def test_enabled_missing_story_area_warns_without_blocking_dialogue(self):
        result = self.diagnostics()._check_story_area(self.settings)
        self.assertEqual(result.status, "warning")
        self.assertTrue(result.passed)
        self.assertEqual(result.remediation, "story-area")
        self.assertIn("Set Story area", result.message)
        self.assertIn("Story phrases outside Dialogue area", result.message)
        self.assertIn("Dialogue-only reading remains available", result.message)

    def test_disabled_discovery_needs_no_profile_or_extra_permission(self):
        loader = Mock(
            side_effect=AssertionError("Disabled discovery should not load profiles")
        )
        diagnostics = OnboardingDiagnostics(profile_store_loader=loader)
        result = diagnostics._check_story_area(
            self.settings.updated(discover_text_layouts=False)
        )
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.remediation)
        loader.assert_not_called()

    def test_valid_target_bound_global_and_profile_story_areas_are_ready(self):
        self.global_story_area()
        before = (self.directory / "region.json").read_bytes()
        self.assertEqual(
            self.diagnostics()._check_story_area(self.settings).status, "ok"
        )
        self.assertEqual((self.directory / "region.json").read_bytes(), before)
        self.profile(story=self.story)
        self.assertEqual(
            self.diagnostics()._check_story_area(self.settings).status, "ok"
        )
        self.assertFalse(self.store.path.exists())

    def test_target_mismatch_and_noncontaining_profile_never_reuse_permission(self):
        self.global_story_area()
        other_target = self.settings.updated(game_window_title="Other window")
        self.assertEqual(
            self.diagnostics()._check_story_area(other_target).status, "warning"
        )
        self.profile(target="Other window", story=self.story)
        self.assertEqual(
            self.diagnostics()._check_story_area(self.settings).status, "warning"
        )
        self.profile(story=DialogRegion(0.1, 0.1, 0.8, 0.2))
        self.assertEqual(
            self.diagnostics()._check_story_area(self.settings).status, "warning"
        )

    def test_missing_selected_profile_cannot_fall_back_to_global_story_permission(self):
        self.global_story_area()
        settings = self.settings.updated(active_profile_id="missing")
        self.assertEqual(
            self.diagnostics()._check_story_area(settings).status, "warning"
        )
        report = collect_capture_configuration(settings, self.store)
        self.assertFalse(report["story_area_permitted"])
        self.assertIsNone(report["story_area"])

    def test_legacy_coordinate_space_warns_and_is_not_exported(self):
        self.profile(story=self.story)
        self.store.profiles = [
            replace(self.store.profiles[0], coordinate_space="legacy")
        ]
        result = self.diagnostics()._check_story_area(self.settings)
        self.assertEqual(result.status, "warning")
        self.assertIn("recalibration", result.message)
        report = collect_capture_configuration(self.settings, self.store)
        self.assertFalse(report["available"])
        self.assertFalse(report["story_area_permitted"])
        self.assertIsNone(report["story_area"])
        self.assertIsNone(report["dialogue_area"])

    def test_run_includes_nonblocking_story_area_result(self):
        diagnostics = self.diagnostics()
        for name in (
            "_check_hotkeys",
            "_check_capture_worker",
            "_check_capture_source",
            "_check_tesseract",
            "_check_audio",
            "_check_model",
            "_check_voice_manifest",
        ):
            setattr(
                diagnostics,
                name,
                Mock(return_value=DiagnosticResult(name, "ok", "ready")),
            )
        diagnostics._check_platform_permissions = Mock(return_value=None)
        results = diagnostics.run(self.settings)
        self.assertTrue(all(result.passed for result in results))
        self.assertEqual(results[-1].name, "Story area")
        self.assertEqual(results[-1].status, "warning")

    def test_capture_report_contains_only_effective_authorized_geometry(self):
        self.profile(story=self.story)
        report = collect_capture_configuration(self.settings, self.store)
        self.assertTrue(report["available"])
        self.assertTrue(report["story_area_present"])
        self.assertTrue(report["story_area_permitted"])
        self.assertTrue(report["discover_text_layouts_enabled"])
        self.assertTrue(report["story_area_contains_dialogue"])
        self.assertEqual(report["story_area"], self.story.to_json())
        self.assertEqual(report["dialogue_area"], self.dialogue.to_json())
        serialized = json.dumps(report)
        for private in ("PRIVATE", str(self.directory), "fixture", "profiles.json"):
            self.assertNotIn(private, serialized)
        self.profile(target="Other window", story=self.story)
        mismatch = collect_capture_configuration(self.settings, self.store)
        self.assertFalse(mismatch["story_area_permitted"])
        self.assertFalse(mismatch["story_area_present"])
        self.assertIsNone(mismatch["story_area"])
        self.assertIsNone(mismatch["story_area_contains_dialogue"])

    def test_support_export_works_without_diagnostic_and_loads_fresh_configuration(
        self,
    ):
        self.profile(story=self.story)
        self.store.save()
        before = self.store.path.read_bytes()
        loader = Mock(side_effect=lambda: GameProfileStore.load(self.store.path))
        builder = SupportBundleBuilder(
            self.settings,
            RuntimeSupportLog(),
            diagnostic=None,
            dependency_probe=lambda: {},
            profile_store_loader=loader,
        )
        output = builder.build(self.directory / "support.zip")
        with zipfile.ZipFile(output) as archive:
            report = json.loads(archive.read("capture-configuration.json"))
            self.assertTrue(report["story_area_permitted"])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.update_story_region("fixture", None)
        builder.build(output)
        with zipfile.ZipFile(output) as archive:
            report = json.loads(archive.read("capture-configuration.json"))
            self.assertFalse(report["story_area_permitted"])
            self.assertIsNone(report["story_area"])
        self.assertEqual(loader.call_count, 2)

    def test_disabled_discovery_report_keeps_permission_separate_from_enabled_flag(
        self,
    ):
        self.global_story_area()
        report = collect_capture_configuration(
            self.settings.updated(discover_text_layouts=False), self.store
        )
        self.assertFalse(report["discover_text_layouts_enabled"])
        self.assertTrue(report["story_area_permitted"])
        self.assertEqual(report["story_area"], self.story.to_json())


if __name__ == "__main__":
    unittest.main()
