import unittest
from threading import Event, Thread
from unittest.mock import Mock

from vntts.player_session import PlayerSessionOwner
from vntts.settings import AppSettings


class PlayerSessionOwnerTest(unittest.TestCase):
    def test_replacement_waits_for_superseded_start(self) -> None:
        entered = Event()
        release = Event()
        controller = Mock()

        def start() -> bool:
            if controller.start.call_count == 1:
                entered.set()
                assert release.wait(2)
            return True

        controller.start.side_effect = start
        owner = PlayerSessionOwner(controller)
        first = owner.begin()
        old = Thread(target=owner.start, args=(first,))
        old.start()
        assert entered.wait(1)

        second = owner.begin()
        replacement = Thread(target=owner.restart, args=(second, AppSettings()))
        replacement.start()
        assert not controller.apply_settings.called
        release.set()
        old.join(2)
        replacement.join(2)
        assert not old.is_alive() and not replacement.is_alive()
        controller.apply_settings.assert_called_once()
        assert controller.start.call_count == 2
        assert owner.is_current(second)

    def test_shutdown_cleans_up_a_late_start_once(self) -> None:
        entered = Event()
        release = Event()
        controller = Mock()

        def start() -> bool:
            entered.set()
            assert release.wait(2)
            return True

        controller.start.side_effect = start
        owner = PlayerSessionOwner(controller)
        generation = owner.begin()
        worker = Thread(target=owner.start, args=(generation,))
        worker.start()
        assert entered.wait(1)
        owner.close()
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        controller.request_shutdown.assert_called_once()
        controller.shutdown.assert_called_once()
        assert not owner.is_current(generation)

    def test_configure_restart_restores_previous_runtime_after_cancel(self) -> None:
        cancellation = Event()
        controller = Mock()
        previous = AppSettings(speech_backend="moss-tts")
        requested = AppSettings(speech_backend="pocket-tts")
        controller.settings = previous

        def apply_settings(
            settings: AppSettings, *, cancellation: Event | None = None
        ) -> bool:
            if settings == requested:
                assert cancellation is not None
                cancellation.set()
                return False
            controller.settings = settings
            return True

        controller.apply_settings.side_effect = apply_settings
        owner = PlayerSessionOwner(controller)
        generation = owner.begin(cancellation)

        assert owner.configure(generation, requested, cancellation, restart=True) == (
            True,
            False,
        )
        assert controller.settings == previous
        assert controller.apply_settings.call_args_list[1].args == (previous,)
        controller.prepare_startup.assert_called_once()
        controller.start.assert_called_once()

    def test_configure_restart_restores_previous_runtime_after_failed_start(
        self,
    ) -> None:
        cancellation = Event()
        controller = Mock()
        previous = AppSettings(speech_backend="moss-tts")
        requested = AppSettings(speech_backend="pocket-tts")
        controller.settings = previous

        def apply_settings(settings: AppSettings, **_kwargs: object) -> bool:
            controller.settings = settings
            return True

        controller.apply_settings.side_effect = apply_settings
        controller.start.side_effect = (False, True)
        owner = PlayerSessionOwner(controller)
        generation = owner.begin(cancellation)

        assert owner.configure(generation, requested, cancellation, restart=True) == (
            True,
            False,
        )
        assert controller.settings == previous
        assert controller.apply_settings.call_args_list[1].args == (previous,)
        assert controller.prepare_startup.call_count == 2
        assert controller.start.call_count == 2

    def test_configure_restart_reports_apply_and_rollback_failures(self) -> None:
        cancellation = Event()
        controller = Mock()
        controller.settings = AppSettings(speech_backend="moss-tts")
        controller.is_ready = False
        controller.apply_settings.side_effect = (
            ValueError("apply failed"),
            RuntimeError("rollback failed"),
        )
        owner = PlayerSessionOwner(controller)
        generation = owner.begin(cancellation)

        with self.assertRaisesRegex(
            RuntimeError,
            "apply failed; previous speech runtime could not be restored: "
            "rollback failed",
        ):
            owner.configure(
                generation,
                AppSettings(speech_backend="pocket-tts"),
                cancellation,
                restart=True,
            )

        self.assertEqual(controller.shutdown.call_count, 2)
