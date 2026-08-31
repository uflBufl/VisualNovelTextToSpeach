"""Atomic portable game-pack publication for self-service pregeneration."""

from __future__ import annotations

import copy
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import GamePackError, write_game_pack
from vntts_artifacts.generated_audio import (
    GeneratedAudioManifestError,
    load_generated_audio_document,
    write_generated_audio_manifest,
)
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)
from vntts_artifacts.voice_manifest import VoiceManifestError, load_voice_manifest

from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.authoring.generation_manifest import approved_manifest_entries
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
)
from vntts.game_pack import GamePackImport, import_game_pack
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.pregeneration_generation import (
    OfflineGenerationCancelled,
    OfflineGenerationError,
    OfflineGenerationResult,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_setup import PregenerationJob
from vntts.source_audio_semantics import (
    canonical_document_sha256,
    load_source_audio_semantic_evidence,
)


class OfflinePackError(OfflineGenerationError):
    """A validated self-service result could not be published portably."""


@dataclass(frozen=True)
class OfflinePackResult:
    identity: str
    directory: Path
    manifest: Path
    imported: GamePackImport
    approved: int
    live_fallbacks: int


class OfflinePackPublisher:
    def publish(
        self,
        job,
        generation_input,
        generation_result,
        cancel_event=None,
    ):
        _validate_inputs(job, generation_input, generation_result)
        _raise_if_cancelled(cancel_event)
        try:
            state = load_generation_state(
                generation_result.state,
                generation_input.queue,
            )
            queue = VoiceGenerationQueue.load(generation_input.queue)
            story = load_story_index_document(generation_input.story_index)
            voice_document, voices = load_voice_manifest(
                generation_input.voice_manifest,
                allow_legacy=False,
            )
        except (
            BulkGenerationError,
            GamePackError,
            GeneratedAudioManifestError,
            OSError,
            StoryIndexError,
            ValueError,
            VoiceGenerationQueueError,
            VoiceManifestError,
        ) as error:
            raise OfflinePackError(
                f"Unable to inspect prepared audio: {error}"
            ) from error
        _require_terminal_generation(state, queue)
        state_sha256 = sha256_file(generation_result.state)
        identity = _identity(generation_input, state_sha256)
        destination = (
            generation_input.directory.parent / "game-packs" / (f"pack-{identity[:24]}")
        )
        if destination.is_dir():
            return _load_existing(destination, identity)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
        )
        try:
            story_copy = staging / "story" / "story-index.jsonl"
            voice_copy = staging / "voices" / "voice-manifest.json"
            generated_copy = staging / "generated" / "manifest.json"
            _copy_file(generation_input.story_index, story_copy)
            _copy_file(generation_input.voice_manifest, voice_copy)
            _copy_voice_references(
                generation_input.voice_manifest,
                voice_copy,
                voice_document,
                voices,
            )
            _raise_if_cancelled(cancel_event)
            generated_records = approved_manifest_entries(
                state,
                generation_result.output,
            )
            for record in generated_records:
                relative = _safe_relative(record["audio"], "Generated WAV")
                _copy_file(
                    generation_result.output / Path(*relative.parts),
                    generated_copy.parent / Path(*relative.parts),
                )
            live_fallbacks = _live_fallback_records(state)
            write_generated_audio_manifest(
                generated_copy,
                {
                    "game": story.game,
                    "language": story.language,
                    "source_queue_sha256": generation_input.queue_sha256,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "vntts.authoring.live_fallback": {
                        "schema_version": 1,
                        "mode": "explicit",
                        "entries": live_fallbacks,
                    },
                    "vntts.authoring.audio_event_omission": {
                        "schema_version": 1,
                        "mode": "explicit",
                        "entries": [],
                    },
                },
                generated_records,
            )
            semantic_copy, semantic_document = _copy_semantic_evidence(
                generation_input,
                staging,
                story_copy,
            )
            components = {
                "story_index": story_copy,
                "voice_manifest": voice_copy,
                "generated_audio": generated_copy,
            }
            pack_metadata = {
                "game": {
                    "id": story.game or job.game,
                    "version": job.game_version or "local",
                },
                "producers": [{"name": "vntts-self-service", "version": "1"}],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "vntts.self-service": {
                    "schema_version": 1,
                    "identity": identity,
                    "job_id": job.job_id,
                    "generation_input_identity": generation_input.identity,
                    "source_queue_sha256": generation_input.queue_sha256,
                    "source_state_sha256": state_sha256,
                    "approved_count": len(generated_records),
                    "live_fallback_count": len(live_fallbacks),
                },
            }
            if semantic_copy is not None:
                pack_metadata["vntts.authoring"] = {
                    "source_audio_semantic_evidence": {
                        "path": "story/source-audio-semantic-evidence.json",
                        "sha256": sha256_file(semantic_copy),
                        "evidence_id": semantic_document["evidence_id"],
                        "entry_count": len(semantic_document["entries"]),
                    }
                }
            pack_manifest = staging / "game-pack.json"
            write_game_pack(pack_manifest, pack_metadata, components)
            import_game_pack(pack_manifest)
            GeneratedAudioLibrary(load_generated_audio_document(generated_copy))
            _raise_if_cancelled(cancel_event)
            try:
                rename_directory_no_replace(staging, destination)
            except AtomicPublicationError:
                if destination.is_dir():
                    return _load_existing(destination, identity)
                raise
            staging = None
            result = _load_existing(destination, identity)
            if result.approved != len(
                generated_records
            ) or result.live_fallbacks != len(live_fallbacks):
                raise OfflinePackError("Published offline pack counts changed")
            return result
        except OfflineGenerationCancelled:
            raise
        except (
            AtomicPublicationError,
            BulkGenerationError,
            GamePackError,
            GeneratedAudioManifestError,
            OSError,
            StoryIndexError,
            ValueError,
            VoiceManifestError,
        ) as error:
            raise OfflinePackError(
                f"Unable to publish offline pack: {error}"
            ) from error
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)


def _validate_inputs(job, generation_input, generation_result):
    if not isinstance(job, PregenerationJob):
        raise OfflinePackError("Offline preparation job is invalid")
    if not isinstance(generation_input, PregenerationInput):
        raise OfflinePackError("Offline generation input is invalid")
    if not isinstance(generation_result, OfflineGenerationResult):
        raise OfflinePackError("Offline generation result is invalid")
    expected_output = generation_input.directory.parent / (
        f"generation-output-{generation_input.identity[:16]}"
    )
    if generation_result.output.resolve() != expected_output.resolve():
        raise OfflinePackError("Offline pack output identity changed")


def _require_terminal_generation(state, queue):
    if state.get("active") is not None:
        raise OfflinePackError("Offline generation is still active")
    expected = {item.queue_id for item in queue.items if item.action == "generate"}
    actual = set(state.get("items", {}))
    if actual != expected:
        raise OfflinePackError("Offline generation does not cover the selected queue")
    for queue_id in sorted(expected):
        item = state["items"][queue_id]
        status = item.get("status") if isinstance(item, dict) else None
        review = item.get("review_status") if isinstance(item, dict) else None
        if (status, review) not in {
            ("approved", "approved"),
            ("live_fallback", "live_fallback"),
        }:
            raise OfflinePackError(
                f"Offline generation item is not terminal: {queue_id!r}"
            )


def _identity(generation_input, state_sha256):
    payload = {
        "schema_version": 1,
        "generation_input_identity": generation_input.identity,
        "queue_sha256": generation_input.queue_sha256,
        "state_sha256": state_sha256,
        "story_index_sha256": sha256_file(generation_input.story_index),
        "voice_manifest_sha256": sha256_file(generation_input.voice_manifest),
        "semantic_evidence_sha256": (
            None
            if generation_input.source_audio_semantic_evidence is None
            else sha256_file(generation_input.source_audio_semantic_evidence)
        ),
    }
    return canonical_document_sha256(payload)


def _live_fallback_records(state):
    records = []
    for item in state["items"].values():
        decision = item.get("live_fallback") if isinstance(item, dict) else None
        if not isinstance(decision, dict):
            continue
        records.append(
            {
                **copy.deepcopy(decision),
                "decision_sha256": canonical_document_sha256(decision),
            }
        )
    return sorted(records, key=lambda value: (value["line_id"], value["text_sha256"]))


def _copy_voice_references(source_manifest, target_manifest, document, voices):
    raw_voices = document.get("voices")
    if not isinstance(raw_voices, list) or len(raw_voices) != len(voices):
        raise OfflinePackError("Offline voice manifest changed")
    for raw, voice in zip(raw_voices, voices, strict=True):
        if tuple(raw.get("references") or ()) != voice.references:
            raise OfflinePackError("Offline voice references changed")
        for configured in voice.references:
            relative = _safe_relative(configured, "Voice reference")
            _copy_file(
                source_manifest.parent / Path(*relative.parts),
                target_manifest.parent / Path(*relative.parts),
            )


def _copy_semantic_evidence(generation_input, staging, story_copy):
    source = generation_input.source_audio_semantic_evidence
    if source is None:
        return None, None
    destination = staging / "story" / "source-audio-semantic-evidence.json"
    _copy_file(source, destination)
    return destination, load_source_audio_semantic_evidence(destination, story_copy)


def _copy_file(source, destination):
    source = Path(source).resolve()
    if not source.is_file() or source.is_symlink():
        raise OfflinePackError(f"Offline pack source is unsafe: {source}")
    before = sha256_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if sha256_file(source) != before or sha256_file(destination) != before:
        raise OfflinePackError(f"Offline pack source changed: {source}")


def _safe_relative(value, label):
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise OfflinePackError(f"{label} path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise OfflinePackError(f"{label} leaves its component directory")
    return relative


def _load_existing(destination, identity):
    imported = import_game_pack(destination / "game-pack.json")
    extension = imported.pack.extensions.get("vntts.self-service")
    if not isinstance(extension, dict) or extension.get("identity") != identity:
        raise OfflinePackError("Existing offline pack identity changed")
    GeneratedAudioLibrary(
        load_generated_audio_document(imported.generated_audio_manifest)
    )
    return OfflinePackResult(
        identity=identity,
        directory=destination,
        manifest=imported.pack.manifest_path,
        imported=imported,
        approved=extension.get("approved_count"),
        live_fallbacks=extension.get("live_fallback_count"),
    )


def _raise_if_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise OfflineGenerationCancelled("Offline pack publication was cancelled")


__all__ = [
    "OfflinePackError",
    "OfflinePackPublisher",
    "OfflinePackResult",
]
