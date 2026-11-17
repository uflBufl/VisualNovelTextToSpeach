import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.durable_settings import DurableSettingsMixin  # noqa: E402
from vntts.ocr import (
    DialogRegion,
    get_story_region,
    save_dialog_region,
    save_story_region,
)  # noqa: E402
from vntts.onboarding import DiagnosticResult  # noqa: E402
from vntts.onboarding_ui import OnboardingWizard  # noqa: E402
from vntts.profiles import (  # noqa: E402
    GameProfile,
    GameProfileStore,
    profiles_schema_version,
)
from vntts.readiness_ui import ReadinessDialog  # noqa: E402
from vntts.settings import AppSettings, load_app_settings  # noqa: E402
from vntts.versioned_json import (  # noqa: E402
    StaleDocumentError,
    write_versioned_json_if_unchanged,
)


class SetupSettings(DurableSettingsMixin):
    pass


class StoryAreaSetupTest(unittest.TestCase):
    dialogue = DialogRegion(0.1, 0.7, 0.8, 0.2)
    story = DialogRegion(0.05, 0.1, 0.9, 0.8)

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.region_file = self.root / "region.json"
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "VNTTS_DIALOG_REGION_FILE": str(self.region_file),
                    "VNTTS_DIALOG_REGION": "",
                },
            )
        )
        self.settings = AppSettings(
            capture_mode="window", game_window_title="Game", discover_text_layouts=True
        )
        self.host = SetupSettings()
        self.host.settings = self.settings
        self.host._last_saved_settings = None
        self.host._settings_commit_lock = Lock()
        self.host._settings_path = self.root / "settings.json"
        self.host._settings_revision = None
        self.host.profile_store = GameProfileStore(self.root / "profiles.json")

    def wizard(self, settings=None):
        wizard = OnboardingWizard(
            settings or self.settings,
            reading_setup=True,
            auto_discover_windows=False,
            diagnostics=Mock(),
            capture_region=self.dialogue,
        )
        wizard.diagnostics_page.runner.start = Mock()
        self.addCleanup(wizard.deleteLater)
        self.addCleanup(wizard.reject)
        wizard.show_page(wizard.pages.index(wizard.calibration_page))
        return wizard

    def test_story_area_is_explicit_and_separate_from_dialogue(self):
        wizard = self.wizard()
        page = wizard.calibration_page
        page.finish_calibration(self.dialogue)
        self.assertFalse(page.isComplete())
        self.assertFalse(wizard.next_button.isEnabled())
        self.assertTrue(page.story_button.isDefault())
        self.assertIn("centre", page.story_instructions.text())
        self.assertIn("any game frame", page.story_instructions.text())
        self.assertIn(
            "No central phrase needs to be visible", page.story_instructions.text()
        )
        self.assertIn("automatically", page.story_instructions.text())
        page.finish_story_area(self.story)
        self.assertTrue(page.isComplete())
        self.assertTrue(wizard.next_button.isEnabled())
        self.assertEqual(wizard.capture_regions(), (self.dialogue, self.story))
        self.assertEqual(page.capture_region, self.dialogue)

    def test_story_selection_passes_draft_preview_to_frozen_overlay(self):
        wizard = self.wizard()
        page = wizard.calibration_page
        page.finish_calibration(self.dialogue)
        preview = Mock()
        page.story_recognizer = preview
        page._capture_story_area = True
        page._capture_binding = (
            self.settings.capture_mode,
            self.settings.game_window_title,
        )
        image = Image.new("RGB", (640, 480))
        with patch("vntts.onboarding_ui.show_calibration_overlay") as show:
            page._capture_finished((None, image), None)
        self.assertIs(show.call_args.kwargs["background"], image)
        self.assertIs(show.call_args.kwargs["story_recognizer"], preview)
        self.assertEqual(show.call_args.kwargs["story_dialogue_region"], self.dialogue)
        self.assertIsNone(show.call_args.kwargs["capture_region"])
        self.assertIsNone(page.story_region)
        self.assertFalse(page.isComplete())

    def test_saved_discovery_opt_out_cannot_skip_story_area_after_back_navigation(self):
        wizard = self.wizard(self.settings.updated(discover_text_layouts=False))
        page = wizard.calibration_page
        page.finish_calibration(self.dialogue)
        self.assertFalse(page.isComplete())
        self.assertFalse(wizard.next_button.isEnabled())
        self.assertFalse(hasattr(page, "discover_story_text"))
        wizard.show_page(0)
        wizard.next_page()
        self.assertTrue(wizard.draft_settings.discover_text_layouts)
        wizard.show_page(wizard.pages.index(page))
        page.finish_calibration(self.dialogue)
        self.assertFalse(page.isComplete())
        self.assertEqual(wizard.capture_regions(), (self.dialogue, None))
        page.finish_story_area(self.story)
        wizard.test_page.set_result(True, "Read the dialogue")
        wizard.accept()
        self.assertTrue(wizard.settings().onboarding_completed)
        self.assertTrue(wizard.settings().discover_text_layouts)
        self.assertEqual(wizard.capture_regions(), (self.dialogue, self.story))

    def test_incoming_story_area_must_contain_dialogue_to_complete_setup(self):
        wizard = self.wizard(self.settings.updated(discover_text_layouts=False))
        page = wizard.calibration_page
        page.finish_calibration(self.dialogue)
        page.story_region = DialogRegion(0.1, 0.1, 0.2, 0.2)
        self.assertFalse(page.isComplete())
        wizard.test_page.set_result(True, "Read the dialogue")
        wizard.accept()
        self.assertIsNone(wizard.completed_settings)

    def test_cancel_does_not_publish_the_story_area(self):
        wizard = self.wizard()
        wizard.calibration_page.finish_calibration(self.dialogue)
        wizard.calibration_page.finish_story_area(self.story)
        wizard.reject()
        self.assertIsNone(
            get_story_region(capture_mode="window", game_window_title="Game")
        )
        self.assertFalse(self.host._settings_path.exists())

    def test_new_target_invalidates_story_permission_and_profile_selection(self):
        wizard = self.wizard(self.settings.updated(active_profile_id="old-profile"))
        wizard.calibration_page.finish_calibration(self.dialogue)
        wizard.calibration_page.finish_story_area(self.story)
        wizard.show_page(0)
        wizard.configuration_page.game_window.setCurrentText("Other game")
        wizard.next_page()
        self.assertIsNone(wizard.calibration_page.story_region)
        self.assertFalse(wizard.calibration_page.calibrated)
        self.assertIsNone(wizard.draft_settings.active_profile_id)

    def test_story_area_must_include_dialogue_and_rejects_stale_capture_target(self):
        wizard = self.wizard()
        page = wizard.calibration_page
        page.finish_calibration(self.dialogue)
        page.finish_story_area(DialogRegion(0.1, 0.1, 0.2, 0.2))
        self.assertFalse(page.isComplete())
        page._capture_binding = ("window", "Other game")
        page.finish_story_area(self.story)
        self.assertFalse(page.isComplete())
        with patch("vntts.onboarding_ui.show_calibration_overlay") as show:
            page._capture_finished((None, Image.new("RGB", (640, 480))), None)
        show.assert_not_called()
        self.assertIn("changed", page.status.text())

    def test_save_after_target_change_preserves_only_newly_confirmed_permission(self):
        save_dialog_region(self.dialogue, self.region_file)
        save_story_region(
            self.story,
            self.region_file,
            capture_mode="window",
            game_window_title="Game",
        )
        selected = self.settings.updated(
            game_window_title="Other game", onboarding_completed=True
        )
        self.host._save_settings_candidate(
            selected, dialog_region=self.dialogue, story_region=self.story
        )
        self.assertTrue(
            load_app_settings(self.host._settings_path).onboarding_completed
        )
        self.assertEqual(
            get_story_region(capture_mode="window", game_window_title="Other game"),
            self.story,
        )
        self.assertIsNone(
            get_story_region(capture_mode="window", game_window_title="Game")
        )

    def test_region_write_failure_does_not_mark_setup_complete(self):
        with patch(
            "vntts.durable_settings.save_story_region", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.host._save_settings_candidate(
                    self.settings.updated(onboarding_completed=True),
                    dialog_region=self.dialogue,
                    story_region=self.story,
                )
        self.assertFalse(self.host._settings_path.exists())
        self.assertIsNone(self.host._last_saved_settings)

    def test_profile_areas_save_without_authorizing_global_or_other_profile(self):
        profile = GameProfile.from_settings(
            "Fixture", self.settings, region=self.dialogue, profile_id="fixture"
        )
        self.host.profile_store.profiles = [profile]
        selected = self.settings.updated(active_profile_id="fixture")
        self.host._save_settings_candidate(
            selected, dialog_region=self.dialogue, story_region=self.story
        )
        saved = GameProfileStore.load(self.host.profile_store.path).get("fixture")
        self.assertEqual(saved.story_region, self.story)
        self.assertIsNone(
            get_story_region(capture_mode="window", game_window_title="Game")
        )

    def test_stale_settings_are_rejected_before_any_story_grant(self):
        self.host._save_settings_candidate(self.settings)
        self.settings.updated(output_volume_percent=42).save(self.host._settings_path)
        with self.assertRaises(StaleDocumentError):
            self.host._save_settings_candidate(
                self.settings.updated(onboarding_completed=True),
                dialog_region=self.dialogue,
                story_region=self.story,
            )
        self.assertFalse(self.region_file.exists())
        saved = load_app_settings(self.host._settings_path)
        self.assertEqual(saved.output_volume_percent, 42)
        self.assertFalse(saved.onboarding_completed)

    def test_settings_write_failure_rolls_back_a_new_story_grant(self):
        self.host._save_settings_candidate(self.settings)
        with patch(
            "vntts.durable_settings.write_versioned_json_if_unchanged",
            side_effect=OSError("settings disk full"),
        ):
            with self.assertRaisesRegex(OSError, "settings disk full"):
                self.host._save_settings_candidate(
                    self.settings.updated(onboarding_completed=True),
                    dialog_region=self.dialogue,
                    story_region=self.story,
                )
        self.assertIsNone(
            get_story_region(capture_mode="window", game_window_title="Game")
        )
        self.assertFalse(
            load_app_settings(self.host._settings_path).onboarding_completed
        )
        self.assertFalse(self.host._last_saved_settings.onboarding_completed)

    def test_story_writer_that_publishes_then_fails_is_rolled_back(self):
        def publish_then_fail(*args, **kwargs):
            save_story_region(*args, **kwargs)
            raise OSError("region durability failure")

        with patch(
            "vntts.durable_settings.save_story_region", side_effect=publish_then_fail
        ):
            with self.assertRaisesRegex(OSError, "region durability failure"):
                self.host._save_settings_candidate(
                    self.settings.updated(onboarding_completed=True),
                    dialog_region=self.dialogue,
                    story_region=self.story,
                )
        self.assertIsNone(
            get_story_region(capture_mode="window", game_window_title="Game")
        )
        self.assertFalse(self.host._settings_path.exists())
        self.assertIsNone(self.host._last_saved_settings)

    def test_settings_writer_that_publishes_then_fails_restores_completion_and_grant(
        self,
    ):
        self.host._save_settings_candidate(self.settings)
        import vntts.durable_settings as durable

        write_settings = durable.write_versioned_json_if_unchanged

        def publish_then_fail(*args, **kwargs):
            write_settings(*args, **kwargs)
            raise OSError("settings durability failure")

        with patch(
            "vntts.durable_settings.write_versioned_json_if_unchanged",
            side_effect=publish_then_fail,
        ):
            with self.assertRaisesRegex(OSError, "settings durability failure"):
                self.host._save_settings_candidate(
                    self.settings.updated(onboarding_completed=True),
                    dialog_region=self.dialogue,
                    story_region=self.story,
                )
        self.assertFalse(
            load_app_settings(self.host._settings_path).onboarding_completed
        )
        self.assertIsNone(
            get_story_region(capture_mode="window", game_window_title="Game")
        )
        self.assertFalse(self.host._last_saved_settings.onboarding_completed)
        self.host._save_settings_candidate(
            self.settings.updated(output_volume_percent=42)
        )

    def test_late_settings_cas_failure_preserves_concurrent_settings_and_region_edits(
        self,
    ):
        self.host._save_settings_candidate(self.settings)
        import vntts.durable_settings as durable

        write_settings = durable.write_versioned_json_if_unchanged

        def concurrent_edit(*args, **kwargs):
            self.settings.updated(output_volume_percent=42).save(
                self.host._settings_path
            )
            region = json.loads(self.region_file.read_text(encoding="utf-8"))
            region["peer_note"] = "keep this unrelated value"
            self.region_file.write_text(json.dumps(region), encoding="utf-8")
            return write_settings(*args, **kwargs)

        with patch(
            "vntts.durable_settings.write_versioned_json_if_unchanged",
            side_effect=concurrent_edit,
        ):
            with self.assertRaises(StaleDocumentError):
                self.host._save_settings_candidate(
                    self.settings.updated(onboarding_completed=True),
                    dialog_region=self.dialogue,
                    story_region=self.story,
                )
        self.assertEqual(
            json.loads(self.region_file.read_text(encoding="utf-8"))["peer_note"],
            "keep this unrelated value",
        )
        self.assertIsNone(
            get_story_region(capture_mode="window", game_window_title="Game")
        )
        saved = load_app_settings(self.host._settings_path)
        self.assertEqual(saved.output_volume_percent, 42)
        self.assertFalse(saved.onboarding_completed)

    def test_losing_settings_cas_does_not_rollback_a_peers_identical_values(self):
        candidate = self.settings.updated(onboarding_completed=True)
        peer_bytes = None

        def concurrent_edit(*args, **kwargs):
            nonlocal peer_bytes
            candidate.save(self.host._settings_path)
            peer_bytes = self.host._settings_path.read_bytes()
            return write_versioned_json_if_unchanged(*args, **kwargs)

        with patch(
            "vntts.durable_settings.write_versioned_json_if_unchanged",
            side_effect=concurrent_edit,
        ):
            with self.assertRaises(StaleDocumentError) as caught:
                self.host._save_settings_candidate(
                    candidate, dialog_region=self.dialogue, story_region=self.story
                )
        self.assertFalse(caught.exception.publication_started)
        self.assertEqual(self.host._settings_path.read_bytes(), peer_bytes)
        self.assertTrue(
            load_app_settings(self.host._settings_path).onboarding_completed
        )
        self.assertIsNone(
            get_story_region(capture_mode="window", game_window_title="Game")
        )

    def test_postpublication_cas_failure_still_rolls_back_owned_settings(self):
        def publish_then_fail(*args, **kwargs):
            write_versioned_json_if_unchanged(*args, **kwargs)
            raise StaleDocumentError(
                "Postpublication failure", publication_started=True
            )

        with patch(
            "vntts.durable_settings.write_versioned_json_if_unchanged",
            side_effect=publish_then_fail,
        ):
            with self.assertRaisesRegex(StaleDocumentError, "Postpublication failure"):
                self.host._save_settings_candidate(
                    self.settings.updated(onboarding_completed=True),
                    dialog_region=self.dialogue,
                    story_region=self.story,
                )
        self.assertFalse(self.host._settings_path.exists())
        self.assertIsNone(
            get_story_region(capture_mode="window", game_window_title="Game")
        )

    def test_failed_postpublication_verification_marks_started_publication(self):
        for observed_revision in (None, b"concurrent document"):
            with self.subTest(observed_revision=observed_revision):
                with patch(
                    "vntts.versioned_json.file_revision",
                    side_effect=(None, observed_revision),
                ):
                    with self.assertRaises(StaleDocumentError) as caught:
                        write_versioned_json_if_unchanged(
                            self.root / "postpublication.json",
                            1,
                            {"saved": True},
                            revision=None,
                            document_name="Fixture",
                        )
                self.assertTrue(caught.exception.publication_started)

    def saved_profile(self):
        profile = GameProfile.from_settings(
            "Fixture", self.settings, region=self.dialogue, profile_id="fixture"
        )
        self.host.profile_store.profiles = [profile]
        self.host.profile_store.save()
        return self.settings.updated(active_profile_id="fixture")

    def test_failed_profile_write_restores_capture_fields_and_refreshes_store(self):
        selected = self.saved_profile()
        previous = self.host.profile_store.get("fixture")
        changed_dialogue = DialogRegion(0.15, 0.7, 0.7, 0.2)
        with patch.object(
            self.host.profile_store,
            "update_story_region",
            side_effect=OSError("profile disk full"),
        ):
            with self.assertRaisesRegex(OSError, "profile disk full"):
                self.host._save_settings_candidate(
                    selected.updated(onboarding_completed=True),
                    dialog_region=changed_dialogue,
                    story_region=self.story,
                )
        self.assertFalse(self.host._settings_path.exists())
        self.assertEqual(
            GameProfileStore.load(self.host.profile_store.path).get("fixture"), previous
        )
        self.assertEqual(self.host.profile_store.get("fixture"), previous)
        self.host.profile_store.rename("fixture", "Ready for next edit")

    def test_losing_profile_cas_does_not_rollback_a_peers_identical_capture_fields(
        self,
    ):
        for method in ("update_region", "update_story_region"):
            with self.subTest(method=method):
                self.host.profile_store = GameProfileStore.load(
                    self.host.profile_store.path
                )
                selected = self.saved_profile()
                changed_dialogue = DialogRegion(0.15, 0.7, 0.7, 0.2)
                write_capture = getattr(self.host.profile_store, method)

                def concurrent_edit(*args):
                    peer = GameProfileStore.load(self.host.profile_store.path)
                    getattr(peer, method)(*args)
                    return write_capture(*args)

                with patch.object(
                    self.host.profile_store, method, side_effect=concurrent_edit
                ):
                    with self.assertRaises(StaleDocumentError) as caught:
                        self.host._save_settings_candidate(
                            selected.updated(onboarding_completed=True),
                            dialog_region=changed_dialogue,
                            story_region=self.story,
                        )
                self.assertFalse(caught.exception.publication_started)
                saved = GameProfileStore.load(self.host.profile_store.path).get(
                    "fixture"
                )
                if method == "update_region":
                    self.assertEqual(saved.dialog_region, changed_dialogue)
                else:
                    self.assertEqual(saved.story_region, self.story)
                self.assertFalse(self.host._settings_path.exists())

    def test_settings_failure_restores_only_owned_profile_fields(self):
        selected = self.saved_profile()

        def fail_settings(*_args, **_kwargs):
            peer = GameProfileStore.load(self.host.profile_store.path)
            peer.rename("fixture", "Peer title")
            raise OSError("settings failure")

        with patch(
            "vntts.durable_settings.write_versioned_json_if_unchanged",
            side_effect=fail_settings,
        ):
            with self.assertRaisesRegex(OSError, "settings failure"):
                self.host._save_settings_candidate(
                    selected.updated(onboarding_completed=True),
                    dialog_region=self.dialogue,
                    story_region=self.story,
                )
        saved = GameProfileStore.load(self.host.profile_store.path).get("fixture")
        self.assertEqual(saved.name, "Peer title")
        self.assertIsNone(saved.story_region)
        self.host.profile_store.rename("fixture", "Next edit")

    def test_future_profile_schema_between_capture_writes_is_preserved(self):
        selected = self.saved_profile()
        changed_dialogue = DialogRegion(0.15, 0.7, 0.7, 0.2)
        write_region = self.host.profile_store.update_region
        peer_bytes = None

        def update_then_upgrade(*args):
            nonlocal peer_bytes
            result = write_region(*args)
            document = json.loads(
                self.host.profile_store.path.read_text(encoding="utf-8")
            )
            document["schema_version"] = profiles_schema_version + 1
            document["future_field"] = {"preserve": True}
            peer_bytes = json.dumps(document).encode("utf-8")
            self.host.profile_store.path.write_bytes(peer_bytes)
            return result

        with patch.object(
            self.host.profile_store, "update_region", side_effect=update_then_upgrade
        ):
            with self.assertRaisesRegex(OSError, "unsupported Game profiles schema"):
                self.host._save_settings_candidate(
                    selected.updated(onboarding_completed=True),
                    dialog_region=changed_dialogue,
                    story_region=self.story,
                )
        self.assertEqual(self.host.profile_store.path.read_bytes(), peer_bytes)
        self.assertFalse(self.host._settings_path.exists())
        with self.assertRaises(StaleDocumentError):
            self.host.profile_store.create("Another profile", self.settings)
        self.assertEqual(self.host.profile_store.path.read_bytes(), peer_bytes)

    def test_profile_writer_that_publishes_then_fails_restores_story_permission(self):
        selected = self.saved_profile()
        write_story = self.host.profile_store.update_story_region

        def publish_then_fail(*args):
            write_story(*args)
            raise OSError("profile durability failure")

        with patch.object(
            self.host.profile_store,
            "update_story_region",
            side_effect=publish_then_fail,
        ):
            with self.assertRaisesRegex(OSError, "profile durability failure"):
                self.host._save_settings_candidate(
                    selected.updated(onboarding_completed=True),
                    dialog_region=self.dialogue,
                    story_region=self.story,
                )
        saved = GameProfileStore.load(self.host.profile_store.path).get("fixture")
        self.assertIsNone(saved.story_region)
        self.assertIsNone(self.host.profile_store.get("fixture").story_region)
        self.assertFalse(self.host._settings_path.exists())

    def test_rollback_does_not_replace_a_peers_new_story_authority(self):
        peer_story = DialogRegion(0, 0, 1, 1)

        def fail_settings(*_args, **_kwargs):
            save_story_region(
                peer_story,
                self.region_file,
                capture_mode="window",
                game_window_title="Other game",
            )
            raise OSError("settings failure")

        with patch(
            "vntts.durable_settings.write_versioned_json_if_unchanged",
            side_effect=fail_settings,
        ):
            with self.assertRaisesRegex(OSError, "settings failure"):
                self.host._save_settings_candidate(
                    self.settings.updated(onboarding_completed=True),
                    dialog_region=self.dialogue,
                    story_region=self.story,
                )
        self.assertEqual(
            get_story_region(capture_mode="window", game_window_title="Other game"),
            peer_story,
        )
        self.assertIsNone(
            get_story_region(capture_mode="window", game_window_title="Game")
        )

    def test_missing_area_remediation_opens_selection_instead_of_installation_help(
        self,
    ):
        wizard = self.wizard()
        page = wizard.diagnostics_page
        page._checks_finished(
            (
                DiagnosticResult(
                    "Story area", "warning", "Select Story area", "story-area"
                ),
            ),
            None,
        )
        self.assertEqual(page.remediation_button.text(), "Select reading areas")
        page._run_remediation()
        self.assertIs(wizard.pages[wizard.current_page_index], wizard.calibration_page)
        dialog = ReadinessDialog(self.settings, Mock(), suspended=True)
        self.addCleanup(dialog.deleteLater)
        requested = Mock()
        dialog.story_area_requested.connect(requested)
        dialog._suspended = False
        dialog._cancellation.clear()
        dialog._checks_finished(
            (
                DiagnosticResult(
                    "Story area", "warning", "Select Story area", "story-area"
                ),
            ),
            None,
        )
        self.assertEqual(dialog.remediation_button.text(), "Set story area")
        dialog._run_selected_remediation()
        requested.assert_called_once_with()
