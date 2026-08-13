import json
import os
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from platformdirs import user_config_path, user_data_path

from vntts.atomic_io import atomic_write_json
from vntts.hotkeys import default_hotkey

application_directory_name = "VisualNovelTextToSpeech"
settings_schema_version = 16


def _platform_app_name():
    return (
        application_directory_name if sys.platform in {"darwin", "win32"} else "vntts"
    )


def get_config_directory():
    return user_config_path(_platform_app_name(), appauthor=False, roaming=True)


def get_local_data_directory():
    return user_data_path(_platform_app_name(), appauthor=False)


def get_settings_path(*, environment=None):
    environment = os.environ if environment is None else environment
    configured_path = environment.get("VNTTS_SETTINGS_FILE")
    if configured_path:
        return Path(configured_path).expanduser()
    return get_config_directory() / "settings.json"


@dataclass(frozen=True)
class AppSettings:
    schema_version: int = settings_schema_version
    onboarding_completed: bool = False
    xtts_terms_accepted: bool = False
    read_hotkey: str = field(default_factory=lambda: default_hotkey("h"))
    live_hotkey: str = field(default_factory=lambda: default_hotkey("l"))
    pause_hotkey: str = field(default_factory=lambda: default_hotkey("p"))
    skip_hotkey: str = field(default_factory=lambda: default_hotkey("s"))
    repeat_hotkey: str = field(default_factory=lambda: default_hotkey("r"))
    clear_queue_hotkey: str = field(default_factory=lambda: default_hotkey("x"))
    emergency_stop_hotkey: str = field(default_factory=lambda: default_hotkey("e"))
    screenshot_directory: str = field(
        default_factory=lambda: str(get_local_data_directory() / "screenshots")
    )
    ocr_diagnostics_directory: str = field(
        default_factory=lambda: str(get_local_data_directory() / "ocr-diagnostics")
    )
    retain_uncertain_frames: bool = False
    capture_mode: str = "screen"
    game_window_title: str | None = None
    live_interval_ms: int = 200
    live_stability_frames: int = 2
    live_idle_flush_ms: int = 400
    live_min_chunk_characters: int = 20
    auto_advance_enabled: bool = False
    auto_advance_key: str = "space"
    auto_advance_delay_ms: int = 350
    ocr_minimum_confidence: int = 60
    ocr_language: str = "eng"
    speech_backend: str = "pocket-tts"
    tts_model: str | None = None
    tts_speaker: str | None = None
    tts_language: str | None = None
    tts_speaker_wav: str | None = None
    tts_profile: str = "stable"
    output_volume_percent: int = 100
    speech_rate_percent: int = 100
    warm_up_voices: bool = False
    launch_at_login: bool = False
    keep_running_on_close: bool = False
    compact_controls: bool = False
    voice_manifest: str | None = None
    story_index: str | None = None
    narrator_speaker: str | None = None
    active_profile_id: str | None = None

    @classmethod
    def from_mapping(cls, values, *, warn=None):
        warn = (lambda _message: None) if warn is None else warn
        defaults = cls()
        parsed = {}
        source_schema = values.get("schema_version", 0)
        if isinstance(source_schema, bool) or not isinstance(source_schema, int):
            source_schema = 0

        string_fields = (
            "read_hotkey",
            "live_hotkey",
            "pause_hotkey",
            "skip_hotkey",
            "repeat_hotkey",
            "clear_queue_hotkey",
            "screenshot_directory",
            "ocr_diagnostics_directory",
            "ocr_language",
            "auto_advance_key",
            "speech_backend",
            "emergency_stop_hotkey",
        )
        optional_string_fields = (
            "tts_model",
            "tts_speaker",
            "tts_language",
            "tts_speaker_wav",
            "voice_manifest",
            "story_index",
            "narrator_speaker",
            "game_window_title",
            "active_profile_id",
        )
        numeric_fields = {
            "live_interval_ms": 1,
            "live_stability_frames": 2,
            "live_idle_flush_ms": 1,
            "live_min_chunk_characters": 1,
            "auto_advance_delay_ms": 0,
            "ocr_minimum_confidence": 0,
            "output_volume_percent": 0,
            "speech_rate_percent": 50,
        }
        boolean_fields = (
            "onboarding_completed",
            "xtts_terms_accepted",
            "retain_uncertain_frames",
            "warm_up_voices",
            "launch_at_login",
            "keep_running_on_close",
            "compact_controls",
            "auto_advance_enabled",
        )

        for name in string_fields:
            value = values.get(name, getattr(defaults, name))
            if isinstance(value, str) and value.strip():
                parsed[name] = value.strip()
            else:
                warn(f"Invalid {name!r} setting; using its default")

        for name in optional_string_fields:
            value = values.get(name, getattr(defaults, name))
            if value is None:
                parsed[name] = None
            elif isinstance(value, str) and value.strip():
                parsed[name] = value.strip()
            else:
                warn(f"Invalid {name!r} setting; using its default")

        for name, minimum in numeric_fields.items():
            value = values.get(name, getattr(defaults, name))
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= minimum
                and (name != "ocr_minimum_confidence" or value <= 100)
                and (name != "output_volume_percent" or value <= 100)
                and (name != "speech_rate_percent" or value <= 150)
            ):
                parsed[name] = value
            else:
                warn(f"Invalid {name!r} setting; using its default")

        # Schema 11 shipped the conservative 700ms idle delay as its only
        # effective value. Move that default forward while preserving an
        # explicitly saved 700ms value in current-schema settings.
        if source_schema < 12 and parsed["live_idle_flush_ms"] == 700:
            parsed["live_idle_flush_ms"] = defaults.live_idle_flush_ms

        for name in boolean_fields:
            value = values.get(name, getattr(defaults, name))
            if isinstance(value, bool):
                parsed[name] = value
            else:
                warn(f"Invalid {name!r} setting; using its default")

        profile = values.get("tts_profile", defaults.tts_profile)
        if isinstance(profile, str) and profile.strip():
            parsed["tts_profile"] = profile.strip().casefold()
        else:
            warn("Invalid 'tts_profile' setting; using its default")

        capture_mode = values.get("capture_mode", defaults.capture_mode)
        if capture_mode in {"screen", "window"}:
            parsed["capture_mode"] = capture_mode
        else:
            warn("Invalid 'capture_mode' setting; using its default")

        if parsed["auto_advance_key"] not in {"space", "enter", "right", "down"}:
            warn("Invalid 'auto_advance_key' setting; using its default")
            parsed["auto_advance_key"] = defaults.auto_advance_key

        if parsed["speech_backend"] not in {
            "coqui-xtts",
            "chatterbox-nano",
            "pocket-tts",
        }:
            warn("Invalid 'speech_backend' setting; using its default")
            parsed["speech_backend"] = defaults.speech_backend

        return cls(**parsed)

    def with_environment_overrides(self, environment=None, *, warn=None):
        environment = os.environ if environment is None else environment
        warn = (lambda _message: None) if warn is None else warn
        values = asdict(self)
        string_overrides = {
            "VNTTS_HOTKEY": "read_hotkey",
            "VNTTS_LIVE_HOTKEY": "live_hotkey",
            "VNTTS_PAUSE_HOTKEY": "pause_hotkey",
            "VNTTS_SKIP_HOTKEY": "skip_hotkey",
            "VNTTS_REPEAT_HOTKEY": "repeat_hotkey",
            "VNTTS_CLEAR_QUEUE_HOTKEY": "clear_queue_hotkey",
            "VNTTS_EMERGENCY_STOP_HOTKEY": "emergency_stop_hotkey",
            "VNTTS_SCREENSHOT_DIR": "screenshot_directory",
            "VNTTS_OCR_DIAGNOSTICS_DIR": "ocr_diagnostics_directory",
            "VNTTS_TTS_MODEL": "tts_model",
            "VNTTS_TTS_SPEAKER": "tts_speaker",
            "VNTTS_TTS_LANGUAGE": "tts_language",
            "VNTTS_TTS_SPEAKER_WAV": "tts_speaker_wav",
            "VNTTS_TTS_PROFILE": "tts_profile",
            "VNTTS_VOICE_MANIFEST": "voice_manifest",
            "VNTTS_STORY_INDEX": "story_index",
            "VNTTS_NARRATOR_SPEAKER": "narrator_speaker",
            "VNTTS_OCR_LANGUAGE": "ocr_language",
            "VNTTS_SPEECH_BACKEND": "speech_backend",
            "VNTTS_CAPTURE_MODE": "capture_mode",
            "VNTTS_GAME_WINDOW_TITLE": "game_window_title",
        }
        numeric_overrides = {
            "VNTTS_LIVE_INTERVAL_MS": "live_interval_ms",
            "VNTTS_LIVE_STABILITY_FRAMES": "live_stability_frames",
            "VNTTS_LIVE_IDLE_FLUSH_MS": "live_idle_flush_ms",
            "VNTTS_LIVE_MIN_CHUNK_CHARACTERS": "live_min_chunk_characters",
            "VNTTS_OCR_MINIMUM_CONFIDENCE": "ocr_minimum_confidence",
            "VNTTS_OUTPUT_VOLUME_PERCENT": "output_volume_percent",
            "VNTTS_SPEECH_RATE_PERCENT": "speech_rate_percent",
        }
        for environment_name, setting_name in string_overrides.items():
            if configured := environment.get(environment_name):
                values[setting_name] = configured

        for environment_name, setting_name in numeric_overrides.items():
            configured = environment.get(environment_name)
            if configured is None:
                continue
            try:
                values[setting_name] = int(configured)
            except ValueError:
                warn(
                    f"Invalid {environment_name} {configured!r}; using saved/default value"
                )

        return self.from_mapping(values, warn=warn)

    def save(self, path=None):
        path = get_settings_path() if path is None else Path(path).expanduser()
        atomic_write_json(path, asdict(self))
        return path

    def updated(self, **changes):
        return replace(self, **changes)


def load_app_settings(path=None, *, environment=None, warn=None):
    environment = os.environ if environment is None else environment
    warn = (lambda message: print(message, file=sys.stderr)) if warn is None else warn
    path = get_settings_path(environment=environment) if path is None else Path(path)

    if not path.is_file():
        settings = AppSettings()
    else:
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(values, dict):
                raise ValueError("settings root must be an object")
            settings = AppSettings.from_mapping(values, warn=warn)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            warn(f"Unable to load settings from {path}: {error}; using defaults")
            settings = AppSettings()

    return settings.with_environment_overrides(environment, warn=warn)
