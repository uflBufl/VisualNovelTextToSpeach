"""Explicit coordination boundaries behind :class:`AppController`."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Callable, Protocol, TypeGuard, runtime_checkable
from uuid import uuid4

if TYPE_CHECKING:
    from vntts.controller import AppController

from vntts.auto_advance_policy import auto_advance_control_state
from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.dialog import is_empty, speak_dialog
from vntts.dialog_capture import (
    DiagnosticSnapshot,
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
from vntts.playback import PlaybackOutcome, PreparedPlayback
from vntts.runtime_config import get_tts_configuration
from vntts.settings import AppSettings, preserve_loaded_runtime_settings
from vntts.speech_backend import XTTSVoiceRouterBackend
from vntts.speech_backend_contract import SpeechBackendCapabilities
from vntts.synthesis import SynthesisCachePolicy
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    VoiceChoice,
    VoiceEngine,
    default_voice_choice_id,
    is_narrator,
    normalize_character_name,
    pocket_tts_preset_voices,
    remember_voice_binding,
    voice_binding_source_id,
)
from vntts.window_capture import WindowGeometry


class _LiveToggle(Protocol):
    def toggle(self) -> bool: ...


class _SpeechChunk(Protocol):
    character: str
    text: str


class _Cancellation(Protocol):
    def is_set(self) -> bool: ...
    def set(self) -> None: ...


@runtime_checkable
class _TypedPlaybackBackend(Protocol):
    def prepare_playback(self, character: str, text: str) -> PreparedPlayback: ...

    def play_prepared(
        self,
        prepared: PreparedPlayback,
        *,
        playback_guard: Callable[[], bool] | None = None,
    ) -> PlaybackOutcome: ...


@runtime_checkable
class _LiveVoiceRouter(_TypedPlaybackBackend, Protocol):
    registry: CharacterVoiceRegistry
    narrator_voice: CharacterVoice | None
    name: str
    capabilities: SpeechBackendCapabilities

    def stop(self) -> bool: ...

    def warm_up(self, *, progress: Callable[[int, int, str], None]) -> int: ...

    def set_volume(self, volume: float) -> None: ...

    def set_speed(self, speed: float) -> None: ...


@runtime_checkable
class _XTTSVoiceRouter(Protocol):
    tts: object
    registry: CharacterVoiceRegistry
    narrator_voice: CharacterVoice | None

    def prepare_playback(
        self,
        character: str,
        text: str,
        *,
        synthesis_options: Mapping[str, object] | None,
        cache_policy: SynthesisCachePolicy,
        cancellation: Callable[[], bool],
    ) -> PreparedPlayback: ...

    def play_prepared(
        self,
        prepared: PreparedPlayback,
        *,
        playback_guard: Callable[[], bool] | None,
    ) -> PlaybackOutcome: ...

    def warm_up(self, *, progress: Callable[[int, int, str], None]) -> int: ...


def _is_voice_engine(value: object) -> TypeGuard[VoiceEngine]:
    return all(
        callable(getattr(value, name, None))
        for name in (
            "speak",
            "synthesize",
            "prepare_synthesis",
            "play",
            "play_prepared",
            "has_speaker",
        )
    )


def _is_live_voice_router(value: object) -> TypeGuard[_LiveVoiceRouter]:
    return value is not None and all(
        callable(getattr(value, name, None))
        for name in (
            "prepare_playback",
            "play_prepared",
            "stop",
            "warm_up",
            "set_volume",
            "set_speed",
        )
    )


def _is_xtts_voice_router(value: object) -> TypeGuard[_XTTSVoiceRouter]:
    return value is not None and all(
        callable(getattr(value, name, None))
        for name in ("prepare_playback", "play_prepared", "warm_up")
    )


def create_live_toggle(live_reader: _LiveToggle) -> Callable[[], None]:
    def toggle_live_reading() -> None:
        if live_reader.toggle():
            print("Live reading started")
        else:
            print("Live reading stopping")

    return toggle_live_reading


def speak_live_chunk(
    voice_router: _TypedPlaybackBackend,
    chunk: _SpeechChunk,
    playback_guard: Callable[[], bool] | None = None,
) -> object:
    print(f"{chunk.character} is speaking now (live)")
    print(chunk.text)
    if is_empty(chunk.text):
        return None
    return play_typed_text(voice_router, chunk.character, chunk.text, playback_guard)


class _RuntimeSettingsApplyGuard:
    def __init__(self) -> None:
        self.lock = Lock()
        self.cancellation: _Cancellation | None = None
        self.committed = False

    def begin(self, cancellation: _Cancellation | None) -> None:
        if cancellation is None:
            return
        with self.lock:
            if self.cancellation is not None:
                raise RuntimeError("Runtime settings are already being applied")
            self.cancellation = cancellation
            self.committed = False

    def finish(self, cancellation: _Cancellation | None) -> None:
        if cancellation is None:
            return
        with self.lock:
            if self.cancellation is cancellation:
                self.cancellation = None
                self.committed = False

    def commit(self, cancellation: _Cancellation | None) -> bool:
        if cancellation is None:
            return True
        with self.lock:
            if self.cancellation is not cancellation or cancellation.is_set():
                return False
            self.committed = True
            return True

    def cancel(
        self,
        cancellation: _Cancellation,
        release_waiters: Callable[[], object],
    ) -> bool:
        with self.lock:
            if self.cancellation is not cancellation or self.committed:
                return False
            cancellation.set()
        release_waiters()
        return True


@dataclass(frozen=True)
class RuntimeLifecycleComponent:
    controller: AppController
    settings_apply_guard: _RuntimeSettingsApplyGuard = field(
        default_factory=_RuntimeSettingsApplyGuard,
        compare=False,
        repr=False,
    )

    def start(self) -> bool:
        controller = self.controller
        if controller.is_ready:
            return True
        if controller.shutdown_requested.is_set():
            return False
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
                tts = controller.tts_factory(
                    **get_tts_configuration(controller.settings)
                )
                if not _is_voice_engine(tts):
                    raise TypeError("XTTS engine does not implement voice routing")
                controller.tts = tts
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
            narrator_reference: str | Path | None = controller.settings.tts_speaker_wav
            narrator_voice = registry.resolve("Narrator")
            if isinstance(narrator_voice, CharacterVoice):
                if narrator_voice.references:
                    narrator_reference = narrator_voice.references[0]
                elif controller.settings.speech_backend == "pocket-tts":
                    narrator_reference = narrator_voice.speaker
            if narrator_reference is None:
                narrator_voice = registry.resolve("Narrator")
                references = getattr(narrator_voice, "references", ())
                if isinstance(references, (tuple, list)) and references:
                    narrator_reference = references[0]
            backend_options: dict[str, object] = {
                "narrator_reference": narrator_reference,
                "volume": controller.settings.output_volume_percent / 100,
            }
            if controller.settings.speech_backend == "pocket-tts":
                backend_options["allow_gated_model_access"] = (
                    controller.settings.pocket_gated_model_accepted
                )
            if getattr(backend_factory, "supports_startup_cancellation", False) is True:
                backend_options["startup_cancellation"] = controller.shutdown_requested
            if getattr(backend_factory, "supports_startup_progress", False) is True:
                backend_options["startup_progress"] = controller.status_handler
            if controller.settings.speech_backend == "moss-tts":
                backend_options.update(
                    model_name=controller.settings.tts_model,
                    language=controller.settings.tts_language or "English",
                    generation_profile=controller.settings.tts_profile,
                )
            backend = backend_factory(registry, **backend_options)
            if not _is_live_voice_router(backend):
                raise TypeError("Speech backend does not implement typed voice routing")
            controller.tts = backend
            return True
        except Exception as error:
            controller.error_handler(TTSInitializationError(str(error)))
            return False

    def _initialize_voice_routing(self, use_xtts: bool) -> bool:
        controller = self.controller
        if use_xtts:
            tts = controller.tts
            if not _is_voice_engine(tts):
                raise TypeError("XTTS engine does not implement voice routing")
            voice_router = controller.voice_router_initializer(
                tts,
                controller.settings,
                controller.error_handler,
            )
            if voice_router is None:
                controller._stop_tts()
                return False
            if not _is_xtts_voice_router(voice_router):
                raise TypeError("XTTS voice router does not implement typed playback")
            controller.voice_router = voice_router
            controller.speech_backend = XTTSVoiceRouterBackend(voice_router)
        else:
            if not _is_live_voice_router(controller.tts):
                raise TypeError("Speech backend does not implement typed voice routing")
            controller.voice_router = controller.tts
            controller.speech_backend = controller.tts
        controller._configure_generated_audio_backend()
        if controller.settings.warm_up_voices:
            warmup_router = controller.voice_router
            if warmup_router is None:
                return False
            controller.status_handler("Warming speech model and voices...")
            try:
                warmed = warmup_router.warm_up(progress=controller._warmup_progress)
            except Exception as error:
                controller.error_handler(error)
                controller.status_handler(
                    "Voice warm-up was incomplete; voices will load on demand"
                )
            else:
                controller.status_handler(f"Speech model and {warmed} voices ready")
        return True

    def _construct_live_runtime(self) -> object:
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
        session_id = uuid4().hex
        controller.live_reader_session_id = session_id
        live_reader = controller.live_reader_factory(
            capture_executor=controller.capture_executor,
            ocr_executor=controller.ocr_executor,
            speech_executor=controller.speech_executor,
            playback_executor=controller.playback_executor,
            capture_frame=controller._capture_live_frame,
            recognize_frame=controller._recognize_live_frame,
            frame_fingerprint=fingerprint_dialog_frame,
            frame_render_fingerprint=fingerprint_dialog_render_activity,
            frame_presence=dialog_glyphs_visible,
            frame_completion=dialog_completion_cue_visible,
            frame_recheck_required=controller._sequence_prefix_recheck_required,
            ocr_purpose=controller._live_ocr_purpose,
            render_completion=controller._confirm_sequence_render_completion,
            stable_frame_route=controller._stable_live_frame_route,
            stable_frame_owner=controller._stable_live_frame_owner,
            line_id_resolver=controller._live_sequence_line_id,
            prepare_chunk=controller._prepare_live_chunk,
            play_prepared=controller._play_live_chunk,
            report_error=controller.error_handler,
            interrupt_speech=controller._interrupt_speech,
            dialog_observed=controller._dialog_observed,
            focus_probe=controller._is_game_focused,
            capture_state_changed=controller._capture_state_changed,
            tracker_factory=IncrementalDialogTracker,
            auto_advance=controller._live_auto_advance_callback(),
            require_visible_auto_advance=controller._live_sequence_audio_active(),
            auto_advance_delay_seconds=(
                controller.settings.auto_advance_delay_ms / 1000
            ),
            auto_advance_state_changed=controller._auto_advance_state_changed,
            pipeline_event_handler=partial(
                controller.pipeline_event_handler,
                session_id=session_id,
            ),
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
        controller.live_reader = live_reader
        capture_executor = controller.capture_executor
        voice_router = controller.voice_router
        if capture_executor is None or voice_router is None:
            return screenshot_directory
        controller.schedule_dialog_read = controller.dialog_read_scheduler_factory(
            capture_executor,
            voice_router,
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

    def apply_settings(
        self, settings: AppSettings, *, cancellation: _Cancellation | None = None
    ) -> object:
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
        settings: AppSettings,
        *,
        commit: Callable[[], bool],
    ) -> object:
        controller = self.controller
        if controller.tts is not None or controller.speech_backend is not None:
            settings = preserve_loaded_runtime_settings(controller.settings, settings)
        was_live = self._stop_live_for_settings()
        if was_live is None:
            return False

        if not commit():
            if was_live:
                controller.live_session.toggle()
            return False

        self._refresh_runtime_settings(settings)
        reader_update = self._refresh_live_reader_settings()
        if reader_update is not True:
            return reader_update
        if was_live:
            controller.live_session.toggle()
        return True

    def _stop_live_for_settings(self) -> bool | None:
        controller = self.controller
        if not controller.is_live_running:
            return False
        reader = controller.live_reader
        if reader is None:
            return None
        controller._set_backend_live_mode(False)
        reader.stop()
        reader.wait()
        return True

    def _refresh_runtime_settings(self, settings: AppSettings) -> None:
        controller = self.controller
        controller.settings = settings
        with controller.speaker_announcement_lock:
            controller.last_visible_speaker_key = None
        controller.chapter_voice_preloader = ChapterVoicePreloader.load_optional(
            settings.story_index
        )
        controller._load_live_sequence_plan()
        controller._load_live_speaker_corpus()
        controller._configure_generated_audio_backend()
        controller.refresh_corrections()
        controller.capture_target = controller._create_capture_target()
        controller.uncertain_frame_recorder = (
            controller._create_uncertain_frame_recorder()
        )
        self._apply_runtime_audio_settings()

    def _apply_runtime_audio_settings(self) -> None:
        controller = self.controller
        tts = controller.tts
        set_tts_volume = getattr(tts, "set_volume", None)
        set_tts_speed = getattr(tts, "set_speed", None)
        if callable(set_tts_volume):
            set_tts_volume(controller.settings.output_volume_percent / 100)
        if callable(set_tts_speed):
            set_tts_speed(controller.settings.speech_rate_percent / 100)
        backend = controller.speech_backend
        if backend is None:
            return
        set_volume = getattr(backend, "set_volume", None)
        set_speed = getattr(backend, "set_speed", None)
        set_generation_profile = getattr(backend, "set_generation_profile", None)
        if callable(set_volume):
            set_volume(controller.settings.output_volume_percent / 100)
        if callable(set_speed):
            set_speed(controller.settings.speech_rate_percent / 100)
        if callable(set_generation_profile):
            set_generation_profile(controller.settings.tts_profile)

    def _refresh_live_reader_settings(self) -> bool | None:
        controller = self.controller
        reader = controller.live_reader
        if reader is None:
            return None
        screenshot_directory = get_screenshot_directory(controller.settings)
        live_configuration = controller._get_live_configuration()
        interval_seconds = live_configuration["interval_seconds"]
        tracker_options = live_configuration["tracker_options"]
        if not isinstance(interval_seconds, (int, float)) or not isinstance(
            tracker_options, dict
        ):
            return None
        reader.interval_seconds = float(interval_seconds)
        reader.tracker_options = tracker_options
        reader.require_visible_auto_advance = controller._live_sequence_audio_active()
        reader.set_auto_advance(controller._live_auto_advance_callback())
        reader.auto_advance_delay_seconds = (
            controller.settings.auto_advance_delay_ms / 1000
        )
        capture_executor = controller.capture_executor
        voice_router = controller.voice_router
        if capture_executor is None or voice_router is None:
            return None
        controller.schedule_dialog_read = controller.dialog_read_scheduler_factory(
            capture_executor,
            voice_router,
            screenshot_directory,
            live_reader=reader,
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
        return True

    def cancel_settings_apply(self, cancellation: _Cancellation) -> bool:
        reader = self.controller.live_reader
        release_waiters = reader.release_waiters if reader is not None else lambda: None
        return self.settings_apply_guard.cancel(cancellation, release_waiters)

    def shutdown(self) -> None:
        controller = self.controller
        live_reader_timed_out = False
        controller.shutdown_requested.set()
        with controller.voice_prime_lock:
            voice_prime_futures = tuple(controller.voice_prime_futures)
        for future in voice_prime_futures:
            future.cancel()
        controller._interrupt_speech()
        controller._set_backend_live_mode(False)
        if controller.live_reader is not None:
            controller.live_reader.emergency_stop()
            try:
                controller.live_reader.wait(timeout_seconds=5.0)
            except FutureTimeoutError as error:
                live_reader_timed_out = True
                controller.error_handler(error)
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
                executor.shutdown(
                    wait=not live_reader_timed_out,
                    cancel_futures=live_reader_timed_out,
                )
                setattr(controller, attribute, None)
        controller.schedule_dialog_read = None
        controller._stop_tts()


@dataclass(frozen=True)
class LiveSessionComponent:
    controller: AppController

    def read_once(self) -> bool:
        controller = self.controller
        reader = controller.live_reader
        schedule = controller.schedule_dialog_read
        if reader is None or schedule is None:
            return False
        reader.resume_after_emergency()
        accepted = schedule()
        if accepted:
            controller.status_handler("Reading current dialog")
        return bool(accepted)

    def identify_scope(self) -> bool:
        controller = self.controller
        if not controller.is_ready or controller.is_live_running:
            return False
        voice_router = controller.voice_router
        if voice_router is None:
            return False
        controller.live_scope_identification_failure = None
        controller.live_scope_identification_match_result = None
        controller.live_scope_identification_diagnostics = {}
        character, text = read_live_snapshot(
            get_screenshot_directory(controller.settings),
            voice_router.registry,
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
        observed_character = character
        character = controller._canonical_observed_character(
            character or "Narrator", text
        )
        line, match_result = controller._resolve_initial_live_sequence_line(
            character,
            text,
        )
        controller.live_scope_identification_match_result = str(match_result)
        latest = controller.get_latest_diagnostic()
        controller.live_scope_identification_diagnostics = {
            **controller.chapter_voice_preloader.last_resolution_diagnostics,
            "live_sequence_mode": controller.settings.live_sequence_mode,
            "plan_speech_line_count": (
                sum(
                    event.is_speech
                    for event in controller.live_sequence_plan.events.values()
                )
                if controller.live_sequence_plan is not None
                else 0
            ),
            "speaker_canonicalized": observed_character != character,
            "ocr_confidence": round(float(getattr(latest, "confidence", 0.0)), 2),
            "correction_count": len(getattr(latest, "corrections", ()) or ()),
        }
        if line is None:
            controller.live_scope_identification_failure = "story-line-no-match"
            return False
        controller.dialog_handler(line.speaker, line.text)
        return True

    def toggle(self) -> bool:
        controller = self.controller
        reader = controller.live_reader
        if reader is None:
            return False
        starting = not reader.is_running
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
        running = reader.toggle()
        if running:
            controller.next_live_narrator_fallback_names.clear()
            reader.max_speech_jobs = controller.live_speech_backpressure.reset()
        elif not starting:
            controller.narrator_fallback_speakers.clear()
            controller.narrator_fallback_names.clear()
            controller.allow_unscoped_live_reading = False
        controller._set_backend_live_mode(running)
        controller.status_handler(
            "Live reading started" if running else "Live reading stopping"
        )
        return bool(running)

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
        if controller.voice_assignments.unresolved_live_speakers() is None:
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
        return True

    def toggle_speech_pause(self) -> bool:
        controller = self.controller
        reader = controller.live_reader
        if reader is None:
            return False
        paused = reader.toggle_pause()
        controller.status_handler("Speech paused" if paused else "Speech resumed")
        return bool(paused)

    def skip_current_speech(self) -> bool:
        controller = self.controller
        reader = controller.live_reader
        if reader is None:
            return False
        skipped = reader.skip_current()
        controller.status_handler(
            "Skipped current speech" if skipped else "Nothing is currently speaking"
        )
        return bool(skipped)

    def repeat_last_speech(self) -> bool:
        controller = self.controller
        reader = controller.live_reader
        if reader is None:
            return False
        repeated = reader.repeat_last()
        controller.status_handler(
            "Repeating last speech" if repeated else "No previous speech to repeat"
        )
        return bool(repeated)

    def clear_speech_queue(self) -> bool:
        controller = self.controller
        reader = controller.live_reader
        if reader is None:
            return False
        cleared = reader.clear_queue()
        controller.status_handler("Speech queue cleared")
        return bool(cleared)

    def emergency_stop(self) -> bool:
        controller = self.controller
        reader = controller.live_reader
        if reader is None:
            return False
        stopped = reader.emergency_stop()
        controller.allow_unscoped_live_reading = False
        controller._set_backend_live_mode(False)
        controller.status_handler("Emergency stop: live reading and speech stopped")
        return bool(stopped)

    def set_auto_advance_enabled(self, enabled: bool) -> bool:
        controller = self.controller
        allowed, effective, reason = auto_advance_control_state(
            controller.settings.capture_mode,
            controller.settings.live_sequence_mode,
            enabled,
        )
        controller.settings = controller.settings.updated(
            auto_advance_enabled=effective
        )
        if controller.live_reader is not None:
            controller.live_reader.set_auto_advance(
                controller._live_auto_advance_callback()
            )
        controller.status_handler(
            reason
            if enabled and not allowed
            else "Auto advance enabled"
            if effective
            else "Auto advance disabled"
        )
        return bool(effective)


@dataclass(frozen=True)
class VoiceAssignmentComponent:
    controller: AppController

    def available_characters(self) -> list[str]:
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

    def available_choices(self) -> list[VoiceChoice]:
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

    def assignment_for(self, character: str) -> str | None:
        controller = self.controller
        binding = controller.voice_library.binding(character)
        return str(voice_binding_source_id(binding)) if binding is not None else None

    def preview_choice(self, source_id: str, text: str) -> object:
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
        executor = controller.speech_executor
        if executor is None:
            raise RuntimeError("The speech engine is not ready")
        return executor.submit(
            controller._preview_voice_choice,
            choice,
            text.strip(),
        )

    def stop_preview(self) -> bool:
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
        commit_settings: Callable[[AppSettings], object] | None = None,
    ) -> AppSettings:
        character = (character or "").strip()
        if not character:
            raise ValueError("Enter a narrator or character name")
        controller = self.controller
        if controller.is_live_running:
            raise RuntimeError("Stop live reading before changing a voice")
        voice_router = controller.voice_router
        if voice_router is None:
            raise RuntimeError("The speech engine is not ready")
        choice = next(
            (item for item in self.available_choices() if item.id == source_id),
            None,
        )
        if choice is None:
            raise ValueError("The selected voice is no longer available")
        character_key = normalize_character_name(character)
        updated_settings = controller.settings
        if commit_settings is not None:
            commit_settings(updated_settings)
        remember_voice_binding(
            controller.voice_library,
            voice_router.registry,
            character,
            source_id,
            method="manual",
            evidence={"selected_in": "live-voice-controls"},
            algorithm="live-voice-controls-v1",
        )
        voice_router.registry.set_assignment(character, source_id)
        controller.settings = updated_settings
        if character_key == "narrator":
            controller._apply_narrator_voice(
                voice_router.registry.resolve_source(source_id)
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
        commit_settings: Callable[[AppSettings], object] | None = None,
    ) -> AppSettings:
        character = (character or "").strip()
        if not character:
            raise ValueError("Enter a narrator or character name")
        controller = self.controller
        if controller.is_live_running:
            raise RuntimeError("Stop live reading before changing a voice")
        voice_router = controller.voice_router
        if voice_router is None:
            raise RuntimeError("The speech engine is not ready")
        character_key = normalize_character_name(character)
        if character_key == "narrator":
            updated_settings = controller.settings.updated(
                force_live_narrator=False,
            )
        else:
            updated_settings = controller.settings
        if commit_settings is not None:
            commit_settings(updated_settings)
        controller.voice_library.clear(character)
        voice_router.registry.assignments.pop(character_key, None)
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
        commit_settings: Callable[[AppSettings], object] | None = None,
    ) -> AppSettings:
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

    def allow_narrator_fallback(self, character: str) -> bool:
        controller = self.controller
        character = (character or "").strip()
        key = normalize_character_name(character)
        if not key or key == "narrator":
            return False
        controller.pending_unknown_speakers.discard(key)
        controller.narrator_fallback_speakers.add(key)
        controller.narrator_fallback_names[key] = character
        if not controller.is_live_running:
            controller.next_live_narrator_fallback_names[key] = character
        controller.status_handler(f"Using narrator voice for {character}")
        return True

    def unresolved_live_speakers(self) -> tuple[str, ...] | None:
        controller = self.controller
        if (
            (
                controller.allow_unscoped_live_reading
                or controller.settings.audio_source_policy == "live-tts-only"
            )
            and not controller._live_sequence_audio_active()
            and not controller.settings.live_speaker_corpus
        ):
            return ()
        scope: Sequence[object] | None = (
            controller.chapter_voice_preloader.live_voice_preflight_rows()
        )
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

    def approve_narrator_fallbacks(
        self, characters: Iterable[object]
    ) -> tuple[str, ...]:
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

    def preview(self, character: str, text: str) -> object:
        controller = self.controller
        if not controller.is_ready:
            raise RuntimeError("The speech engine is not ready")
        if controller.is_live_running:
            raise RuntimeError("Stop live reading before previewing a voice")
        if not text or not text.strip():
            raise ValueError("Enter preview text")
        controller.status_handler(f"Previewing {character or 'Narrator'} voice")
        executor = controller.speech_executor
        if executor is None:
            raise RuntimeError("The speech engine is not ready")
        return executor.submit(
            controller._preview_voice,
            character or "Narrator",
            text.strip(),
        )

    def replay(self, character: str, text: str) -> object:
        return self.preview(character, text)


@dataclass(frozen=True)
class DiagnosticsComponent:
    controller: AppController

    def capture_geometry(self) -> WindowGeometry | None:
        target = self.controller.capture_target
        return None if target is None else target.get_geometry()

    def latest(self) -> object:
        with self.controller.diagnostic_lock:
            return self.controller.last_diagnostic

    def pipeline_metrics(self) -> object:
        reader = self.controller.live_reader
        return None if reader is None else reader.get_pipeline_metrics()

    def inspect_current_dialog(self, *, notify: bool = True) -> object:
        controller = self.controller
        registry = (
            controller.voice_router.registry
            if controller.voice_router is not None
            else None
        )
        snapshots: list[DiagnosticSnapshot] = []
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

    def test_current_dialog(self) -> tuple[str, str]:
        controller = self.controller
        if not controller.is_ready:
            raise RuntimeError("The speech engine is not ready")
        voice_router = controller.voice_router
        if voice_router is None:
            raise RuntimeError("The speech engine is not ready")
        image, _output, result = analyze_dialog_snapshot(
            get_screenshot_directory(controller.settings),
            voice_router.registry,
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
