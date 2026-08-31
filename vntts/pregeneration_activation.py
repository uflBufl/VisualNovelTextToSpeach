"""Failure-atomic activation of a published self-service game pack."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vntts.game_pack import GamePackError, import_game_pack
from vntts.pregeneration_pack import OfflinePackResult
from vntts.settings import AppSettings


class OfflinePackActivationError(RuntimeError):
    """A published pack could not replace the active runtime configuration."""

    def __init__(self, message, *, rollback_failed=False):
        super().__init__(message)
        self.rollback_failed = bool(rollback_failed)


class OfflinePackActivationCancelled(OfflinePackActivationError):
    """Pack activation was cancelled before settings were committed."""


@dataclass(frozen=True)
class OfflinePackActivationResult:
    settings: AppSettings
    settings_path: Path
    restarted_runtime: bool


class OfflinePackActivator:
    def __init__(self, *, save_settings=None):
        self.save_settings = save_settings or (lambda settings: settings.save())

    def activate(
        self,
        current_settings,
        pack_result,
        controller,
        cancellation=None,
        restart_previous=None,
    ):
        if not isinstance(current_settings, AppSettings):
            raise OfflinePackActivationError("Current settings are invalid")
        if not isinstance(pack_result, OfflinePackResult):
            raise OfflinePackActivationError("Offline game pack result is invalid")
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
        candidate = imported.apply_to(current_settings).updated(
            audio_source_policy="prefer-generated"
        )
        _raise_if_cancelled(cancellation)
        was_ready = bool(controller.is_ready)
        runtime_changed = False
        try:
            if was_ready:
                controller.shutdown()
            _raise_if_cancelled(cancellation)
            applied = controller.apply_settings(candidate, cancellation=cancellation)
            if applied is False:
                raise OfflinePackActivationCancelled(
                    "Offline game pack activation was cancelled"
                )
            runtime_changed = True
            _raise_if_cancelled(cancellation)
            if was_ready and controller.start() is not True:
                raise OfflinePackActivationError(
                    "The speech runtime could not start with the offline game pack"
                )
            _raise_if_cancelled(cancellation)
            settings_path = Path(self.save_settings(candidate)).expanduser()
        except Exception as error:
            if runtime_changed or was_ready:
                rollback_error = _restore_runtime(
                    controller,
                    current_settings,
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
        return OfflinePackActivationResult(candidate, settings_path, was_ready)


def _restore_runtime(controller, settings, *, was_ready, restart_previous=None):
    try:
        controller.shutdown()
        if controller.apply_settings(settings) is False:
            raise RuntimeError("previous settings were not applied")
        should_restart = restart_previous is None or restart_previous.is_set()
        if was_ready and should_restart and controller.start() is not True:
            raise RuntimeError("previous speech runtime did not restart")
    except Exception as error:
        return error
    return None


def _raise_if_cancelled(cancellation):
    if cancellation is not None and cancellation.is_set():
        raise OfflinePackActivationCancelled(
            "Offline game pack activation was cancelled"
        )


__all__ = [
    "OfflinePackActivationCancelled",
    "OfflinePackActivationError",
    "OfflinePackActivationResult",
    "OfflinePackActivator",
]
