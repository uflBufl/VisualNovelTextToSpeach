import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QSizePolicy  # noqa: E402

from tests.test_authoring_bulk_generation import SyntheticRenderer  # noqa: E402
from tests.test_pregeneration_setup import (  # noqa: E402
    ManualThreadPool,
    write_story_index,
)
from tests.test_pregeneration_voices import (  # noqa: E402
    write_conflicting_manifest,
    write_content,
    write_manifest,
)
from vntts.app import TrayApplication  # noqa: E402
from vntts.async_ui import LatestTaskRunner  # noqa: E402
from vntts.authoring.bulk_generation import run_bulk_generation  # noqa: E402
from vntts.authoring.missing_voice_policy import (  # noqa: E402
    NARRATOR_ROLES,
    MissingVoicePolicy,
)
from vntts.game_content_importer import GameContentImportError  # noqa: E402
from vntts.game_narrator_ui import GameNarratorDialog  # noqa: E402
from vntts.pregeneration_acceptance import OfflineAcceptanceWorker  # noqa: E402
from vntts.pregeneration_activation import OfflinePackActivator  # noqa: E402
from vntts.pregeneration_audition import VoiceAuditionCancelled  # noqa: E402
from vntts.pregeneration_generation import (  # noqa: E402
    OfflineGenerationCancelled,
    OfflineGenerationWorker,
)
from vntts.pregeneration_queue import PregenerationInputStore  # noqa: E402
from vntts.pregeneration_recovery import OfflineRecoveryWorker  # noqa: E402
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    inspect_story_index,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.pregeneration_voices import (  # noqa: E402
    VoiceDecisionStore,
    VoicePlanStore,
)
from vntts.settings import AppSettings  # noqa: E402
from vntts.speech_presentation import engine_model_label  # noqa: E402
from vntts.synthesis import SynthesisCompletion  # noqa: E402
from vntts.ui_text import plain_label_text  # noqa: E402


class InProcessPocketGenerator(OfflineGenerationWorker):
    def __init__(self):
        super().__init__()
        self.rendered = False

    def generate(self, generation_input, voice_plan, cancel_event=None):
        output = generation_input.directory.parent / (
            f"generation-output-{generation_input.identity[:16]}"
        )
        renderer = SyntheticRenderer(
            [SynthesisCompletion.COMPLETE, SynthesisCompletion.LIMITED]
        )
        renderer.name = "pocket-tts"
        renderer.model_name = "pocket-tts"
        run_bulk_generation(
            generation_input.queue,
            output,
            renderer,
            provider="pocket-tts",
            model="pocket-tts",
            generation_profile=voice_plan.synthesis_profile,
            retries=0,
            cancellation=cancel_event,
            missing_voice_policy=MissingVoicePolicy(
                NARRATOR_ROLES,
                generation_input.narrator_fallback_roles,
            ),
            narrator_character="Narrator",
        )
        self.rendered = True
        return self.inspect(generation_input)


class InterruptingPocketGenerator(InProcessPocketGenerator):
    def __init__(self, *, interrupt):
        super().__init__()
        self.interrupt = interrupt
        self.rendered_texts = []

    def generate(self, generation_input, voice_plan, cancel_event=None):
        output = generation_input.directory.parent / (
            f"generation-output-{generation_input.identity[:16]}"
        )
        renderer = SyntheticRenderer(
            (
                [SynthesisCompletion.COMPLETE, SynthesisCompletion.CANCELLED]
                if self.interrupt
                else [SynthesisCompletion.COMPLETE]
            )
        )
        renderer.name = "pocket-tts"
        renderer.model_name = "pocket-tts"
        run_bulk_generation(
            generation_input.queue,
            output,
            renderer,
            provider="pocket-tts",
            model="pocket-tts",
            generation_profile="default",
            retries=0,
            cancellation=cancel_event,
            missing_voice_policy=MissingVoicePolicy(
                NARRATOR_ROLES,
                generation_input.narrator_fallback_roles,
            ),
            narrator_character="Narrator",
        )
        self.rendered_texts.extend(request.text for request in renderer.requests)
        if self.interrupt:
            raise OfflineGenerationCancelled("Synthetic generation interrupted")
        self.rendered = True
        return self.inspect(generation_input)


class SelfServicePregenerationJourneyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_generation_exception_keeps_exact_copyable_details(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery(()),
                job_store=PregenerationJobStore(root / "jobs"),
            )
            error = RuntimeError("C:\\models\\" + "mlx-model-segment-" * 20)

            dialog._generation_finished(None, error)

            details = f"Unable to generate offline audio: {error}"
            self.assertEqual(dialog.resume_status.text(), details)
            dialog.copy_resume_error.click()
            self.assertEqual(self.application.clipboard().text(), details)
            self.assertEqual(
                dialog.resume_status.sizePolicy().horizontalPolicy(),
                QSizePolicy.Policy.Ignored,
            )
            dialog.deleteLater()

    def test_quit_button_finishes_idle_embedded_preparation(self):
        for action in ("button", "window", "tray"):
            with self.subTest(action=action), TemporaryDirectory() as directory:
                root = Path(directory)
                dialog = OfflineAudioPreparationDialog(
                    AppSettings(),
                    discovery=lambda: ContentDiscovery(()),
                    job_store=PregenerationJobStore(root / "jobs"),
                    voice_decisions=VoiceDecisionStore(root / "voices.json"),
                    thread_pool=ManualThreadPool(),
                )
                tray = TrayApplication(
                    self.application,
                    AppSettings(),
                    controller_factory=Mock(
                        return_value=Mock(is_ready=False, is_live_running=False)
                    ),
                )
                try:
                    with (
                        patch(
                            "vntts.app.OfflineAudioPreparationDialog",
                            return_value=dialog,
                        ),
                        patch.object(self.application, "quit") as quit_application,
                    ):
                        tray.open_pregeneration()
                        tray.dashboard.show_reading()
                        self.application.processEvents()
                        if action == "button":
                            tray.dashboard.quit_button.click()
                        elif action == "window":
                            tray.dashboard.close()
                        else:
                            tray.quit_action.trigger()
                        self.application.processEvents()
                        quit_application.assert_called_once()
                        self.assertIsNone(tray.pregeneration_dialog)
                finally:
                    tray.shutdown()
                    tray.dashboard.deleteLater()
                    tray.compact_controller.deleteLater()

    def test_quit_waits_for_voice_cleanup_then_finishes_idle_stories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pool = ManualThreadPool()
            previews = Mock()
            narrator = GameNarratorDialog(
                AppSettings(), preview_service=previews, player=Mock(), thread_pool=pool
            )
            preparation = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery(()),
                job_store=PregenerationJobStore(root / "jobs"),
                voice_decisions=VoiceDecisionStore(root / "voices.json"),
                thread_pool=ManualThreadPool(),
            )
            tray = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(
                    return_value=Mock(is_ready=False, is_live_running=False)
                ),
            )
            try:
                with (
                    patch(
                        "vntts.app.OfflineAudioPreparationDialog",
                        return_value=preparation,
                    ),
                    patch("vntts.app.GameNarratorDialog", return_value=narrator),
                    patch.object(self.application, "quit") as quit_application,
                ):
                    tray.open_pregeneration()
                    tray.open_voice_previews()
                    narrator._start("preview", "Generating...", lambda: None)
                    tray.dashboard.show_reading()
                    tray.dashboard.quit_button.click()
                    quit_application.assert_not_called()
                    self.assertTrue(narrator.cancellation.is_set())
                    pool.tasks.pop(0).run()
                    self.application.processEvents()
                    quit_application.assert_not_called()
                    previews.close.assert_not_called()
                    pool.tasks.pop(0).run()
                    self.application.processEvents()
                    previews.close.assert_called_once()
                    quit_application.assert_called_once()
                    self.assertIsNone(tray.narrator_dialog)
                    self.assertIsNone(tray.pregeneration_dialog)
            finally:
                tray.shutdown()
                tray.dashboard.deleteLater()
                tray.compact_controller.deleteLater()

    def test_cancelled_discovery_can_finish_after_its_ui_is_deleted(self):
        pool = ManualThreadPool()
        runner = LatestTaskRunner(thread_pool=pool)
        received = []
        runner.finished.connect(lambda result, error: received.append(result))
        runner.start(lambda: "obsolete result")
        runner.cancel()
        runner.deleteLater()
        QCoreApplication.sendPostedEvents(runner, QEvent.Type.DeferredDelete)
        pool.tasks.pop(0).run()
        self.application.processEvents()
        self.assertEqual(received, [])

    def test_embedded_preparation_keeps_work_on_navigation_and_waits_before_quit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_story_index(root / "content"))
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                thread_pool=pool,
                voice_decisions=VoiceDecisionStore(root / "voices.json"),
            )
            controller = Mock(is_ready=False, is_live_running=False)
            tray = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(return_value=controller),
            )
            with patch("vntts.app.OfflineAudioPreparationDialog", return_value=dialog):
                tray.open_pregeneration()
                dialog.select_all_button.click()
                dialog.continue_button.click()
                self.assertTrue(dialog.has_pending_work())
                tray.dashboard.show_reading()
                self.assertIs(tray.open_pregeneration(), dialog)
                tray.prepare_reading()
                self.assertIsNone(tray.onboarding_wizard)
                controller.start.assert_not_called()
                with patch.object(self.application, "quit") as quit_application:
                    tray.dashboard.quit_button.click()
                    quit_application.assert_not_called()
                    self.assertTrue(dialog.voice_cancel_event.is_set())
                    pool.tasks.pop(0).run()
                    self.application.processEvents()
                    quit_application.assert_called_once()
                self.assertIsNone(tray.pregeneration_dialog)
            tray.shutdown()

    def test_pregeneration_shows_and_applies_narrator_choice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            selected = AppSettings(voice_assignments={"Narrator": "preset:marius"})
            chooser = Mock(return_value=selected)
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                game_narrator_chooser=chooser,
            )

            self.assertTrue(dialog.game_narrator_button.isVisibleTo(dialog))
            self.assertIn("Alba", plain_label_text(dialog.narrator_status))
            dialog.game_narrator_button.click()

            chooser.assert_called_once_with(AppSettings(), dialog)
            self.assertIs(dialog.settings, selected)
            self.assertIn("Marius", plain_label_text(dialog.narrator_status))
            self.assertIn("no account", dialog.pocket_terms.text())
            self.assertTrue(dialog.model_choice.isHidden())
            self.assertTrue(dialog.engine_controls.isHidden())
            self.assertTrue(dialog.pocket_terms.isHidden())
            self.assertTrue(dialog.pocket_voice_cloning.isHidden())
            chooser.return_value = None
            dialog.game_narrator_button.click()
            self.assertIs(dialog.settings, selected)

    def test_character_route_opens_editor_and_saved_choice_invalidates_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_content(root / "content"))
            manifest = write_manifest(root / "voices")
            for name in ("rhiannon", "centurion", "unrelated"):
                sf.write(
                    manifest.parent / "references" / f"{name}.wav",
                    np.zeros(2400),
                    24_000,
                    subtype="PCM_16",
                )
            settings = AppSettings(voice_manifest=str(manifest))
            chooser = Mock(return_value=None)
            player = Mock()
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                game_narrator_chooser=chooser,
                preview_player=player,
                thread_pool=pool,
            )
            self.assertFalse(dialog.choose_character_voice.isEnabled())
            dialog.select_all_button.click()
            dialog.continue_button.click()
            for _ in range(2):
                pool.tasks.pop(0).run()
                self.application.processEvents()
            plan = dialog.voice_plan()
            # This UI check uses a distinct target; persistence is covered by
            # the imported-character save/reload/planning regression.
            plan = replace(
                plan,
                groups=tuple(
                    replace(group, character="Aderyn")
                    if group.character == "Rhiannon"
                    else group
                    for group in plan.groups
                ),
            )
            dialog._voice_plan = plan
            dialog.show_all_voice_routes.setChecked(True)
            dialog._render_voice_routes(plan)
            for row in range(dialog.voice_routes.count()):
                if (
                    dialog.voice_routes.item(row).data(Qt.ItemDataRole.UserRole)
                    == "Aderyn"
                ):
                    dialog.voice_routes.setCurrentRow(row)
                    break
            dialog._render_voice_routes(plan)
            dialog.choose_character_voice.click()
            chooser.assert_called_once_with(settings, dialog, character="Aderyn")
            player.stop.assert_called()
            self.assertIs(dialog.voice_plan(), plan)
            self.assertIs(dialog.settings, settings)

            saved = settings.updated(
                character_voice_defaults={"Aderyn": "character:rhiannon"}
            )
            chooser.return_value = saved
            dialog.choose_character_voice.click()
            self.assertIs(dialog.settings, saved)
            self.assertIsNone(dialog.voice_plan())
            self.assertTrue(dialog.voice_confirmation.isHidden())
            self.assertTrue(dialog.continue_button.isEnabled())
            self.assertEqual(pool.tasks, [])
            dialog.reject()
            dialog.deleteLater()

    def test_shared_voice_save_replans_selected_stories_with_the_saved_engine(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_content(root / "content"))
            manifest = write_manifest(root / "voices")
            for name in ("rhiannon", "centurion", "unrelated"):
                sf.write(
                    manifest.parent / "references" / f"{name}.wav",
                    np.zeros(2400),
                    24_000,
                    subtype="PCM_16",
                )
            original = AppSettings(voice_manifest=str(manifest))
            selected = original.updated(
                speech_backend="moss-tts",
                tts_model="selected/model",
                voice_assignments={"Narrator": "character:centurion"},
            )
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                original,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                voice_decisions=VoiceDecisionStore(root / "decisions.json"),
                game_narrator_chooser=Mock(return_value=selected),
                thread_pool=pool,
            )
            dialog.select_all_button.click()
            stories = dialog.selected_story_ids()
            dialog.continue_button.click()
            for _ in range(2):
                pool.tasks.pop(0).run()
                self.application.processEvents()
            self.assertTrue(dialog._awaiting_voice_confirmation)
            self.assertFalse(dialog.stories.isEnabled())
            old_input = dialog.generation_input()

            dialog.game_narrator_button.click()

            self.assertIs(dialog.settings, selected)
            self.assertEqual(dialog.selected_story_ids(), stories)
            self.assertTrue(dialog.stories.isEnabled())
            self.assertTrue(dialog.continue_button.isEnabled())
            self.assertIsNone(dialog.generation_input())
            self.assertIsNone(dialog.voice_plan())
            self.assertFalse(dialog._awaiting_voice_confirmation)
            self.assertTrue(dialog.voice_confirmation.isHidden())
            self.assertIn("Step 1", dialog.step.text())
            self.assertIn("MOSS", plain_label_text(dialog.narrator_status))
            self.assertIn("Engine:", plain_label_text(dialog.narrator_status))
            dialog.copy_narrator_details.click()
            self.assertIn(
                engine_model_label("moss-tts", "selected/model"),
                self.application.clipboard().text(),
            )
            self.assertEqual(dialog.engine_choice.currentData(), "moss-tts")
            self.assertEqual(dialog.model_choice.text(), "selected/model")

            dialog.continue_button.click()
            for _ in range(2):
                pool.tasks.pop(0).run()
                self.application.processEvents()
            self.assertTrue(dialog._awaiting_voice_confirmation)
            self.assertEqual(dialog.voice_plan().synthesis_backend, "moss-tts")
            self.assertEqual(dialog.voice_plan().synthesis_model, "selected/model")
            self.assertNotEqual(dialog.generation_input().identity, old_input.identity)
            self.assertTrue(old_input.directory.exists())
            self.assertTrue(dialog.narrator_controls.isHidden())
            self.assertTrue(dialog.engine_controls.isHidden())
            self.assertTrue(dialog.pocket_terms.isHidden())
            dialog.reject()
            dialog.deleteLater()

    def test_generation_configuration_remains_visible_and_locked_during_work(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_content(root / "content"))
            settings = AppSettings(
                voice_assignments={"Narrator": "character:centurion"}
            )
            dialog = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
            )
            dialog.engine_choice.setCurrentIndex(
                dialog.engine_choice.findData("moss-tts")
            )
            dialog.model_choice.setText("selected/model")
            self.assertEqual(dialog.settings.tts_model, "selected/model")
            self.assertEqual(settings.speech_backend, "pocket-tts")
            dialog.select_all_button.click()
            self.assertIn("Stories:", plain_label_text(dialog.story_context))
            dialog._set_import_controls(False)
            dialog.selection_panel.hide()
            dialog._show_waiting_phase(
                "Generating", "Please wait", "Saved work is retained"
            )
            dialog.show()
            self.application.processEvents()
            self.assertTrue(dialog.progress_configuration.isVisibleTo(dialog))
            self.assertTrue(dialog.story_context.isVisibleTo(dialog))
            self.assertFalse(dialog.engine_choice.isEnabled())
            self.assertFalse(dialog.model_choice.isEnabled())
            self.assertFalse(dialog.game_narrator_button.isEnabled())
            self.assertIn("Centurion", plain_label_text(dialog.progress_configuration))
            self.assertIn("Model:", plain_label_text(dialog.progress_configuration))
            self.assertIn("Source:", plain_label_text(dialog.story_context))
            self.assertTrue(dialog.copy_progress_configuration.isVisibleTo(dialog))
            dialog.copy_progress_configuration.click()
            self.assertIn("Model: selected/model", self.application.clipboard().text())
            self.assertIn(str(content.story_index), self.application.clipboard().text())
            dialog.reject()
            dialog.deleteLater()

    def test_unsupported_engine_stays_blocked_after_discovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_content(root / "content"))
            dialog = OfflineAudioPreparationDialog(
                AppSettings(speech_backend="coqui-xtts"),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
            )
            dialog._set_discovery_loading(False)
            self.assertFalse(dialog.continue_button.isEnabled())
            dialog._save_selection()
            self.assertIsNone(dialog.job())
            self.assertIn(
                "Choose an available generation engine",
                plain_label_text(dialog.summary),
            )
            dialog.engine_choice.setCurrentIndex(
                dialog.engine_choice.findData("pocket-tts")
            )
            dialog.select_all_button.click()
            self.assertTrue(dialog.continue_button.isEnabled())
            dialog.reject()
            dialog.deleteLater()

    def test_game_narrator_and_routes_are_confirmed_before_generation(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_content(root / "content"))
            portraits = root / "content" / "portraits"
            portraits.mkdir()
            portrait = QPixmap(40, 80)
            portrait.fill()
            self.assertTrue(portrait.save(str(portraits / "10.png")))
            manifest = write_manifest(root / "voices")
            for name in ("rhiannon", "centurion", "unrelated"):
                sf.write(
                    manifest.parent / "references" / f"{name}.wav",
                    np.zeros(2400),
                    24_000,
                    subtype="PCM_16",
                )
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(
                    voice_manifest=str(manifest),
                ),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                thread_pool=pool,
            )

            dialog.select_all_button.click()
            dialog.continue_button.click()
            pool.tasks.pop(0).run()
            self.application.processEvents()

            self.assertTrue(dialog.preparing_inputs)
            pool.tasks.pop(0).run()
            self.application.processEvents()

            self.assertTrue(dialog._awaiting_voice_confirmation)
            self.assertEqual(dialog.work_summary.text(), dialog.summary.text())
            self.assertFalse(dialog.show_all_voice_routes.isChecked())
            self.assertTrue(
                all(
                    not dialog.voice_routes.item(index).text().startswith("Narrator ->")
                    for index in range(dialog.voice_routes.count())
                )
            )
            dialog.show_all_voice_routes.setChecked(True)
            self.assertEqual(
                dialog.voice_routes.count(), len(dialog._voice_plan.groups)
            )
            for index in range(dialog.voice_routes.count()):
                item = dialog.voice_routes.item(index)
                self.assertEqual(
                    not item.icon().isNull(), item.text().startswith("Rhiannon ->")
                )
            (portraits / "10.png").unlink()
            dialog._render_voice_routes(dialog._voice_plan)
            self.assertTrue(
                all(
                    dialog.voice_routes.item(index).icon().isNull()
                    for index in range(dialog.voice_routes.count())
                )
            )
            dialog.show_all_voice_routes.setChecked(False)
            self.assertIn("Step 2", dialog.step.text())
            self.assertIn("Pocket TTS", plain_label_text(dialog.narrator_status))
            dialog.copy_narrator_details.click()
            self.assertIn("Model:", self.application.clipboard().text())
            self.assertNotIn("Model:", dialog.voice_configuration.text())
            self.assertIn(
                "Changing voices or model may require new recordings",
                dialog.voice_configuration.text(),
            )
            self.assertFalse(dialog.voice_panel.preview_service._closed)
            self.assertEqual(
                dialog.continue_button.text(), "Generate with these voices"
            )
            self.assertFalse(dialog.generation_runner.active)
            choices = {
                dialog.narrator_choice.itemData(index)
                for index in range(dialog.narrator_choice.count())
            }
            self.assertNotIn("character:centurion", choices)
            self.assertIn("preset:alba", choices)
            dialog.pocket_voice_cloning.setChecked(True)
            self.assertEqual(dialog.continue_button.text(), "Update voice routes")
            choices = {
                dialog.narrator_choice.itemData(index)
                for index in range(dialog.narrator_choice.count())
            }
            self.assertIn("character:centurion", choices)
            self.assertIn("character:rhiannon", choices)
            self.assertIn(
                "Rhiannon",
                " ".join(
                    dialog.voice_routes.item(index).text()
                    for index in range(dialog.voice_routes.count())
                ),
            )

            dialog.narrator_choice.setCurrentIndex(
                dialog.narrator_choice.findData("character:centurion")
            )
            self.assertIn(
                "Narrator: Centurion", plain_label_text(dialog.narrator_status)
            )
            dialog.continue_button.click()
            self.assertTrue(pool.tasks)
            pool.tasks.pop(0).run()
            self.application.processEvents()
            pool.tasks.pop(0).run()
            self.application.processEvents()

            self.assertTrue(dialog._awaiting_voice_confirmation)
            self.assertEqual(
                dialog.narrator_choice.currentData(), "character:centurion"
            )
            narrator_groups = tuple(
                group
                for group in dialog._voice_plan.groups
                if group.route == "narrator"
            )
            self.assertTrue(narrator_groups)
            self.assertTrue(
                all(group.source_character == "Centurion" for group in narrator_groups)
            )
            self.assertFalse(dialog.input_runner.active)
            from vntts.voices import CharacterVoiceRegistry

            generation_input = dialog.input_store.materialize(
                dialog.job(), dialog.voice_plan()
            )
            narrator = CharacterVoiceRegistry.from_file(
                generation_input.voice_manifest
            ).resolve("Narrator")
            self.assertEqual(narrator.source_character, "Centurion")
            dialog.reject()

    def test_moss_confirmation_ignores_pocket_permission_and_stops_preview(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_content(root / "content"))
            manifest = write_manifest(root / "voices")
            for name in ("rhiannon", "centurion", "unrelated"):
                sf.write(
                    manifest.parent / "references" / f"{name}.wav",
                    np.zeros(2400),
                    24_000,
                    subtype="PCM_16",
                )
            pool = ManualThreadPool()
            player = Mock()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(
                    speech_backend="moss-tts",
                    voice_manifest=str(manifest),
                    pocket_gated_model_accepted=True,
                    voice_assignments={"Narrator": "character:centurion"},
                ),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                thread_pool=pool,
                preview_player=player,
            )
            dialog.select_all_button.click()
            dialog.continue_button.click()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertTrue(
                dialog._awaiting_voice_confirmation, dialog.resume_status.text()
            )
            self.assertFalse(dialog.voice_panel.preview_service._closed)
            player.reset_mock()
            dialog.continue_button.click()
            self.assertTrue(dialog.generating)
            self.assertFalse(dialog.planning_voices)
            player.stop.assert_called()
            self.assertTrue(dialog.voice_panel.preview_service._closed)
            dialog.reject()
            player.reset_mock()
            dialog.reject()
            player.stop.assert_called()

    def test_broken_manifest_on_cloning_toggle_returns_to_retry(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_content(root / "content"))
            manifest = write_manifest(root / "voices")
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(voice_manifest=str(manifest)),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                thread_pool=pool,
            )
            dialog.select_all_button.click()
            dialog.continue_button.click()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            manifest.write_text("broken", encoding="utf-8")
            dialog.pocket_voice_cloning.setChecked(True)
            self.assertFalse(dialog._awaiting_voice_confirmation)
            self.assertTrue(dialog.selection_panel.isVisibleTo(dialog))
            self.assertFalse(dialog.narrator_choice.signalsBlocked())
            self.assertIn("Unable to show", dialog.selection_status.text())
            self.assertFalse(pool.tasks)
            dialog.reject()

    def test_failed_candidate_import_does_not_silently_assign_narrator(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_content(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            importer = Mock()
            importer.prepare_voice_candidates.side_effect = GameContentImportError(
                "Voice catalog is broken"
            )
            importer.availability.return_value = Mock(
                available=True, message="Available"
            )
            plans = Mock()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                importer=importer,
                voice_plan_store=plans,
            )
            self.addCleanup(dialog.reject)
            job = jobs.create_or_resume(content, ("story",))
            with patch(
                "vntts.pregeneration_ui.find_default_voice_manifest", return_value=None
            ):
                with self.assertRaisesRegex(
                    GameContentImportError, "Voice catalog is broken"
                ):
                    dialog._create_voice_plan(job)
            plans.create.assert_not_called()

    def test_prepared_candidates_are_reused_only_for_the_same_selection(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            importer = Mock()
            importer.availability.return_value = Mock(
                available=True, message="Available"
            )
            importer.prepare_voice_candidates.side_effect = (
                root / "first.json",
                root / "second.json",
            )
            plans = Mock()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                importer=importer,
                voice_plan_store=plans,
            )
            first = jobs.create_or_resume(content, ("main-1",))
            second = jobs.create_or_resume(content, ("rhiannon",))
            with patch(
                "vntts.pregeneration_ui.find_default_voice_manifest", return_value=None
            ):
                dialog._create_voice_plan(first)
                dialog._create_voice_plan(first)
                dialog._create_voice_plan(second)
            self.assertEqual(importer.prepare_voice_candidates.call_count, 2)
            self.assertEqual(
                plans.create.call_args.kwargs["manifest_path"], root / "second.json"
            )
            dialog.reject()

    def test_zero_ambiguity_story_reaches_an_active_portable_pack(self):
        with (
            TemporaryDirectory() as temporary_directory,
            patch(
                "vntts.settings.get_settings_path",
                return_value=Path(temporary_directory) / "settings.json",
            ),
        ):
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            decisions = VoiceDecisionStore(root / "voice-decisions.json")
            voices = VoicePlanStore(jobs, decisions=decisions)
            inputs = PregenerationInputStore(jobs)
            generator = InProcessPocketGenerator()
            recovery = OfflineRecoveryWorker(generator)
            acceptance = OfflineAcceptanceWorker(generator)
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                voice_plan_store=voices,
                input_store=inputs,
                generator=generator,
                recovery=recovery,
                acceptance=acceptance,
                thread_pool=pool,
            )
            visible_text = [dialog.summary.text(), dialog.resume_status.text()]

            dialog.select_all_button.click()
            dialog.continue_button.click()
            for _step in range(12):
                if dialog.pack_result() is not None:
                    break
                if dialog._awaiting_voice_confirmation:
                    self.assertEqual(
                        dialog.continue_button.text(), "Generate with these voices"
                    )
                    dialog.continue_button.click()
                self.assertTrue(
                    pool.tasks,
                    f"step {_step}: {dialog.resume_status.text()}",
                )
                pool.tasks.pop(0).run()
                self.application.processEvents()
                visible_text.extend(
                    (
                        dialog.summary.text(),
                        dialog.resume_status.text(),
                        dialog.cancel_button.text(),
                    )
                )

            self.assertEqual(
                dialog.progress_phase.text(),
                "Ready with live speech for remaining lines",
            )
            self.assertTrue(dialog.pocket_terms.isHidden())
            self.assertEqual(dialog.continue_button.text(), "Use prepared audio")
            self.assertEqual(dialog.progress_bar.maximum(), 1)
            self.assertEqual(dialog.progress_bar.value(), 1)
            self.assertEqual(dialog.progress_bar.format(), "Audio saved")
            dialog.continue_button.click()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertTrue(generator.rendered)
            self.assertEqual(dialog.voice_plan().audition_count, 0)
            self.assertEqual(dialog.recovery_result().live_fallbacks, 1)
            self.assertEqual(dialog.acceptance_result().approved, 1)
            self.assertEqual(dialog.pack_result().approved, 1)
            self.assertEqual(dialog.pack_result().live_fallbacks, 1)
            player_copy = " ".join(visible_text).casefold()
            for authoring_term in (
                "workspace",
                "queue id",
                "manifest",
                "checksum",
                "seed",
                "per-line review",
            ):
                self.assertNotIn(authoring_term, player_copy)

            saved_settings = root / "settings.json"
            controller = Mock(is_ready=False)
            controller.apply_settings.return_value = True
            tray = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(return_value=controller),
                pregeneration_activator=OfflinePackActivator(
                    save_settings=lambda settings: settings.save(saved_settings)
                ),
            )
            with patch(
                "vntts.app.OfflineAudioPreparationDialog",
                return_value=dialog,
            ):
                self.assertIs(tray.open_pregeneration(), dialog)
                self.assertFalse(dialog.isWindow())
                tray.dashboard.show_reading()
                self.assertIs(tray.open_pregeneration(), dialog)
                dialog.automatic_activation = True
                tray._remember_preparation_context()
                dialog._show_final_handoff(dialog.pack_result())
                self.assertIsNone(tray.pregeneration_dialog)
            for _attempt in range(400):
                self.application.processEvents()
                if not tray.pregeneration_activation_runner.active:
                    break
                QTest.qWait(5)

            self.assertFalse(tray.pregeneration_activation_runner.active)
            self.assertEqual(tray.settings.audio_source_policy, "prefer-game-audio")
            self.assertEqual(
                tray.settings.game_pack, str(dialog.pack_result().manifest)
            )
            self.assertTrue(saved_settings.is_file())
            self.assertIn("Prepared audio is active", tray.dashboard.status.text())
            self.assertIn("click Start reading", tray.dashboard.status.text())
            self.assertTrue(tray.dashboard.isVisible())
            controller.start.assert_not_called()
            tray.shutdown()

    def test_ambiguous_voice_choice_resumes_then_completes_without_line_review(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_content(root / "content"))
            manifest = write_conflicting_manifest(root / "voices")
            for reference in (manifest.parent / "references").glob("*.wav"):
                sf.write(
                    reference,
                    np.zeros(1_600, dtype=np.float32),
                    16_000,
                    subtype="PCM_16",
                )
            settings = AppSettings(
                voice_manifest=str(manifest),
                pocket_gated_model_accepted=True,
            )
            jobs = PregenerationJobStore(root / "jobs")
            decisions = VoiceDecisionStore(root / "voice-decisions.json")
            voices = VoicePlanStore(jobs, decisions=decisions)
            pool = ManualThreadPool()
            cancelled_preview = Mock()
            cancelled_preview.generate.side_effect = VoiceAuditionCancelled("cancelled")
            first = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                voice_plan_store=voices,
                voice_decisions=decisions,
                audition_service=cancelled_preview,
                preview_player=Mock(),
                thread_pool=pool,
            )

            first.select_all_button.click()
            first.continue_button.click()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertTrue(first.auditioning_voices)
            interrupted_job_id = first.job().job_id
            first.voice_panel.a_play.click()
            first.cancel_button.click()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertEqual(first.result(), QDialog.DialogCode.Rejected)

            preview = Mock()
            preview.generate.side_effect = lambda _plan, _group, source_id, **_options: (
                Mock(path=root / f"{source_id.removeprefix('character:')}.wav")
            )
            generator = InProcessPocketGenerator()
            second = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                voice_plan_store=voices,
                voice_decisions=decisions,
                audition_service=preview,
                preview_player=Mock(),
                input_store=PregenerationInputStore(jobs),
                generator=generator,
                recovery=OfflineRecoveryWorker(generator),
                acceptance=OfflineAcceptanceWorker(generator),
                thread_pool=pool,
            )

            second.continue_button.click()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertEqual(second.job().job_id, interrupted_job_id)
            self.assertTrue(second.auditioning_voices)
            second.voice_panel.a_play.click()
            pool.tasks.pop(0).run()
            self.application.processEvents()
            self.assertTrue(second.voice_panel.a_use.isEnabled())
            second.voice_panel.a_use.click()
            for _step in range(8):
                if second.pack_result() is not None:
                    break
                if second._awaiting_voice_confirmation:
                    second.continue_button.click()
                self.assertTrue(
                    pool.tasks,
                    f"step {_step}: {second.resume_status.text()}",
                )
                pool.tasks.pop(0).run()
                self.application.processEvents()

            self.assertEqual(
                second.progress_phase.text(),
                "Ready with live speech for remaining lines",
            )
            second.continue_button.click()
            self.assertEqual(second.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(second.voice_plan().audition_count, 0)
            self.assertTrue(generator.rendered)
            self.assertTrue(decisions.path.is_file())
            first.deleteLater()
            second.deleteLater()

    def test_process_restart_resumes_only_the_cancelled_line(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            settings = AppSettings(
                speech_backend="pocket-tts",
                tts_profile="default",
            )
            jobs = PregenerationJobStore(root / "jobs")
            decisions = VoiceDecisionStore(root / "voice-decisions.json")
            voices = VoicePlanStore(jobs, decisions=decisions)
            inputs = PregenerationInputStore(jobs)
            pool = ManualThreadPool()
            interrupted = InterruptingPocketGenerator(interrupt=True)
            first = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                voice_plan_store=voices,
                input_store=inputs,
                generator=interrupted,
                thread_pool=pool,
            )

            first.select_all_button.click()
            first.continue_button.click()
            for _step in range(8):
                if first.progress_phase.text() == "Generation paused":
                    break
                if first._awaiting_voice_confirmation:
                    first.continue_button.click()
                pool.tasks.pop(0).run()
                self.application.processEvents()
            interrupted_job_id = first.job().job_id
            interrupted_input_id = first.generation_input().identity
            self.assertIn("Generation cancelled", first.resume_status.text())
            self.assertEqual(first.progress_phase.text(), "Generation paused")
            self.assertIn(
                "last available progress", plain_label_text(first.progress_timing)
            )
            self.assertIn(
                "generate only unfinished lines",
                first.progress_cancel_consequence.text(),
            )
            self.assertEqual(len(interrupted.rendered_texts), 2)
            first.reject()
            first.deleteLater()

            resumed = InterruptingPocketGenerator(interrupt=False)
            second = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                voice_plan_store=voices,
                input_store=inputs,
                generator=resumed,
                recovery=OfflineRecoveryWorker(resumed),
                acceptance=OfflineAcceptanceWorker(resumed),
                thread_pool=pool,
            )
            second.continue_button.click()
            for _step in range(8):
                if second.pack_result() is not None:
                    break
                if second._awaiting_voice_confirmation:
                    second.continue_button.click()
                self.assertTrue(
                    pool.tasks,
                    f"step {_step}: {second.resume_status.text()}",
                )
                pool.tasks.pop(0).run()
                self.application.processEvents()

            self.assertEqual(second.progress_phase.text(), "Offline audio is ready")
            second.continue_button.click()
            self.assertEqual(second.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(second.job().job_id, interrupted_job_id)
            self.assertEqual(second.generation_input().identity, interrupted_input_id)
            self.assertEqual(resumed.rendered_texts, interrupted.rendered_texts[-1:])
            self.assertNotIn(interrupted.rendered_texts[0], resumed.rendered_texts)
            self.assertEqual(second.pack_result().approved, 2)
            self.assertEqual(second.pack_result().live_fallbacks, 0)
            second.deleteLater()


if __name__ == "__main__":
    unittest.main()
