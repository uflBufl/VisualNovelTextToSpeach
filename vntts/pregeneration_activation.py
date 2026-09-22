"""Failure-atomic activation of a published self-service game pack."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter, process_time
from typing import Protocol

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.game_pack import GamePackError, import_game_pack
from vntts.pregeneration_pack import OfflinePackResult
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
    ) -> object: ...

    def prepare_startup(self) -> None: ...

    def start(self) -> bool: ...


class OfflinePackActivationError(RuntimeError):
    """A published pack could not replace the active runtime configuration."""

    def __init__(self, message: str, *, rollback_failed: bool = False) -> None:
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
    def __init__(
        self,
        *,
        save_settings: Callable[[AppSettings], str | Path] | None = None,
    ) -> None:
        self.save_settings = save_settings or (lambda settings: settings.save())

    def activate(
        self,
        current_settings: AppSettings,
        pack_result: OfflinePackResult,
        controller: _Controller,
        cancellation: _Cancellation | None = None,
        restart_previous: _Cancellation | None = None,
        *,
        generation_settings: AppSettings | None = None,
        save_settings: Callable[[AppSettings], str | Path] | None = None,
    ) -> OfflinePackActivationResult:
        if not isinstance(current_settings, AppSettings):
            raise OfflinePackActivationError("Current settings are invalid")
        if generation_settings is not None and not isinstance(
            generation_settings, AppSettings
        ):
            raise OfflinePackActivationError("Generation settings are invalid")
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
        source_settings = generation_settings or current_settings
        source_dialogue = ChapterVoicePreloader.load_optional(
            imported.story_index
        ).dialogue
        candidate = imported.apply_to(source_settings).updated(
            audio_source_policy=(
                "prefer-game-audio"
                if any(
                    line.source_audio_authoritative
                    and line.source_audio_completeness == "full"
                    for line in source_dialogue
                )
                else "prefer-generated"
            ),
            force_live_narrator=False,
            tts_speaker_wav=None,
        )
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
            settings_path = Path(
                (save_settings or self.save_settings)(candidate)
            ).expanduser()
            _record_activation_phase("settings-save", phase_started, cpu_started)
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
