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

from tests.test_chapter_voice_preload import write_verified_source_story
from tests.test_generated_audio import FakeAudioOutput
from tests.test_pregeneration_pack import fixture
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
from vntts.settings import AppSettings


def published_pack(root):
    job, generation_input, generation_result, _items = fixture(root)
    return OfflinePackPublisher().publish(job, generation_input, generation_result)


class OfflinePackActivatorTest(unittest.TestCase):
    def test_mixed_pack_activation_routes_original_and_generated_without_live_synthesis(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, inputs, result, _items = fixture(root)
            story = load_story_index_document(inputs.story_index)
            write_verified_source_story(inputs.story_index)
            verified = load_story_index_document(inputs.story_index)
            original = verified.records[0]
            write_story_index_document(
                inputs.story_index,
                verified.metadata,
                [
                    *(record.to_record() for record in story.records),
                    original.to_record(),
                ],
            )
            inputs = replace(
                inputs,
                source_audio_semantic_evidence=(
                    inputs.story_index.parent / "source-audio-semantic-evidence.json"
                ),
            )
            job = replace(
                job,
                story_index_sha256=sha256_file(inputs.story_index),
                selected_line_ids=(*job.selected_line_ids, original.line_id),
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
            source = backend.prepare_route(original.speaker, original.text)
            generated = backend.prepare_route("Narrator", "Prepared line generated.")
            self.assertIsInstance(source, SourceAudioRoute)
            self.assertGreater(source.prepared.completion_seconds, 1.25)
            self.assertIsInstance(generated, GeneratedAudioRoute)
            live.prepare_playback.assert_not_called()

    def test_activation_uses_saved_audio_instead_of_previous_live_overrides(self):
        from vntts.controller import AppController

        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack = published_pack(root)
            current = AppSettings(force_live_narrator=True)
            controller = Mock(is_ready=True)
            controller.apply_settings.return_value = True
            controller.start.return_value = True
            activator = OfflinePackActivator(
                save_settings=lambda _settings: root / "settings.json"
            )
            result = activator.activate(current, pack, controller)
            self.assertFalse(result.settings.force_live_narrator)
            live = AppController(result.settings)
            self.assertFalse(live._has_manual_voice_override("Narrator"))
            self.assertFalse(live._has_manual_voice_override("Hotelier"))
            self.assertFalse(live._has_manual_voice_override("Other story speaker"))

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

            with patch(
                "vntts.pregeneration_activation.record_background_operation"
            ) as record:
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
        self.assertEqual(
            [call.args[0] for call in record.call_args_list],
            [
                "pregeneration-activation-pack-preflight",
                "pregeneration-activation-settings-build",
                "pregeneration-activation-runtime-apply",
                "pregeneration-activation-settings-save",
            ],
        )

    def test_new_choices_use_pack_narrator_and_rollback_keeps_old_settings(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pack = published_pack(root)
            previous = AppSettings(game_pack="previous-pack.json")
            selected = previous.updated(
                pocket_gated_model_accepted=True,
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
