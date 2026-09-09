import json
import os
import unittest
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402
from vntts_artifacts.file_integrity import sha256_file  # noqa: E402

from tests.test_game_pack import write_synthetic_game_pack  # noqa: E402
from tests.test_pregeneration_audition import FakeBackend, clean_wav_bytes  # noqa: E402
from tests.test_pregeneration_setup import ManualThreadPool  # noqa: E402
from tests.test_pregeneration_voices import (  # noqa: E402
    write_content,
    write_manifest,
    write_player_candidate_manifest,
)
from tests.test_voice_default_impact import voice_impact_fixture  # noqa: E402
from vntts.app import TrayApplication  # noqa: E402
from vntts.configuration_apply import ConfigurationApplyMixin  # noqa: E402
from vntts.game_audio_decoder import DecoderSetupRequired  # noqa: E402
from vntts.game_content_importer import Reverse1999GameImporter  # noqa: E402
from vntts.game_narrator import bind_game_narrator, narrator_preview_plan  # noqa: E402
from vntts.game_narrator_ui import GameNarratorDialog  # noqa: E402
from vntts.game_pack import GamePackError, apply_game_pack  # noqa: E402
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
from vntts.speech_presentation import (  # noqa: E402
    engine_model_label,
    narrator_voice_label,
)


class GameNarratorTest(unittest.TestCase):
    def test_voice_impact_loads_stories_only_on_request_and_selects_without_generating(
        self,
    ):
        with TemporaryDirectory() as directory:
            content, jobs, decisions, settings, pack = voice_impact_fixture(
                Path(directory)
            )
            before = {
                path: path.read_bytes() for path in pack.iterdir() if path.is_file()
            }
            pool = ManualThreadPool()
            preparation = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                voice_decisions=decisions,
                thread_pool=pool,
            )
            controller = Mock(is_live_running=False)
            tray = TrayApplication(
                self.application,
                settings,
                controller_factory=Mock(return_value=controller),
            )
            dialog = GameNarratorDialog(
                settings, thread_pool=pool, player=Mock(), preview_service=Mock()
            )
            try:
                with (
                    patch(
                        "vntts.settings.get_settings_path",
                        return_value=Path(directory) / "settings.json",
                    ),
                    patch("vntts.app.GameNarratorDialog", return_value=dialog),
                    patch(
                        "vntts.app.OfflineAudioPreparationDialog",
                        return_value=preparation,
                    ) as create_preparation,
                    patch.object(tray, "_reload_game_narrator"),
                ):
                    tray.open_voice_previews()
                    self.application.processEvents()
                    create_preparation.assert_not_called()
                    dialog.role.setCurrentText("Rhiannon")
                    dialog.source.setCurrentIndex(dialog.source.findData("preset"))
                    dialog.presets.setCurrentIndex(
                        dialog.presets.findData("preset:marius")
                    )
                    dialog.check_impact.click()
                    create_preparation.assert_called_once()
                    self.run_task(pool)
                    self.assertEqual(tray.dashboard.sections.currentIndex(), 1)
                    self.assertIn(
                        "1 prepared lines in 1 stories", dialog.impact_status.text()
                    )
                    self.assertIn("Chapter 1: 1 changed", dialog.impact_status.text())
                    self.assertIsNone(dialog.result_settings)
                    dialog.consent.setChecked(not dialog.consent.isChecked())
                    self.assertIsNone(dialog._impact_results)
                    dialog.check_impact.click()
                    self.run_task(pool)
                    dialog.select_affected.click()
                    self.run_task(pool)
                    self.assertEqual(preparation.selected_story_ids(), ("chapter:1",))
                    self.assertEqual(
                        load_app_settings(
                            Path(directory) / "settings.json"
                        ).character_voice_defaults["Rhiannon"],
                        "preset:marius",
                    )
                    self.assertIsNone(preparation._generation_input)
                    self.assertEqual(tray.dashboard.sections.currentIndex(), 0)
                    self.assertEqual(
                        before,
                        {
                            path: path.read_bytes()
                            for path in pack.iterdir()
                            if path.is_file()
                        },
                    )
            finally:
                tray.shutdown()

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def run_task(self, pool):
        pool.tasks.pop(0).run()
        self.application.processEvents()

    def narrator_manifest(self, root):
        manifest = write_manifest(root, rhiannon=clean_wav_bytes())
        (root / "references" / "centurion.wav").write_bytes(
            clean_wav_bytes(amplitude=0.2)
        )
        return manifest

    def test_preview_compute_stays_visible_and_cached_playback_clears_generation(self):
        pool = ManualThreadPool()
        previews = Mock()
        previews.backend.runtime_status = "GPU: RTX 2070 SUPER <8 GB>; auxiliary: CPU"
        previews.generate.return_value = SimpleNamespace(
            path=Path("/tmp/preview.wav"), reused=True
        )
        dialog = GameNarratorDialog(
            AppSettings(voice_assignments={"Narrator": "preset:alba"}),
            preview_service=previews,
            thread_pool=pool,
            player=Mock(),
        )
        dialog.show()
        self.application.processEvents()
        dialog.preview_button.click()
        self.assertTrue(dialog.runtime.isVisibleTo(dialog))
        self.assertIn("GPU: RTX 2070 SUPER", dialog.runtime.text())
        self.assertEqual(dialog.runtime.textFormat(), Qt.TextFormat.PlainText)
        self.run_task(pool)
        self.assertIn("no generation", dialog.runtime.text())
        self.assertNotIn("GPU", dialog.runtime.text())
        dialog.preview_button.click()
        self.assertIn("GPU: RTX 2070 SUPER", dialog.runtime.text())
        self.run_task(pool)
        dialog.reject()
        self.run_task(pool)
        self.assertFalse(dialog.runtime_timer.isActive())

    def narrator_importer(self, manifest):
        document = json.loads(manifest.read_text())
        document["voices"] = [
            entry for entry in document["voices"] if entry["character"] == "Centurion"
        ]
        manifest.write_text(json.dumps(document))
        importer = Mock()
        importer.narrator_characters.return_value = ("Centurion",)
        importer.narrator_references.return_value = tuple(
            SimpleNamespace(
                line_id=f"playable-voice:3032:{index}",
                collection_title=f"Spoken line {index}",
                source_audio_id=index,
                text=f"Original transcript {index}. <Not markup.>",
            )
            for index in range(1, 6)
        )
        importer.prepare_voice_roles.return_value = manifest
        return importer

    def test_imported_catalog_preview_plays_and_reports_cached_reuse(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(root / "voices", rhiannon=clean_wav_bytes())
            backend = FakeBackend("pocket-tts")
            backend.runtime_status = "CPU test worker"
            previews = VoiceAuditionPreviewService(
                root / "previews", backend_factory=Mock(return_value=backend)
            )
            pool, player = ManualThreadPool(), Mock()
            settings = AppSettings(
                voice_manifest=str(manifest),
                pocket_gated_model_accepted=True,
                voice_assignments={"Narrator": "preset:alba"},
                character_voice_defaults={"Hotelier": "character:rhiannon"},
            )
            dialog = GameNarratorDialog(
                settings,
                importer=Mock(),
                preview_service=previews,
                thread_pool=pool,
                player=player,
            )
            self.application.processEvents()
            dialog.role.setCurrentText("Hotelier")
            self.assertEqual(dialog.source.currentData(), "catalog")
            try:
                dialog.preview_button.click()
                self.run_task(pool)
                self.assertIn("Playing generated preview", dialog.status.text())
                self.assertIn("CPU test worker", dialog.runtime.text())
                source = player.setSource.call_args.args[0]
                self.assertTrue(Path(source.toLocalFile()).is_file())
                self.assertEqual(len(backend.requests), 1)

                dialog.preview_button.click()
                self.run_task(pool)
                self.assertIn("Playing saved preview", dialog.status.text())
                self.assertIn("no generation", dialog.runtime.text())
                self.assertNotIn("CPU test worker", dialog.runtime.text())
                self.assertEqual(player.setSource.call_args.args[0], source)
                self.assertEqual(player.play.call_count, 2)
                self.assertEqual(len(backend.requests), 1)
                self.assertIsNone(dialog.result_settings)
            finally:
                dialog.reject()
                self.run_task(pool)
            self.assertFalse(dialog.runtime_timer.isActive())

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

    def test_character_preset_save_and_cancel_keep_other_roles_unchanged(self):
        for save in (False, True):
            with self.subTest(save=save):
                pool, importer, previews = ManualThreadPool(), Mock(), Mock()
                original = AppSettings(
                    voice_assignments={
                        "Narrator": "preset:alba",
                        "HOTELIER": "preset:anna",
                        "Other": "preset:anna",
                    },
                    character_voice_defaults={
                        "Hotelier": "default",
                        "Ada": "preset:anna",
                    },
                )
                dialog = GameNarratorDialog(
                    original,
                    importer=importer,
                    preview_service=previews,
                    thread_pool=pool,
                    player=Mock(),
                )
                self.application.processEvents()
                dialog.role.setCurrentText("Hotelier")
                dialog.source.setCurrentIndex(dialog.source.findData("preset"))
                dialog.presets.setCurrentIndex(dialog.presets.findData("preset:marius"))
                dialog.announcements.setCurrentIndex(
                    dialog.announcements.findData("all-speakers")
                )
                self.assertIsNone(dialog.result_settings)
                (dialog.save_button if save else dialog.cancel_button).click()
                self.run_task(pool)
                if save:
                    self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                    self.assertEqual(
                        dialog.result_settings.character_voice_defaults,
                        {"Hotelier": "preset:marius", "Ada": "preset:anna"},
                    )
                    self.assertEqual(
                        dialog.result_settings.voice_assignments,
                        {"Narrator": "preset:alba", "Other": "preset:anna"},
                    )
                else:
                    self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
                    self.assertIsNone(dialog.result_settings)
                self.assertEqual(
                    original.character_voice_defaults["Hotelier"], "default"
                )
                self.assertIn("HOTELIER", original.voice_assignments)
                self.assertEqual(original.effective_speaker_announcement_mode, "off")
                importer.narrator_characters.assert_not_called()
                previews.generate.assert_not_called()

    def test_character_policies_restore_recording_priority_and_save_announcements(self):
        for policy in ("automatic", "narrator"):
            with self.subTest(policy=policy):
                pool = ManualThreadPool()
                original = AppSettings(
                    voice_assignments={
                        "Narrator": "preset:alba",
                        "Hotelier": "preset:anna",
                    },
                    character_voice_defaults={
                        "HOTELIER": "preset:marius",
                        "Ada": "preset:anna",
                    },
                    announce_speaker_changes=True,
                )
                dialog = GameNarratorDialog(
                    original,
                    importer=Mock(),
                    preview_service=Mock(),
                    thread_pool=pool,
                    player=Mock(),
                )
                self.application.processEvents()
                dialog.role.setCurrentText("Hotelier")
                dialog.source.setCurrentIndex(dialog.source.findData(policy))
                dialog.announcements.setCurrentIndex(
                    dialog.announcements.findData("narrator-fallback-roles")
                )
                dialog.save_button.click()
                self.run_task(pool)
                saved = dialog.result_settings
                self.assertEqual(saved.voice_assignments, {"Narrator": "preset:alba"})
                self.assertEqual(
                    saved.character_voice_defaults,
                    {
                        "Ada": "preset:anna",
                        **({"Hotelier": "default"} if policy == "narrator" else {}),
                    },
                )
                self.assertEqual(
                    saved.effective_speaker_announcement_mode, "narrator-fallback-roles"
                )
                self.assertFalse(saved.announce_speaker_changes)
                self.assertTrue(original.announce_speaker_changes)

    def test_imported_character_save_captures_role_and_preserves_narrator_references(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.narrator_manifest(root / "voices")
            original = bind_game_narrator(
                AppSettings(
                    voice_manifest=str(manifest), pocket_gated_model_accepted=True
                ),
                manifest,
                "character:centurion",
                "Centurion",
                root=root / "saved",
            )
            original = original.updated(
                voice_assignments={
                    **original.voice_assignments,
                    "Hotelier": "preset:anna",
                }
            )
            before = Path(original.voice_manifest).read_bytes()
            narrator_metadata = json.loads(before)["vntts.game_narrator"]
            expected_refs = {
                voice.character: tuple(path.read_bytes() for path in voice.references)
                for voice in initialize_voice_registry(original).unique_voices()
            }
            imported = self.narrator_manifest(root / "story-voices")
            imported_document = json.loads(imported.read_text())
            imported_document["voices"][-1]["character"] = "New story role"
            imported.write_text(json.dumps(imported_document))
            pool, importer = ManualThreadPool(), Mock()
            dialog = GameNarratorDialog(
                original,
                importer=importer,
                preview_service=Mock(),
                thread_pool=pool,
                player=Mock(),
                binder=partial(bind_game_narrator, root=root / "saved"),
            )
            self.application.processEvents()
            dialog.set_voice_context(
                SimpleNamespace(voice_manifest=str(imported), groups=()),
                character="Hotelier",
            )
            dialog.source.setCurrentIndex(dialog.source.findData("catalog"))
            dialog.catalog_choice.setCurrentIndex(
                dialog.catalog_choice.findData("character:rhiannon")
            )
            dialog.save_button.click()
            # A queued edit must not retarget the already submitted save.
            dialog.role.setCurrentText("Ada")
            self.run_task(pool)
            self.run_task(pool)
            saved = dialog.result_settings
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(
                saved.voice_assignments,
                {"Narrator": original.voice_assignments["Narrator"]},
            )
            self.assertEqual(set(saved.character_voice_defaults), {"Hotelier"})
            registry = initialize_voice_registry(saved)
            self.assertEqual(registry.resolve("Narrator").source_character, "Centurion")
            self.assertEqual(
                registry.resolve("Narrator").reference.read_bytes(),
                clean_wav_bytes(amplitude=0.2),
            )
            self.assertEqual(registry.resolve("Hotelier").source_character, "Rhiannon")
            self.assertEqual(
                registry.resolve("Hotelier").reference.read_bytes(), clean_wav_bytes()
            )
            self.assertEqual(
                registry.resolve("New story role").reference.read_bytes(), b"unrelated"
            )
            for character, references in expected_refs.items():
                self.assertEqual(
                    tuple(
                        path.read_bytes()
                        for path in registry.resolve_source(
                            f"character:{character}"
                        ).references
                    ),
                    references,
                )
            self.assertEqual(
                json.loads(Path(saved.voice_manifest).read_bytes())[
                    "vntts.game_narrator"
                ],
                narrator_metadata,
            )
            self.assertEqual(Path(original.voice_manifest).read_bytes(), before)
            self.assertEqual(original.voice_assignments["Hotelier"], "preset:anna")
            importer.narrator_characters.assert_not_called()

    def test_saved_game_voice_is_not_replaced_by_pocket_preset_without_access(self):
        with TemporaryDirectory() as directory:
            manifest = write_manifest(Path(directory))
            for role in ("Narrator", "Hotelier"):
                with self.subTest(role=role):
                    pool, importer, previews = ManualThreadPool(), Mock(), Mock()
                    settings = AppSettings(
                        voice_manifest=str(manifest),
                        voice_assignments={"Narrator": "character:centurion"},
                        character_voice_defaults={"Hotelier": "character:rhiannon"},
                    )
                    dialog = GameNarratorDialog(
                        settings,
                        importer=importer,
                        preview_service=previews,
                        thread_pool=pool,
                        player=Mock(),
                    )
                    self.application.processEvents()
                    dialog.role.setCurrentText(role)
                    self.assertEqual(dialog.source.currentData(), "catalog")
                    self.assertEqual(
                        dialog.catalog_choice.currentData(),
                        "character:centurion"
                        if role == "Narrator"
                        else "character:rhiannon",
                    )
                    self.assertFalse(dialog.save_button.isEnabled())
                    self.assertFalse(dialog.preview_button.isEnabled())
                    self.assertTrue(dialog.original_button.isEnabled())
                    dialog.cancel_button.click()
                    self.run_task(pool)
                    self.assertIsNone(dialog.result_settings)
                    importer.narrator_characters.assert_not_called()
                    previews.generate.assert_not_called()

    def test_story_context_shows_verified_portrait_and_human_voice_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.narrator_manifest(root / "voices")
            settings = bind_game_narrator(
                AppSettings(
                    voice_manifest=str(manifest), pocket_gated_model_accepted=True
                ),
                manifest,
                "character:centurion",
                "Centurion",
                target_character="Hotelier",
                root=root / "saved",
            )
            source = settings.character_voice_defaults["Hotelier"]
            plan = narrator_preview_plan(
                settings, settings.voice_manifest, source, "Line."
            )
            portrait = root / "portrait.png"
            pixmap = QPixmap(20, 20)
            pixmap.fill()
            self.assertTrue(pixmap.save(str(portrait)))
            group = replace(
                plan.groups[0],
                character="Hotelier",
                portrait_image=str(portrait),
                portrait_image_sha256=sha256_file(portrait),
            )
            plan = replace(plan, groups=(group,))
            pool = ManualThreadPool()
            dialog = GameNarratorDialog(
                settings,
                importer=Mock(),
                preview_service=Mock(),
                thread_pool=pool,
                player=Mock(),
            )
            self.application.processEvents()
            dialog.set_voice_context(
                plan,
                character="Hotelier",
                roles=("???", "Other role"),
                story_titles=("<Literal story>",),
            )
            self.assertFalse(dialog.portrait.isHidden())
            self.assertFalse(dialog.portrait.pixmap().isNull())
            self.assertIn("Planned: Centurion", dialog.role_summary.text())
            self.assertNotIn("Game voice ", dialog.role_summary.text())
            self.assertIn("<Literal story>", dialog.role_summary.text())
            self.assertEqual(dialog.role_summary.textFormat(), Qt.TextFormat.PlainText)
            self.assertGreaterEqual(dialog.role.findText("Other role"), 0)
            dialog.set_voice_context(
                replace(plan, groups=(replace(group, portrait_image_sha256="0" * 64),)),
                character="Hotelier",
            )
            self.assertTrue(dialog.portrait.isHidden())
            dialog.role.setCurrentText("???")
            self.assertEqual(dialog.role.currentText(), "Narrator")
            dialog.cancel_button.click()
            self.run_task(pool)

    def test_engine_switch_preserves_source_intent_and_cancel_discards_changes(self):
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
        self.assertEqual(dialog.engine_choice.findData("coqui-xtts"), -1)
        dialog.engine_choice.setCurrentIndex(dialog.engine_choice.findData("moss-tts"))
        self.assertEqual(dialog.source.currentData(), "preset")
        self.assertFalse(dialog.preview_button.isEnabled())
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertIn("Choose a game voice", dialog.engine_guidance.text())
        self.assertTrue(dialog.model_choice.isHidden())
        dialog.model_details.click()
        dialog.model_choice.setText("custom-moss-model")
        self.assertEqual(
            dialog.engine.text(), engine_model_label("moss-tts", "custom-moss-model")
        )
        dialog.engine_choice.setCurrentIndex(
            dialog.engine_choice.findData("pocket-tts")
        )
        self.assertEqual(dialog.source.currentData(), "preset")
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertIsNone(dialog._settings().tts_model)
        self.assertEqual(dialog._settings().tts_profile, "default")
        dialog.presets.setCurrentIndex(dialog.presets.findData("preset:marius"))
        dialog.reject()
        self.run_task(pool)
        self.assertIsNone(dialog.result_settings)
        self.assertEqual(original.voice_assignments, {"Narrator": "preset:alba"})
        self.assertEqual(original.speech_backend, "pocket-tts")

    def test_game_engine_model_and_consent_are_staged_for_preview_and_save(self):
        with TemporaryDirectory() as directory:
            manifest = write_manifest(Path(directory))
            importer = self.narrator_importer(manifest)
            pool, previews = ManualThreadPool(), Mock()
            previews.generate.return_value.path = Path(directory) / "preview.wav"
            original = AppSettings(
                speech_backend="moss-tts",
                tts_model="saved-custom-model",
                tts_profile="natural",
                voice_assignments={"Other": "character:other"},
            )
            binder = Mock(side_effect=lambda settings, *_args: settings)
            dialog = GameNarratorDialog(
                original,
                importer=importer,
                preview_service=previews,
                thread_pool=pool,
                player=Mock(),
                binder=binder,
            )
            self.application.processEvents()
            self.assertEqual(
                dialog.engine.text(),
                engine_model_label("moss-tts", "saved-custom-model"),
            )
            self.assertTrue(dialog.model_choice.isHidden())
            self.run_task(pool)
            dialog.prepare_button.click()
            self.run_task(pool)
            self.run_task(pool)
            dialog.engine_choice.setCurrentIndex(
                dialog.engine_choice.findData("pocket-tts")
            )
            self.assertEqual(dialog.source.currentData(), "game")
            self.assertFalse(dialog.consent.isHidden())
            self.assertFalse(dialog.save_button.isEnabled())
            self.assertTrue(dialog.original_button.isEnabled())
            dialog.consent.setChecked(True)
            self.assertTrue(dialog.save_button.isEnabled())
            dialog.engine_choice.setCurrentIndex(
                dialog.engine_choice.findData("moss-tts")
            )
            self.assertIsNone(dialog._settings().tts_model)
            self.assertEqual(dialog._settings().tts_profile, "stable")
            self.assertTrue(dialog.consent.isHidden())
            dialog.model_details.click()
            dialog.model_choice.setText("new-custom-model")
            dialog.preview_button.click()
            for control in (
                dialog.engine_choice,
                dialog.model_choice,
                dialog.source,
                dialog.presets,
                dialog.consent,
            ):
                self.assertFalse(control.isEnabled())
            # Even a queued UI change cannot alter an active preview's settings.
            dialog.engine_choice.setCurrentIndex(
                dialog.engine_choice.findData("pocket-tts")
            )
            dialog.model_choice.setText("stale-model-change")
            self.assertEqual(dialog.engine_choice.currentData(), "moss-tts")
            self.assertEqual(dialog.model_choice.text(), "new-custom-model")
            self.run_task(pool)
            plan = previews.generate.call_args.args[0]
            self.assertEqual(plan.synthesis_backend, "moss-tts")
            self.assertEqual(plan.synthesis_model, "new-custom-model")
            self.assertIsNone(dialog.result_settings)
            dialog.save_button.click()
            self.run_task(pool)
            self.run_task(pool)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(dialog.result_settings.tts_model, "new-custom-model")
            self.assertEqual(
                dialog.result_settings.voice_assignments, original.voice_assignments
            )
            self.assertEqual(original.tts_model, "saved-custom-model")
            self.assertFalse(original.pocket_gated_model_accepted)

    def test_unavailable_and_xtts_engines_require_explicit_supported_selection(self):
        for backend in ("moss-tts", "coqui-xtts"):
            with (
                self.subTest(backend=backend),
                patch(
                    "vntts.game_narrator_ui.speech_backend_options",
                    return_value=(
                        ("Pocket TTS", "pocket-tts", True),
                        (backend, backend, backend == "coqui-xtts"),
                    ),
                ),
            ):
                pool = ManualThreadPool()
                importer = Mock()
                importer.narrator_characters.return_value = ()
                dialog = GameNarratorDialog(
                    AppSettings(speech_backend=backend),
                    importer=importer,
                    preview_service=Mock(),
                    thread_pool=pool,
                    player=Mock(),
                )
                self.application.processEvents()
                self.run_task(pool)
                self.assertFalse(dialog._engine_available())
                self.assertFalse(dialog.save_button.isEnabled())
                self.assertFalse(dialog.preview_button.isEnabled())
                self.assertIn(
                    "not supported for story preparation"
                    if backend == "coqui-xtts"
                    else "not included in this package",
                    dialog.engine_guidance.text(),
                )
                dialog.engine_choice.setCurrentIndex(0)
                self.assertEqual(dialog.source.currentData(), "game")
                self.run_task(pool)
                dialog.reject()
                self.run_task(pool)

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
                            "version": 5,
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
            manifest = self.narrator_manifest(root / "candidates")
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

    def test_character_reference_defaults_survive_restart_and_later_narrator_choice(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack_root = root / "pack"
            pack_root.mkdir()
            pack, *_ = write_synthetic_game_pack(pack_root)
            original = apply_game_pack(
                AppSettings(
                    pocket_gated_model_accepted=True,
                    tts_speaker_wav="custom-narrator.wav",
                ),
                pack,
            )
            source = self.narrator_manifest(root / "candidates")
            candidate = bind_game_narrator(
                original,
                source,
                "character:centurion",
                "Centurion",
                target_character="Ada",
                root=root / "saved",
            )
            self.assertEqual(candidate.tts_speaker_wav, "custom-narrator.wav")
            self.assertNotIn(
                "vntts.game_narrator",
                json.loads(Path(candidate.voice_manifest).read_text()),
            )
            for choose_narrator in (False, True):
                with self.subTest(choose_narrator=choose_narrator):
                    selected = (
                        bind_game_narrator(
                            candidate,
                            source,
                            "character:rhiannon",
                            "Rhiannon",
                            root=root / "saved",
                        )
                        if choose_narrator
                        else candidate
                    )
                    loaded = load_app_settings(
                        selected.save(root / "settings.json"), environment={}
                    )
                    self.assertEqual(loaded.voice_manifest, selected.voice_manifest)
                    self.assertEqual(
                        loaded.character_voice_defaults,
                        candidate.character_voice_defaults,
                    )
                    registry = initialize_voice_registry(loaded)
                    self.assertEqual(
                        registry.resolve("Ada").reference.read_bytes(),
                        clean_wav_bytes(amplitude=0.2),
                    )
                    self.assertEqual(
                        registry.resolve("Ada").source_character, "Centurion"
                    )
                    if choose_narrator:
                        self.assertEqual(
                            registry.resolve("Narrator").reference.read_bytes(),
                            clean_wav_bytes(),
                        )
                        self.assertIsNone(loaded.tts_speaker_wav)
                    self.assertEqual(
                        apply_game_pack(loaded, pack).voice_manifest,
                        original.voice_manifest,
                    )

            custom = Path(candidate.voice_manifest)
            document = json.loads(custom.read_text())
            document["voices"] = [
                row
                for row in document["voices"]
                if not row["character"].startswith("Game voice ")
            ]
            custom.write_text(json.dumps(document))
            with self.assertRaisesRegex(
                GamePackError, "missing from its saved catalog"
            ):
                apply_game_pack(candidate)
            document["vntts.game_character_voices"]["base_manifest_sha256"] = "0" * 64
            custom.write_text(json.dumps(document))
            self.assertEqual(
                apply_game_pack(candidate).voice_manifest, original.voice_manifest
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
                voice_saves = []

                def save_settings(candidate):
                    if candidate.voice_assignments != tray.settings.voice_assignments:
                        voice_saves.append(candidate)
                        if decision == "save-failure":
                            raise OSError("disk full")
                    return root / "settings.json"

                with (
                    patch("vntts.app.GameNarratorDialog", return_value=picker),
                    patch.object(
                        AppSettings,
                        "save",
                        autospec=True,
                        side_effect=save_settings,
                    ),
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
                        self.assertEqual(len(voice_saves), 1)
                        self.assertEqual(
                            tray.settings.voice_assignments["Narrator"], "preset:marius"
                        )
                        self.assertEqual(
                            preparation.settings.updated(
                                last_main_section=tray.settings.last_main_section
                            ),
                            tray.settings,
                        )
                        self.assertIn("Marius", preparation.narrator_status.text())
                        reload.assert_called_once()
                    else:
                        self.assertEqual(
                            len(voice_saves), int(decision == "save-failure")
                        )
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

    def test_binding_rejects_unusable_selected_reference_before_mutation(self):
        for name, payload in (
            ("short", clean_wav_bytes(seconds=0.06075, sample_rate=24_000)),
            ("silent", clean_wav_bytes(amplitude=0)),
            ("malformed", b"not a WAV"),
        ):
            with self.subTest(reference=name), TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = self.narrator_manifest(root / "source")
                (manifest.parent / "references" / "centurion.wav").write_bytes(
                    payload
                )
                settings = AppSettings(
                    voice_manifest=str(manifest),
                    pocket_gated_model_accepted=True,
                    voice_assignments={"Aderyn": "character:rhiannon"},
                )
                before_settings, before_manifest = asdict(settings), manifest.read_bytes()
                output_root = root / "saved"

                with self.assertRaisesRegex(
                    ValueError, r"Cannot save Centurion: reference 1 is unusable"
                ):
                    bind_game_narrator(
                        settings,
                        manifest,
                        "character:centurion",
                        "Centurion",
                        root=output_root,
                    )

                self.assertEqual(asdict(settings), before_settings)
                self.assertEqual(manifest.read_bytes(), before_manifest)
                self.assertFalse(output_root.exists())

    def test_binding_preserves_existing_routes_and_original_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.narrator_manifest(root / "existing")
            before = manifest.read_bytes()
            settings = AppSettings(
                voice_manifest=str(manifest),
                pocket_gated_model_accepted=True,
                voice_assignments={"Aderyn": "character:rhiannon"},
            )
            self.assertEqual(
                (manifest.parent / "references" / "unrelated.wav").read_bytes(),
                b"unrelated",
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
                clean_wav_bytes(amplitude=0.2),
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
            manifest = self.narrator_manifest(root / "candidates")
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
            manifest = self.narrator_manifest(root / "narrators")
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

    def test_picker_shows_original_reference_title_and_plain_text_transcript(self):
        with TemporaryDirectory() as directory:
            manifest = write_manifest(Path(directory))
            importer = self.narrator_importer(manifest)
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
            self.run_task(pool)
            self.assertEqual(dialog.references.count(), 5)
            dialog.references.setCurrentIndex(4)
            importer.prepare_voice_roles.assert_not_called()
            self.assertIn("Spoken line 5", dialog.references.currentText())
            self.assertEqual(
                dialog.reference_text.text(),
                "Original transcript 5. <Not markup.>",
            )
            dialog.original_button.click()
            self.run_task(pool)  # Finish the first selection, which must not play.
            dialog.player.play.assert_not_called()
            self.run_task(pool)  # Prepare the latest selection.
            self.run_task(pool)  # Play the queued selection.
            self.assertEqual(
                importer.prepare_voice_roles.call_args.kwargs["narrator_line_id"],
                "playable-voice:3032:5",
            )
            dialog.original_button.click()
            self.run_task(pool)
            self.assertEqual(importer.prepare_voice_roles.call_count, 2)
            dialog.references.setCurrentIndex(0)
            dialog.original_button.click()
            self.run_task(pool)
            self.assertEqual(importer.prepare_voice_roles.call_count, 2)
            dialog.characters.clear()
            self.assertEqual(dialog.reference_text.text(), "")
            dialog.reject()
            self.run_task(pool)

    def test_decoder_setup_consent_retries_in_worker_and_keeps_controls_gated(self):
        with TemporaryDirectory() as directory:
            manifest = write_manifest(Path(directory) / "candidates")
            importer = self.narrator_importer(manifest)
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
            self.run_task(pool)
            dialog.original_button.click()
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

    def test_prefetch_never_autoplays_and_stop_switch_close_drop_queued_playback(self):
        for action in ("no play", "stop", "switch", "close"):
            with self.subTest(action=action), TemporaryDirectory() as directory:
                manifest = write_manifest(Path(directory))
                importer = self.narrator_importer(manifest)
                pool, player = ManualThreadPool(), Mock()
                dialog = GameNarratorDialog(
                    AppSettings(speech_backend="moss-tts"),
                    importer=importer,
                    preview_service=Mock(),
                    thread_pool=pool,
                    player=player,
                )
                self.application.processEvents()
                self.run_task(pool)
                dialog.prepare_button.click()
                self.run_task(pool)
                self.assertEqual(dialog._operation, "warm")
                self.assertTrue(dialog.references.isEnabled())
                self.assertTrue(dialog.original_button.isEnabled())
                self.assertFalse(dialog.prepare_button.isEnabled())
                if action != "no play":
                    dialog.original_button.click()
                    self.assertIn("Playback will start", dialog.status.text())
                if action == "stop":
                    dialog.stop_button.click()
                elif action == "switch":
                    dialog.references.setCurrentIndex(1)
                    dialog.references.setCurrentIndex(4)
                elif action == "close":
                    dialog.reject()
                while pool.tasks:
                    self.run_task(pool)
                player.play.assert_not_called()
                if action == "switch":
                    self.assertEqual(
                        [
                            call.kwargs["narrator_line_id"]
                            for call in importer.prepare_voice_roles.call_args_list
                        ],
                        ["playable-voice:3032:1", "playable-voice:3032:5"],
                    )
                if action != "close":
                    dialog.reject()
                    self.run_task(pool)

    def test_background_decoder_setup_waits_for_explicit_play(self):
        with TemporaryDirectory() as directory:
            importer = self.narrator_importer(write_manifest(Path(directory)))
            importer.prepare_voice_roles.side_effect = DecoderSetupRequired(
                "Install decoder?"
            )
            pool = ManualThreadPool()
            dialog = GameNarratorDialog(
                AppSettings(speech_backend="moss-tts"),
                importer=importer,
                preview_service=Mock(),
                thread_pool=pool,
                player=Mock(),
            )
            self.application.processEvents()
            self.run_task(pool)
            dialog.prepare_button.click()
            self.run_task(pool)
            with patch(
                "vntts.game_narrator_ui.confirm_decoder_setup", return_value=False
            ) as prompt:
                self.run_task(pool)
                prompt.assert_not_called()
                self.assertIn("Press Play", dialog.status.text())
                dialog.original_button.click()
                self.run_task(pool)
                prompt.assert_called_once()
            dialog.reject()
            self.run_task(pool)

    def test_play_cached_selection_while_another_prefetch_finishes(self):
        with TemporaryDirectory() as directory:
            importer = self.narrator_importer(write_manifest(Path(directory)))
            pool, player = ManualThreadPool(), Mock()
            dialog = GameNarratorDialog(
                AppSettings(speech_backend="moss-tts"),
                importer=importer,
                preview_service=Mock(),
                thread_pool=pool,
                player=player,
            )
            self.application.processEvents()
            self.run_task(pool)
            dialog.prepare_button.click()
            self.run_task(pool)
            self.run_task(pool)
            dialog.references.setCurrentIndex(4)
            dialog.references.setCurrentIndex(0)
            dialog.original_button.click()
            self.run_task(pool)
            self.run_task(pool)
            player.play.assert_called_once()
            self.assertIsNone(dialog._queued_action)
            dialog.reject()
            self.run_task(pool)

    def test_save_unplayed_reference_prepares_only_that_selection(self):
        with TemporaryDirectory() as directory:
            manifest = write_manifest(Path(directory))
            importer = self.narrator_importer(manifest)
            pool = ManualThreadPool()
            binder = Mock(return_value=AppSettings())
            previews = Mock()
            dialog = GameNarratorDialog(
                AppSettings(speech_backend="moss-tts"),
                importer=importer,
                preview_service=previews,
                thread_pool=pool,
                player=Mock(),
                binder=binder,
            )
            self.application.processEvents()
            self.run_task(pool)
            dialog.prepare_button.click()
            self.run_task(pool)
            dialog.references.setCurrentIndex(4)
            dialog.save_button.click()
            self.run_task(pool)
            self.run_task(pool)
            self.run_task(pool)
            self.run_task(pool)
            self.assertEqual(
                importer.prepare_voice_roles.call_args.kwargs["narrator_line_id"],
                "playable-voice:3032:5",
            )
            binder.assert_called_once_with(
                dialog._settings(), manifest, "character:centurion", "Centurion"
            )
            previews.generate.assert_not_called()
            previews.reference_audio.assert_not_called()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_guided_flow_gates_controls_previews_and_saves(self):
        with (
            TemporaryDirectory() as directory,
            patch("vntts.game_narrator.find_default_voice_manifest", return_value=None),
        ):
            root = Path(directory)
            manifest = self.narrator_manifest(root / "candidates")
            importer = self.narrator_importer(manifest)
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
            importer.prepare_voice_roles.assert_not_called()
            self.assertFalse(dialog.preview_button.isEnabled())
            self.assertFalse(dialog.save_button.isEnabled())
            dialog.original_button.click()
            self.run_task(pool)
            importer.prepare_voice_roles.assert_called_once_with(
                ("Centurion",),
                dialog.cancellation,
                progress=dialog.decoderProgress.emit,
                narrator=True,
                narrator_line_id="playable-voice:3032:1",
            )
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
            importer = self.narrator_importer(manifest)
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
