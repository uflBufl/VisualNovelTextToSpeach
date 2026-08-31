"""Explicit coordination boundaries behind :class:`AppController`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class _ControllerImplementation(Protocol):
    """Private implementation surface consumed by controller components."""

    def _start_runtime(self): ...
    def _apply_runtime_settings(self, settings): ...
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
    def _assign_voice_impl(self, character, source_id, *, commit_settings=None): ...
    def _clear_voice_assignment_impl(self, character, *, commit_settings=None): ...
    def _set_force_live_narrator_impl(self, enabled, *, commit_settings=None): ...
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


@dataclass(frozen=True)
class RuntimeLifecycleComponent:
    controller: _ControllerImplementation

    def start(self):
        return self.controller._start_runtime()

    def apply_settings(self, settings):
        return self.controller._apply_runtime_settings(settings)

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
        return self.controller._assign_voice_impl(
            character,
            source_id,
            commit_settings=commit_settings,
        )

    def clear(self, character, *, commit_settings=None):
        return self.controller._clear_voice_assignment_impl(
            character,
            commit_settings=commit_settings,
        )

    def set_force_live_narrator(self, enabled, *, commit_settings=None):
        return self.controller._set_force_live_narrator_impl(
            enabled,
            commit_settings=commit_settings,
        )

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
