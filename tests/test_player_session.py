from threading import Event, Thread
from unittest.mock import Mock

from vntts.player_session import PlayerSessionOwner
from vntts.settings import AppSettings


def test_replacement_waits_for_superseded_start() -> None:
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


def test_shutdown_cleans_up_a_late_start_once() -> None:
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
