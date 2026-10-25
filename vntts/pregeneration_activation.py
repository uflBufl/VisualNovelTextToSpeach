"""Failure-atomic activation of a published self-service game pack."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter, process_time
from typing import Protocol

from vntts.game_pack import GamePackError, import_game_pack
from vntts.pregeneration_pack import OfflinePackResult
from vntts.prepared_playback import PreparedPlaybackStore, project_prepared_pack
from vntts.settings import AppSettings
from vntts.support import record_background_operation


class _Cancellation(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...


class _Controller(Protocol):
    @property
    def is_ready(self) -> bool: ...

    def shutdown(self) -> None: ...

    def apply_settings(
        self,
        settings: AppSettings,
        *,
        cancellation: _Cancellation | None = None,
    ) -> bool | None: ...

    def prepare_startup(self) -> None: ...

    def start(self) -> bool: ...


class OfflinePackActivationError(RuntimeError):
    """A published pack could not replace the active runtime configuration."""

    def __init__(self, message: str, *, rollback_failed: bool = False) -> None:
        super().__init__(message)
        self.rollback_failed = bool(rollback_failed)


class OfflinePackActivationCancelled(OfflinePackActivationError):
    """Pack activation was cancelled before playback state was committed."""


@dataclass(frozen=True)
class OfflinePackActivationResult:
    settings: AppSettings
    state_path: Path
    restarted_runtime: bool


class OfflinePackActivator:
    def __init__(
        self,
        *,
        save_state: Callable[[AppSettings, OfflinePackResult], str | Path]
        | None = None,
    ) -> None:
        self.save_state = save_state or PreparedPlaybackStore().save

    def activate(
        self,
        current_settings: AppSettings,
        pack_result: OfflinePackResult,
        controller: _Controller,
        cancellation: _Cancellation | None = None,
        restart_previous: _Cancellation | None = None,
        *,
        save_state: Callable[[AppSettings, OfflinePackResult], str | Path]
        | None = None,
        previous_runtime_settings: AppSettings | None = None,
    ) -> OfflinePackActivationResult:
        if not isinstance(current_settings, AppSettings):
            raise OfflinePackActivationError("Current settings are invalid")
        if not isinstance(pack_result, OfflinePackResult):
            raise OfflinePackActivationError("Offline game pack result is invalid")
        phase_started, cpu_started = perf_counter(), process_time()
        try:
            imported = import_game_pack(pack_result.manifest)
        except (GamePackError, OSError, ValueError) as error:
            raise OfflinePackActivationError(
                f"Offline game pack preflight failed: {error}"
            ) from error
        extension = imported.pack.extensions.get("vntts.self-service")
        if (
            not isinstance(extension, dict)
            or extension.get("identity") != pack_result.identity
        ):
            raise OfflinePackActivationError("Offline game pack identity changed")
        _record_activation_phase("pack-preflight", phase_started, cpu_started)
        phase_started, cpu_started = perf_counter(), process_time()
        candidate = project_prepared_pack(current_settings, imported)
        _record_activation_phase("settings-build", phase_started, cpu_started)
        _raise_if_cancelled(cancellation)
        was_ready = bool(controller.is_ready)
        runtime_changed = False
        try:
            phase_started, cpu_started = perf_counter(), process_time()
            if was_ready:
                controller.shutdown()
            _raise_if_cancelled(cancellation)
            runtime_changed = True
            applied = controller.apply_settings(candidate, cancellation=cancellation)
            if applied is False:
                raise OfflinePackActivationCancelled(
                    "Offline game pack activation was cancelled"
                )
            _raise_if_cancelled(cancellation)
            if was_ready:
                controller.prepare_startup()
                _raise_if_cancelled(cancellation)
                if controller.start() is not True:
                    raise OfflinePackActivationError(
                        "The speech runtime could not start with the offline game pack"
                    )
            _record_activation_phase("runtime-apply", phase_started, cpu_started)
            _raise_if_cancelled(cancellation)
            phase_started, cpu_started = perf_counter(), process_time()
            state_path = Path(
                (save_state or self.save_state)(current_settings, pack_result)
            ).expanduser()
            _record_activation_phase("state-save", phase_started, cpu_started)
        except Exception as error:
            if runtime_changed or was_ready:
                rollback_error = _restore_runtime(
                    controller,
                    previous_runtime_settings or current_settings,
                    was_ready=was_ready,
                    restart_previous=restart_previous,
                )
                if rollback_error is not None:
                    raise OfflinePackActivationError(
                        f"Offline pack activation failed ({error}); restoring the "
                        f"previous pack also failed ({rollback_error})",
                        rollback_failed=True,
                    ) from error
            if isinstance(error, OfflinePackActivationError):
                raise
            raise OfflinePackActivationError(
                f"Unable to activate the offline game pack: {error}"
            ) from error
        return OfflinePackActivationResult(candidate, state_path, was_ready)


def _restore_runtime(
    controller: _Controller,
    settings: AppSettings,
    *,
    was_ready: bool,
    restart_previous: _Cancellation | None = None,
) -> Exception | None:
    try:
        controller.shutdown()
        if controller.apply_settings(settings) is False:
            raise RuntimeError("previous settings were not applied")
        should_restart = restart_previous is None or restart_previous.is_set()
        if was_ready and should_restart:
            controller.prepare_startup()
            if controller.start() is not True:
                raise RuntimeError("previous speech runtime did not restart")
    except Exception as error:
        return error
    return None


def _raise_if_cancelled(cancellation: _Cancellation | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise OfflinePackActivationCancelled(
            "Offline game pack activation was cancelled"
        )


def _record_activation_phase(name: str, started: float, cpu_started: float) -> None:
    record_background_operation(
        f"pregeneration-activation-{name}",
        (perf_counter() - started) * 1000,
        "complete",
        cpu_ms=(process_time() - cpu_started) * 1000,
    )


__all__ = [
    "OfflinePackActivationCancelled",
    "OfflinePackActivationError",
    "OfflinePackActivationResult",
    "OfflinePackActivator",
]
