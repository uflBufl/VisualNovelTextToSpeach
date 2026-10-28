"""Application controller and live-reading orchestration."""

import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, nullcontext
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from functools import partial
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock, RLock
from time import monotonic
from typing import Protocol, TypeGuard, runtime_checkable

from vntts.audio_lifecycle import audio_lifecycle_context
from vntts.audio_output import PcmAdmissionRejected
from vntts.auto_advance import DialogueAdvancer
from vntts.auto_advance_policy import auto_advance_allowed
from vntts.chapter_voice_preload import (
    ChapterDialogue,
    ChapterVoicePreloader,
)
from vntts.chapter_voice_preload import _normalize as _normalize_story_text
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
from vntts.dialog import speak_dialog
from vntts.dialog_capture import (
    CapturedDialogFrame,
    DiagnosticSnapshot,
    OCRUncertainError,
    capture_live_frame,
    dialog_glyphs_visible,
    fingerprint_dialog_frame,
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
    LivePipelineMetrics,
    RecognizedDialog,
    SilentDialogRoute,
    SpeechChunk,
)
from vntts.live_sequence import (
    LiveSequenceEvent,
    StoryCursor,
    StoryCursorError,
    StoryCursorSnapshot,
    StoryCursorState,
    load_live_sequence_plan,
)
from vntts.live_snapshot import read_live_snapshot as read_live_snapshot
from vntts.live_speaker_corpus import LiveSpeakerCorpus
from vntts.live_speech import (
    TypedPlaybackBackend,
    play_typed_text,
    raise_playback_failure,
)
from vntts.model_assets import ModelAssetManager
from vntts.ocr import (
    DialogRegion,
    OCRRegions,
    OCRResult,
    UncertainFrameRecorder,
    default_minimum_ocr_confidence,
    get_dialog_region,
    get_ocr_regions,
)
from vntts.ocr_corrections import OCRCorrectionDictionary, OCRCorrectionStore
from vntts.playback import PreparedPlayback
from vntts.profiles import GameProfileStore
from vntts.reading_diagnostics import record_diagnostic_event, retain_ocr_resolution
from vntts.runtime_config import (
    LiveConfiguration,
    LiveTrackerOptions,
    get_live_configuration,
    initialize_voice_registry,
    initialize_voice_router,
)
from vntts.services.tts_engine import TTSEngine
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
    create_qwen_worker_backend,
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
from vntts.window_capture import (
    DisplayCaptureTarget,
    WindowCaptureTarget,
    WindowGeometry,
)


class _DialogReadFuture(Protocol):
    def done(self) -> bool: ...

    def cancel(self) -> bool: ...
    def cancelled(self) -> bool: ...
    def result(self, timeout: float | None = None) -> object: ...


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
    def result(self, timeout: float | None = None) -> object: ...
    def add_done_callback(
        self, callback: Callable[["_ExecutorFuture"], object]
    ) -> object: ...


class _Executor(Protocol):
    def submit(
        self, callback: Callable[..., object], /, *args: object, **kwargs: object
    ) -> _ExecutorFuture: ...

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None: ...


class _VoiceRouter(Protocol):
    registry: CharacterVoiceRegistry

    def warm_up(self, *, progress: Callable[[int, int, str], None]) -> int: ...


class _CaptureTarget(Protocol):
    def get_geometry(self) -> WindowGeometry | None: ...
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
    indexed_current_event_id: str | None = None


class _ScheduledDialogRead:
    def __init__(
        self,
        submit: Callable[[Callable[[str, str], object]], _DialogReadFuture],
        deliver: Callable[[str, str], object],
        live_reader: _LiveReaderState | None,
        shutdown_requested: Event | None = None,
    ) -> None:
        self._submit = submit
        self._deliver = deliver
        self._live_reader = live_reader
        self._shutdown_requested = shutdown_requested
        self._active_read: _DialogReadFuture | None = None
        self._generation = 0
        self._draining_reads: list[_DialogReadFuture] = []
        self._lock = RLock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._active_read is not None and not self._active_read.done()

    @property
    def has_pending_work(self) -> bool:
        with self._lock:
            return self.is_running or any(
                not future.done() for future in self._draining_reads
            )

    def cancel(self) -> bool:
        with self._lock:
            active = self.is_running
            self._generation += 1
            future = self._active_read
            self._active_read = None
            if future is not None:
                self._draining_reads.append(future)
        if future is not None:
            future.cancel()
        return active

    def wait(self, timeout_seconds: float = 5.0) -> None:
        deadline = monotonic() + timeout_seconds
        with self._lock:
            futures = tuple(self._draining_reads)
        for future in futures:
            if not future.cancelled():
                future.result(timeout=max(0.0, deadline - monotonic()))
        with self._lock:
            self._draining_reads = [f for f in self._draining_reads if not f.done()]

    def __call__(self) -> bool:
        with self._lock:
            if (
                self._shutdown_requested is not None
                and self._shutdown_requested.is_set()
            ):
                return False
            if self._live_reader is not None and self._live_reader.is_running:
                print("Stop live reading before requesting a one-time read")
                return False
            if self.is_running:
                print("A dialog read is already in progress")
                return False

            self._generation += 1
            generation = self._generation

            def guarded_deliver(character: str, text: str) -> object:
                with self._lock:
                    if generation != self._generation or (
                        self._shutdown_requested is not None
                        and self._shutdown_requested.is_set()
                    ):
                        return False
                    return self._deliver(character, text)

            self._active_read = self._submit(guarded_deliver)
            return True


def create_dialog_read_scheduler(
    executor: _DialogReadExecutor,
    voice_router: object,
    screenshot_directory: str | Path,
    *,
    live_reader: _LiveReaderState | None = None,
    shutdown_requested: Event | None = None,
    error_handler: Callable[[Exception], object] | None = None,
    capture_target: object | None = None,
    speech_handler: Callable[..., object] | None = None,
    minimum_confidence: float = default_minimum_ocr_confidence,
    uncertain_frame_recorder: object | None = None,
    diagnostic_handler: Callable[[DiagnosticSnapshot], object] | None = None,
    voice_resolver: Callable[[str], str] | None = None,
    ocr_language: str = "eng",
    correction_dictionary: OCRCorrectionDictionary | None = None,
    correction_dictionary_provider: Callable[[], OCRCorrectionDictionary] | None = None,
    region_provider: Callable[[], DialogRegion] | None = None,
    ocr_regions_provider: Callable[[], OCRRegions | None] | None = None,
    result_resolver: Callable[[OCRResult], OCRResult] | None = None,
) -> _ScheduledDialogRead:
    def deliver(character: str, text: str) -> object:
        if speech_handler is not None:
            return speech_handler(character, text)
        speak = getattr(voice_router, "speak")
        speak_dialog(text, lambda value: speak(character, value))
        return None

    def submit(guarded_deliver: Callable[[str, str], object]) -> _DialogReadFuture:
        return executor.submit(
            read_dialog_safely,
            voice_router,
            screenshot_directory,
            error_handler=error_handler,
            capture_target=capture_target,
            speech_handler=guarded_deliver,
            minimum_confidence=minimum_confidence,
            uncertain_frame_recorder=uncertain_frame_recorder,
            diagnostic_handler=diagnostic_handler,
            voice_resolver=voice_resolver,
            ocr_language=ocr_language,
            correction_dictionary=correction_dictionary_provider()
            if correction_dictionary_provider is not None
            else correction_dictionary,
            region=region_provider() if region_provider is not None else None,
            ocr_regions=ocr_regions_provider()
            if ocr_regions_provider is not None
            else None,
            result_resolver=result_resolver,
        )

    return _ScheduledDialogRead(submit, deliver, live_reader, shutdown_requested)


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
    last_sequence_prefix_gate: tuple[object, ...] | None

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
    story_cursor_lock: RLock
    indexed_current_recovery: (
        tuple[ChapterDialogue, str, object, int, object | None] | None
    )
    observation_serial: int
    explicit_sequence_anchor_pending: bool
    sequence_prefix_dispatch_lease: SequenceEventLease | None
    speech_backend: SpeechBackend | GeneratedAudioFallbackBackend | None
    tts: VoiceEngine | _VoiceRouter | None
    voice_router: _VoiceRouter | None
    schedule_dialog_read: _ScheduledDialogRead | None
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
        qwen_backend_factory: Callable[..., object] = create_qwen_worker_backend,
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
        ] = load_live_sequence_plan,
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
        self.qwen_backend_factory = qwen_backend_factory
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
        self.live_scope_identification_token: str | None = None
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
        self._initialize_live_sequence_state()
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
        self.capture_interval_ms = (
            get_live_configuration(self.settings)["interval_seconds"] * 1000
        )
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
        self.shutdown_complete = Event()
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
        context = audio_lifecycle_context.get() or {}
        reader = getattr(self, "live_reader", None)
        run_id = getattr(reader, "run_id", None)
        details = {
            **({"run_id": run_id} if isinstance(run_id, str) else {}),
            **context,
            **details,
        }
        details.pop("generation", None)
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

    @property
    def is_one_shot_read_running(self) -> bool:
        schedule = self.schedule_dialog_read
        return schedule is not None and schedule.is_running is True

    def runtime_control_snapshot(self) -> dict[str, bool]:
        reader = self.live_reader
        snapshot = reader.runtime_control_snapshot() if reader is not None else {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        starting = self.is_one_shot_read_running
        draining = getattr(self.schedule_dialog_read, "has_pending_work", False) is True
        return {
            **snapshot,
            "starting": starting,
            "queued": bool(starting or draining or snapshot.get("queued")),
        }

    def wait_for_reading(
        self, timeout_seconds: float = 5.0, *, include_speech: bool = True
    ) -> None:
        schedule = self.schedule_dialog_read
        if schedule is not None:
            schedule.cancel()
            wait = getattr(schedule, "wait", None)
            if callable(wait):
                wait(timeout_seconds)
        if self.live_reader is not None:
            self.live_reader.wait(
                timeout_seconds=timeout_seconds, include_speech=include_speech
            )

    def start(self) -> bool:
        return self.runtime_lifecycle.start()

    def prepare_startup(self) -> None:
        self.runtime_lifecycle.prepare_startup()

    def request_shutdown(self) -> None:
        self.shutdown_requested.set()
        self._close_capture_source()

    def apply_settings(
        self, settings: AppSettings, *, cancellation: _Cancellation | None = None
    ) -> bool | None:
        return self.runtime_lifecycle.apply_settings(
            settings, cancellation=cancellation
        )

    def cancel_settings_apply(self, cancellation: _Cancellation) -> bool:
        return self.runtime_lifecycle.cancel_settings_apply(cancellation)

    def shutdown(self) -> None:
        return self.runtime_lifecycle.shutdown()

    def read_once(self) -> bool:
        return self.live_session.read_once()

    def identify_live_scope(self, *, cancellation: _Cancellation | None = None) -> bool:
        return self.live_session.identify_scope(cancellation=cancellation)

    def toggle_live(self) -> bool:
        return self.live_session.toggle()

    def stop_live(self) -> bool:
        return self.live_session.stop()

    def can_start_live_from_ocr(self) -> bool:
        return (
            not self._live_sequence_audio_active()
            and not self.settings.live_speaker_corpus
        )

    def start_live_from_ocr(self) -> bool:
        if not self.can_start_live_from_ocr():
            return False
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

    def preview_voice_choice(self, source_id: str, text: str) -> _ExecutorFuture:
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

    def preview_voice(self, character: str, text: str) -> _ExecutorFuture:
        return self.voice_assignments.preview(character, text)

    def replay_dialog(self, character: str, text: str) -> _ExecutorFuture:
        return self.voice_assignments.replay(character, text)

    def get_capture_geometry(self) -> WindowGeometry | None:
        return self.diagnostics.capture_geometry()

    def get_latest_diagnostic(self) -> DiagnosticSnapshot | None:
        return self.diagnostics.latest()

    def get_live_pipeline_metrics(self) -> LivePipelineMetrics | None:
        return self.diagnostics.pipeline_metrics()

    def inspect_current_dialog(
        self, *, notify: bool = True, publish: bool = True
    ) -> DiagnosticSnapshot:
        return self.diagnostics.inspect_current_dialog(notify=notify, publish=publish)

    def commit_diagnostic_snapshot(
        self, snapshot: DiagnosticSnapshot
    ) -> DiagnosticSnapshot:
        return self._publish_diagnostic(snapshot, notify=False)

    def test_current_dialog(
        self,
        *,
        capture_context: Callable[[], AbstractContextManager[None]] | None = None,
        prepare_live: Callable[[], None] | None = None,
    ) -> tuple[str, str]:
        return self.diagnostics.test_current_dialog(
            capture_context=capture_context, prepare_live=prepare_live
        )

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
            logging.getLogger(__name__).exception(
                "Unable to load live speaker corpus %s",
                self.settings.live_speaker_corpus,
            )
            self.live_speaker_corpus_error = str(error)

    def _revalidate_live_speaker_corpus(self) -> bool:
        if not self.settings.live_speaker_corpus:
            return True
        if self.live_speaker_corpus is None:
            return False
        try:
            self.live_speaker_corpus.revalidate()
        except (OSError, TypeError, ValueError) as error:
            logging.getLogger(__name__).exception(
                "Unable to revalidate live speaker corpus %s",
                self.settings.live_speaker_corpus,
            )
            self.live_speaker_corpus_error = str(error)
            return False
        self.live_speaker_corpus_error = None
        return True

    def _get_live_configuration(self) -> LiveConfiguration:
        configuration = get_live_configuration(self.settings)
        tracker_options: LiveTrackerOptions = {
            **configuration["tracker_options"],
            "complete_dialogue_only": (
                self.settings.audio_source_policy != "live-tts-only"
                or self._live_sequence_audio_active()
            ),
        }
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
        return {
            "interval_seconds": configuration["interval_seconds"],
            "tracker_options": tracker_options,
        }

    def _resolve_early_indexed_dialogue(self, character: str, text: str) -> str | None:
        backend = self.speech_backend
        if not isinstance(backend, GeneratedAudioFallbackBackend):
            return None
        # Unprepared story lines must still count: they may share this prefix.
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
        generated_backend = self._create_generated_audio_backend(live_backend)
        if generated_backend is None:
            return False
        self.speech_backend = generated_backend
        return True

    def _create_generated_audio_backend(
        self, live_backend: SpeechBackend | None
    ) -> GeneratedAudioFallbackBackend | None:
        policy = self.settings.audio_source_policy
        live_name = (
            self.settings.speech_backend if live_backend is None else live_backend.name
        )
        if policy == "live-tts-only":
            self.status_handler(f"Audio policy: live TTS only ({live_name})")
            return None
        if not self.settings.story_index:
            self.status_handler(
                "Audio fallback disabled: configure a story index for stable line IDs"
            )
            return None
        library = None
        if self.settings.generated_audio_manifest:
            library = self.generated_audio_library_factory(
                self.settings.generated_audio_manifest,
                warn=self.status_handler,
            )
        if policy == "prefer-generated" and library is None:
            self.status_handler(
                f"Generated audio unavailable; using live TTS ({live_name})"
            )
            return None
        generated_backend = self.generated_audio_backend_factory(
            live_backend,
            library,
            self.chapter_voice_preloader,
            volume=self.settings.output_volume_percent / 100,
            speed=self.settings.speech_rate_percent / 100,
            audio_source_policy=policy,
        )
        generated_backend.voice_override = self._has_manual_voice_override
        generated_backend.progress_wait_status = self.status_handler
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
        return generated_backend

    def refresh_corrections(self) -> None:
        self.correction_store = OCRCorrectionStore.load(self.correction_store.path)
        self.correction_dictionary = self.correction_store.dictionary_for(
            self.settings.active_profile_id
        )

    def _create_capture_target(self) -> _CaptureTarget | None:
        if self.settings.capture_mode != "window":
            return DisplayCaptureTarget()
        return self.capture_target_factory(self.settings.game_window_title)

    def _close_capture_source(self) -> None:
        if isinstance(self.capture_target, DisplayCaptureTarget):
            self.capture_target.close()

    def _capture_live_frame(self) -> object:
        return capture_live_frame(
            get_screenshot_directory(self.settings),
            self.capture_target,
            region=self._capture_region(),
            ocr_regions=self._capture_ocr_regions(),
            run_id=getattr(self.live_reader, "run_id", None),
            provenance_handler=self._record_capture_provenance,
        )

    def _record_capture_provenance(self, provenance: object) -> None:
        from vntts.capture_provenance import CaptureProvenance

        if not isinstance(provenance, CaptureProvenance):
            return
        reader = self.live_reader
        health = (
            reader.worker_health_snapshot()
            if isinstance(reader, LiveDialogReader)
            else {}
        )
        details = {
            key: value
            for key, value in provenance.as_dict().items()
            if key not in {"requested_window_title", "window_title"}
        }
        details.update(health)
        self.pipeline_event_handler(
            "capture",
            max(1, reader.active_generation if reader else 1),
            provenance.completed_monotonic,
            **details,
        )

    def _capture_region(self) -> DialogRegion:
        profile = self.profile_store.get(self.settings.active_profile_id)
        return get_dialog_region(
            profile.dialog_region if profile is not None else None,
            coordinate_space=profile.coordinate_space if profile is not None else None,
        )

    def _capture_ocr_regions(self) -> OCRRegions | None:
        profile = self.profile_store.get(self.settings.active_profile_id)
        return get_ocr_regions(
            self._capture_region(),
            profile.dialog_region if profile is not None else None,
            profile.ocr_regions if profile is not None else None,
        )

    def _recognize_live_frame(
        self, frame: object
    ) -> tuple[str, str] | RecognizedDialog:
        voice_router = self.voice_router
        if voice_router is None:
            raise RuntimeError("The speech engine is not ready")
        try:
            image = getattr(frame, "image", None)
            provenance = getattr(image, "info", {}).get("capture_provenance")
            identity = (
                {key: provenance.get(key) for key in ("run_id", "capture_id")}
                if isinstance(provenance, dict)
                else {}
            )
            if not identity:
                frame_provenance = getattr(frame, "provenance", None)
                if frame_provenance is not None:
                    identity = {
                        key: getattr(frame_provenance, key, None)
                        for key in ("run_id", "capture_id")
                    }
        except Exception:
            identity = {}

        raw_result: OCRResult | None = None
        canonical_line: ChapterDialogue | None = None
        canonical_exact = False

        def observe_raw_result(result: OCRResult) -> None:
            nonlocal raw_result
            raw_result = result

        def resolve_result(result: OCRResult) -> OCRResult:
            nonlocal canonical_line, canonical_exact
            started = monotonic()
            outcome = "success"
            error_type: str | None = None
            resolution: dict[str, object] = {}
            try:
                resolved = self._resolve_story_ocr_result(result, resolution=resolution)
                line = resolution.get("line")
                canonical_line = line if isinstance(line, ChapterDialogue) else None
                canonical_exact = resolution.get("match_result") in {
                    "expected-exact",
                    "expected-normalized-exact",
                    "expected-text-only",
                    "expected-normalized-text-only",
                }
                return resolved
            except BaseException as error:
                outcome = "error"
                error_type = type(error).__name__
                raise
            finally:
                try:
                    retain_ocr_resolution(
                        frame, raw_result or result, resolution, **identity
                    )
                    record_diagnostic_event(
                        "ocr",
                        "story-result-resolution",
                        **identity,
                        outcome=outcome,
                        error_type=error_type,
                        resolver_ms=round((monotonic() - started) * 1000, 3),
                    )
                except Exception:
                    pass

        character, text = recognize_live_frame(
            frame,
            voice_router.registry,
            self.settings.ocr_minimum_confidence,
            self._reject_uncertain_live_frame,
            self.uncertain_frame_recorder,
            self._publish_diagnostic,
            self._resolve_voice_label,
            self.settings.ocr_language,
            self.correction_dictionary,
            ellipsis_speaker_resolver=self.chapter_voice_preloader,
            result_resolver=resolve_result,
            raw_result_observer=observe_raw_result,
        )
        character = self._canonical_observed_character(character, text)
        if (
            canonical_line is not None
            and canonical_line.line_id is not None
            and canonical_line.text_sha256 is not None
            and (character, text) == (canonical_line.speaker, canonical_line.text)
        ):
            replacement = bool(
                isinstance(self.speech_backend, GeneratedAudioFallbackBackend)
                and self.speech_backend.reserve_generated_line_for_early_playback(
                    canonical_line
                )
            )
            return RecognizedDialog(
                character,
                text,
                canonical_line.line_id,
                exact_match=canonical_exact,
                allow_unheard_replacement=replacement,
            )
        return character, text

    @staticmethod
    def _reject_uncertain_live_frame(
        result: OCRResult, minimum_confidence: float
    ) -> None:
        raise OCRUncertainError(result, minimum_confidence)

    def _admit_live_ocr_uncertainty(
        self, error: OCRUncertainError, is_current: Callable[[], bool]
    ) -> None:
        self._ocr_uncertain(
            error.result,
            error.minimum_confidence,
            observation_is_current=is_current,
        )

    def _resolve_story_ocr_result(
        self, result: OCRResult, *, resolution: dict[str, object] | None = None
    ) -> OCRResult:
        confidence = (
            result.confidence
            if result.dialogue_confidence is None
            else result.dialogue_confidence
        )
        if (
            not result.text.strip()
            or not confidence >= self.settings.ocr_minimum_confidence
        ):
            if resolution is not None:
                resolution["match_result"] = (
                    "empty" if not result.text.strip() else "ocr-uncertain"
                )
            return result
        with self.story_cursor_lock:
            expected_prefix = self._expected_sequence_prefix_line_locked()
            if expected_prefix is not None and _normalize_story_text(
                expected_prefix.text
            ).startswith(_normalize_story_text(result.text)):
                if resolution is not None:
                    resolution["match_result"] = "owned-prefix"
                return result
            preloader = self.chapter_voice_preloader
            plan, cursor = self.live_sequence_plan, self.story_cursor
            if plan is not None and self.settings.live_sequence_mode != "off":
                events = (
                    (cursor.current_event, *cursor.bounded_visible_successors())
                    if cursor is not None
                    and cursor.current_event is not None
                    and cursor.state
                    not in {StoryCursorState.UNSYNCHRONIZED, StoryCursorState.ANCHORING}
                    else tuple(plan.events.values())
                )
                line_ids = tuple(
                    event.line_id
                    for event in events
                    if event is not None
                    and event.is_speech
                    and event.line_id is not None
                )
            else:
                rows = (
                    preloader.dialogue
                    if preloader.current_match is None
                    else preloader.by_chapter.get(preloader.current_match.chapter, ())
                )
                line_ids = tuple(row.line_id for row in rows if row.line_id)
            line, match = preloader.resolve_complete_among(
                "", result.text, line_ids, update_context=False
            )
            if line is None:
                chapter_line, chapter_match = (
                    self._resolve_chapter_exact_sequence_line_locked("", result.text)
                )
                if chapter_line is not None:
                    line, match = chapter_line, chapter_match
            if resolution is not None:
                resolution.update(match_result=match, line=line)
        if line is None:
            return result
        return replace(
            result,
            character=line.speaker,
            text=line.text,
            confidence=confidence,
            speaker_note=(
                f"Speaker from the prepared story; OCR name ignored: {result.character}"
            ),
        )

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

    def _speak_with_live_backend(self, character: str, text: str) -> bool:
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
            setattr(voice_router, "narrator_voice", voice)

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

    def _finish_observed_dialogue(self) -> bool:
        self.history.finish_current()
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
        self.dialog_handler("Narrator", "")
        return True

    def _dialog_observed(
        self, character: str | None, text: str
    ) -> DialogObservationDecision:
        if self.shutdown_requested.is_set():
            return False
        if not text:
            with self.story_cursor_lock:
                self.observation_serial += 1
                self.indexed_current_recovery = None
            return self._finish_observed_dialogue()
        with self.story_cursor_lock:
            self.observation_serial += 1
            self.indexed_current_recovery = None
        observed_character = character or "Narrator"
        character = self._canonical_observed_character(character, text)
        canonical_routing = False
        canonical_event_changed = False
        early_prefix = False
        owned_confirmation = False
        with self.story_cursor_lock:
            previous_event_id = (
                None
                if self.story_cursor is None
                else self.story_cursor.current_event_id
            )
            sequence_observation = self._observe_live_sequence(character, text)
            if self._live_sequence_audio_active():
                if self.story_cursor is None:
                    return False
                if sequence_observation is None:
                    # An unresolved typewriter prefix cannot authorize speech;
                    # a later exact frame can still establish the anchor.
                    return False
                else:
                    snapshot, observed_line, _match_result = sequence_observation
                    early_prefix = _match_result == "expected-owned-prefix"
                    owned_confirmation = _match_result in {
                        "expected-owned-prefix",
                        "expected-owned-exact",
                    }
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
                    canonical_event_changed = event.event_id != previous_event_id
        speech_deferred = self._offer_unknown_speaker_mapping(character, text)
        self._prime_observed_voice(character)
        if not owned_confirmation:
            self._prime_likely_chapter_voice(character, text)
        if canonical_event_changed:
            self.history.finish_current()
        self._record_and_display_dialogue(character, text)
        if speech_deferred:
            return False
        if early_prefix:
            return CanonicalDialogRoute(character, text)
        return (
            (character, text)
            if canonical_routing or character != observed_character
            else True
        )

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

    def _sequence_cursor_for_lease_locked(
        self, lease: SequenceEventLease
    ) -> _StoryCursor | None:
        cursor = self.story_cursor
        if (
            cursor is None
            or cursor.current_event_id != lease.event_id
            or cursor.occurrence_id != lease.occurrence_id
        ):
            return None
        return cursor

    def _seal_sequence_route_locked(
        self, cursor: _StoryCursor, lease: SequenceEventLease, route: str
    ) -> bool:
        existing = self.sequence_event_terminal_routes.setdefault(lease, route)
        if existing != route:
            cursor.desynchronize("conflicting-terminal-audio-route")
            self._publish_live_sequence_status()
            return False
        return True

    def _start_live_sequence_session(self) -> None:
        with self.story_cursor_lock:
            self.sequence_admitted_chunks.clear()
            self.sequence_playback_leases.clear()
            self.sequence_prefix_dispatch_lease = None
            cursor = self.story_cursor
            if cursor is None:
                return
            if self.explicit_sequence_anchor_pending:
                self.explicit_sequence_anchor_pending = False
            else:
                cursor.reset("live-session-started")
            self._publish_live_sequence_status()

    def _mark_sequence_silence_locked(self, event_id: str) -> bool:
        cursor = self.story_cursor
        if cursor is None or cursor.current_event_id != event_id:
            return False
        event = cursor.current_event
        if event is None or event.kind != "silent":
            return False
        lease = SequenceEventLease(event.event_id, cursor.occurrence_id)
        return self._seal_sequence_route_locked(cursor, lease, "silence")

    def _load_live_sequence_plan(self) -> bool:
        with self.story_cursor_lock:
            return self._load_live_sequence_plan_locked()

    def _load_live_sequence_plan_locked(self) -> bool:
        self.live_sequence_plan = None
        self.story_cursor = None
        self.explicit_sequence_anchor_pending = False
        self.sequence_prefix_confirmation_event_id = None
        self.sequence_prefix_dispatch_lease = None
        with self.sequence_prefetch_lock:
            self.sequence_prefetch_keys.clear()
        self.sequence_event_terminal_routes.clear()
        self.sequence_admitted_chunks.clear()
        self.sequence_playback_leases.clear()
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
            logging.getLogger(__name__).exception(
                "Unable to load live sequence plan %s with story index %s",
                self.settings.live_sequence_plan,
                self.settings.story_index,
            )
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

    def _sequence_ocr_activity(self, state: StoryCursorState) -> str:
        recognized_frames = 0
        live_reader = getattr(self, "live_reader", None)
        if live_reader is not None:
            recognized_frames = live_reader.get_pipeline_metrics().recognized_frames
        if state in {
            StoryCursorState.UNSYNCHRONIZED,
            StoryCursorState.ANCHORING,
        }:
            ocr_activity = (
                f"Full OCR anchoring; {recognized_frames} frame(s) recognized"
            )
        elif state in {
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
        return ocr_activity

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
            guidance = (
                "Canonical audio is playing; watching for the next visible dialogue."
            )
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
        recovery = self._indexed_current_recovery_locked()
        if recovery is not None:
            recovery_required = True
            guidance = (
                "The visible line is indexed outside the expected story path. "
                "Read indexed current line uses the configured audio priority; "
                "then continue manually in the game, or set story position."
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
            ocr_activity=self._sequence_ocr_activity(snapshot.state),
            expected_candidate_count=expected_candidate_count,
            indexed_current_event_id=recovery[1] if recovery is not None else None,
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

    def _resolve_chapter_exact_sequence_line_locked(
        self, character: str, text: str
    ) -> tuple[ChapterDialogue | None, str]:
        cursor, plan = self.story_cursor, self.live_sequence_plan
        current = None if cursor is None else cursor.current_event
        if current is None or plan is None or self.settings.live_sequence_mode == "off":
            return None, "expected-no-active-chapter"
        line_ids = tuple(
            event.line_id
            for event in plan.events.values()
            if event.is_speech and event.chapter == current.chapter
        )
        line, match = self.chapter_voice_preloader.resolve_exact_among(
            character,
            text,
            line_ids,
            update_context=False,
        )
        if line is None:
            return None, match
        line, match = self.chapter_voice_preloader.resolve_complete_among(
            character, text, line_ids, update_context=False
        )
        if match not in {
            "expected-exact",
            "expected-normalized-exact",
            "expected-text-only",
            "expected-normalized-text-only",
        }:
            return None, match
        return line, match

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
            if (
                silent_event is None
                and self.settings.live_sequence_mode == "shadow"
                and cursor.state != StoryCursorState.PLAYING
            ):
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
                expected = self._expected_sequence_prefix_line_locked()
                if expected is not None:
                    observed = _normalize_story_text(text)
                    speaker = _normalize_story_text(character)
                    prefix_matches = bool(
                        any(character.isalnum() for character in observed)
                        and _normalize_story_text(expected.text).startswith(observed)
                    )
                    speaker_matches = bool(
                        speaker in {"", "narrator"}
                        or speaker == _normalize_story_text(expected.speaker)
                    )
                    reserved = bool(
                        prefix_matches
                        and speaker_matches
                        and self._reserve_generated_prefix(expected)
                    )
                    self._report_sequence_prefix_gate_locked(
                        "owned-prefix-accepted"
                        if reserved
                        else "prefix-text-mismatch"
                        if not prefix_matches
                        else "prefix-speaker-mismatch"
                        if not speaker_matches
                        else "generated-reservation-failed",
                        line_id=expected.line_id,
                        normalized_text_characters=len(observed),
                        prefix_matches=prefix_matches,
                        speaker_matches=speaker_matches,
                        reservation_ready=reserved,
                    )
                    if reserved:
                        line = expected
                        match_result = (
                            "expected-owned-exact"
                            if observed == _normalize_story_text(expected.text)
                            else "expected-owned-prefix"
                        )
                    else:
                        line, match_result = resolve_bounded(
                            character, text, candidate_line_ids
                        )
                else:
                    line, match_result = resolve_bounded(
                        character, text, candidate_line_ids
                    )
                snapshot, line, match_result = self._apply_sequence_observation_locked(
                    cursor, line, match_result, candidate_event_ids, character, text
                )
        self._remember_indexed_current_recovery_locked(
            character, text, line, previous_event_id, candidate_events
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

    def _apply_sequence_observation_locked(
        self,
        cursor: _StoryCursor,
        line: ChapterDialogue | None,
        match_result: object,
        candidate_event_ids: tuple[str, ...],
        character: str,
        text: str,
    ) -> tuple[StoryCursorSnapshot | None, ChapterDialogue | None, object]:
        chapter_line, chapter_match = (
            self._resolve_chapter_exact_sequence_line_locked(character, text)
            if line is None
            else (None, match_result)
        )
        chapter_event = (
            None
            if chapter_line is None or chapter_line.line_id is None
            else cursor.plan.event_for_line(chapter_line.line_id)
        )
        resolved_event = (
            None
            if line is None or line.line_id is None
            else cursor.plan.event_for_line(line.line_id)
        )
        observed_event = chapter_event or resolved_event
        if (
            observed_event is not None
            and observed_event.event_id != cursor.current_event_id
            and any(
                lease.event_id == observed_event.event_id
                for lease in self.sequence_admitted_chunks
            )
        ):
            return None, None, "expected-previously-admitted-occurrence"
        if (
            chapter_event is not None
            and chapter_event.event_id not in candidate_event_ids
        ):
            return (
                cursor.anchor_event(
                    chapter_event.event_id, "observation-chapter-exact"
                ),
                chapter_line,
                chapter_match,
            )
        line = self._apply_sequence_prefix_barrier_locked(
            cursor, line, match_result, resolved_event
        )
        snapshot = (
            None
            if line is None or line.line_id is None
            else cursor.observe_bounded_line(line.line_id, candidate_event_ids)
        )
        return snapshot, line, match_result

    def _apply_sequence_prefix_barrier_locked(
        self,
        cursor: _StoryCursor,
        line: ChapterDialogue | None,
        match_result: object,
        resolved_event: LiveSequenceEvent | None,
    ) -> ChapterDialogue | None:
        prefix_match = "prefix" in str(match_result)
        pending_prefix_continuation = bool(
            prefix_match
            and resolved_event is not None
            and resolved_event.event_id == cursor.current_event_id
            and self.sequence_prefix_confirmation_event_id == resolved_event.event_id
        )
        if (
            self.settings.live_sequence_mode == "audio-auto"
            and line is not None
            and prefix_match
            and not pending_prefix_continuation
            and (
                cursor.state != StoryCursorState.WAITING_TRANSITION
                or (
                    match_result != "expected-owned-prefix"
                    and not self._reserve_generated_prefix(line)
                )
            )
        ):
            line = None
        elif self.settings.live_sequence_mode == "audio-auto" and line is not None:
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
                    "expected-owned-exact",
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
        return line

    def _report_sequence_prefix_gate_locked(
        self, reason: str, **details: object
    ) -> None:
        cursor = self.story_cursor
        if cursor is None:
            return
        identity = (
            cursor.current_event_id,
            cursor.occurrence_id,
            self.live_reader.run_id if self.live_reader is not None else None,
            reason,
            *details.items(),
        )
        if identity == self.last_sequence_prefix_gate:
            return
        self.last_sequence_prefix_gate = identity
        self.pipeline_event_handler(
            "sequence-prefix-gate",
            self.live_reader.active_generation if self.live_reader is not None else 0,
            monotonic(),
            event_id=cursor.current_event_id,
            occurrence_id=cursor.occurrence_id,
            reason=reason,
            **details,
        )

    def _expected_sequence_prefix_line_locked(
        self, *, report_gate: bool = False
    ) -> ChapterDialogue | None:
        cursor = self.story_cursor
        if (
            cursor is None
            or self.settings.live_sequence_mode != "audio-auto"
            or cursor.state != StoryCursorState.WAITING_TRANSITION
            or cursor.current_event_id is None
        ):
            return None
        reason = None
        if not self.game_focused:
            reason = "game-focus-not-owned"
        elif self.sequence_prefix_dispatch_lease != SequenceEventLease(
            cursor.current_event_id, cursor.occurrence_id
        ):
            reason = "dispatch-lease-not-current"
        reader = self.live_reader
        if reason is None and reader is not None:
            if reader.failed_auto_advance_generation == reader.active_generation:
                reason = "advance-confirmation-timed-out"
            elif reader.paused is True:
                reason = "reading-paused"
            elif reader.emergency_stopped is True or reader.stop_event.is_set() is True:
                reason = "reading-stopped"
        if reason is not None:
            if report_gate:
                self._report_sequence_prefix_gate_locked(reason)
            return None
        event = cursor.deterministic_visual_successor()
        return self._declared_generated_sequence_line_locked(
            event, report_gate=report_gate
        )

    def _declared_generated_sequence_line_locked(
        self, event: LiveSequenceEvent | None, *, report_gate: bool = False
    ) -> ChapterDialogue | None:
        if event is None or not event.is_speech:
            if report_gate:
                self._report_sequence_prefix_gate_locked(
                    "no-deterministic-speech-successor"
                )
            return None
        line = self._canonical_sequence_line_locked(event.event_id)
        backend = self.speech_backend
        if line is None:
            reason = "canonical-line-integrity-failed"
        elif not isinstance(backend, GeneratedAudioFallbackBackend):
            reason = "generated-backend-unavailable"
        elif backend.library is None:
            reason = "generated-library-unavailable"
        elif (
            backend.library.index.find(
                line.line_id, line.text_sha256, verify_file=False
            )
            is None
        ):
            reason = "generated-entry-unavailable"
        else:
            reason = "owned-successor-eligible"
        if report_gate and reason != "owned-successor-eligible":
            self._report_sequence_prefix_gate_locked(
                reason, target_event_id=event.event_id, line_id=event.line_id
            )
        return line if reason == "owned-successor-eligible" else None

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
        self.sequence_admitted_chunks.clear()
        self.sequence_playback_leases.clear()
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
            reader.clear_queue(reason="explicit-sequence-replacement")

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

    def _remember_indexed_current_recovery_locked(
        self,
        character: str,
        text: str,
        line: ChapterDialogue | None,
        previous_event_id: str | None,
        candidate_events: tuple[LiveSequenceEvent, ...],
    ) -> None:
        cursor, plan = self.story_cursor, self.live_sequence_plan
        if (
            line is not None
            or cursor is None
            or plan is None
            or cursor.state == StoryCursorState.PLAYING
        ):
            return
        exact, exact_result = self.chapter_voice_preloader.resolve_exact_among(
            character,
            text,
            (event.line_id for event in plan.events.values() if event.is_speech),
            update_context=False,
        )
        if exact_result not in {"expected-exact", "expected-normalized-exact"}:
            exact = None
        exact_event = (
            plan.event_for_line(exact.line_id)
            if exact is not None and exact.line_id
            else None
        )
        allowed = {event.event_id for event in candidate_events}
        if (
            exact is not None
            and exact_event is not None
            and exact_event.event_id != previous_event_id
            and exact_event.event_id not in allowed
        ):
            reader = self.live_reader
            self.indexed_current_recovery = (
                exact,
                exact_event.event_id,
                getattr(reader, "run_id", None),
                self.observation_serial,
                getattr(reader, "observation_frame", None),
            )

    def _indexed_current_recovery_locked(
        self,
    ) -> tuple[ChapterDialogue, str, object, int, object | None] | None:
        recovery = self.indexed_current_recovery
        reader = getattr(self, "live_reader", None)
        if (
            recovery is None
            or reader is None
            or self.shutdown_requested.is_set()
            or reader.emergency_stopped is True
            or recovery[2] != getattr(reader, "run_id", None)
            or recovery[3] != self.observation_serial
        ):
            return None
        if isinstance(reader, LiveDialogReader):
            with reader.state_lock:
                latest = reader.latest_frame
                visible = reader.latest_frame_visible
            captured = recovery[4]
            if (
                not visible
                or captured is None
                or latest is None
                or (
                    captured is not latest
                    and not reader.frame_equivalent(captured, latest)
                )
            ):
                return None
        return recovery

    def read_indexed_current_dialogue(self) -> bool:
        """Explicitly read the displayed indexed recovery; never infer a graph edge."""
        with self.story_cursor_lock:
            recovery = self._indexed_current_recovery_locked()
            reader = self.live_reader
            cursor = self.story_cursor
            if recovery is None or reader is None or cursor is None:
                return False
            line, event_id, _run, _serial, _frame = recovery
            self.indexed_current_recovery = None
            self.sequence_prefix_dispatch_lease = None
            self.sequence_prefix_confirmation_event_id = None
            self.sequence_admitted_chunks.clear()
            self.sequence_playback_leases.clear()
            reader.clear_queue(reason="indexed-current-recovery")
            cursor.anchor_event(event_id, "explicit-indexed-current-read")
            accepted = self._enqueue_selected_sequence_line(
                line, manual_continuation=True
            )
            if accepted:
                reader.bind_current_frame_route()
            else:
                cursor.desynchronize("explicit-route-failed")
            self._publish_live_sequence_status()
        self.status_handler(
            "Reading indexed current line; continue manually in the game"
            if accepted
            else "Indexed current line could not be queued"
        )
        return accepted

    def _enqueue_selected_sequence_line(
        self, line: ChapterDialogue, *, manual_continuation: bool = False
    ) -> bool:
        self._prime_observed_voice(line.speaker)
        self._prime_likely_chapter_voice(line.speaker, line.text)
        self.history.finish_current()
        self._record_and_display_dialogue(line.speaker, line.text)
        reader = self.live_reader
        if reader is None:
            return False
        return reader.enqueue(
            line.speaker,
            line.text,
            line_id=line.line_id,
            **(
                {
                    "auto_advance_block_reason": "explicit-current-read-manual-continuation"
                }
                if manual_continuation
                else {}
            ),
        )

    def _record_and_display_dialogue(self, character: str | None, text: str) -> None:
        self.history.add(character, text)
        self.dialog_handler(character or "Narrator", text)

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
            if not self._stable_live_frame_owner_is_current_locked(
                cursor, expected_owner, route_epoch
            ):
                return False
            if cursor.state in {
                StoryCursorState.UNSYNCHRONIZED,
                StoryCursorState.ANCHORING,
            }:
                return None
            if (
                self.live_reader is not None
                and self.live_reader.observation_uncertain is True
            ):
                return None
            if self._expected_sequence_prefix_line_locked() is not None:
                return None
            if not settled:
                return None
            if cursor.state in {
                StoryCursorState.MANUAL,
                StoryCursorState.DESYNCHRONIZED,
            }:
                return None
            if self.sequence_prefix_confirmation_event_id == cursor.current_event_id:
                return self._route_sequence_prefix_confirmation_locked(cursor)
            if not cursor.can_confirm_visual_transition:
                return None
            return self._route_visual_successor_locked(cursor)

    def _stable_live_frame_owner_is_current_locked(
        self,
        cursor: _StoryCursor,
        expected_owner: str | None,
        route_epoch: int | None,
    ) -> bool:
        if expected_owner is not None and cursor.current_event_id != expected_owner:
            return False
        return not (
            route_epoch is not None
            and self.live_reader is not None
            and not self.live_reader.frame_route_epoch_is_current(route_epoch)
        )

    def _route_sequence_prefix_confirmation_locked(
        self, cursor: _StoryCursor
    ) -> tuple[str, str] | None:
        completion_probe = getattr(
            self.live_reader,
            "current_frame_has_completion_cue",
            None,
        )
        if not callable(completion_probe) or not completion_probe():
            return None
        event = cursor.current_event
        line = self._canonical_sequence_line_locked(
            None if event is None else event.event_id
        )
        if line is None:
            return None
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
    ) -> bool | SilentDialogRoute | None:
        event = cursor.deterministic_visual_successor()
        application_owned_transition = bool(
            self.settings.live_sequence_mode == "audio-auto"
            and cursor.state == StoryCursorState.WAITING_TRANSITION
        )
        if event is None or event.kind != "silent":
            return None
        if not self._has_unique_visible_successor(
            cursor,
            event,
            application_owned_transition,
        ):
            return None
        return self._confirm_visual_sequence_successor_locked(
            cursor,
            event,
            application_owned_transition,
        )

    def _has_unique_visible_successor(
        self,
        cursor: _StoryCursor,
        event: LiveSequenceEvent | None,
        application_owned_transition: bool,
    ) -> bool:
        immediate = bool(
            application_owned_transition
            and event is not None
            and event.kind == "silent"
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
    ) -> bool | SilentDialogRoute:
        if event is None or event.kind != "silent":
            return False
        previous_event_id = cursor.current_event_id
        confirming_dispatch = cursor.state == StoryCursorState.WAITING_TRANSITION
        confirmed_event = cursor.confirm_visual_transition()
        if confirmed_event is None or confirmed_event.event_id != event.event_id:
            return False
        if confirming_dispatch and self.live_reader is not None:
            self.live_reader.confirm_pending_auto_advance()
        generation = self.live_reader.active_generation if self.live_reader else 0
        return self._confirm_silent_visual_successor_locked(
            cursor,
            event,
            previous_event_id,
            generation,
            application_owned_transition,
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
            if (
                self.live_reader is not None
                and self.live_reader.observation_uncertain is True
                and self._current_canonical_occurrence_owned_locked()
            ):
                return "current-canonical-recheck"
            if self._expected_sequence_prefix_line_locked(report_gate=True) is not None:
                return "expected-successor-prefix"
            reader = self.live_reader
            if (
                self.settings.live_sequence_mode == "audio-auto"
                and cursor.state == StoryCursorState.WAITING_TRANSITION
                and reader is not None
                and reader.failed_auto_advance_generation == reader.active_generation
                and reader.last_auto_advance_dispatched_generation
                == reader.active_generation
            ):
                return "post-advance-recovery"
            if (
                self.settings.live_sequence_mode == "audio-auto"
                and cursor.state == StoryCursorState.WAITING_TRANSITION
                and (event := cursor.deterministic_visual_successor()) is not None
                and self._declared_generated_sequence_line_locked(event) is not None
            ):
                return "bounded-transition-confirmation"
            return (
                self._bounded_successor_ocr_purpose_locked(cursor)
                or "visible-dialogue-confirmation"
            )

    def _bounded_successor_ocr_purpose_locked(self, cursor: _StoryCursor) -> str | None:
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
        quiet_ms = None
        with self.story_cursor_lock:
            cursor = self.story_cursor
            reader = self.live_reader
            target_event_id = self.sequence_prefix_confirmation_event_id
            event_id = None if cursor is None else cursor.current_event_id
            if (
                cursor is None
                or reader is None
                or self.settings.live_sequence_mode != "audio-auto"
                or cursor.state != StoryCursorState.LOCKED
                or target_event_id != cursor.current_event_id
            ):
                reason = (
                    "prefix-owner-changed"
                    if cursor is not None and target_event_id != cursor.current_event_id
                    else "prefix-cursor-unavailable"
                )
            else:
                quiet_probe = getattr(reader, "current_frame_render_quiet_ms", None)
                quiet_ms = (
                    quiet_probe(expected_owner=cursor.current_event_id)
                    if callable(quiet_probe)
                    else None
                )
                if quiet_ms is None:
                    reason = "prefix-render-not-quiet"
                else:
                    event = cursor.current_event
                    line = self._canonical_sequence_line_locked(
                        None if event is None else event.event_id
                    )
                    bind_frame = getattr(reader, "bind_current_frame_route", None)
                    if (
                        line is None
                        or not callable(bind_frame)
                        or not bind_frame(expected_render_owner=cursor.current_event_id)
                    ):
                        reason = "prefix-observation-unavailable"
                    else:
                        self.sequence_prefix_confirmation_event_id = None
                        record_full_text = getattr(
                            reader, "record_canonical_full_text", None
                        )
                        if callable(record_full_text):
                            record_full_text(
                                line_id=line.line_id,
                                reason="owner-render-quiet",
                                settled_ms=quiet_ms,
                            )
                        return True
        if reader is not None and target_event_id is not None:
            record_wait = getattr(reader, "record_prefix_confirmation_wait", None)
            if callable(record_wait):
                record_wait(
                    reason,
                    event_id=event_id,
                    target_event_id=target_event_id,
                    quiet_ms=quiet_ms,
                )
        return False

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

    def _accept_live_chunk(self, chunk: SpeechChunk) -> SpeechChunk | None:
        if chunk.explicit_replay or not self._live_sequence_audio_active():
            return chunk
        with self.story_cursor_lock:
            cursor = self.story_cursor
            event = None if cursor is None else cursor.current_event
            if (
                cursor is None
                or cursor.state != StoryCursorState.LOCKED
                or event is None
                or not event.is_speech
            ):
                return None
            line = self._canonical_sequence_line_locked(event.event_id)
            if (
                line is None
                or chunk.line_id != event.line_id
                or (chunk.character, chunk.text) != (line.speaker, line.text)
            ):
                return None
            lease = SequenceEventLease(event.event_id, cursor.occurrence_id)
            if (
                lease in self.sequence_admitted_chunks
                or lease in self.sequence_event_terminal_routes
            ):
                return None
            accepted = replace(
                chunk,
                sequence_event_id=lease.event_id,
                sequence_occurrence_id=lease.occurrence_id,
            )
            self.sequence_admitted_chunks[lease] = accepted
            return accepted

    def _reject_live_chunk(self, chunk: SpeechChunk) -> None:
        if chunk.sequence_event_id is None or chunk.sequence_occurrence_id is None:
            return
        lease = SequenceEventLease(
            chunk.sequence_event_id, chunk.sequence_occurrence_id
        )
        with self.story_cursor_lock:
            if (
                self.sequence_admitted_chunks.get(lease) is chunk
                and lease not in self.sequence_playback_leases
                and lease not in self.sequence_event_terminal_routes
            ):
                del self.sequence_admitted_chunks[lease]

    def _begin_sequence_playback(self, chunk: SpeechChunk) -> SequenceEventLease | None:
        successor = None
        with self.story_cursor_lock:
            cursor = self.story_cursor
            if cursor is None or not is_live_sequence_audio_mode(
                self.settings.live_sequence_mode
            ):
                return None
            admitted_lease = (
                SequenceEventLease(
                    chunk.sequence_event_id, chunk.sequence_occurrence_id
                )
                if chunk.sequence_event_id is not None
                and chunk.sequence_occurrence_id is not None
                else None
            )
            if admitted_lease is not None:
                if (
                    self.sequence_admitted_chunks.get(admitted_lease) is not chunk
                    or admitted_lease in self.sequence_event_terminal_routes
                    or admitted_lease in self.sequence_playback_leases
                ):
                    return None
                if self._sequence_cursor_for_lease_locked(admitted_lease) is None:
                    self.sequence_playback_leases.add(admitted_lease)
                    return admitted_lease
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
                self.sequence_playback_leases.discard(lease)
                return None
            self.sequence_playback_leases.add(lease)
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
            authorized = bool(
                backend is self.speech_backend
                and settings is self.settings
                and self._sequence_cursor_for_lease_locked(owner_lease) is not None
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
            if outcome in {"reserved", "prepared"} and (
                backend is not self.speech_backend
                or settings is not self.settings
                or self._sequence_cursor_for_lease_locked(owner_lease) is None
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
        self,
        lease: SequenceEventLease | None,
        outcome: PlaybackOutcome | None,
        *,
        interrupted_for_pause: bool = False,
    ) -> bool:
        with self.story_cursor_lock:
            if not isinstance(lease, SequenceEventLease):
                return False
            cursor = self._sequence_cursor_for_lease_locked(lease)
            admitted_playback = (
                lease in self.sequence_playback_leases
                and lease in self.sequence_admitted_chunks
            )
            self.sequence_playback_leases.discard(lease)
            if not admitted_playback and (
                cursor is None or cursor.state != StoryCursorState.PLAYING
            ):
                return False
            successful = isinstance(outcome, PlaybackOutcome) and outcome.successful
            audible = bool(
                isinstance(outcome, PlaybackOutcome)
                and isinstance(outcome.first_audio_ms, (int, float))
                and not isinstance(outcome.first_audio_ms, bool)
            )
            terminal = (successful or audible) and not interrupted_for_pause
            if terminal and isinstance(outcome, PlaybackOutcome):
                route = str(outcome.audio_source or "unknown")
                if cursor is None:
                    self.sequence_event_terminal_routes.setdefault(lease, route)
                elif not self._seal_sequence_route_locked(cursor, lease, route):
                    return False
            if cursor is None or cursor.state != StoryCursorState.PLAYING:
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
                    self.sequence_event_terminal_routes.get(lease) if terminal else None
                ),
                outcome="completed" if successful else "failed",
            )
            self._publish_live_sequence_status()
            return successful

    def _canonical_observed_character(
        self, character: str | None, text: str | None = None
    ) -> str:
        original = str(character or "Narrator").strip() or "Narrator"
        with self.story_cursor_lock:
            if self._expected_sequence_prefix_line_locked() is not None:
                return original
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

        return self._canonical_narrator_fallback_character(original)

    def _canonical_narrator_fallback_character(self, original: str) -> str:
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
            if self.shutdown_requested.is_set():
                return False
            if key in self.primed_voice_keys:
                return False
            future = self.speech_executor.submit(prime, character)
            self.primed_voice_keys.add(key)
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
        if self.shutdown_requested.is_set():
            return False
        reader = self.live_reader
        if isinstance(reader, LiveDialogReader) and not reader.is_running:
            with self.diagnostic_lock:
                snapshot = self.last_diagnostic
            if (
                snapshot is not None
                and snapshot.image is not None
                and (snapshot.character, snapshot.text) == (character, text)
            ):
                from vntts.capture_provenance import CaptureProvenance

                provenance = snapshot.capture_provenance
                frame = CapturedDialogFrame(
                    snapshot.image,
                    snapshot.capture_ms or 0,
                    self._capture_ocr_regions(),
                    CaptureProvenance(**provenance)
                    if isinstance(provenance, dict)
                    else None,
                )
                captured_at = (
                    frame.provenance.completed_monotonic if frame.provenance else None
                )
                with reader.state_lock:
                    reader.latest_frame = frame
                    reader.latest_frame_fingerprint = fingerprint_dialog_frame(frame)
                    reader.latest_frame_visible = dialog_glyphs_visible(frame)
                    reader.latest_frame_captured_at = captured_at
                    reader.frame_version += 1
                    reader._record_ocr_completion(
                        frame, reader.frame_version, captured_at
                    )
                    reader._record_route_observation(
                        frame, reader.frame_version, captured_at, "ocr"
                    )
            else:
                reader.observation_frame = None
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

    def _current_canonical_occurrence_owned_locked(self) -> bool:
        cursor = self.story_cursor
        return bool(
            cursor is not None
            and self._live_sequence_audio_active()
            and cursor.state in {StoryCursorState.LOCKED, StoryCursorState.PLAYING}
            and self._canonical_sequence_line_locked(cursor.current_event_id)
            is not None
        )

    def _ocr_uncertain(
        self,
        result: OCRResult,
        minimum_confidence: float,
        *,
        observation_is_current: Callable[[], bool] | None = None,
    ) -> None:
        with ExitStack() as cleanup:
            with self.story_cursor_lock:
                reader = self.live_reader
                with reader.state_lock if reader is not None else nullcontext():
                    if (
                        observation_is_current is not None
                        and not observation_is_current()
                    ):
                        return
                    self.observation_serial += 1
                    self.indexed_current_recovery = None
                    transition_owned = (
                        self._expected_sequence_prefix_line_locked() is not None
                    )
                    if not transition_owned:
                        self.sequence_prefix_dispatch_lease = None
                    if reader is not None and not transition_owned:
                        # Uncertain OCR cannot revoke speech admitted by a trusted frame.
                        reader.invalidate_observation()
                        chunk = reader.current_chunk or next(
                            iter(reader.speech_futures.values()), None
                        )
                        context = {} if chunk is None else reader._chunk_context(chunk)
                        context.pop("generation", None)
                        cleanup.callback(
                            reader._report_pipeline_event,
                            "observation-invalidated",
                            reader.active_generation,
                            monotonic(),
                            reason="ocr-uncertain",
                            ocr_confidence=result.confidence,
                            minimum_confidence=minimum_confidence,
                            speech_future_count=len(reader.speech_futures),
                            current_chunk_present=reader.current_chunk is not None,
                            suppressed_generation=reader.suppressed_generation,
                            **context,
                        )
        self.status_handler(
            f"OCR uncertain: {result.confidence:.0f}% (requires {minimum_confidence}%). "
            "Waiting for clearer text."
        )

    def _resolve_voice_label(self, character: str) -> str:
        return str(resolve_voice_label(self.voice_router, character))

    def _is_game_focused(self) -> bool:
        return (
            self.settings.capture_mode == "window"
            and self.capture_target is not None
            and self.capture_target.is_focused()
        )

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
            reader = self.live_reader
            visual_guard = self._reader_visual_dispatch_guard(reader)
            with reader.state_lock if reader is not None else nullcontext():
                refusal = self._reader_auto_advance_refusal_locked(reader)
                if refusal is not None:
                    return refusal
                if not visual_guard():
                    return AutoAdvanceAttempt(False, "visual-wait")
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
            self.sequence_prefix_dispatch_lease = lease
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

    def _reader_visual_dispatch_guard(
        self, reader: LiveDialogReader | None
    ) -> Callable[[], bool]:
        if reader is None or reader.require_visible_auto_advance is not True:
            return lambda: True
        with reader.state_lock:
            latest, routed = reader.latest_frame, reader.routed_frame
            run_id = reader.run_id
            version, epoch = reader.frame_version, reader.frame_route_epoch
            same = reader.latest_frame_fingerprint == reader.routed_frame_fingerprint
        if latest is not None and routed is not None and not same:
            try:
                same = reader.frame_equivalent(routed, latest)
            except Exception as error:
                same = False
                self.error_handler(error)
        return lambda: bool(
            same
            and latest is not None
            and routed is not None
            and reader.latest_frame_visible
            and reader.run_id == run_id
            and reader.frame_version == version
            and reader.frame_route_epoch == epoch
            and reader.latest_frame is latest
            and reader.routed_frame is routed
        )

    @staticmethod
    def _reader_auto_advance_refusal_locked(
        reader: LiveDialogReader | None,
    ) -> AutoAdvanceAttempt | None:
        if reader is None:
            return None
        if reader.observation_uncertain is True:
            return AutoAdvanceAttempt(False, "visual-wait")
        if (
            reader.paused is True
            or reader.emergency_stopped is True
            or reader.is_shutting_down is True
            or reader.stop_event.is_set() is True
            or reader.suppressed_generation == reader.active_generation
        ):
            return AutoAdvanceAttempt(False, "reading-control-barrier")
        return None

    def _auto_advance_dialog(
        self, *, focus_verified: bool = False
    ) -> bool | AutoAdvanceAttempt:
        if (
            not self.settings.auto_advance_enabled
            or not auto_advance_allowed(
                self.settings.capture_mode,
                self.settings.live_sequence_mode,
            )
            or (not focus_verified and not self._is_game_focused())
        ):
            return False
        reader = None if focus_verified else self.live_reader
        visual_guard = self._reader_visual_dispatch_guard(reader)
        with reader.state_lock if reader is not None else nullcontext():
            refusal = self._reader_auto_advance_refusal_locked(reader)
            if refusal is not None:
                return refusal
            if not visual_guard():
                return AutoAdvanceAttempt(False, "visual-wait")
            DialogueAdvancer(self.settings.auto_advance_key).advance()
        return True

    def _initialize_live_sequence_state(self) -> None:
        self.sequence_admitted_chunks: dict[SequenceEventLease, SpeechChunk] = {}
        self.sequence_playback_leases: set[SequenceEventLease] = set()
        self.last_sequence_prefix_gate = None
        self.indexed_current_recovery = None
        self.observation_serial = 0
        self.story_cursor_lock = RLock()
        self.live_sequence_plan = None
        self.story_cursor = None
        self.explicit_sequence_anchor_pending = False
        self.sequence_prefix_confirmation_event_id = None
        self._load_live_sequence_plan()

    def _auto_advance_state_changed(
        self, state: str, _generation: int, _attempt: object
    ) -> None:
        with self.story_cursor_lock:
            awaiting_manual_boundary = bool(
                self.story_cursor is not None
                and self.story_cursor.deterministic_manual_successor() is not None
            )
        messages = {
            "focus-wait": "Auto advance is waiting; focus the selected game window",
            "typing": "The game advanced; waiting for the next line to finish typing",
            "recognition-wait": "The game image changed; waiting to identify the next dialogue",
            "successor-unavailable": "The game image changed but the next dialogue is unavailable; read the current line or continue manually",
            "visual-wait": "Auto advance is waiting for the current line to finish rendering",
            "blocked": "Auto advance was blocked because the current cursor event no longer owns one safe automatic transition. Resynchronize manually.",
            "confirmed": "Auto advance confirmed by new dialogue",
        }
        if state in messages:
            self.status_handler(messages[state])
        elif state == "preparation-failed":
            self._report_preparation_failure(_generation)
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

    def _report_preparation_failure(self, generation: int) -> None:
        reader = self.live_reader
        if reader is None:
            return
        with reader.state_lock:
            if (
                generation == reader.active_generation
                and generation == reader.auto_advance_blocked_generation
            ):
                self.status_handler(
                    "Audio preparation failed after retry; this dialogue was not played. "
                    "Check Support and logs, or restart reading to retry."
                )

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
        blocked_reason = None
        reader = self.live_reader
        if isinstance(reader, LiveDialogReader):
            with reader.state_lock:
                if reader.auto_advance_blocked_generation == reader.active_generation:
                    blocked_reason = reader.auto_advance_block_reason
        if lost_focus:
            with self.story_cursor_lock:
                self.sequence_prefix_dispatch_lease = None
            self.status_handler(
                "Game focus lost; live capture and auto advance paused"
                + (f". {blocked_reason}" if blocked_reason else "")
            )
        elif regained_focus:
            self.status_handler(
                blocked_reason or "Game focus restored; live reading resumed"
            )

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
        with self.diagnostic_lock:
            snapshot = self._diagnostic_with_current_state(
                snapshot, pipeline_metrics, route_metrics, audio_source
            )
            self.last_diagnostic = snapshot
        if notify:
            self.diagnostic_handler(snapshot)
        return snapshot

    def _diagnostic_with_current_state(
        self,
        snapshot: DiagnosticSnapshot,
        pipeline_metrics: LivePipelineMetrics | None,
        route_metrics: _DiagnosticRouteMetrics | None,
        audio_source: str | None,
    ) -> DiagnosticSnapshot:
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
        return snapshot

    def _prepare_live_chunk(self, chunk: SpeechChunk) -> object:
        prepared = None
        try:
            prepare_route = getattr(self.speech_backend, "prepare_route", None)
            prepare_playback = getattr(self.speech_backend, "prepare_playback", None)
            if callable(prepare_route):
                prepared = (
                    prepare_route(
                        chunk.character,
                        chunk.text,
                        line_id=chunk.line_id,
                    )
                    if isinstance(self.speech_backend, GeneratedAudioFallbackBackend)
                    else prepare_route(chunk.character, chunk.text)
                )
            elif callable(prepare_playback):
                prepared = prepare_playback(chunk.character, chunk.text)
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
            if (
                isinstance(prepared, LiveTTSRoute)
                and prepared.trace.match_result == "no-match"
                and prepared.trace.line_id is None
                and chunk.line_id is None
                and not chunk.explicit_replay
                and self.live_reader is not None
            ):
                self.live_reader.allow_unheard_replacement(chunk)
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
        if self._live_sequence_playback_suppressed(reader, chunk, sequence_lease):
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
        interrupted_for_pause = False
        context_token = audio_lifecycle_context.set(
            {
                "session_id": self.live_reader_session_id,
                **(
                    {"run_id": chunk.run_id or reader.run_id}
                    if isinstance(chunk.run_id or getattr(reader, "run_id", None), str)
                    else {}
                ),
                "playback_id": chunk.playback_id,
                "capture_id": chunk.capture_id,
                "generation": chunk.generation,
                "chunk_id": chunk.chunk_id,
            }
        )
        try:
            outcome = self._dispatch_live_playback(reader, chunk, audio)
            result = outcome.successful
            interrupted_for_pause = (
                outcome.status is PlaybackStatus.INTERRUPTED
                and reader.was_playback_paused(chunk) is True
            )
            audible = isinstance(
                outcome.first_audio_ms, (int, float)
            ) and not isinstance(outcome.first_audio_ms, bool)
            if not result and not chunk.explicit_replay and not interrupted_for_pause:
                reader.block_auto_advance_for_generation(
                    chunk.generation,
                    "Playback was interrupted; retry or wait for a new dialogue",
                )
            if (
                (result or audible)
                and not chunk.explicit_replay
                and not interrupted_for_pause
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
            self._report_live_playback_outcome(chunk, outcome, source_audio_lead_ms)
            onset = outcome.onset_observation(playback_started)
            if onset is not None:
                reader.record_first_pcm(onset[0], provenance=onset[1], chunk=chunk)
            if outcome.status is PlaybackStatus.FAILED:
                raise_playback_failure(outcome)
            if generation_limited:
                self.status_handler(
                    "MOSS stopped at the dialogue safety limit; the line was not "
                    "cached. Auto advance remains safe after playback completes."
                )
            self._observe_live_playback_backpressure(underflowed)
            return bool(result)
        finally:
            audio_lifecycle_context.reset(context_token)
            self._finish_sequence_playback(
                sequence_lease, outcome, interrupted_for_pause=interrupted_for_pause
            )
            self._refresh_diagnostic_metrics(outcome, source)

    def _live_sequence_playback_suppressed(
        self,
        reader: LiveDialogReader,
        chunk: SpeechChunk,
        sequence_lease: SequenceEventLease | None,
    ) -> bool:
        if (
            chunk.explicit_replay
            or not self._live_sequence_audio_active()
            or chunk.line_id is None
            or sequence_lease is not None
        ):
            return False
        self.pipeline_event_handler(
            "sequence-playback-suppressed",
            reader.active_generation,
            monotonic(),
            line_id=chunk.line_id,
            reason="cursor-does-not-own-unplayed-line",
            outcome="suppressed",
        )
        self.status_handler(
            f"Duplicate or stale canonical audio suppressed: {chunk.line_id}"
        )
        return True

    def _live_playback_guard(
        self, reader: LiveDialogReader, chunk: SpeechChunk, audio: object
    ) -> bool:
        if not reader.wait_until_playable(chunk):
            return False
        if not isinstance(audio, (SourceAudioRoute, PreparedSourceAudioPassThrough)):
            return True
        if chunk.sequence_event_id is None or chunk.sequence_occurrence_id is None:
            return reader.active_generation == chunk.generation
        with self.story_cursor_lock:
            return (
                self._sequence_cursor_for_lease_locked(
                    SequenceEventLease(
                        chunk.sequence_event_id, chunk.sequence_occurrence_id
                    )
                )
                is not None
            )

    def _dispatch_live_playback(
        self, reader: LiveDialogReader, chunk: SpeechChunk, audio: object
    ) -> PlaybackOutcome:
        play_route = getattr(self.speech_backend, "play_route", None)
        play_prepared = getattr(self.speech_backend, "play_prepared", None)
        outcome = None
        playback_guard = partial(self._live_playback_guard, reader, chunk, audio)
        try:
            if isinstance(audio, RouteDecision):
                if callable(play_route):
                    outcome = play_route(
                        audio,
                        playback_guard=playback_guard,
                    )
            if (
                outcome is None
                and callable(play_prepared)
                and isinstance(audio, PreparedPlayback)
            ):
                outcome = play_prepared(
                    audio,
                    playback_guard=playback_guard,
                )
        except PcmAdmissionRejected:
            return PlaybackOutcome(PlaybackStatus.INTERRUPTED, None)
        if outcome is None:
            raise TypeError("Speech backend does not implement typed playback")
        if not isinstance(outcome, PlaybackOutcome):
            raise TypeError("Speech backend returned an untyped playback outcome")
        if (
            outcome.status is PlaybackStatus.FAILED
            and outcome.error_type is PcmAdmissionRejected
        ):
            outcome = replace(
                outcome, status=PlaybackStatus.INTERRUPTED, error=None, error_type=None
            )
        return outcome

    def _report_live_playback_outcome(
        self,
        chunk: SpeechChunk,
        outcome: PlaybackOutcome,
        source_audio_lead_ms: int | None,
    ) -> None:
        playback_telemetry = {
            "outcome": outcome.status.value,
            "underflowed": outcome.underflowed,
            "generation_limited": outcome.generation_limited,
            "synthesis_ms": outcome.synthesis_ms,
            "playback_ms": outcome.playback_ms,
            "first_audio_ms": outcome.first_audio_ms,
            "onset_provenance": (
                outcome.onset_provenance
                if outcome.first_audio_at is not None
                else "estimated"
                if outcome.first_audio_ms is not None
                else "unobserved"
            ),
            "end_provenance": outcome.end_provenance,
            "measurement": "software-output; physical-onset-unobserved",
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
            prepare = getattr(backend, "prepare_playback", None)
            if not callable(prepare):
                raise TypeError(
                    "Live backend does not implement typed speaker announcements"
                )
            prepared = prepare("Narrator", f"{announcement_speaker}.")
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
        play_route = getattr(backend, "play_route", None)
        live_backend = getattr(backend, "live_backend", backend)
        play_prepared = getattr(live_backend, "play_prepared", None)
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
                    route,
                    playback_guard=lambda: reader.wait_until_playable(chunk),
                )
            else:
                assert callable(play_prepared)
                outcome = play_prepared(
                    route.prepared,
                    playback_guard=lambda: reader.wait_until_playable(chunk),
                )
        except PcmAdmissionRejected:
            outcome = PlaybackOutcome(PlaybackStatus.INTERRUPTED, None)
        finally:
            audio_lifecycle_context.reset(context_token)
        if not isinstance(outcome, PlaybackOutcome):
            raise TypeError("Live backend returned an untyped announcement outcome")
        if (
            outcome.status is PlaybackStatus.FAILED
            and outcome.error_type is PcmAdmissionRejected
        ):
            outcome = replace(
                outcome, status=PlaybackStatus.INTERRUPTED, error=None, error_type=None
            )
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
            try:
                raise_playback_failure(outcome)
            except Exception as error:
                self.error_handler(error)
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
        reader = self.live_reader
        pipeline_metrics = None if reader is None else reader.get_pipeline_metrics()
        with self.diagnostic_lock:
            snapshot = self.last_diagnostic
            if snapshot is None:
                return
            snapshot = self._diagnostic_with_current_state(
                snapshot, pipeline_metrics, route_metrics, audio_source
            )
            self.last_diagnostic = snapshot
        self.diagnostic_handler(snapshot)

    def _interrupt_speech(self) -> bool | None:
        stop = getattr(self.speech_backend, "stop", None)
        if not callable(stop):
            stop = getattr(self.tts, "stop", None)
        return bool(stop()) if callable(stop) else False
