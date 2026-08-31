"""Explicit coordination boundaries behind :class:`AppController`."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Protocol

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.dialog import is_empty, speak_dialog
from vntts.dialog_capture import (
    OCRError,
    OCRUncertainError,
    ScreenCaptureError,
    TTSInitializationError,
    analyze_dialog_snapshot,
    dialog_completion_cue_visible,
    dialog_glyphs_visible,
    fingerprint_dialog_frame,
    fingerprint_dialog_render_activity,
    get_screenshot_directory,
)
from vntts.generated_audio import GeneratedAudioFallbackBackend
from vntts.live import IncrementalDialogTracker
from vntts.live_snapshot import read_live_snapshot
from vntts.live_speech import play_typed_text
from vntts.runtime_config import get_tts_configuration
from vntts.settings import preserve_loaded_runtime_settings
from vntts.speech_backend import XTTSVoiceRouterBackend
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
    auto_advance_state_changed: Callable[..., Any]
    chatterbox_backend_factory: Callable[..., Any]
    capture_executor: Any
    capture_target: Any
    chapter_voice_preloader: Any
    correction_dictionary: Any
    dialog_read_scheduler_factory: Callable[..., Any]
    error_handler: Callable[[Exception], Any]
    is_ready: bool
    is_live_running: bool
    last_visible_speaker_key: Any
    live_session: Any
    live_reader: Any
    live_reader_factory: Callable[..., Any]
    live_speech_backpressure: Any
    model_assets: Any
    moss_backend_factory: Callable[..., Any]
    ocr_executor: Any
    playback_executor: Any
    pipeline_event_handler: Callable[..., Any]
    pocket_backend_factory: Callable[..., Any]
    schedule_dialog_read: Any
    settings: Any
    shutdown_requested: Any
    speaker_announcement_lock: Any
    speech_backend: Any
    speech_backpressure_factory: Callable[..., Any]
    speech_executor: Any
    status_handler: Callable[[str], Any]
    thread_pool_executor_factory: Callable[..., Any]
    tts: Any
    tts_factory: Callable[..., Any]
    uncertain_frame_recorder: Any
    voice_router: Any
    voice_registry_initializer: Callable[..., Any]
    voice_router_initializer: Callable[..., Any]
    voice_prime_futures: Any
    voice_prime_lock: Any

    def _auto_advance_state_changed(self, *args: Any, **kwargs: Any) -> Any: ...

    def _capture_live_frame(self) -> Any: ...

    def _capture_state_changed(self, *args: Any, **kwargs: Any) -> Any: ...

    def _confirm_sequence_render_completion(self, *args: Any, **kwargs: Any) -> Any: ...

    def _configure_generated_audio_backend(self) -> Any: ...

    def _create_capture_target(self) -> Any: ...

    def _create_uncertain_frame_recorder(self) -> Any: ...

    def _enqueue_dialog(self, character: str, text: str) -> Any: ...

    def _get_live_configuration(self) -> Any: ...

    def _interrupt_speech(self) -> Any: ...

    def _is_game_focused(self) -> Any: ...

    def _live_sequence_line_id(self, *args: Any, **kwargs: Any) -> Any: ...

    def _live_auto_advance_callback(self) -> Any: ...

    def _load_live_sequence_plan(self) -> Any: ...

    def _load_live_speaker_corpus(self) -> Any: ...

    def _ocr_uncertain(self, result: Any, minimum_confidence: float) -> Any: ...

    def _play_live_chunk(self, *args: Any, **kwargs: Any) -> Any: ...

    def _prepare_live_chunk(self, *args: Any, **kwargs: Any) -> Any: ...

    def _publish_diagnostic(self, snapshot: Any, *, notify: bool = True) -> Any: ...

    def _resolve_voice_label(self, character: str) -> Any: ...

    def _sequence_prefix_recheck_required(self, *args: Any, **kwargs: Any) -> Any: ...

    def _set_backend_live_mode(self, active: bool) -> Any: ...

    def _stop_tts(self) -> Any: ...

    def _stable_live_frame_owner(self, *args: Any, **kwargs: Any) -> Any: ...

    def _stable_live_frame_route(self, *args: Any, **kwargs: Any) -> Any: ...

    def _speak_live_chunk(self, *args: Any, **kwargs: Any) -> Any: ...

    def _warmup_progress(self, *args: Any, **kwargs: Any) -> Any: ...

    def _dialog_observed(self, *args: Any, **kwargs: Any) -> Any: ...

    def _recognize_live_frame(self, *args: Any, **kwargs: Any) -> Any: ...

    def refresh_corrections(self) -> Any: ...


class _LiveSessionPort(Protocol):
    capture_target: Any
    chapter_voice_preloader: Any
    correction_dictionary: Any
    dialog_handler: Callable[[str, str], Any]
    error_handler: Callable[[Exception], Any]
    explicit_sequence_anchor_pending: bool
    is_ready: bool
    is_live_running: bool
    last_visible_speaker_key: Any
    live_reader: Any
    live_scope_identification_failure: str | None
    live_speaker_corpus_error: Any
    live_speech_backpressure: Any
    narrator_fallback_names: dict[str, str]
    narrator_fallback_speakers: set[str]
    next_live_narrator_fallback_names: dict[str, str]
    pending_unknown_speakers: set[str]
    reported_unknown_speakers: set[str]
    schedule_dialog_read: Callable[[], Any]
    settings: Any
    speaker_announcement_lock: Any
    speech_backend: Any
    status_handler: Callable[[str], Any]
    story_cursor: Any
    story_cursor_lock: Any
    uncertain_frame_recorder: Any
    voice_assignments: Any
    voice_router: Any

    def _canonical_observed_character(
        self,
        character: str | None,
        text: str,
    ) -> str: ...

    def _ocr_uncertain(self, result: Any, minimum_confidence: float) -> Any: ...

    def _publish_diagnostic(self, snapshot: Any, *, notify: bool = True) -> Any: ...

    def _resolve_initial_live_sequence_line(
        self,
        character: str,
        text: str,
    ) -> tuple[Any, Any]: ...

    def _resolve_voice_label(self, character: str) -> Any: ...

    def _live_auto_advance_callback(self) -> Any: ...

    def _publish_live_sequence_status(self) -> Any: ...

    def _revalidate_live_speaker_corpus(self) -> bool: ...

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
        controller = self.controller
        if controller.is_ready:
            return True

        controller.shutdown_requested.clear()
        use_xtts = controller.settings.speech_backend == "coqui-xtts"
        controller.status_handler(
            {
                "coqui-xtts": "Loading TTS model...",
                "chatterbox-nano": "Loading Chatterbox Nano...",
                "moss-tts": "Loading MOSS-TTS...",
                "pocket-tts": "Loading Pocket TTS...",
            }[controller.settings.speech_backend]
        )
        if not self._initialize_backend(use_xtts):
            return False
        if controller.shutdown_requested.is_set():
            controller._stop_tts()
            return False

        try:
            if not self._initialize_voice_routing(use_xtts):
                return False
            screenshot_directory = self._construct_live_runtime()
        except Exception as error:
            controller.error_handler(error)
            self.shutdown()
            return False

        if not controller.settings.warm_up_voices:
            controller.status_handler("Speech model loaded; voice warm-up skipped")
        controller.status_handler(
            f"Screenshots will be stored in {screenshot_directory}"
        )
        return True

    def _initialize_backend(self, use_xtts: bool) -> bool:
        controller = self.controller
        try:
            if use_xtts:
                controller.model_assets.configure_environment()
                if controller.settings.xtts_terms_accepted:
                    os.environ["COQUI_TOS_AGREED"] = "1"
                controller.tts = controller.tts_factory(
                    **get_tts_configuration(controller.settings)
                )
                return True
            if controller.settings.speech_backend in {
                "chatterbox-nano",
                "moss-tts",
                "pocket-tts",
            }:
                controller.model_assets.configure_huggingface_environment()
            registry = controller.voice_registry_initializer(
                controller.settings,
                controller.error_handler,
            )
            if registry is None:
                return False
            backend_factory = {
                "chatterbox-nano": controller.chatterbox_backend_factory,
                "moss-tts": controller.moss_backend_factory,
                "pocket-tts": controller.pocket_backend_factory,
            }[controller.settings.speech_backend]
            narrator_reference = controller.settings.tts_speaker_wav
            narrator_source_id = find_voice_assignment(
                controller.settings.voice_assignments,
                "Narrator",
            )
            if narrator_source_id is not None:
                narrator_voice = registry.resolve_source(narrator_source_id)
                if narrator_voice is None:
                    narrator_reference = controller.settings.tts_speaker_wav
                elif narrator_voice.references:
                    narrator_reference = narrator_voice.references[0]
                elif controller.settings.speech_backend == "pocket-tts":
                    narrator_reference = narrator_voice.speaker
            backend_options = {
                "narrator_reference": narrator_reference,
                "volume": controller.settings.output_volume_percent / 100,
            }
            if getattr(backend_factory, "supports_startup_cancellation", False) is True:
                backend_options["startup_cancellation"] = controller.shutdown_requested
            if controller.settings.speech_backend == "moss-tts":
                backend_options.update(
                    model_name=controller.settings.tts_model,
                    language=controller.settings.tts_language or "English",
                    generation_profile=controller.settings.tts_profile,
                )
            controller.tts = backend_factory(registry, **backend_options)
            return True
        except Exception as error:
            controller.error_handler(TTSInitializationError(str(error)))
            return False

    def _initialize_voice_routing(self, use_xtts: bool) -> bool:
        controller = self.controller
        if use_xtts:
            controller.voice_router = controller.voice_router_initializer(
                controller.tts,
                controller.settings,
                controller.error_handler,
            )
            if controller.voice_router is None:
                controller._stop_tts()
                return False
            controller.speech_backend = XTTSVoiceRouterBackend(controller.voice_router)
        else:
            controller.voice_router = controller.tts
            controller.speech_backend = controller.tts
        controller._configure_generated_audio_backend()
        if controller.settings.warm_up_voices:
            controller.status_handler("Warming speech model and voices...")
            try:
                warmed = controller.voice_router.warm_up(
                    progress=controller._warmup_progress
                )
            except Exception as error:
                controller.error_handler(error)
                controller.status_handler(
                    "Voice warm-up was incomplete; voices will load on demand"
                )
            else:
                controller.status_handler(f"Speech model and {warmed} voices ready")
        return True

    def _construct_live_runtime(self) -> Any:
        controller = self.controller
        executor_specs = (
            ("capture_executor", "dialog-capture"),
            ("ocr_executor", "dialog-ocr"),
            ("speech_executor", "dialog-synthesis"),
            ("playback_executor", "dialog-playback"),
        )
        for attribute, thread_name_prefix in executor_specs:
            setattr(
                controller,
                attribute,
                controller.thread_pool_executor_factory(
                    max_workers=1,
                    thread_name_prefix=thread_name_prefix,
                ),
            )
        backend_capabilities = getattr(controller.speech_backend, "capabilities", None)
        can_prepare_during_playback = bool(
            getattr(backend_capabilities, "concurrent_prepare_and_play", True)
        )
        max_speech_jobs = 2 if can_prepare_during_playback else 1
        controller.live_speech_backpressure = controller.speech_backpressure_factory(
            normal_jobs=max_speech_jobs,
        )
        screenshot_directory = get_screenshot_directory(controller.settings)
        controller.live_reader = controller.live_reader_factory(
            capture_executor=controller.capture_executor,
            ocr_executor=controller.ocr_executor,
            speech_executor=controller.speech_executor,
            playback_executor=controller.playback_executor,
            read_snapshot=lambda: read_live_snapshot(
                screenshot_directory,
                controller.voice_router.registry,
                controller.capture_target,
                controller.settings.ocr_minimum_confidence,
                controller._ocr_uncertain,
                controller.uncertain_frame_recorder,
                controller._publish_diagnostic,
                controller._resolve_voice_label,
                controller.settings.ocr_language,
                controller.correction_dictionary,
            ),
            capture_frame=controller._capture_live_frame,
            recognize_frame=controller._recognize_live_frame,
            frame_fingerprint=fingerprint_dialog_frame,
            frame_render_fingerprint=fingerprint_dialog_render_activity,
            frame_presence=dialog_glyphs_visible,
            frame_completion=dialog_completion_cue_visible,
            frame_recheck_required=controller._sequence_prefix_recheck_required,
            render_completion=controller._confirm_sequence_render_completion,
            stable_frame_route=controller._stable_live_frame_route,
            stable_frame_owner=controller._stable_live_frame_owner,
            line_id_resolver=controller._live_sequence_line_id,
            speak_chunk=controller._speak_live_chunk,
            prepare_chunk=controller._prepare_live_chunk,
            play_prepared=controller._play_live_chunk,
            report_error=controller.error_handler,
            interrupt_speech=controller._interrupt_speech,
            dialog_observed=controller._dialog_observed,
            focus_probe=controller._is_game_focused,
            capture_state_changed=controller._capture_state_changed,
            tracker_factory=IncrementalDialogTracker,
            auto_advance=controller._live_auto_advance_callback(),
            require_visible_auto_advance=(
                controller.settings.live_sequence_mode == "audio-auto"
            ),
            auto_advance_delay_seconds=(
                controller.settings.auto_advance_delay_ms / 1000
            ),
            auto_advance_state_changed=controller._auto_advance_state_changed,
            pipeline_event_handler=controller.pipeline_event_handler,
            max_speech_jobs=max_speech_jobs,
            interrupt_on_dialog_replacement=bool(
                getattr(
                    backend_capabilities,
                    "interrupt_on_dialog_replacement",
                    False,
                )
            ),
            first_pcm_on_prepare=False,
            **controller._get_live_configuration(),
        )
        controller.schedule_dialog_read = controller.dialog_read_scheduler_factory(
            controller.capture_executor,
            controller.voice_router,
            screenshot_directory,
            live_reader=controller.live_reader,
            error_handler=controller.error_handler,
            capture_target=controller.capture_target,
            speech_handler=controller._enqueue_dialog,
            minimum_confidence=controller.settings.ocr_minimum_confidence,
            uncertain_frame_recorder=controller.uncertain_frame_recorder,
            diagnostic_handler=controller._publish_diagnostic,
            voice_resolver=controller._resolve_voice_label,
            ocr_language=controller.settings.ocr_language,
            correction_dictionary=controller.correction_dictionary,
        )
        return screenshot_directory

    def apply_settings(self, settings: Any, *, cancellation: Any = None) -> Any:
        self.settings_apply_guard.begin(cancellation)
        try:
            return self._apply_settings(
                settings,
                commit=lambda: self.settings_apply_guard.commit(cancellation),
            )
        finally:
            self.settings_apply_guard.finish(cancellation)

    def _apply_settings(
        self,
        settings: Any,
        *,
        commit: Callable[[], bool],
    ) -> Any:
        controller = self.controller
        if controller.tts is not None or controller.speech_backend is not None:
            settings = preserve_loaded_runtime_settings(controller.settings, settings)
        was_live = controller.is_live_running
        if was_live:
            controller._set_backend_live_mode(False)
            controller.live_reader.stop()
            controller.live_reader.wait()

        if not commit():
            if was_live:
                controller.live_session.toggle()
            return False

        controller.settings = settings
        with controller.speaker_announcement_lock:
            controller.last_visible_speaker_key = None
        controller.chapter_voice_preloader = ChapterVoicePreloader.load_optional(
            controller.settings.story_index
        )
        controller._load_live_sequence_plan()
        controller._load_live_speaker_corpus()
        controller._configure_generated_audio_backend()
        controller.refresh_corrections()
        controller.capture_target = controller._create_capture_target()
        controller.uncertain_frame_recorder = (
            controller._create_uncertain_frame_recorder()
        )
        if controller.tts is not None:
            controller.tts.set_volume(controller.settings.output_volume_percent / 100)
            controller.tts.set_speed(controller.settings.speech_rate_percent / 100)
        if controller.speech_backend is not None:
            set_volume = getattr(controller.speech_backend, "set_volume", None)
            set_speed = getattr(controller.speech_backend, "set_speed", None)
            set_generation_profile = getattr(
                controller.speech_backend,
                "set_generation_profile",
                None,
            )
            if callable(set_volume):
                set_volume(controller.settings.output_volume_percent / 100)
            if callable(set_speed):
                set_speed(controller.settings.speech_rate_percent / 100)
            if callable(set_generation_profile):
                set_generation_profile(controller.settings.tts_profile)
        if controller.live_reader is None:
            return None

        screenshot_directory = get_screenshot_directory(controller.settings)
        controller.live_reader.read_snapshot = lambda: read_live_snapshot(
            screenshot_directory,
            controller.voice_router.registry,
            controller.capture_target,
            controller.settings.ocr_minimum_confidence,
            controller._ocr_uncertain,
            controller.uncertain_frame_recorder,
            controller._publish_diagnostic,
            controller._resolve_voice_label,
            controller.settings.ocr_language,
            controller.correction_dictionary,
        )
        live_configuration = controller._get_live_configuration()
        controller.live_reader.interval_seconds = live_configuration["interval_seconds"]
        controller.live_reader.tracker_options = live_configuration["tracker_options"]
        controller.live_reader.require_visible_auto_advance = (
            controller.settings.live_sequence_mode == "audio-auto"
        )
        controller.live_reader.set_auto_advance(
            controller._live_auto_advance_callback()
        )
        controller.live_reader.auto_advance_delay_seconds = (
            controller.settings.auto_advance_delay_ms / 1000
        )
        controller.schedule_dialog_read = controller.dialog_read_scheduler_factory(
            controller.capture_executor,
            controller.voice_router,
            screenshot_directory,
            live_reader=controller.live_reader,
            error_handler=controller.error_handler,
            capture_target=controller.capture_target,
            speech_handler=controller._enqueue_dialog,
            minimum_confidence=controller.settings.ocr_minimum_confidence,
            uncertain_frame_recorder=controller.uncertain_frame_recorder,
            diagnostic_handler=controller._publish_diagnostic,
            voice_resolver=controller._resolve_voice_label,
            ocr_language=controller.settings.ocr_language,
            correction_dictionary=controller.correction_dictionary,
        )
        if was_live:
            controller.live_session.toggle()
        return True

    def cancel_settings_apply(self, cancellation: Any) -> bool:
        reader = self.controller.live_reader
        release_waiters = reader.release_waiters if reader is not None else lambda: None
        return self.settings_apply_guard.cancel(cancellation, release_waiters)

    def shutdown(self) -> Any:
        controller = self.controller
        controller.shutdown_requested.set()
        with controller.voice_prime_lock:
            voice_prime_futures = tuple(controller.voice_prime_futures)
        for future in voice_prime_futures:
            future.cancel()
        controller._interrupt_speech()
        controller._set_backend_live_mode(False)
        if controller.live_reader is not None:
            controller.live_reader.stop()
            controller.live_reader.clear_queue()
            controller.live_reader.release_waiters()
            try:
                controller.live_reader.wait()
            except Exception as error:
                controller.error_handler(error)
            controller.live_reader = None

        for attribute in (
            "capture_executor",
            "ocr_executor",
            "speech_executor",
            "playback_executor",
        ):
            executor = getattr(controller, attribute)
            if executor is not None:
                executor.shutdown(wait=True)
                setattr(controller, attribute, None)
        controller.schedule_dialog_read = None
        controller._stop_tts()


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
        controller = self.controller
        if not controller.is_ready or controller.is_live_running:
            return False
        controller.live_scope_identification_failure = None
        character, text = read_live_snapshot(
            get_screenshot_directory(controller.settings),
            controller.voice_router.registry,
            controller.capture_target,
            controller.settings.ocr_minimum_confidence,
            controller._ocr_uncertain,
            controller.uncertain_frame_recorder,
            controller._publish_diagnostic,
            controller._resolve_voice_label,
            controller.settings.ocr_language,
            controller.correction_dictionary,
        )
        if is_empty(text):
            controller.live_scope_identification_failure = "no-dialog-text"
            return False
        character = controller._canonical_observed_character(character, text)
        line, _match_result = controller._resolve_initial_live_sequence_line(
            character,
            text,
        )
        if line is None:
            controller.live_scope_identification_failure = "story-line-no-match"
            return False
        controller.dialog_handler(line.speaker, line.text)
        return True

    def toggle(self) -> Any:
        controller = self.controller
        if not controller.is_ready:
            return False
        starting = not controller.live_reader.is_running
        if starting and not self._voice_preflight_allows_start():
            return False
        if starting and controller.capture_target is not None:
            try:
                controller.capture_target.get_geometry()
            except Exception as error:
                controller.error_handler(ScreenCaptureError(str(error)))
                controller.status_handler("Live reading could not start")
                return False
        if starting:
            # Unknown-speaker prompts are deduplicated within one live session,
            # not for the entire application lifetime.
            controller.reported_unknown_speakers.clear()
            controller.pending_unknown_speakers.clear()
            controller.narrator_fallback_speakers.clear()
            controller.narrator_fallback_names.clear()
            controller.narrator_fallback_speakers.update(
                controller.next_live_narrator_fallback_names
            )
            controller.narrator_fallback_names.update(
                controller.next_live_narrator_fallback_names
            )
            with controller.speaker_announcement_lock:
                controller.last_visible_speaker_key = None
            with controller.story_cursor_lock:
                if controller.story_cursor is not None:
                    if controller.explicit_sequence_anchor_pending:
                        controller.explicit_sequence_anchor_pending = False
                    else:
                        controller.story_cursor.reset("live-session-started")
                    controller._publish_live_sequence_status()
        running = controller.live_reader.toggle()
        if running:
            controller.next_live_narrator_fallback_names.clear()
            controller.live_reader.max_speech_jobs = (
                controller.live_speech_backpressure.reset()
            )
        elif not starting:
            controller.narrator_fallback_speakers.clear()
            controller.narrator_fallback_names.clear()
        controller._set_backend_live_mode(running)
        controller.status_handler(
            "Live reading started" if running else "Live reading stopping"
        )
        return running

    def _voice_preflight_allows_start(self) -> bool:
        controller = self.controller
        if (
            not controller.chapter_voice_preloader.dialogue
            and not controller._revalidate_live_speaker_corpus()
        ):
            controller.status_handler(
                "Live reading could not start: configured speaker corpus is "
                f"invalid: {controller.live_speaker_corpus_error}"
            )
            return False
        unresolved = controller.voice_assignments.unresolved_live_speakers()
        if unresolved is None:
            if controller.live_speaker_corpus_error:
                controller.status_handler(
                    "Live reading could not start: configured speaker corpus is "
                    f"invalid: {controller.live_speaker_corpus_error}"
                )
            else:
                controller.status_handler(
                    "Live reading could not start: read the current dialog once to "
                    "identify the story chapter"
                )
            return False
        unresolved = tuple(unresolved)
        approved = tuple(controller.next_live_narrator_fallback_names.values())
        if not unresolved:
            if approved:
                controller.next_live_narrator_fallback_names.clear()
                controller.status_handler(
                    "Live reading could not start: voice preflight scope changed; "
                    "start live reading again"
                )
                return False
            controller.next_live_narrator_fallback_names.clear()
            return True
        if approved == unresolved:
            return True
        controller.next_live_narrator_fallback_names.clear()
        controller.status_handler(
            "Live reading could not start: choose voices or explicitly approve "
            f"Narrator for {', '.join(unresolved)}"
        )
        return False

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
