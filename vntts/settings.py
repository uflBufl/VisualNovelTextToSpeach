import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Self, TypeAlias, TypedDict, Unpack

from vntts.application_directories import (
    application_directory_name as application_directory_name,
)
from vntts.application_directories import get_config_directory, get_local_data_directory
from vntts.hotkeys import default_hotkey
from vntts.versioned_json import load_versioned_json, write_versioned_json
from vntts.voices import is_narrator

settings_schema_version = 29
main_sections = ("stories", "voices", "reading")

audio_source_policies = {
    "live-tts-only",
    "prefer-generated",
    "prefer-game-audio",
}
default_audio_source_policy = "live-tts-only"
speaker_announcement_modes = {
    "off",
    "all-speakers",
    "narrator-fallback-roles",
}
live_sequence_audio_modes = frozenset({"audio-manual", "audio-auto"})
live_sequence_modes = {"off", "shadow", *live_sequence_audio_modes}

PathInput: TypeAlias = str | Path
Environment: TypeAlias = Mapping[str, str]
WarningHandler: TypeAlias = Callable[[str], None]


class AppSettingsChanges(TypedDict, total=False):
    schema_version: int
    onboarding_completed: bool
    xtts_terms_accepted: bool
    pocket_gated_model_accepted: bool
    read_hotkey: str
    live_hotkey: str
    pause_hotkey: str
    skip_hotkey: str
    repeat_hotkey: str
    clear_queue_hotkey: str
    emergency_stop_hotkey: str
    screenshot_directory: str
    ocr_diagnostics_directory: str
    retain_uncertain_frames: bool
    capture_mode: str
    game_window_title: str | None
    live_interval_ms: int
    live_stability_frames: int
    live_idle_flush_ms: int
    live_min_chunk_characters: int
    auto_advance_enabled: bool
    speaker_announcement_mode: str
    announce_speaker_changes: bool
    auto_advance_key: str
    auto_advance_delay_ms: int
    ocr_minimum_confidence: int
    ocr_language: str
    speech_backend: str
    audio_source_policy: str
    tts_model: str | None
    tts_speaker: str | None
    tts_language: str | None
    tts_speaker_wav: str | None
    tts_profile: str
    output_volume_percent: int
    speech_rate_percent: int
    warm_up_voices: bool
    launch_at_login: bool
    keep_running_on_close: bool
    compact_controls: bool
    last_main_section: str
    game_pack: str | None
    voice_manifest: str | None
    story_index: str | None
    live_sequence_plan: str | None
    live_sequence_mode: str
    live_speaker_corpus: str | None
    generated_audio_manifest: str | None
    narrator_speaker: str | None
    voice_assignments: dict[str, str]
    character_voice_defaults: dict[str, str]
    force_live_narrator: bool
    active_profile_id: str | None


def is_live_sequence_audio_mode(value: object) -> bool:
    return value in live_sequence_audio_modes


restart_required_setting_names = (
    "speech_backend",
    "tts_model",
    "tts_speaker",
    "tts_language",
    "tts_speaker_wav",
    "voice_manifest",
    "narrator_speaker",
    "pocket_gated_model_accepted",
    "character_voice_defaults",
)


def get_settings_path(*, environment: Environment | None = None) -> Path:
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
    pocket_gated_model_accepted: bool = False
    read_hotkey: str = field(default_factory=lambda: default_hotkey("h"))
    live_hotkey: str = field(default_factory=lambda: default_hotkey("l"))
    pause_hotkey: str = field(default_factory=lambda: default_hotkey("p"))
    skip_hotkey: str = field(default_factory=lambda: default_hotkey("s"))
    repeat_hotkey: str = field(default_factory=lambda: default_hotkey("r"))
    clear_queue_hotkey: str = field(default_factory=lambda: default_hotkey("x"))
    emergency_stop_hotkey: str = field(default_factory=lambda: default_hotkey("e"))
    screenshot_directory: str = field(
        default_factory=lambda: str(get_local_data_directory() / "screenshots"),
        metadata={"support_sensitivity": "path"},
    )
    ocr_diagnostics_directory: str = field(
        default_factory=lambda: str(get_local_data_directory() / "ocr-diagnostics"),
        metadata={"support_sensitivity": "path"},
    )
    retain_uncertain_frames: bool = False
    capture_mode: str = "screen"
    game_window_title: str | None = None
    live_interval_ms: int = 200
    live_stability_frames: int = 2
    live_idle_flush_ms: int = 400
    live_min_chunk_characters: int = 20
    auto_advance_enabled: bool = True
    speaker_announcement_mode: str = "off"
    # Compatibility for callers and settings written before schema 24.
    announce_speaker_changes: bool = False
    auto_advance_key: str = "space"
    auto_advance_delay_ms: int = 350
    ocr_minimum_confidence: int = 60
    ocr_language: str = "eng"
    speech_backend: str = "pocket-tts"
    audio_source_policy: str = default_audio_source_policy
    tts_model: str | None = field(
        default=None,
        metadata={"support_sensitivity": "path-or-id"},
    )
    tts_speaker: str | None = None
    tts_language: str | None = None
    tts_speaker_wav: str | None = field(
        default=None,
        metadata={"support_sensitivity": "path"},
    )
    tts_profile: str = "stable"
    output_volume_percent: int = 100
    speech_rate_percent: int = 100
    warm_up_voices: bool = False
    launch_at_login: bool = False
    keep_running_on_close: bool = False
    compact_controls: bool = False
    last_main_section: str = "stories"
    game_pack: str | None = field(
        default=None,
        metadata={"support_sensitivity": "path"},
    )
    voice_manifest: str | None = field(
        default=None,
        metadata={"support_sensitivity": "path"},
    )
    story_index: str | None = field(
        default=None,
        metadata={"support_sensitivity": "path"},
    )
    live_sequence_plan: str | None = field(
        default=None,
        metadata={"support_sensitivity": "path"},
    )
    live_sequence_mode: str = "audio-auto"
    live_speaker_corpus: str | None = field(
        default=None,
        metadata={"support_sensitivity": "path"},
    )
    generated_audio_manifest: str | None = field(
        default=None,
        metadata={"support_sensitivity": "path"},
    )
    narrator_speaker: str | None = None
    voice_assignments: dict[str, str] = field(default_factory=dict)
    character_voice_defaults: dict[str, str] = field(default_factory=dict)
    force_live_narrator: bool = False
    active_profile_id: str | None = None

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, object],
        *,
        warn: WarningHandler | None = None,
    ) -> Self:
        report: WarningHandler = (lambda _message: None) if warn is None else warn
        defaults = cls()
        parsed: dict[str, object] = {}
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
            "audio_source_policy",
            "speaker_announcement_mode",
            "live_sequence_mode",
            "emergency_stop_hotkey",
        )
        optional_string_fields = (
            "tts_model",
            "tts_speaker",
            "tts_language",
            "tts_speaker_wav",
            "game_pack",
            "voice_manifest",
            "story_index",
            "live_sequence_plan",
            "live_speaker_corpus",
            "generated_audio_manifest",
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
            "pocket_gated_model_accepted",
            "retain_uncertain_frames",
            "warm_up_voices",
            "launch_at_login",
            "keep_running_on_close",
            "compact_controls",
            "auto_advance_enabled",
            "announce_speaker_changes",
            "force_live_narrator",
        )

        for name in string_fields:
            value = values.get(name, getattr(defaults, name))
            if isinstance(value, str) and value.strip():
                parsed[name] = value.strip()
            else:
                report(f"Invalid {name!r} setting; using its default")

        for name in optional_string_fields:
            value = values.get(name, getattr(defaults, name))
            if value is None:
                parsed[name] = None
            elif isinstance(value, str) and value.strip():
                parsed[name] = value.strip()
            else:
                report(f"Invalid {name!r} setting; using its default")

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
                report(f"Invalid {name!r} setting; using its default")

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
                report(f"Invalid {name!r} setting; using its default")

        # Schema 27 promotes guarded sequence control for new installations.
        # Settings created before that rollout retain their former conservative
        # behavior when either field was not explicitly persisted.
        if source_schema < 27:
            if "live_sequence_mode" not in values:
                parsed["live_sequence_mode"] = "off"
            if "auto_advance_enabled" not in values:
                parsed["auto_advance_enabled"] = False

        profile = values.get("tts_profile", defaults.tts_profile)
        if isinstance(profile, str) and profile.strip():
            parsed["tts_profile"] = profile.strip().casefold()
        else:
            report("Invalid 'tts_profile' setting; using its default")

        capture_mode = values.get("capture_mode", defaults.capture_mode)
        if capture_mode in {"screen", "window"}:
            parsed["capture_mode"] = capture_mode
        else:
            report("Invalid 'capture_mode' setting; using its default")

        last_main_section = values.get("last_main_section", defaults.last_main_section)
        if last_main_section in main_sections:
            parsed["last_main_section"] = last_main_section
        else:
            report("Invalid 'last_main_section' setting; using Stories")

        if parsed["auto_advance_key"] not in {"space", "enter", "right", "down"}:
            report("Invalid 'auto_advance_key' setting; using its default")
            parsed["auto_advance_key"] = defaults.auto_advance_key

        if parsed["speech_backend"] not in {
            "coqui-xtts",
            "chatterbox-nano",
            "moss-tts",
            "pocket-tts",
        }:
            report("Invalid 'speech_backend' setting; using its default")
            parsed["speech_backend"] = defaults.speech_backend

        if parsed["audio_source_policy"] not in audio_source_policies:
            report("Invalid 'audio_source_policy' setting; using its default")
            parsed["audio_source_policy"] = defaults.audio_source_policy

        if parsed["speaker_announcement_mode"] not in speaker_announcement_modes:
            report("Invalid 'speaker_announcement_mode' setting; using its default")
            parsed["speaker_announcement_mode"] = defaults.speaker_announcement_mode
        if parsed["live_sequence_mode"] not in live_sequence_modes:
            report("Invalid 'live_sequence_mode' setting; disabling sequence control")
            parsed["live_sequence_mode"] = "off"
        if (
            "speaker_announcement_mode" not in values
            and parsed["announce_speaker_changes"]
        ):
            parsed["speaker_announcement_mode"] = "all-speakers"

        validated_assignments: dict[str, dict[str, str]] = {}
        for name in ("voice_assignments", "character_voice_defaults"):
            assignments = values.get(name, getattr(defaults, name))
            if isinstance(assignments, dict) and all(
                isinstance(character, str)
                and character.strip()
                and isinstance(source_id, str)
                and source_id.strip()
                for character, source_id in assignments.items()
            ):
                validated_assignments[name] = {
                    character.strip(): source_id.strip()
                    for character, source_id in assignments.items()
                }
            else:
                report(f"Invalid {name!r} setting; using its default")
                validated_assignments[name] = {}
            parsed[name] = validated_assignments[name]
        character_voice_defaults = validated_assignments["character_voice_defaults"]
        for character in tuple(character_voice_defaults):
            if is_narrator(character):
                report("Narrator cannot be set in 'character_voice_defaults'")
                del character_voice_defaults[character]

        # Before schema 22 a saved Narrator assignment always bypassed source
        # and pregenerated audio. Preserve that behavior during migration while
        # making the routing choice explicit for newly saved settings.
        if source_schema < 22 and any(
            character.strip().casefold() == "narrator"
            for character in validated_assignments["voice_assignments"]
        ):
            parsed["force_live_narrator"] = True

        # The keys and values above are validated dynamically from versioned JSON;
        # typeshed cannot express that mapping through dataclasses.replace.
        replace_settings: Callable[..., Self] = replace
        return replace_settings(defaults, **parsed)

    @property
    def effective_speaker_announcement_mode(self) -> str:
        if self.speaker_announcement_mode != "off":
            return self.speaker_announcement_mode
        if self.announce_speaker_changes:
            return "all-speakers"
        return "off"

    def with_environment_overrides(
        self,
        environment: Environment | None = None,
        *,
        warn: WarningHandler | None = None,
    ) -> Self:
        environment = os.environ if environment is None else environment
        report: WarningHandler = (lambda _message: None) if warn is None else warn
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
            "VNTTS_GAME_PACK": "game_pack",
            "VNTTS_VOICE_MANIFEST": "voice_manifest",
            "VNTTS_STORY_INDEX": "story_index",
            "VNTTS_LIVE_SEQUENCE_PLAN": "live_sequence_plan",
            "VNTTS_LIVE_SPEAKER_CORPUS": "live_speaker_corpus",
            "VNTTS_GENERATED_AUDIO_MANIFEST": "generated_audio_manifest",
            "VNTTS_NARRATOR_SPEAKER": "narrator_speaker",
            "VNTTS_OCR_LANGUAGE": "ocr_language",
            "VNTTS_SPEECH_BACKEND": "speech_backend",
            "VNTTS_AUDIO_SOURCE_POLICY": "audio_source_policy",
            "VNTTS_LIVE_SEQUENCE_MODE": "live_sequence_mode",
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
                report(
                    f"Invalid {environment_name} {configured!r}; using saved/default value"
                )

        return self.from_mapping(values, warn=report)

    def save(self, path: PathInput | None = None) -> Path:
        path = get_settings_path() if path is None else Path(path).expanduser()
        write_versioned_json(path, settings_schema_version, asdict(self))
        return path

    def updated(self, **changes: Unpack[AppSettingsChanges]) -> Self:
        return replace(self, **changes)


def restart_required_setting_changes(
    current: object, requested: object
) -> tuple[str, ...]:
    """Return runtime-bound fields that cannot change in the current process."""
    if not isinstance(current, AppSettings) or not isinstance(requested, AppSettings):
        raise TypeError("Restart comparison requires AppSettings values")
    return tuple(
        name
        for name in restart_required_setting_names
        if getattr(current, name) != getattr(requested, name)
    )


def preserve_loaded_runtime_settings(
    current: AppSettings, requested: AppSettings
) -> AppSettings:
    """Apply ordinary settings while retaining the loaded runtime identity."""
    changes = restart_required_setting_changes(current, requested)
    if not changes:
        return requested
    return requested.updated(**{name: getattr(current, name) for name in changes})


def load_app_settings(
    path: PathInput | None = None,
    *,
    environment: Environment | None = None,
    warn: WarningHandler | None = None,
    on_game_pack_error: Callable[[Exception], None] | None = None,
) -> AppSettings:
    environment = os.environ if environment is None else environment
    report: WarningHandler = (
        (lambda message: print(message, file=sys.stderr)) if warn is None else warn
    )
    path = get_settings_path(environment=environment) if path is None else Path(path)

    settings = load_versioned_json(
        path,
        schema_version=settings_schema_version,
        document_name="settings",
        decode=lambda values: AppSettings.from_mapping(values, warn=report),
        fallback=AppSettings,
        warn=report,
        allow_older=True,
        allow_unversioned=True,
    )

    settings = settings.with_environment_overrides(environment, warn=report)
    if settings.game_pack:
        from vntts.game_pack import GamePackError, apply_game_pack

        try:
            settings = apply_game_pack(settings)
        except (GamePackError, OSError) as error:
            if on_game_pack_error is None:
                raise
            on_game_pack_error(error)
    return settings
