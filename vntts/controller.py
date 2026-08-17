"""Application controller and live-reading orchestration."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from difflib import SequenceMatcher
from threading import Event, Lock
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
    IncrementalDialogTracker,
    LiveDialogReader,
)
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
from vntts.settings import AppSettings
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    MossTTSPreparedSpeech,
    MossTTSVoiceRouterBackend,
    PocketTTSVoiceRouterBackend,
    XTTSVoiceRouterBackend,
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
        unknown_speaker_handler=None,
        error_handler=report_runtime_error,
        capture_target_factory=WindowCaptureTarget,
        model_asset_manager_factory=ModelAssetManager,
        chatterbox_backend_factory=ChatterboxNanoVoiceRouterBackend,
        moss_backend_factory=MossTTSVoiceRouterBackend,
        pocket_backend_factory=PocketTTSVoiceRouterBackend,
        speech_backpressure_factory=AdaptiveSpeechBackpressure,
        correction_store=None,
        history=None,
        chapter_voice_preloader=None,
        generated_audio_library_factory=GeneratedAudioLibrary.load_optional,
        generated_audio_backend_factory=GeneratedAudioFallbackBackend,
        route_trace_handler=None,
        pipeline_event_handler=None,
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
        self.generated_audio_library_factory = generated_audio_library_factory
        self.generated_audio_backend_factory = generated_audio_backend_factory
        self.route_trace_handler = route_trace_handler or (lambda _trace: None)
        self.pipeline_event_handler = pipeline_event_handler or (
            lambda _stage, _generation, _occurred_at, **_details: None
        )
        self.tts_factory = tts_factory
        self.status_handler = status_handler
        self.dialog_handler = dialog_handler or status_handler
        self.diagnostic_handler = diagnostic_handler or (lambda _snapshot: None)
        self.unknown_speaker_handler = unknown_speaker_handler or (lambda _name: None)
        self.error_handler = error_handler
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
                speak_chunk=self._speak_live_chunk,
                prepare_chunk=self._prepare_live_chunk,
                play_prepared=self._play_live_chunk,
                report_error=self.error_handler,
                interrupt_speech=self._interrupt_speech,
                dialog_observed=self._dialog_observed,
                focus_probe=self._is_game_focused,
                capture_state_changed=self._capture_state_changed,
                tracker_factory=IncrementalDialogTracker,
                auto_advance=(
                    self._auto_advance_dialog
                    if self.settings.auto_advance_enabled
                    else None
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
        unresolved = self.unresolved_live_speakers()
        if unresolved is None:
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
            self.speech_backend.require_source_audio_completion = bool(enabled)
        if self.live_reader is not None:
            self.live_reader.set_auto_advance(
                self._auto_advance_dialog if enabled else None
            )
        self.status_handler(
            "Auto advance enabled" if enabled else "Auto advance disabled"
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
        self.settings = self.settings.updated(voice_assignments=assignments)
        if character_key == "narrator":
            self._apply_narrator_voice(None)
        self._clear_voice_runtime_cache()
        self.status_handler(
            "Pregenerated narrator tracks enabled when available"
            if character_key == "narrator"
            else f"Automatic voice routing restored for {character}"
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
        if scope is None:
            return None
        unresolved = []
        seen = set()
        for line in scope:
            character = str(line.speaker or "").strip()
            key = normalize_character_name(character)
            if key in seen or not self._speaker_requires_voice_decision(
                character,
                line.text,
                live_preflight=True,
            ):
                continue
            seen.add(key)
            unresolved.append(character)
        return tuple(unresolved)

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
        was_live = self.is_live_running
        if was_live:
            self._set_backend_live_mode(False)
            self.live_reader.stop()
            self.live_reader.wait()

        self.settings = settings
        self.chapter_voice_preloader = ChapterVoicePreloader.load_optional(
            self.settings.story_index
        )
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
        self.live_reader.set_auto_advance(
            self._auto_advance_dialog if self.settings.auto_advance_enabled else None
        )
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
            self._set_backend_live_mode(True)
            self.live_reader.start()

    def _get_live_configuration(self):
        configuration = get_live_configuration(self.settings)
        tracker_options = dict(configuration["tracker_options"])
        tracker_options["complete_dialogue_only"] = (
            self.settings.audio_source_policy != "live-tts-only"
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
                self._resolve_early_generated_dialogue
            )
        return {**configuration, "tracker_options": tracker_options}

    def _resolve_early_generated_dialogue(self, character, text):
        backend = self.speech_backend
        if not isinstance(backend, GeneratedAudioFallbackBackend):
            return None
        if self._has_manual_voice_override(character):
            return None
        line = self.chapter_voice_preloader.resolve_unique_prefix(
            character,
            text,
            candidate_filter=backend.has_generated_line,
        )
        return line.text if line is not None else None

    def _has_manual_voice_override(self, character):
        if is_unattributed_speaker(character):
            return False
        return (
            find_voice_assignment(self.settings.voice_assignments, character)
            is not None
        )

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
            backend_options["require_source_audio_completion"] = (
                self.settings.auto_advance_enabled
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
        )
        return self._canonical_observed_character(character), text

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
        if isinstance(self.voice_router, PocketTTSVoiceRouterBackend):
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
        character = self._canonical_observed_character(character)
        speech_deferred = self._offer_unknown_speaker_mapping(character, text)
        self._prime_observed_voice(character)
        self._prime_likely_chapter_voice(character, text)
        self.history.add(character, text)
        preview = text if len(text) <= 100 else f"{text[:97]}..."
        self.dialog_handler(character or "Narrator", preview)
        return not speech_deferred

    def _canonical_observed_character(self, character):
        original = str(character or "Narrator").strip() or "Narrator"
        canonicalize = getattr(self.chapter_voice_preloader, "canonical_speaker", None)
        if callable(canonicalize):
            canonical = canonicalize(original)
            if isinstance(canonical, str) and normalize_character_name(
                canonical
            ) != normalize_character_name(original):
                return canonical

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
        self._dialog_observed(character, text)
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

    def _auto_advance_dialog(self):
        if not self.settings.auto_advance_enabled or not self._is_game_focused():
            return False
        DialogueAdvancer(self.settings.auto_advance_key).advance()
        return True

    def _auto_advance_state_changed(self, state, _generation, _attempt):
        if state == "dispatched":
            self.status_handler("Auto advance key sent; waiting for dialogue change")
        elif state == "waiting":
            self.status_handler(
                "The game is still changing; auto advance is continuing to wait. "
                "No second key will be sent."
            )
        elif state == "failed":
            self.status_handler(
                "Dialogue change was not confirmed after the extended wait; no "
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
                prepared = prepare_route(
                    self.speech_backend, chunk.character, chunk.text
                )
            elif callable(prepare_playback):
                prepared = prepare_playback(
                    self.speech_backend, chunk.character, chunk.text
                )
            else:
                raise TypeError("Speech backend does not implement prepare_playback()")
            self.last_audio_source_description = self._describe_audio_source(prepared)
            trace = self._build_audio_route_trace(chunk, prepared)
            self.last_audio_route_trace = trace
            try:
                self.route_trace_handler(trace)
            except Exception as error:
                self.error_handler(error)
            self._record_pipeline_route(chunk, trace)
            return prepared
        finally:
            self._refresh_diagnostic_metrics(
                prepared
                if isinstance(
                    prepared,
                    (
                        GeneratedAudioRoute,
                        SourceAudioRoute,
                        LiveTTSRoute,
                        PreparedPlayback,
                    ),
                )
                else None,
                self._describe_audio_source(prepared) if prepared is not None else None,
            )

    def _play_live_chunk(self, chunk, audio):
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
                    audio, (GeneratedAudioRoute, SourceAudioRoute, LiveTTSRoute)
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
            if result and isinstance(
                audio,
                (
                    GeneratedAudioRoute,
                    SourceAudioRoute,
                    PreparedGeneratedAudio,
                    PreparedSourceAudioPassThrough,
                ),
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
            first_audio_ms = outcome.first_audio_ms
            if isinstance(first_audio_ms, (int, float)) and not isinstance(
                first_audio_ms, bool
            ):
                self.live_reader.record_first_pcm(
                    playback_started + first_audio_ms / 1000
                )
            return result
        finally:
            self._refresh_diagnostic_metrics(outcome, source)

    def _describe_audio_source(self, prepared):
        if isinstance(prepared, (SourceAudioRoute, GeneratedAudioRoute, LiveTTSRoute)):
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
                prepared, (SourceAudioRoute, GeneratedAudioRoute, LiveTTSRoute)
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
                prepared, (SourceAudioRoute, GeneratedAudioRoute, LiveTTSRoute)
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
            and isinstance(live_backend, MossTTSVoiceRouterBackend)
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
        if self.tts is not None and hasattr(self.tts, "stop"):
            try:
                self.tts.stop()
            except Exception as error:
                self.error_handler(error)
        self.tts = None
        self.voice_router = None
        self.speech_backend = None

    def _interrupt_speech(self):
        if self.tts is not None and hasattr(self.tts, "stop"):
            return self.tts.stop()
        return False
