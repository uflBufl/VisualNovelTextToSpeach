import json
import os
import unittest
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from tests.test_game_pack import write_synthetic_game_pack  # noqa: E402
from tests.test_pregeneration_audition import FakeBackend  # noqa: E402
from tests.test_pregeneration_setup import ManualThreadPool  # noqa: E402
from tests.test_pregeneration_voices import (  # noqa: E402
    write_content,
    write_manifest,
    write_player_candidate_manifest,
)
from vntts.app import TrayApplication  # noqa: E402
from vntts.configuration_apply import ConfigurationApplyMixin  # noqa: E402
from vntts.game_audio_decoder import DecoderSetupRequired  # noqa: E402
from vntts.game_content_importer import Reverse1999GameImporter  # noqa: E402
from vntts.game_narrator import bind_game_narrator, narrator_preview_plan  # noqa: E402
from vntts.game_narrator_ui import GameNarratorDialog  # noqa: E402
from vntts.game_pack import apply_game_pack  # noqa: E402
from vntts.pregeneration_audition import VoiceAuditionPreviewService  # noqa: E402
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    inspect_story_index,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.pregeneration_voices import VoicePlanStore  # noqa: E402
from vntts.runtime_config import initialize_voice_registry  # noqa: E402
from vntts.settings import AppSettings, load_app_settings  # noqa: E402
from vntts.speech_presentation import narrator_voice_label  # noqa: E402


class GameNarratorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def run_task(self, pool):
        pool.tasks.pop(0).run()
        self.application.processEvents()

    def test_builtin_preview_and_save_need_no_game_or_gated_model(self):
        with TemporaryDirectory() as directory:
            pool = ManualThreadPool()
            importer = Mock()
            backend = FakeBackend("pocket-tts")
            factory = Mock(return_value=backend)
            previews = VoiceAuditionPreviewService(directory, backend_factory=factory)
            settings = AppSettings(
                pocket_gated_model_accepted=True,
                voice_assignments={
                    "Narrator": "preset:alba",
                    "Rhiannon": "character:rhiannon",
                },
            )
            dialog = GameNarratorDialog(
                settings,
                importer=importer,
                preview_service=previews,
                thread_pool=pool,
                player=Mock(),
            )
            self.application.processEvents()
            self.assertEqual(pool.tasks, [])
            self.assertTrue(dialog.game_controls.isHidden())
            self.assertFalse(dialog.original_button.isEnabled())
            dialog.presets.setCurrentIndex(dialog.presets.findData("preset:marius"))
            dialog.preview_button.click()
            self.assertFalse(dialog.save_button.isEnabled())
            self.run_task(pool)
            self.assertEqual(backend.requests[0].voice, "marius")
            self.assertFalse(factory.call_args.kwargs["allow_gated_model_access"])
            self.assertIsNone(dialog.result_settings)
            dialog.save_button.click()
            self.run_task(pool)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(
                dialog.result_settings.voice_assignments,
                {"Narrator": "preset:marius", "Rhiannon": "character:rhiannon"},
            )
            importer.narrator_characters.assert_not_called()
            self.assertEqual(backend.shutdown_count, 1)

    def test_cancel_builtin_candidate_preserves_saved_narrator(self):
        pool = ManualThreadPool()
        original = AppSettings(voice_assignments={"Narrator": "preset:alba"})
        dialog = GameNarratorDialog(
            original,
            importer=Mock(),
            preview_service=Mock(),
            thread_pool=pool,
            player=Mock(),
        )
        self.application.processEvents()
        dialog.presets.setCurrentIndex(dialog.presets.findData("preset:marius"))
        dialog.reject()
        self.run_task(pool)
        self.assertIsNone(dialog.result_settings)
        self.assertEqual(original.voice_assignments, {"Narrator": "preset:alba"})

    def test_discovery_reuses_import_and_manual_folder_reimports(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            importer = Reverse1999GameImporter(output_root=root)
            # An older import already has story audio but no playable narrator index.
            write_content(root / "reverse1999")
            (root / "reverse1999" / "english-bank-index.json").touch()

            def importing_game(*args):
                write_content(root / "reverse1999")
                (root / "audio").mkdir(exist_ok=True)
                (root / "reverse1999" / "english-bank-index.json").write_text(
                    json.dumps(
                        {
                            "version": 4,
                            "game_audio_directory": str(root / "audio"),
                            "banks": [],
                        }
                    )
                )
                (root / "reverse1999" / "narrator-banks.json").write_text(
                    '{"Centurion": "hero3032_mainstory.bnk"}'
                )
                story = root / "reverse1999" / "story-index.jsonl"
                (story.parent / "narrator-index.jsonl").write_text(
                    story.read_text().replace("Rhiannon", "Centurion")
                )

            with patch.object(
                importer,
                "import_installed",
                side_effect=importing_game,
            ) as importing:
                self.assertEqual(importer.narrator_characters(), ("Centurion",))
                self.assertEqual(importer.narrator_characters(), ("Centurion",))
                self.assertEqual(importing.call_count, 1)
                importer.narrator_characters(installation_root=root / "game")
                self.assertEqual(importing.call_count, 2)
                self.assertEqual(importing.call_args.args[1], root / "game")

    def test_saved_pack_does_not_replace_explicit_game_narrator_on_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_root = root / "pack"
            pack_root.mkdir()
            pack, *_ = write_synthetic_game_pack(pack_root)
            original = apply_game_pack(AppSettings(), pack)
            manifest = write_manifest(root / "candidates")
            candidate = bind_game_narrator(
                original,
                manifest,
                "character:centurion",
                "Centurion",
                root=root / "saved",
            )
            loaded = load_app_settings(candidate.save(root / "settings.json"))
            self.assertEqual(narrator_voice_label(candidate), "Centurion")
            self.assertEqual(narrator_voice_label(loaded), "Centurion")
            self.assertEqual(loaded.voice_manifest, candidate.voice_manifest)
            self.assertEqual(
                loaded.generated_audio_manifest, original.generated_audio_manifest
            )
            self.assertEqual(
                initialize_voice_registry(loaded).resolve("Narrator").source_character,
                "Centurion",
            )
            self.assertEqual(
                apply_game_pack(loaded, pack).voice_manifest, original.voice_manifest
            )

    def test_main_and_preparation_persist_only_an_accepted_selection(self):
        for decision in ("save", "cancel", "save-failure"):
            with self.subTest(decision=decision), TemporaryDirectory() as directory:
                root = Path(directory)
                original = AppSettings()
                pool = ManualThreadPool()
                controller = Mock(is_ready=False, is_live_running=False)
                tray = TrayApplication(
                    self.application,
                    original,
                    controller_factory=Mock(return_value=controller),
                )
                preparation = OfflineAudioPreparationDialog(
                    original,
                    discovery=lambda: ContentDiscovery(()),
                    job_store=PregenerationJobStore(root / "jobs"),
                    thread_pool=pool,
                    game_narrator_chooser=tray._open_preparation_narrator,
                )
                tray.pregeneration_dialog = preparation
                tray.dashboard.embed_preparation(preparation)
                picker = GameNarratorDialog(
                    original,
                    importer=Mock(),
                    preview_service=Mock(),
                    thread_pool=pool,
                    player=Mock(),
                )
                with (
                    patch("vntts.app.GameNarratorDialog", return_value=picker),
                    patch.object(
                        AppSettings,
                        "save",
                        return_value=root / "settings.json",
                        side_effect=OSError("disk full")
                        if decision == "save-failure"
                        else None,
                    ) as save,
                    patch.object(tray, "_reload_game_narrator") as reload,
                    patch.object(tray, "_sync_active_profile"),
                ):
                    preparation.game_narrator_button.click()
                    self.application.processEvents()
                    self.assertFalse(picker.isWindow())
                    self.assertFalse(preparation.isEnabled())
                    picker.presets.setCurrentIndex(
                        picker.presets.findData("preset:marius")
                    )
                    tray.dashboard.show_stories()
                    tray.open_voice_previews()
                    self.assertIs(tray.narrator_dialog, picker)
                    self.assertEqual(picker.presets.currentData(), "preset:marius")
                    tray.read_once()
                    tray.toggle_live()
                    controller.read_once.assert_not_called()
                    controller.toggle_live.assert_not_called()
                    if decision == "cancel":
                        picker.cancel_button.click()
                    else:
                        picker.save_button.click()
                    self.run_task(pool)
                    self.assertIsNone(tray.narrator_dialog)
                    self.assertTrue(preparation.isEnabled())
                    if decision == "save":
                        save.assert_called_once()
                        self.assertEqual(
                            tray.settings.voice_assignments["Narrator"], "preset:marius"
                        )
                        self.assertEqual(preparation.settings, tray.settings)
                        self.assertIn("Marius", preparation.narrator_status.text())
                        reload.assert_called_once()
                    else:
                        self.assertEqual(tray.settings, original)
                        self.assertEqual(preparation.settings, original)
                        reload.assert_not_called()
                tray.shutdown()

    def test_embedded_preview_quit_waits_for_cancellation(self):
        pool = ManualThreadPool()
        previews = Mock()
        previews.generate.return_value.path = Path("unused.wav")
        picker = GameNarratorDialog(
            AppSettings(),
            importer=Mock(),
            preview_service=previews,
            thread_pool=pool,
            player=Mock(),
        )
        tray = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(
                return_value=Mock(is_ready=False, is_live_running=False)
            ),
        )
        with (
            patch("vntts.app.GameNarratorDialog", return_value=picker),
            patch.object(self.application, "quit") as quit_app,
        ):
            tray.open_voice_previews()
            self.application.processEvents()
            picker.preview_button.click()
            tray.dashboard.show_reading()
            self.assertFalse(tray.dashboard.live_button.isEnabled())
            self.assertFalse(tray.dashboard.voice_edit_status.isHidden())
            tray.dashboard.close()
            self.application.processEvents()
            self.assertTrue(tray.dashboard.isVisible())
            quit_app.assert_not_called()
            self.assertTrue(picker.cancellation.is_set())
            self.run_task(pool)
            quit_app.assert_not_called()
            self.run_task(pool)
            quit_app.assert_called_once()
            self.assertIsNone(picker.result_settings)
            picker.player.play.assert_not_called()
        tray.shutdown()

    def test_narrator_reload_stops_old_worker_and_honors_cancellation(self):
        shell = Mock()
        shell._lifecycle_is_current.return_value = True
        shell.controller.start.return_value = True
        candidate = AppSettings()
        event = Event()
        self.assertEqual(
            ConfigurationApplyMixin._apply_configuration(
                shell, candidate, 1, event, True
            ),
            (True, True),
        )
        self.assertEqual(
            [call[0] for call in shell.controller.mock_calls],
            ["shutdown", "apply_settings", "prepare_startup", "start"],
        )
        shell.controller.reset_mock()
        event.set()
        self.assertEqual(
            ConfigurationApplyMixin._apply_configuration(
                shell, candidate, 1, event, True
            ),
            (False, False),
        )
        shell.controller.start.assert_not_called()

    def test_binding_preserves_existing_routes_and_original_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(root / "existing")
            before = manifest.read_bytes()
            settings = AppSettings(
                voice_manifest=str(manifest),
                pocket_gated_model_accepted=True,
                voice_assignments={"Aderyn": "character:rhiannon"},
            )
            candidate = bind_game_narrator(
                settings,
                manifest,
                "character:centurion",
                "Centurion",
                root=root / "saved",
            )
            registry = initialize_voice_registry(candidate)
            self.assertEqual(registry.resolve("Narrator").source_character, "Centurion")
            self.assertEqual(registry.resolve("Aderyn").character, "Rhiannon")
            self.assertEqual(manifest.read_bytes(), before)
            self.assertEqual(
                candidate.voice_assignments["Aderyn"], "character:rhiannon"
            )
            saved = candidate.save(root / "settings.json")
            loaded = load_app_settings(saved)
            self.assertEqual(
                initialize_voice_registry(loaded)
                .resolve("Narrator")
                .reference.read_bytes(),
                b"centurion",
            )
            again = bind_game_narrator(
                settings,
                manifest,
                "character:centurion",
                "Centurion",
                root=root / "saved",
            )
            self.assertEqual(again.voice_manifest, candidate.voice_manifest)

    def test_fresh_narrator_keeps_story_character_candidates(self):
        with (
            TemporaryDirectory() as directory,
            patch("vntts.game_narrator.find_default_voice_manifest", return_value=None),
        ):
            root = Path(directory)
            manifest = write_manifest(root / "candidates")
            settings = AppSettings(pocket_gated_model_accepted=True)
            narrator = bind_game_narrator(
                settings,
                manifest,
                "character:centurion",
                "Centurion",
                root=root / "saved",
            )
            combined = bind_game_narrator(
                narrator,
                narrator.voice_manifest,
                narrator.voice_assignments["Narrator"],
                "Centurion",
                additional_manifest=manifest,
                root=root / "saved",
            )
            content = inspect_story_index(write_content(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(content, ["story"])
            plan = VoicePlanStore(jobs).create(job, combined)
            self.assertTrue(
                any(group.source_character == "Rhiannon" for group in plan.groups)
            )
            self.assertEqual(
                initialize_voice_registry(combined)
                .resolve("Narrator")
                .source_character,
                "Centurion",
            )

    def test_preparation_extracts_story_voices_after_fresh_narrator_selection(self):
        with (
            TemporaryDirectory() as directory,
            patch("vntts.game_narrator.find_default_voice_manifest", return_value=None),
        ):
            root = Path(directory)
            content = inspect_story_index(write_content(root / "content"))
            manifest = write_manifest(root / "narrators")
            selected = bind_game_narrator(
                AppSettings(pocket_gated_model_accepted=True),
                manifest,
                "character:centurion",
                "Centurion",
                root=root / "saved",
            )
            candidates = write_player_candidate_manifest(
                root / "story-candidates", content.story_index_sha256
            )
            importer = Mock()
            importer.prepare_voice_candidates.return_value = candidates
            importer.availability.return_value = Mock(
                available=True, message="Available"
            )
            jobs = PregenerationJobStore(root / "jobs")
            dialog = OfflineAudioPreparationDialog(
                selected,
                importer=importer,
                job_store=jobs,
                discovery=lambda: ContentDiscovery((content,)),
                thread_pool=ManualThreadPool(),
            )
            job = jobs.create_or_resume(content, ["story"])
            with patch(
                "vntts.game_narrator.get_local_data_directory",
                return_value=root / "local",
            ):
                plan = dialog._create_voice_plan(job)
            importer.prepare_voice_candidates.assert_called_once()
            self.assertTrue(any(group.candidates for group in plan.groups))
            narrator = initialize_voice_registry(selected).resolve("Narrator")
            self.assertEqual(narrator.source_character, "Centurion")
            self.assertNotEqual(plan.voice_manifest, str(candidates))
            dialog.reject()

    def test_preview_plan_binds_exact_reference_and_engine(self):
        with TemporaryDirectory() as directory:
            manifest = write_manifest(Path(directory))
            settings = AppSettings(speech_backend="moss-tts", tts_profile="natural")
            plan = narrator_preview_plan(
                settings, manifest, "character:centurion", "Hello."
            )
            self.assertEqual(plan.synthesis_backend, "moss-tts")
            self.assertEqual(plan.synthesis_profile, "natural")
            self.assertEqual(plan.groups[0].candidates[0].source_character, "Centurion")

    def test_decoder_setup_consent_retries_in_worker_and_keeps_controls_gated(self):
        with TemporaryDirectory() as directory:
            manifest = write_manifest(Path(directory) / "candidates")
            importer = Mock()
            importer.narrator_characters.return_value = ("Centurion",)
            importer.prepare_voice_roles.side_effect = [
                DecoderSetupRequired("Install decoder?"),
                manifest,
            ]
            pool = ManualThreadPool()
            dialog = GameNarratorDialog(
                AppSettings(voice_assignments={"Narrator": "character:centurion"}),
                importer=importer,
                preview_service=Mock(),
                thread_pool=pool,
                player=Mock(),
            )
            self.application.processEvents()
            self.run_task(pool)
            dialog.prepare_button.click()
            with patch(
                "vntts.game_narrator_ui.confirm_decoder_setup", return_value=True
            ) as consent:
                self.run_task(pool)
            consent.assert_called_once()
            self.assertTrue(importer.allow_decoder_homebrew)
            self.assertFalse(dialog.controls.isEnabled())
            self.assertTrue(dialog.runner.active)
            self.run_task(pool)
            self.assertTrue(dialog.controls.isEnabled())
            self.assertGreater(dialog.references.count(), 0)
            dialog.reject()
            self.run_task(pool)

    def test_guided_flow_gates_controls_previews_and_saves(self):
        with (
            TemporaryDirectory() as directory,
            patch("vntts.game_narrator.find_default_voice_manifest", return_value=None),
        ):
            root = Path(directory)
            manifest = write_manifest(root / "candidates")
            importer = Mock()
            importer.narrator_characters.return_value = ("Centurion",)
            importer.prepare_voice_roles.return_value = manifest
            previews = Mock()
            previews.reference_audio.return_value = (
                manifest.parent / "references/centurion.wav"
            )
            previews.generate.return_value.path = root / "preview.wav"
            pool = ManualThreadPool()
            player = Mock()
            dialog = GameNarratorDialog(
                AppSettings(voice_assignments={"Narrator": "character:centurion"}),
                importer=importer,
                preview_service=previews,
                thread_pool=pool,
                player=player,
                binder=partial(bind_game_narrator, root=root / "saved"),
            )
            self.application.processEvents()
            self.assertFalse(dialog.controls.isEnabled())
            self.assertFalse(dialog.progress.isHidden())
            self.run_task(pool)
            dialog.prepare_button.click()
            self.assertFalse(dialog.controls.isEnabled())
            dialog.decoderProgress.emit("Downloading game-audio decoder: 1.0 MB...")
            self.assertIn("Downloading", dialog.status.text())
            self.run_task(pool)
            importer.prepare_voice_roles.assert_called_once_with(
                ("Centurion",),
                dialog.cancellation,
                progress=dialog.decoderProgress.emit,
                narrator=True,
            )
            dialog.references.setCurrentIndex(
                dialog.references.findData("character:centurion")
            )
            self.assertFalse(dialog.preview_button.isEnabled())
            self.assertFalse(dialog.save_button.isEnabled())
            dialog.original_button.click()
            self.run_task(pool)
            player.play.assert_called_once()
            dialog.consent.setChecked(True)
            dialog.preview_button.click()
            self.run_task(pool)
            plan = previews.generate.call_args.args[0]
            self.assertTrue(plan.pocket_voice_cloning)
            self.assertEqual(plan.groups[0].source_id, "character:centurion")
            dialog.save_button.click()
            self.run_task(pool)
            self.run_task(pool)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(
                initialize_voice_registry(dialog.result_settings)
                .resolve("Narrator")
                .source_character,
                "Centurion",
            )
            previews.close.assert_called_once()

    def test_moss_picker_keeps_engine_without_cpp_or_apple_silicon(self):
        with (
            TemporaryDirectory() as directory,
            patch("platform.machine", return_value="AMD64"),
            patch.dict(
                os.environ, {"VNTTS_MOSS_CPP_EXECUTABLE": "", "VNTTS_MOSS_GGUF": ""}
            ),
        ):
            manifest = write_manifest(Path(directory))
            importer = Mock()
            importer.narrator_characters.return_value = ("Centurion",)
            importer.prepare_voice_roles.return_value = manifest
            previews = Mock()
            previews.generate.side_effect = RuntimeError("MOSS runtime unavailable")
            pool = ManualThreadPool()
            settings = AppSettings(
                speech_backend="moss-tts", tts_model="local-moss", tts_profile="natural"
            )
            dialog = GameNarratorDialog(
                settings,
                importer=importer,
                preview_service=previews,
                thread_pool=pool,
                player=Mock(),
            )
            self.application.processEvents()
            self.run_task(pool)
            dialog.prepare_button.click()
            self.run_task(pool)
            self.assertIn("MOSS", dialog.engine.text())
            self.assertTrue(dialog.terms.isHidden())
            self.assertTrue(dialog.consent.isHidden())
            self.assertTrue(dialog.preview_button.isEnabled())
            dialog.preview_button.click()
            self.run_task(pool)
            plan = previews.generate.call_args.args[0]
            self.assertEqual(plan.synthesis_backend, "moss-tts")
            self.assertEqual(plan.synthesis_model, "local-moss")
            self.assertEqual(plan.synthesis_profile, "natural")
            self.assertIn("MOSS runtime unavailable", dialog.status.text())
            self.assertEqual(dialog._settings().speech_backend, "moss-tts")
            self.assertEqual(previews.generate.call_count, 1)
            dialog.reject()
            self.run_task(pool)

    def test_cancel_discards_late_discovery_and_closes_worker(self):
        pool = ManualThreadPool()
        importer = Mock()
        importer.narrator_characters.return_value = ("Centurion",)
        previews = Mock()
        dialog = GameNarratorDialog(
            AppSettings(voice_assignments={"Narrator": "character:centurion"}),
            importer=importer,
            preview_service=previews,
            thread_pool=pool,
            player=Mock(),
        )
        self.application.processEvents()
        dialog.reject()
        self.assertTrue(dialog.cancellation.is_set())
        self.run_task(pool)
        self.run_task(pool)
        self.assertEqual(dialog.characters.count(), 0)
        self.assertIsNone(dialog.result_settings)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        previews.close.assert_called_once()

    def test_missing_game_restores_retry_controls(self):
        pool = ManualThreadPool()
        importer = Mock()
        importer.narrator_characters.side_effect = ValueError("Game not found")
        dialog = GameNarratorDialog(
            AppSettings(voice_assignments={"Narrator": "character:centurion"}),
            importer=importer,
            preview_service=Mock(),
            thread_pool=pool,
            player=Mock(),
        )
        self.application.processEvents()
        self.run_task(pool)
        self.assertIn("Game not found", dialog.status.text())
        self.assertTrue(dialog.folder_button.isEnabled())
        self.assertTrue(dialog.controls.isEnabled())
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.reject()
        self.run_task(pool)
