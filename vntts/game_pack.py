"""Public, device-independent import boundary for complete VNTTS game packs."""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import GamePack, GamePackError, load_game_pack
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document

from vntts.source_audio_semantics import (
    SourceAudioSemanticEvidenceError,
    load_source_audio_semantic_evidence,
)


@dataclass(frozen=True)
class GamePackImport:
    """A fully preflighted game pack and the paths consumed by VNTTS."""

    pack: GamePack
    story_index: Path
    voice_manifest: Path
    generated_audio_manifest: Path | None
    live_sequence_plan: Path | None
    source_audio_semantic_evidence: Path | None

    def apply_to(self, settings, *, preserve_external_sequence=False):
        """Return settings routed to this pack without modifying app or pack data."""
        sequence_plan = (
            str(self.live_sequence_plan)
            if self.live_sequence_plan is not None
            else settings.live_sequence_plan
            if preserve_external_sequence
            else None
        )
        return settings.updated(
            game_pack=str(self.pack.manifest_path),
            story_index=str(self.story_index),
            live_sequence_plan=sequence_plan,
            live_sequence_mode=(
                settings.live_sequence_mode if sequence_plan is not None else "off"
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
    semantic_evidence = _source_audio_semantic_evidence(pack)
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
        source_audio_semantic_evidence=semantic_evidence,
    )


def _source_audio_semantic_evidence(pack):
    authoring = pack.extensions.get("vntts.authoring")
    extension = (
        authoring.get("source_audio_semantic_evidence")
        if isinstance(authoring, dict)
        else None
    )
    try:
        story_metadata = load_story_index_document(pack.story_index.path).metadata.get(
            "source_audio_semantics"
        )
    except StoryIndexError as error:
        raise GamePackError(str(error)) from error
    if extension is None:
        if isinstance(story_metadata, dict):
            raise GamePackError(
                "Game pack story semantic decisions have no evidence component"
            )
        return None
    if not isinstance(extension, dict) or set(extension) != {
        "path",
        "sha256",
        "evidence_id",
        "entry_count",
    }:
        raise GamePackError("Game pack semantic evidence extension is malformed")
    relative = extension.get("path")
    if not isinstance(relative, str) or not relative:
        raise GamePackError("Game pack semantic evidence path is invalid")
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise GamePackError("Game pack semantic evidence path is unsafe")
    root = pack.manifest_path.parent.resolve()
    evidence_path = (root / Path(*pure.parts)).resolve()
    try:
        evidence_path.relative_to(root)
    except ValueError as error:
        raise GamePackError("Game pack semantic evidence leaves its pack") from error
    if not evidence_path.is_file() or sha256_file(evidence_path) != extension.get(
        "sha256"
    ):
        raise GamePackError("Game pack semantic evidence checksum changed")
    try:
        document = load_source_audio_semantic_evidence(
            evidence_path,
            pack.story_index.path,
        )
    except SourceAudioSemanticEvidenceError as error:
        raise GamePackError(str(error)) from error
    if document["evidence_id"] != extension.get("evidence_id") or len(
        document["entries"]
    ) != extension.get("entry_count"):
        raise GamePackError("Game pack semantic evidence extension changed")
    return evidence_path


def apply_game_pack(settings, path=None):
    """Preflight and apply ``path`` (or ``settings.game_pack``) in one step."""
    configured_path = path if path is not None else settings.game_pack
    if not configured_path:
        return settings
    return import_game_pack(configured_path).apply_to(
        settings,
        preserve_external_sequence=path is None,
    )


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
                "source_audio_semantic_evidence": (
                    str(imported.source_audio_semantic_evidence)
                    if imported.source_audio_semantic_evidence is not None
                    else None
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0
