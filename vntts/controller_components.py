"""Explicit coordination boundaries behind :class:`AppController`."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Protocol

from vntts.dialog import is_empty
from vntts.live_speech import play_typed_text
from vntts.voices import normalize_character_name


def create_live_toggle(live_reader: Any) -> Callable[[], None]:
    def toggle_live_reading() -> None:
        if live_reader.toggle():
            print("Live reading started")
        else:
            print("Live reading stopping")

    return toggle_live_reading


def speak_live_chunk(
    voice_router: Any,
    chunk: Any,
    playback_guard: Any = None,
) -> Any:
    print(f"{chunk.character} is speaking now (live)")
    print(chunk.text)
    if is_empty(chunk.text):
        return None
    return play_typed_text(voice_router, chunk.character, chunk.text, playback_guard)


class _RuntimeLifecyclePort(Protocol):
    live_reader: Any

    def _start_runtime(self) -> Any: ...

    def _apply_runtime_settings(
        self,
        settings: Any,
        *,
        commit: Callable[[], bool],
    ) -> Any: ...

    def _shutdown_runtime(self) -> Any: ...


class _LiveSessionPort(Protocol):
    def _read_once_live(self) -> Any: ...

    def _identify_live_scope_impl(self) -> Any: ...

    def _toggle_live_impl(self) -> Any: ...

    def _toggle_speech_pause_impl(self) -> Any: ...

    def _skip_current_speech_impl(self) -> Any: ...

    def _repeat_last_speech_impl(self) -> Any: ...

    def _clear_speech_queue_impl(self) -> Any: ...

    def _emergency_stop_impl(self) -> Any: ...

    def _set_auto_advance_enabled_impl(self, enabled: bool) -> Any: ...


class _VoiceAssignmentPort(Protocol):
    is_live_running: bool
    settings: Any
    voice_router: Any
    reported_unknown_speakers: set[str]
    pending_unknown_speakers: set[str]
    narrator_fallback_speakers: set[str]
    status_handler: Callable[[str], Any]

    def _available_voice_characters_impl(self) -> Any: ...

    def _available_voice_choices_impl(self) -> Any: ...

    def _voice_assignment_for_impl(self, character: str) -> Any: ...

    def _preview_voice_choice_impl(self, source_id: str, text: str) -> Any: ...

    def _stop_voice_preview_impl(self) -> Any: ...

    def _allow_narrator_fallback_impl(self, character: str) -> Any: ...

    def _unresolved_live_speakers_impl(self) -> Any: ...

    def _approve_live_narrator_fallbacks_impl(self, characters: Any) -> Any: ...

    def _preview_voice_impl(self, character: str, text: str) -> Any: ...

    def _replay_dialog_impl(self, character: str, text: str) -> Any: ...

    def _apply_narrator_voice(self, voice: Any) -> Any: ...

    def _clear_voice_runtime_cache(self) -> Any: ...


class _DiagnosticsPort(Protocol):
    capture_target: Any
    diagnostic_lock: Any
    last_diagnostic: Any
    live_reader: Any

    def _inspect_current_dialog_impl(self, *, notify: bool = True) -> Any: ...

    def _test_current_dialog_impl(self) -> Any: ...


class _RuntimeSettingsApplyGuard:
    def __init__(self) -> None:
        self.lock = Lock()
        self.cancellation: Any = None
        self.committed = False

    def begin(self, cancellation: Any) -> None:
        if cancellation is None:
            return
        with self.lock:
            if self.cancellation is not None:
                raise RuntimeError("Runtime settings are already being applied")
            self.cancellation = cancellation
            self.committed = False

    def finish(self, cancellation: Any) -> None:
        if cancellation is None:
            return
        with self.lock:
            if self.cancellation is cancellation:
                self.cancellation = None
                self.committed = False

    def commit(self, cancellation: Any) -> bool:
        if cancellation is None:
            return True
        with self.lock:
            if self.cancellation is not cancellation or cancellation.is_set():
                return False
            self.committed = True
            return True

    def cancel(
        self,
        cancellation: Any,
        release_waiters: Callable[[], Any],
    ) -> bool:
        with self.lock:
            if self.cancellation is not cancellation or self.committed:
                return False
            cancellation.set()
        release_waiters()
        return True


@dataclass(frozen=True)
class RuntimeLifecycleComponent:
    controller: _RuntimeLifecyclePort
    settings_apply_guard: _RuntimeSettingsApplyGuard = field(
        default_factory=_RuntimeSettingsApplyGuard,
        compare=False,
        repr=False,
    )

    def start(self) -> Any:
        return self.controller._start_runtime()

    def apply_settings(self, settings: Any, *, cancellation: Any = None) -> Any:
        self.settings_apply_guard.begin(cancellation)
        try:
            return self.controller._apply_runtime_settings(
                settings,
                commit=lambda: self.settings_apply_guard.commit(cancellation),
            )
        finally:
            self.settings_apply_guard.finish(cancellation)

    def cancel_settings_apply(self, cancellation: Any) -> bool:
        reader = self.controller.live_reader
        release_waiters = (
            reader.release_waiters if reader is not None else lambda: None
        )
        return self.settings_apply_guard.cancel(cancellation, release_waiters)

    def shutdown(self) -> Any:
        return self.controller._shutdown_runtime()


@dataclass(frozen=True)
class LiveSessionComponent:
    controller: _LiveSessionPort

    def read_once(self) -> Any:
        return self.controller._read_once_live()

    def identify_scope(self) -> Any:
        return self.controller._identify_live_scope_impl()

    def toggle(self) -> Any:
        return self.controller._toggle_live_impl()

    def toggle_speech_pause(self) -> Any:
        return self.controller._toggle_speech_pause_impl()

    def skip_current_speech(self) -> Any:
        return self.controller._skip_current_speech_impl()

    def repeat_last_speech(self) -> Any:
        return self.controller._repeat_last_speech_impl()

    def clear_speech_queue(self) -> Any:
        return self.controller._clear_speech_queue_impl()

    def emergency_stop(self) -> Any:
        return self.controller._emergency_stop_impl()

    def set_auto_advance_enabled(self, enabled: bool) -> Any:
        return self.controller._set_auto_advance_enabled_impl(enabled)


@dataclass(frozen=True)
class VoiceAssignmentComponent:
    controller: _VoiceAssignmentPort

    def available_characters(self) -> Any:
        return self.controller._available_voice_characters_impl()

    def available_choices(self) -> Any:
        return self.controller._available_voice_choices_impl()

    def assignment_for(self, character: str) -> Any:
        return self.controller._voice_assignment_for_impl(character)

    def preview_choice(self, source_id: str, text: str) -> Any:
        return self.controller._preview_voice_choice_impl(source_id, text)

    def stop_preview(self) -> Any:
        return self.controller._stop_voice_preview_impl()

    def assign(
        self,
        character: str,
        source_id: str,
        *,
        commit_settings: Callable[[Any], Any] | None = None,
    ) -> Any:
        character = (character or "").strip()
        if not character:
            raise ValueError("Enter a narrator or character name")
        controller = self.controller
        if controller.is_live_running:
            raise RuntimeError("Stop live reading before changing a voice")
        choice = next(
            (item for item in self.available_choices() if item.id == source_id),
            None,
        )
        if choice is None:
            raise ValueError("The selected voice is no longer available")
        character_key = normalize_character_name(character)
        assignments = {
            configured_character: configured_source
            for configured_character, configured_source in (
                controller.settings.voice_assignments or {}
            ).items()
            if normalize_character_name(configured_character) != character_key
        }
        assignments[character] = source_id
        updated_settings = controller.settings.updated(voice_assignments=assignments)
        if commit_settings is not None:
            commit_settings(updated_settings)
        controller.voice_router.registry.set_assignment(character, source_id)
        controller.settings = updated_settings
        if character_key == "narrator":
            controller._apply_narrator_voice(
                controller.voice_router.registry.resolve_source(source_id)
            )
        controller._clear_voice_runtime_cache()
        controller.reported_unknown_speakers.discard(character_key)
        controller.pending_unknown_speakers.discard(character_key)
        controller.narrator_fallback_speakers.discard(character_key)
        controller.status_handler(f"{choice.label} assigned to {character}")
        return controller.settings

    def clear(
        self,
        character: str,
        *,
        commit_settings: Callable[[Any], Any] | None = None,
    ) -> Any:
        character = (character or "").strip()
        if not character:
            raise ValueError("Enter a narrator or character name")
        controller = self.controller
        if controller.is_live_running:
            raise RuntimeError("Stop live reading before changing a voice")
        character_key = normalize_character_name(character)
        assignments = {
            configured_character: configured_source
            for configured_character, configured_source in (
                controller.settings.voice_assignments or {}
            ).items()
            if normalize_character_name(configured_character) != character_key
        }
        update: dict[str, Any] = {"voice_assignments": assignments}
        if character_key == "narrator":
            update["force_live_narrator"] = False
        updated_settings = controller.settings.updated(**update)
        if commit_settings is not None:
            commit_settings(updated_settings)
        controller.voice_router.registry.assignments.pop(character_key, None)
        controller.settings = updated_settings
        if character_key == "narrator":
            controller._apply_narrator_voice(None)
        controller._clear_voice_runtime_cache()
        controller.status_handler(
            "Pregenerated narrator tracks enabled when available"
            if character_key == "narrator"
            else f"Automatic voice routing restored for {character}"
        )
        return controller.settings

    def set_force_live_narrator(
        self,
        enabled: bool,
        *,
        commit_settings: Callable[[Any], Any] | None = None,
    ) -> Any:
        controller = self.controller
        if controller.is_live_running:
            raise RuntimeError("Stop live reading before changing Narrator routing")
        enabled = bool(enabled)
        if enabled and self.assignment_for("Narrator") is None:
            raise ValueError("Choose a Narrator voice before forcing live TTS")
        updated_settings = controller.settings.updated(force_live_narrator=enabled)
        if commit_settings is not None:
            commit_settings(updated_settings)
        controller.settings = updated_settings
        controller.status_handler(
            "Narrator will always use live TTS"
            if enabled
            else "Pregenerated Narrator tracks enabled with live voice fallback"
        )
        return controller.settings

    def allow_narrator_fallback(self, character: str) -> Any:
        return self.controller._allow_narrator_fallback_impl(character)

    def unresolved_live_speakers(self) -> Any:
        return self.controller._unresolved_live_speakers_impl()

    def approve_narrator_fallbacks(self, characters: Any) -> Any:
        return self.controller._approve_live_narrator_fallbacks_impl(characters)

    def preview(self, character: str, text: str) -> Any:
        return self.controller._preview_voice_impl(character, text)

    def replay(self, character: str, text: str) -> Any:
        return self.controller._replay_dialog_impl(character, text)


@dataclass(frozen=True)
class DiagnosticsComponent:
    controller: _DiagnosticsPort

    def capture_geometry(self) -> Any:
        target = self.controller.capture_target
        return None if target is None else target.get_geometry()

    def latest(self) -> Any:
        with self.controller.diagnostic_lock:
            return self.controller.last_diagnostic

    def pipeline_metrics(self) -> Any:
        reader = self.controller.live_reader
        return None if reader is None else reader.get_pipeline_metrics()

    def inspect_current_dialog(self, *, notify: bool = True) -> Any:
        return self.controller._inspect_current_dialog_impl(notify=notify)

    def test_current_dialog(self) -> Any:
        return self.controller._test_current_dialog_impl()
