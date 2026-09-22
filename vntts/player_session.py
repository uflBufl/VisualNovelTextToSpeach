"""Serialize mutations of the shared player controller across UI workers."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Lock
from typing import TYPE_CHECKING, TypeVar

from vntts.pregeneration_activation import (
    OfflinePackActivationCancelled,
    OfflinePackActivationResult,
    OfflinePackActivator,
)
from vntts.pregeneration_pack import OfflinePackResult
from vntts.settings import AppSettings

if TYPE_CHECKING:
    from vntts.controller import AppController

_Result = TypeVar("_Result")


class PlayerSessionOwner:
    def __init__(self, controller: AppController) -> None:
        self.controller = controller
        self._operation_lock = Lock()
        self._state_lock = Lock()
        self._generation = 0
        self._closed = False
        self._active = False
        self._cancellation = Event()

    def begin(self, cancellation: Event | None = None) -> int:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Player session is closed")
            self._cancellation.set()
            self._generation += 1
            self._cancellation = cancellation or Event()
            active = self._active
            generation = self._generation
        if active:
            self.controller.request_shutdown()
        return generation

    def is_current(self, generation: int | None) -> bool:
        with self._state_lock:
            return (
                isinstance(generation, int)
                and generation == self._generation
                and not self._closed
            )

    def cancel(self) -> None:
        with self._state_lock:
            self._cancellation.set()
            active = self._active
        if active:
            self.controller.request_shutdown()

    def close(self) -> int:
        with self._state_lock:
            self._closed = True
            self._cancellation.set()
            self._generation += 1
            active = self._active
            generation = self._generation
        self.controller.request_shutdown()
        if not active:
            with self._operation_lock:
                self.controller.shutdown()
        return generation

    def run(
        self,
        generation: int,
        operation: Callable[[Event], _Result],
        stale_result: _Result,
    ) -> _Result:
        with self._operation_lock:
            with self._state_lock:
                if (
                    self._closed
                    or generation != self._generation
                    or self._cancellation.is_set()
                ):
                    return stale_result
                cancellation = self._cancellation
                self._active = True
            try:
                return operation(cancellation)
            finally:
                with self._state_lock:
                    self._active = False
                    stale = self._closed or generation != self._generation
                if stale:
                    self.controller.shutdown()

    def start(self, generation: int) -> bool:
        def operation(_cancellation: Event) -> bool:
            self.controller.prepare_startup()
            try:
                ready = bool(self.controller.start())
            except Exception:
                self.controller.shutdown()
                raise
            if not self.is_current(generation):
                return False
            return ready

        return self.run(generation, operation, False)

    def attach(self, generation: int, settings: AppSettings) -> bool:
        return self.run(
            generation,
            lambda _cancellation: self.controller.apply_settings(settings) is not False,
            False,
        )

    def configure(
        self,
        generation: int,
        settings: AppSettings,
        cancellation: Event,
        *,
        restart: bool = False,
    ) -> tuple[bool, bool]:
        def operation(_cancellation: Event) -> tuple[bool, bool]:
            if restart:
                self.controller.shutdown()
                if cancellation.is_set() or not self.is_current(generation):
                    return False, False
            applied = self.controller.apply_settings(
                settings, cancellation=cancellation
            )
            if restart and applied is not False and not cancellation.is_set():
                if not self.is_current(generation):
                    return False, False
                self.controller.prepare_startup()
                if cancellation.is_set() or not self.is_current(generation):
                    self.controller.request_shutdown()
                    return False, False
                applied = self.controller.start()
                if cancellation.is_set() or not self.is_current(generation):
                    self.controller.shutdown()
                    return False, False
            return self.is_current(generation), applied is not False

        return self.run(generation, operation, (False, False))

    def restart(self, generation: int, settings: AppSettings) -> bool:
        def operation(_cancellation: Event) -> bool:
            self.controller.shutdown()
            if not self.is_current(generation):
                return False
            if self.controller.apply_settings(settings) is False:
                return False
            if not self.is_current(generation):
                return False
            self.controller.prepare_startup()
            if not self.is_current(generation):
                self.controller.request_shutdown()
                return False
            ready = bool(self.controller.start())
            if not self.is_current(generation):
                return False
            return ready

        return self.run(generation, operation, False)

    def activate(
        self,
        generation: int,
        activator: OfflinePackActivator,
        settings: AppSettings,
        pack: OfflinePackResult,
        cancellation: Event,
        restart_previous: Event,
        generation_settings: AppSettings | None,
    ) -> OfflinePackActivationResult | None:
        def save_if_current(candidate: AppSettings) -> str:
            with self._state_lock:
                if (
                    self._closed
                    or generation != self._generation
                    or cancellation.is_set()
                ):
                    raise OfflinePackActivationCancelled(
                        "Offline game pack activation was cancelled"
                    )
                return str(activator.save_settings(candidate))

        return self.run(
            generation,
            lambda _cancellation: activator.activate(
                settings,
                pack,
                self.controller,
                cancellation,
                restart_previous,
                generation_settings=generation_settings,
                save_settings=save_if_current,
            ),
            None,
        )
