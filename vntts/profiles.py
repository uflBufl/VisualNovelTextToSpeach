from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Self, TypeAlias
from uuid import uuid4

from vntts.ocr import DialogRegion, get_dialog_region
from vntts.settings import (
    AppSettings,
    audio_source_policies,
    default_audio_source_policy,
    get_config_directory,
    live_sequence_modes,
)
from vntts.versioned_json import load_versioned_json, write_versioned_json
from vntts.voices import is_narrator

profiles_schema_version = 7

PathInput: TypeAlias = str | Path
WarningHandler: TypeAlias = Callable[[str], None]


def get_profiles_path() -> Path:
    return get_config_directory() / "profiles.json"


@dataclass(frozen=True)
class GameProfile:
    id: str
    name: str
    capture_mode: str
    game_window_title: str | None
    dialog_region: DialogRegion
    ocr_language: str
    game_pack: str | None
    voice_manifest: str | None
    story_index: str | None
    live_sequence_plan: str | None
    live_sequence_mode: str
    generated_audio_manifest: str | None
    audio_source_policy: str
    voice_assignments: dict[str, str]
    character_voice_defaults: dict[str, str]
    force_live_narrator: bool

    @classmethod
    def from_settings(
        cls,
        name: object,
        settings: AppSettings,
        *,
        region: DialogRegion | None = None,
        profile_id: str | None = None,
    ) -> Self:
        return cls(
            id=profile_id or uuid4().hex,
            name=_validated_name(name),
            capture_mode=settings.capture_mode,
            game_window_title=settings.game_window_title,
            dialog_region=region or get_dialog_region(),
            ocr_language=settings.ocr_language,
            game_pack=settings.game_pack,
            voice_manifest=settings.voice_manifest,
            story_index=settings.story_index,
            live_sequence_plan=settings.live_sequence_plan,
            live_sequence_mode=settings.live_sequence_mode,
            generated_audio_manifest=settings.generated_audio_manifest,
            audio_source_policy=settings.audio_source_policy,
            voice_assignments=dict(settings.voice_assignments),
            character_voice_defaults=dict(settings.character_voice_defaults),
            force_live_narrator=settings.force_live_narrator,
        )

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, object],
        *,
        source_schema: int = profiles_schema_version,
    ) -> Self:
        region = _dialog_region(values["dialog_region"])
        voice_assignments = _voice_assignments(values.get("voice_assignments"))
        force_live_narrator = values.get("force_live_narrator", False)
        if not isinstance(force_live_narrator, bool):
            raise ValueError("force_live_narrator must be a boolean")
        if source_schema < 5 and any(
            character.casefold() == "narrator" for character in voice_assignments
        ):
            force_live_narrator = True
        live_sequence_mode = values.get("live_sequence_mode")
        recognized_live_sequence_mode = live_sequence_mode in live_sequence_modes
        if not isinstance(live_sequence_mode, str) or not recognized_live_sequence_mode:
            live_sequence_mode = "off"
        return cls(
            id=str(values["id"]),
            name=_validated_name(values["name"]),
            capture_mode=(
                values["capture_mode"]
                if values["capture_mode"] in {"screen", "window"}
                else "screen"
            ),
            game_window_title=_optional_text(values.get("game_window_title")),
            dialog_region=region,
            ocr_language=str(values.get("ocr_language") or "eng").strip(),
            game_pack=_optional_text(values.get("game_pack")),
            voice_manifest=_optional_text(values.get("voice_manifest")),
            story_index=_optional_text(values.get("story_index")),
            live_sequence_plan=_optional_text(values.get("live_sequence_plan")),
            live_sequence_mode=live_sequence_mode,
            generated_audio_manifest=_optional_text(
                values.get("generated_audio_manifest")
            ),
            audio_source_policy=_audio_source_policy(values.get("audio_source_policy")),
            voice_assignments=voice_assignments,
            character_voice_defaults={
                name: source
                for name, source in _voice_assignments(
                    values.get("character_voice_defaults")
                ).items()
                if not is_narrator(name)
            },
            force_live_narrator=force_live_narrator,
        )

    def to_mapping(self) -> dict[str, object]:
        values = asdict(self)
        values["dialog_region"] = self.dialog_region.to_json()
        return values

    def apply(self, settings: AppSettings) -> AppSettings:
        settings = settings.updated(
            active_profile_id=self.id,
            capture_mode=self.capture_mode,
            game_window_title=self.game_window_title,
            ocr_language=self.ocr_language,
            game_pack=self.game_pack,
            voice_manifest=self.voice_manifest,
            story_index=self.story_index,
            live_sequence_plan=self.live_sequence_plan,
            live_sequence_mode=self.live_sequence_mode,
            generated_audio_manifest=self.generated_audio_manifest,
            audio_source_policy=self.audio_source_policy,
            voice_assignments=dict(self.voice_assignments),
            character_voice_defaults=dict(self.character_voice_defaults),
            force_live_narrator=self.force_live_narrator,
        )
        if self.game_pack:
            from vntts.game_pack import apply_game_pack

            settings = apply_game_pack(settings)
        return settings

    def updated_from_settings(
        self, settings: AppSettings, *, region: DialogRegion | None = None
    ) -> Self:
        return replace(
            self,
            capture_mode=settings.capture_mode,
            game_window_title=settings.game_window_title,
            dialog_region=region or get_dialog_region(),
            ocr_language=settings.ocr_language,
            game_pack=settings.game_pack,
            voice_manifest=settings.voice_manifest,
            story_index=settings.story_index,
            live_sequence_plan=settings.live_sequence_plan,
            live_sequence_mode=settings.live_sequence_mode,
            generated_audio_manifest=settings.generated_audio_manifest,
            audio_source_policy=settings.audio_source_policy,
            voice_assignments=dict(settings.voice_assignments),
            character_voice_defaults=dict(settings.character_voice_defaults),
            force_live_narrator=settings.force_live_narrator,
        )


class GameProfileStore:
    def __init__(
        self,
        path: PathInput | None = None,
        profiles: Iterable[GameProfile] = (),
    ) -> None:
        self.path = get_profiles_path() if path is None else Path(path).expanduser()
        self.profiles = list(profiles)

    @classmethod
    def load(
        cls,
        path: PathInput | None = None,
        *,
        warn: WarningHandler | None = None,
    ) -> Self:
        report: WarningHandler = (lambda _message: None) if warn is None else warn
        store = cls(path)

        def decode(payload: dict[str, object]) -> GameProfileStore:
            profile_documents = payload["profiles"]
            if not isinstance(profile_documents, list):
                raise ValueError("profiles must be a list")
            source_schema = payload["schema_version"]
            if isinstance(source_schema, bool) or not isinstance(source_schema, int):
                raise ValueError("profile schema version must be an integer")
            store.profiles = [
                GameProfile.from_mapping(
                    profile,
                    source_schema=source_schema,
                )
                for profile in profile_documents
                if isinstance(profile, dict)
            ]
            if len(store.profiles) != len(profile_documents):
                raise ValueError("profiles must contain objects")
            store._ensure_unique_names()
            return store

        def fallback() -> GameProfileStore:
            store.profiles = []
            return store

        return load_versioned_json(
            store.path,
            schema_version=profiles_schema_version,
            document_name="game profiles",
            decode=decode,
            fallback=fallback,
            warn=report,
            allow_older=True,
        )

    def save(self) -> Path:
        return self._save_profiles(self.profiles)

    def _save_profiles(self, profiles: Iterable[GameProfile]) -> Path:
        write_versioned_json(
            self.path,
            profiles_schema_version,
            {
                "profiles": [profile.to_mapping() for profile in profiles],
            },
        )
        return self.path

    def _commit_profiles(self, profiles: Iterable[GameProfile]) -> None:
        profiles = list(profiles)
        self._save_profiles(profiles)
        self.profiles = profiles

    def get(self, profile_id: str) -> GameProfile | None:
        return next(
            (profile for profile in self.profiles if profile.id == profile_id),
            None,
        )

    def create(
        self,
        name: object,
        settings: AppSettings,
        *,
        region: DialogRegion | None = None,
    ) -> GameProfile:
        self._ensure_name_available(name)
        profile = GameProfile.from_settings(name, settings, region=region)
        self._commit_profiles((*self.profiles, profile))
        return profile

    def duplicate(self, profile_id: str, name: object) -> GameProfile:
        source = self._required(profile_id)
        self._ensure_name_available(name)
        duplicate = replace(source, id=uuid4().hex, name=_validated_name(name))
        self._commit_profiles((*self.profiles, duplicate))
        return duplicate

    def rename(self, profile_id: str, name: object) -> GameProfile:
        profile = self._required(profile_id)
        self._ensure_name_available(name, excluding=profile_id)
        updated = replace(profile, name=_validated_name(name))
        self._commit_profiles(self._replaced(updated))
        return updated

    def remove(self, profile_id: str) -> GameProfile:
        profile = self._required(profile_id)
        self._commit_profiles(item for item in self.profiles if item.id != profile.id)
        return profile

    def update_from_settings(
        self,
        profile_id: str,
        settings: AppSettings,
        *,
        region: DialogRegion | None = None,
    ) -> GameProfile:
        profile = self._required(profile_id).updated_from_settings(
            settings,
            region=region,
        )
        self._commit_profiles(self._replaced(profile))
        return profile

    def update_region(self, profile_id: str, region: DialogRegion) -> GameProfile:
        profile = replace(self._required(profile_id), dialog_region=region)
        self._commit_profiles(self._replaced(profile))
        return profile

    def _replaced(self, updated: GameProfile) -> list[GameProfile]:
        return [
            updated if profile.id == updated.id else profile
            for profile in self.profiles
        ]

    def _required(self, profile_id: str) -> GameProfile:
        profile = self.get(profile_id)
        if profile is None:
            raise KeyError(f"Unknown game profile: {profile_id}")
        return profile

    def _ensure_unique_names(self) -> None:
        names = [profile.name.casefold() for profile in self.profiles]
        if len(names) != len(set(names)):
            raise ValueError("profile names must be unique")

    def _ensure_name_available(
        self, name: object, *, excluding: str | None = None
    ) -> None:
        normalized = _validated_name(name).casefold()
        if any(
            profile.name.casefold() == normalized and profile.id != excluding
            for profile in self.profiles
        ):
            raise ValueError(f"A profile named {name!r} already exists")


def _validated_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Profile name must not be empty")
    return name.strip()


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _audio_source_policy(value: object) -> str:
    return value if value in audio_source_policies else default_audio_source_policy


def _voice_assignments(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        character.strip(): source_id.strip()
        for character, source_id in value.items()
        if isinstance(character, str)
        and character.strip()
        and isinstance(source_id, str)
        and source_id.strip()
    }


def _dialog_region(value: object) -> DialogRegion:
    if not isinstance(value, Mapping):
        raise ValueError("dialog_region must be an object")
    coordinates: list[float] = []
    for name in ("left", "top", "width", "height"):
        coordinate = value[name]
        if not isinstance(coordinate, (int, float)):
            raise ValueError(f"dialog_region {name} must be a number")
        coordinates.append(coordinate)
    return DialogRegion(*coordinates)
