"""Explicit coordination boundaries behind :class:`AppController`."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Protocol

from vntts.dialog import is_empty, speak_dialog
from vntts.dialog_capture import (
    OCRError,
    OCRUncertainError,
    analyze_dialog_snapshot,
    get_screenshot_directory,
)
from vntts.generated_audio import GeneratedAudioFallbackBackend
from vntts.live_speech import play_typed_text
from vntts.voices import (
    VoiceChoice,
    default_voice_choice_id,
    find_voice_assignment,
    is_narrator,
    normalize_character_name,
    pocket_tts_preset_voices,
)


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
    is_ready: bool
    live_reader: Any
    schedule_dialog_read: Callable[[], Any]
    settings: Any
    speech_backend: Any
    status_handler: Callable[[str], Any]

    def _identify_live_scope_impl(self) -> Any: ...

    def _toggle_live_impl(self) -> Any: ...

    def _live_auto_advance_callback(self) -> Any: ...

    def _set_backend_live_mode(self, active: bool) -> Any: ...


class _VoiceAssignmentPort(Protocol):
    chapter_voice_preloader: Any
    is_ready: bool
    is_live_running: bool
    live_speaker_corpus: Any
    live_speaker_corpus_error: Any
    narrator_fallback_names: dict[str, str]
    next_live_narrator_fallback_names: dict[str, str]
    speech_backend: Any
    speech_executor: Any
    settings: Any
    tts: Any
    voice_router: Any
    reported_unknown_speakers: set[str]
    pending_unknown_speakers: set[str]
    narrator_fallback_speakers: set[str]
    status_handler: Callable[[str], Any]

    def _apply_narrator_voice(self, voice: Any) -> Any: ...

    def _clear_voice_runtime_cache(self) -> Any: ...

    def _preview_voice(self, character: str, text: str) -> Any: ...

    def _preview_voice_choice(self, choice: Any, text: str) -> Any: ...

    def _speaker_requires_voice_decision(
        self,
        character: str,
        text: str | None,
        *,
        live_preflight: bool = False,
    ) -> bool: ...


class _DiagnosticsPort(Protocol):
    capture_target: Any
    correction_dictionary: Any
    diagnostic_lock: Any
    is_ready: bool
    last_diagnostic: Any
    live_reader: Any
    settings: Any
    status_handler: Callable[[str], Any]
    uncertain_frame_recorder: Any
    voice_router: Any

    def _publish_diagnostic(self, snapshot: Any, *, notify: bool = True) -> Any: ...

    def _refresh_diagnostic_metrics(
        self,
        route_metrics: Any = None,
        audio_source: Any = None,
    ) -> Any: ...

    def _resolve_voice_label(self, character: str) -> Any: ...

    def _speak_with_live_backend(self, character: str, text: str) -> Any: ...


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
        controller = self.controller
        if not controller.is_ready:
            return False
        controller.live_reader.resume_after_emergency()
        accepted = controller.schedule_dialog_read()
        if accepted:
            controller.status_handler("Reading current dialog")
        return accepted

    def identify_scope(self) -> Any:
        return self.controller._identify_live_scope_impl()

    def toggle(self) -> Any:
        return self.controller._toggle_live_impl()

    def toggle_speech_pause(self) -> Any:
        controller = self.controller
        if not controller.is_ready:
            return False
        paused = controller.live_reader.toggle_pause()
        controller.status_handler("Speech paused" if paused else "Speech resumed")
        return paused

    def skip_current_speech(self) -> Any:
        controller = self.controller
        if not controller.is_ready:
            return False
        skipped = controller.live_reader.skip_current()
        controller.status_handler(
            "Skipped current speech" if skipped else "Nothing is currently speaking"
        )
        return skipped

    def repeat_last_speech(self) -> Any:
        controller = self.controller
        if not controller.is_ready:
            return False
        repeated = controller.live_reader.repeat_last()
        controller.status_handler(
            "Repeating last speech" if repeated else "No previous speech to repeat"
        )
        return repeated

    def clear_speech_queue(self) -> Any:
        controller = self.controller
        if not controller.is_ready:
            return False
        cleared = controller.live_reader.clear_queue()
        controller.status_handler("Speech queue cleared")
        return cleared

    def emergency_stop(self) -> Any:
        controller = self.controller
        if not controller.is_ready:
            return False
        stopped = controller.live_reader.emergency_stop()
        controller._set_backend_live_mode(False)
        controller.status_handler("Emergency stop: live reading and speech stopped")
        return stopped

    def set_auto_advance_enabled(self, enabled: bool) -> Any:
        controller = self.controller
        controller.settings = controller.settings.updated(
            auto_advance_enabled=bool(enabled)
        )
        if isinstance(controller.speech_backend, GeneratedAudioFallbackBackend):
            # Never replace audio already spoken by the game with live TTS just
            # to obtain a completion duration. Unknown timing pauses automatic
            # advance; it must not create audible duplicate dialogue.
            controller.speech_backend.require_source_audio_completion = False
        if controller.live_reader is not None:
            controller.live_reader.set_auto_advance(
                controller._live_auto_advance_callback()
            )
        controller.status_handler(
            "Auto advance saved but suppressed by sequence-first manual mode"
            if enabled and controller.settings.live_sequence_mode == "audio-manual"
            else "Auto advance enabled"
            if enabled
            else "Auto advance disabled"
        )
        return bool(enabled)


@dataclass(frozen=True)
class VoiceAssignmentComponent:
    controller: _VoiceAssignmentPort

    def available_characters(self) -> Any:
        router = self.controller.voice_router
        if router is None:
            return ["Narrator"]
        voices = {id(voice): voice for voice in router.registry.voices.values()}
        return [
            "Narrator",
            *(
                voice.character
                for voice in sorted(
                    voices.values(), key=lambda item: item.character.casefold()
                )
            ),
        ]

    def available_choices(self) -> Any:
        controller = self.controller
        if controller.voice_router is None:
            return []
        choices = [
            VoiceChoice(
                default_voice_choice_id,
                "Backend default live voice",
                "Use the speech backend's default live voice",
            )
        ]
        if controller.settings.speech_backend == "pocket-tts":
            choices.extend(
                VoiceChoice(
                    f"preset:{name}",
                    name.replace("_", " ").title(),
                    "Pocket TTS built-in voice",
                )
                for name in pocket_tts_preset_voices
            )
        elif controller.settings.speech_backend == "coqui-xtts":
            speakers = getattr(getattr(controller.tts, "tts", None), "speakers", None)
            choices.extend(
                VoiceChoice(
                    f"preset:{speaker}",
                    str(speaker),
                    "XTTS model speaker",
                )
                for speaker in (speakers or ())
            )
        choices.extend(controller.voice_router.registry.choices())
        seen: set[str] = set()
        unique_choices: list[VoiceChoice] = []
        for choice in choices:
            if choice.id in seen:
                continue
            seen.add(choice.id)
            unique_choices.append(choice)
        return unique_choices

    def assignment_for(self, character: str) -> Any:
        controller = self.controller
        configured = find_voice_assignment(
            controller.settings.voice_assignments,
            character,
        )
        if configured is not None:
            return configured
        voice = controller.voice_router.registry.resolve(character)
        if voice is None:
            return default_voice_choice_id
        return f"character:{normalize_character_name(voice.character)}"

    def preview_choice(self, source_id: str, text: str) -> Any:
        controller = self.controller
        if not controller.is_ready:
            raise RuntimeError("The speech engine is not ready")
        if controller.is_live_running:
            raise RuntimeError("Stop live reading before previewing a voice")
        if not text or not text.strip():
            raise ValueError("Enter preview text")
        choice = next(
            (item for item in self.available_choices() if item.id == source_id),
            None,
        )
        if choice is None:
            raise ValueError("The selected voice is no longer available")
        controller.status_handler(f"Previewing {choice.label} voice")
        return controller.speech_executor.submit(
            controller._preview_voice_choice,
            choice,
            text.strip(),
        )

    def stop_preview(self) -> Any:
        controller = self.controller
        if controller.is_live_running:
            raise RuntimeError("Stop live reading before stopping a voice preview")
        backend = controller.speech_backend
        if isinstance(backend, GeneratedAudioFallbackBackend):
            backend = backend.live_backend
        stop = getattr(backend, "stop", None)
        if callable(stop):
            stop()
            return True
        return False

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
        controller = self.controller
        character = (character or "").strip()
        key = normalize_character_name(character)
        if not key or key == "narrator":
            return False
        controller.pending_unknown_speakers.discard(key)
        controller.narrator_fallback_speakers.add(key)
        controller.narrator_fallback_names[key] = character
        controller.status_handler(f"Using narrator voice for {character}")
        return True

    def unresolved_live_speakers(self) -> Any:
        controller = self.controller
        scope = controller.chapter_voice_preloader.live_voice_preflight_rows()
        if not controller.chapter_voice_preloader.dialogue:
            if controller.live_speaker_corpus_error:
                return None
            if controller.live_speaker_corpus is not None:
                scope = controller.live_speaker_corpus.speakers
        if scope is None:
            return None
        unresolved = []
        seen: set[str] = set()
        for line in scope:
            character = str(getattr(line, "speaker", line) or "").strip()
            text = getattr(line, "text", None)
            key = normalize_character_name(character)
            if key in seen or not controller._speaker_requires_voice_decision(
                character,
                text,
                live_preflight=True,
            ):
                continue
            seen.add(key)
            unresolved.append(character)
        return tuple(unresolved)

    def approve_narrator_fallbacks(self, characters: Any) -> Any:
        controller = self.controller
        if controller.is_live_running:
            raise RuntimeError("Stop live reading before approving narrator fallbacks")
        approved = {}
        for character in characters:
            name = str(character or "").strip()
            key = normalize_character_name(name)
            if not key or is_narrator(name):
                continue
            approved[key] = name
        controller.next_live_narrator_fallback_names = approved
        return tuple(approved.values())

    def preview(self, character: str, text: str) -> Any:
        controller = self.controller
        if not controller.is_ready:
            raise RuntimeError("The speech engine is not ready")
        if controller.is_live_running:
            raise RuntimeError("Stop live reading before previewing a voice")
        if not text or not text.strip():
            raise ValueError("Enter preview text")
        controller.status_handler(f"Previewing {character or 'Narrator'} voice")
        return controller.speech_executor.submit(
            controller._preview_voice,
            character or "Narrator",
            text.strip(),
        )

    def replay(self, character: str, text: str) -> Any:
        return self.preview(character, text)


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
        controller = self.controller
        registry = (
            controller.voice_router.registry
            if controller.voice_router is not None
            else None
        )
        snapshots: list[Any] = []
        analyze_dialog_snapshot(
            get_screenshot_directory(controller.settings),
            registry,
            capture_target=controller.capture_target,
            minimum_confidence=controller.settings.ocr_minimum_confidence,
            diagnostic_handler=snapshots.append,
            voice_resolver=controller._resolve_voice_label,
            ocr_language=controller.settings.ocr_language,
            correction_dictionary=controller.correction_dictionary,
        )
        return controller._publish_diagnostic(snapshots[-1], notify=notify)

    def test_current_dialog(self) -> Any:
        controller = self.controller
        if not controller.is_ready:
            raise RuntimeError("The speech engine is not ready")
        image, _output, result = analyze_dialog_snapshot(
            get_screenshot_directory(controller.settings),
            controller.voice_router.registry,
            capture_target=controller.capture_target,
            minimum_confidence=controller.settings.ocr_minimum_confidence,
            diagnostic_handler=controller._publish_diagnostic,
            voice_resolver=controller._resolve_voice_label,
            ocr_language=controller.settings.ocr_language,
            correction_dictionary=controller.correction_dictionary,
        )
        if result.text and not result.is_confident(
            controller.settings.ocr_minimum_confidence
        ):
            error = OCRUncertainError(
                result,
                controller.settings.ocr_minimum_confidence,
            )
            if controller.uncertain_frame_recorder is not None:
                controller.uncertain_frame_recorder.record(
                    image,
                    error.result,
                    controller.settings.ocr_minimum_confidence,
                )
            raise error
        character, text = result.character, result.text
        if controller.uncertain_frame_recorder is not None:
            controller.uncertain_frame_recorder.reset()
        if is_empty(text):
            raise OCRError("No dialogue text was detected in the calibrated region")
        controller.status_handler(f"Testing OCR and speech with {character}")
        try:
            speak_dialog(
                text,
                lambda value: controller._speak_with_live_backend(character, value),
            )
        finally:
            controller._refresh_diagnostic_metrics()
        return character, text
