"""Application controller and live-reading orchestration."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
    GeneratedAudioFallbackBackend,
    GeneratedAudioLibrary,
)
from vntts.history import DialogueHistory
from vntts.live import (
    AdaptiveSpeechBackpressure,
    IncrementalDialogTracker,
    LiveDialogReader,
)
from vntts.ocr import OCRResult, UncertainFrameRecorder, default_minimum_ocr_confidence
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.runtime_config import (
    get_live_configuration,
    get_tts_configuration,
    initialize_voice_registry,
    initialize_voice_router,
)
from vntts.services.tts_engine import TTSEngine
from vntts.settings import AppSettings
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    PocketTTSVoiceRouterBackend,
    XTTSVoiceRouterBackend,
)
from vntts.voices import (
    VoiceChoice,
    default_voice_choice_id,
    find_voice_assignment,
    normalize_character_name,
    pocket_tts_preset_voices,
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
        pocket_backend_factory=PocketTTSVoiceRouterBackend,
        speech_backpressure_factory=AdaptiveSpeechBackpressure,
        correction_store=None,
        history=None,
        chapter_voice_preloader=None,
        generated_audio_library_factory=GeneratedAudioLibrary.load_optional,
        generated_audio_backend_factory=GeneratedAudioFallbackBackend,
    ):
        self.settings = settings or AppSettings()
        self.capture_target_factory = capture_target_factory
        self.model_assets = model_asset_manager_factory()
        self.chatterbox_backend_factory = chatterbox_backend_factory
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
        self.capture_interval_ms = self.settings.live_interval_ms
        self.game_focused = True
        self.diagnostic_lock = Lock()
        self.voice_prime_lock = Lock()
        self.primed_voice_keys = set()
        self.reported_unknown_speakers = set()
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
                if self.settings.speech_backend == "chatterbox-nano":
                    self.model_assets.configure_huggingface_environment()
                registry = initialize_voice_registry(
                    self.settings,
                    self.error_handler,
                )
                if registry is None:
                    return False
                backend_factory = (
                    self.pocket_backend_factory
                    if self.settings.speech_backend == "pocket-tts"
                    else self.chatterbox_backend_factory
                )
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
                self.tts = backend_factory(
                    registry,
                    narrator_reference=narrator_reference,
                    volume=self.settings.output_volume_percent / 100,
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
                max_speech_jobs=max_speech_jobs,
                interrupt_on_dialog_replacement=bool(
                    getattr(
                        backend_capabilities,
                        "interrupt_on_dialog_replacement",
                        False,
                    )
                ),
                first_pcm_on_prepare=not bool(
                    getattr(backend_capabilities, "streaming", False)
                ),
                **get_live_configuration(self.settings),
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
        if not self.live_reader.is_running and self.capture_target is not None:
            try:
                self.capture_target.get_geometry()
            except Exception as error:
                self.error_handler(ScreenCaptureError(str(error)))
                self.status_handler("Live reading could not start")
                return False
        running = self.live_reader.toggle()
        if running:
            self.live_reader.max_speech_jobs = self.live_speech_backpressure.reset()
        self._set_backend_live_mode(running)
        self.status_handler(
            "Live reading started" if running else "Live reading stopping"
        )
        return running

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
        if self.live_reader is not None:
            self.live_reader.auto_advance = (
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
                "Narrator / backend default",
                "Use the same fallback voice as unmapped dialogue",
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
        self.reported_unknown_speakers.discard(normalize_character_name(character))
        self.status_handler(f"{choice.label} assigned to {character}")
        return self.settings

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
                lambda value: self.voice_router.speak(character, value),
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
            if callable(set_volume):
                set_volume(self.settings.output_volume_percent / 100)
            if callable(set_speed):
                set_speed(self.settings.speech_rate_percent / 100)
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
        live_configuration = get_live_configuration(self.settings)
        self.live_reader.interval_seconds = live_configuration["interval_seconds"]
        self.live_reader.tracker_options = live_configuration["tracker_options"]
        self.live_reader.auto_advance = (
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
        if not self.settings.generated_audio_manifest:
            return False
        if not self.settings.story_index:
            self.status_handler(
                "Generated audio disabled: configure a story index for stable line IDs"
            )
            return False
        library = self.generated_audio_library_factory(
            self.settings.generated_audio_manifest,
            warn=self.status_handler,
        )
        if library is None:
            return False
        self.speech_backend = self.generated_audio_backend_factory(
            live_backend,
            library,
            self.chapter_voice_preloader,
            volume=self.settings.output_volume_percent / 100,
            speed=self.settings.speech_rate_percent / 100,
        )
        self.speech_backend.voice_override = lambda character: (
            find_voice_assignment(self.settings.voice_assignments, character)
            is not None
        )
        self.status_handler(
            f"Loaded {len(library.index.entries)} generated audio entries"
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
        return recognize_live_frame(
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

    def _preview_voice(self, character, text):
        try:
            self.voice_router.speak(character, text)
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
            self.voice_router.speak(preview_character, text)
        finally:
            if had_assignment:
                registry.assignments[preview_key] = previous
            else:
                registry.assignments.pop(preview_key, None)
            self._clear_voice_runtime_cache()
            self._refresh_diagnostic_metrics()
        return choice.label, text

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
            return
        self._offer_unknown_speaker_mapping(character)
        self._prime_observed_voice(character)
        self._prime_likely_chapter_voice(character, text)
        self.history.add(character, text)
        preview = text if len(text) <= 100 else f"{text[:97]}..."
        self.dialog_handler(character or "Narrator", preview)

    def _offer_unknown_speaker_mapping(self, character):
        key = normalize_character_name(character)
        if not key or key == "narrator" or self.voice_router is None:
            return False
        assignments = getattr(self.voice_router.registry, "assignments", {})
        if isinstance(assignments, dict) and key in assignments:
            return False
        if self.voice_router.registry.resolve_closest(character) is not None:
            return False
        if key in self.reported_unknown_speakers:
            return False
        self.reported_unknown_speakers.add(key)
        self.unknown_speaker_handler(character.strip())
        return True

    def _prime_observed_voice(self, character):
        prime = getattr(self.speech_backend, "prime", None)
        if not callable(prime) or self.speech_executor is None:
            return False
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
        with self.diagnostic_lock:
            snapshot = self.last_diagnostic
        if snapshot is not None and snapshot.choice_detected:
            self.status_handler("Auto advance paused: choice menu detected")
            return False
        DialogueAdvancer(self.settings.auto_advance_key).advance()
        self.status_handler("Advanced to the next dialogue")
        return True

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

    def _publish_diagnostic(self, snapshot):
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
        metric_source = self.speech_backend or self.tts
        if metric_source is not None:
            snapshot = replace(
                snapshot,
                synthesis_ms=getattr(metric_source, "last_synthesis_ms", None),
                playback_ms=getattr(metric_source, "last_playback_ms", None),
                last_first_audio_ms=getattr(metric_source, "last_first_audio_ms", None),
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
        print(f"Preparing {chunk.character} (live)")
        print(chunk.text)
        try:
            return self.speech_backend.prepare(chunk.character, chunk.text)
        finally:
            self._refresh_diagnostic_metrics()

    def _play_live_chunk(self, chunk, audio):
        print(f"{chunk.character} is speaking now (live)")
        playback_started = monotonic()
        try:
            result = self.speech_backend.play(
                audio,
                playback_guard=lambda: self.live_reader.wait_until_playable(chunk),
            )
            underflowed = bool(
                getattr(self.speech_backend, "last_playback_underrun", False)
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
            first_audio_ms = getattr(self.speech_backend, "last_first_audio_ms", None)
            if isinstance(first_audio_ms, (int, float)) and not isinstance(
                first_audio_ms, bool
            ):
                self.live_reader.record_first_pcm(
                    playback_started + first_audio_ms / 1000
                )
            return result
        finally:
            self._refresh_diagnostic_metrics()

    def _refresh_diagnostic_metrics(self):
        with self.diagnostic_lock:
            snapshot = self.last_diagnostic
        if snapshot is not None:
            self._publish_diagnostic(snapshot)

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
