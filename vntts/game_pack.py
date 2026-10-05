"""Public, device-independent import boundary for complete VNTTS game packs."""

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from time import perf_counter, process_time

from durable_file import sha256_file
from vntts_artifacts.game_pack import GamePack, GamePackError, load_game_pack
from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    load_story_index_document,
)

from vntts.path_safety import contained_path, safe_relative_path
from vntts.settings import AppSettings
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

    def apply_to(
        self,
        settings: AppSettings,
        *,
        preserve_external_sequence: bool = False,
    ) -> AppSettings:
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


def import_game_pack(path: str | Path) -> GamePackImport:
    """Load and fully preflight a versioned game pack for VNTTS consumption."""
    started = perf_counter()
    cpu_started = process_time()
    try:
        pack = load_game_pack(path)
        semantic_evidence = _source_audio_semantic_evidence(pack)
    except Exception:
        _record_pack_timing(started, cpu_started, "failed")
        raise
    files = (
        pack.story_index,
        pack.voice_manifest,
        *pack.voice_wavs,
        *((pack.generated_audio,) if pack.generated_audio is not None else ()),
        *pack.generated_wavs,
        *((pack.live_sequence_plan,) if pack.live_sequence_plan is not None else ()),
    )
    try:
        total_bytes = sum(binding.path.stat().st_size for binding in files)
    except OSError:
        total_bytes = None
    details = {"files_examined": len(files)}
    if total_bytes is not None:
        details["bytes_examined"] = total_bytes
    _record_pack_timing(started, cpu_started, "complete", **details)
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


def _record_pack_timing(
    started: float,
    cpu_started: float,
    outcome: str,
    **details: int,
) -> None:
    from vntts.support import record_background_operation

    record_background_operation(
        "game-pack-validation",
        (perf_counter() - started) * 1000,
        outcome,
        cpu_ms=round((process_time() - cpu_started) * 1000, 3),
        **details,
    )


def _source_audio_semantic_evidence(pack: GamePack) -> Path | None:
    authoring = pack.extensions.get("vntts.authoring")
    extension = (
        authoring.get("source_audio_semantic_evidence")
        if isinstance(authoring, dict)
        else None
    )
    if extension is None:
        try:
            story = _load_bound_story_index(
                pack.story_index.path, pack.story_index.sha256
            )
            story_metadata = story.metadata.get("source_audio_semantics")
        except (StoryIndexError, OSError) as error:
            raise GamePackError(str(error)) from error
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
    evidence_path = contained_path(
        Path(pack.manifest_path).parent,
        safe_relative_path(
            extension.get("path"),
            "Game pack semantic evidence path",
            error_type=GamePackError,
        ),
        "Game pack semantic evidence",
        error_type=GamePackError,
    )
    evidence_sha256 = extension.get("sha256")
    if not evidence_path.is_file() or sha256_file(evidence_path) != evidence_sha256:
        raise GamePackError("Game pack semantic evidence checksum changed")
    evidence_id = extension.get("evidence_id")
    entry_count = extension.get("entry_count")
    if not isinstance(evidence_id, str) or type(entry_count) is not int:
        raise GamePackError("Game pack semantic evidence extension changed")
    try:
        _validate_semantic_evidence(
            str(pack.story_index.path),
            pack.story_index.sha256,
            str(evidence_path),
            evidence_sha256,
            evidence_id,
            entry_count,
        )
    except (StoryIndexError, SourceAudioSemanticEvidenceError, OSError) as error:
        raise GamePackError(str(error)) from error
    return evidence_path


@lru_cache(maxsize=8)
def _validate_semantic_evidence(
    story_path: str,
    story_sha256: str,
    evidence_path: str,
    evidence_sha256: str,
    evidence_id: str,
    entry_count: int,
) -> None:
    story = _load_bound_story_index(Path(story_path), story_sha256)
    document = load_source_audio_semantic_evidence(evidence_path, story)
    if (
        document["evidence_id"] != evidence_id
        or len(document["entries"]) != entry_count
    ):
        raise SourceAudioSemanticEvidenceError(
            "Game pack semantic evidence extension changed"
        )

    if sha256_file(evidence_path) != evidence_sha256:
        raise SourceAudioSemanticEvidenceError(
            "Game pack semantic evidence checksum changed while it was being read"
        )


def _load_bound_story_index(path: Path, checksum: str) -> StoryIndexDocument:
    document = load_story_index_document(path)
    if sha256_file(path) != checksum:
        raise GamePackError("Game pack story checksum changed while it was being read")
    return document


def apply_game_pack(
    settings: AppSettings,
    path: str | Path | None = None,
) -> AppSettings:
    """Preflight and apply ``path`` (or ``settings.game_pack``) in one step."""
    configured_path = path if path is not None else settings.game_pack
    if not configured_path:
        return settings
    imported = import_game_pack(configured_path)
    return imported.apply_to(
        settings,
        preserve_external_sequence=path is None,
    )


def main(argv: Sequence[str] | None = None) -> int:
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
