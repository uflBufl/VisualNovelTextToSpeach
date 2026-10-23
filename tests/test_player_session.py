import unittest
from concurrent.futures import Future
from threading import Event, Thread
from unittest.mock import Mock

from vntts.player_session import PlayerSessionOwner
from vntts.settings import AppSettings


class _BackgroundWarmupBackend:
    def __init__(self, events):
        self.events = events
        self.startup_pending = False
        self.ready = Future()
        self.cancelled = Event()

    def cancel_startup(self):
        self.events.append("cancel-startup")
        self.cancelled.set()
        return True

    def finish_warmup(self):
        if self.cancelled.is_set():
            return False
        self.events.append("warmup")
        return True


class PlayerSessionOwnerTest(unittest.TestCase):
    def test_completed_start_operation_cancels_remaining_background_warmup(
        self,
    ) -> None:
        for action in ("begin", "cancel", "close"):
            with self.subTest(action=action):
                events = []
                shutdown_requested = Event()

                backend = _BackgroundWarmupBackend(events)

                class Lease:
                    def __getattr__(self, name):
                        return getattr(backend, name)

                controller = Mock(tts=Lease())

                def prepare_startup():
                    events.append("prepare")
                    shutdown_requested.clear()

                def start():
                    backend.startup_pending = True
                    if not backend.ready.done():
                        backend.ready.set_result(True)
                    return True

                def request_shutdown():
                    events.append("request-shutdown")
                    self.assertTrue(backend.cancelled.is_set())
                    shutdown_requested.set()

                controller.prepare_startup.side_effect = prepare_startup
                controller.start.side_effect = start
                controller.request_shutdown.side_effect = request_shutdown
                owner = PlayerSessionOwner(controller)
                generation = owner.begin()
                self.assertTrue(owner.start(generation))
                self.assertTrue(backend.ready.done())
                self.assertTrue(backend.startup_pending)
                events.clear()

                result = getattr(owner, action)()

                self.assertEqual(events, ["cancel-startup", "request-shutdown"])
                self.assertTrue(shutdown_requested.is_set())
                if action == "begin":
                    # Reconfiguration can clear the shared shutdown event;
                    # cancellation of the previous flight must remain set.
                    self.assertEqual(
                        owner.configure(result, AppSettings(), Event(), restart=True),
                        (True, True),
                    )
                    self.assertFalse(shutdown_requested.is_set())
                elif action == "cancel":
                    self.assertFalse(owner.start(generation))
                else:
                    controller.shutdown.assert_called_once()
                    self.assertFalse(owner.is_current(generation))
                    with self.assertRaisesRegex(RuntimeError, "session is closed"):
                        owner.begin()
                self.assertFalse(backend.finish_warmup())
                self.assertNotIn("warmup", events)

    def test_active_operation_cancels_startup_before_requesting_shutdown(self) -> None:
        events = []
        backend = Mock(startup_pending=False)
        backend.cancel_startup.side_effect = lambda: events.append("cancel-startup")
        controller = Mock(tts=backend)
        controller.request_shutdown.side_effect = lambda: events.append("shutdown")
        owner = PlayerSessionOwner(controller)
        generation = owner.begin()

        def operation(_cancellation):
            owner.cancel()
            self.assertEqual(events, ["cancel-startup", "shutdown"])
            return True

        self.assertTrue(owner.run(generation, operation, False))

    def test_idle_eager_and_unspecified_mock_startup_do_not_request_shutdown(
        self,
    ) -> None:
        for backend in (object(), Mock(), Mock(startup_pending=False)):
            with self.subTest(backend=backend):
                controller = Mock(tts=backend)
                owner = PlayerSessionOwner(controller)
                owner.begin()
                owner.cancel()
                controller.request_shutdown.assert_not_called()
                owner.close()
                controller.request_shutdown.assert_called_once()
                controller.shutdown.assert_called_once()
                if isinstance(backend, Mock):
                    backend.cancel_startup.assert_not_called()

    def test_startup_cancellation_failure_still_requests_shutdown(self) -> None:
        failure = RuntimeError("cancel failed")
        backend = Mock(startup_pending=True)
        backend.cancel_startup.side_effect = failure
        controller = Mock(tts=backend)
        owner = PlayerSessionOwner(controller)

        with self.assertRaises(RuntimeError) as raised:
            owner.cancel()

        self.assertIs(raised.exception, failure)
        controller.request_shutdown.assert_called_once()

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

    def test_restart_shuts_down_after_start_failure(self) -> None:
        controller = Mock()
        controller.start.side_effect = RuntimeError("startup failed")
        owner = PlayerSessionOwner(controller)
        generation = owner.begin()

        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            owner.restart(generation, AppSettings())

        self.assertEqual(controller.shutdown.call_count, 2)

    def test_start_errors_survive_failed_shutdown(self) -> None:
        for restart in (False, True):
            for error in (ValueError("startup failed"), KeyboardInterrupt()):
                with self.subTest(restart=restart, error=type(error)):
                    controller = Mock()
                    controller.start.side_effect = error
                    cleanup_error = (
                        SystemExit("shutdown interrupted")
                        if restart and isinstance(error, KeyboardInterrupt)
                        else RuntimeError("shutdown failed")
                    )
                    controller.shutdown.side_effect = (
                        [None, cleanup_error] if restart else cleanup_error
                    )
                    owner = PlayerSessionOwner(controller)
                    generation = owner.begin()

                    with self.assertRaises(type(error)) as raised:
                        if restart:
                            owner.restart(generation, AppSettings())
                        else:
                            owner.start(generation)

                    self.assertIs(raised.exception, error)
                    self.assertIn("shutdown", error.__notes__[0])

    def test_stale_cleanup_preserves_operation_error_but_reports_its_own(self) -> None:
        for fails in (False, True):
            with self.subTest(fails=fails):
                controller = Mock()
                cleanup_error = RuntimeError("shutdown failed")
                controller.shutdown.side_effect = cleanup_error
                owner = PlayerSessionOwner(controller)
                generation = owner.begin()
                operation_error = ValueError("operation failed")

                def operation(_cancellation: Event) -> bool:
                    owner.begin()
                    if fails:
                        raise operation_error
                    return True

                with self.assertRaises(ValueError if fails else RuntimeError) as raised:
                    owner.run(generation, operation, False)

                self.assertIs(
                    raised.exception, operation_error if fails else cleanup_error
                )
                if fails:
                    self.assertIn("shutdown failed", operation_error.__notes__[0])

    def test_start_and_restart_honor_cancellation_between_phases(self) -> None:
        for restart in (False, True):
            phases = ("prepare_startup", "start")
            if restart:
                phases = ("shutdown", "apply_settings", *phases)
            for phase in phases:
                with self.subTest(restart=restart, phase=phase):
                    controller = Mock()
                    controller.start.return_value = True
                    shutdown_requested = Event()
                    controller.request_shutdown.side_effect = shutdown_requested.set
                    controller.prepare_startup.side_effect = shutdown_requested.clear
                    owner = PlayerSessionOwner(controller)
                    generation = owner.begin()

                    def cancel(*_args: object, **_kwargs: object) -> bool:
                        owner.cancel()
                        if phase == "prepare_startup":
                            shutdown_requested.clear()
                        return True

                    getattr(controller, phase).side_effect = cancel
                    result = (
                        owner.restart(generation, AppSettings())
                        if restart
                        else owner.start(generation)
                    )

                    self.assertFalse(result)
                    self.assertTrue(owner.is_current(generation))
                    self.assertTrue(shutdown_requested.is_set())
                    self.assertEqual(controller.start.call_count, phase == "start")
                    self.assertEqual(
                        controller.shutdown.call_count,
                        int(restart) + int(phase == "start"),
                    )

    def test_settings_apply_receives_the_owned_cancellation(self) -> None:
        for operation in ("attach", "restart"):
            with self.subTest(operation=operation):
                controller = Mock()
                controller.start.return_value = True
                owner = PlayerSessionOwner(controller)
                cancellation = Event()
                generation = owner.begin(cancellation)
                settings = AppSettings()

                self.assertTrue(getattr(owner, operation)(generation, settings))

                controller.apply_settings.assert_called_once_with(
                    settings, cancellation=cancellation
                )

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
