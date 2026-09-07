import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import (
    load_story_index_document,
    write_story_index_document,
)

from tests.test_generated_audio import FakeAudioOutput
from tests.test_pregeneration_pack import fixture
from tests.test_pregeneration_voices import write_manifest
from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioLibrary,
    GeneratedAudioRoute,
    SourceAudioRoute,
)
from vntts.pregeneration_activation import (
    OfflinePackActivationError,
    OfflinePackActivator,
)
from vntts.pregeneration_pack import OfflinePackPublisher
from vntts.runtime_config import initialize_voice_registry
from vntts.settings import AppSettings, load_app_settings
from vntts.voices import CharacterVoiceRegistry


def published_pack(root):
    job, generation_input, generation_result, _items = fixture(root)
    return OfflinePackPublisher().publish(job, generation_input, generation_result)


class OfflinePackActivatorTest(unittest.TestCase):
    def test_activation_retains_selected_and_unrelated_character_reference_defaults(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack = published_pack(root / "pack")
            sources = write_manifest(root / "sources")
            original = AppSettings(
                pocket_gated_model_accepted=True,
                voice_manifest=str(sources),
                character_voice_defaults={
                    "Hotelier": "character:centurion",
                    "Unrelated story": "character:rhiannon",
                    "Narrator fallback role": "default",
                    "Built-in role": "preset:anna",
                },
            )
            controller = Mock(is_ready=False)
            controller.apply_settings.return_value = True
            activator = OfflinePackActivator(
                save_settings=lambda value: value.save(root / "settings.json")
            )
            with patch(
                "vntts.game_narrator.get_local_data_directory",
                return_value=root / "local",
            ):
                result = activator.activate(original, pack, controller)
            loaded = load_app_settings(root / "settings.json", environment={})
            self.assertEqual(loaded.voice_manifest, result.settings.voice_manifest)
            self.assertEqual(
                loaded.character_voice_defaults,
                result.settings.character_voice_defaults,
            )
            registry = initialize_voice_registry(loaded)
            self.assertEqual(
                registry.resolve("Hotelier").reference.read_bytes(), b"centurion"
            )
            self.assertEqual(
                registry.resolve("Unrelated story").reference.read_bytes(), b"rhiannon"
            )
            self.assertIsNone(registry.resolve("Narrator fallback role"))
            self.assertEqual(registry.resolve("Built-in role").speaker, "anna")
            self.assertEqual(
                original.character_voice_defaults["Hotelier"], "character:centurion"
            )
            controller.reset_mock()
            missing = original.updated(
                character_voice_defaults={"Hotelier": "character:missing"}
            )
            with self.assertRaisesRegex(
                OfflinePackActivationError, "retain saved character voices"
            ):
                activator.activate(missing, pack, controller)
            controller.shutdown.assert_not_called()
            controller.apply_settings.assert_not_called()
            self.assertEqual(
                load_app_settings(root / "settings.json", environment={}), loaded
            )

    def test_mixed_pack_activation_routes_original_and_generated_without_live_synthesis(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, inputs, result, _items = fixture(root)
            story = load_story_index_document(inputs.story_index)
            original = {
                "record_type": "line",
                "line_id": "pack:original",
                "chapter": "1",
                "sequence": 3,
                "speaker": "Ada",
                "text": "Original spoken line.",
                "kind": "dialogue",
                "speakable": True,
                "source_audio_status": "available",
                "source_audio_id": "voice-7",
                "source_audio_duration_seconds": 2.75,
                "source_audio_completeness": "full",
            }
            write_story_index_document(
                inputs.story_index,
                {
                    "game": story.game,
                    "language": story.language,
                    "source_audio_completion": "duration-seconds",
                },
                [*(record.to_record() for record in story.records), original],
            )
            job = replace(
                job,
                story_index_sha256=sha256_file(inputs.story_index),
                selected_line_ids=(*job.selected_line_ids, "pack:original"),
            )
            pack = OfflinePackPublisher().publish(job, inputs, result)
            controller = Mock(is_ready=False)
            controller.apply_settings.return_value = True
            activated = OfflinePackActivator(
                save_settings=lambda _settings: root / "settings.json"
            ).activate(AppSettings(), pack, controller)
            self.assertEqual(
                activated.settings.audio_source_policy, "prefer-game-audio"
            )
            live = Mock()
            live.name = "unused-live"
            backend = GeneratedAudioFallbackBackend(
                live,
                GeneratedAudioLibrary.load_optional(
                    activated.settings.generated_audio_manifest
                ),
                ChapterVoicePreloader.load_optional(activated.settings.story_index),
                audio_source_policy=activated.settings.audio_source_policy,
                audio_output=FakeAudioOutput(),
            )
            backend.set_live_mode_active(True)
            source = backend.prepare_route("Ada", "Original spoken line.")
            generated = backend.prepare_route("Narrator", "Prepared line generated.")
            self.assertIsInstance(source, SourceAudioRoute)
            self.assertGreater(source.prepared.completion_seconds, 2.75)
            self.assertIsInstance(generated, GeneratedAudioRoute)
            live.prepare_playback.assert_not_called()

    def test_activation_uses_saved_audio_instead_of_previous_live_overrides(self):
        from types import SimpleNamespace

        from vntts.controller import AppController

        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack = published_pack(root)
            current = AppSettings(
                force_live_narrator=True,
                voice_assignments={
                    "Narrator": "preset:marius",
                    "Hotelier": "preset:alba",
                    "Other story speaker": "preset:anna",
                },
            )
            controller = Mock(is_ready=True)
            controller.apply_settings.return_value = True
            controller.start.return_value = True
            activator = OfflinePackActivator(
                save_settings=lambda _settings: root / "settings.json"
            )
            with patch(
                "vntts.pregeneration_activation.load_story_index_document",
                return_value=SimpleNamespace(
                    records=(SimpleNamespace(speaker="Hotelier"),),
                ),
            ):
                result = activator.activate(current, pack, controller)
            self.assertFalse(result.settings.force_live_narrator)
            self.assertNotIn("Hotelier", result.settings.voice_assignments)
            self.assertEqual(
                result.settings.voice_assignments["Other story speaker"], "preset:anna"
            )
            live = AppController(result.settings)
            self.assertFalse(live._has_manual_voice_override("Narrator"))
            self.assertFalse(live._has_manual_voice_override("Hotelier"))
            self.assertTrue(live._has_manual_voice_override("Other story speaker"))

    def test_restarts_runtime_before_committing_generated_first_settings(self):
        with TemporaryDirectory() as temporary_directory:
            pack = published_pack(Path(temporary_directory))
            saved = []
            controller = Mock(is_ready=True)
            controller.apply_settings.return_value = True
            controller.start.return_value = True
            activator = OfflinePackActivator(
                save_settings=lambda settings: (
                    saved.append(settings)
                    or Path(temporary_directory) / "settings.json"
                )
            )

            result = activator.activate(AppSettings(), pack, controller)

        self.assertEqual(result.settings.audio_source_policy, "prefer-generated")
        self.assertEqual(result.settings.game_pack, str(pack.manifest))
        self.assertEqual(
            result.settings.generated_audio_manifest,
            str(pack.imported.generated_audio_manifest),
        )
        self.assertTrue(result.restarted_runtime)
        self.assertEqual(saved, [result.settings])
        controller.shutdown.assert_called_once_with()
        controller.apply_settings.assert_called_once_with(
            result.settings,
            cancellation=None,
        )
        controller.start.assert_called_once_with()

    def test_new_choices_use_pack_narrator_and_rollback_keeps_old_settings(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pack = published_pack(root)
            previous = AppSettings(game_pack="previous-pack.json")
            selected = previous.updated(
                pocket_gated_model_accepted=True,
                voice_assignments={"Narrator": "character:centurion"},
                tts_speaker_wav="old-narrator.wav",
            )
            for failure in (None, "start", "save", "apply"):
                with self.subTest(failure=failure):
                    controller = Mock(is_ready=failure != "apply")
                    controller.apply_settings.side_effect = (
                        [RuntimeError("apply failed"), True]
                        if failure == "apply"
                        else None
                    )
                    controller.start.side_effect = (
                        [False, True] if failure == "start" else None
                    )
                    controller.start.return_value = True
                    save = Mock(return_value=root / "settings.json")
                    if failure == "save":
                        save.side_effect = OSError("disk full")
                    activator = OfflinePackActivator(save_settings=save)
                    if failure:
                        with self.assertRaises(OfflinePackActivationError):
                            activator.activate(
                                previous, pack, controller, generation_settings=selected
                            )
                        self.assertEqual(
                            controller.apply_settings.call_args.args, (previous,)
                        )
                    else:
                        result = activator.activate(
                            previous, pack, controller, generation_settings=selected
                        )
                        self.assertTrue(result.settings.pocket_gated_model_accepted)
                        self.assertIsNone(result.settings.tts_speaker_wav)
                        registry = CharacterVoiceRegistry.from_file(
                            result.settings.voice_manifest
                        )
                        narrator = registry.resolve_source(
                            result.settings.voice_assignments["Narrator"]
                        )
                        self.assertEqual(narrator, registry.resolve("Narrator"))

    def test_save_failure_restores_the_previous_running_pack(self):
        with TemporaryDirectory() as temporary_directory:
            pack = published_pack(Path(temporary_directory))
            previous = AppSettings(game_pack="previous-pack.json")
            controller = Mock(is_ready=True)
            controller.apply_settings.return_value = True
            controller.start.return_value = True
            activator = OfflinePackActivator(
                save_settings=Mock(side_effect=OSError("disk full"))
            )

            with self.assertRaisesRegex(OfflinePackActivationError, "disk full"):
                activator.activate(previous, pack, controller)

        self.assertEqual(controller.shutdown.call_count, 2)
        self.assertEqual(controller.start.call_count, 2)
        self.assertEqual(controller.apply_settings.call_args_list[-1].args, (previous,))

    def test_failed_candidate_start_restores_previous_runtime_without_saving(self):
        with TemporaryDirectory() as temporary_directory:
            pack = published_pack(Path(temporary_directory))
            previous = AppSettings(game_pack="previous-pack.json")
            controller = Mock(is_ready=True)
            controller.apply_settings.return_value = True
            controller.start.side_effect = [False, True]
            save = Mock()

            with self.assertRaisesRegex(
                OfflinePackActivationError,
                "could not start",
            ):
                OfflinePackActivator(save_settings=save).activate(
                    previous,
                    pack,
                    controller,
                )

        save.assert_not_called()
        self.assertEqual(controller.shutdown.call_count, 2)
        self.assertEqual(controller.apply_settings.call_args_list[-1].args, (previous,))

    def test_shutdown_rollback_does_not_restart_the_previous_runtime(self):
        with TemporaryDirectory() as temporary_directory:
            pack = published_pack(Path(temporary_directory))
            previous = AppSettings(game_pack="previous-pack.json")
            controller = Mock(is_ready=True)
            controller.apply_settings.return_value = True
            controller.start.return_value = True
            restart_previous = Event()
            restart_previous.clear()

            with self.assertRaisesRegex(OfflinePackActivationError, "disk full"):
                OfflinePackActivator(
                    save_settings=Mock(side_effect=OSError("disk full"))
                ).activate(
                    previous,
                    pack,
                    controller,
                    restart_previous=restart_previous,
                )

        self.assertEqual(controller.start.call_count, 1)
        self.assertEqual(controller.apply_settings.call_args_list[-1].args, (previous,))


if __name__ == "__main__":
    unittest.main()
