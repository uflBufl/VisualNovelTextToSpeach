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
from tests.test_pregeneration_setup import ManualThreadPool  # noqa: E402
from tests.test_pregeneration_voices import (  # noqa: E402
    write_content,
    write_manifest,
    write_player_candidate_manifest,
)
from vntts.app import TrayApplication  # noqa: E402
from vntts.configuration_apply import ConfigurationApplyMixin  # noqa: E402
from vntts.game_content_importer import Reverse1999GameImporter  # noqa: E402
from vntts.game_narrator import bind_game_narrator, narrator_preview_plan  # noqa: E402
from vntts.game_narrator_ui import GameNarratorDialog  # noqa: E402
from vntts.game_pack import apply_game_pack  # noqa: E402
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    inspect_story_index,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.pregeneration_voices import VoicePlanStore  # noqa: E402
from vntts.runtime_config import initialize_voice_registry  # noqa: E402
from vntts.settings import AppSettings, load_app_settings  # noqa: E402


class GameNarratorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def run_task(self, pool):
        pool.tasks.pop(0).run()
        self.application.processEvents()

    def test_discovery_reuses_import_and_manual_folder_reimports(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            importer = Reverse1999GameImporter(output_root=root)

            def importing_game(*args):
                write_content(root / "reverse1999")
                (root / "reverse1999" / "english-bank-index.json").touch()

            with patch.object(
                importer,
                "import_installed",
                side_effect=importing_game,
            ) as importing:
                self.assertEqual(importer.narrator_characters(), ("Rhiannon",))
                self.assertEqual(importer.narrator_characters(), ("Rhiannon",))
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
        candidate = AppSettings(pocket_gated_model_accepted=True)
        parent = Mock()
        shell = Mock(settings=AppSettings())
        shell._pick_game_narrator.return_value = candidate
        with patch.object(
            AppSettings, "save", return_value=Path("settings.json")
        ) as save:
            self.assertTrue(TrayApplication._choose_live_game_narrator(shell, parent))
            shell._reload_game_narrator.assert_called_once()
            self.assertEqual(shell.settings, candidate)
            selected = TrayApplication._choose_game_narrator_for_preparation(
                shell, candidate, parent
            )
            self.assertEqual(selected, candidate)
            self.assertTrue(shell._narrator_changed_in_preparation)
            self.assertEqual(save.call_count, 2)
            shell._pick_game_narrator.return_value = None
            self.assertFalse(TrayApplication._choose_live_game_narrator(shell, parent))
            self.assertEqual(save.call_count, 2)

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
                AppSettings(),
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
            self.run_task(pool)
            importer.prepare_voice_roles.assert_called_once_with(
                ("Centurion",), dialog.cancellation
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
            AppSettings(),
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
            AppSettings(),
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
