"""Local selection of prepared audio, independent of user preferences."""

import logging
import re
from dataclasses import asdict
from pathlib import Path

from vntts.application_directories import get_local_data_directory
from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.game_pack import GamePackError, GamePackImport, import_game_pack
from vntts.pregeneration_pack import OfflinePackResult
from vntts.settings import AppSettings, content_path_setting_names
from vntts.versioned_json import (
    read_versioned_json_snapshot,
    write_versioned_json_if_unchanged,
)


def project_prepared_pack(
    settings: AppSettings, imported: GamePackImport
) -> AppSettings:
    dialogue = ChapterVoicePreloader.load_optional(imported.story_index).dialogue
    return imported.apply_to(settings).updated(
        live_sequence_mode=(
            "audio-manual"
            if imported.live_sequence_plan is not None
            and settings.live_sequence_mode == "audio-manual"
            else "audio-auto"
            if imported.live_sequence_plan is not None and settings.auto_advance_enabled
            else "off"
        ),
        audio_source_policy=(
            "prefer-game-audio"
            if any(
                line.source_audio_authoritative
                and line.source_audio_completeness == "full"
                for line in dialogue
            )
            else "prefer-generated"
        ),
        force_live_narrator=False,
    )


def _sources(settings: AppSettings) -> dict[str, object]:
    values = asdict(settings)
    return {name: values[name] for name in content_path_setting_names}


class PreparedPlaybackStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (
            path or get_local_data_directory() / "pregeneration" / "playback.json"
        )

    def _read(self) -> tuple[dict[str, object], bytes | None]:
        try:
            document, revision = read_versioned_json_snapshot(
                self.path, schema_version=1, document_name="prepared playback state"
            )
        except FileNotFoundError:
            return {}, None
        profiles = document.get("profiles")
        if not isinstance(profiles, dict):
            raise ValueError("Invalid prepared playback profiles")
        return profiles, revision

    def restore(self, settings: AppSettings) -> AppSettings:
        try:
            profiles, _revision = self._read()
            selected = profiles.get(settings.active_profile_id or "")
            if selected is None:
                return settings
            if not isinstance(selected, dict):
                raise ValueError("Invalid prepared playback selection")
            if selected.get("sources") != _sources(settings):
                return settings
            manifest, identity = selected.get("manifest"), selected.get("identity")
            if not isinstance(manifest, str) or not manifest.strip():
                raise ValueError("Invalid prepared playback manifest")
            if (
                not isinstance(identity, str)
                or re.fullmatch(r"[a-f0-9]{64}", identity) is None
            ):
                raise ValueError("Invalid prepared playback identity")
            imported = import_game_pack(manifest)
            extension = imported.pack.extensions.get("vntts.self-service")
            if not isinstance(extension, dict) or extension.get("identity") != identity:
                raise ValueError("Prepared playback identity changed")
            projected = project_prepared_pack(settings, imported)
            routing = selected.get("routing")
            if not isinstance(routing, dict):
                raise ValueError("Invalid prepared playback routing preferences")
            return projected.updated(
                audio_source_policy=(
                    settings.audio_source_policy
                    if settings.audio_source_policy
                    != routing.get("audio_source_policy")
                    else projected.audio_source_policy
                ),
                force_live_narrator=(
                    settings.force_live_narrator
                    if settings.force_live_narrator
                    != routing.get("force_live_narrator")
                    else projected.force_live_narrator
                ),
                live_sequence_mode=(
                    settings.live_sequence_mode
                    if settings.live_sequence_mode != routing.get("live_sequence_mode")
                    else projected.live_sequence_mode
                ),
            )
        except (GamePackError, OSError, ValueError) as error:
            logging.getLogger(__name__).exception(
                "Prepared audio is unavailable; retaining user preferences (%s): %s",
                self.path,
                error,
            )
            return settings

    def save(self, settings: AppSettings, pack: OfflinePackResult) -> Path:
        profiles, revision = self._read()
        profiles[settings.active_profile_id or ""] = {
            "manifest": str(pack.manifest.resolve()),
            "identity": pack.identity,
            "sources": _sources(settings),
            "routing": {
                "audio_source_policy": settings.audio_source_policy,
                "force_live_narrator": settings.force_live_narrator,
                "live_sequence_mode": settings.live_sequence_mode,
            },
        }
        self._write(profiles, revision)
        return self.path

    def _write(self, profiles: dict[str, object], revision: bytes | None) -> None:
        write_versioned_json_if_unchanged(
            self.path,
            1,
            {"profiles": profiles},
            revision=revision,
            document_name="Prepared playback state",
        )
