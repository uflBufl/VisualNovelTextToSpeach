"""Explicit coordination boundaries behind :class:`AppController`."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Protocol

from vntts.dialog import is_empty
from vntts.live_speech import play_typed_text
from vntts.voices import normalize_character_name


def create_live_toggle(live_reader):
    def toggle_live_reading():
        if live_reader.toggle():
            print("Live reading started")
        else:
            print("Live reading stopping")

    return toggle_live_reading


def speak_live_chunk(voice_router, chunk, playback_guard=None):
    print(f"{chunk.character} is speaking now (live)")
    print(chunk.text)
    if is_empty(chunk.text):
        return None
    return play_typed_text(voice_router, chunk.character, chunk.text, playback_guard)


class _ControllerImplementation(Protocol):
    """Private implementation surface consumed by controller components."""

    def _start_runtime(self): ...
    def _apply_runtime_settings(self, settings, *, commit): ...
    def _shutdown_runtime(self): ...
    def _read_once_live(self): ...
    def _identify_live_scope_impl(self): ...
    def _toggle_live_impl(self): ...
    def _toggle_speech_pause_impl(self): ...
    def _skip_current_speech_impl(self): ...
    def _repeat_last_speech_impl(self): ...
    def _clear_speech_queue_impl(self): ...
    def _emergency_stop_impl(self): ...
    def _set_auto_advance_enabled_impl(self, enabled): ...
    def _available_voice_characters_impl(self): ...
    def _available_voice_choices_impl(self): ...
    def _voice_assignment_for_impl(self, character): ...
    def _preview_voice_choice_impl(self, source_id, text): ...
    def _stop_voice_preview_impl(self): ...
    def _allow_narrator_fallback_impl(self, character): ...
    def _unresolved_live_speakers_impl(self): ...
    def _approve_live_narrator_fallbacks_impl(self, characters): ...
    def _preview_voice_impl(self, character, text): ...
    def _replay_dialog_impl(self, character, text): ...
    def _get_capture_geometry_impl(self): ...
    def _get_latest_diagnostic_impl(self): ...
    def _get_live_pipeline_metrics_impl(self): ...
    def _inspect_current_dialog_impl(self, *, notify=True): ...
    def _test_current_dialog_impl(self): ...


class _RuntimeSettingsApplyGuard:
    def __init__(self):
        self.lock = Lock()
        self.cancellation = None
        self.committed = False

    def begin(self, cancellation):
        if cancellation is None:
            return
        with self.lock:
            if self.cancellation is not None:
                raise RuntimeError("Runtime settings are already being applied")
            self.cancellation = cancellation
            self.committed = False

    def finish(self, cancellation):
        if cancellation is None:
            return
        with self.lock:
            if self.cancellation is cancellation:
                self.cancellation = None
                self.committed = False

    def commit(self, cancellation):
        if cancellation is None:
            return True
        with self.lock:
            if self.cancellation is not cancellation or cancellation.is_set():
                return False
            self.committed = True
            return True

    def cancel(self, cancellation, release_waiters):
        with self.lock:
            if self.cancellation is not cancellation or self.committed:
                return False
            cancellation.set()
        release_waiters()
        return True


@dataclass(frozen=True)
class RuntimeLifecycleComponent:
    controller: _ControllerImplementation
    settings_apply_guard: _RuntimeSettingsApplyGuard = field(
        default_factory=_RuntimeSettingsApplyGuard,
        compare=False,
        repr=False,
    )

    def start(self):
        return self.controller._start_runtime()

    def apply_settings(self, settings, *, cancellation=None):
        self.settings_apply_guard.begin(cancellation)
        try:
            return self.controller._apply_runtime_settings(
                settings,
                commit=lambda: self.settings_apply_guard.commit(cancellation),
            )
        finally:
            self.settings_apply_guard.finish(cancellation)

    def cancel_settings_apply(self, cancellation):
        reader = self.controller.live_reader
        release_waiters = (
            reader.release_waiters if reader is not None else lambda: None
        )
        return self.settings_apply_guard.cancel(cancellation, release_waiters)

    def shutdown(self):
        return self.controller._shutdown_runtime()


@dataclass(frozen=True)
class LiveSessionComponent:
    controller: _ControllerImplementation

    def read_once(self):
        return self.controller._read_once_live()

    def identify_scope(self):
        return self.controller._identify_live_scope_impl()

    def toggle(self):
        return self.controller._toggle_live_impl()

    def toggle_speech_pause(self):
        return self.controller._toggle_speech_pause_impl()

    def skip_current_speech(self):
        return self.controller._skip_current_speech_impl()

    def repeat_last_speech(self):
        return self.controller._repeat_last_speech_impl()

    def clear_speech_queue(self):
        return self.controller._clear_speech_queue_impl()

    def emergency_stop(self):
        return self.controller._emergency_stop_impl()

    def set_auto_advance_enabled(self, enabled):
        return self.controller._set_auto_advance_enabled_impl(enabled)


@dataclass(frozen=True)
class VoiceAssignmentComponent:
    controller: _ControllerImplementation

    def available_characters(self):
        return self.controller._available_voice_characters_impl()

    def available_choices(self):
        return self.controller._available_voice_choices_impl()

    def assignment_for(self, character):
        return self.controller._voice_assignment_for_impl(character)

    def preview_choice(self, source_id, text):
        return self.controller._preview_voice_choice_impl(source_id, text)

    def stop_preview(self):
        return self.controller._stop_voice_preview_impl()

    def assign(self, character, source_id, *, commit_settings=None):
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

    def clear(self, character, *, commit_settings=None):
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
        update = {"voice_assignments": assignments}
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

    def set_force_live_narrator(self, enabled, *, commit_settings=None):
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

    def allow_narrator_fallback(self, character):
        return self.controller._allow_narrator_fallback_impl(character)

    def unresolved_live_speakers(self):
        return self.controller._unresolved_live_speakers_impl()

    def approve_narrator_fallbacks(self, characters):
        return self.controller._approve_live_narrator_fallbacks_impl(characters)

    def preview(self, character, text):
        return self.controller._preview_voice_impl(character, text)

    def replay(self, character, text):
        return self.controller._replay_dialog_impl(character, text)


@dataclass(frozen=True)
class DiagnosticsComponent:
    controller: _ControllerImplementation

    def capture_geometry(self):
        return self.controller._get_capture_geometry_impl()

    def latest(self):
        return self.controller._get_latest_diagnostic_impl()

    def pipeline_metrics(self):
        return self.controller._get_live_pipeline_metrics_impl()

    def inspect_current_dialog(self, *, notify=True):
        return self.controller._inspect_current_dialog_impl(notify=notify)

    def test_current_dialog(self):
        return self.controller._test_current_dialog_impl()
