import json
import os
import unittest
from dataclasses import replace
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QFormLayout,
    QSizePolicy,
)
from vntts_artifacts.file_integrity import sha256_file  # noqa: E402
from vntts_artifacts.story_index import load_story_index_document  # noqa: E402

from tests.test_authoring_pcm_playback import FakeAudioModule  # noqa: E402
from tests.test_pregeneration_audition import FakeBackend, clean_wav_bytes  # noqa: E402
from tests.test_pregeneration_setup import ManualThreadPool  # noqa: E402
from tests.test_pregeneration_voices import (  # noqa: E402
    write_content,
    write_manifest,
    write_player_candidate_manifest,
)
from tests.test_voice_default_impact import voice_impact_fixture  # noqa: E402
from vntts.app import TrayApplication  # noqa: E402
from vntts.authoring.pcm_playback import PersistentPcmPlayer  # noqa: E402
from vntts.configuration_apply import ConfigurationApplyMixin  # noqa: E402
from vntts.game_audio_decoder import DecoderSetupRequired  # noqa: E402
from vntts.game_content_importer import Reverse1999GameImporter  # noqa: E402
from vntts.game_narrator import (  # noqa: E402
    bind_voice_library_selection,
    load_original_reference,
    narrator_preview_plan,
)
from vntts.game_narrator_ui import GameNarratorDialog  # noqa: E402
from vntts.pregeneration_audition import VoiceAuditionPreviewService  # noqa: E402
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    inspect_story_index,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.pregeneration_voices import VoicePlanStore  # noqa: E402
from vntts.qt_audio import QtPcmPlayer  # noqa: E402
from vntts.runtime_config import initialize_voice_registry  # noqa: E402
from vntts.settings import AppSettings, load_app_settings  # noqa: E402
from vntts.voice_library import VoiceLibrary  # noqa: E402


class GameNarratorTest(unittest.TestCase):
    def test_voice_picker_saves_one_library_binding_without_a_manifest_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.narrator_manifest(root / "voices")
            library = VoiceLibrary(root / "library")

            saved = bind_voice_library_selection(
                AppSettings(voice_manifest=str(manifest)),
                manifest,
                "character:centurion",
                "Centurion",
                root=library.root,
            )

            self.assertEqual(saved.voice_manifest, str(manifest))
            self.assertEqual(len(library.resolve_source_paths("Narrator")), 1)
            registry = initialize_voice_registry(saved, voice_library=library)
            self.assertEqual(registry.resolve("Narrator").source_character, "Centurion")

    def test_original_snapshot_keeps_inspected_bytes_if_file_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.narrator_manifest(root)
            with patch("vntts.support.record_game_import") as record:
                original = load_original_reference(manifest, "character:centurion")
            self.assertEqual(record.call_args.args, ("original-reference",))
            self.assertEqual(record.call_args.kwargs["duration_seconds"], 1.2)
            self.assertEqual(
                record.call_args.kwargs["reference_bytes"], len(original.payload)
            )
            self.assertEqual(
                record.call_args.kwargs["reference_sha256"], original.sha256
            )
            expected = original.path.read_bytes()
            original.path.write_bytes(clean_wav_bytes(seconds=0.06))
            self.assertEqual(original.payload, expected)
            self.assertNotEqual(original.sha256, sha256_file(original.path))
            self.assertEqual(original.duration_seconds, 1.2)
            self.assertEqual(original.rejection_reasons, ())

    def test_original_plays_inspected_bytes_and_reports_device_state_without_tts(self):
        for seconds in (0.06, 1.2):
            with self.subTest(seconds=seconds), TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = self.narrator_manifest(root)
                reference = root / "references" / "centurion.wav"
                payload = clean_wav_bytes(seconds=seconds)
                reference.write_bytes(payload)
                importer = self.narrator_importer(manifest)
                pool, previews = ManualThreadPool(), Mock()
                audio = FakeAudioModule()
                pcm = PersistentPcmPlayer(audio)
                player = QtPcmPlayer(player_factory=lambda: pcm)
                dialog = GameNarratorDialog(
                    AppSettings(speech_backend="moss-tts"),
                    importer=importer,
                    preview_service=previews,
                    thread_pool=pool,
                    player=player,
                )
                try:
                    self.application.processEvents()
                    self.run_task(pool)
                    self.run_task(pool)
                    for _ in range(2):
                        dialog.original_button.click()
                        while pool.tasks:
                            self.run_task(pool)
                        self.assertEqual(dialog.status.text(), "Starting playback...")
                        self.assertIn("Centurion", dialog.reference_details.text())
                        self.assertIn(
                            f"{seconds:.3f} s", dialog.reference_details.text()
                        )
                        self.assertIn(
                            sha256_file(reference), dialog.reference_details.toolTip()
                        )
                        if seconds < 1:
                            self.assertIn(
                                "Not suitable for cloning",
                                dialog.reference_details.text(),
                            )
                        else:
                            self.assertIn(
                                "checks passed", dialog.reference_details.text()
                            )
                        self.assertGreater(abs(audio.stream.pump(480)).max(), 0)
                        player._poll()
                        self.assertEqual(
                            dialog.status.text(), "Playing original reference."
                        )
                        audio.stream.pump(round(seconds * 48_000))
                        audio.stream.time += 1
                        player._poll()
                        self.assertIn(
                            "Original reference finished", dialog.status.text()
                        )
                    previews.generate.assert_not_called()
                    previews.reference_audio.assert_not_called()
                    dialog.references.setCurrentIndex(1)
                    self.assertEqual(dialog.reference_details.text(), "")
                finally:
                    dialog.reject()
                    while pool.tasks:
                        self.run_task(pool)
                    pcm.close()

    def test_original_and_preview_report_device_failures(self):
        with TemporaryDirectory() as directory:
            manifest = self.narrator_manifest(Path(directory))
            pool = ManualThreadPool()
            pcm = PersistentPcmPlayer(FakeAudioModule())
            player = QtPcmPlayer(player_factory=lambda: pcm)
            dialog = GameNarratorDialog(
                AppSettings(speech_backend="moss-tts"),
                importer=self.narrator_importer(manifest),
                preview_service=Mock(),
                thread_pool=pool,
                player=player,
            )
            try:
                self.application.processEvents()
                self.run_task(pool)
                self.run_task(pool)
                dialog.original_button.click()
                while pool.tasks:
                    self.run_task(pool)
                for operation, message, expected in (
                    (
                        "audio",
                        "Test device failure",
                        "Original reference playback failed: Test device failure",
                    ),
                    (
                        "preview",
                        "Preview device failure",
                        "Preview playback failed: Preview device failure",
                    ),
                ):
                    dialog._operation = operation
                    dialog._playback_requested = True
                    dialog.player.errorOccurred.emit(
                        player.Error.ResourceError, message
                    )
                    self.assertEqual(dialog.status.text(), expected)
            finally:
                dialog.reject()
                while pool.tasks:
                    self.run_task(pool)
                pcm.close()

    def test_voice_impact_loads_stories_only_on_request_and_selects_without_generating(
        self,
    ):
        with TemporaryDirectory() as directory:
            content, jobs, decisions, settings, pack, library = voice_impact_fixture(
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
                voice_library=library,
                thread_pool=pool,
            )
            controller = Mock(is_live_running=False)
            tray = TrayApplication(
                self.application,
                settings,
                controller_factory=Mock(return_value=controller),
            )
            dialog = GameNarratorDialog(
                settings,
                thread_pool=pool,
                player=Mock(),
                preview_service=Mock(),
                voice_library=library,
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
                    # Stories also validates its saved audio in the background.
                    while pool.tasks:
                        self.run_task(pool)
                    self.assertEqual(tray.dashboard.sections.currentIndex(), 1)
                    self.assertIn(
                        "1 prepared lines in 1 stories", dialog.impact_status.text()
                    )
                    self.assertIn("Chapter 1: 1 changed", dialog._impact_copy_text())
                    self.assertIsNone(dialog.result_settings)
                    dialog.consent.setChecked(not dialog.consent.isChecked())
                    self.assertIsNone(dialog._impact_results)
                    dialog.check_impact.click()
                    self.run_task(pool)
                    dialog.select_affected.click()
                    self.run_task(pool)
                    self.assertEqual(preparation.selected_story_ids(), ("chapter:1",))
                    self.assertEqual(
                        library.binding("Rhiannon").source_id,
                        "preset:marius",
                    )
                    load_app_settings(Path(directory) / "settings.json")
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

    def setUp(self):
        self._voice_library_directory = TemporaryDirectory()
        self._voice_library = VoiceLibrary(
            Path(self._voice_library_directory.name) / "library"
        )
        self._voice_library_patches = (
            patch(
                "vntts.game_narrator_ui.application_voice_library",
                return_value=self._voice_library,
            ),
            patch(
                "vntts.game_narrator.application_voice_library",
                return_value=self._voice_library,
            ),
            patch(
                "vntts.pregeneration_ui.application_voice_library",
                return_value=self._voice_library,
            ),
        )
        for library_patch in self._voice_library_patches:
            library_patch.start()

    def tearDown(self):
        for library_patch in reversed(self._voice_library_patches):
            library_patch.stop()
        self._voice_library_directory.cleanup()

    def run_task(self, pool):
        pool.tasks.pop(0).run()
        self.application.processEvents()

    def select_preset(self, source_id="preset:alba", *, role="Narrator"):
        self._voice_library.select(role, route="voice", source_id=source_id)

    def bind_game_voice(self, settings, manifest, source_id, *, role="Narrator"):
        return bind_voice_library_selection(
            settings,
            manifest,
            source_id,
            source_id.partition(":")[2],
            root=self._voice_library.root,
            target_character=role,
        )

    @staticmethod
    def choose_game_source(dialog):
        dialog.source.setCurrentIndex(dialog.source.findData("game"))

    def narrator_manifest(self, root):
        manifest = write_manifest(root, rhiannon=clean_wav_bytes())
        (root / "references" / "centurion.wav").write_bytes(
            clean_wav_bytes(amplitude=0.2)
        )
        return manifest

    def test_preview_compute_stays_visible_and_cached_playback_clears_generation(self):
        self.select_preset()
        pool = ManualThreadPool()
        previews = Mock()
        previews.backend.runtime_status = "GPU: RTX 2070 SUPER <8 GB>; auxiliary: CPU"
        previews.generate.return_value = SimpleNamespace(
            path=Path("/tmp/preview.wav"), reused=True
        )
        dialog = GameNarratorDialog(
            AppSettings(),
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

    def test_game_reference_requires_a_selected_character(self):
        importer = Mock()
        dialog = GameNarratorDialog(
            AppSettings(),
            importer=importer,
            preview_service=Mock(),
            player=Mock(),
        )
        try:
            with self.assertRaisesRegex(ValueError, "Choose a game character first"):
                dialog._perform_candidate_action(
                    "audio", AppSettings(), None, "game:reference", ""
                )
            importer.prepare_voice_roles.assert_not_called()
        finally:
            dialog.reject()

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
            )
            self.bind_game_voice(
                settings, manifest, "character:rhiannon", role="Hotelier"
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
            self.assertTrue(dialog.catalog_original_button.isVisibleTo(dialog))
            self.assertFalse(dialog.original_button.isVisibleTo(dialog))
            self.assertTrue(dialog.stop_button.isHidden())
            try:
                dialog.preview_button.click()
                self.run_task(pool)
                self.assertEqual(dialog.status.text(), "Starting playback...")
                player.playbackStateChanged.connect.call_args.args[0](
                    QtPcmPlayer.PlaybackState.PlayingState
                )
                self.assertFalse(dialog.stop_button.isHidden())
                self.assertIn("Playing generated preview", dialog.status.text())
                self.assertIn("CPU test worker", dialog.runtime.text())
                source = player.setSource.call_args.args[0]
                self.assertTrue(Path(source.toLocalFile()).is_file())
                self.assertEqual(len(backend.requests), 1)
                player.mediaStatusChanged.connect.call_args.args[0](
                    QtPcmPlayer.MediaStatus.EndOfMedia
                )
                self.assertTrue(dialog.stop_button.isHidden())

                dialog.preview_button.click()
                self.run_task(pool)
                player.playbackStateChanged.connect.call_args.args[0](
                    QtPcmPlayer.PlaybackState.PlayingState
                )
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
            self.select_preset()
            pool = ManualThreadPool()
            importer = Mock()
            backend = FakeBackend("pocket-tts")
            factory = Mock(return_value=backend)
            previews = VoiceAuditionPreviewService(directory, backend_factory=factory)
            settings = AppSettings(pocket_gated_model_accepted=True)
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
                self._voice_library.binding("Narrator").source_id,
                "preset:marius",
            )
            importer.narrator_characters.assert_not_called()
            self.assertEqual(backend.shutdown_count, 1)

    def test_cancel_builtin_candidate_preserves_saved_narrator(self):
        self.select_preset()
        pool = ManualThreadPool()
        original = AppSettings()
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
        self.assertEqual(
            self._voice_library.binding("Narrator").source_id, "preset:alba"
        )

    def test_character_preset_save_and_cancel_keep_other_roles_unchanged(self):
        for save in (False, True):
            with self.subTest(save=save):
                self.select_preset()
                self._voice_library.clear("Hotelier")
                pool, importer, previews = ManualThreadPool(), Mock(), Mock()
                original = AppSettings()
                dialog = GameNarratorDialog(
                    original,
                    importer=importer,
                    preview_service=previews,
                    thread_pool=pool,
                    player=Mock(),
                )
                self.application.processEvents()
                dialog.set_voice_context(roles=("Hotelier",))
                dialog.role.setCurrentText("Hotelier")
                dialog.source.setCurrentIndex(dialog.source.findData("preset"))
                dialog.presets.setCurrentIndex(dialog.presets.findData("preset:marius"))
                self.assertIsNone(dialog.result_settings)
                (dialog.save_button if save else dialog.cancel_button).click()
                self.run_task(pool)
                if save:
                    self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                    self.assertEqual(
                        self._voice_library.binding("Hotelier").source_id,
                        "preset:marius",
                    )
                else:
                    self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
                    self.assertIsNone(dialog.result_settings)
                self.assertEqual(
                    original.effective_speaker_announcement_mode,
                    "narrator-fallback-roles",
                )
                importer.narrator_characters.assert_not_called()
                previews.generate.assert_not_called()

    def test_current_assignment_does_not_follow_an_unsaved_candidate(self):
        self.select_preset("preset:alba")
        self.select_preset("preset:marius", role="Hotelier")
        pool = ManualThreadPool()
        dialog = GameNarratorDialog(
            AppSettings(),
            importer=Mock(),
            preview_service=Mock(),
            thread_pool=pool,
            player=Mock(),
        )
        try:
            dialog.set_voice_context(roles=("Hotelier",))
            self.assertFalse(dialog.role.isEditable())
            self.assertEqual(
                tuple(
                    dialog.source.itemData(index)
                    for index in range(dialog.source.count())
                ),
                ("game", "preset", "catalog", "automatic", "narrator"),
            )
            self.assertEqual(dialog.role_summary.text(), "Alba")
            dialog.presets.setCurrentIndex(dialog.presets.findData("preset:marius"))
            self.assertEqual(dialog.role_summary.text(), "Alba")

            dialog.role.setCurrentText("Hotelier")
            self.assertIn("Marius", dialog.role_summary.text())
            dialog.presets.setCurrentIndex(dialog.presets.findData("preset:alba"))
            self.assertIn("Marius", dialog.role_summary.text())
            for mode in ("automatic", "narrator"):
                dialog.source.setCurrentIndex(dialog.source.findData(mode))
                self.assertFalse(dialog.form.isRowVisible(dialog.preview_row))
            dialog.source.setCurrentIndex(dialog.source.findData("preset"))
            self.assertTrue(dialog.form.isRowVisible(dialog.preview_row))
        finally:
            dialog.reject()
            self.run_task(pool)

    def test_character_policies_restore_recording_priority_and_global_announcements(
        self,
    ):
        for policy in ("automatic", "narrator"):
            with self.subTest(policy=policy):
                self.select_preset()
                self._voice_library.clear("Hotelier")
                pool = ManualThreadPool()
                original = AppSettings(announce_speaker_changes=True)
                dialog = GameNarratorDialog(
                    original,
                    importer=Mock(),
                    preview_service=Mock(),
                    thread_pool=pool,
                    player=Mock(),
                )
                self.application.processEvents()
                dialog.set_voice_context(roles=("Hotelier",))
                dialog.role.setCurrentText("Hotelier")
                dialog.source.setCurrentIndex(dialog.source.findData(policy))
                self.assertFalse(
                    any(
                        choice.accessibleName() == "Announce speaker names"
                        for choice in dialog.findChildren(type(dialog.role))
                    )
                )
                dialog.save_button.click()
                self.run_task(pool)
                saved = dialog.result_settings
                binding = self._voice_library.binding("Hotelier")
                if policy == "automatic":
                    self.assertIsNone(binding)
                else:
                    self.assertEqual(binding.route, "narrator")
                self.assertEqual(
                    saved.effective_speaker_announcement_mode,
                    original.effective_speaker_announcement_mode,
                )
                self.assertTrue(saved.announce_speaker_changes)
                self.assertTrue(original.announce_speaker_changes)

    def test_imported_character_save_captures_role_and_preserves_narrator_references(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.narrator_manifest(root / "voices")
            original = bind_voice_library_selection(
                AppSettings(
                    voice_manifest=str(manifest), pocket_gated_model_accepted=True
                ),
                manifest,
                "character:centurion",
                "Centurion",
                root=self._voice_library.root,
            )
            before = Path(original.voice_manifest).read_bytes()
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
                binder=partial(
                    bind_voice_library_selection, root=self._voice_library.root
                ),
            )
            self.application.processEvents()
            dialog.set_voice_context(
                SimpleNamespace(voice_manifest=str(imported), groups=()),
                character="Hotelier",
                roles=("Ada",),
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
            registry = initialize_voice_registry(
                saved, voice_library=self._voice_library
            )
            self.assertEqual(registry.resolve("Narrator").source_character, "Centurion")
            self.assertEqual(
                registry.resolve("Narrator").reference.read_bytes(),
                clean_wav_bytes(amplitude=0.2),
            )
            self.assertEqual(registry.resolve("Hotelier").source_character, "Rhiannon")
            self.assertEqual(
                registry.resolve("Hotelier").reference.read_bytes(), clean_wav_bytes()
            )
            self.assertEqual(Path(original.voice_manifest).read_bytes(), before)
            importer.narrator_characters.assert_not_called()

    def test_imported_rhiannon_voice_can_be_saved_for_aderyn_and_used_by_planning(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.narrator_manifest(root / "voices")
            original = bind_voice_library_selection(
                AppSettings(
                    voice_manifest=str(manifest), pocket_gated_model_accepted=True
                ),
                manifest,
                "character:centurion",
                "Centurion",
                root=self._voice_library.root,
            )
            imported = self.narrator_manifest(root / "story-voices")
            expected_reference = imported.parent / "references" / "rhiannon.wav"
            pool = ManualThreadPool()
            dialog = GameNarratorDialog(
                original,
                importer=Mock(),
                preview_service=Mock(),
                thread_pool=pool,
                player=Mock(),
                binder=partial(
                    bind_voice_library_selection, root=self._voice_library.root
                ),
            )
            self.application.processEvents()
            dialog.set_voice_context(
                SimpleNamespace(voice_manifest=str(imported), groups=()),
                character="Aderyn",
            )
            dialog.source.setCurrentIndex(dialog.source.findData("catalog"))
            dialog.catalog_choice.setCurrentIndex(
                dialog.catalog_choice.findData("character:rhiannon")
            )
            dialog.save_button.click()
            self.run_task(pool)
            self.run_task(pool)
            saved = dialog.result_settings
            loaded = load_app_settings(
                saved.save(root / "settings.json"), environment={}
            )

            registry = initialize_voice_registry(
                loaded, voice_library=self._voice_library
            )
            self.assertEqual(
                registry.resolve("Narrator").reference.read_bytes(),
                clean_wav_bytes(amplitude=0.2),
            )
            self.assertEqual(
                registry.resolve("Aderyn").reference.read_bytes(),
                expected_reference.read_bytes(),
            )

            content_path = write_content(root / "content")
            records = [
                json.loads(line) for line in content_path.read_text().splitlines()
            ]
            next(
                record
                for record in records
                if record.get("line_id") == "line:rhiannon:1"
            )["voice_character"] = "Aderyn"
            content_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n"
            )
            content = inspect_story_index(content_path)
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(content, ("story",))
            plan = VoicePlanStore(jobs, voice_library=self._voice_library).create(
                job, loaded
            )
            aderyn = next(group for group in plan.groups if group.character == "Aderyn")
            self.assertEqual(aderyn.source_character, "Rhiannon")
            self.assertEqual(
                aderyn.reference_sha256s, (sha256_file(expected_reference),)
            )

    def test_saved_game_voice_is_not_replaced_by_pocket_preset_without_access(self):
        with TemporaryDirectory() as directory:
            manifest = self.narrator_manifest(Path(directory))
            for role in ("Narrator", "Hotelier"):
                with self.subTest(role=role):
                    pool, importer, previews = ManualThreadPool(), Mock(), Mock()
                    settings = self.bind_game_voice(
                        AppSettings(voice_manifest=str(manifest)),
                        manifest,
                        "character:centurion"
                        if role == "Narrator"
                        else "character:rhiannon",
                        role=role,
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
            settings = AppSettings(
                voice_manifest=str(manifest), pocket_gated_model_accepted=True
            )
            plan = narrator_preview_plan(
                settings, settings.voice_manifest, "character:centurion", "Line."
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
            self.assertEqual(dialog.role.findText("???"), -1)
            dialog.cancel_button.click()
            self.run_task(pool)

    def test_global_engine_and_model_are_not_exposed_in_voice_picker(self):
        self.select_preset()
        pool = ManualThreadPool()
        original = AppSettings(
            speech_backend="moss-tts",
            tts_model="custom-moss-model",
            tts_profile="natural",
        )
        dialog = GameNarratorDialog(
            original,
            importer=Mock(),
            preview_service=Mock(),
            thread_pool=pool,
            player=Mock(),
        )
        self.application.processEvents()
        self.assertFalse(hasattr(dialog, "engine_choice"))
        self.assertFalse(hasattr(dialog, "model_choice"))
        self.assertFalse(hasattr(dialog, "model_details"))
        self.assertEqual(dialog.source.currentData(), "preset")
        self.assertFalse(dialog.preview_button.isEnabled())
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertIn("change the engine in Settings", dialog.status.text())
        self.assertEqual(dialog._settings().speech_backend, "moss-tts")
        self.assertEqual(dialog._settings().tts_model, "custom-moss-model")
        self.assertEqual(dialog._settings().tts_profile, "natural")
        dialog.reject()
        self.run_task(pool)
        self.assertIsNone(dialog.result_settings)
        self.assertEqual(
            self._voice_library.binding("Narrator").source_id, "preset:alba"
        )
        self.assertEqual(original.speech_backend, "moss-tts")

    def test_candidate_rows_follow_the_user_journey(self):
        pool = ManualThreadPool()
        dialog = GameNarratorDialog(
            AppSettings(),
            importer=Mock(),
            preview_service=Mock(),
            thread_pool=pool,
            player=Mock(),
        )
        try:

            def row(widget):
                return dialog.form.getWidgetPosition(widget)[0]

            def layout_row(layout):
                return dialog.form.getLayoutPosition(layout)[0]

            self.assertLess(row(dialog.role), row(dialog.source))
            self.assertLess(row(dialog.source), row(dialog.game_controls))
            self.assertLess(row(dialog.game_controls), layout_row(dialog.preview_row))
        finally:
            dialog.reject()
            self.run_task(pool)

    def test_live_recovery_explains_role_and_save_consequence(self):
        pool = ManualThreadPool()
        importer = Mock()
        dialog = GameNarratorDialog(
            AppSettings(),
            importer=importer,
            preview_service=Mock(),
            thread_pool=pool,
            player=Mock(),
        )
        try:
            dialog.set_voice_context(character="Selone")
            dialog._initializing = True
            dialog.characters.addItem("Selone")
            dialog._initializing = False
            dialog.set_recovery_context("Selone", resume_live=True)
            dialog.show()
            self.application.processEvents()

            self.assertTrue(dialog.context_note.isVisibleTo(dialog))
            self.assertIn("Selone", dialog.context_note.text())
            self.assertIn("Live reading is paused", dialog.context_note.text())
            self.assertIn("reading resumes", dialog.context_note.text())
            self.assertIn("live reading stays paused", dialog.context_note.text())
            self.assertIn("Not assigned", dialog.role_summary.text())
            self.assertEqual(dialog.source.currentData(), "game")
            self.assertEqual(dialog.save_button.text(), "Save voice")
            self.assertEqual(dialog.cancel_button.text(), "Back to recovery choices")
            self.assertIn("Back to recovery choices", dialog.context_note.text())
            self.application.processEvents()
            self.assertFalse(pool.tasks)
            importer.narrator_characters.assert_not_called()
            self.assertIn("future speech", dialog.impact_note.text())
            self.assertIn(
                "Existing prepared audio will not change",
                dialog.impact_note.text(),
            )
        finally:
            dialog.reject()
            while pool.tasks:
                self.run_task(pool)

    def test_voice_picker_fields_grow_and_buttons_stay_compact(self):
        pool = ManualThreadPool()
        importer = Mock()
        importer.selected_installation_root.return_value = Path(
            "/Games/Reverse 1999/StreamingAssets/Windows"
        )
        dialog = GameNarratorDialog(
            AppSettings(),
            importer=importer,
            preview_service=Mock(),
            thread_pool=pool,
            player=Mock(),
        )
        try:
            self.assertGreaterEqual(dialog.width(), 900)
            self.assertLessEqual(dialog.height(), 560)
            self.assertEqual(
                dialog.game_installation.text(),
                "/Games/Reverse 1999/StreamingAssets/Windows",
            )
            self.assertTrue(dialog.save_button.isDefault())
            growth = QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            self.assertEqual(dialog.form.fieldGrowthPolicy(), growth)
            self.assertEqual(dialog.game_controls.layout().fieldGrowthPolicy(), growth)
            for choice in dialog.findChildren(type(dialog.role)):
                self.assertEqual(
                    choice.sizePolicy().horizontalPolicy(),
                    QSizePolicy.Policy.Expanding,
                )
            for button in (
                dialog.folder_button,
                dialog.discover_button,
                dialog.original_button,
                dialog.catalog_original_button,
                dialog.preview_button,
                dialog.check_impact,
                dialog.select_affected,
            ):
                self.assertEqual(
                    button.sizePolicy().horizontalPolicy(),
                    QSizePolicy.Policy.Fixed,
                )
            dialog.source.setCurrentIndex(dialog.source.findData("preset"))
            dialog.resize(900, 560)
            dialog.show()
            self.application.processEvents()
            self.assertTrue(dialog.text.tabChangesFocus())
            self.assertEqual(dialog.copy_details.text(), "Copy diagnostics")
            self.assertTrue(dialog.stop_button.sizePolicy().retainSizeWhenHidden())
            self.assertIn("future speech", dialog.impact_note.text())
            self.assertLess(dialog.role_summary.height(), 80)
            self.assertLess(dialog.game_controls.height(), 200)
            self.assertLess(dialog.impact_note.height(), 80)
            scroll_top = dialog.scroll.geometry().top()
            dialog.stop_button.show()
            self.application.processEvents()
            self.assertEqual(dialog.scroll.geometry().top(), scroll_top)
            dialog.stop_button.hide()
            dialog.source.setCurrentIndex(dialog.source.findData("game"))
            self.application.processEvents()
            self.assertTrue(dialog.folder_button.isVisible())
            self.assertTrue(dialog.discover_button.isVisible())
            self.assertEqual(
                {
                    dialog.catalog_original_button.minimumWidth(),
                    dialog.original_button.minimumWidth(),
                    dialog.preview_button.minimumWidth(),
                },
                {dialog.preview_button.minimumWidth()},
            )
            self.assertEqual(
                dialog.original_button.mapTo(dialog.controls, QPoint()).x(),
                dialog.preview_button.mapTo(dialog.controls, QPoint()).x(),
            )
            self.assertEqual(
                dialog.references.mapTo(
                    dialog.controls, QPoint(dialog.references.width(), 0)
                ).x(),
                dialog.text.mapTo(dialog.controls, QPoint(dialog.text.width(), 0)).x(),
            )
            self.assertEqual(
                dialog.references.mapTo(dialog.controls, QPoint()).x(),
                dialog.text.mapTo(dialog.controls, QPoint()).x(),
            )
            for choice in (dialog.role, dialog.characters):
                self.assertGreater(choice.width(), choice.sizeHint().width())
            for button in (
                dialog.copy_details,
                dialog.save_button,
            ):
                if button.isVisible():
                    self.assertEqual(button.width(), button.sizeHint().width())
            self.assertGreaterEqual(
                dialog.cancel_button.width(), dialog.cancel_button.sizeHint().width()
            )
        finally:
            dialog.reject()
            while pool.tasks:
                self.run_task(pool)

    def test_voice_picker_keyboard_navigation_follows_selected_source(self):
        pool = ManualThreadPool()
        dialog = GameNarratorDialog(
            AppSettings(),
            importer=Mock(),
            preview_service=Mock(),
            thread_pool=pool,
            player=Mock(),
        )
        try:
            self.assertFalse(dialog.role.isEditable())
            selected_role = dialog.role.currentText()
            dialog.role.setCurrentText("Invented role")
            self.assertEqual(dialog.role.currentText(), selected_role)
            self.assertEqual(dialog.role.findText("Invented role"), -1)
            dialog.source.setCurrentIndex(dialog.source.findData("preset"))
            dialog.show()
            self.application.processEvents()
            self.assertTrue(dialog.text.tabChangesFocus())
            dialog.source.setFocus()
            self.assertTrue(dialog.focusNextChild())
            self.assertIs(self.application.focusWidget(), dialog.presets)
            dialog.catalog_choice.addItem("Imported voice", "character:imported")
            dialog.source.setCurrentIndex(dialog.source.findData("catalog"))
            self.application.processEvents()
            dialog.source.setFocus()
            self.assertTrue(dialog.focusNextChild())
            self.assertIs(self.application.focusWidget(), dialog.catalog_choice)
            self.assertTrue(dialog.focusNextChild())
            self.assertIs(
                self.application.focusWidget(), dialog.catalog_original_button
            )
        finally:
            dialog.reject()
            while pool.tasks:
                self.run_task(pool)

    def test_custom_preview_is_regenerated_after_candidate_change(self):
        with TemporaryDirectory() as directory:
            self.select_preset("preset:alba")
            pool, previews, player = ManualThreadPool(), Mock(), Mock()
            previews.generate.return_value = SimpleNamespace(
                path=Path(directory) / "preview.wav", reused=False
            )
            dialog = GameNarratorDialog(
                AppSettings(),
                importer=Mock(),
                preview_service=previews,
                thread_pool=pool,
                player=player,
            )
            try:
                self.application.processEvents()
                first_text = "A custom sentence for the selected voice."
                dialog.text.setPlainText(first_text)
                dialog.preview_button.click()
                self.assertEqual(dialog.status.text(), "Generating your preview...")
                self.assertEqual(dialog.preview_button.text(), "Generate preview")
                self.run_task(pool)
                self.assertEqual(dialog.preview_button.text(), "Generate preview")
                first_plan = previews.generate.call_args_list[0].args[0]
                self.assertEqual(first_plan.groups[0].sample_text, first_text)
                self.assertEqual(first_plan.groups[0].source_id, "preset:alba")
                player.playbackStateChanged.connect.call_args.args[0](
                    QtPcmPlayer.PlaybackState.PlayingState
                )

                stops_before_switch = player.stop.call_count
                dialog.presets.setCurrentIndex(dialog.presets.findData("preset:marius"))
                self.assertGreater(player.stop.call_count, stops_before_switch)
                self.assertFalse(dialog._playback_requested)
                self.assertEqual(dialog.status.text(), "Preview playback stopped.")
                player.mediaStatusChanged.connect.call_args.args[0](
                    QtPcmPlayer.MediaStatus.EndOfMedia
                )
                player.errorOccurred.connect.call_args.args[0](
                    player.Error.ResourceError, "Stale preview failure"
                )
                self.assertEqual(dialog.status.text(), "Preview playback stopped.")

                second_text = "A different sentence for the new candidate."
                dialog.text.setPlainText(second_text)
                dialog.preview_button.click()
                player.playbackStateChanged.connect.call_args.args[0](
                    QtPcmPlayer.PlaybackState.PlayingState
                )
                player.mediaStatusChanged.connect.call_args.args[0](
                    QtPcmPlayer.MediaStatus.EndOfMedia
                )
                player.errorOccurred.connect.call_args.args[0](
                    player.Error.ResourceError, "Late old-preview failure"
                )
                self.assertEqual(dialog.status.text(), "Generating your preview...")
                self.assertTrue(dialog._playback_requested)
                self.run_task(pool)
                second_plan = previews.generate.call_args_list[1].args[0]
                self.assertEqual(second_plan.groups[0].sample_text, second_text)
                self.assertEqual(second_plan.groups[0].source_id, "preset:marius")
            finally:
                dialog.reject()
                self.run_task(pool)

    def test_manual_installation_switch_replaces_old_references(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.narrator_manifest(root / "voices")
            first_root = root / "game-a"
            second_root = root / "game-b"
            selected_root = [first_root]
            importer = Mock()
            importer.selected_installation_root.side_effect = lambda: selected_root[0]

            def characters(_cancel, installation_root):
                if installation_root is not None:
                    selected_root[0] = Path(installation_root)
                return ("Centurion" if selected_root[0] == first_root else "Rhiannon",)

            importer.narrator_characters.side_effect = characters
            importer.narrator_references.side_effect = lambda character: (
                SimpleNamespace(
                    line_id=f"{character}:reference",
                    collection_title=f"{character} line",
                    text=f"Transcript from {character}.",
                ),
            )
            importer.prepare_voice_roles.return_value = manifest
            pool = ManualThreadPool()
            dialog = GameNarratorDialog(
                AppSettings(),
                importer=importer,
                preview_service=Mock(),
                thread_pool=pool,
                player=Mock(),
            )
            try:
                self.choose_game_source(dialog)
                self.application.processEvents()
                while pool.tasks:
                    self.run_task(pool)
                self.assertEqual(dialog.characters.currentText(), "Centurion")
                self.assertEqual(dialog.references.currentData(), "Centurion:reference")
                self.assertIsNone(importer.narrator_characters.call_args.args[1])

                dialog.discover()
                while pool.tasks:
                    self.run_task(pool)
                self.assertIsNone(importer.narrator_characters.call_args.args[1])

                dialog.discover(second_root)
                self.assertEqual(dialog.game_installation.text(), str(second_root))
                self.assertEqual(dialog.references.count(), 0)
                while pool.tasks:
                    self.run_task(pool)
                self.assertEqual(dialog.characters.currentText(), "Rhiannon")
                self.assertEqual(dialog.references.currentData(), "Rhiannon:reference")
                self.assertEqual(
                    importer.narrator_characters.call_args.args[1], second_root
                )
            finally:
                dialog.reject()
                self.run_task(pool)

    def test_global_engine_is_preserved_while_consent_is_staged_for_save(self):
        with TemporaryDirectory() as directory:
            manifest = write_manifest(Path(directory))
            importer = self.narrator_importer(manifest)
            pool, previews = ManualThreadPool(), Mock()
            previews.generate.return_value.path = Path(directory) / "preview.wav"
            original = AppSettings(
                speech_backend="pocket-tts",
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
            self.choose_game_source(dialog)
            self.application.processEvents()
            while pool.tasks:
                self.run_task(pool)
            self.assertEqual(dialog.source.currentData(), "game")
            self.assertFalse(dialog.consent.isHidden())
            self.assertFalse(dialog.save_button.isEnabled())
            self.assertTrue(dialog.original_button.isEnabled())
            dialog.consent.setChecked(True)
            self.assertTrue(dialog.save_button.isEnabled())
            self.assertEqual(dialog._settings().speech_backend, "pocket-tts")
            self.assertIsNone(dialog._settings().tts_model)
            self.assertEqual(dialog._settings().tts_profile, "default")
            dialog.preview_button.click()
            for control in (
                dialog.source,
                dialog.presets,
                dialog.consent,
            ):
                self.assertFalse(control.isEnabled())
            self.run_task(pool)
            plan = previews.generate.call_args.args[0]
            self.assertEqual(plan.synthesis_backend, "pocket-tts")
            self.assertIsNone(plan.synthesis_model)
            self.assertIsNone(dialog.result_settings)
            dialog.save_button.click()
            self.run_task(pool)
            self.run_task(pool)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertIsNone(dialog.result_settings.tts_model)
            self.assertIsNone(original.tts_model)
            self.assertFalse(original.pocket_gated_model_accepted)

    def test_unavailable_and_xtts_engines_point_to_global_settings(self):
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
                    dialog._engine_guidance_text(),
                )
                self.assertIn("Settings", dialog._engine_guidance_text())
                self.assertEqual(dialog.source.currentData(), "game")
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
                from r1999extractor.reverse1999_index import index_version

                write_content(root / "reverse1999")
                (root / "audio").mkdir(exist_ok=True)
                (root / "reverse1999" / "english-bank-index.json").write_text(
                    json.dumps(
                        {
                            "version": index_version,
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

            with (
                patch.object(
                    importer, "import_installed", side_effect=importing_game
                ) as importing,
                patch(
                    "vntts.game_content_importer.load_story_index_document",
                    wraps=load_story_index_document,
                ) as parse_index,
            ):
                self.assertEqual(importer.narrator_characters(), ("Centurion",))
                self.assertEqual(importer.narrator_characters(), ("Centurion",))
                self.assertEqual(
                    Reverse1999GameImporter(output_root=root).narrator_characters(),
                    ("Centurion",),
                )
                self.assertEqual(importing.call_count, 1)
                parse_index.assert_called_once()
                (root / "reverse1999" / "narrator-banks.json").write_text(
                    '{"Centurion": "hero3032_mainstory.bnk", "Rhiannon": "other.bnk"}'
                )
                self.assertEqual(
                    importer.narrator_characters(), ("Centurion", "Rhiannon")
                )
                self.assertEqual(parse_index.call_count, 2)
                importer.narrator_characters(installation_root=root / "game")
                self.assertEqual(importing.call_count, 2)
                self.assertEqual(importing.call_args.args[1], root / "game")
                self.assertEqual(parse_index.call_count, 3)

    def test_preparation_character_picker_preselects_target_role(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = AppSettings()
            content = inspect_story_index(write_content(root / "content"))
            preparation = Mock(
                settings=settings,
                job_store=Mock(),
                voice_decisions=Mock(),
            )
            preparation.has_pending_work.return_value = False
            preparation.voice_plan.return_value = None
            preparation.current_content.return_value = content
            preparation.selected_story_ids.return_value = tuple(
                item.selection_id for item in content.selections
            )
            pool = ManualThreadPool()
            picker = GameNarratorDialog(
                settings,
                importer=Mock(),
                preview_service=Mock(),
                thread_pool=pool,
                player=Mock(),
            )
            tray = TrayApplication(
                self.application,
                settings,
                controller_factory=Mock(
                    return_value=Mock(is_ready=False, is_live_running=False)
                ),
            )
            tray.pregeneration_dialog = preparation
            try:
                with patch("vntts.app.GameNarratorDialog", return_value=picker):
                    tray._open_preparation_narrator(
                        settings, tray.dashboard, character="Aderyn"
                    )
                    self.application.processEvents()
                    self.assertIs(tray.narrator_dialog, picker)
                    self.assertEqual(picker.role.currentText(), "Aderyn")

                    picker.role.setCurrentText("Narrator")
                    tray.open_voice_previews(character="Aderyn")
                    self.assertEqual(picker.role.currentText(), "Aderyn")
                    tray.open_voice_previews(character="New Story Speaker")
                    self.assertEqual(picker.role.currentText(), "New Story Speaker")
                    self.assertGreaterEqual(
                        picker.role.findText("New Story Speaker"), 0
                    )
                    picker.importer.narrator_characters.assert_not_called()
                    picker.previews.generate.assert_not_called()
            finally:
                tray.shutdown()

    def test_main_and_preparation_persist_only_an_accepted_selection(self):
        for decision in ("save", "cancel", "save-failure"):
            with self.subTest(decision=decision), TemporaryDirectory() as directory:
                self.select_preset("preset:alba")
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
                    binding = self._voice_library.binding("Narrator")
                    if binding is not None and binding.source_id == "preset:marius":
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
                        self.assertGreaterEqual(len(voice_saves), 1)
                        self.assertEqual(
                            self._voice_library.binding("Narrator").source_id,
                            "preset:marius",
                        )
                        self.assertEqual(
                            preparation.settings.updated(
                                last_main_section=tray.settings.last_main_section
                            ),
                            tray.settings,
                        )
                        self.assertIn("Marius", preparation.narrator_status.text())
                        reload.assert_called_once()
                        self.assertEqual(reload.call_args.args[1], "Narrator: Marius")
                    else:
                        self.assertEqual(bool(voice_saves), decision == "save-failure")
                        self.assertEqual(preparation.settings, original)
                        self.assertEqual(
                            self._voice_library.binding("Narrator").source_id,
                            "preset:alba",
                        )
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

    def test_ui_save_persists_selected_reference_over_retained_short_base_voice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = self.narrator_manifest(root / "base")
            old_reference = base.parent / "references" / "centurion.wav"
            old_reference.write_bytes(clean_wav_bytes(seconds=0.06075))
            first = self.narrator_manifest(root / "first")
            selected = self.narrator_manifest(root / "selected")
            selected_reference = selected.parent / "references" / "centurion.wav"
            selected_reference.write_bytes(clean_wav_bytes(amplitude=0.3, seconds=4))
            self.narrator_importer(selected)
            importer = self.narrator_importer(first)
            importer.prepare_voice_roles.side_effect = lambda *_args, **kwargs: (
                selected
                if kwargs["narrator_line_id"] == "playable-voice:3032:5"
                else first
            )
            pool, player = ManualThreadPool(), Mock()
            dialog = GameNarratorDialog(
                AppSettings(speech_backend="moss-tts", voice_manifest=str(base)),
                importer=importer,
                preview_service=VoiceAuditionPreviewService(root / "previews"),
                thread_pool=pool,
                player=player,
                binder=partial(
                    bind_voice_library_selection, root=self._voice_library.root
                ),
            )
            self.application.processEvents()
            dialog.source.setCurrentIndex(dialog.source.findData("game"))
            if not pool.tasks:
                dialog.discover_button.click()
            self.run_task(pool)
            self.run_task(pool)
            dialog.references.setCurrentIndex(4)
            dialog.original_button.click()
            while pool.tasks:
                self.run_task(pool)
            self.assertEqual(
                player.play_bytes.call_args.args[0],
                selected_reference.read_bytes(),
            )
            dialog.save_button.click()
            self.run_task(pool)
            self.run_task(pool)
            dialog.result_settings.save(root / "settings.json")
            self.assertEqual(
                self._voice_library.resolve_source_path("Narrator").read_bytes(),
                selected_reference.read_bytes(),
            )

    def test_preparation_extracts_story_voices_after_fresh_narrator_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_content(root / "content"))
            manifest = self.narrator_manifest(root / "narrators")
            selected = bind_voice_library_selection(
                AppSettings(pocket_gated_model_accepted=True),
                manifest,
                "character:centurion",
                "Centurion",
                root=self._voice_library.root,
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
            plan = dialog._create_voice_plan(job)
            importer.prepare_voice_candidates.assert_called_once()
            self.assertTrue(any(group.candidates for group in plan.groups))
            narrator = initialize_voice_registry(
                selected, voice_library=self._voice_library
            ).resolve("Narrator")
            self.assertEqual(narrator.source_character, "Centurion")
            self.assertNotEqual(plan.voice_manifest, str(candidates))
            dialog.reject()

    def test_preparation_extracts_story_voices_with_plain_narrator_assignment(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_content(root / "content"))
            manifest = write_manifest(root / "configured", rhiannon=clean_wav_bytes())
            document = json.loads(manifest.read_text(encoding="utf-8"))
            narrator_reference = manifest.parent / "references" / "narrator.wav"
            narrator_reference.write_bytes(clean_wav_bytes(amplitude=0.2))
            document["voices"].append(
                {
                    "character": "Narrator",
                    "speaker": "configured-narrator",
                    "aliases": [],
                    "references": ["references/narrator.wav"],
                }
            )
            manifest.write_text(json.dumps(document), encoding="utf-8")
            candidates = write_player_candidate_manifest(
                root / "story-candidates", content.story_index_sha256
            )
            importer = Mock()
            importer.prepare_voice_candidates.return_value = candidates
            importer.availability.return_value = Mock(
                available=True, message="Available"
            )
            settings = AppSettings(
                voice_manifest=str(manifest),
                pocket_gated_model_accepted=True,
            )
            settings = bind_voice_library_selection(
                settings,
                manifest,
                "character:narrator",
                "Narrator",
                root=self._voice_library.root,
            )
            settings = bind_voice_library_selection(
                settings,
                manifest,
                "character:rhiannon",
                "Rhiannon",
                root=self._voice_library.root,
                target_character="Rhiannon",
            )
            jobs = PregenerationJobStore(root / "jobs")
            dialog = OfflineAudioPreparationDialog(
                settings,
                importer=importer,
                job_store=jobs,
                discovery=lambda: ContentDiscovery((content,)),
                thread_pool=ManualThreadPool(),
            )
            job = jobs.create_or_resume(content, ["story"])

            plan = dialog._create_voice_plan(job)

            importer.prepare_voice_candidates.assert_called_once()
            group = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            self.assertEqual(len(group.candidate_inventory), 3)
            self.assertEqual(
                initialize_voice_registry(
                    settings.updated(voice_manifest=plan.voice_manifest),
                    voice_library=self._voice_library,
                )
                .resolve("Narrator")
                .speaker,
                "configured-narrator",
            )
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
            manifest = self.narrator_manifest(Path(directory))
            importer = self.narrator_importer(manifest)
            long_transcript = " ".join(
                ["A deliberately long original transcript remains readable."] * 12
            )
            references = list(importer.narrator_references.return_value)
            references[-1] = SimpleNamespace(
                line_id=references[-1].line_id,
                collection_title=(
                    "A very long chapter and scene title for the original recording"
                ),
                text=long_transcript,
            )
            importer.narrator_references.return_value = tuple(references)
            pool = ManualThreadPool()
            dialog = GameNarratorDialog(
                self.bind_game_voice(
                    AppSettings(speech_backend="pocket-tts"),
                    manifest,
                    "character:centurion",
                ),
                importer=importer,
                preview_service=Mock(),
                thread_pool=pool,
                player=Mock(),
            )
            self.choose_game_source(dialog)
            self.application.processEvents()
            self.run_task(pool)
            self.run_task(pool)
            self.assertEqual(dialog.references.count(), 5)
            dialog.references.setCurrentIndex(4)
            importer.prepare_voice_roles.assert_not_called()
            self.assertEqual(
                dialog.references.currentText(),
                "A very long chapter and scene title for the original recording",
            )
            self.assertEqual(
                dialog.references.toolTip(), dialog.references.currentText()
            )
            self.assertEqual(dialog.reference_text.text(), long_transcript)
            dialog.show()
            self.application.processEvents()
            dialog.original_button.click()
            self.run_task(pool)  # Finish the first selection, which must not play.
            dialog.player.play_bytes.assert_not_called()
            self.run_task(pool)  # Prepare the latest selection.
            self.run_task(pool)  # Play the queued selection.
            dialog.player.play_bytes.assert_called_once()
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
            self.assertEqual(dialog.player.play_bytes.call_count, 3)
            dialog.consent.setChecked(True)
            dialog.original_button.setFocus()
            self.assertTrue(dialog.focusNextChild())
            self.assertIs(self.application.focusWidget(), dialog.consent)
            self.assertTrue(dialog.focusNextChild())
            self.assertIs(self.application.focusWidget(), dialog.text)
            self.assertTrue(dialog.focusNextChild())
            self.assertIs(self.application.focusWidget(), dialog.preview_button)
            dialog.characters.clear()
            self.assertEqual(dialog.reference_text.text(), "")
            dialog.reject()
            self.run_task(pool)

    def test_voice_choices_signal_only_saved_setting_changes_and_copy_details(self):
        with TemporaryDirectory() as directory:
            manifest = self.narrator_manifest(Path(directory))
            pool = ManualThreadPool()
            dialog = GameNarratorDialog(
                self.bind_game_voice(AppSettings(), manifest, "character:centurion"),
                importer=self.narrator_importer(manifest),
                preview_service=Mock(),
                thread_pool=pool,
                player=Mock(),
            )
            self.choose_game_source(dialog)
            changed = []
            dialog.settingsChanged.connect(changed.append)
            self.application.processEvents()
            self.run_task(pool)
            self.run_task(pool)

            self.assertEqual(changed, [])
            self.assertTrue(
                dialog.status.textInteractionFlags()
                & Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.assertTrue(
                dialog.reference_text.textInteractionFlags()
                & Qt.TextInteractionFlag.TextSelectableByKeyboard
            )

            dialog.source.setCurrentIndex(dialog.source.findData("preset"))
            self.assertEqual(changed, [True])
            dialog.source.setCurrentIndex(dialog.source.findData("game"))
            self.assertEqual(changed, [True, False])
            dialog.references.setCurrentIndex(1)
            self.assertEqual(changed, [True, False, True])
            self.assertIn("Source ID: playable-voice:3032:2", dialog._copy_details())
            dialog.reject()
            self.run_task(pool)

    def test_decoder_setup_consent_retries_in_worker_and_keeps_controls_gated(self):
        with TemporaryDirectory() as directory:
            manifest = self.narrator_manifest(Path(directory) / "candidates")
            importer = self.narrator_importer(manifest)
            importer.prepare_voice_roles.side_effect = [
                DecoderSetupRequired("Install decoder?"),
                manifest,
            ]
            pool = ManualThreadPool()
            dialog = GameNarratorDialog(
                self.bind_game_voice(AppSettings(), manifest, "character:centurion"),
                importer=importer,
                preview_service=Mock(),
                thread_pool=pool,
                player=Mock(),
            )
            self.choose_game_source(dialog)
            self.application.processEvents()
            self.run_task(pool)
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
                self.run_task(pool)
                self.assertEqual(dialog._operation, "warm")
                self.assertTrue(dialog.references.isEnabled())
                self.assertTrue(dialog.original_button.isEnabled())
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
                player.play_bytes.assert_not_called()
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
            self.choose_game_source(dialog)
            self.application.processEvents()
            self.run_task(pool)
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
            importer = self.narrator_importer(self.narrator_manifest(Path(directory)))
            pool, player = ManualThreadPool(), Mock()
            dialog = GameNarratorDialog(
                AppSettings(speech_backend="moss-tts"),
                importer=importer,
                preview_service=Mock(),
                thread_pool=pool,
                player=player,
            )
            self.choose_game_source(dialog)
            self.application.processEvents()
            self.run_task(pool)
            self.run_task(pool)
            self.run_task(pool)
            dialog.references.setCurrentIndex(4)
            dialog.references.setCurrentIndex(0)
            dialog.original_button.click()
            self.run_task(pool)
            self.run_task(pool)
            player.play_bytes.assert_called_once()
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
            self.choose_game_source(dialog)
            self.application.processEvents()
            self.run_task(pool)
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
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.narrator_manifest(root / "candidates")
            importer = self.narrator_importer(manifest)
            previews = Mock()
            previews.generate.return_value.path = root / "preview.wav"
            pool = ManualThreadPool()
            player = Mock()
            dialog = GameNarratorDialog(
                self.bind_game_voice(AppSettings(), manifest, "character:centurion"),
                importer=importer,
                preview_service=previews,
                thread_pool=pool,
                player=player,
                binder=partial(
                    bind_voice_library_selection, root=self._voice_library.root
                ),
            )
            self.choose_game_source(dialog)
            self.application.processEvents()
            self.assertFalse(dialog.controls.isEnabled())
            self.assertFalse(dialog.progress.isHidden())
            self.run_task(pool)
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
            player.play_bytes.assert_called_once()
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
                initialize_voice_registry(
                    dialog.result_settings, voice_library=self._voice_library
                )
                .resolve("Narrator")
                .source_character,
                "Centurion",
            )
            previews.close.assert_called_once()

    def test_moss_picker_uses_openmoss_default_on_non_apple_silicon(self):
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
            self.choose_game_source(dialog)
            self.application.processEvents()
            self.run_task(pool)
            self.run_task(pool)
            self.assertFalse(hasattr(dialog, "engine"))
            self.assertTrue(dialog.terms.isHidden())
            self.assertTrue(dialog.consent.isHidden())
            self.assertTrue(dialog.preview_button.isEnabled())
            dialog.preview_button.click()
            self.run_task(pool)
            self.run_task(pool)
            plan = previews.generate.call_args.args[0]
            self.assertEqual(plan.synthesis_backend, "moss-tts")
            self.assertIsNone(plan.synthesis_model)
            self.assertEqual(plan.synthesis_profile, "natural")
            self.assertIn("MOSS runtime unavailable", dialog.status.text())
            self.assertEqual(dialog._settings().speech_backend, "moss-tts")
            self.assertEqual(previews.generate.call_count, 1)
            dialog.reject()
            self.run_task(pool)

    def test_cancel_discards_late_discovery_and_closes_worker(self):
        with TemporaryDirectory() as directory:
            manifest = self.narrator_manifest(Path(directory))
            settings = self.bind_game_voice(
                AppSettings(), manifest, "character:centurion"
            )
            pool = ManualThreadPool()
            importer = Mock()
            importer.narrator_characters.return_value = ("Centurion",)
            previews = Mock()
            dialog = GameNarratorDialog(
                settings,
                importer=importer,
                preview_service=previews,
                thread_pool=pool,
                player=Mock(),
            )
            self.choose_game_source(dialog)
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
        with TemporaryDirectory() as directory:
            manifest = self.narrator_manifest(Path(directory))
            settings = self.bind_game_voice(
                AppSettings(), manifest, "character:centurion"
            )
            pool = ManualThreadPool()
            importer = Mock()
            importer.narrator_characters.side_effect = ValueError("Game not found")
            dialog = GameNarratorDialog(
                settings,
                importer=importer,
                preview_service=Mock(),
                thread_pool=pool,
                player=Mock(),
            )
            self.choose_game_source(dialog)
            self.application.processEvents()
            self.run_task(pool)
            self.assertIn("Game not found", dialog.status.text())
            self.assertTrue(dialog.folder_button.isEnabled())
            self.assertTrue(dialog.controls.isEnabled())
            self.assertFalse(dialog.save_button.isEnabled())
            dialog.reject()
            self.run_task(pool)
