"""Application controller and live-reading orchestration."""

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from functools import partial
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, RLock
from time import monotonic
from typing import Protocol, TypeGuard, runtime_checkable

from vntts.assets import ModelAssetManager
from vntts.audio_lifecycle import audio_lifecycle_context
from vntts.auto_advance import DialogueAdvancer
from vntts.auto_advance_policy import auto_advance_allowed
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller_components import (
    DiagnosticsComponent,
    LiveSessionComponent,
    RuntimeLifecycleComponent,
    VoiceAssignmentComponent,
)
from vntts.controller_components import (
    create_live_toggle as create_live_toggle,
)
from vntts.controller_components import speak_live_chunk as speak_live_chunk
from vntts.diagnostics import resolve_voice_label
from vntts.dialog_capture import (
    DiagnosticSnapshot,
    capture_live_frame,
    get_screenshot_directory,
    read_dialog_safely,
    recognize_live_frame,
    report_runtime_error,
)
from vntts.generated_audio import (
    AudioEventOmissionRoute,
    AudioRouteTrace,
    GeneratedAudioFallbackBackend,
    GeneratedAudioLibrary,
    GeneratedAudioRoute,
    LiveFallbackRoute,
    LiveTTSRoute,
    PendingGeneratedAudioRoute,
    PlaybackOutcome,
    PlaybackStatus,
    PreparedGeneratedAudio,
    PreparedSourceAudioPassThrough,
    RouteDecision,
    SourceAudioRoute,
)
from vntts.history import DialogueHistory
from vntts.live import (
    AdaptiveSpeechBackpressure,
    AutoAdvanceAttempt,
    CanonicalDialogRoute,
    DialogObservationDecision,
    LiveDialogReader,
    SilentDialogRoute,
    SpeechChunk,
)
from vntts.live_sequence import (
    LiveSequenceEvent,
    LiveSequencePlan,
    StoryCursor,
    StoryCursorError,
    StoryCursorSnapshot,
    StoryCursorState,
)
from vntts.live_snapshot import read_live_snapshot as read_live_snapshot
from vntts.live_speaker_corpus import LiveSpeakerCorpus
from vntts.live_speech import TypedPlaybackBackend, play_typed_text
from vntts.ocr import (
    DialogRegion,
    OCRResult,
    UncertainFrameRecorder,
    default_minimum_ocr_confidence,
    get_dialog_region,
)
from vntts.ocr_corrections import OCRCorrectionDictionary, OCRCorrectionStore
from vntts.playback import PreparedPlayback
from vntts.profiles import GameProfileStore
from vntts.runtime_config import (
    get_live_configuration,
    initialize_voice_registry,
    initialize_voice_router,
)
from vntts.services.tts_engine import AudioPlaybackError, TTSEngine
from vntts.settings import (
    AppSettings,
    is_live_sequence_audio_mode,
)
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    MossTTSPreparedSpeech,
    MossTTSVoiceRouterBackend,
    PocketTTSVoiceRouterBackend,
)
from vntts.speech_backend_contract import SpeechBackend
from vntts.speech_worker import (
    create_chatterbox_worker_backend,
    create_moss_worker_backend,
    create_pocket_worker_backend,
)
from vntts.voice_library import VoiceLibrary
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    VoiceChoice,
    VoiceEngine,
    application_voice_library,
    is_narrator,
    is_unattributed_speaker,
    normalize_character_name,
    synthesis_character,
)
from vntts.window_capture import WindowCaptureTarget, WindowGeometry


class _DialogReadFuture(Protocol):
    def done(self) -> bool: ...


class _DialogReadExecutor(Protocol):
    def submit(
        self, callback: Callable[..., object], /, *args: object, **kwargs: object
    ) -> _DialogReadFuture: ...


class _LiveReaderState(Protocol):
    @property
    def is_running(self) -> bool: ...


class _Cancellation(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...


class _ExecutorFuture(Protocol):
    def cancel(self) -> bool: ...
    def done(self) -> bool: ...
    def cancelled(self) -> bool: ...
    def result(self) -> object: ...
    def add_done_callback(
        self, callback: Callable[["_ExecutorFuture"], object]
    ) -> object: ...


class _Executor(Protocol):
    def submit(
        self, callback: Callable[..., object], /, *args: object, **kwargs: object
    ) -> _ExecutorFuture: ...

    def shutdown(self, wait: bool = True) -> None: ...


class _VoiceRouter(Protocol):
    registry: CharacterVoiceRegistry
    narrator_voice: CharacterVoice | None

    def warm_up(self, *, progress: Callable[[int, int, str], None]) -> int: ...


class _CaptureTarget(Protocol):
    def get_geometry(self) -> WindowGeometry: ...
    def is_focused(self) -> bool: ...


class _SpeechBackpressure(Protocol):
    def observe_playback(self, *, underflowed: bool) -> tuple[int, bool]: ...
    def reset(self) -> int: ...


class _DiagnosticRouteMetrics(Protocol):
    @property
    def synthesis_ms(self) -> float | None: ...

    @property
    def playback_ms(self) -> float | None: ...

    @property
    def first_audio_ms(self) -> float | None: ...

    @property
    def cache_source(self) -> str | None: ...

    @property
    def audio_source(self) -> str | None: ...


@dataclass(frozen=True)
class _RouteDiagnosticMetrics:
    synthesis_ms: float | None
    playback_ms: float | None
    first_audio_ms: float | None
    cache_source: str | None
    audio_source: str | None


class _LiveSequenceChapter(Protocol):
    chapter: str
    entry_event_ids: Sequence[str]
    event_ids: Sequence[str]


class _LiveSequencePlanContract(Protocol):
    events: Mapping[str, LiveSequenceEvent]
    chapters: Sequence[_LiveSequenceChapter]

    def event_for_line(self, line_id: str) -> LiveSequenceEvent | None: ...


@runtime_checkable
class _StoryCursor(Protocol):
    plan: _LiveSequencePlanContract
    current_event_id: str | None
    occurrence_id: int
    reason: str | None
    state: StoryCursorState
    current_event: LiveSequenceEvent | None
    can_auto_advance: bool
    can_confirm_visual_transition: bool

    def snapshot(self) -> StoryCursorSnapshot: ...
    def reset(self, reason: str) -> None: ...
    def desynchronize(self, reason: str) -> None: ...
    def bounded_visible_successors(
        self, *, maximum_visible_depth: int = 3, maximum_nodes: int = 24
    ) -> tuple[LiveSequenceEvent, ...]: ...
    def deterministic_visual_successor(self) -> LiveSequenceEvent | None: ...
    def deterministic_upcoming_visible_event(self) -> LiveSequenceEvent | None: ...
    def deterministic_manual_successor(self) -> LiveSequenceEvent | None: ...
    def confirm_visual_transition(self) -> LiveSequenceEvent | None: ...
    def anchor_event(
        self, event_id: str, reason: str | None = None
    ) -> StoryCursorSnapshot: ...
    def begin_playback(self) -> StoryCursorSnapshot: ...
    def finish_playback(self, *, successful: bool = True) -> StoryCursorSnapshot: ...
    def dispatch_advance(self) -> StoryCursorSnapshot: ...
    def observe_line(self, line_id: str) -> StoryCursorSnapshot: ...
    def observe_bounded_line(
        self, line_id: str, allowed_event_ids: Sequence[str]
    ) -> StoryCursorSnapshot: ...


def _is_silent_sequence_text(value: object) -> bool:
    return "".join(str(value).split()) in {"...", "…"}


def _is_typed_playback_backend(value: object) -> TypeGuard[TypedPlaybackBackend]:
    return callable(getattr(value, "prepare_playback", None)) and callable(
        getattr(value, "play_prepared", None)
    )


def _is_story_cursor(value: object) -> TypeGuard[_StoryCursor]:
    return isinstance(value, _StoryCursor)


def _diagnostic_route_metrics(value: object) -> _DiagnosticRouteMetrics | None:
    if isinstance(
        value,
        (RouteDecision, PreparedPlayback),
    ):
        trace = getattr(value, "trace", None)
        return _RouteDiagnosticMetrics(
            synthesis_ms=value.synthesis_ms,
            playback_ms=getattr(value, "playback_ms", None),
            first_audio_ms=value.first_audio_ms,
            cache_source=value.cache_source,
            audio_source=(
                value.audio_source
                if isinstance(value, PreparedPlayback)
                else trace.effective_source
                if isinstance(trace, AudioRouteTrace)
                else None
            ),
        )
    return None


def _unique_silent_sequence_successor(
    cursor: _StoryCursor,
) -> LiveSequenceEvent | None:
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
    story_title: str | None = None


def create_dialog_read_scheduler(
    executor: _DialogReadExecutor,
    voice_router: object,
    screenshot_directory: str | Path,
    *,
    live_reader: _LiveReaderState | None = None,
    error_handler: Callable[[Exception], object] | None = None,
    capture_target: object | None = None,
    speech_handler: Callable[..., object] | None = None,
    minimum_confidence: float = default_minimum_ocr_confidence,
    uncertain_frame_recorder: object | None = None,
    diagnostic_handler: Callable[[DiagnosticSnapshot], object] | None = None,
    voice_resolver: Callable[[str], str] | None = None,
    ocr_language: str = "eng",
    correction_dictionary: OCRCorrectionDictionary | None = None,
    region_provider: Callable[[], DialogRegion] | None = None,
) -> Callable[[], bool]:
    active_read: _DialogReadFuture | None = None
    active_read_lock = Lock()

    def schedule_dialog_read() -> bool:
        nonlocal active_read

        with active_read_lock:
            if live_reader is not None and live_reader.is_running:
                print("Stop live reading before requesting a one-time read")
                return False
            if active_read is not None and not active_read.done():
                print("A dialog read is already in progress")
                return False

            active_read = executor.submit(
                read_dialog_safely,
                voice_router,
                screenshot_directory,
                error_handler=error_handler,
                capture_target=capture_target,
                speech_handler=speech_handler,
                minimum_confidence=minimum_confidence,
                uncertain_frame_recorder=uncertain_frame_recorder,
                diagnostic_handler=diagnostic_handler,
                voice_resolver=voice_resolver,
                ocr_language=ocr_language,
                correction_dictionary=correction_dictionary,
                region=region_provider() if region_provider is not None else None,
            )
            return True

    return schedule_dialog_read


@dataclass(frozen=True)
class PreparedLiveChunkRoutes:
    dialogue: object
    speaker_announcement: LiveTTSRoute | None = None
    announced_speaker: str | None = None


@dataclass(frozen=True)
class SequenceEventLease:
    event_id: str
    occurrence_id: int


class AppController:
    settings: AppSettings
    capture_target: _CaptureTarget | None
    capture_executor: _Executor | None
    ocr_executor: _Executor | None
    speech_executor: _Executor | None
    playback_executor: _Executor | None
    chapter_voice_preloader: ChapterVoicePreloader
    live_reader: LiveDialogReader | None
    live_sequence_plan: _LiveSequencePlanContract | None
    story_cursor: _StoryCursor | None
    speech_backend: SpeechBackend | GeneratedAudioFallbackBackend | None
    tts: VoiceEngine | _VoiceRouter | None
    voice_router: _VoiceRouter | None
    schedule_dialog_read: Callable[[], bool] | None
    live_speaker_corpus: LiveSpeakerCorpus | None
    live_speaker_corpus_error: str | None
    last_diagnostic: DiagnosticSnapshot | None
    last_audio_route_trace: AudioRouteTrace | None
    dialog_handler: Callable[..., object]
    diagnostic_handler: Callable[[object], object]
    error_handler: Callable[[Exception], object]
    sequence_status_handler: Callable[[LiveSequenceStatus], object]
    status_handler: Callable[..., object]
    live_scope_identification_failure: str | None
    live_scope_identification_match_result: str | None
    last_visible_speaker_key: str | None
    capture_interval_ms: float

    def __init__(
        self,
        settings: AppSettings | None = None,
        *,
        tts_factory: Callable[..., object] = TTSEngine,
        status_handler: Callable[..., object] = print,
        dialog_handler: Callable[..., object] | None = None,
        diagnostic_handler: Callable[[object], object] | None = None,
        sequence_status_handler: Callable[[LiveSequenceStatus], object] | None = None,
        unknown_speaker_handler: Callable[[str], object] | None = None,
        error_handler: Callable[[Exception], object] = report_runtime_error,
        capture_target_factory: Callable[
            [str | None], _CaptureTarget
        ] = WindowCaptureTarget,
        model_asset_manager_factory: Callable[
            [], ModelAssetManager
        ] = ModelAssetManager,
        chatterbox_backend_factory: Callable[
            ..., object
        ] = create_chatterbox_worker_backend,
        moss_backend_factory: Callable[..., object] = create_moss_worker_backend,
        pocket_backend_factory: Callable[..., object] = create_pocket_worker_backend,
        speech_backpressure_factory: Callable[
            ..., _SpeechBackpressure
        ] = AdaptiveSpeechBackpressure,
        correction_store: OCRCorrectionStore | None = None,
        history: DialogueHistory | None = None,
        chapter_voice_preloader: ChapterVoicePreloader | None = None,
        generated_audio_library_factory: Callable[
            ..., GeneratedAudioLibrary | None
        ] = GeneratedAudioLibrary.load_optional,
        generated_audio_backend_factory: Callable[
            ..., GeneratedAudioFallbackBackend
        ] = GeneratedAudioFallbackBackend,
        route_trace_handler: Callable[[AudioRouteTrace], object] | None = None,
        pipeline_event_handler: Callable[..., object] | None = None,
        live_sequence_plan_factory: Callable[
            [str, str], _LiveSequencePlanContract
        ] = LiveSequencePlan.load,
        voice_library: VoiceLibrary | None = None,
        profile_store: GameProfileStore | None = None,
    ) -> None:
        self.settings = settings or AppSettings()
        self.profile_store = profile_store or GameProfileStore.load()
        self.voice_library = voice_library or application_voice_library()
        self.capture_target_factory = capture_target_factory
        self.model_assets = model_asset_manager_factory()
        self.chatterbox_backend_factory = chatterbox_backend_factory
        self.moss_backend_factory = moss_backend_factory
        self.pocket_backend_factory = pocket_backend_factory
        self.speech_backpressure_factory = speech_backpressure_factory
        self.dialog_read_scheduler_factory = create_dialog_read_scheduler
        self.thread_pool_executor_factory = ThreadPoolExecutor
        self.live_reader_factory: Callable[..., LiveDialogReader] = LiveDialogReader
        self.voice_registry_initializer = partial(
            initialize_voice_registry, voice_library=self.voice_library
        )
        self.voice_router_initializer = partial(
            initialize_voice_router, voice_library=self.voice_library
        )
        self.correction_store = correction_store or OCRCorrectionStore.load()
        self.correction_dictionary = self.correction_store.dictionary_for(
            self.settings.active_profile_id
        )
        self.live_scope_identification_failure = None
        self.live_scope_identification_match_result = None
        self.live_scope_identification_diagnostics: dict[str, object] = {}
        self.allow_unscoped_live_reading = False
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
        self._pipeline_event_sink = pipeline_event_handler or (
            lambda _stage, _generation, _occurred_at, **_details: None
        )
        self.live_reader_session_id: str | None = None
        self.pipeline_event_handler = self._record_pipeline_event
        self.live_sequence_plan_factory = live_sequence_plan_factory
        self.sequence_prefetch_lock = Lock()
        self.sequence_prefetch_keys: set[tuple[object, ...]] = set()
        self.sequence_event_terminal_routes: dict[SequenceEventLease, str] = {}
        self.sequence_advance_leases: set[SequenceEventLease] = set()
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
        self.sequence_prefix_confirmation_event_id = None
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
        self.capture_interval_ms = float(self.settings.live_interval_ms)
        self.game_focused = True
        self.diagnostic_lock = Lock()
        self.voice_prime_lock = Lock()
        self.speaker_announcement_lock = Lock()
        self.last_visible_speaker_key = None
        self.primed_voice_keys: set[str] = set()
        self.reported_unknown_speakers: set[str] = set()
        self.pending_unknown_speakers: set[str] = set()
        self.narrator_fallback_speakers: set[str] = set()
        self.narrator_fallback_names: dict[str, str] = {}
        self.next_live_narrator_fallback_names: dict[str, str] = {}
        self.voice_prime_futures: set[_ExecutorFuture] = set()
        self.shutdown_requested = Event()
        self.runtime_lifecycle = RuntimeLifecycleComponent(self)
        self.live_session = LiveSessionComponent(self)
        self.voice_assignments = VoiceAssignmentComponent(self)
        self.diagnostics = DiagnosticsComponent(self)

    def _record_pipeline_event(
        self,
        stage: str,
        generation: int,
        occurred_at: float,
        **details: object,
    ) -> object:
        session_id = details.pop("session_id", None)
        active_session_id = (
            session_id if isinstance(session_id, str) else self.live_reader_session_id
        )
        if active_session_id is not None:
            details["session_id"] = active_session_id
        return self._pipeline_event_sink(stage, generation, occurred_at, **details)

    @property
    def is_ready(self) -> bool:
        return self.live_reader is not None

    @property
    def is_live_running(self) -> bool:
        return self.live_reader is not None and self.live_reader.is_running

    def start(self) -> bool:
        return self.runtime_lifecycle.start()

    def prepare_startup(self) -> None:
        self.shutdown_requested.clear()

    def request_shutdown(self) -> None:
        self.shutdown_requested.set()

    def apply_settings(
        self, settings: AppSettings, *, cancellation: _Cancellation | None = None
    ) -> object:
        return self.runtime_lifecycle.apply_settings(
            settings, cancellation=cancellation
        )

    def cancel_settings_apply(self, cancellation: _Cancellation) -> bool:
        return self.runtime_lifecycle.cancel_settings_apply(cancellation)

    def shutdown(self) -> None:
        return self.runtime_lifecycle.shutdown()

    def read_once(self) -> bool:
        return self.live_session.read_once()

    def identify_live_scope(self) -> bool:
        return self.live_session.identify_scope()

    def toggle_live(self) -> bool:
        return self.live_session.toggle()

    def start_live_from_ocr(self) -> bool:
        self.allow_unscoped_live_reading = True
        running = self.live_session.toggle()
        if not running:
            self.allow_unscoped_live_reading = False
        return running

    def toggle_speech_pause(self) -> bool:
        return self.live_session.toggle_speech_pause()

    def skip_current_speech(self) -> bool:
        return self.live_session.skip_current_speech()

    def repeat_last_speech(self) -> bool:
        return self.live_session.repeat_last_speech()

    def clear_speech_queue(self) -> bool:
        return self.live_session.clear_speech_queue()

    def emergency_stop(self) -> bool:
        return self.live_session.emergency_stop()

    def set_auto_advance_enabled(self, enabled: bool) -> bool:
        return self.live_session.set_auto_advance_enabled(enabled)

    def available_voice_characters(self) -> list[str]:
        return self.voice_assignments.available_characters()

    def available_voice_choices(self) -> list[VoiceChoice]:
        return self.voice_assignments.available_choices()

    def voice_assignment_for(self, character: str) -> str | None:
        return self.voice_assignments.assignment_for(character)

    def preview_voice_choice(self, source_id: str, text: str) -> object:
        return self.voice_assignments.preview_choice(source_id, text)

    def stop_voice_preview(self) -> bool:
        return self.voice_assignments.stop_preview()

    def assign_voice(
        self,
        character: str,
        source_id: str,
        *,
        commit_settings: Callable[[AppSettings], object] | None = None,
    ) -> AppSettings:
        return self.voice_assignments.assign(
            character,
            source_id,
            commit_settings=commit_settings,
        )

    def clear_voice_assignment(
        self,
        character: str,
        *,
        commit_settings: Callable[[AppSettings], object] | None = None,
    ) -> AppSettings:
        return self.voice_assignments.clear(
            character,
            commit_settings=commit_settings,
        )

    def set_force_live_narrator(
        self,
        enabled: bool,
        *,
        commit_settings: Callable[[AppSettings], object] | None = None,
    ) -> AppSettings:
        return self.voice_assignments.set_force_live_narrator(
            enabled,
            commit_settings=commit_settings,
        )

    def allow_narrator_fallback(self, character: str) -> bool:
        return self.voice_assignments.allow_narrator_fallback(character)

    def unresolved_live_speakers(self) -> tuple[str, ...] | None:
        return self.voice_assignments.unresolved_live_speakers()

    def approve_live_narrator_fallbacks(
        self, characters: Sequence[object]
    ) -> tuple[str, ...]:
        return self.voice_assignments.approve_narrator_fallbacks(characters)

    def preview_voice(self, character: str, text: str) -> object:
        return self.voice_assignments.preview(character, text)

    def replay_dialog(self, character: str, text: str) -> object:
        return self.voice_assignments.replay(character, text)

    def get_capture_geometry(self) -> WindowGeometry | None:
        return self.diagnostics.capture_geometry()

    def get_latest_diagnostic(self) -> DiagnosticSnapshot | None:
        return self.diagnostics.latest()

    def get_live_pipeline_metrics(self) -> object:
        return self.diagnostics.pipeline_metrics()

    def inspect_current_dialog(self, *, notify: bool = True) -> object:
        return self.diagnostics.inspect_current_dialog(notify=notify)

    def test_current_dialog(self) -> tuple[str, str]:
        return self.diagnostics.test_current_dialog()

    def _resolve_initial_live_sequence_line(
        self, character: str, text: str
    ) -> tuple[ChapterDialogue | None, object]:
        """Resolve one complete startup line inside the configured sequence."""
        plan = self.live_sequence_plan
        line_ids = (
            tuple(
                line.line_id
                for line in self.chapter_voice_preloader.dialogue
                if line.line_id and line.text_sha256
            )
            if plan is None or self.settings.live_sequence_mode == "off"
            else tuple(
                event.line_id
                for event in plan.events.values()
                if event.is_speech and event.line_id is not None
            )
        )
        previous_match = self.chapter_voice_preloader.current_match
        line, match_result = self.chapter_voice_preloader.resolve_bounded_among(
            character,
            text,
            line_ids,
        )
        if line is None:
            return None, match_result

        # Startup must not lock onto typewriter text that is still growing. A
        # full line with a small OCR substitution is safe when it remains the
        # only bounded candidate; a prefix is not proof that the line is done.
        normalized_result = str(match_result)
        observed = " ".join(str(text).casefold().split())
        canonical = " ".join(str(line.text).casefold().split())
        coverage = min(len(observed), len(canonical)) / max(
            1,
            len(observed),
            len(canonical),
        )
        if "prefix" in normalized_result or (
            "similarity" in normalized_result and coverage < 0.9
        ):
            self.chapter_voice_preloader.current_match = previous_match
            self.chapter_voice_preloader.last_resolution_diagnostics.update(
                match_result="expected-incomplete",
                candidate_rejection_reason="incomplete-typewriter-text",
            )
            return None, "expected-incomplete"
        return line, match_result

    def _load_live_speaker_corpus(self) -> None:
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

    def _revalidate_live_speaker_corpus(self) -> bool:
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

    def _get_live_configuration(self) -> dict[str, object]:
        configuration = get_live_configuration(self.settings)
        tracker_options = dict(configuration["tracker_options"])
        tracker_options["complete_dialogue_only"] = bool(
            self.settings.audio_source_policy != "live-tts-only"
            or self._live_sequence_audio_active()
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

    def _resolve_early_indexed_dialogue(self, character: str, text: str) -> str | None:
        backend = self.speech_backend
        if not isinstance(backend, GeneratedAudioFallbackBackend):
            return None
        line = self.chapter_voice_preloader.resolve_unique_prefix(
            character,
            text,
        )
        if line is None or not backend.has_generated_line(line):
            return None
        return str(line.text)

    def _has_manual_voice_override(self, character: str) -> bool:
        if is_unattributed_speaker(character):
            return False
        return bool(is_narrator(character) and self.settings.force_live_narrator)

    def _set_backend_live_mode(self, active: bool) -> None:
        configure = getattr(self.speech_backend, "set_live_mode_active", None)
        if callable(configure):
            configure(active)

    def _configure_generated_audio_backend(self) -> bool:
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
        generated_backend = self.generated_audio_backend_factory(
            live_backend,
            library,
            self.chapter_voice_preloader,
            **backend_options,
        )
        generated_backend.voice_override = self._has_manual_voice_override
        generated_backend.progress_wait_status = self.status_handler
        self.speech_backend = generated_backend
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

    def refresh_corrections(self) -> None:
        self.correction_store = OCRCorrectionStore.load(self.correction_store.path)
        self.correction_dictionary = self.correction_store.dictionary_for(
            self.settings.active_profile_id
        )

    def _create_capture_target(self) -> _CaptureTarget | None:
        if self.settings.capture_mode != "window":
            return None
        return self.capture_target_factory(self.settings.game_window_title)

    def _capture_live_frame(self) -> object:
        return capture_live_frame(
            get_screenshot_directory(self.settings),
            self.capture_target,
            region=self._capture_region(),
        )

    def _capture_region(self) -> DialogRegion:
        profile = self.profile_store.get(self.settings.active_profile_id)
        return get_dialog_region(profile.dialog_region if profile is not None else None)

    def _recognize_live_frame(self, frame: object) -> tuple[str, str]:
        voice_router = self.voice_router
        if voice_router is None:
            raise RuntimeError("The speech engine is not ready")
        character, text = recognize_live_frame(
            frame,
            voice_router.registry,
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

    def _preview_voice(self, character: str, text: str) -> tuple[str, str]:
        try:
            self._speak_with_live_backend(character, text)
        finally:
            self._refresh_diagnostic_metrics()
        return character, text

    def _preview_voice_choice(self, choice: VoiceChoice, text: str) -> tuple[str, str]:
        voice_router = self.voice_router
        if voice_router is None:
            raise RuntimeError("The speech engine is not ready")
        registry = voice_router.registry
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

    def _speak_with_live_backend(self, character: str, text: str) -> object:
        live_backend: object = self.speech_backend
        if isinstance(live_backend, GeneratedAudioFallbackBackend):
            live_backend = live_backend.live_backend
        live_backend = live_backend or self.voice_router
        if not _is_typed_playback_backend(live_backend):
            raise TypeError("Live backend does not implement typed playback")
        return play_typed_text(live_backend, character, text)

    def _apply_narrator_voice(self, voice: CharacterVoice | None) -> None:
        voice_router = self.voice_router
        if voice_router is None:
            return
        set_narrator_voice = getattr(voice_router, "set_narrator_voice", None)
        if callable(set_narrator_voice):
            set_narrator_voice(voice, self.settings.tts_speaker_wav)
        elif isinstance(voice_router, PocketTTSVoiceRouterBackend):
            voice_router.narrator_reference = (
                voice.references[0]
                if voice is not None and voice.references
                else voice.speaker
                if voice is not None
                else self.settings.tts_speaker_wav or "alba"
            )
            voice_router.voice_states.pop("narrator", None)
        elif isinstance(voice_router, MossTTSVoiceRouterBackend):
            voice_router.narrator_reference = (
                voice.references[0]
                if voice is not None and voice.references
                else self.settings.tts_speaker_wav
            )
            voice_router.prompt_audio_codes.pop("narrator", None)
        elif isinstance(voice_router, ChatterboxNanoVoiceRouterBackend):
            voice_router.narrator_reference = (
                voice.references[0]
                if voice is not None and voice.references
                else self.settings.tts_speaker_wav
            )
            voice_router.conditionals.pop("narrator", None)
        else:
            voice_router.narrator_voice = voice

    def _clear_voice_runtime_cache(self) -> None:
        clear_runtime_cache = getattr(self.voice_router, "clear_runtime_cache", None)
        if callable(clear_runtime_cache):
            clear_runtime_cache()
            return
        cache = getattr(self.voice_router, "audio_cache", None)
        clear = getattr(cache, "clear", None)
        if callable(clear):
            clear()

    def _warmup_progress(self, current: int, total: int, character: str) -> None:
        self.status_handler(f"Warming voice {current}/{total}: {character}")

    def _create_uncertain_frame_recorder(self) -> UncertainFrameRecorder | None:
        if not self.settings.retain_uncertain_frames:
            return None
        return UncertainFrameRecorder(self.settings.ocr_diagnostics_directory)

    def _dialog_observed(
        self, character: str | None, text: str
    ) -> DialogObservationDecision:
        if not text:
            with self.story_cursor_lock:
                if (
                    self.story_cursor is not None
                    and is_live_sequence_audio_mode(self.settings.live_sequence_mode)
                    and self.story_cursor.state
                    not in {
                        StoryCursorState.UNSYNCHRONIZED,
                        StoryCursorState.ANCHORING,
                    }
                ):
                    return False
            self.history.finish_current()
            self.dialog_handler("Narrator", "")
            return True
        character = self._canonical_observed_character(character, text)
        canonical_routing = False
        with self.story_cursor_lock:
            sequence_observation = self._observe_live_sequence(character, text)
            if self._live_sequence_audio_active():
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
                        return False
                    if self.story_cursor.state in {
                        StoryCursorState.LOCKED,
                        StoryCursorState.PLAYING,
                        StoryCursorState.WAITING_TRANSITION,
                        StoryCursorState.MANUAL,
                        StoryCursorState.DESYNCHRONIZED,
                    }:
                        return False
                else:
                    snapshot, observed_line, _match_result = sequence_observation
                    if snapshot.state == StoryCursorState.DESYNCHRONIZED:
                        return False
                    plan = self.live_sequence_plan
                    if plan is None:
                        return False
                    event = (
                        None
                        if snapshot.current_event_id is None
                        else plan.events.get(snapshot.current_event_id)
                    )
                    if (
                        event is not None
                        and event.kind == "silent"
                        and observed_line is None
                    ):
                        if not self._mark_sequence_silence_locked(event.event_id):
                            return False
                        return SilentDialogRoute(event.event_id)
                    line = self._canonical_sequence_line_locked(
                        None if event is None else event.event_id,
                        observed_line_id=(
                            None if observed_line is None else observed_line.line_id
                        ),
                    )
                    if (
                        event is None
                        or not event.is_speech
                        or line is None
                        or event.line_id != line.line_id
                    ):
                        if event is not None and event.is_speech and line is None:
                            self.story_cursor.desynchronize(
                                "canonical-line-integrity-failed"
                            )
                            self._publish_live_sequence_status()
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

    def _canonical_sequence_line_locked(
        self, event_id: str | None, *, observed_line_id: str | None = None
    ) -> ChapterDialogue | None:
        """Return the checksum-bound story payload owned by one plan event.

        OCR and visual tracking may identify an event, but they are never speech
        payload authorities. Every sequence speech route crosses this boundary
        and receives speaker/text from the story index selected by the plan's
        line identity.
        """
        plan = self.live_sequence_plan
        if plan is None or event_id is None:
            return None
        event = plan.events.get(str(event_id))
        if event is None or not event.is_speech or not event.line_id:
            return None
        if observed_line_id is not None and str(observed_line_id) != event.line_id:
            return None
        if plan.event_for_line(event.line_id) != event:
            return None
        line = self.chapter_voice_preloader.select_line_id(event.line_id)
        if line is None or not line.text_sha256:
            return None
        if sha256(line.text.encode("utf-8")).hexdigest() != line.text_sha256:
            return None
        return line

    def _mark_sequence_silence_locked(self, event_id: str) -> bool:
        cursor = self.story_cursor
        if cursor is None or cursor.current_event_id != event_id:
            return False
        event = cursor.current_event
        if event is None or event.kind != "silent":
            return False
        lease = SequenceEventLease(event.event_id, cursor.occurrence_id)
        existing = self.sequence_event_terminal_routes.setdefault(lease, "silence")
        if existing != "silence":
            cursor.desynchronize("conflicting-terminal-audio-route")
            self._publish_live_sequence_status()
            return False
        return True

    def _load_live_sequence_plan(self) -> bool:
        with self.story_cursor_lock:
            return self._load_live_sequence_plan_locked()

    def _load_live_sequence_plan_locked(self) -> bool:
        self.live_sequence_plan = None
        self.story_cursor = None
        self.explicit_sequence_anchor_pending = False
        self.sequence_prefix_confirmation_event_id = None
        with self.sequence_prefetch_lock:
            self.sequence_prefetch_keys.clear()
        self.sequence_event_terminal_routes.clear()
        self.sequence_advance_leases.clear()
        if self.settings.live_sequence_mode == "off":
            self._publish_live_sequence_status()
            return False
        if (
            self.settings.live_sequence_mode == "audio-auto"
            and not self.settings.live_sequence_plan
            and not self.settings.story_index
        ):
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
            if not _is_story_cursor(cursor):
                raise TypeError(
                    "Live sequence cursor factory returned an invalid cursor"
                )
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

    def _live_sequence_audio_active(self) -> bool:
        return bool(
            self.story_cursor is not None
            and is_live_sequence_audio_mode(self.settings.live_sequence_mode)
        )

    def get_live_sequence_status(self) -> LiveSequenceStatus:
        with self.story_cursor_lock:
            return self._get_live_sequence_status_locked()

    def _get_live_sequence_status_locked(self) -> LiveSequenceStatus:
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
            guidance = (
                "Make the in-game decision. Reading resumes automatically when the "
                "next dialogue appears."
            )
        elif (
            snapshot.state == StoryCursorState.WAITING_TRANSITION
            and cursor.deterministic_manual_successor() is not None
        ):
            guidance = (
                "The next planned event is a choice or manual boundary. Make the "
                "in-game decision; reading resumes when the next dialogue appears."
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
            story_title=(
                self.chapter_voice_preloader.story_title_for(
                    event.chapter, event.line_id
                )
                if event is not None
                else None
            ),
        )

    def _expected_sequence_audio_route(
        self, event: LiveSequenceEvent | None, line: ChapterDialogue | None
    ) -> str:
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
            and line.source_audio_authoritative
            and line.source_audio_completeness == "full"
            and line.source_audio_duration_seconds is not None
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

    def _publish_live_sequence_status(self) -> LiveSequenceStatus:
        status = self.get_live_sequence_status()
        try:
            self.sequence_status_handler(status)
        except Exception as error:
            self.error_handler(error)
        return status

    def _observe_live_sequence(
        self, character: str, text: str
    ) -> tuple[StoryCursorSnapshot, ChapterDialogue | None, object] | None:
        with self.story_cursor_lock:
            return self._observe_live_sequence_locked(character, text)

    def _observe_live_sequence_locked(
        self, character: str, text: str
    ) -> tuple[StoryCursorSnapshot, ChapterDialogue | None, object] | None:
        cursor = self.story_cursor
        plan = self.live_sequence_plan
        if cursor is None or plan is None or self.settings.live_sequence_mode == "off":
            return None
        previous_event_id = cursor.current_event_id
        confirming_dispatch = cursor.state == StoryCursorState.WAITING_TRANSITION
        candidate_events: tuple[LiveSequenceEvent, ...] = ()
        if previous_event_id is None or cursor.state in {
            StoryCursorState.UNSYNCHRONIZED,
            StoryCursorState.ANCHORING,
        }:
            line, match_result = self._resolve_initial_live_sequence_line(
                character,
                text,
            )
            snapshot = (
                None
                if line is None or line.line_id is None
                else cursor.observe_line(line.line_id)
            )
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
                    plan.events[event_id].line_id
                    for event_id in candidate_event_ids
                    if plan.events[event_id].line_id is not None
                )
                resolve_bounded = getattr(
                    self.chapter_voice_preloader,
                    "resolve_bounded_among",
                    self.chapter_voice_preloader.resolve_exact_among,
                )
                line, match_result = resolve_bounded(
                    character, text, candidate_line_ids
                )
                resolved_event = (
                    None
                    if line is None or line.line_id is None
                    else plan.event_for_line(line.line_id)
                )
                prefix_match = "prefix" in str(match_result)
                pending_prefix_continuation = bool(
                    prefix_match
                    and resolved_event is not None
                    and resolved_event.event_id == cursor.current_event_id
                    and self.sequence_prefix_confirmation_event_id
                    == resolved_event.event_id
                )
                if (
                    self.settings.live_sequence_mode == "audio-auto"
                    and line is not None
                    and prefix_match
                    and not pending_prefix_continuation
                    and (
                        cursor.state != StoryCursorState.WAITING_TRANSITION
                        or not self._reserve_generated_prefix(line)
                    )
                ):
                    line = None
                elif (
                    self.settings.live_sequence_mode == "audio-auto"
                    and line is not None
                ):
                    if prefix_match:
                        self.sequence_prefix_confirmation_event_id = (
                            None if resolved_event is None else resolved_event.event_id
                        )
                    elif (
                        match_result
                        in {
                            "expected-exact",
                            "expected-normalized-exact",
                            "expected-text-only",
                            "expected-bounded-ocr-suffix",
                            "expected-bounded-speaker-text",
                        }
                        and resolved_event is not None
                        and self.sequence_prefix_confirmation_event_id
                        == resolved_event.event_id
                    ):
                        self.sequence_prefix_confirmation_event_id = None
                        record_full_text = getattr(
                            self.live_reader,
                            "record_canonical_full_text",
                            None,
                        )
                        if callable(record_full_text) and line.line_id is not None:
                            record_full_text(line_id=line.line_id)
                snapshot = (
                    None
                    if line is None or line.line_id is None
                    else cursor.observe_bounded_line(line.line_id, candidate_event_ids)
                )
        if self._report_sequence_candidate_miss_if_needed(
            cursor,
            line,
            snapshot,
            previous_event_id,
            candidate_events,
            match_result,
        ):
            return None
        if snapshot is None:
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

    def _report_sequence_candidate_miss_if_needed(
        self,
        cursor: _StoryCursor,
        line: ChapterDialogue | None,
        snapshot: StoryCursorSnapshot | None,
        previous_event_id: str | None,
        candidate_events: Sequence[LiveSequenceEvent],
        match_result: object,
    ) -> bool:
        if line is None and snapshot is not None:
            return False
        if line is not None and line.line_id is not None:
            return False
        generation = self.live_reader.active_generation if self.live_reader else 0
        diagnostics = dict(self.chapter_voice_preloader.last_resolution_diagnostics)
        diagnostics.pop("match_result", None)
        self.pipeline_event_handler(
            "sequence-candidate-miss",
            generation,
            monotonic(),
            state=cursor.state.value,
            event_id=previous_event_id,
            candidate_event_ids=tuple(event.event_id for event in candidate_events),
            match_result=match_result,
            **diagnostics,
        )
        self._publish_live_sequence_status()
        return True

    def _reserve_generated_prefix(self, line: ChapterDialogue) -> bool:
        backend = self.speech_backend
        if (
            not isinstance(backend, GeneratedAudioFallbackBackend)
            or backend.library is None
        ):
            return False
        try:
            return bool(backend.reserve_generated_line_for_early_playback(line))
        except Exception as error:
            self.error_handler(error)
            return False

    def live_sequence_anchor_options(self) -> tuple[tuple[str, str], ...]:
        with self.story_cursor_lock:
            return self._live_sequence_anchor_options_locked()

    def _live_sequence_anchor_options_locked(self) -> tuple[tuple[str, str], ...]:
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

    def _expected_live_sequence_events(self) -> tuple[LiveSequenceEvent, ...]:
        with self.story_cursor_lock:
            return self._expected_live_sequence_events_locked()

    def _expected_live_sequence_events_locked(self) -> tuple[LiveSequenceEvent, ...]:
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

    def live_sequence_expected_options(self) -> tuple[tuple[str, str], ...]:
        with self.story_cursor_lock:
            return self._live_sequence_expected_options_locked()

    def _live_sequence_expected_options_locked(self) -> tuple[tuple[str, str], ...]:
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

    def select_expected_live_sequence_event(self, event_id: str) -> bool:
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
        event: LiveSequenceEvent,
        *,
        reason: str,
        pipeline_stage: str,
        success_message: str,
    ) -> bool:
        cursor = self.story_cursor
        if cursor is None:
            return False
        previous_event_id = cursor.current_event_id
        running = bool(self.live_reader is not None and self.live_reader.is_running)
        line = self._canonical_sequence_line_locked(event.event_id)
        if self._reject_missing_explicit_sequence_line(
            event,
            line,
            pipeline_stage,
            previous_event_id,
        ):
            return False
        if self._defer_explicit_sequence_voice_decision(
            event,
            line,
            running,
            pipeline_stage,
            previous_event_id,
        ):
            return False
        if running:
            self._clear_explicit_sequence_queue()
        cursor.anchor_event(event.event_id, reason)
        self.sequence_prefix_confirmation_event_id = None
        if line is None and not self._mark_sequence_silence_locked(event.event_id):
            return False
        if running:
            if not self._deliver_explicit_sequence_line(line):
                self._report_explicit_sequence_queue_failure(
                    cursor,
                    event,
                    pipeline_stage,
                    previous_event_id,
                )
                return False
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

    def _reject_missing_explicit_sequence_line(
        self,
        event: LiveSequenceEvent,
        line: ChapterDialogue | None,
        pipeline_stage: str,
        previous_event_id: str | None,
    ) -> bool:
        if not event.is_speech or line is not None:
            return False
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
        return True

    def _defer_explicit_sequence_voice_decision(
        self,
        event: LiveSequenceEvent,
        line: ChapterDialogue | None,
        running: bool,
        pipeline_stage: str,
        previous_event_id: str | None,
    ) -> bool:
        if (
            not running
            or line is None
            or not self._offer_unknown_speaker_mapping(line.speaker, line.text)
        ):
            return False
        self._report_explicit_live_sequence_outcome(
            pipeline_stage,
            previous_event_id,
            event,
            "voice-decision-deferred",
        )
        self._publish_live_sequence_status()
        return True

    def _enqueue_explicit_sequence_line(self, line: ChapterDialogue | None) -> bool:
        try:
            return True if line is None else self._enqueue_selected_sequence_line(line)
        except Exception as error:
            self.error_handler(error)
            return False

    def _clear_explicit_sequence_queue(self) -> None:
        reader = self.live_reader
        if reader is not None:
            reader.clear_queue()

    def _deliver_explicit_sequence_line(self, line: ChapterDialogue | None) -> bool:
        if not self._enqueue_explicit_sequence_line(line):
            return False
        reader = self.live_reader
        if reader is None:
            return False
        reader.bind_current_frame_route()
        if line is None:
            self.dialog_handler("Narrator", "Silent dialogue")
        return True

    def _report_explicit_sequence_queue_failure(
        self,
        cursor: _StoryCursor,
        event: LiveSequenceEvent,
        pipeline_stage: str,
        previous_event_id: str | None,
    ) -> None:
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

    def resync_live_sequence(self, event_id: str) -> bool:
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

    def _enqueue_selected_sequence_line(self, line: ChapterDialogue) -> bool:
        self._prime_observed_voice(line.speaker)
        self._prime_likely_chapter_voice(line.speaker, line.text)
        self.history.add(line.speaker, line.text)
        preview = line.text if len(line.text) <= 100 else f"{line.text[:97]}..."
        self.dialog_handler(line.speaker or "Narrator", preview)
        reader = self.live_reader
        if reader is None:
            return False
        return reader.enqueue(
            line.speaker,
            line.text,
            line_id=line.line_id,
        )

    def _report_explicit_live_sequence_outcome(
        self,
        stage: str,
        previous_event_id: str | None,
        event: LiveSequenceEvent,
        outcome: str,
    ) -> None:
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
        _fingerprint: object,
        settled: bool,
        expected_owner: str | None = None,
        route_epoch: int | None = None,
    ) -> bool | tuple[str, str] | CanonicalDialogRoute | SilentDialogRoute | None:
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
            if self.sequence_prefix_confirmation_event_id == cursor.current_event_id:
                return self._route_sequence_prefix_confirmation_locked(cursor)
            if not cursor.can_confirm_visual_transition:
                return False
            return self._route_visual_successor_locked(cursor)

    def _route_sequence_prefix_confirmation_locked(
        self, cursor: _StoryCursor
    ) -> tuple[str, str] | bool:
        completion_probe = getattr(
            self.live_reader,
            "current_frame_has_completion_cue",
            None,
        )
        if not callable(completion_probe) or not completion_probe():
            return False
        event = cursor.current_event
        line = self._canonical_sequence_line_locked(
            None if event is None else event.event_id
        )
        if line is None:
            return False
        self.sequence_prefix_confirmation_event_id = None
        record_full_text = getattr(
            self.live_reader,
            "record_canonical_full_text",
            None,
        )
        if callable(record_full_text):
            record_full_text(line_id=line.line_id)
        return line.speaker, line.text

    def _route_visual_successor_locked(
        self, cursor: _StoryCursor
    ) -> bool | tuple[str, str] | CanonicalDialogRoute | SilentDialogRoute | None:
        event = cursor.deterministic_visual_successor()
        application_owned_transition = bool(
            self.settings.live_sequence_mode == "audio-auto"
            and cursor.state == StoryCursorState.WAITING_TRANSITION
        )
        early_generated_line = self._early_generated_sequence_line(
            event,
            application_owned_transition,
        )
        if not self._has_unique_visible_successor(
            cursor,
            event,
            application_owned_transition,
            early_generated_line,
        ):
            return None
        return self._confirm_visual_sequence_successor_locked(
            cursor,
            event,
            application_owned_transition,
            early_generated_line,
        )

    def _early_generated_sequence_line(
        self, event: LiveSequenceEvent | None, application_owned_transition: bool
    ) -> ChapterDialogue | None:
        if (
            not application_owned_transition
            or event is None
            or not event.is_speech
            or event.line_id is None
        ):
            return None
        line = self._canonical_sequence_line_locked(event.event_id)
        return (
            line if line is not None and self._reserve_generated_prefix(line) else None
        )

    def _has_unique_visible_successor(
        self,
        cursor: _StoryCursor,
        event: LiveSequenceEvent | None,
        application_owned_transition: bool,
        early_generated_line: ChapterDialogue | None,
    ) -> bool:
        immediate = bool(
            application_owned_transition
            and event is not None
            and (event.kind == "silent" or early_generated_line is not None)
        )
        candidates = cursor.bounded_visible_successors(
            maximum_visible_depth=1 if immediate else 3
        )
        return (
            event is not None
            and len(candidates) == 1
            and candidates[0].event_id == event.event_id
        )

    def _confirm_visual_sequence_successor_locked(
        self,
        cursor: _StoryCursor,
        event: LiveSequenceEvent | None,
        application_owned_transition: bool,
        early_generated_line: ChapterDialogue | None,
    ) -> bool | tuple[str, str] | CanonicalDialogRoute | SilentDialogRoute:
        if event is None:
            return False
        previous_event_id = cursor.current_event_id
        confirming_dispatch = cursor.state == StoryCursorState.WAITING_TRANSITION
        confirmed_event = cursor.confirm_visual_transition()
        if confirmed_event is None or confirmed_event.event_id != event.event_id:
            return False
        if confirming_dispatch and self.live_reader is not None:
            self.live_reader.confirm_pending_auto_advance()
        generation = self.live_reader.active_generation if self.live_reader else 0
        if event.kind == "silent":
            return self._confirm_silent_visual_successor_locked(
                cursor,
                event,
                previous_event_id,
                generation,
                application_owned_transition,
            )
        return self._confirm_spoken_visual_successor_locked(
            cursor,
            event,
            previous_event_id,
            generation,
            early_generated_line,
        )

    def _confirm_silent_visual_successor_locked(
        self,
        cursor: _StoryCursor,
        event: LiveSequenceEvent,
        previous_event_id: str | None,
        generation: int,
        application_owned_transition: bool,
    ) -> SilentDialogRoute | bool:
        if not self._mark_sequence_silence_locked(event.event_id):
            return False
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
            match_result="expected-silent-ellipsis",
            proof=(
                "application-owned-single-dispatch"
                if application_owned_transition
                else "unique-bounded-visible-successor"
            ),
        )
        self._publish_live_sequence_status()
        return SilentDialogRoute(event.event_id)

    def _confirm_spoken_visual_successor_locked(
        self,
        cursor: _StoryCursor,
        event: LiveSequenceEvent,
        previous_event_id: str | None,
        generation: int,
        early_generated_line: ChapterDialogue | None,
    ) -> bool | tuple[str, str] | CanonicalDialogRoute:
        line = self._canonical_sequence_line_locked(event.event_id)
        if line is None:
            cursor.desynchronize(f"missing-story-line:{event.line_id}")
            self.status_handler(
                "Sequence-first routing stopped: the expected story line is missing"
            )
            self._publish_live_sequence_status()
            return False
        if early_generated_line is not None:
            self.sequence_prefix_confirmation_event_id = event.event_id
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
            proof=(
                "application-owned-single-dispatch-generated-preflight"
                if early_generated_line is not None
                else "unique-bounded-visible-successor"
            ),
        )
        self._publish_live_sequence_status()
        if early_generated_line is not None:
            return CanonicalDialogRoute(line.speaker, line.text)
        return line.speaker, line.text

    def _stable_live_frame_owner(self) -> str | None:
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if cursor is None or not is_live_sequence_audio_mode(
                self.settings.live_sequence_mode
            ):
                return None
            return cursor.current_event_id

    def _live_ocr_purpose(self) -> str | None:
        """Authorize full OCR only where the cursor cannot route safely."""
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if not self._live_sequence_audio_active():
                return "legacy"
            if cursor is None:
                return None
            if cursor.state in {
                StoryCursorState.UNSYNCHRONIZED,
                StoryCursorState.ANCHORING,
            }:
                return "initial-anchor"
            if cursor.state in {
                StoryCursorState.MANUAL,
                StoryCursorState.DESYNCHRONIZED,
            }:
                return "explicit-recovery"
            if cursor.state == StoryCursorState.PLAYING:
                return None
            if self.sequence_prefix_confirmation_event_id == cursor.current_event_id:
                return None
            if not cursor.can_confirm_visual_transition:
                return None
            candidates = cursor.bounded_visible_successors()
            deterministic = cursor.deterministic_visual_successor()
            if candidates and (
                deterministic is None
                or len(candidates) != 1
                or candidates[0].event_id != deterministic.event_id
            ):
                return "bounded-branch-disambiguation"
            return None

    def _sequence_prefix_recheck_required(self) -> bool:
        with self.story_cursor_lock:
            return bool(
                self.story_cursor is not None
                and is_live_sequence_audio_mode(self.settings.live_sequence_mode)
                and self.sequence_prefix_confirmation_event_id is not None
                and self.sequence_prefix_confirmation_event_id
                == self.story_cursor.current_event_id
            )

    def _confirm_sequence_render_completion(self) -> bool:
        """Close only the current prefix barrier from owner-bound render quiet."""
        with self.story_cursor_lock:
            cursor = self.story_cursor
            reader = self.live_reader
            if (
                cursor is None
                or reader is None
                or self.settings.live_sequence_mode != "audio-auto"
                or cursor.state != StoryCursorState.LOCKED
                or self.sequence_prefix_confirmation_event_id != cursor.current_event_id
            ):
                return False
            quiet_probe = getattr(reader, "current_frame_render_quiet_ms", None)
            quiet_ms = (
                quiet_probe(expected_owner=cursor.current_event_id)
                if callable(quiet_probe)
                else None
            )
            if quiet_ms is None:
                return False
            event = cursor.current_event
            line = self._canonical_sequence_line_locked(
                None if event is None else event.event_id
            )
            bind_frame = getattr(reader, "bind_current_frame_route", None)
            if line is None or not callable(bind_frame) or not bind_frame():
                return False
            self.sequence_prefix_confirmation_event_id = None
            record_full_text = getattr(reader, "record_canonical_full_text", None)
            if callable(record_full_text):
                record_full_text(
                    line_id=line.line_id,
                    reason="owner-render-quiet",
                    settled_ms=quiet_ms,
                )
            return True

    def _live_sequence_line_id(self, character: str, text: str) -> str | None:
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
            line = self._canonical_sequence_line_locked(event.event_id)
            if line is None or (line.speaker, line.text) != (character, text):
                return None
            return str(line.line_id)

    def _begin_sequence_playback(self, chunk: SpeechChunk) -> SequenceEventLease | None:
        successor = None
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
            line = self._canonical_sequence_line_locked(event.event_id)
            if line is None or (line.speaker, line.text) != (
                chunk.character,
                chunk.text,
            ):
                return None
            lease = SequenceEventLease(event.event_id, cursor.occurrence_id)
            if lease in self.sequence_event_terminal_routes:
                return None
            try:
                cursor.begin_playback()
            except StoryCursorError:
                return None
            candidate = cursor.deterministic_upcoming_visible_event()
            if candidate is not None and candidate.is_speech and candidate.line_id:
                successor = self._canonical_sequence_line_locked(candidate.event_id)
            self._publish_live_sequence_status()
        self._schedule_sequence_successor_prefetch(lease, successor)
        return lease

    def _schedule_sequence_successor_prefetch(
        self, owner_lease: SequenceEventLease, line: ChapterDialogue | None
    ) -> bool:
        backend = self.speech_backend
        executor = self.speech_executor
        live_backend = (
            backend.live_backend
            if isinstance(backend, GeneratedAudioFallbackBackend)
            else backend
        )
        can_generate = callable(getattr(live_backend, "materialize_prepared", None))
        can_reserve = bool(
            isinstance(backend, GeneratedAudioFallbackBackend)
            and backend.library is not None
        )
        if (
            line is None
            or executor is None
            or not isinstance(owner_lease, SequenceEventLease)
            or not (can_generate or can_reserve)
            or not line.line_id
            or not line.text_sha256
        ):
            return False
        key = (id(backend), owner_lease, line.line_id, line.text_sha256)
        with self.sequence_prefetch_lock:
            if key in self.sequence_prefetch_keys:
                return False
            self.sequence_prefetch_keys.add(key)
        try:
            executor.submit(
                self._prefetch_sequence_successor,
                key,
                backend,
                owner_lease,
                line,
            )
        except RuntimeError:
            with self.sequence_prefetch_lock:
                self.sequence_prefetch_keys.discard(key)
            return False
        return True

    def _prefetch_sequence_successor(
        self,
        key: tuple[object, ...],
        backend: SpeechBackend | GeneratedAudioFallbackBackend,
        owner_lease: SequenceEventLease,
        line: ChapterDialogue,
    ) -> str:
        started_at = monotonic()
        settings = self.settings
        with self.story_cursor_lock:
            cursor = self.story_cursor
            authorized = bool(
                backend is self.speech_backend
                and settings is self.settings
                and cursor is not None
                and cursor.current_event_id == owner_lease.event_id
                and cursor.occurrence_id == owner_lease.occurrence_id
                and self.game_focused
                and is_live_sequence_audio_mode(self.settings.live_sequence_mode)
            )
        if not authorized:
            outcome = "stale"
        else:
            try:
                outcome = "unavailable"
                if isinstance(backend, GeneratedAudioFallbackBackend):
                    if backend.reserve_generated_line_for_early_playback(line):
                        outcome = "reserved"
                    else:
                        route = backend.prepare_route(
                            line.speaker, line.text, line_id=line.line_id
                        )
                        materialize = getattr(
                            backend.live_backend, "materialize_prepared", None
                        )
                        if isinstance(
                            route, (LiveFallbackRoute, LiveTTSRoute)
                        ) and callable(materialize):
                            materialized = materialize(
                                route.prepared,
                                cancellation=lambda: not self.game_focused,
                            )
                            outcome = (
                                "prepared"
                                if materialized.generation_completed
                                else "stale"
                            )
                else:
                    materialize = getattr(backend, "materialize_prepared", None)
                    if not callable(materialize):
                        raise TypeError(
                            "Live backend cannot materialize prepared audio"
                        )
                    materialized = materialize(
                        backend.prepare_playback(line.speaker, line.text),
                        cancellation=lambda: not self.game_focused,
                    )
                    outcome = (
                        "prepared" if materialized.generation_completed else "stale"
                    )
            except Exception as error:
                outcome = "failed"
                self.error_handler(error)
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if outcome in {"reserved", "prepared"} and (
                backend is not self.speech_backend
                or settings is not self.settings
                or cursor is None
                or cursor.current_event_id != owner_lease.event_id
                or cursor.occurrence_id != owner_lease.occurrence_id
                or not self.game_focused
            ):
                outcome = "stale"
            generation = (
                self.live_reader.active_generation
                if self.live_reader is not None
                else 0
            )
            plan = self.live_sequence_plan
            target = (
                None
                if plan is None or line.line_id is None
                else plan.event_for_line(line.line_id)
            )
        try:
            self.pipeline_event_handler(
                "sequence-successor-prefetch",
                generation,
                monotonic(),
                event_id=owner_lease.event_id,
                target_event_id=None if target is None else target.event_id,
                line_id=line.line_id,
                outcome=outcome,
                prefetch_ms=round((monotonic() - started_at) * 1000),
            )
        except Exception as error:
            self.error_handler(error)
        with self.sequence_prefetch_lock:
            self.sequence_prefetch_keys.discard(key)
        return outcome

    def _materialize_live_route(self, route: object, chunk: SpeechChunk) -> object:
        if not self._live_sequence_audio_active():
            return route
        if isinstance(route, LiveFallbackRoute):
            backend = self.speech_backend
            if isinstance(backend, GeneratedAudioFallbackBackend):
                backend = backend.live_backend
            prepared = route.prepared
        elif isinstance(route, LiveTTSRoute):
            backend = self.speech_backend
            prepared = route.prepared
        elif isinstance(route, PreparedPlayback):
            backend = self.speech_backend
            prepared = route
        else:
            return route
        materialize = getattr(backend, "materialize_prepared", None)
        if not callable(materialize):
            return route
        prepared = materialize(
            prepared,
            cancellation=lambda: (
                self.live_reader is not None
                and not self.live_reader.wait_until_playable(chunk)
            ),
        )
        if isinstance(route, (LiveFallbackRoute, LiveTTSRoute)):
            return replace(
                route,
                prepared=prepared,
                synthesis_ms=prepared.synthesis_ms,
                first_audio_ms=prepared.first_audio_ms,
                cache_source=prepared.cache_source,
            )
        return prepared

    def _finish_sequence_playback(
        self, lease: SequenceEventLease | None, outcome: PlaybackOutcome | None
    ) -> bool:
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if (
                not isinstance(lease, SequenceEventLease)
                or cursor is None
                or cursor.current_event_id != lease.event_id
                or cursor.occurrence_id != lease.occurrence_id
                or cursor.state != StoryCursorState.PLAYING
            ):
                return False
            successful = isinstance(outcome, PlaybackOutcome) and outcome.successful
            audible = bool(
                isinstance(outcome, PlaybackOutcome)
                and isinstance(outcome.first_audio_ms, (int, float))
                and not isinstance(outcome.first_audio_ms, bool)
            )
            if (successful or audible) and isinstance(outcome, PlaybackOutcome):
                route = str(outcome.audio_source or "unknown")
                existing = self.sequence_event_terminal_routes.setdefault(
                    lease,
                    route,
                )
                if existing != route:
                    cursor.desynchronize("conflicting-terminal-audio-route")
                    self._publish_live_sequence_status()
                    return False
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
                event_id=lease.event_id,
                line_id=cursor.snapshot().current_line_id,
                occurrence_id=lease.occurrence_id,
                terminal_route=(
                    self.sequence_event_terminal_routes.get(lease)
                    if successful or audible
                    else None
                ),
                outcome="completed" if successful else "failed",
            )
            self._publish_live_sequence_status()
            return successful

    def _canonical_observed_character(
        self, character: str | None, text: str | None = None
    ) -> str:
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
            canonical = registry.resolve_closest_character(
                original, minimum_similarity=0.86
            )
            if isinstance(canonical, str):
                return canonical

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

    def _offer_unknown_speaker_mapping(
        self, character: str, text: str | None = None
    ) -> bool:
        if not self._speaker_requires_voice_decision(character, text):
            return False
        key = normalize_character_name(character)
        if key in self.pending_unknown_speakers:
            return False
        if key in self.reported_unknown_speakers:
            return False
        self.reported_unknown_speakers.add(key)
        self.pending_unknown_speakers.add(key)
        self.status_handler(
            f"Using the narrator for {character.strip()}; assign another voice "
            "later if needed"
        )
        self.unknown_speaker_handler(character.strip())
        return False

    def _speaker_requires_voice_decision(
        self,
        character: str,
        text: str | None = None,
        *,
        live_preflight: bool = False,
    ) -> bool:
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

    def _prime_observed_voice(self, character: str) -> bool:
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

    def _prime_likely_chapter_voice(self, character: str, text: str) -> bool:
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

    def _voice_prime_finished(self, future: _ExecutorFuture) -> None:
        with self.voice_prime_lock:
            self.voice_prime_futures.discard(future)
        if future.cancelled():
            return
        try:
            future.result()
        except Exception as error:
            self.error_handler(error)

    def _enqueue_dialog(self, character: str, text: str) -> bool:
        canonical_character = self._canonical_observed_character(character, text)
        resolved_text = self._resolve_early_indexed_dialogue(canonical_character, text)
        if resolved_text is not None:
            text = resolved_text
        decision = self._dialog_observed(canonical_character, text)
        if decision is False:
            return False
        if isinstance(decision, SilentDialogRoute):
            return True
        if isinstance(decision, tuple) and len(decision) == 2:
            routed_character, routed_text = decision
        else:
            routed_character, routed_text = canonical_character, text
        reader = self.live_reader
        if reader is None:
            return False
        if routed_character is None:
            routed_character = canonical_character
        return reader.enqueue(routed_character, routed_text)

    def _ocr_uncertain(self, result: OCRResult, minimum_confidence: float) -> None:
        if self.live_reader is not None:
            self.live_reader.clear_queue()
        preview = result.text if len(result.text) <= 80 else f"{result.text[:77]}..."
        self.dialog_handler(
            "OCR uncertain",
            f"{result.confidence:.0f}% (requires {minimum_confidence}%): {preview}",
        )

    def _resolve_voice_label(self, character: str) -> str:
        return str(resolve_voice_label(self.voice_router, character))

    def _is_game_focused(self) -> bool:
        return self.capture_target is not None and self.capture_target.is_focused()

    def _live_auto_advance_callback(self) -> Callable[[], object] | None:
        if not self.settings.auto_advance_enabled or not auto_advance_allowed(
            self.settings.capture_mode,
            self.settings.live_sequence_mode,
        ):
            return None
        if self.settings.live_sequence_mode == "audio-auto":
            if not self._live_sequence_audio_active():
                return None
            return self._sequence_auto_advance_dialog
        return self._auto_advance_dialog

    def _sequence_auto_advance_dialog(self) -> AutoAdvanceAttempt:
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if (
                cursor is None
                or self.settings.live_sequence_mode != "audio-auto"
                or not cursor.can_auto_advance
            ):
                return AutoAdvanceAttempt(False, "cursor-not-auto-advance-eligible")
            event = cursor.current_event
            lease = (
                None
                if event is None
                else SequenceEventLease(event.event_id, cursor.occurrence_id)
            )
            if lease not in self.sequence_event_terminal_routes:
                return AutoAdvanceAttempt(False, "event-route-not-terminal")
            if lease in self.sequence_advance_leases:
                return AutoAdvanceAttempt(False, "event-advance-already-dispatched")
            if self.sequence_prefix_confirmation_event_id == cursor.current_event_id:
                return AutoAdvanceAttempt(False, "visual-wait")
            if not self._is_game_focused():
                return AutoAdvanceAttempt(False, "focus-wait")
            advanced = self._auto_advance_dialog(focus_verified=True)
            if advanced is False:
                return AutoAdvanceAttempt(False, "dispatch-disabled")
            self.sequence_advance_leases.add(lease)
            try:
                snapshot = cursor.dispatch_advance()
            except StoryCursorError:
                cursor.desynchronize("advance-dispatched-without-cursor-transition")
                self._publish_live_sequence_status()
                return AutoAdvanceAttempt(False, "cursor-dispatch-failed")
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
                occurrence_id=lease.occurrence_id,
                next_event_count=len(snapshot.expected_successor_ids),
            )
            self._publish_live_sequence_status()
            return AutoAdvanceAttempt(True, "dispatched")

    def _auto_advance_dialog(self, *, focus_verified: bool = False) -> bool:
        if (
            not self.settings.auto_advance_enabled
            or not auto_advance_allowed(
                self.settings.capture_mode,
                self.settings.live_sequence_mode,
            )
            or (not focus_verified and not self._is_game_focused())
        ):
            return False
        DialogueAdvancer(self.settings.auto_advance_key).advance()
        return True

    def _auto_advance_state_changed(
        self, state: str, _generation: int, _attempt: object
    ) -> None:
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
                "Auto advance is waiting for the current line to finish rendering"
            )
        elif state == "blocked":
            self.status_handler(
                "Auto advance was blocked because the current cursor event no longer "
                "owns one safe automatic transition. Resynchronize manually."
            )
        elif state == "dispatched":
            self.status_handler(
                "Auto advance key sent; a choice/manual boundary is next. Make the "
                "in-game decision; reading resumes with the next dialogue."
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
                "key was sent. Make the decision; reading is still watching for the "
                "next dialogue."
                if awaiting_manual_boundary
                else "Dialogue change was not confirmed after the extended wait; no "
                "second key was sent. Advance manually."
            )
        elif state == "confirmed":
            self.status_handler("Auto advance confirmed by new dialogue")

    def _capture_state_changed(self, focused: bool, interval_seconds: float) -> None:
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

    def _publish_diagnostic(
        self,
        snapshot: DiagnosticSnapshot,
        route_metrics: _DiagnosticRouteMetrics | None = None,
        audio_source: str | None = None,
        *,
        notify: bool = True,
    ) -> DiagnosticSnapshot:
        reader = self.live_reader
        pipeline_metrics = None if reader is None else reader.get_pipeline_metrics()
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
        if notify:
            self.diagnostic_handler(snapshot)
        return snapshot

    def _prepare_live_chunk(self, chunk: SpeechChunk) -> object:
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
            prepared = self._materialize_live_route(prepared, chunk)
            try:
                announcement, announced_speaker = self._prepare_speaker_announcement(
                    chunk, prepared
                )
                if announcement is not None:
                    materialized_announcement = self._materialize_live_route(
                        announcement,
                        chunk,
                    )
                    if not isinstance(materialized_announcement, LiveTTSRoute):
                        raise TypeError("Speaker announcement route was not preserved")
                    announcement = materialized_announcement
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
                _diagnostic_route_metrics(prepared),
                self._describe_audio_source(prepared) if prepared is not None else None,
            )

    def _play_live_chunk(self, chunk: SpeechChunk, audio: object) -> bool:
        reader = self.live_reader
        if reader is None:
            return False
        sequence_lease = (
            None if chunk.explicit_replay else self._begin_sequence_playback(chunk)
        )
        if (
            not chunk.explicit_replay
            and self._live_sequence_audio_active()
            and chunk.line_id is not None
            and sequence_lease is None
        ):
            generation = (
                reader.active_generation if reader is not None else chunk.generation
            )
            self.pipeline_event_handler(
                "sequence-playback-suppressed",
                generation,
                monotonic(),
                line_id=chunk.line_id,
                reason="cursor-does-not-own-unplayed-line",
                outcome="suppressed",
            )
            self.status_handler(
                f"Duplicate or stale canonical audio suppressed: {chunk.line_id}"
            )
            return False
        if isinstance(audio, PreparedLiveChunkRoutes):
            if (
                audio.speaker_announcement is not None
                and audio.announced_speaker is not None
            ):
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
        source_audio_lead_seconds = float(
            getattr(audio, "source_audio_lead_seconds", 0.0) or 0.0
        )
        source_audio_lead_ms = (
            round(source_audio_lead_seconds * 1000)
            if source_audio_lead_seconds > 0
            else None
        )
        self.last_audio_source_description = source
        self.status_handler(f"Audio source for {chunk.character}: {source}")
        if not chunk.explicit_replay and (
            (
                isinstance(audio, SourceAudioRoute)
                and audio.prepared.completion_seconds is None
            )
            or (
                isinstance(audio, PreparedSourceAudioPassThrough)
                and audio.completion_seconds is None
            )
        ):
            source_audio = (
                audio.prepared if isinstance(audio, SourceAudioRoute) else audio
            )
            reason = (
                f"Auto advance paused for original game audio line {source_audio.line_id}: "
                "completion timing is unavailable. Wait for the original game voice "
                "to finish, then advance manually in the game."
            )
            if reader.block_auto_advance_for_generation(
                chunk.generation,
                reason,
            ):
                self.status_handler(reason)
        playback_started = monotonic()
        outcome = None
        context_token = audio_lifecycle_context.set(
            {
                "session_id": self.live_reader_session_id,
                "generation": chunk.generation,
                "chunk_id": chunk.chunk_id,
            }
        )
        try:
            play_route = getattr(type(self.speech_backend), "play_route", None)
            play_prepared = getattr(type(self.speech_backend), "play_prepared", None)
            outcome = (
                play_route(
                    self.speech_backend,
                    audio,
                    playback_guard=lambda: reader.wait_until_playable(chunk),
                )
                if callable(play_route) and isinstance(audio, RouteDecision)
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
                    playback_guard=lambda: reader.wait_until_playable(chunk),
                )
            if outcome is None:
                raise TypeError("Speech backend does not implement typed playback")
            result = outcome.successful
            audible = isinstance(
                outcome.first_audio_ms, (int, float)
            ) and not isinstance(outcome.first_audio_ms, bool)
            if not result and not chunk.explicit_replay:
                reader.block_auto_advance_for_generation(
                    chunk.generation,
                    "Playback was interrupted; retry or wait for a new dialogue",
                )
            if (
                (result or audible)
                and not chunk.explicit_replay
                and (
                    self._live_sequence_audio_active()
                    or isinstance(
                        audio,
                        (
                            GeneratedAudioRoute,
                            PendingGeneratedAudioRoute,
                            SourceAudioRoute,
                            PreparedGeneratedAudio,
                            PreparedSourceAudioPassThrough,
                            AudioEventOmissionRoute,
                        ),
                    )
                )
            ):
                reader.seal_generation(chunk.generation)
            underflowed = outcome.underflowed
            generation_limited = outcome.generation_limited
            playback_telemetry = {
                "outcome": outcome.status.value,
                "underflowed": underflowed,
                "generation_limited": generation_limited,
                "synthesis_ms": outcome.synthesis_ms,
                "playback_ms": outcome.playback_ms,
                "first_audio_ms": outcome.first_audio_ms,
                "cache_source": outcome.cache_source,
                "effective_source": outcome.audio_source,
                "source_audio_lead_ms": source_audio_lead_ms,
                "chunk_id": chunk.chunk_id,
                "chunk_ordinal": chunk.ordinal,
                "chunk_characters": len(chunk.text),
            }
            try:
                self.pipeline_event_handler(
                    "playback-completion",
                    chunk.generation,
                    monotonic(),
                    **playback_telemetry,
                    source_sample_rate=outcome.source_sample_rate,
                    playback_sample_rate=outcome.playback_sample_rate,
                    sample_count=outcome.sample_count,
                    expected_playback_ms=outcome.expected_playback_ms,
                )
                self.pipeline_event_handler(
                    "playback-outcome",
                    chunk.generation,
                    monotonic(),
                    **playback_telemetry,
                )
            except Exception as error:
                self.error_handler(error)
            if outcome.status is PlaybackStatus.FAILED:
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
                reader.record_first_pcm(playback_started + first_audio_ms / 1000)
            return bool(result)
        finally:
            audio_lifecycle_context.reset(context_token)
            self._finish_sequence_playback(sequence_lease, outcome)
            self._refresh_diagnostic_metrics(outcome, source)

    def _prepare_speaker_announcement(
        self, chunk: SpeechChunk, dialogue_route: object
    ) -> tuple[LiveTTSRoute | None, str | None]:
        mode = self.settings.effective_speaker_announcement_mode
        if mode == "off" or chunk.ordinal not in {None, 1}:
            return None, None
        visible_speaker = str(chunk.character or "Narrator").strip() or "Narrator"
        announcement_speaker = self._speaker_announcement_candidate(
            mode, visible_speaker, dialogue_route
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
                (
                    SourceAudioRoute,
                    PreparedSourceAudioPassThrough,
                    AudioEventOmissionRoute,
                ),
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
            self.pipeline_event_handler(
                "speaker-announcement-route",
                chunk.generation,
                monotonic(),
                **trace.support_details(),
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

    def _speaker_announcement_candidate(
        self, mode: str, visible_speaker: str, dialogue_route: object
    ) -> str | None:
        if mode == "narrator-fallback-roles":
            if isinstance(dialogue_route, GeneratedAudioRoute):
                return dialogue_route.prepared.narrator_fallback_role
            if isinstance(dialogue_route, LiveFallbackRoute):
                return (
                    "Unknown"
                    if is_unattributed_speaker(visible_speaker)
                    else dialogue_route.decision.requested_voice_character
                )
            if isinstance(dialogue_route, (LiveTTSRoute, PreparedPlayback)):
                return (
                    "Unknown"
                    if is_unattributed_speaker(visible_speaker)
                    else visible_speaker
                    if not is_narrator(visible_speaker)
                    and self.voice_router is not None
                    and self.voice_router.registry.resolve(visible_speaker) is None
                    else None
                )
            return None
        return (
            "Unknown" if is_unattributed_speaker(visible_speaker) else visible_speaker
        )

    def _play_speaker_announcement(
        self, chunk: SpeechChunk, route: LiveTTSRoute, announced_speaker: str
    ) -> PlaybackOutcome:
        speaker_key = normalize_character_name(announced_speaker) or "unknown"
        with self.speaker_announcement_lock:
            if speaker_key == self.last_visible_speaker_key:
                return PlaybackOutcome(
                    PlaybackStatus.PASSTHROUGH_UNOBSERVED,
                    0.0,
                    audio_source="live-accessibility-announcement",
                )
        backend = self.speech_backend
        play_route = getattr(type(backend), "play_route", None)
        live_backend = getattr(backend, "live_backend", backend)
        play_prepared = getattr(type(live_backend), "play_prepared", None)
        if not callable(play_route) and not callable(play_prepared):
            raise TypeError("Live backend cannot play a typed speaker announcement")
        self.status_handler(f"Announcing speaker: {announced_speaker}")
        reader = self.live_reader
        if reader is None:
            raise RuntimeError("The speech engine is not ready")
        context_token = audio_lifecycle_context.set(
            {
                "session_id": self.live_reader_session_id,
                "generation": chunk.generation,
                "chunk_id": route.trace.chunk_id,
            }
        )
        try:
            if callable(play_route):
                outcome = play_route(
                    backend,
                    route,
                    playback_guard=lambda: reader.wait_until_playable(chunk),
                )
            else:
                assert callable(play_prepared)
                outcome = play_prepared(
                    live_backend,
                    route.prepared,
                    playback_guard=lambda: reader.wait_until_playable(chunk),
                )
        finally:
            audio_lifecycle_context.reset(context_token)
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
        if outcome.status is PlaybackStatus.COMPLETED:
            with self.speaker_announcement_lock:
                self.last_visible_speaker_key = speaker_key
        elif outcome.status is PlaybackStatus.FAILED:
            self.error_handler(
                AudioPlaybackError(
                    outcome.error or "Speaker announcement playback failed"
                )
            )
        return outcome

    def _observe_live_playback_backpressure(self, underflowed: bool) -> None:
        jobs, changed = self.live_speech_backpressure.observe_playback(
            underflowed=underflowed,
        )
        reader = self.live_reader
        if reader is None:
            return
        reader.max_speech_jobs = jobs
        if changed:
            self.status_handler(
                "Audio underrun detected; live speech prefetch disabled temporarily"
                if underflowed
                else "Audio playback stable; live speech prefetch restored"
            )

    def _describe_audio_source(self, prepared: object) -> str:
        if isinstance(prepared, AudioEventOmissionRoute):
            return "Authorized silent audio event omission"
        if isinstance(prepared, PendingGeneratedAudioRoute):
            return f"Waiting for prepared audio (line {prepared.line_id})"
        lead_seconds = float(getattr(prepared, "source_audio_lead_seconds", 0.0) or 0.0)
        if lead_seconds > 0 and isinstance(
            prepared,
            (GeneratedAudioRoute, LiveFallbackRoute, LiveTTSRoute),
        ):
            following = self._describe_audio_source(
                replace(prepared, source_audio_lead_seconds=0.0)
            )
            return (
                f"Original game cue ({lead_seconds:.2f}s including post-roll), "
                f"then {following}"
            )
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
            identity = prepared.recorded_voice
            source = (
                f"source voice: {identity['source_character']}"
                + (
                    f"; voice ID: {identity['speaker']}"
                    if identity["speaker"] != identity["source_character"]
                    else ""
                )
                if identity is not None
                else "source voice: unknown (not recorded)"
            )
            return (
                f"Generated audio (line {prepared.line_id})\n"
                f"Recorded with: {prepared.provider or 'engine not recorded'}; "
                f"model: {prepared.model or 'not recorded'}; "
                f"voice role: {prepared.voice_character or 'not recorded'}; {source}"
            )
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

    def _record_pipeline_route(
        self, chunk: SpeechChunk, trace: AudioRouteTrace
    ) -> None:
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

    def _build_audio_route_trace(
        self, chunk: SpeechChunk, prepared: object
    ) -> AudioRouteTrace:
        route = prepared.trace if isinstance(prepared, RouteDecision) else None
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
            None
            if isinstance(
                prepared, (PendingGeneratedAudioRoute, AudioEventOmissionRoute)
            )
            else prepared.prepared
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
            if prepared_payload is None
            or isinstance(
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

    def _resolve_trace_line(
        self, chunk: SpeechChunk
    ) -> tuple[ChapterDialogue | None, str]:
        resolve = getattr(
            self.chapter_voice_preloader, "resolve_exact_with_result", None
        )
        if callable(resolve):
            resolved = resolve(chunk.character, chunk.text)
            if (
                isinstance(resolved, tuple)
                and len(resolved) == 2
                and (resolved[0] is None or isinstance(resolved[0], ChapterDialogue))
                and isinstance(resolved[1], str)
            ):
                return resolved[0], resolved[1]
        line = self.chapter_voice_preloader.resolve_exact(
            chunk.character,
            chunk.text,
        )
        return line, "exact" if line is not None else "no-match"

    def _voice_reference_identifier(
        self, character: str, prepared: object
    ) -> str | None:
        voice_key = str(getattr(prepared, "voice_key", "")).strip()
        registry = getattr(self.voice_router, "registry", None)
        voice = registry.resolve(character) if registry is not None else None
        if voice is not None and voice.references:
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
            and getattr(live_backend, "narrator_reference", None)
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

    def _refresh_diagnostic_metrics(
        self,
        route_metrics: _DiagnosticRouteMetrics | None = None,
        audio_source: str | None = None,
    ) -> None:
        with self.diagnostic_lock:
            snapshot = self.last_diagnostic
        if snapshot is not None:
            self._publish_diagnostic(snapshot, route_metrics, audio_source)

    def _stop_tts(self) -> None:
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

    def _interrupt_speech(self) -> bool | None:
        if self.tts is not None and hasattr(self.tts, "stop"):
            return bool(self.tts.stop())
        return False
