"""Runtime configuration and speech backend construction."""

import os
import sys

from pynput import keyboard

from vntts.hotkeys import default_hotkey as default_hotkey_for_key
from vntts.services.tts_engine import (
    TTSEngine,
    default_tts_profile,
    get_tts_profile,
)
from vntts.voices import (
    CharacterVoiceRegistry,
    CharacterVoiceRouter,
    VoiceManifestError,
    find_default_voice_manifest,
    normalize_character_name,
    pocket_tts_preset_voices,
)

default_hotkey = default_hotkey_for_key("h")
default_live_hotkey = default_hotkey_for_key("l")
default_pause_hotkey = default_hotkey_for_key("p")
default_skip_hotkey = default_hotkey_for_key("s")
default_repeat_hotkey = default_hotkey_for_key("r")
default_clear_queue_hotkey = default_hotkey_for_key("x")
default_emergency_stop_hotkey = default_hotkey_for_key("e")
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


def get_emergency_stop_hotkey(settings=None):
    if settings is None:
        return get_validated_hotkey(
            "VNTTS_EMERGENCY_STOP_HOTKEY",
            default_emergency_stop_hotkey,
        )
    return validate_hotkey(
        settings.emergency_stop_hotkey,
        default_emergency_stop_hotkey,
        "emergency stop hotkey",
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

    if settings is not None:
        preset_validator = None
        if settings.speech_backend == "pocket-tts":
            preset_validator = pocket_tts_preset_voices.__contains__
        elif settings.speech_backend in {"chatterbox-nano", "moss-tts"}:
            preset_validator = ().__contains__
        registry.apply_assignments(
            settings.voice_assignments,
            warn=(
                (lambda message: error_handler(VoiceManifestError(message)))
                if error_handler is not None
                else (lambda message: print(message, file=sys.stderr))
            ),
            preset_validator=preset_validator,
        )

    return registry


def initialize_voice_router(tts, settings=None, error_handler=None):
    registry = initialize_voice_registry(settings, error_handler)
    if registry is None:
        return None
    if settings is not None:
        for character, voice in tuple(registry.assignments.items()):
            if (
                voice is not None
                and not voice.references
                and not tts.has_speaker(voice.speaker)
            ):
                registry.assignments.pop(character)
                error = VoiceManifestError(
                    f"Voice {voice.speaker!r} is not available in the loaded XTTS model"
                )
                if error_handler is not None:
                    error_handler(error)
                else:
                    print(error, file=sys.stderr)
    return CharacterVoiceRouter(
        tts,
        registry,
        narrator_speaker=(
            settings.narrator_speaker
            if settings is not None
            else os.environ.get("VNTTS_NARRATOR_SPEAKER")
        ),
        narrator_voice=(
            registry.resolve("Narrator")
            if settings is not None
            and any(
                normalize_character_name(character) == "narrator"
                for character in settings.voice_assignments
            )
            else None
        ),
    )


def initialize_tts(tts_factory=TTSEngine):
    print("Loading TTS model...")
    try:
        tts = tts_factory(**get_tts_configuration())
    except Exception as error:
        print(f"Unable to initialize TTS engine: {error}", file=sys.stderr)
        return None

    print("TTS model loaded")
    return tts
