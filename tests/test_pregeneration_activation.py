import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock

from tests.test_pregeneration_pack import fixture
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
