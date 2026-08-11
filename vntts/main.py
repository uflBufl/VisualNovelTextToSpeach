import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import blake2b
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from uuid import uuid4

import mss
from PIL import Image
from pynput import keyboard

from vntts.assets import ModelAssetManager
from vntts.auto_advance import DialogueAdvancer
from vntts.diagnostics import DiagnosticSnapshot, resolve_voice_label
from vntts.dialog import is_empty, speak_dialog
from vntts.history import DialogueHistory
from vntts.hotkeys import (
    HotkeyValidationError,
    validate_hotkey_assignments,
)
from vntts.hotkeys import (
    default_hotkey as default_hotkey_for_key,
)
from vntts.live import (
    AdaptiveSpeechBackpressure,
    IncrementalDialogTracker,
    LiveDialogReader,
)
from vntts.ocr import (
    OCRResult,
    UncertainFrameRecorder,
    default_minimum_ocr_confidence,
    get_dialog_region,
    recognize_dialog_image_result,
)
from vntts.ocr_backend import TesseractOCRBackend
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.services.tts_engine import (
    AudioPlaybackError,
    TTSEngine,
    TTSError,
    default_tts_profile,
    get_tts_profile,
)
from vntts.settings import AppSettings, load_app_settings
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    PocketTTSVoiceRouterBackend,
    XTTSVoiceRouterBackend,
)
from vntts.voices import (
    CharacterVoiceRegistry,
    CharacterVoiceRouter,
    VoiceManifestError,
    find_default_voice_manifest,
    normalize_character_name,
)
from vntts.window_capture import (
    WindowCaptureTarget,
    enable_windows_dpi_awareness,
    ensure_screen_capture_supported,
)

default_screenshot_directory = Path("logs/screenshots")
default_hotkey = default_hotkey_for_key("h")
default_live_hotkey = default_hotkey_for_key("l")
default_pause_hotkey = default_hotkey_for_key("p")
default_skip_hotkey = default_hotkey_for_key("s")
default_repeat_hotkey = default_hotkey_for_key("r")
default_clear_queue_hotkey = default_hotkey_for_key("x")
default_live_interval_ms = 200
default_live_stability_frames = 2
default_live_idle_flush_ms = 400
default_live_min_chunk_characters = 20
tts_environment_variables = {
    "model_name": "VNTTS_TTS_MODEL",
    "speaker": "VNTTS_TTS_SPEAKER",
    "language": "VNTTS_TTS_LANGUAGE",
    "speaker_wav": "VNTTS_TTS_SPEAKER_WAV",
}


class ScreenCaptureError(RuntimeError):
    pass


class OCRError(RuntimeError):
    pass


class OCRUncertainError(OCRError):
    def __init__(self, result, minimum_confidence):
        self.result = result
        self.minimum_confidence = minimum_confidence
        super().__init__(
            f"confidence {result.confidence:.0f}% is below the required "
            f"{minimum_confidence}%"
        )


class TTSInitializationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CapturedDialogFrame:
    image: Image.Image
    capture_ms: float


def get_screenshot_directory(settings=None):
    if settings is not None:
        return Path(settings.screenshot_directory).expanduser()

    configured_directory = os.environ.get("VNTTS_SCREENSHOT_DIR")
    if not configured_directory:
        return default_screenshot_directory

    return Path(configured_directory)


def create_screenshot_path(screenshot_directory):
    screenshot_directory = Path(screenshot_directory)
    formatted_date = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return screenshot_directory / f"dialog-{formatted_date}-{uuid4().hex}.png"


def capture_dialog(
    screenshot_directory=None,
    *,
    save_screenshot=True,
    region=None,
    capture_target=None,
):
    try:
        ensure_screen_capture_supported()
        if screenshot_directory is None:
            screenshot_directory = get_screenshot_directory()
        screenshot_directory = Path(screenshot_directory)
        screenshot_directory.mkdir(parents=True, exist_ok=True)

        with mss.mss() as sct:
            region = region or get_dialog_region()
            dialog_box = (
                capture_target.capture_box(region)
                if capture_target is not None
                else region.capture_box(sct.monitors[1])
            )
            screenshot = sct.grab(dialog_box)

            image = Image.frombytes(
                "RGB",
                screenshot.size,
                screenshot.bgra,
                "raw",
                "BGRX",
            )

        output = None
        if save_screenshot:
            output = create_screenshot_path(screenshot_directory)
            image.save(output)
        return image, output
    except Exception as error:
        raise ScreenCaptureError(str(error)) from error


def recognize_screenshot_result(
    image,
    voice_registry=None,
    minimum_confidence=default_minimum_ocr_confidence,
    ocr_language="eng",
    correction_dictionary=None,
    ocr_backend=None,
):
    try:
        backend = ocr_backend or TesseractOCRBackend(recognize_dialog_image_result)
        result = backend.recognize(
            image,
            voice_registry,
            minimum_confidence=minimum_confidence,
            language=ocr_language,
        )
        return (
            correction_dictionary.correct_result(result)
            if correction_dictionary is not None
            else result
        )
    except Exception as error:
        raise OCRError(str(error)) from error


def capture_live_frame(screenshot_directory, capture_target=None, *, clock=monotonic):
    capture_started = clock()
    image, _output = capture_dialog(
        screenshot_directory,
        save_screenshot=False,
        capture_target=capture_target,
    )
    return CapturedDialogFrame(
        image=image,
        capture_ms=(clock() - capture_started) * 1000,
    )


def fingerprint_dialog_frame(frame):
    """Cheap fingerprint that preserves small glyph changes after downsampling."""
    grayscale = frame.image.convert("L").resize(
        (256, 64),
        Image.Resampling.LANCZOS,
    )
    # Coarse quantization ignores insignificant capture noise, while retaining
    # several intensity levels so a new sentence cannot disappear like it did
    # in the previous 96x54 binary mask.
    quantized = grayscale.point(tuple((value // 16) * 16 for value in range(256)))
    return blake2b(quantized.tobytes(), digest_size=16).digest()


def recognize_live_frame(
    frame,
    voice_registry=None,
    minimum_confidence=default_minimum_ocr_confidence,
    uncertain_handler=None,
    uncertain_frame_recorder=None,
    diagnostic_handler=None,
    voice_resolver=None,
    ocr_language="eng",
    correction_dictionary=None,
    *,
    clock=monotonic,
):
    ocr_started = clock()
    result = recognize_screenshot_result(
        frame.image,
        voice_registry,
        minimum_confidence,
        ocr_language,
        correction_dictionary,
    )
    ocr_ms = (clock() - ocr_started) * 1000
    snapshot = DiagnosticSnapshot(
        image=frame.image,
        character=result.character or "Narrator",
        text=result.text,
        confidence=result.confidence,
        preprocessing_profile=result.profile,
        voice=(
            voice_resolver(result.character)
            if voice_resolver is not None
            else "Not loaded"
        ),
        capture_ms=frame.capture_ms,
        ocr_ms=ocr_ms,
        corrections=result.corrections,
        choice_detected=result.choice_detected,
    )
    if diagnostic_handler is not None:
        diagnostic_handler(snapshot)
    if result.text and not result.is_confident(minimum_confidence):
        if uncertain_frame_recorder is not None:
            uncertain_frame_recorder.record(
                frame.image,
                result,
                minimum_confidence,
            )
        if uncertain_handler is not None:
            uncertain_handler(result, minimum_confidence)
        return None, ""
    if uncertain_frame_recorder is not None:
        uncertain_frame_recorder.reset()
    return result.character, result.text


def recognize_screenshot(
    image,
    voice_registry=None,
    minimum_confidence=default_minimum_ocr_confidence,
    ocr_language="eng",
    correction_dictionary=None,
):
    result = recognize_screenshot_result(
        image,
        voice_registry,
        minimum_confidence,
        ocr_language,
        correction_dictionary,
    )
    if result.text and not result.is_confident(minimum_confidence):
        raise OCRUncertainError(result, minimum_confidence)
    return result.character, result.text


def analyze_dialog_snapshot(
    screenshot_directory,
    voice_registry=None,
    capture_target=None,
    minimum_confidence=default_minimum_ocr_confidence,
    *,
    save_screenshot=False,
    diagnostic_handler=None,
    voice_resolver=None,
    clock=monotonic,
    ocr_language="eng",
    correction_dictionary=None,
):
    capture_started = clock()
    image, output = capture_dialog(
        screenshot_directory,
        save_screenshot=save_screenshot,
        capture_target=capture_target,
    )
    capture_ms = (clock() - capture_started) * 1000

    ocr_started = clock()
    result = recognize_screenshot_result(
        image,
        voice_registry,
        minimum_confidence,
        ocr_language,
        correction_dictionary,
    )
    ocr_ms = (clock() - ocr_started) * 1000
    snapshot = DiagnosticSnapshot(
        image=image,
        character=result.character or "Narrator",
        text=result.text,
        confidence=result.confidence,
        preprocessing_profile=result.profile,
        voice=(
            voice_resolver(result.character)
            if voice_resolver is not None
            else "Not loaded"
        ),
        capture_ms=capture_ms,
        ocr_ms=ocr_ms,
        corrections=result.corrections,
        choice_detected=result.choice_detected,
    )
    if diagnostic_handler is not None:
        diagnostic_handler(snapshot)
    return image, output, result


def read_dialog(
    voice_router,
    screenshot_directory,
    capture_target=None,
    speech_handler=None,
    minimum_confidence=default_minimum_ocr_confidence,
    uncertain_frame_recorder=None,
    diagnostic_handler=None,
    voice_resolver=None,
    ocr_language="eng",
    correction_dictionary=None,
):
    image, output, result = analyze_dialog_snapshot(
        screenshot_directory,
        voice_router.registry,
        capture_target=capture_target,
        minimum_confidence=minimum_confidence,
        save_screenshot=True,
        diagnostic_handler=diagnostic_handler,
        voice_resolver=voice_resolver,
        ocr_language=ocr_language,
        correction_dictionary=correction_dictionary,
    )
    if result.text and not result.is_confident(minimum_confidence):
        error = OCRUncertainError(result, minimum_confidence)
        if uncertain_frame_recorder is not None:
            uncertain_frame_recorder.record(
                image,
                error.result,
                minimum_confidence,
            )
        raise error
    character, text = result.character, result.text
    if uncertain_frame_recorder is not None:
        uncertain_frame_recorder.reset()

    if is_empty(text):
        print(f"Screenshot {output} has no text")
    else:
        print(f"{character} is speaking now")
        print(f"Screenshot {output} with text:\n{text}")

        if speech_handler is None:
            speak_dialog(text, lambda value: voice_router.speak(character, value))
        else:
            speech_handler(character, text)


def read_dialog_safely(
    voice_router,
    screenshot_directory,
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
    try:
        read_dialog(
            voice_router,
            screenshot_directory,
            capture_target,
            speech_handler,
            minimum_confidence,
            uncertain_frame_recorder,
            diagnostic_handler,
            voice_resolver,
            ocr_language,
            correction_dictionary,
        )
    except Exception as error:
        (error_handler or report_runtime_error)(error)


def format_runtime_error(error):
    if isinstance(error, ScreenCaptureError):
        message = f"Screen capture failed: {error}"
    elif isinstance(error, OCRError):
        message = f"Tesseract OCR failed: {error}"
    elif isinstance(error, TTSInitializationError):
        message = f"Unable to initialize TTS engine: {error}"
    elif isinstance(error, TTSError):
        message = f"TTS model or synthesis failed: {error}"
    elif isinstance(error, AudioPlaybackError):
        message = f"Audio playback failed: {error}"
    elif isinstance(error, VoiceManifestError):
        message = f"Voice configuration failed: {error}"
    else:
        message = f"Unexpected dialog processing failure: {error}"
    return message


def report_runtime_error(error):
    print(format_runtime_error(error), file=sys.stderr)


def get_validated_hotkey(environment_variable, default):
    hotkey = os.environ.get(environment_variable, default)
    try:
        keyboard.HotKey.parse(hotkey)
    except (TypeError, ValueError) as error:
        print(
            f"Invalid {environment_variable} {hotkey!r}: {error}. "
            f"Using default {default!r}"
        )
        return default

    return hotkey


def get_hotkey(settings=None):
    if settings is None:
        return get_validated_hotkey("VNTTS_HOTKEY", default_hotkey)
    return validate_hotkey(settings.read_hotkey, default_hotkey, "read hotkey")


def get_live_hotkey(settings=None):
    if settings is None:
        return get_validated_hotkey("VNTTS_LIVE_HOTKEY", default_live_hotkey)
    return validate_hotkey(settings.live_hotkey, default_live_hotkey, "live hotkey")


def get_pause_hotkey(settings=None):
    if settings is None:
        return get_validated_hotkey("VNTTS_PAUSE_HOTKEY", default_pause_hotkey)
    return validate_hotkey(settings.pause_hotkey, default_pause_hotkey, "pause hotkey")


def get_skip_hotkey(settings=None):
    if settings is None:
        return get_validated_hotkey("VNTTS_SKIP_HOTKEY", default_skip_hotkey)
    return validate_hotkey(settings.skip_hotkey, default_skip_hotkey, "skip hotkey")


def get_repeat_hotkey(settings=None):
    if settings is None:
        return get_validated_hotkey("VNTTS_REPEAT_HOTKEY", default_repeat_hotkey)
    return validate_hotkey(
        settings.repeat_hotkey,
        default_repeat_hotkey,
        "repeat hotkey",
    )


def get_clear_queue_hotkey(settings=None):
    if settings is None:
        return get_validated_hotkey(
            "VNTTS_CLEAR_QUEUE_HOTKEY",
            default_clear_queue_hotkey,
        )
    return validate_hotkey(
        settings.clear_queue_hotkey,
        default_clear_queue_hotkey,
        "clear queue hotkey",
    )


def validate_hotkey(hotkey, default, label):
    try:
        keyboard.HotKey.parse(hotkey)
    except (TypeError, ValueError) as error:
        print(f"Invalid {label} {hotkey!r}: {error}. Using default {default!r}")
        return default
    return hotkey


def get_numeric_environment_variable(environment_variable, default, *, minimum):
    configured_value = os.environ.get(environment_variable)
    if not configured_value:
        return default

    try:
        value = int(configured_value)
    except ValueError:
        value = None

    if value is None or value < minimum:
        print(
            f"Invalid {environment_variable} {configured_value!r}; "
            f"using default {default}"
        )
        return default
    return value


def get_live_configuration(settings=None):
    if settings is not None:
        return {
            "interval_seconds": settings.live_interval_ms / 1000,
            "tracker_options": {
                "stability_frames": settings.live_stability_frames,
                "idle_flush_seconds": settings.live_idle_flush_ms / 1000,
                "min_chunk_characters": settings.live_min_chunk_characters,
            },
        }

    interval_ms = get_numeric_environment_variable(
        "VNTTS_LIVE_INTERVAL_MS",
        default_live_interval_ms,
        minimum=1,
    )
    idle_flush_ms = get_numeric_environment_variable(
        "VNTTS_LIVE_IDLE_FLUSH_MS",
        default_live_idle_flush_ms,
        minimum=1,
    )
    stability_frames = get_numeric_environment_variable(
        "VNTTS_LIVE_STABILITY_FRAMES",
        default_live_stability_frames,
        minimum=2,
    )
    min_chunk_characters = get_numeric_environment_variable(
        "VNTTS_LIVE_MIN_CHUNK_CHARACTERS",
        default_live_min_chunk_characters,
        minimum=1,
    )
    return {
        "interval_seconds": interval_ms / 1000,
        "tracker_options": {
            "stability_frames": stability_frames,
            "idle_flush_seconds": idle_flush_ms / 1000,
            "min_chunk_characters": min_chunk_characters,
        },
    }


def get_tts_configuration(settings=None):
    if settings is not None:
        configuration = {
            name: value
            for name, value in {
                "model_name": settings.tts_model,
                "speaker": settings.tts_speaker,
                "language": settings.tts_language,
                "speaker_wav": settings.tts_speaker_wav,
                "volume": settings.output_volume_percent / 100,
            }.items()
            if value
        }
        configuration["volume"] = settings.output_volume_percent / 100
        if settings.tts_model and "xtts" in settings.tts_model.casefold():
            profile_name = settings.tts_profile
            try:
                configuration["synthesis_options"] = get_tts_profile(profile_name)
            except ValueError as error:
                print(f"{error}. Using {default_tts_profile!r}", file=sys.stderr)
                configuration["synthesis_options"] = get_tts_profile(
                    default_tts_profile
                )
        configuration.setdefault("synthesis_options", {})["speed"] = (
            settings.speech_rate_percent / 100
        )
        return configuration

    configuration = {
        argument: value
        for argument, environment_variable in tts_environment_variables.items()
        if (value := os.environ.get(environment_variable))
    }
    profile_name = os.environ.get("VNTTS_TTS_PROFILE")
    if profile_name:
        profile_name = profile_name.strip().casefold()
        try:
            configuration["synthesis_options"] = get_tts_profile(profile_name)
        except ValueError as error:
            print(f"{error}. Using {default_tts_profile!r}", file=sys.stderr)
            configuration["synthesis_options"] = get_tts_profile(default_tts_profile)
    return configuration


def initialize_voice_registry(settings=None, error_handler=None):
    manifest_path = (
        settings.voice_manifest
        if settings is not None
        else os.environ.get("VNTTS_VOICE_MANIFEST")
    )
    if not manifest_path:
        manifest_path = find_default_voice_manifest()
    try:
        registry = (
            CharacterVoiceRegistry.from_file(manifest_path)
            if manifest_path
            else CharacterVoiceRegistry()
        )
    except VoiceManifestError as error:
        if error_handler is None:
            print(f"Unable to initialize character voices: {error}", file=sys.stderr)
        else:
            error_handler(error)
        return None

    return registry


def initialize_voice_router(tts, settings=None, error_handler=None):
    registry = initialize_voice_registry(settings, error_handler)
    if registry is None:
        return None
    return CharacterVoiceRouter(
        tts,
        registry,
        narrator_speaker=(
            settings.narrator_speaker
            if settings is not None
            else os.environ.get("VNTTS_NARRATOR_SPEAKER")
        ),
    )


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


def listen_for_hotkeys(
    hotkey,
    live_hotkey,
    pause_hotkey,
    skip_hotkey,
    repeat_hotkey,
    clear_queue_hotkey,
    on_activate,
    on_live_toggle,
    on_pause_toggle,
    on_skip,
    on_repeat,
    on_clear_queue,
):
    print(f"Press {hotkey} to read from screen once")
    print(f"Press {live_hotkey} to start or stop live reading")
    print(f"Press {pause_hotkey} to pause or resume speech")
    print(f"Press {skip_hotkey} to skip current speech")
    print(f"Press {repeat_hotkey} to repeat the last speech")
    print(f"Press {clear_queue_hotkey} to clear the speech queue")
    with keyboard.GlobalHotKeys(
        {
            hotkey: on_activate,
            live_hotkey: on_live_toggle,
            pause_hotkey: on_pause_toggle,
            skip_hotkey: on_skip,
            repeat_hotkey: on_repeat,
            clear_queue_hotkey: on_clear_queue,
        }
    ) as listener:
        listener.join()


def initialize_tts(tts_factory=TTSEngine):
    print("Loading TTS model...")
    try:
        tts = tts_factory(**get_tts_configuration())
    except Exception as error:
        print(f"Unable to initialize TTS engine: {error}", file=sys.stderr)
        return None

    print("TTS model loaded")
    return tts


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
                self.tts = backend_factory(
                    registry,
                    narrator_reference=self.settings.tts_speaker_wav,
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
        accepted = self.schedule_dialog_read()
        if accepted:
            self.status_handler("Reading current dialog")
        return accepted

    def toggle_live(self):
        if not self.is_ready:
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
        self.refresh_corrections()
        self.capture_target = self._create_capture_target()
        self.uncertain_frame_recorder = self._create_uncertain_frame_recorder()
        if self.tts is not None:
            self.tts.set_volume(self.settings.output_volume_percent / 100)
            self.tts.set_speed(self.settings.speech_rate_percent / 100)
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
        self.history.add(character, text)
        preview = text if len(text) <= 100 else f"{text[:97]}..."
        self.dialog_handler(character or "Narrator", preview)

    def _offer_unknown_speaker_mapping(self, character):
        key = normalize_character_name(character)
        if not key or key == "narrator" or self.voice_router is None:
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
        with self.voice_prime_lock:
            if key in self.primed_voice_keys:
                return False
            self.primed_voice_keys.add(key)
            future = self.speech_executor.submit(prime, character)
            self.voice_prime_futures.add(future)
        future.add_done_callback(self._voice_prime_finished)
        return True

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

    def _publish_diagnostic(self, snapshot):
        snapshot = replace(
            snapshot,
            capture_interval_ms=self.capture_interval_ms,
            game_focused=self.game_focused,
        )
        if self.tts is not None:
            snapshot = replace(
                snapshot,
                synthesis_ms=getattr(self.tts, "last_synthesis_ms", None),
                playback_ms=getattr(self.tts, "last_playback_ms", None),
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


def main(tts_factory=TTSEngine):
    enable_windows_dpi_awareness()
    settings = load_app_settings()
    controller = AppController(settings, tts_factory=tts_factory)
    if not controller.start():
        return 1

    hotkey = get_hotkey(settings)
    live_hotkey = get_live_hotkey(settings)
    pause_hotkey = get_pause_hotkey(settings)
    skip_hotkey = get_skip_hotkey(settings)
    repeat_hotkey = get_repeat_hotkey(settings)
    clear_queue_hotkey = get_clear_queue_hotkey(settings)
    try:
        validate_hotkey_assignments(
            {
                "Read once": hotkey,
                "Live reading": live_hotkey,
                "Pause or resume": pause_hotkey,
                "Skip speech": skip_hotkey,
                "Repeat speech": repeat_hotkey,
                "Clear queue": clear_queue_hotkey,
            }
        )
    except HotkeyValidationError as error:
        print(f"Invalid hotkeys: {error}", file=sys.stderr)
        controller.shutdown()
        return 1

    try:
        listen_for_hotkeys(
            hotkey,
            live_hotkey,
            pause_hotkey,
            skip_hotkey,
            repeat_hotkey,
            clear_queue_hotkey,
            controller.read_once,
            controller.toggle_live,
            controller.toggle_speech_pause,
            controller.skip_current_speech,
            controller.repeat_last_speech,
            controller.clear_speech_queue,
        )
    finally:
        controller.shutdown()

    return 0
