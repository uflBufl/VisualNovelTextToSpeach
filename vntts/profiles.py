from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import uuid4

from vntts.ocr import DialogRegion, get_dialog_region
from vntts.settings import (
    audio_source_policies,
    default_audio_source_policy,
    get_config_directory,
)
from vntts.versioned_json import load_versioned_json, write_versioned_json

profiles_schema_version = 4


def get_profiles_path():
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
    generated_audio_manifest: str | None
    audio_source_policy: str
    voice_assignments: dict[str, str]

    @classmethod
    def from_settings(cls, name, settings, *, region=None, profile_id=None):
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
            generated_audio_manifest=settings.generated_audio_manifest,
            audio_source_policy=settings.audio_source_policy,
            voice_assignments=dict(settings.voice_assignments),
        )

    @classmethod
    def from_mapping(cls, values):
        region = values["dialog_region"]
        return cls(
            id=str(values["id"]),
            name=_validated_name(values["name"]),
            capture_mode=(
                values["capture_mode"]
                if values["capture_mode"] in {"screen", "window"}
                else "screen"
            ),
            game_window_title=_optional_text(values.get("game_window_title")),
            dialog_region=DialogRegion(
                region["left"],
                region["top"],
                region["width"],
                region["height"],
            ),
            ocr_language=str(values.get("ocr_language") or "eng").strip(),
            game_pack=_optional_text(values.get("game_pack")),
            voice_manifest=_optional_text(values.get("voice_manifest")),
            story_index=_optional_text(values.get("story_index")),
            generated_audio_manifest=_optional_text(
                values.get("generated_audio_manifest")
            ),
            audio_source_policy=_audio_source_policy(values.get("audio_source_policy")),
            voice_assignments=_voice_assignments(values.get("voice_assignments")),
        )

    def to_mapping(self):
        values = asdict(self)
        values["dialog_region"] = self.dialog_region.to_json()
        return values

    def apply(self, settings):
        settings = settings.updated(
            active_profile_id=self.id,
            capture_mode=self.capture_mode,
            game_window_title=self.game_window_title,
            ocr_language=self.ocr_language,
            game_pack=self.game_pack,
            voice_manifest=self.voice_manifest,
            story_index=self.story_index,
            generated_audio_manifest=self.generated_audio_manifest,
            audio_source_policy=self.audio_source_policy,
            voice_assignments=dict(self.voice_assignments),
        )
        if self.game_pack:
            from vntts.game_pack import apply_game_pack

            settings = apply_game_pack(settings)
        return settings

    def updated_from_settings(self, settings, *, region=None):
        return replace(
            self,
            capture_mode=settings.capture_mode,
            game_window_title=settings.game_window_title,
            dialog_region=region or get_dialog_region(),
            ocr_language=settings.ocr_language,
            game_pack=settings.game_pack,
            voice_manifest=settings.voice_manifest,
            story_index=settings.story_index,
            generated_audio_manifest=settings.generated_audio_manifest,
            audio_source_policy=settings.audio_source_policy,
            voice_assignments=dict(settings.voice_assignments),
        )


class GameProfileStore:
    def __init__(self, path=None, profiles=()):
        self.path = get_profiles_path() if path is None else Path(path).expanduser()
        self.profiles = list(profiles)

    @classmethod
    def load(cls, path=None, *, warn=None):
        warn = (lambda _message: None) if warn is None else warn
        store = cls(path)

        def decode(payload):
            store.profiles = [
                GameProfile.from_mapping(profile) for profile in payload["profiles"]
            ]
            store._ensure_unique_names()
            return store

        def fallback():
            store.profiles = []
            return store

        return load_versioned_json(
            store.path,
            schema_version=profiles_schema_version,
            document_name="game profiles",
            decode=decode,
            fallback=fallback,
            warn=warn,
            allow_older=True,
        )

    def save(self):
        write_versioned_json(
            self.path,
            profiles_schema_version,
            {
                "profiles": [profile.to_mapping() for profile in self.profiles],
            },
        )
        return self.path

    def get(self, profile_id):
        return next(
            (profile for profile in self.profiles if profile.id == profile_id),
            None,
        )

    def create(self, name, settings, *, region=None):
        self._ensure_name_available(name)
        profile = GameProfile.from_settings(name, settings, region=region)
        self.profiles.append(profile)
        self.save()
        return profile

    def duplicate(self, profile_id, name):
        source = self._required(profile_id)
        self._ensure_name_available(name)
        duplicate = replace(source, id=uuid4().hex, name=_validated_name(name))
        self.profiles.append(duplicate)
        self.save()
        return duplicate

    def rename(self, profile_id, name):
        profile = self._required(profile_id)
        self._ensure_name_available(name, excluding=profile_id)
        updated = replace(profile, name=_validated_name(name))
        self._replace(updated)
        self.save()
        return updated

    def remove(self, profile_id):
        profile = self._required(profile_id)
        self.profiles.remove(profile)
        self.save()
        return profile

    def update_from_settings(self, profile_id, settings, *, region=None):
        profile = self._required(profile_id).updated_from_settings(
            settings,
            region=region,
        )
        self._replace(profile)
        self.save()
        return profile

    def update_region(self, profile_id, region):
        profile = replace(self._required(profile_id), dialog_region=region)
        self._replace(profile)
        self.save()
        return profile

    def _replace(self, updated):
        self.profiles = [
            updated if profile.id == updated.id else profile
            for profile in self.profiles
        ]

    def _required(self, profile_id):
        profile = self.get(profile_id)
        if profile is None:
            raise KeyError(f"Unknown game profile: {profile_id}")
        return profile

    def _ensure_unique_names(self):
        names = [profile.name.casefold() for profile in self.profiles]
        if len(names) != len(set(names)):
            raise ValueError("profile names must be unique")

    def _ensure_name_available(self, name, *, excluding=None):
        normalized = _validated_name(name).casefold()
        if any(
            profile.name.casefold() == normalized and profile.id != excluding
            for profile in self.profiles
        ):
            raise ValueError(f"A profile named {name!r} already exists")


def _validated_name(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Profile name must not be empty")
    return name.strip()


def _optional_text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _audio_source_policy(value):
    return value if value in audio_source_policies else default_audio_source_policy


def _voice_assignments(value):
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
