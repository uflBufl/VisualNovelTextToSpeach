import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

import numpy as np
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import (
    load_story_index_document,
    write_story_index_document,
)

from tests.audio_output_fixtures import FakeAudioOutput
from tests.bulk_generation_fixtures import audio_samples
from tests.pregeneration_fixtures import fixture
from tests.story_fixtures import write_verified_source_story
from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.controller import AppController
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioLibrary,
    GeneratedAudioRoute,
    PlaybackStatus,
    SourceAudioRoute,
)
from vntts.pregeneration_activation import (
    OfflinePackActivationError,
    OfflinePackActivator,
)
from vntts.pregeneration_pack import OfflinePackPublisher, OfflinePackResult
from vntts.settings import AppSettings


def published_pack(root):
    job, generation_input, generation_result, _items = fixture(root)
    return OfflinePackPublisher().publish(job, generation_input, generation_result)


class OfflinePackActivatorTest(unittest.TestCase):
    def test_safe_pack_sequence_uses_audio_auto_only_when_auto_advance_is_enabled(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pack = OfflinePackResult(
                identity="a" * 64,
                directory=root,
                manifest=root / "game-pack.json",
                imported=Mock(),
                approved=0,
                live_fallbacks=0,
            )
            imported = Mock()
            imported.pack.extensions = {
                "vntts.self-service": {"identity": pack.identity}
            }
            imported.live_sequence_plan = root / "story" / "live-sequence.json"
            imported.apply_to.side_effect = lambda settings: settings.updated(
                game_pack=str(pack.manifest)
            )
            controller = Mock(is_ready=False)
            controller.apply_settings.return_value = True
            for enabled, expected in ((True, "audio-auto"), (False, "off")):
                with (
                    self.subTest(auto_advance_enabled=enabled),
                    patch(
                        "vntts.pregeneration_activation.import_game_pack",
                        return_value=imported,
                    ),
                    patch.object(
                        ChapterVoicePreloader,
                        "load_optional",
                        return_value=Mock(dialogue=()),
                    ),
                ):
                    result = OfflinePackActivator(
                        save_state=lambda _settings, _pack: root / "settings.json"
                    ).activate(
                        AppSettings(
                            live_sequence_mode="off",
                            auto_advance_enabled=enabled,
                        ),
                        pack,
                        controller,
                    )
                    self.assertEqual(result.settings.live_sequence_mode, expected)

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
                story_index_sha256=sha256_file(inputs.story_index),
                source_audio_semantic_evidence=(
                    inputs.story_index.parent / "source-audio-semantic-evidence.json"
                ),
                source_audio_semantic_evidence_sha256=sha256_file(
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
                save_state=lambda _settings, _pack: root / "settings.json"
            ).activate(AppSettings(force_live_narrator=True), pack, controller)
            self.assertEqual(
                activated.settings.audio_source_policy, "prefer-game-audio"
            )
            live = Mock()
            live.name = "unused-live"
            output = FakeAudioOutput()
            output.query_devices = Mock(return_value={"default_samplerate": 16_000})
            backend = GeneratedAudioFallbackBackend(
                live,
                GeneratedAudioLibrary.load_optional(
                    activated.settings.generated_audio_manifest
                ),
                ChapterVoicePreloader.load_optional(activated.settings.story_index),
                audio_source_policy=activated.settings.audio_source_policy,
                audio_output=output,
            )
            backend.voice_override = AppController(
                activated.settings
            )._has_manual_voice_override
            backend.set_live_mode_active(True)
            source = backend.prepare_route(original.speaker, original.text)
            generated = backend.prepare_route("Narrator", "Prepared line generated.")
            self.assertIsInstance(source, SourceAudioRoute)
            self.assertGreater(source.prepared.completion_seconds, 1.25)
            self.assertIsInstance(generated, GeneratedAudioRoute)
            outcome = backend.play_route(generated)
            self.assertEqual(outcome.status, PlaybackStatus.COMPLETED)
            self.assertEqual(len(output.plays), 1)
            played, sample_rate, _options = output.plays[0]
            self.assertEqual(sample_rate, 16_000)
            np.testing.assert_allclose(played, audio_samples(), atol=1 / 32768)
            live.prepare_playback.assert_not_called()
            live.play_prepared.assert_not_called()

    def test_restarts_runtime_before_committing_playback_state(self):
        with TemporaryDirectory() as temporary_directory:
            pack = published_pack(Path(temporary_directory))
            previous = AppSettings(
                speech_backend="moss-tts", tts_speaker_wav="live-narrator.wav"
            )
            saved = []
            controller = Mock(is_ready=True)
            controller.apply_settings.return_value = True
            controller.start.return_value = True

            def save_state(settings, _pack):
                controller.start.assert_called_once_with()
                saved.append(settings)
                return Path(temporary_directory) / "settings.json"

            result = OfflinePackActivator(save_state=save_state).activate(
                previous, pack, controller
            )

        self.assertEqual(result.settings.audio_source_policy, "prefer-generated")
        self.assertEqual(result.settings.game_pack, str(pack.manifest))
        self.assertEqual(
            result.settings.generated_audio_manifest,
            str(pack.imported.generated_audio_manifest),
        )
        self.assertTrue(result.restarted_runtime)
        self.assertEqual(saved, [previous])
        self.assertEqual(result.settings.speech_backend, "moss-tts")
        self.assertEqual(result.settings.tts_speaker_wav, "live-narrator.wav")
        controller.shutdown.assert_called_once_with()
        controller.apply_settings.assert_called_once_with(
            result.settings,
            cancellation=None,
        )

    def test_failed_apply_restores_previous_settings_without_saving(self):
        with TemporaryDirectory() as directory:
            pack = published_pack(Path(directory))
            previous = AppSettings(game_pack="previous-pack.json")
            controller = Mock(is_ready=False)
            controller.apply_settings.side_effect = [RuntimeError("apply failed"), True]
            save = Mock()

            with self.assertRaisesRegex(OfflinePackActivationError, "apply failed"):
                OfflinePackActivator(save_state=save).activate(
                    previous, pack, controller
                )

            save.assert_not_called()
            self.assertEqual(controller.apply_settings.call_args.args, (previous,))
            controller.start.assert_not_called()

    def test_save_failure_reports_whether_previous_runtime_restarted(self):
        with TemporaryDirectory() as temporary_directory:
            pack = published_pack(Path(temporary_directory))
            preferences = AppSettings(tts_speaker_wav="live-narrator.wav")
            previous_runtime = preferences.updated(game_pack="previous-pack.json")
            for restarted in (True, False):
                with self.subTest(restarted=restarted):
                    controller = Mock(is_ready=True)
                    controller.apply_settings.return_value = True
                    controller.start.side_effect = [True, restarted]
                    save = Mock(side_effect=OSError("disk full"))
                    activator = OfflinePackActivator(save_state=save)

                    with self.assertRaisesRegex(
                        OfflinePackActivationError, "disk full"
                    ) as raised:
                        activator.activate(
                            preferences,
                            pack,
                            controller,
                            previous_runtime_settings=previous_runtime,
                        )

                    save.assert_called_once_with(preferences, pack)
                    self.assertEqual(raised.exception.rollback_failed, not restarted)
                    self.assertEqual(controller.shutdown.call_count, 2)
                    self.assertEqual(controller.start.call_count, 2)
                    self.assertEqual(
                        controller.apply_settings.call_args.args, (previous_runtime,)
                    )

    def test_failed_candidate_start_restores_previous_runtime_without_saving(self):
        with TemporaryDirectory() as temporary_directory:
            pack = published_pack(Path(temporary_directory))
            previous = AppSettings(
                game_pack="previous-pack.json", tts_speaker_wav="live-narrator.wav"
            )
            controller = Mock(is_ready=True)
            controller.apply_settings.return_value = True
            controller.start.side_effect = [False, True]
            save = Mock()

            with self.assertRaisesRegex(
                OfflinePackActivationError,
                "could not start",
            ):
                OfflinePackActivator(save_state=save).activate(
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
                    save_state=Mock(side_effect=OSError("disk full"))
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
