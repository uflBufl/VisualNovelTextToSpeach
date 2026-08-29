"""Public, device-independent import boundary for complete VNTTS game packs."""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.game_pack import GamePack, GamePackError, load_game_pack


@dataclass(frozen=True)
class GamePackImport:
    """A fully preflighted game pack and the paths consumed by VNTTS."""

    pack: GamePack
    story_index: Path
    voice_manifest: Path
    generated_audio_manifest: Path | None
    live_sequence_plan: Path | None

    def apply_to(self, settings):
        """Return settings routed to this pack without modifying app or pack data."""
        return settings.updated(
            game_pack=str(self.pack.manifest_path),
            story_index=str(self.story_index),
            live_sequence_plan=(
                str(self.live_sequence_plan)
                if self.live_sequence_plan is not None
                else None
            ),
            live_sequence_mode=(
                settings.live_sequence_mode
                if self.pack.live_sequence_plan is not None
                else "off"
            ),
            voice_manifest=str(self.voice_manifest),
            generated_audio_manifest=(
                str(self.generated_audio_manifest)
                if self.generated_audio_manifest is not None
                else None
            ),
        )


def import_game_pack(path):
    """Load and fully preflight a versioned game pack for VNTTS consumption."""
    pack = load_game_pack(path)
    return GamePackImport(
        pack=pack,
        story_index=pack.story_index.path,
        voice_manifest=pack.voice_manifest.path,
        generated_audio_manifest=(
            pack.generated_audio.path if pack.generated_audio is not None else None
        ),
        live_sequence_plan=(
            pack.live_sequence_plan.path
            if pack.live_sequence_plan is not None
            else None
        ),
    )


def apply_game_pack(settings, path=None):
    """Preflight and apply ``path`` (or ``settings.game_pack``) in one step."""
    configured_path = path if path is not None else settings.game_pack
    if not configured_path:
        return settings
    return import_game_pack(configured_path).apply_to(settings)


def main(argv=None):
    """Preflight a game pack and print its resolved VNTTS input paths."""
    parser = argparse.ArgumentParser(
        description="Validate a vntts.game-pack and resolve its VNTTS inputs"
    )
    parser.add_argument("game_pack", help="Path to the game-pack JSON document")
    arguments = parser.parse_args(argv)
    try:
        imported = import_game_pack(arguments.game_pack)
    except GamePackError as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "game_pack": str(imported.pack.manifest_path),
                "game_id": imported.pack.game_id,
                "game_version": imported.pack.game_version,
                "story_index": str(imported.story_index),
                "voice_manifest": str(imported.voice_manifest),
                "generated_audio_manifest": (
                    str(imported.generated_audio_manifest)
                    if imported.generated_audio_manifest is not None
                    else None
                ),
                "live_sequence_plan": (
                    str(imported.live_sequence_plan)
                    if imported.live_sequence_plan is not None
                    else None
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0
