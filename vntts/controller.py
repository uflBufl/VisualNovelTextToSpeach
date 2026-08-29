"""Application controller and live-reading orchestration."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from threading import Event, Lock, RLock
from time import monotonic

from vntts.assets import ModelAssetManager
from vntts.auto_advance import DialogueAdvancer
from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.diagnostics import resolve_voice_label
from vntts.dialog import is_empty, speak_dialog
from vntts.dialog_capture import (
    OCRError,
    OCRUncertainError,
    ScreenCaptureError,
    TTSInitializationError,
    analyze_dialog_snapshot,
    capture_live_frame,
    dialog_glyphs_visible,
    fingerprint_dialog_frame,
    get_screenshot_directory,
    read_dialog_safely,
    recognize_live_frame,
    report_runtime_error,
)
from vntts.generated_audio import (
    AudioRouteTrace,
    GeneratedAudioFallbackBackend,
    GeneratedAudioLibrary,
    GeneratedAudioRoute,
    LiveFallbackRoute,
    LiveTTSRoute,
    PlaybackOutcome,
    PlaybackStatus,
    PreparedGeneratedAudio,
    PreparedSourceAudioPassThrough,
    SourceAudioRoute,
)
from vntts.history import DialogueHistory
from vntts.live import (
    AdaptiveSpeechBackpressure,
    AutoAdvanceAttempt,
    IncrementalDialogTracker,
    LiveDialogReader,
    SilentDialogRoute,
)
from vntts.live_sequence import (
    LiveSequencePlan,
    StoryCursor,
    StoryCursorError,
    StoryCursorState,
)
from vntts.live_speaker_corpus import LiveSpeakerCorpus
from vntts.ocr import OCRResult, UncertainFrameRecorder, default_minimum_ocr_confidence
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.playback import PreparedPlayback
from vntts.runtime_config import (
    get_live_configuration,
    get_tts_configuration,
    initialize_voice_registry,
    initialize_voice_router,
)
from vntts.services.tts_engine import AudioPlaybackError, TTSEngine
from vntts.settings import (
    AppSettings,
    is_live_sequence_audio_mode,
    preserve_loaded_runtime_settings,
)
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    MossTTSPreparedSpeech,
    MossTTSVoiceRouterBackend,
    PocketTTSVoiceRouterBackend,
    XTTSVoiceRouterBackend,
)
from vntts.speech_worker import (
    create_chatterbox_worker_backend,
    create_moss_worker_backend,
    create_pocket_worker_backend,
)
from vntts.voices import (
    VoiceChoice,
    default_voice_choice_id,
    find_voice_assignment,
    is_narrator,
    is_unattributed_speaker,
    normalize_character_name,
    pocket_tts_preset_voices,
    synthesis_character,
)
from vntts.window_capture import WindowCaptureTarget


def _is_silent_sequence_text(value):
    return "".join(str(value).split()) in {"...", "…"}


def _unique_silent_sequence_successor(cursor):
    current = cursor.current_event
    visited = set()
    while (
        current is not None
        and current.event_id not in visited
        and current.control in {"automatic", "passive"}
        and len(current.successors) == 1
    ):
        visited.add(current.event_id)
        candidate = cursor.plan.events[current.successors[0]]
        if candidate.kind in {"speech", "silent"}:
            return candidate if candidate.kind == "silent" else None
        if candidate.kind in {"choice", "wait"} or candidate.control == "manual":
            return None
        current = candidate
    return None


@dataclass(frozen=True)
class LiveSequenceStatus:
    mode: str
    state: str
    chapter: str | None = None
    sequence: int | None = None
    event_id: str | None = None
    line_id: str | None = None
    speaker: str | None = None
    text: str | None = None
    reason: str | None = None
    next_event_count: int = 0
    recovery_required: bool = False
    guidance: str = ""
    expected_audio_route: str = "-"
    actual_audio_route: str = "-"
    ocr_activity: str = "-"
    expected_candidate_count: int = 0


def create_dialog_read_scheduler(
    executor,
    voice_router,
    screenshot_directory,
    *,
    live_reader=None,
    error_handler=None,
    capture_target=None,
    speech_handler=None,
    minimum_confidence=default_minimum_ocr_confidence,
    uncertain_frame_recorder=None,
    diagnostic_handler=None,
    voice_resolver=None,
    ocr_language="eng",
    correction_dictionary=None,
):
    active_read = None
    active_read_lock = Lock()

    def schedule_dialog_read():
        nonlocal active_read

        with active_read_lock:
            if live_reader is not None and live_reader.is_running:
                print("Stop live reading before requesting a one-time read")
                return False
            if active_read is not None and not active_read.done():
                print("A dialog read is already in progress")
                return False

            options = {}
            if error_handler is not None:
                options["error_handler"] = error_handler
            if capture_target is not None:
                options["capture_target"] = capture_target
            if speech_handler is not None:
                options["speech_handler"] = speech_handler
            options["minimum_confidence"] = minimum_confidence
            if uncertain_frame_recorder is not None:
                options["uncertain_frame_recorder"] = uncertain_frame_recorder
            if diagnostic_handler is not None:
                options["diagnostic_handler"] = diagnostic_handler
            if voice_resolver is not None:
                options["voice_resolver"] = voice_resolver
            options["ocr_language"] = ocr_language
            if correction_dictionary is not None:
                options["correction_dictionary"] = correction_dictionary
            active_read = executor.submit(
                read_dialog_safely,
                voice_router,
                screenshot_directory,
                **options,
            )
            return True

    return schedule_dialog_read


@dataclass(frozen=True)
class PreparedLiveChunkRoutes:
    dialogue: object
    speaker_announcement: LiveTTSRoute | None = None
    announced_speaker: str | None = None


def read_live_snapshot(
    screenshot_directory,
    voice_registry=None,
    capture_target=None,
    minimum_confidence=default_minimum_ocr_confidence,
    uncertain_handler=None,
    uncertain_frame_recorder=None,
    diagnostic_handler=None,
    voice_resolver=None,
    ocr_language="eng",
    correction_dictionary=None,
):
    image, _, result = analyze_dialog_snapshot(
        screenshot_directory,
        voice_registry,
        capture_target=capture_target,
        minimum_confidence=minimum_confidence,
        diagnostic_handler=diagnostic_handler,
        voice_resolver=voice_resolver,
        ocr_language=ocr_language,
        correction_dictionary=correction_dictionary,
    )
    if result.text and not result.is_confident(minimum_confidence):
        if uncertain_frame_recorder is not None:
            uncertain_frame_recorder.record(image, result, minimum_confidence)
        if uncertain_handler is not None:
            uncertain_handler(result, minimum_confidence)
        return None, ""
    if uncertain_frame_recorder is not None:
        uncertain_frame_recorder.reset()
    return result.character, result.text


def speak_live_chunk(voice_router, chunk, playback_guard=None):
    print(f"{chunk.character} is speaking now (live)")
    print(chunk.text)
    speak_dialog(
        chunk.text,
        lambda value: voice_router.speak(
            chunk.character,
            value,
            **({"playback_guard": playback_guard} if playback_guard else {}),
        ),
    )


def create_live_toggle(live_reader):
    def toggle_live_reading():
        if live_reader.toggle():
            print("Live reading started")
        else:
            print("Live reading stopping")

    return toggle_live_reading


class AppController:
    def __init__(
        self,
        settings=None,
        *,
        tts_factory=TTSEngine,
        status_handler=print,
        dialog_handler=None,
        diagnostic_handler=None,
        sequence_status_handler=None,
        unknown_speaker_handler=None,
        error_handler=report_runtime_error,
        capture_target_factory=WindowCaptureTarget,
        model_asset_manager_factory=ModelAssetManager,
        chatterbox_backend_factory=create_chatterbox_worker_backend,
        moss_backend_factory=create_moss_worker_backend,
        pocket_backend_factory=create_pocket_worker_backend,
        speech_backpressure_factory=AdaptiveSpeechBackpressure,
        correction_store=None,
        history=None,
        chapter_voice_preloader=None,
        generated_audio_library_factory=GeneratedAudioLibrary.load_optional,
        generated_audio_backend_factory=GeneratedAudioFallbackBackend,
        route_trace_handler=None,
        pipeline_event_handler=None,
        live_sequence_plan_factory=LiveSequencePlan.load,
    ):
        self.settings = settings or AppSettings()
        self.capture_target_factory = capture_target_factory
        self.model_assets = model_asset_manager_factory()
        self.chatterbox_backend_factory = chatterbox_backend_factory
        self.moss_backend_factory = moss_backend_factory
        self.pocket_backend_factory = pocket_backend_factory
        self.speech_backpressure_factory = speech_backpressure_factory
        self.correction_store = correction_store or OCRCorrectionStore.load()
        self.correction_dictionary = self.correction_store.dictionary_for(
            self.settings.active_profile_id
        )
        self.history = history or DialogueHistory()
        self.chapter_voice_preloader = (
            chapter_voice_preloader
            or ChapterVoicePreloader.load_optional(self.settings.story_index)
        )
        self.live_speaker_corpus = None
        self.live_speaker_corpus_error = None
        self._load_live_speaker_corpus()
        self.generated_audio_library_factory = generated_audio_library_factory
        self.generated_audio_backend_factory = generated_audio_backend_factory
        self.route_trace_handler = route_trace_handler or (lambda _trace: None)
        self.pipeline_event_handler = pipeline_event_handler or (
            lambda _stage, _generation, _occurred_at, **_details: None
        )
        self.live_sequence_plan_factory = live_sequence_plan_factory
        self.tts_factory = tts_factory
        self.status_handler = status_handler
        self.dialog_handler = dialog_handler or status_handler
        self.diagnostic_handler = diagnostic_handler or (lambda _snapshot: None)
        self.sequence_status_handler = sequence_status_handler or (
            lambda _snapshot: None
        )
        self.unknown_speaker_handler = unknown_speaker_handler or (lambda _name: None)
        self.error_handler = error_handler
        self.story_cursor_lock = RLock()
        self.live_sequence_plan = None
        self.story_cursor = None
        self.explicit_sequence_anchor_pending = False
        self._load_live_sequence_plan()
        self.capture_target = self._create_capture_target()
        self.uncertain_frame_recorder = self._create_uncertain_frame_recorder()
        self.tts = None
        self.voice_router = None
        self.speech_backend = None
        self.capture_executor = None
        self.ocr_executor = None
        self.speech_executor = None
        self.playback_executor = None
        self.live_reader = None
        self.live_speech_backpressure = self.speech_backpressure_factory()
        self.schedule_dialog_read = None
        self.last_diagnostic = None
        self.last_audio_source_description = "Not selected"
        self.last_audio_route_trace = None
        self.capture_interval_ms = self.settings.live_interval_ms
        self.game_focused = True
        self.diagnostic_lock = Lock()
        self.voice_prime_lock = Lock()
        self.speaker_announcement_lock = Lock()
        self.last_visible_speaker_key = None
        self.primed_voice_keys = set()
        self.reported_unknown_speakers = set()
        self.pending_unknown_speakers = set()
        self.narrator_fallback_speakers = set()
        self.narrator_fallback_names = {}
        self.next_live_narrator_fallback_names = {}
        self.voice_prime_futures = set()
        self.shutdown_requested = Event()

    @property
    def is_ready(self):
        return self.live_reader is not None

    @property
    def is_live_running(self):
        return self.live_reader is not None and self.live_reader.is_running

    def start(self):
        if self.is_ready:
            return True

        self.shutdown_requested.clear()
        use_xtts = self.settings.speech_backend == "coqui-xtts"
        loading_status = {
            "coqui-xtts": "Loading TTS model...",
            "chatterbox-nano": "Loading Chatterbox Nano...",
            "moss-tts": "Loading MOSS-TTS...",
            "pocket-tts": "Loading Pocket TTS...",
        }[self.settings.speech_backend]
        self.status_handler(loading_status)
        try:
            if use_xtts:
                self.model_assets.configure_environment()
                if self.settings.xtts_terms_accepted:
                    os.environ["COQUI_TOS_AGREED"] = "1"
                self.tts = self.tts_factory(**get_tts_configuration(self.settings))
            else:
                if self.settings.speech_backend in {
                    "chatterbox-nano",
                    "moss-tts",
                }:
                    self.model_assets.configure_huggingface_environment()
                registry = initialize_voice_registry(
                    self.settings,
                    self.error_handler,
                )
                if registry is None:
                    return False
                backend_factory = {
                    "chatterbox-nano": self.chatterbox_backend_factory,
                    "moss-tts": self.moss_backend_factory,
                    "pocket-tts": self.pocket_backend_factory,
                }[self.settings.speech_backend]
                narrator_reference = self.settings.tts_speaker_wav
                narrator_source_id = find_voice_assignment(
                    self.settings.voice_assignments,
                    "Narrator",
                )
                if narrator_source_id is not None:
                    narrator_voice = registry.resolve_source(narrator_source_id)
                    if narrator_voice is None:
                        narrator_reference = self.settings.tts_speaker_wav
                    elif narrator_voice.references:
                        narrator_reference = narrator_voice.references[0]
                    elif self.settings.speech_backend == "pocket-tts":
                        narrator_reference = narrator_voice.speaker
                backend_options = {
                    "narrator_reference": narrator_reference,
                    "volume": self.settings.output_volume_percent / 100,
                }
                if (
                    getattr(backend_factory, "supports_startup_cancellation", False)
                    is True
                ):
                    backend_options["startup_cancellation"] = self.shutdown_requested
                if self.settings.speech_backend == "moss-tts":
                    backend_options.update(
                        model_name=self.settings.tts_model,
                        language=self.settings.tts_language or "English",
                        generation_profile=self.settings.tts_profile,
                    )
                self.tts = backend_factory(
                    registry,
                    **backend_options,
                )
        except Exception as error:
            self.error_handler(TTSInitializationError(str(error)))
            return False

        if self.shutdown_requested.is_set():
            self._stop_tts()
            return False

        try:
            if use_xtts:
                self.voice_router = initialize_voice_router(
                    self.tts,
                    self.settings,
                    self.error_handler,
                )
                if self.voice_router is None:
                    self._stop_tts()
                    return False
                self.speech_backend = XTTSVoiceRouterBackend(self.voice_router)
            else:
                self.voice_router = self.tts
                self.speech_backend = self.tts

            self._configure_generated_audio_backend()

            if self.settings.warm_up_voices:
                self.status_handler("Warming speech model and voices...")
                try:
                    warmed = self.voice_router.warm_up(progress=self._warmup_progress)
                except Exception as error:
                    self.error_handler(error)
                    self.status_handler(
                        "Voice warm-up was incomplete; voices will load on demand"
                    )
                else:
                    self.status_handler(f"Speech model and {warmed} voices ready")

            self.capture_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="dialog-capture",
            )
            self.ocr_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="dialog-ocr",
            )
            self.speech_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="dialog-synthesis",
            )
            self.playback_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="dialog-playback",
            )
            backend_capabilities = getattr(self.speech_backend, "capabilities", None)
            can_prepare_during_playback = bool(
                getattr(
                    backend_capabilities,
                    "concurrent_prepare_and_play",
                    True,
                )
            )
            max_speech_jobs = 2 if can_prepare_during_playback else 1
            self.live_speech_backpressure = self.speech_backpressure_factory(
                normal_jobs=max_speech_jobs,
            )
            screenshot_directory = get_screenshot_directory(self.settings)
            self.live_reader = LiveDialogReader(
                capture_executor=self.capture_executor,
                ocr_executor=self.ocr_executor,
                speech_executor=self.speech_executor,
                playback_executor=self.playback_executor,
                read_snapshot=lambda: read_live_snapshot(
                    screenshot_directory,
                    self.voice_router.registry,
                    self.capture_target,
                    self.settings.ocr_minimum_confidence,
                    self._ocr_uncertain,
                    self.uncertain_frame_recorder,
                    self._publish_diagnostic,
                    self._resolve_voice_label,
                    self.settings.ocr_language,
                    self.correction_dictionary,
                ),
                capture_frame=self._capture_live_frame,
                recognize_frame=self._recognize_live_frame,
                frame_fingerprint=fingerprint_dialog_frame,
                frame_presence=dialog_glyphs_visible,
                stable_frame_route=self._stable_live_frame_route,
                stable_frame_owner=self._stable_live_frame_owner,
                line_id_resolver=self._live_sequence_line_id,
                speak_chunk=self._speak_live_chunk,
                prepare_chunk=self._prepare_live_chunk,
                play_prepared=self._play_live_chunk,
                report_error=self.error_handler,
                interrupt_speech=self._interrupt_speech,
                dialog_observed=self._dialog_observed,
                focus_probe=self._is_game_focused,
                capture_state_changed=self._capture_state_changed,
                tracker_factory=IncrementalDialogTracker,
                auto_advance=self._live_auto_advance_callback(),
                require_visible_auto_advance=(
                    self.settings.live_sequence_mode == "audio-auto"
                ),
                auto_advance_delay_seconds=(self.settings.auto_advance_delay_ms / 1000),
                auto_advance_state_changed=self._auto_advance_state_changed,
                pipeline_event_handler=self.pipeline_event_handler,
                max_speech_jobs=max_speech_jobs,
                interrupt_on_dialog_replacement=bool(
                    getattr(
                        backend_capabilities,
                        "interrupt_on_dialog_replacement",
                        False,
                    )
                ),
                first_pcm_on_prepare=False,
                **self._get_live_configuration(),
            )
            self.schedule_dialog_read = create_dialog_read_scheduler(
                self.capture_executor,
                self.voice_router,
                screenshot_directory,
                live_reader=self.live_reader,
                error_handler=self.error_handler,
                capture_target=self.capture_target,
                speech_handler=self._enqueue_dialog,
                minimum_confidence=self.settings.ocr_minimum_confidence,
                uncertain_frame_recorder=self.uncertain_frame_recorder,
                diagnostic_handler=self._publish_diagnostic,
                voice_resolver=self._resolve_voice_label,
                ocr_language=self.settings.ocr_language,
                correction_dictionary=self.correction_dictionary,
            )
        except Exception as error:
            self.error_handler(error)
            self.shutdown()
            return False

        if not self.settings.warm_up_voices:
            self.status_handler("Speech model loaded; voice warm-up skipped")
        self.status_handler(f"Screenshots will be stored in {screenshot_directory}")
        return True

    def read_once(self):
        if not self.is_ready:
            return False
        self.live_reader.resume_after_emergency()
        accepted = self.schedule_dialog_read()
        if accepted:
            self.status_handler("Reading current dialog")
        return accepted

    def identify_live_scope(self):
        """Identify the visible story position without speaking or advancing it."""
        if not self.is_ready or self.is_live_running:
            return False
        character, text = read_live_snapshot(
            get_screenshot_directory(self.settings),
            self.voice_router.registry,
            self.capture_target,
            self.settings.ocr_minimum_confidence,
            self._ocr_uncertain,
            self.uncertain_frame_recorder,
            self._publish_diagnostic,
            self._resolve_voice_label,
            self.settings.ocr_language,
            self.correction_dictionary,
        )
        if is_empty(text):
            return False
        character = self._canonical_observed_character(character, text)
        if self.chapter_voice_preloader.resolve_exact(character, text) is None:
            return False
        self.dialog_handler(character, text)
        return True

    def toggle_live(self):
        if not self.is_ready:
            return False
        starting = not self.live_reader.is_running
        if starting and not self._live_voice_preflight_allows_start():
            return False
        if starting and self.capture_target is not None:
            try:
                self.capture_target.get_geometry()
            except Exception as error:
                self.error_handler(ScreenCaptureError(str(error)))
                self.status_handler("Live reading could not start")
                return False
        if starting:
            # Unknown-speaker prompts are deduplicated within one live session,
            # not for the entire application lifetime. A speaker dismissed
            # during a one-time read must still be reported when live reading
            # begins.
            self.reported_unknown_speakers.clear()
            self.pending_unknown_speakers.clear()
            self.narrator_fallback_speakers.clear()
            self.narrator_fallback_names.clear()
            self.narrator_fallback_speakers.update(
                self.next_live_narrator_fallback_names
            )
            self.narrator_fallback_names.update(self.next_live_narrator_fallback_names)
            with self.speaker_announcement_lock:
                self.last_visible_speaker_key = None
            with self.story_cursor_lock:
                if self.story_cursor is not None:
                    if self.explicit_sequence_anchor_pending:
                        self.explicit_sequence_anchor_pending = False
                    else:
                        self.story_cursor.reset("live-session-started")
                    self._publish_live_sequence_status()
        running = self.live_reader.toggle()
        if running:
            self.next_live_narrator_fallback_names.clear()
            self.live_reader.max_speech_jobs = self.live_speech_backpressure.reset()
        elif not starting:
            self.narrator_fallback_speakers.clear()
            self.narrator_fallback_names.clear()
        self._set_backend_live_mode(running)
        self.status_handler(
            "Live reading started" if running else "Live reading stopping"
        )
        return running

    def _live_voice_preflight_allows_start(self):
        if (
            not self.chapter_voice_preloader.dialogue
            and not self._revalidate_live_speaker_corpus()
        ):
            self.status_handler(
                "Live reading could not start: configured speaker corpus is "
                f"invalid: {self.live_speaker_corpus_error}"
            )
            return False
        unresolved = self.unresolved_live_speakers()
        if unresolved is None:
            if self.live_speaker_corpus_error:
                self.status_handler(
                    "Live reading could not start: configured speaker corpus is "
                    f"invalid: {self.live_speaker_corpus_error}"
                )
            else:
                self.status_handler(
                    "Live reading could not start: read the current dialog once to "
                    "identify the story chapter"
                )
            return False
        unresolved = tuple(unresolved)
        approved = tuple(self.next_live_narrator_fallback_names.values())
        if not unresolved:
            if approved:
                self.next_live_narrator_fallback_names.clear()
                self.status_handler(
                    "Live reading could not start: voice preflight scope changed; "
                    "start live reading again"
                )
                return False
            self.next_live_narrator_fallback_names.clear()
            return True
        if approved == unresolved:
            return True
        self.next_live_narrator_fallback_names.clear()
        self.status_handler(
            "Live reading could not start: choose voices or explicitly approve "
            f"Narrator for {', '.join(unresolved)}"
        )
        return False

    def toggle_speech_pause(self):
        if not self.is_ready:
            return False
        paused = self.live_reader.toggle_pause()
        self.status_handler("Speech paused" if paused else "Speech resumed")
        return paused

    def skip_current_speech(self):
        if not self.is_ready:
            return False
        skipped = self.live_reader.skip_current()
        self.status_handler(
            "Skipped current speech" if skipped else "Nothing is currently speaking"
        )
        return skipped

    def repeat_last_speech(self):
        if not self.is_ready:
            return False
        repeated = self.live_reader.repeat_last()
        self.status_handler(
            "Repeating last speech" if repeated else "No previous speech to repeat"
        )
        return repeated

    def clear_speech_queue(self):
        if not self.is_ready:
            return False
        cleared = self.live_reader.clear_queue()
        self.status_handler("Speech queue cleared")
        return cleared

    def emergency_stop(self):
        if not self.is_ready:
            return False
        stopped = self.live_reader.emergency_stop()
        self._set_backend_live_mode(False)
        self.status_handler("Emergency stop: live reading and speech stopped")
        return stopped

    def set_auto_advance_enabled(self, enabled):
        self.settings = self.settings.updated(auto_advance_enabled=bool(enabled))
        if isinstance(self.speech_backend, GeneratedAudioFallbackBackend):
            self.speech_backend.require_source_audio_completion = bool(
                enabled and self.settings.live_sequence_mode != "audio-manual"
            )
        if self.live_reader is not None:
            self.live_reader.set_auto_advance(self._live_auto_advance_callback())
        self.status_handler(
            "Auto advance saved but suppressed by sequence-first manual mode"
            if enabled and self.settings.live_sequence_mode == "audio-manual"
            else "Auto advance enabled"
            if enabled
            else "Auto advance disabled"
        )
        return bool(enabled)

    def available_voice_characters(self):
        if self.voice_router is None:
            return ["Narrator"]
        voices = {
            id(voice): voice for voice in self.voice_router.registry.voices.values()
        }
        return [
            "Narrator",
            *(
                voice.character
                for voice in sorted(
                    voices.values(), key=lambda item: item.character.casefold()
                )
            ),
        ]

    def available_voice_choices(self):
        if self.voice_router is None:
            return []
        choices = [
            VoiceChoice(
                default_voice_choice_id,
                "Backend default live voice",
                "Use the speech backend's default live voice",
            )
        ]
        if self.settings.speech_backend == "pocket-tts":
            choices.extend(
                VoiceChoice(
                    f"preset:{name}",
                    name.replace("_", " ").title(),
                    "Pocket TTS built-in voice",
                )
                for name in pocket_tts_preset_voices
            )
        elif self.settings.speech_backend == "coqui-xtts":
            speakers = getattr(getattr(self.tts, "tts", None), "speakers", None)
            choices.extend(
                VoiceChoice(
                    f"preset:{speaker}",
                    str(speaker),
                    "XTTS model speaker",
                )
                for speaker in (speakers or ())
            )
        choices.extend(self.voice_router.registry.choices())
        seen = set()
        return [
            choice
            for choice in choices
            if not (choice.id in seen or seen.add(choice.id))
        ]

    def voice_assignment_for(self, character):
        configured = find_voice_assignment(
            self.settings.voice_assignments,
            character,
        )
        if configured is not None:
            return configured
        voice = self.voice_router.registry.resolve(character)
        if voice is None:
            return default_voice_choice_id
        return f"character:{normalize_character_name(voice.character)}"

    def preview_voice_choice(self, source_id, text):
        if not self.is_ready:
            raise RuntimeError("The speech engine is not ready")
        if self.is_live_running:
            raise RuntimeError("Stop live reading before previewing a voice")
        if not text or not text.strip():
            raise ValueError("Enter preview text")
        choice = next(
            (item for item in self.available_voice_choices() if item.id == source_id),
            None,
        )
        if choice is None:
            raise ValueError("The selected voice is no longer available")
        self.status_handler(f"Previewing {choice.label} voice")
        return self.speech_executor.submit(
            self._preview_voice_choice,
            choice,
            text.strip(),
        )

    def stop_voice_preview(self):
        if self.is_live_running:
            raise RuntimeError("Stop live reading before stopping a voice preview")
        backend = self.speech_backend
        if isinstance(backend, GeneratedAudioFallbackBackend):
            backend = backend.live_backend
        stop = getattr(backend, "stop", None)
        if callable(stop):
            stop()
            return True
        return False

    def assign_voice(self, character, source_id):
        character = (character or "").strip()
        if not character:
            raise ValueError("Enter a narrator or character name")
        if self.is_live_running:
            raise RuntimeError("Stop live reading before changing a voice")
        choice = next(
            (item for item in self.available_voice_choices() if item.id == source_id),
            None,
        )
        if choice is None:
            raise ValueError("The selected voice is no longer available")

        assignments = {
            configured_character: configured_source
            for configured_character, configured_source in (
                self.settings.voice_assignments or {}
            ).items()
            if normalize_character_name(configured_character)
            != normalize_character_name(character)
        }
        assignments[character] = source_id
        self.voice_router.registry.set_assignment(character, source_id)
        self.settings = self.settings.updated(voice_assignments=assignments)
        if normalize_character_name(character) == "narrator":
            self._apply_narrator_voice(
                self.voice_router.registry.resolve_source(source_id)
            )
        self._clear_voice_runtime_cache()
        character_key = normalize_character_name(character)
        self.reported_unknown_speakers.discard(character_key)
        self.pending_unknown_speakers.discard(character_key)
        self.narrator_fallback_speakers.discard(character_key)
        self.status_handler(f"{choice.label} assigned to {character}")
        return self.settings

    def clear_voice_assignment(self, character):
        character = (character or "").strip()
        if not character:
            raise ValueError("Enter a narrator or character name")
        if self.is_live_running:
            raise RuntimeError("Stop live reading before changing a voice")
        character_key = normalize_character_name(character)
        assignments = {
            configured_character: configured_source
            for configured_character, configured_source in (
                self.settings.voice_assignments or {}
            ).items()
            if normalize_character_name(configured_character) != character_key
        }
        self.voice_router.registry.assignments.pop(character_key, None)
        update = {"voice_assignments": assignments}
        if character_key == "narrator":
            update["force_live_narrator"] = False
        self.settings = self.settings.updated(**update)
        if character_key == "narrator":
            self._apply_narrator_voice(None)
        self._clear_voice_runtime_cache()
        self.status_handler(
            "Pregenerated narrator tracks enabled when available"
            if character_key == "narrator"
            else f"Automatic voice routing restored for {character}"
        )
        return self.settings

    def set_force_live_narrator(self, enabled):
        if self.is_live_running:
            raise RuntimeError("Stop live reading before changing Narrator routing")
        enabled = bool(enabled)
        if enabled and self.voice_assignment_for("Narrator") is None:
            raise ValueError("Choose a Narrator voice before forcing live TTS")
        self.settings = self.settings.updated(force_live_narrator=enabled)
        self.status_handler(
            "Narrator will always use live TTS"
            if enabled
            else "Pregenerated Narrator tracks enabled with live voice fallback"
        )
        return self.settings

    def allow_narrator_fallback(self, character):
        character = (character or "").strip()
        key = normalize_character_name(character)
        if not key or key == "narrator":
            return False
        self.pending_unknown_speakers.discard(key)
        self.narrator_fallback_speakers.add(key)
        self.narrator_fallback_names[key] = character
        self.status_handler(f"Using narrator voice for {character}")
        return True

    def unresolved_live_speakers(self):
        """Return scoped named speakers, or ``None`` until the chapter is known."""
        scope = self.chapter_voice_preloader.live_voice_preflight_rows()
        if not self.chapter_voice_preloader.dialogue:
            if self.live_speaker_corpus_error:
                return None
            if self.live_speaker_corpus is not None:
                scope = self.live_speaker_corpus.speakers
        if scope is None:
            return None
        unresolved = []
        seen = set()
        for line in scope:
            character = str(getattr(line, "speaker", line) or "").strip()
            text = getattr(line, "text", None)
            key = normalize_character_name(character)
            if key in seen or not self._speaker_requires_voice_decision(
                character,
                text,
                live_preflight=True,
            ):
                continue
            seen.add(key)
            unresolved.append(character)
        return tuple(unresolved)

    def _load_live_speaker_corpus(self):
        self.live_speaker_corpus = None
        self.live_speaker_corpus_error = None
        if not self.settings.live_speaker_corpus:
            return
        try:
            self.live_speaker_corpus = LiveSpeakerCorpus.load(
                self.settings.live_speaker_corpus
            )
        except (OSError, TypeError, ValueError) as error:
            self.live_speaker_corpus_error = str(error)

    def _revalidate_live_speaker_corpus(self):
        if not self.settings.live_speaker_corpus:
            return True
        if self.live_speaker_corpus is None:
            return False
        try:
            self.live_speaker_corpus.revalidate()
        except (OSError, TypeError, ValueError) as error:
            self.live_speaker_corpus_error = str(error)
            return False
        self.live_speaker_corpus_error = None
        return True

    def approve_live_narrator_fallbacks(self, characters):
        """Stage explicit narrator choices for the next live session only."""
        if self.is_live_running:
            raise RuntimeError("Stop live reading before approving narrator fallbacks")
        approved = {}
        for character in characters:
            name = str(character or "").strip()
            key = normalize_character_name(name)
            if not key or is_narrator(name):
                continue
            approved[key] = name
        self.next_live_narrator_fallback_names = approved
        return tuple(approved.values())

    def preview_voice(self, character, text):
        if not self.is_ready:
            raise RuntimeError("The speech engine is not ready")
        if self.is_live_running:
            raise RuntimeError("Stop live reading before previewing a voice")
        if not text or not text.strip():
            raise ValueError("Enter preview text")
        self.status_handler(f"Previewing {character or 'Narrator'} voice")
        return self.speech_executor.submit(
            self._preview_voice,
            character or "Narrator",
            text.strip(),
        )

    def replay_dialog(self, character, text):
        return self.preview_voice(character, text)

    def get_capture_geometry(self):
        if self.capture_target is None:
            return None
        return self.capture_target.get_geometry()

    def get_latest_diagnostic(self):
        with self.diagnostic_lock:
            return self.last_diagnostic

    def get_live_pipeline_metrics(self):
        if self.live_reader is None:
            return None
        return self.live_reader.get_pipeline_metrics()

    def inspect_current_dialog(self):
        registry = self.voice_router.registry if self.voice_router is not None else None
        _image, _output, result = analyze_dialog_snapshot(
            get_screenshot_directory(self.settings),
            registry,
            capture_target=self.capture_target,
            minimum_confidence=self.settings.ocr_minimum_confidence,
            diagnostic_handler=self._publish_diagnostic,
            voice_resolver=self._resolve_voice_label,
            ocr_language=self.settings.ocr_language,
            correction_dictionary=self.correction_dictionary,
        )
        return result

    def test_current_dialog(self):
        if not self.is_ready:
            raise RuntimeError("The speech engine is not ready")
        image, _output, result = analyze_dialog_snapshot(
            get_screenshot_directory(self.settings),
            self.voice_router.registry,
            capture_target=self.capture_target,
            minimum_confidence=self.settings.ocr_minimum_confidence,
            diagnostic_handler=self._publish_diagnostic,
            voice_resolver=self._resolve_voice_label,
            ocr_language=self.settings.ocr_language,
            correction_dictionary=self.correction_dictionary,
        )
        if result.text and not result.is_confident(
            self.settings.ocr_minimum_confidence
        ):
            error = OCRUncertainError(
                result,
                self.settings.ocr_minimum_confidence,
            )
            if self.uncertain_frame_recorder is not None:
                self.uncertain_frame_recorder.record(
                    image,
                    error.result,
                    self.settings.ocr_minimum_confidence,
                )
            raise error
        character, text = result.character, result.text
        if self.uncertain_frame_recorder is not None:
            self.uncertain_frame_recorder.reset()
        if is_empty(text):
            raise OCRError("No dialogue text was detected in the calibrated region")
        self.status_handler(f"Testing OCR and speech with {character}")
        try:
            speak_dialog(
                text,
                lambda value: self._speak_with_live_backend(character, value),
            )
        finally:
            self._refresh_diagnostic_metrics()
        return character, text

    def apply_settings(self, settings):
        if self.tts is not None or self.speech_backend is not None:
            settings = preserve_loaded_runtime_settings(self.settings, settings)
        was_live = self.is_live_running
        if was_live:
            self._set_backend_live_mode(False)
            self.live_reader.stop()
            self.live_reader.wait()

        self.settings = settings
        with self.speaker_announcement_lock:
            self.last_visible_speaker_key = None
        self.chapter_voice_preloader = ChapterVoicePreloader.load_optional(
            self.settings.story_index
        )
        self._load_live_sequence_plan()
        self._load_live_speaker_corpus()
        self._configure_generated_audio_backend()
        self.refresh_corrections()
        self.capture_target = self._create_capture_target()
        self.uncertain_frame_recorder = self._create_uncertain_frame_recorder()
        if self.tts is not None:
            self.tts.set_volume(self.settings.output_volume_percent / 100)
            self.tts.set_speed(self.settings.speech_rate_percent / 100)
        if self.speech_backend is not None:
            set_volume = getattr(self.speech_backend, "set_volume", None)
            set_speed = getattr(self.speech_backend, "set_speed", None)
            set_generation_profile = getattr(
                self.speech_backend,
                "set_generation_profile",
                None,
            )
            if callable(set_volume):
                set_volume(self.settings.output_volume_percent / 100)
            if callable(set_speed):
                set_speed(self.settings.speech_rate_percent / 100)
            if callable(set_generation_profile):
                set_generation_profile(self.settings.tts_profile)
        if self.live_reader is None:
            return

        screenshot_directory = get_screenshot_directory(self.settings)
        self.live_reader.read_snapshot = lambda: read_live_snapshot(
            screenshot_directory,
            self.voice_router.registry,
            self.capture_target,
            self.settings.ocr_minimum_confidence,
            self._ocr_uncertain,
            self.uncertain_frame_recorder,
            self._publish_diagnostic,
            self._resolve_voice_label,
            self.settings.ocr_language,
            self.correction_dictionary,
        )
        live_configuration = self._get_live_configuration()
        self.live_reader.interval_seconds = live_configuration["interval_seconds"]
        self.live_reader.tracker_options = live_configuration["tracker_options"]
        self.live_reader.require_visible_auto_advance = (
            self.settings.live_sequence_mode == "audio-auto"
        )
        self.live_reader.set_auto_advance(self._live_auto_advance_callback())
        self.live_reader.auto_advance_delay_seconds = (
            self.settings.auto_advance_delay_ms / 1000
        )
        self.schedule_dialog_read = create_dialog_read_scheduler(
            self.capture_executor,
            self.voice_router,
            screenshot_directory,
            live_reader=self.live_reader,
            error_handler=self.error_handler,
            capture_target=self.capture_target,
            speech_handler=self._enqueue_dialog,
            minimum_confidence=self.settings.ocr_minimum_confidence,
            uncertain_frame_recorder=self.uncertain_frame_recorder,
            diagnostic_handler=self._publish_diagnostic,
            voice_resolver=self._resolve_voice_label,
            ocr_language=self.settings.ocr_language,
            correction_dictionary=self.correction_dictionary,
        )
        if was_live:
            self.toggle_live()

    def _get_live_configuration(self):
        configuration = get_live_configuration(self.settings)
        tracker_options = dict(configuration["tracker_options"])
        tracker_options["complete_dialogue_only"] = bool(
            self.settings.audio_source_policy != "live-tts-only"
            or is_live_sequence_audio_mode(self.settings.live_sequence_mode)
        )
        if tracker_options["complete_dialogue_only"] and self.settings.story_index:
            tracker_options["incomplete_dialogue_probe"] = (
                self.chapter_voice_preloader.is_unique_incomplete_prefix
            )
        if (
            isinstance(self.speech_backend, GeneratedAudioFallbackBackend)
            and self.speech_backend.library is not None
        ):
            tracker_options["early_dialogue_resolver"] = (
                self._resolve_early_indexed_dialogue
            )
        return {**configuration, "tracker_options": tracker_options}

    def _resolve_early_indexed_dialogue(self, character, text):
        backend = self.speech_backend
        if not isinstance(backend, GeneratedAudioFallbackBackend):
            return None
        line = self.chapter_voice_preloader.resolve_unique_prefix(
            character,
            text,
        )
        return line.text if line is not None else None

    def _has_manual_voice_override(self, character):
        if is_unattributed_speaker(character):
            return False
        assignment = find_voice_assignment(self.settings.voice_assignments, character)
        if assignment is None:
            return False
        if is_narrator(character):
            return self.settings.force_live_narrator
        return True

    def _set_backend_live_mode(self, active):
        configure = getattr(self.speech_backend, "set_live_mode_active", None)
        if callable(configure):
            configure(active)

    def _configure_generated_audio_backend(self):
        if self.speech_backend is None:
            return False
        live_backend = (
            self.speech_backend.live_backend
            if isinstance(self.speech_backend, GeneratedAudioFallbackBackend)
            else self.speech_backend
        )
        self.speech_backend = live_backend
        policy = self.settings.audio_source_policy
        if policy == "live-tts-only":
            self.status_handler(f"Audio policy: live TTS only ({live_backend.name})")
            return False
        if not self.settings.story_index:
            self.status_handler(
                "Audio fallback disabled: configure a story index for stable line IDs"
            )
            return False
        library = None
        if self.settings.generated_audio_manifest:
            library = self.generated_audio_library_factory(
                self.settings.generated_audio_manifest,
                warn=self.status_handler,
            )
        if policy == "prefer-generated" and library is None:
            self.status_handler(
                f"Generated audio unavailable; using live TTS ({live_backend.name})"
            )
            return False
        backend_options = {
            "volume": self.settings.output_volume_percent / 100,
            "speed": self.settings.speech_rate_percent / 100,
            "audio_source_policy": policy,
        }
        if policy == "prefer-game-audio":
            backend_options["require_source_audio_completion"] = bool(
                self.settings.auto_advance_enabled
                and self.settings.live_sequence_mode != "audio-manual"
            )
        self.speech_backend = self.generated_audio_backend_factory(
            live_backend,
            library,
            self.chapter_voice_preloader,
            **backend_options,
        )
        self.speech_backend.voice_override = self._has_manual_voice_override
        if policy == "prefer-game-audio":
            suffix = (
                ", then generated/live TTS"
                if library is not None
                else ", then live TTS"
            )
            self.status_handler(f"Audio policy: original game audio{suffix}")
        elif library is not None:
            self.status_handler(
                f"Audio policy: {len(library.index.entries)} generated entries, "
                "then live TTS"
            )
        return True

    def refresh_corrections(self):
        self.correction_store = OCRCorrectionStore.load(self.correction_store.path)
        self.correction_dictionary = self.correction_store.dictionary_for(
            self.settings.active_profile_id
        )

    def _create_capture_target(self):
        if self.settings.capture_mode != "window":
            return None
        return self.capture_target_factory(self.settings.game_window_title)

    def _capture_live_frame(self):
        return capture_live_frame(
            get_screenshot_directory(self.settings),
            self.capture_target,
        )

    def _recognize_live_frame(self, frame):
        character, text = recognize_live_frame(
            frame,
            self.voice_router.registry,
            self.settings.ocr_minimum_confidence,
            self._ocr_uncertain,
            self.uncertain_frame_recorder,
            self._publish_diagnostic,
            self._resolve_voice_label,
            self.settings.ocr_language,
            self.correction_dictionary,
            ellipsis_speaker_resolver=self.chapter_voice_preloader,
        )
        return self._canonical_observed_character(character, text), text

    def _preview_voice(self, character, text):
        try:
            self._speak_with_live_backend(character, text)
        finally:
            self._refresh_diagnostic_metrics()
        return character, text

    def _preview_voice_choice(self, choice, text):
        registry = self.voice_router.registry
        preview_character = "VNTTS voice preview"
        preview_key = normalize_character_name(preview_character)
        had_assignment = preview_key in registry.assignments
        previous = registry.assignments.get(preview_key)
        registry.set_assignment(preview_character, choice.id)
        self._clear_voice_runtime_cache()
        try:
            self._speak_with_live_backend(preview_character, text)
        finally:
            if had_assignment:
                registry.assignments[preview_key] = previous
            else:
                registry.assignments.pop(preview_key, None)
            self._clear_voice_runtime_cache()
            self._refresh_diagnostic_metrics()
        return choice.label, text

    def _speak_with_live_backend(self, character, text):
        backend = self.speech_backend
        if isinstance(backend, GeneratedAudioFallbackBackend):
            backend = backend.live_backend
        speak = getattr(backend, "speak", None)
        if callable(speak):
            return speak(character, text)
        return self.voice_router.speak(character, text)

    def _apply_narrator_voice(self, voice):
        set_narrator_voice = getattr(self.voice_router, "set_narrator_voice", None)
        if callable(set_narrator_voice):
            set_narrator_voice(voice, self.settings.tts_speaker_wav)
        elif isinstance(self.voice_router, PocketTTSVoiceRouterBackend):
            self.voice_router.narrator_reference = (
                voice.references[0]
                if voice is not None and voice.references
                else voice.speaker
                if voice is not None
                else self.settings.tts_speaker_wav or "alba"
            )
            self.voice_router.voice_states.pop("narrator", None)
        elif isinstance(self.voice_router, MossTTSVoiceRouterBackend):
            self.voice_router.narrator_reference = (
                voice.references[0]
                if voice is not None and voice.references
                else self.settings.tts_speaker_wav
            )
            self.voice_router.prompt_audio_codes.pop("narrator", None)
        elif isinstance(self.voice_router, ChatterboxNanoVoiceRouterBackend):
            self.voice_router.narrator_reference = (
                voice.references[0]
                if voice is not None and voice.references
                else self.settings.tts_speaker_wav
            )
            self.voice_router.conditionals.pop("narrator", None)
        else:
            self.voice_router.narrator_voice = voice

    def _clear_voice_runtime_cache(self):
        clear_runtime_cache = getattr(self.voice_router, "clear_runtime_cache", None)
        if callable(clear_runtime_cache):
            clear_runtime_cache()
            return
        cache = getattr(self.voice_router, "audio_cache", None)
        clear = getattr(cache, "clear", None)
        if callable(clear):
            clear()

    def _warmup_progress(self, current, total, character):
        self.status_handler(f"Warming voice {current}/{total}: {character}")

    def _create_uncertain_frame_recorder(self):
        if not self.settings.retain_uncertain_frames:
            return None
        return UncertainFrameRecorder(self.settings.ocr_diagnostics_directory)

    def _dialog_observed(self, character, text):
        if not text:
            self.history.finish_current()
            self.dialog_handler("Narrator", "")
            return True
        character = self._canonical_observed_character(character, text)
        canonical_routing = False
        with self.story_cursor_lock:
            sequence_observation = self._observe_live_sequence(character, text)
            if is_live_sequence_audio_mode(self.settings.live_sequence_mode):
                if self.story_cursor is None:
                    return False
                if sequence_observation is None:
                    if self.story_cursor.state in {
                        StoryCursorState.UNSYNCHRONIZED,
                        StoryCursorState.ANCHORING,
                    }:
                        # Typewriter prefixes may not yet identify one canonical
                        # line. Consume the changing frame without ever routing
                        # its unbound OCR text to speech; a later exact frame can
                        # still establish the anchor.
                        return (None, "")
                    if self.story_cursor.state in {
                        StoryCursorState.LOCKED,
                        StoryCursorState.MANUAL,
                        StoryCursorState.DESYNCHRONIZED,
                    }:
                        return False
                else:
                    snapshot, line, _match_result = sequence_observation
                    if snapshot.state == StoryCursorState.DESYNCHRONIZED:
                        return False
                    event = self.live_sequence_plan.events.get(
                        snapshot.current_event_id
                    )
                    if event is not None and event.kind == "silent" and line is None:
                        return SilentDialogRoute(event.event_id)
                    if (
                        event is None
                        or not event.is_speech
                        or line is None
                        or event.line_id != line.line_id
                    ):
                        return False
                    character, text = line.speaker, line.text
                    canonical_routing = True
        speech_deferred = self._offer_unknown_speaker_mapping(character, text)
        self._prime_observed_voice(character)
        self._prime_likely_chapter_voice(character, text)
        self.history.add(character, text)
        preview = text if len(text) <= 100 else f"{text[:97]}..."
        self.dialog_handler(character or "Narrator", preview)
        if speech_deferred:
            return False
        return (character, text) if canonical_routing else True

    def _load_live_sequence_plan(self):
        with self.story_cursor_lock:
            return self._load_live_sequence_plan_locked()

    def _load_live_sequence_plan_locked(self):
        self.live_sequence_plan = None
        self.story_cursor = None
        self.explicit_sequence_anchor_pending = False
        if self.settings.live_sequence_mode == "off":
            self._publish_live_sequence_status()
            return False
        if not self.settings.live_sequence_plan:
            self.status_handler(
                "Sequence-first rollout disabled: configure a live sequence plan"
            )
            self._publish_live_sequence_status()
            return False
        if not self.settings.story_index:
            self.status_handler(
                "Sequence-first rollout disabled: configure its story index"
            )
            self._publish_live_sequence_status()
            return False
        try:
            plan = self.live_sequence_plan_factory(
                self.settings.live_sequence_plan,
                self.settings.story_index,
            )
            cursor = StoryCursor(plan)
        except Exception as error:
            self.status_handler(f"Sequence-first rollout disabled: {error}")
            self._publish_live_sequence_status()
            return False
        self.live_sequence_plan = plan
        self.story_cursor = cursor
        self.status_handler(
            f"Sequence-first {self.settings.live_sequence_mode} ready: "
            f"{len(plan.events)} planned events"
        )
        self._publish_live_sequence_status()
        return True

    def get_live_sequence_status(self):
        with self.story_cursor_lock:
            return self._get_live_sequence_status_locked()

    def _get_live_sequence_status_locked(self):
        cursor = self.story_cursor
        mode = self.settings.live_sequence_mode
        if cursor is None:
            return LiveSequenceStatus(
                mode,
                "off" if mode == "off" else "unavailable",
                guidance=(
                    "Sequence-first routing is off."
                    if mode == "off"
                    else "Configure a valid story index and live sequence plan."
                ),
            )
        snapshot = cursor.snapshot()
        event = cursor.current_event
        line = (
            None
            if event is None or event.line_id is None
            else self.chapter_voice_preloader.line_for_id(event.line_id)
        )
        recovery_required = False
        if snapshot.state == StoryCursorState.UNSYNCHRONIZED:
            guidance = (
                "Waiting for one exact OCR anchor. You can set the visible story "
                "position manually."
            )
        elif snapshot.state == StoryCursorState.PLAYING:
            guidance = "Canonical audio is playing; visual transitions are closed."
        elif snapshot.reason == "playback-failed":
            recovery_required = True
            guidance = (
                "Playback failed. Replay or set the visible story position before "
                "continuing."
            )
        elif snapshot.state == StoryCursorState.DESYNCHRONIZED:
            recovery_required = True
            guidance = (
                "The observed line is outside the allowed successor path. Set the "
                "visible story position to resume."
            )
        elif snapshot.state == StoryCursorState.MANUAL:
            recovery_required = True
            guidance = (
                "A choice or manual boundary needs an explicit story-position "
                "selection."
            )
        elif (
            snapshot.state == StoryCursorState.WAITING_TRANSITION
            and cursor.deterministic_manual_successor() is not None
        ):
            recovery_required = True
            guidance = (
                "The advance key was sent and the next planned event is a choice or "
                "manual boundary. Make the in-game decision, then select the visible "
                "expected event; no second key will be sent."
            )
        elif event is not None and not event.successors:
            guidance = "This is a terminal sequence event; no successor is expected."
        elif cursor.can_confirm_visual_transition:
            candidate = cursor.deterministic_visual_successor()
            if candidate is None:
                recovery_required = True
                guidance = (
                    "The next event is not deterministic. Set the visible story "
                    "position after making the in-game choice."
                )
            else:
                guidance = (
                    "Waiting for the next stable dialogue fingerprint; locked routing "
                    "will not run OCR."
                )
        else:
            guidance = "Waiting for canonical playback to complete."
        expected_audio_route = self._expected_sequence_audio_route(event, line)
        trace = getattr(self, "last_audio_route_trace", None)
        actual_audio_route = "-"
        if (
            trace is not None
            and snapshot.current_line_id is not None
            and trace.line_id == snapshot.current_line_id
        ):
            actual_audio_route = trace.effective_source
            if trace.fallback_reason:
                actual_audio_route += f" ({trace.fallback_reason})"
        recognized_frames = 0
        live_reader = getattr(self, "live_reader", None)
        if live_reader is not None:
            recognized_frames = live_reader.get_pipeline_metrics().recognized_frames
        if snapshot.state in {
            StoryCursorState.UNSYNCHRONIZED,
            StoryCursorState.ANCHORING,
        }:
            ocr_activity = (
                f"Full OCR anchoring; {recognized_frames} frame(s) recognized"
            )
        elif snapshot.state in {
            StoryCursorState.DESYNCHRONIZED,
            StoryCursorState.MANUAL,
        }:
            ocr_activity = (
                f"Recovery OCR available; {recognized_frames} frame(s) recognized"
            )
        else:
            ocr_activity = (
                f"Full OCR idle in locked routing; {recognized_frames} anchor/recovery "
                "frame(s) recognized"
            )
        expected_candidate_count = len(self._expected_live_sequence_events())
        return LiveSequenceStatus(
            mode,
            snapshot.state.value,
            chapter=event.chapter if event is not None else None,
            sequence=event.sequence if event is not None else None,
            event_id=snapshot.current_event_id,
            line_id=snapshot.current_line_id,
            speaker=line.speaker if line is not None else None,
            text=line.text if line is not None else None,
            reason=snapshot.reason,
            next_event_count=len(snapshot.expected_successor_ids),
            recovery_required=recovery_required,
            guidance=guidance,
            expected_audio_route=expected_audio_route,
            actual_audio_route=actual_audio_route,
            ocr_activity=ocr_activity,
            expected_candidate_count=expected_candidate_count,
        )

    def _expected_sequence_audio_route(self, event, line):
        if event is None:
            return "Waiting for a canonical event"
        if event.kind == "silent":
            return "No speech (silent event)"
        if line is None:
            return "Unavailable canonical line"
        if self.settings.audio_source_policy == "live-tts-only":
            return "Live TTS"
        if (
            self.settings.audio_source_policy == "prefer-game-audio"
            and line.source_audio_status == "available"
            and not self._has_manual_voice_override(line.speaker)
        ):
            return "Original game audio"
        backend = getattr(self, "speech_backend", None)
        if isinstance(backend, GeneratedAudioFallbackBackend):
            library = backend.library
            if (
                library is not None
                and line.line_id
                and line.text_sha256
                and library.index.find(
                    line.line_id,
                    line.text_sha256,
                    verify_file=False,
                )
                is not None
            ):
                return "Generated audio (manifest declaration)"
        return "Live TTS fallback"

    def _publish_live_sequence_status(self):
        status = self.get_live_sequence_status()
        try:
            self.sequence_status_handler(status)
        except Exception as error:
            self.error_handler(error)
        return status

    def _observe_live_sequence(self, character, text):
        with self.story_cursor_lock:
            return self._observe_live_sequence_locked(character, text)

    def _observe_live_sequence_locked(self, character, text):
        cursor = self.story_cursor
        if cursor is None or self.settings.live_sequence_mode == "off":
            return None
        previous_event_id = cursor.current_event_id
        confirming_dispatch = cursor.state == StoryCursorState.WAITING_TRANSITION
        candidate_events = ()
        if previous_event_id is None or cursor.state in {
            StoryCursorState.UNSYNCHRONIZED,
            StoryCursorState.ANCHORING,
        }:
            resolve = getattr(
                self.chapter_voice_preloader,
                "resolve_exact_with_result",
                None,
            )
            if callable(resolve):
                line, match_result = resolve(character, text)
            else:
                line = self.chapter_voice_preloader.resolve_exact(character, text)
                match_result = "exact" if line is not None else "no-match"
            snapshot = None if line is None else cursor.observe_line(line.line_id)
        else:
            current = cursor.current_event
            candidate_events = cursor.bounded_visible_successors()
            silent_event = cursor.deterministic_visual_successor()
            if silent_event is None and self.settings.live_sequence_mode == "shadow":
                silent_event = _unique_silent_sequence_successor(cursor)
            if (
                silent_event is not None
                and silent_event.kind == "silent"
                and _is_silent_sequence_text(text)
            ):
                snapshot = cursor.anchor_event(
                    silent_event.event_id,
                    "visual-transition-confirmed",
                )
                line = None
                match_result = "expected-silent-ellipsis"
                candidate_events = (silent_event,)
            else:
                candidate_event_ids = tuple(
                    dict.fromkeys(
                        (
                            *(
                                (current.event_id,)
                                if current is not None and current.is_speech
                                else ()
                            ),
                            *(event.event_id for event in candidate_events),
                        )
                    )
                )
                candidate_line_ids = tuple(
                    self.live_sequence_plan.events[event_id].line_id
                    for event_id in candidate_event_ids
                    if self.live_sequence_plan.events[event_id].line_id is not None
                )
                resolve_bounded = getattr(
                    self.chapter_voice_preloader,
                    "resolve_bounded_among",
                    self.chapter_voice_preloader.resolve_exact_among,
                )
                line, match_result = resolve_bounded(
                    character, text, candidate_line_ids
                )
                if self.settings.live_sequence_mode == "audio-auto" and "prefix" in str(
                    match_result
                ):
                    # Canonical audio may be prepared from a safe prefix in manual
                    # mode, but automatic control must wait until the visible box
                    # itself is complete so a later typewriter update cannot look
                    # like the post-key transition.
                    line = None
                snapshot = (
                    None
                    if line is None
                    else cursor.observe_bounded_line(line.line_id, candidate_event_ids)
                )
        if line is None and snapshot is not None:
            pass
        elif line is None or line.line_id is None:
            generation = (
                self.live_reader.active_generation
                if self.live_reader is not None
                else 0
            )
            self.pipeline_event_handler(
                "sequence-candidate-miss",
                generation,
                monotonic(),
                state=cursor.state.value,
                event_id=previous_event_id,
                candidate_event_ids=tuple(event.event_id for event in candidate_events),
                match_result=match_result,
            )
            self._publish_live_sequence_status()
            return None
        generation = (
            self.live_reader.active_generation if self.live_reader is not None else 0
        )
        if (
            confirming_dispatch
            and snapshot.state != StoryCursorState.DESYNCHRONIZED
            and snapshot.current_event_id != previous_event_id
            and self.live_reader is not None
        ):
            self.live_reader.confirm_pending_auto_advance()
        self.pipeline_event_handler(
            (
                "sequence-shadow"
                if self.settings.live_sequence_mode == "shadow"
                else f"sequence-{self.settings.live_sequence_mode}"
            ),
            generation,
            monotonic(),
            state=snapshot.state.value,
            previous_event_id=previous_event_id,
            event_id=snapshot.current_event_id,
            line_id=None if line is None else snapshot.current_line_id,
            next_event_count=len(snapshot.expected_successor_ids),
            reason=snapshot.reason,
            match_result=match_result,
        )
        self._publish_live_sequence_status()
        return snapshot, line, match_result

    def live_sequence_anchor_options(self):
        with self.story_cursor_lock:
            return self._live_sequence_anchor_options_locked()

    def _live_sequence_anchor_options_locked(self):
        plan = self.live_sequence_plan
        if plan is None or not is_live_sequence_audio_mode(
            self.settings.live_sequence_mode
        ):
            return ()
        options = []
        for chapter in plan.chapters:
            entries = set(chapter.entry_event_ids)
            for event_id in chapter.event_ids:
                event = plan.events[event_id]
                if event.kind not in {"speech", "silent"}:
                    continue
                line = (
                    None
                    if event.line_id is None
                    else self.chapter_voice_preloader.line_for_id(event.line_id)
                )
                speaker = line.speaker if line is not None else "Silent"
                text = line.text if line is not None else "silent dialogue"
                preview = text if len(text) <= 90 else f"{text[:87]}..."
                entry = "entry; " if event_id in entries else ""
                label = (
                    f"Chapter {chapter.chapter}, {entry}sequence {event.sequence} - "
                    f"{speaker}: {preview} [{event_id}]"
                )
                options.append((label, event_id))
        return tuple(options)

    def _expected_live_sequence_events(self):
        with self.story_cursor_lock:
            return self._expected_live_sequence_events_locked()

    def _expected_live_sequence_events_locked(self):
        cursor = self.story_cursor
        if cursor is None or not is_live_sequence_audio_mode(
            self.settings.live_sequence_mode
        ):
            return ()
        if cursor.state in {
            StoryCursorState.UNSYNCHRONIZED,
            StoryCursorState.ANCHORING,
            StoryCursorState.PLAYING,
        }:
            return ()
        if (
            cursor.state == StoryCursorState.LOCKED
            and not cursor.can_confirm_visual_transition
        ):
            return ()
        return cursor.bounded_visible_successors()

    def live_sequence_expected_options(self):
        with self.story_cursor_lock:
            return self._live_sequence_expected_options_locked()

    def _live_sequence_expected_options_locked(self):
        options = []
        for event in self._expected_live_sequence_events_locked():
            line = (
                None
                if event.line_id is None
                else self.chapter_voice_preloader.line_for_id(event.line_id)
            )
            speaker = line.speaker if line is not None else "Silent"
            text = line.text if line is not None else "silent dialogue"
            preview = text if len(text) <= 90 else f"{text[:87]}..."
            options.append(
                (
                    f"Sequence {event.sequence} - {speaker}: {preview} "
                    f"[{event.event_id}]",
                    event.event_id,
                )
            )
        return tuple(options)

    def select_expected_live_sequence_event(self, event_id):
        with self.story_cursor_lock:
            candidates = {
                event.event_id: event
                for event in self._expected_live_sequence_events_locked()
            }
            event = candidates.get(str(event_id))
            if self.story_cursor is None or event is None:
                self.status_handler(
                    "Expected story event was not selected: the candidate is stale or "
                    "outside the current bounded path"
                )
                return False
            return self._apply_explicit_live_sequence_event_locked(
                event,
                reason=(
                    "visual-transition-confirmed"
                    if event.kind == "silent"
                    else "explicit-expected-selection"
                ),
                pipeline_stage="sequence-explicit-expected-selection",
                success_message=(
                    f"Expected story event selected: sequence {event.sequence}"
                ),
            )

    def _apply_explicit_live_sequence_event_locked(
        self,
        event,
        *,
        reason,
        pipeline_stage,
        success_message,
    ):
        cursor = self.story_cursor
        if cursor is None:
            return False
        previous_event_id = cursor.current_event_id
        running = bool(self.live_reader is not None and self.live_reader.is_running)
        line = (
            None
            if event.line_id is None
            else self.chapter_voice_preloader.select_line_id(event.line_id)
        )
        if event.is_speech and line is None:
            self._report_explicit_live_sequence_outcome(
                pipeline_stage,
                previous_event_id,
                event,
                "missing-canonical-line",
            )
            self._publish_live_sequence_status()
            self.status_handler(
                "Story event was not selected: its canonical line is unavailable"
            )
            return False
        if (
            running
            and line is not None
            and self._offer_unknown_speaker_mapping(line.speaker, line.text)
        ):
            self._report_explicit_live_sequence_outcome(
                pipeline_stage,
                previous_event_id,
                event,
                "voice-decision-deferred",
            )
            self._publish_live_sequence_status()
            return False
        if running:
            self.live_reader.clear_queue()
        cursor.anchor_event(event.event_id, reason)
        if running:
            try:
                enqueued = (
                    True if line is None else self._enqueue_selected_sequence_line(line)
                )
            except Exception as error:
                self.error_handler(error)
                enqueued = False
            if not enqueued:
                cursor.desynchronize("explicit-route-failed")
                self._report_explicit_live_sequence_outcome(
                    pipeline_stage,
                    previous_event_id,
                    event,
                    "route-failed",
                )
                self._publish_live_sequence_status()
                self.status_handler(
                    "Story event was selected but canonical audio could not be queued; "
                    "replay or set the visible story position"
                )
                return False
            self.live_reader.bind_current_frame_route()
            if line is None:
                self.dialog_handler("Narrator", "Silent dialogue")
        else:
            self.explicit_sequence_anchor_pending = True
            self.dialog_handler(
                line.speaker if line is not None else "Narrator",
                line.text if line is not None else "Silent dialogue",
            )
        self._report_explicit_live_sequence_outcome(
            pipeline_stage,
            previous_event_id,
            event,
            "accepted",
        )
        self._publish_live_sequence_status()
        self.status_handler(success_message)
        return True

    def resync_live_sequence(self, event_id):
        with self.story_cursor_lock:
            cursor = self.story_cursor
            plan = self.live_sequence_plan
            if (
                cursor is None
                or plan is None
                or not is_live_sequence_audio_mode(self.settings.live_sequence_mode)
            ):
                self.status_handler(
                    "Story position is unavailable: configure sequence-first manual "
                    "audio"
                )
                return False
            event = plan.events.get(str(event_id))
            if event is None or event.kind not in {"speech", "silent"}:
                self.status_handler(
                    "Story position was not changed: invalid visible event"
                )
                return False
            return self._apply_explicit_live_sequence_event_locked(
                event,
                reason=(
                    "visual-transition-confirmed"
                    if event.kind == "silent"
                    else "explicit-user-resync"
                ),
                pipeline_stage="sequence-explicit-user-resync",
                success_message=(
                    f"Story position set to chapter {event.chapter}, "
                    f"sequence {event.sequence}"
                ),
            )

    def _enqueue_selected_sequence_line(self, line):
        self._prime_observed_voice(line.speaker)
        self._prime_likely_chapter_voice(line.speaker, line.text)
        self.history.add(line.speaker, line.text)
        preview = line.text if len(line.text) <= 100 else f"{line.text[:97]}..."
        self.dialog_handler(line.speaker or "Narrator", preview)
        return self.live_reader.enqueue(
            line.speaker,
            line.text,
            line_id=line.line_id,
        )

    def _report_explicit_live_sequence_outcome(
        self,
        stage,
        previous_event_id,
        event,
        outcome,
    ):
        generation = (
            self.live_reader.active_generation if self.live_reader is not None else 0
        )
        self.pipeline_event_handler(
            stage,
            generation,
            monotonic(),
            previous_event_id=previous_event_id,
            event_id=event.event_id,
            line_id=event.line_id,
            reason=self.story_cursor.reason if self.story_cursor is not None else None,
            outcome=outcome,
        )

    def _stable_live_frame_route(
        self,
        _fingerprint,
        settled,
        expected_owner=None,
        route_epoch=None,
    ):
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if cursor is None or not is_live_sequence_audio_mode(
                self.settings.live_sequence_mode
            ):
                return None
            if expected_owner is not None and cursor.current_event_id != expected_owner:
                return False
            if (
                route_epoch is not None
                and self.live_reader is not None
                and not self.live_reader.frame_route_epoch_is_current(route_epoch)
            ):
                return False
            if cursor.state in {
                StoryCursorState.UNSYNCHRONIZED,
                StoryCursorState.ANCHORING,
            }:
                return None
            if not settled:
                return False
            if cursor.state in {
                StoryCursorState.MANUAL,
                StoryCursorState.DESYNCHRONIZED,
            }:
                return None
            if not cursor.can_confirm_visual_transition:
                return False
            visible_candidates = cursor.bounded_visible_successors()
            event = cursor.deterministic_visual_successor()
            if (
                event is None
                or len(visible_candidates) != 1
                or visible_candidates[0].event_id != event.event_id
            ):
                # Manual input may have crossed more than one dialogue box before
                # capture settled. Let bounded canonical recognition identify the
                # visible event instead of speaking an inferred intermediate line.
                return None
            previous_event_id = cursor.current_event_id
            confirming_dispatch = cursor.state == StoryCursorState.WAITING_TRANSITION
            confirmed_event = cursor.confirm_visual_transition()
            if confirmed_event is None or confirmed_event.event_id != event.event_id:
                return False
            if confirming_dispatch and self.live_reader is not None:
                self.live_reader.confirm_pending_auto_advance()
            generation = (
                self.live_reader.active_generation
                if self.live_reader is not None
                else 0
            )
            if event.kind == "silent":
                self.pipeline_event_handler(
                    "sequence-visual-transition",
                    generation,
                    monotonic(),
                    state=cursor.state.value,
                    previous_event_id=previous_event_id,
                    event_id=event.event_id,
                    line_id=None,
                    route="silent",
                    reason=cursor.reason,
                )
                self._publish_live_sequence_status()
                return SilentDialogRoute(event.event_id)
            line = self.chapter_voice_preloader.select_line_id(event.line_id)
            if line is None:
                cursor.desynchronize(f"missing-story-line:{event.line_id}")
                self.status_handler(
                    "Sequence-first routing stopped: the expected story line is missing"
                )
                self._publish_live_sequence_status()
                return False
            self.pipeline_event_handler(
                "sequence-visual-transition",
                generation,
                monotonic(),
                state=cursor.state.value,
                previous_event_id=previous_event_id,
                event_id=event.event_id,
                line_id=line.line_id,
                route="canonical-story-line",
                reason=cursor.reason,
            )
            self._publish_live_sequence_status()
            return (line.speaker, line.text)

    def _stable_live_frame_owner(self):
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if cursor is None or not is_live_sequence_audio_mode(
                self.settings.live_sequence_mode
            ):
                return None
            return cursor.current_event_id

    def _live_sequence_line_id(self, character, text):
        """Return the exact cursor-owned line identity for a routed observation."""
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if cursor is None or not is_live_sequence_audio_mode(
                self.settings.live_sequence_mode
            ):
                return None
            event = cursor.current_event
            if event is None or not event.is_speech or event.line_id is None:
                return None
            line = self.chapter_voice_preloader.line_for_id(event.line_id)
            if line is None or (line.speaker, line.text) != (character, text):
                return None
            return line.line_id

    def _begin_sequence_playback(self, chunk):
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if cursor is None or not is_live_sequence_audio_mode(
                self.settings.live_sequence_mode
            ):
                return None
            event = cursor.current_event
            if (
                cursor.state != StoryCursorState.LOCKED
                or event is None
                or not event.is_speech
                or (chunk.line_id is not None and chunk.line_id != event.line_id)
            ):
                return None
            line = self.chapter_voice_preloader.line_for_id(event.line_id)
            if line is None or (line.speaker, line.text) != (
                chunk.character,
                chunk.text,
            ):
                return None
            try:
                cursor.begin_playback()
            except StoryCursorError:
                return None
            self._publish_live_sequence_status()
            return event.event_id

    def _finish_sequence_playback(self, event_id, outcome):
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if (
                event_id is None
                or cursor is None
                or cursor.current_event_id != event_id
                or cursor.state != StoryCursorState.PLAYING
            ):
                return False
            successful = isinstance(outcome, PlaybackOutcome) and outcome.successful
            cursor.finish_playback(successful=successful)
            generation = (
                self.live_reader.active_generation
                if self.live_reader is not None
                else 0
            )
            self.pipeline_event_handler(
                "sequence-playback-state",
                generation,
                monotonic(),
                state=cursor.state.value,
                event_id=event_id,
                line_id=cursor.snapshot().current_line_id,
                outcome="completed" if successful else "failed",
            )
            self._publish_live_sequence_status()
            return successful

    def _canonical_observed_character(self, character, text=None):
        original = str(character or "Narrator").strip() or "Narrator"
        canonicalize = getattr(self.chapter_voice_preloader, "canonical_speaker", None)
        if callable(canonicalize):
            canonical = canonicalize(original)
            if isinstance(canonical, str) and normalize_character_name(
                canonical
            ) != normalize_character_name(original):
                original = canonical

        if text:
            resolve_by_text = getattr(
                self.chapter_voice_preloader,
                "resolve_unique_prefix_by_text",
                None,
            )
            if callable(resolve_by_text):
                line = resolve_by_text(text)
                if line is not None and isinstance(line.speaker, str):
                    return line.speaker

        registry = getattr(self.voice_router, "registry", None)
        if registry is not None:
            voice = registry.resolve_closest(original, minimum_similarity=0.86)
            if voice is not None and isinstance(getattr(voice, "character", None), str):
                return voice.character

        normalized = normalize_character_name(original)
        ranked = sorted(
            (
                SequenceMatcher(None, normalized, candidate).ratio(),
                candidate,
            )
            for candidate in self.narrator_fallback_speakers
            if len(normalized) >= 5 and len(candidate) >= 5
        )
        if ranked:
            best_score, best_key = ranked[-1]
            second_score = ranked[-2][0] if len(ranked) > 1 else 0.0
            if best_score >= 0.86 and best_score - second_score >= 0.08:
                return self.narrator_fallback_names.get(best_key, original)
        return original

    def _offer_unknown_speaker_mapping(self, character, text=None):
        if not self._speaker_requires_voice_decision(character, text):
            return False
        key = normalize_character_name(character)
        if key in self.pending_unknown_speakers:
            return True
        if key in self.reported_unknown_speakers:
            return False
        self.reported_unknown_speakers.add(key)
        self.pending_unknown_speakers.add(key)
        self.status_handler(
            f"No voice is assigned to {character.strip()}; speech is waiting "
            "for a voice choice"
        )
        self.unknown_speaker_handler(character.strip())
        return True

    def _speaker_requires_voice_decision(
        self,
        character,
        text=None,
        *,
        live_preflight=False,
    ):
        key = normalize_character_name(character)
        if not key or is_narrator(character) or self.voice_router is None:
            return False
        assignments = getattr(self.voice_router.registry, "assignments", {})
        if isinstance(assignments, dict) and key in assignments:
            return False
        if self.voice_router.registry.resolve(character) is not None:
            return False
        if key in self.narrator_fallback_speakers and not live_preflight:
            return False
        resolved_route_check = getattr(
            self.speech_backend,
            "has_resolved_route_in_live_mode",
            None,
        )
        if (
            text
            and (live_preflight or self.is_live_running)
            and callable(resolved_route_check)
            and resolved_route_check(character, text) is True
        ):
            return False
        source_audio_check = getattr(
            self.speech_backend,
            (
                "will_use_source_audio_in_live_mode"
                if live_preflight
                else "will_use_source_audio"
            ),
            None,
        )
        return not (
            text
            and callable(source_audio_check)
            and source_audio_check(character, text) is True
        )

    def _prime_observed_voice(self, character):
        prime = getattr(self.speech_backend, "prime", None)
        if not callable(prime) or self.speech_executor is None:
            return False
        character = synthesis_character(character)
        key = normalize_character_name(character) or "narrator"
        registry = getattr(self.voice_router, "registry", None)
        if (
            key != "narrator"
            and registry is not None
            and registry.resolve(character) is None
        ):
            return False
        with self.voice_prime_lock:
            if key in self.primed_voice_keys:
                return False
            self.primed_voice_keys.add(key)
            future = self.speech_executor.submit(prime, character)
            self.voice_prime_futures.add(future)
        future.add_done_callback(self._voice_prime_finished)
        return True

    def _prime_likely_chapter_voice(self, character, text):
        if self.voice_router is None:
            return False
        registry = getattr(self.voice_router, "registry", None)
        if registry is None:
            return False
        for recommendation in self.chapter_voice_preloader.recommend(character, text):
            voice = registry.resolve(recommendation)
            if voice is None:
                continue
            if self._prime_observed_voice(voice.character):
                return True
        return False

    def _voice_prime_finished(self, future):
        with self.voice_prime_lock:
            self.voice_prime_futures.discard(future)
        try:
            future.result()
        except Exception as error:
            self.error_handler(error)

    def _enqueue_dialog(self, character, text):
        character = self._canonical_observed_character(character, text)
        resolved_text = self._resolve_early_indexed_dialogue(character, text)
        if resolved_text is not None:
            text = resolved_text
        decision = self._dialog_observed(character, text)
        if decision is False:
            return False
        if isinstance(decision, SilentDialogRoute):
            return True
        if isinstance(decision, tuple) and len(decision) == 2:
            character, text = decision
        return self.live_reader.enqueue(character, text)

    def _ocr_uncertain(self, result: OCRResult, minimum_confidence):
        if self.live_reader is not None:
            self.live_reader.clear_queue()
        preview = result.text if len(result.text) <= 80 else f"{result.text[:77]}..."
        self.dialog_handler(
            "OCR uncertain",
            f"{result.confidence:.0f}% (requires {minimum_confidence}%): {preview}",
        )

    def _resolve_voice_label(self, character):
        return resolve_voice_label(self.voice_router, character)

    def _is_game_focused(self):
        if self.capture_target is None:
            return True
        return self.capture_target.is_focused()

    def _live_auto_advance_callback(self):
        if (
            not self.settings.auto_advance_enabled
            or self.settings.live_sequence_mode == "audio-manual"
        ):
            return None
        if self.settings.live_sequence_mode == "audio-auto":
            return self._sequence_auto_advance_dialog
        return self._auto_advance_dialog

    def _sequence_auto_advance_dialog(self):
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if (
                cursor is None
                or self.settings.live_sequence_mode != "audio-auto"
                or not cursor.can_auto_advance
            ):
                return AutoAdvanceAttempt(False, "cursor-not-auto-advance-eligible")
            if not self._is_game_focused():
                return AutoAdvanceAttempt(False, "focus-wait")
            advanced = self._auto_advance_dialog(focus_verified=True)
            if advanced is False:
                return AutoAdvanceAttempt(False, "dispatch-disabled")
            snapshot = cursor.dispatch_advance()
            generation = (
                self.live_reader.active_generation
                if self.live_reader is not None
                else 0
            )
            self.pipeline_event_handler(
                "sequence-key-dispatch-authorized",
                generation,
                monotonic(),
                event_id=snapshot.current_event_id,
                line_id=snapshot.current_line_id,
                next_event_count=len(snapshot.expected_successor_ids),
            )
            self._publish_live_sequence_status()
            return AutoAdvanceAttempt(True, "dispatched")

    def _auto_advance_dialog(self, *, focus_verified=False):
        if (
            not self.settings.auto_advance_enabled
            or self.settings.live_sequence_mode == "audio-manual"
            or (not focus_verified and not self._is_game_focused())
        ):
            return False
        DialogueAdvancer(self.settings.auto_advance_key).advance()
        return True

    def _auto_advance_state_changed(self, state, _generation, _attempt):
        with self.story_cursor_lock:
            awaiting_manual_boundary = bool(
                self.story_cursor is not None
                and self.story_cursor.deterministic_manual_successor() is not None
            )
        if state == "focus-wait":
            self.status_handler(
                "Auto advance is waiting; focus the selected game window"
            )
        elif state == "visual-wait":
            self.status_handler(
                "Auto advance is waiting for the current dialogue frame to remain "
                "visible and stable"
            )
        elif state == "blocked":
            self.status_handler(
                "Auto advance was blocked because the current cursor event no longer "
                "owns one safe automatic transition. Resynchronize manually."
            )
        elif state == "dispatched":
            self.status_handler(
                "Auto advance key sent; a choice/manual boundary is next. Make the "
                "in-game decision, then select the visible expected event."
                if awaiting_manual_boundary
                else "Auto advance key sent; waiting for dialogue change"
            )
        elif state == "waiting":
            self.status_handler(
                "A choice/manual boundary is waiting for your in-game decision; no "
                "second key will be sent."
                if awaiting_manual_boundary
                else "The game is still changing; auto advance is continuing to "
                "wait. No second key will be sent."
            )
        elif state == "failed":
            self.status_handler(
                "The expected choice/manual transition was not confirmed; no second "
                "key was sent. Make the decision and select the visible expected "
                "event."
                if awaiting_manual_boundary
                else "Dialogue change was not confirmed after the extended wait; no "
                "second key was sent. Advance manually."
            )
        elif state == "confirmed":
            self.status_handler("Auto advance confirmed by new dialogue")

    def _capture_state_changed(self, focused, interval_seconds):
        lost_focus = self.game_focused and not focused
        regained_focus = not self.game_focused and focused
        self.game_focused = focused
        self.capture_interval_ms = interval_seconds * 1000
        with self.diagnostic_lock:
            snapshot = self.last_diagnostic
            if snapshot is not None:
                snapshot = replace(
                    snapshot,
                    capture_interval_ms=self.capture_interval_ms,
                    game_focused=self.game_focused,
                )
                self.last_diagnostic = snapshot
        if snapshot is not None:
            self.diagnostic_handler(snapshot)
        if lost_focus:
            self.status_handler("Game focus lost; live capture and auto advance paused")
        elif regained_focus:
            self.status_handler("Game focus restored; live reading resumed")

    def _publish_diagnostic(self, snapshot, route_metrics=None, audio_source=None):
        pipeline_metrics = self.get_live_pipeline_metrics()
        snapshot = replace(
            snapshot,
            capture_interval_ms=self.capture_interval_ms,
            game_focused=self.game_focused,
            speech_queue_depth=(
                pipeline_metrics.speech_queue_depth if pipeline_metrics else 0
            ),
            max_speech_queue_depth=(
                pipeline_metrics.max_speech_queue_depth if pipeline_metrics else 0
            ),
        )
        if route_metrics is not None:
            snapshot = replace(
                snapshot,
                synthesis_ms=route_metrics.synthesis_ms,
                playback_ms=(
                    route_metrics.playback_ms
                    if isinstance(route_metrics, PlaybackOutcome)
                    else snapshot.playback_ms
                ),
                last_first_audio_ms=(
                    route_metrics.first_audio_ms
                    if isinstance(route_metrics, PlaybackOutcome)
                    else snapshot.last_first_audio_ms
                ),
                cache_source=route_metrics.cache_source,
                audio_source=audio_source
                or route_metrics.audio_source
                or "Not selected",
            )
        with self.diagnostic_lock:
            self.last_diagnostic = snapshot
        self.diagnostic_handler(snapshot)

    def _speak_live_chunk(self, chunk):
        try:
            return speak_live_chunk(
                self.voice_router,
                chunk,
                playback_guard=lambda: self.live_reader.wait_until_playable(chunk),
            )
        finally:
            self._refresh_diagnostic_metrics()

    def _prepare_live_chunk(self, chunk):
        prepared = None
        try:
            prepare_route = getattr(type(self.speech_backend), "prepare_route", None)
            prepare_playback = getattr(
                type(self.speech_backend), "prepare_playback", None
            )
            if callable(prepare_route):
                prepared = (
                    prepare_route(
                        self.speech_backend,
                        chunk.character,
                        chunk.text,
                        line_id=chunk.line_id,
                    )
                    if isinstance(self.speech_backend, GeneratedAudioFallbackBackend)
                    else prepare_route(self.speech_backend, chunk.character, chunk.text)
                )
            elif callable(prepare_playback):
                prepared = prepare_playback(
                    self.speech_backend, chunk.character, chunk.text
                )
            else:
                raise TypeError("Speech backend does not implement prepare_playback()")
            try:
                announcement, announced_speaker = self._prepare_speaker_announcement(
                    chunk, prepared
                )
            except Exception as error:
                announcement, announced_speaker = None, None
                self.error_handler(error)
                self.status_handler(
                    "Speaker announcement could not be prepared; continuing dialogue"
                )
            self.last_audio_source_description = self._describe_audio_source(prepared)
            trace = self._build_audio_route_trace(chunk, prepared)
            self.last_audio_route_trace = trace
            try:
                self.route_trace_handler(trace)
            except Exception as error:
                self.error_handler(error)
            self._record_pipeline_route(chunk, trace)
            self._publish_live_sequence_status()
            return (
                PreparedLiveChunkRoutes(
                    prepared,
                    speaker_announcement=announcement,
                    announced_speaker=announced_speaker,
                )
                if announcement is not None
                else prepared
            )
        finally:
            self._refresh_diagnostic_metrics(
                prepared
                if isinstance(
                    prepared,
                    (
                        GeneratedAudioRoute,
                        LiveFallbackRoute,
                        SourceAudioRoute,
                        LiveTTSRoute,
                        PreparedPlayback,
                    ),
                )
                else None,
                self._describe_audio_source(prepared) if prepared is not None else None,
            )

    def _play_live_chunk(self, chunk, audio):
        if isinstance(audio, PreparedLiveChunkRoutes):
            if audio.speaker_announcement is not None:
                try:
                    self._play_speaker_announcement(
                        chunk,
                        audio.speaker_announcement,
                        audio.announced_speaker,
                    )
                except Exception as error:
                    self.error_handler(error)
                    self.status_handler(
                        "Speaker announcement failed; continuing dialogue"
                    )
            audio = audio.dialogue
        source = self._describe_audio_source(audio)
        self.last_audio_source_description = source
        self.status_handler(f"Audio source for {chunk.character}: {source}")
        if (
            isinstance(audio, SourceAudioRoute)
            and audio.prepared.completion_seconds is None
        ) or (
            isinstance(audio, PreparedSourceAudioPassThrough)
            and audio.completion_seconds is None
        ):
            source_audio = (
                audio.prepared if isinstance(audio, SourceAudioRoute) else audio
            )
            reason = (
                f"Auto advance paused for original game audio line {source_audio.line_id}: "
                "completion timing is unavailable. Advance manually or select "
                "Live TTS only."
            )
            if self.live_reader.block_auto_advance_for_generation(
                chunk.generation,
                reason,
            ):
                self.status_handler(reason)
        playback_started = monotonic()
        outcome = None
        sequence_event_id = self._begin_sequence_playback(chunk)
        try:
            play_route = getattr(type(self.speech_backend), "play_route", None)
            play_prepared = getattr(type(self.speech_backend), "play_prepared", None)
            outcome = (
                play_route(
                    self.speech_backend,
                    audio,
                    playback_guard=lambda: self.live_reader.wait_until_playable(chunk),
                )
                if callable(play_route)
                and isinstance(
                    audio,
                    (
                        GeneratedAudioRoute,
                        LiveFallbackRoute,
                        SourceAudioRoute,
                        LiveTTSRoute,
                    ),
                )
                else None
            )
            if (
                outcome is None
                and callable(play_prepared)
                and isinstance(audio, PreparedPlayback)
            ):
                outcome = play_prepared(
                    self.speech_backend,
                    audio,
                    playback_guard=lambda: self.live_reader.wait_until_playable(chunk),
                )
            if outcome is None:
                raise TypeError("Speech backend does not implement typed playback")
            result = outcome.successful
            if not result:
                self.live_reader.block_auto_advance_for_generation(
                    chunk.generation,
                    "Playback was interrupted; retry or wait for a new dialogue",
                )
            if result and (
                is_live_sequence_audio_mode(self.settings.live_sequence_mode)
                or isinstance(
                    audio,
                    (
                        GeneratedAudioRoute,
                        SourceAudioRoute,
                        PreparedGeneratedAudio,
                        PreparedSourceAudioPassThrough,
                    ),
                )
            ):
                self.live_reader.seal_generation(chunk.generation)
            underflowed = outcome.underflowed
            generation_limited = outcome.generation_limited
            outcome_name = (
                outcome.status.value
                if outcome is not None
                else "completed"
                if result
                else "interrupted"
            )
            try:
                self.pipeline_event_handler(
                    "playback-completion",
                    chunk.generation,
                    monotonic(),
                    underflowed=underflowed,
                    generation_limited=generation_limited,
                    outcome=outcome_name,
                    synthesis_ms=(outcome.synthesis_ms if outcome else None),
                    playback_ms=(outcome.playback_ms if outcome else None),
                    first_audio_ms=(outcome.first_audio_ms if outcome else None),
                    cache_source=(outcome.cache_source if outcome else None),
                    effective_source=(outcome.audio_source if outcome else None),
                    chunk_id=chunk.chunk_id,
                    chunk_ordinal=chunk.ordinal,
                    chunk_characters=len(chunk.text),
                )
                self.pipeline_event_handler(
                    "playback-outcome",
                    chunk.generation,
                    monotonic(),
                    outcome=outcome_name,
                    underflowed=underflowed,
                    generation_limited=generation_limited,
                    synthesis_ms=(outcome.synthesis_ms if outcome else None),
                    playback_ms=(outcome.playback_ms if outcome else None),
                    first_audio_ms=(outcome.first_audio_ms if outcome else None),
                    cache_source=(outcome.cache_source if outcome else None),
                    effective_source=(outcome.audio_source if outcome else None),
                    chunk_id=chunk.chunk_id,
                    chunk_ordinal=chunk.ordinal,
                    chunk_characters=len(chunk.text),
                )
            except Exception as error:
                self.error_handler(error)
            if outcome is not None and outcome.status is PlaybackStatus.FAILED:
                raise AudioPlaybackError(outcome.error or "Audio playback failed")
            if generation_limited:
                self.status_handler(
                    "MOSS stopped at the dialogue safety limit; the line was not "
                    "cached. Auto advance remains safe after playback completes."
                )
            self._observe_live_playback_backpressure(underflowed)
            first_audio_ms = outcome.first_audio_ms
            if isinstance(first_audio_ms, (int, float)) and not isinstance(
                first_audio_ms, bool
            ):
                self.live_reader.record_first_pcm(
                    playback_started + first_audio_ms / 1000
                )
            return result
        finally:
            self._finish_sequence_playback(sequence_event_id, outcome)
            self._refresh_diagnostic_metrics(outcome, source)

    def _prepare_speaker_announcement(self, chunk, dialogue_route):
        mode = self.settings.effective_speaker_announcement_mode
        if mode == "off" or chunk.ordinal not in {None, 1}:
            return None, None
        visible_speaker = str(chunk.character or "Narrator").strip() or "Narrator"
        if mode == "narrator-fallback-roles":
            if isinstance(dialogue_route, GeneratedAudioRoute):
                announcement_speaker = dialogue_route.prepared.narrator_fallback_role
            elif isinstance(dialogue_route, LiveFallbackRoute):
                announcement_speaker = (
                    "Unknown"
                    if is_unattributed_speaker(visible_speaker)
                    else dialogue_route.decision.requested_voice_character
                )
            else:
                announcement_speaker = None
        else:
            announcement_speaker = (
                "Narrator"
                if is_unattributed_speaker(visible_speaker)
                else visible_speaker
            )
        speaker_key = normalize_character_name(
            announcement_speaker or visible_speaker
        ) or ("unknown" if is_unattributed_speaker(visible_speaker) else "narrator")
        with self.speaker_announcement_lock:
            if speaker_key == self.last_visible_speaker_key:
                return None, None
            if announcement_speaker is None:
                self.last_visible_speaker_key = speaker_key
                return None, None
            if isinstance(
                dialogue_route,
                (SourceAudioRoute, PreparedSourceAudioPassThrough),
            ):
                self.last_visible_speaker_key = speaker_key
                return None, None
            backend = getattr(self.speech_backend, "live_backend", self.speech_backend)
            prepare = getattr(type(backend), "prepare_playback", None)
            if not callable(prepare):
                raise TypeError(
                    "Live backend does not implement typed speaker announcements"
                )
            prepared = prepare(backend, "Narrator", f"{announcement_speaker}.")
            if not isinstance(prepared, PreparedPlayback):
                raise TypeError("Live backend returned an untyped speaker announcement")
            self.last_visible_speaker_key = speaker_key
        payload = prepared.payload
        trace = AudioRouteTrace(
            chunk.generation,
            "live-accessibility-announcement",
            "speaker-change",
            "setting-enabled",
            self._voice_reference_identifier("Narrator", payload),
            None,
            "speaker-announcement-v1",
            chunk_id=f"{chunk.chunk_id or chunk.generation}:speaker-announcement",
            chunk_ordinal=0,
            chunk_characters=len(announcement_speaker) + 1,
        )
        try:
            self.route_trace_handler(trace)
            trace_fields = trace.support_fields()
            trace_fields.pop("generation", None)
            self.pipeline_event_handler(
                "speaker-announcement-route",
                chunk.generation,
                monotonic(),
                **trace_fields,
            )
        except Exception as error:
            self.error_handler(error)
        return (
            LiveTTSRoute(
                prepared,
                trace,
                prepared.synthesis_ms,
                prepared.first_audio_ms,
                prepared.cache_source,
            ),
            announcement_speaker,
        )

    def _play_speaker_announcement(self, chunk, route, announced_speaker):
        backend = getattr(self.speech_backend, "live_backend", self.speech_backend)
        play = getattr(type(backend), "play_prepared", None)
        if not callable(play):
            raise TypeError("Live backend cannot play a typed speaker announcement")
        self.status_handler(f"Announcing speaker: {announced_speaker}")
        outcome = play(
            backend,
            route.prepared,
            playback_guard=lambda: self.live_reader.wait_until_playable(chunk),
        )
        if not isinstance(outcome, PlaybackOutcome):
            raise TypeError("Live backend returned an untyped announcement outcome")
        outcome = replace(
            outcome,
            audio_source="live-accessibility-announcement",
        )
        try:
            self.pipeline_event_handler(
                "speaker-announcement-outcome",
                chunk.generation,
                monotonic(),
                outcome=outcome.status.value,
                underflowed=outcome.underflowed,
                generation_limited=outcome.generation_limited,
                synthesis_ms=outcome.synthesis_ms,
                playback_ms=outcome.playback_ms,
                first_audio_ms=outcome.first_audio_ms,
                cache_source=outcome.cache_source,
                effective_source=outcome.audio_source,
                announced_speaker=announced_speaker,
                chunk_id=route.trace.chunk_id,
                chunk_ordinal=route.trace.chunk_ordinal,
                chunk_characters=route.trace.chunk_characters,
            )
        except Exception as error:
            self.error_handler(error)
        self._observe_live_playback_backpressure(outcome.underflowed)
        if outcome.status is PlaybackStatus.FAILED:
            self.error_handler(
                AudioPlaybackError(
                    outcome.error or "Speaker announcement playback failed"
                )
            )
        return outcome

    def _observe_live_playback_backpressure(self, underflowed):
        jobs, changed = self.live_speech_backpressure.observe_playback(
            underflowed=underflowed,
        )
        self.live_reader.max_speech_jobs = jobs
        if changed:
            self.status_handler(
                "Audio underrun detected; live speech prefetch disabled temporarily"
                if underflowed
                else "Audio playback stable; live speech prefetch restored"
            )

    def _describe_audio_source(self, prepared):
        if isinstance(prepared, LiveFallbackRoute):
            return (
                "Authorized live fallback "
                f"({prepared.decision.provider}/{prepared.decision.model})"
            )
        if isinstance(
            prepared,
            (SourceAudioRoute, GeneratedAudioRoute, LiveFallbackRoute, LiveTTSRoute),
        ):
            prepared = prepared.prepared
        if isinstance(prepared, PreparedPlayback):
            prepared = prepared.payload
        if isinstance(prepared, PreparedSourceAudioPassThrough):
            completion = (
                f", completion {prepared.completion_seconds:.2f}s"
                if prepared.completion_seconds is not None
                else ", completion unavailable"
            )
            return f"Original game audio (line {prepared.line_id}{completion})"
        if isinstance(prepared, PreparedGeneratedAudio):
            return f"Generated audio (line {prepared.line_id})"
        if isinstance(prepared, MossTTSPreparedSpeech):
            source = {
                "fresh-generation": "fresh generation",
                "memory-cache": "memory cache",
                "persistent-cache": "persistent cache",
            }.get(prepared.cache_source, prepared.cache_source)
            return f"MOSS {source} (voice {prepared.voice_key})"
        backend = (
            self.speech_backend.live_backend
            if isinstance(self.speech_backend, GeneratedAudioFallbackBackend)
            else self.speech_backend
        )
        name = getattr(backend, "name", self.settings.speech_backend)
        return f"Live TTS ({name})"

    def _record_pipeline_route(self, chunk, trace):
        occurred_at = monotonic()
        try:
            self.pipeline_event_handler(
                "route-decision",
                chunk.generation,
                occurred_at,
                effective_source=trace.effective_source,
                match_result=trace.match_result,
                fallback_reason=trace.fallback_reason,
                line_id=trace.line_id,
                artifact_preflight_state=trace.artifact_preflight_state,
                chunk_id=trace.chunk_id,
                chunk_ordinal=trace.chunk_ordinal,
                chunk_characters=trace.chunk_characters,
            )
            self.pipeline_event_handler(
                "voice-resolution",
                chunk.generation,
                occurred_at,
                voice_reference_id=trace.voice_reference_id,
                chunk_id=trace.chunk_id,
                chunk_ordinal=trace.chunk_ordinal,
                chunk_characters=trace.chunk_characters,
            )
        except Exception as error:
            self.error_handler(error)

    def _build_audio_route_trace(self, chunk, prepared):
        route = (
            prepared.trace
            if isinstance(
                prepared,
                (
                    SourceAudioRoute,
                    GeneratedAudioRoute,
                    LiveFallbackRoute,
                    LiveTTSRoute,
                ),
            )
            else None
        )
        if route is None:
            line, match_result = self._resolve_trace_line(chunk)
            if not isinstance(prepared, PreparedPlayback):
                raise TypeError("Speech backend returned an untyped prepared payload")
            effective_source = prepared.audio_source
            if self.settings.audio_source_policy == "live-tts-only":
                fallback_reason = "policy-live-tts-only"
                artifact_state = "not-requested-live-tts-policy"
            elif not self.settings.story_index:
                fallback_reason = "story-index-not-configured"
                artifact_state = "story-index-not-configured"
            else:
                fallback_reason = "audio-route-wrapper-unavailable"
                artifact_state = "audio-artifact-unavailable"
            route = AudioRouteTrace(
                None,
                effective_source,
                match_result,
                fallback_reason,
                None,
                line.line_id if line is not None else None,
                artifact_state,
            )
        prepared_payload = (
            prepared.prepared
            if isinstance(
                prepared,
                (
                    SourceAudioRoute,
                    GeneratedAudioRoute,
                    LiveFallbackRoute,
                    LiveTTSRoute,
                ),
            )
            else prepared
        )
        if isinstance(prepared_payload, PreparedPlayback):
            prepared_payload = prepared_payload.payload
        voice_reference_id = (
            None
            if isinstance(
                prepared_payload,
                (PreparedGeneratedAudio, PreparedSourceAudioPassThrough),
            )
            else self._voice_reference_identifier(chunk.character, prepared_payload)
        )
        return replace(
            route,
            generation=chunk.generation,
            voice_reference_id=voice_reference_id,
            chunk_id=chunk.chunk_id,
            chunk_ordinal=chunk.ordinal,
            chunk_characters=len(chunk.text),
        )

    def _resolve_trace_line(self, chunk):
        resolve = getattr(
            self.chapter_voice_preloader,
            "resolve_exact_with_result",
            None,
        )
        if callable(resolve):
            return resolve(chunk.character, chunk.text)
        line = self.chapter_voice_preloader.resolve_exact(
            chunk.character,
            chunk.text,
        )
        return line, "exact" if line is not None else "no-match"

    def _voice_reference_identifier(self, character, prepared):
        voice_key = str(getattr(prepared, "voice_key", "")).strip()
        registry = getattr(self.voice_router, "registry", None)
        voice = registry.resolve(character) if registry is not None else None
        if voice is not None and getattr(voice, "references", ()):
            key = voice_key or normalize_character_name(voice.character)
            return f"voice:{key}:reference-1"
        live_backend = (
            self.speech_backend.live_backend
            if isinstance(self.speech_backend, GeneratedAudioFallbackBackend)
            else self.speech_backend
        )
        if (
            voice is None
            and voice_key == "narrator"
            and getattr(live_backend, "name", None) == "moss-tts"
            and live_backend.narrator_reference
        ):
            return "voice:narrator:reference-1"
        narrator = normalize_character_name(character) in {"", "narrator"}
        if voice is None and narrator and self.settings.tts_speaker_wav:
            return f"voice:{voice_key or 'narrator'}:reference-1"
        if voice_key:
            return f"voice:{voice_key}:built-in"
        if voice is not None:
            return f"speaker:{voice.speaker}"
        narrator_speaker = getattr(self.voice_router, "narrator_speaker", None)
        return f"speaker:{narrator_speaker}" if narrator_speaker else None

    def _refresh_diagnostic_metrics(self, route_metrics=None, audio_source=None):
        with self.diagnostic_lock:
            snapshot = self.last_diagnostic
        if snapshot is not None:
            self._publish_diagnostic(snapshot, route_metrics, audio_source)

    def shutdown(self):
        self.shutdown_requested.set()
        self._set_backend_live_mode(False)
        if self.live_reader is not None:
            self.live_reader.stop()
            self.live_reader.clear_queue()
            self.live_reader.release_waiters()
            try:
                self.live_reader.wait()
            except Exception as error:
                self.error_handler(error)
            self.live_reader = None

        if self.capture_executor is not None:
            self.capture_executor.shutdown(wait=True)
            self.capture_executor = None
        if self.ocr_executor is not None:
            self.ocr_executor.shutdown(wait=True)
            self.ocr_executor = None
        if self.speech_executor is not None:
            self.speech_executor.shutdown(wait=True)
            self.speech_executor = None
        if self.playback_executor is not None:
            self.playback_executor.shutdown(wait=True)
            self.playback_executor = None
        self.schedule_dialog_read = None
        self._stop_tts()

    def _stop_tts(self):
        active_tts = self.tts
        if active_tts is not None and hasattr(active_tts, "stop"):
            try:
                active_tts.stop()
            except Exception as error:
                self.error_handler(error)
        shutdown = getattr(active_tts, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception as error:
                self.error_handler(error)
        self.tts = None
        self.voice_router = None
        self.speech_backend = None

    def _interrupt_speech(self):
        if self.tts is not None and hasattr(self.tts, "stop"):
            return self.tts.stop()
        return False
