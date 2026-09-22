"""Atomic final game-pack publication from authoritative authoring state."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import socket
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from types import TracebackType
from typing import Literal, Protocol, TypeAlias
from uuid import uuid4

import numpy as np
import soundfile as sf
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import probe_pcm16_mono_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import GamePackError, load_game_pack, write_game_pack
from vntts_artifacts.generated_audio import (
    GeneratedAudioManifestError,
    write_generated_audio_manifest,
)
from vntts_artifacts.live_sequence import (
    LiveSequencePlanError,
    load_live_sequence_plan,
)
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
)

from vntts.authoring.advisory_lock import (
    AdvisoryLockBusyError,
    exclusive_advisory_lock,
)
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    JsonDocument,
    load_generation_state,
    process_is_alive,
    validate_authoring_publication_authority,
)
from vntts.authoring.failure_reference_binding_records import (
    FailureReferenceBindingError,
    load_failure_reference_binding_document,
)
from vntts.authoring.generation_lease import GenerationLease, process_started_at
from vntts.authoring.generation_manifest import approved_manifest_entries
from vntts.authoring.generation_state import (
    load_stable_generation_queue,
    reviewed_waveform_publication_queue_ids,
    validate_generation_state_document,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    generation_publication_leases,
)
from vntts.authoring.publication import (
    rename_directory_no_replace as _rename_directory_no_replace,
)
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
)
from vntts.source_audio_semantics import (
    SourceAudioSemanticEvidenceError,
    load_source_audio_semantic_evidence,
)
from vntts.voices import synthesis_character_for_line

_canonical_sha256 = canonical_document_sha256
Inventory: TypeAlias = dict[Path, str]
DecisionRecord: TypeAlias = dict[str, object]
Producer: TypeAlias = dict[str, str]
RoleMatcher: TypeAlias = Callable[[str], bool]
VoiceControlPaths: TypeAlias = dict[Path, tuple[str, RoleMatcher]]


class _QueueItem(Protocol):
    queue_id: str
    speaker: str
    voice_character: str | None


class _Queue(Protocol):
    items: Sequence[_QueueItem]
    metadata: Mapping[str, object]


class _StoryRecord(Protocol):
    line_id: str
    text_sha256: str


class _Story(Protocol):
    metadata: Mapping[str, object]
    records: Sequence[_StoryRecord]
    game: str | None
    language: str | None


class _VoiceEntry(Protocol):
    character: str
    aliases: Sequence[str]
    references: Sequence[str]


class FinalGamePackError(RuntimeError):
    """Raised before an incomplete or unsafe final pack can be published."""


@dataclass(frozen=True)
class FinalGamePackResult:
    directory: Path
    manifest: Path
    live_sequence_plan: Path | None
    source_audio_semantic_evidence: Path | None
    game_id: str
    game_version: str
    approved_count: int
    rejected_count: int
    live_fallback_count: int
    omitted_count: int
    source_queue_sha256: str
    source_state_sha256: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["directory"] = str(self.directory)
        payload["manifest"] = str(self.manifest)
        payload["live_sequence_plan"] = (
            None if self.live_sequence_plan is None else str(self.live_sequence_plan)
        )
        payload["source_audio_semantic_evidence"] = (
            None
            if self.source_audio_semantic_evidence is None
            else str(self.source_audio_semantic_evidence)
        )
        return payload


@dataclass(frozen=True)
class _PublicationPaths:
    destination: Path
    state: Path
    queue: Path
    story: Path
    voice_manifest: Path
    live_sequence: Path | None
    semantic_evidence: Path | None
    failure_reference_binding: Path | None


@dataclass(frozen=True)
class _PublicationRequest:
    paths: _PublicationPaths
    game_id: object | None
    game_version: str
    producers: list[Producer]
    created_at: str


@dataclass(frozen=True)
class _StagedControls:
    directory: Path
    inventory: Inventory
    story_copy: Path
    voice_copy: Path
    story: _Story
    story_sha256: str
    voice_sha256: str
    live_sequence_copy: Path | None
    semantic_evidence_copy: Path | None
    semantic_evidence_document: JsonDocument | None
    semantic_evidence_sha256: str | None
    failure_reference_document: JsonDocument | None


@dataclass(frozen=True)
class _ValidatedVoiceControls:
    narrator_selection: DecisionRecord | None
    voice_override: bool
    projection: DecisionRecord | None


@dataclass(frozen=True)
class _GeneratedAudioStage:
    manifest: Path
    generated_records: list[DecisionRecord]
    live_fallback_records: list[DecisionRecord]
    omission_records: list[DecisionRecord]
    reviewed_waveform_records: list[DecisionRecord]


@dataclass(frozen=True)
class _VoiceControlRequirements:
    required_paths: VoiceControlPaths
    narrator_reference_bindings: dict[str, set[tuple[Path, str]]]


@dataclass(frozen=True)
class _VoiceOverrideControls:
    queue: dict[str, str]
    failure: dict[str, str]
    combined: dict[str, str]
    queue_digest: str | None
    combined_digest: str | None
    failure_paths: VoiceControlPaths


def publish_final_game_pack(
    destination: str | Path,
    *,
    state_path: str | Path,
    queue_path: str | Path,
    story_index_path: str | Path,
    voice_manifest_path: str | Path,
    live_sequence_plan_path: str | Path | None = None,
    source_audio_semantic_evidence_path: str | Path | None = None,
    failure_reference_binding_path: str | Path | None = None,
    game_id: object | None = None,
    game_version: object,
    producers: object,
    created_at: str | None = None,
) -> FinalGamePackResult:
    """Stage, verify and atomically publish one immutable game-pack directory."""
    request = _select_publication_request(
        destination,
        state_path=state_path,
        queue_path=queue_path,
        story_index_path=story_index_path,
        voice_manifest_path=voice_manifest_path,
        live_sequence_plan_path=live_sequence_plan_path,
        source_audio_semantic_evidence_path=source_audio_semantic_evidence_path,
        failure_reference_binding_path=failure_reference_binding_path,
        game_id=game_id,
        game_version=game_version,
        producers=producers,
        created_at=created_at,
    )
    initial_state = _load_initial_publication_state(request.paths.state)
    request.paths.destination.parent.mkdir(parents=True, exist_ok=True)
    with _PublicationLease(request.paths.destination) as publication_lease:
        with generation_publication_leases(
            (
                (
                    request.paths.state.parent,
                    _required_text(initial_state.get("queue_sha256"), "queue SHA-256"),
                ),
            ),
            process_checker=process_is_alive,
        ) as generation_leases:
            generation_lease = generation_leases[0]
            queue, queue_sha256, state, state_sha256 = _select_stable_publication_state(
                request.paths
            )
            with TemporaryDirectory(
                dir=request.paths.destination.parent,
                prefix=f".{request.paths.destination.name}.staging-",
            ) as staging_directory:
                controls = _stage_publication_controls(
                    request.paths, Path(staging_directory), state_sha256, queue_sha256
                )
                validated_voices = _validate_staged_voice_controls(
                    state,
                    queue,
                    request.paths,
                    controls,
                )
                generated = _stage_generated_audio(state, queue, request, controls)
                resolved_game_id, counts = _write_staged_game_pack(
                    request,
                    state,
                    queue,
                    state_sha256,
                    queue_sha256,
                    controls,
                    validated_voices,
                    generated,
                )
                _publish_staged_game_pack(
                    request.paths.destination,
                    controls,
                    generation_lease,
                    publication_lease,
                )
    return _publication_result(
        request, resolved_game_id, counts, queue_sha256, state_sha256
    )


def _select_publication_request(
    destination: str | Path,
    *,
    state_path: str | Path,
    queue_path: str | Path,
    story_index_path: str | Path,
    voice_manifest_path: str | Path,
    live_sequence_plan_path: str | Path | None,
    source_audio_semantic_evidence_path: str | Path | None,
    failure_reference_binding_path: str | Path | None,
    game_id: object | None,
    game_version: object,
    producers: object,
    created_at: str | None,
) -> _PublicationRequest:
    paths = _PublicationPaths(
        destination=_new_destination(destination),
        state=_resolved_path(state_path),
        queue=_resolved_path(queue_path),
        story=_resolved_path(story_index_path),
        voice_manifest=_resolved_path(voice_manifest_path),
        live_sequence=_optional_resolved_path(live_sequence_plan_path),
        semantic_evidence=_optional_resolved_path(source_audio_semantic_evidence_path),
        failure_reference_binding=_failure_reference_binding_path(
            failure_reference_binding_path
        ),
    )
    return _PublicationRequest(
        paths=paths,
        game_id=game_id,
        game_version=_required_text(game_version, "game version"),
        producers=_validate_producers(producers),
        created_at=created_at or _now(),
    )


def _resolved_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _optional_resolved_path(path: str | Path | None) -> Path | None:
    return None if path is None else _resolved_path(path)


def _failure_reference_binding_path(path: str | Path | None) -> Path | None:
    resolved = _optional_resolved_path(path)
    if resolved is not None and resolved.is_dir():
        return resolved / "binding.json"
    return resolved


def _load_initial_publication_state(state_path: Path) -> JsonDocument:
    try:
        return load_generation_state(state_path)
    except BulkGenerationError as error:
        raise FinalGamePackError(str(error)) from error


def _select_stable_publication_state(
    paths: _PublicationPaths,
) -> tuple[_Queue, str, JsonDocument, str]:
    try:
        queue, queue_sha256 = load_stable_generation_queue(paths.queue)
    except BulkGenerationError as error:
        raise FinalGamePackError(str(error)) from error
    state, state_sha256 = _load_stable_state(paths.state, queue, queue_sha256)
    if queue_sha256 != state["queue_sha256"]:
        raise FinalGamePackError(
            "Generation state does not match the exact queue bytes"
        )
    _require_final_review_state(state, queue)
    return queue, queue_sha256, state, state_sha256


def _stage_publication_controls(
    paths: _PublicationPaths,
    staging: Path,
    state_sha256: str,
    queue_sha256: str,
) -> _StagedControls:
    inventory = {paths.state: state_sha256}
    story_copy = staging / "story" / "story-index.jsonl"
    voice_copy = staging / "voices" / "voice-manifest.json"
    story_sha256 = _copy_control(paths.story, story_copy, inventory, "story index")
    voice_sha256 = _copy_control(
        paths.voice_manifest, voice_copy, inventory, "voice manifest"
    )
    _capture_staged_queue(paths.queue, queue_sha256, inventory)
    failure_document = _stage_failure_reference_binding(
        paths.failure_reference_binding, staging, inventory, queue_sha256, voice_sha256
    )
    story = _load_story(story_copy)
    semantic_copy, semantic_document, semantic_sha256 = _stage_semantic_evidence(
        paths.semantic_evidence, staging, story_copy, story, inventory
    )
    live_copy = _stage_live_sequence(
        paths.live_sequence, staging, story_copy, inventory
    )
    return _StagedControls(
        directory=staging,
        inventory=inventory,
        story_copy=story_copy,
        voice_copy=voice_copy,
        story=story,
        story_sha256=story_sha256,
        voice_sha256=voice_sha256,
        live_sequence_copy=live_copy,
        semantic_evidence_copy=semantic_copy,
        semantic_evidence_document=semantic_document,
        semantic_evidence_sha256=semantic_sha256,
        failure_reference_document=failure_document,
    )


def _capture_staged_queue(
    queue: Path, expected_sha256: str, inventory: Inventory
) -> None:
    if _capture_control(queue, inventory, "generation queue") != expected_sha256:
        raise FinalGamePackError(
            "Generation queue changed while publication was staged"
        )


def _stage_failure_reference_binding(
    source: Path | None,
    staging: Path,
    inventory: Inventory,
    queue_sha256: str,
    voice_sha256: str,
) -> JsonDocument | None:
    if source is None:
        return None
    try:
        document = load_failure_reference_binding_document(source.parent)
    except FailureReferenceBindingError as error:
        raise FinalGamePackError(str(error)) from error
    authority = _required_mapping(
        document.get("source_authority"), "failure-reference source authority"
    )
    if (
        authority["queue_sha256"] != queue_sha256
        or authority["voice_manifest_sha256"] != voice_sha256
    ):
        raise FinalGamePackError(
            "Failure-reference binding belongs to different pack controls"
        )
    binding_copy = staging / "voices" / "failure-reference-binding" / "binding.json"
    _copy_control(source, binding_copy, inventory, "failure-reference binding")
    _copy_failure_reference_audio(
        source.parent, binding_copy.parent, document, inventory
    )
    return document


def _copy_failure_reference_audio(
    source_root: Path,
    destination_root: Path,
    document: JsonDocument,
    inventory: Inventory,
) -> None:
    for group in _mapping_sequence(document.get("groups"), "failure-reference groups"):
        relative = _safe_relative(group["reference"], "Selected reference")
        source = _contained_source(source_root, relative, "selected reference")
        _copy_control(
            source,
            destination_root / Path(*relative.parts),
            inventory,
            "selected reference",
        )


def _stage_semantic_evidence(
    source: Path | None,
    staging: Path,
    story_copy: Path,
    story: _Story,
    inventory: Inventory,
) -> tuple[Path | None, JsonDocument | None, str | None]:
    if source is None:
        if isinstance(story.metadata.get("source_audio_semantics"), dict):
            raise FinalGamePackError(
                "Story index semantic decisions require their exact evidence file"
            )
        return None, None, None
    destination = staging / "story" / "source-audio-semantic-evidence.json"
    digest = _copy_control(
        source, destination, inventory, "source-audio semantic evidence"
    )
    try:
        document = load_source_audio_semantic_evidence(destination, story_copy)
    except SourceAudioSemanticEvidenceError as error:
        raise FinalGamePackError(str(error)) from error
    return destination, document, digest


def _stage_live_sequence(
    source: Path | None, staging: Path, story_copy: Path, inventory: Inventory
) -> Path | None:
    if source is None:
        return None
    destination = staging / "story" / "live-sequence.json"
    _copy_control(source, destination, inventory, "live sequence plan")
    try:
        load_live_sequence_plan(destination, story_copy)
    except LiveSequencePlanError as error:
        raise FinalGamePackError(str(error)) from error
    return destination


def _validate_staged_voice_controls(
    state: JsonDocument,
    queue: _Queue,
    paths: _PublicationPaths,
    controls: _StagedControls,
) -> _ValidatedVoiceControls:
    voice_document, voice_entries = _load_voices(controls.voice_copy)
    narrator_selection = _verify_voice_control_provenance(
        state,
        queue,
        paths.voice_manifest,
        voice_document,
        voice_entries,
        failure_reference_binding_path=paths.failure_reference_binding,
        failure_reference_document=controls.failure_reference_document,
    )
    voice_override = _validate_source_bindings(
        queue.metadata,
        queue_path=paths.queue,
        story_index_path=paths.story,
        voice_manifest_path=paths.voice_manifest,
        story_sha256=controls.story_sha256,
        voice_manifest_sha256=controls.voice_sha256,
        reviewed_waveform_publication=state.get("reviewed_waveform_publication"),
    )
    _validate_story_identity(state, controls.story)
    projection = _copy_portable_voice_manifest_and_references(
        paths.voice_manifest,
        controls.voice_copy,
        voice_document,
        voice_entries,
        controls.inventory,
    )
    return _ValidatedVoiceControls(narrator_selection, voice_override, projection)


def _stage_generated_audio(
    state: JsonDocument,
    queue: _Queue,
    request: _PublicationRequest,
    controls: _StagedControls,
) -> _GeneratedAudioStage:
    manifest = controls.directory / "generated" / "manifest.json"
    if not _reviewed_waveform_supersedes_legacy_authority(state):
        try:
            validate_authoring_publication_authority(request.paths.state, state)
        except BulkGenerationError as error:
            raise FinalGamePackError(str(error)) from error
    generated_records = approved_manifest_entries(state, request.paths.state.parent)
    live_fallback_records = _decision_records(
        state, queue, "live_fallback", "Live fallback item"
    )
    omission_records = _decision_records(
        state, queue, "audio_event_omission", "Audio-event omission"
    )
    reviewed_waveform_records = _reviewed_waveform_publication_records(state, queue)
    _validate_generated_story_records(
        controls.story,
        generated_records,
        live_fallback_records,
        omission_records,
        reviewed_waveform_records,
    )
    _copy_generated_audio(
        generated_records,
        request.paths.state.parent,
        manifest.parent,
        controls.inventory,
    )
    _write_generated_manifest(
        manifest,
        state,
        request.created_at,
        generated_records,
        live_fallback_records,
        omission_records,
        reviewed_waveform_records,
    )
    return _GeneratedAudioStage(
        manifest,
        generated_records,
        live_fallback_records,
        omission_records,
        reviewed_waveform_records,
    )


def _validate_generated_story_records(
    story: _Story,
    generated: Sequence[DecisionRecord],
    fallback: Sequence[DecisionRecord],
    omissions: Sequence[DecisionRecord],
    reviewed: Sequence[DecisionRecord],
) -> None:
    _validate_story_records(generated, story, "Approved generated item")
    _validate_story_records(fallback, story, "Live fallback item")
    _validate_story_records(omissions, story, "Audio-event omission")
    _validate_story_records(reviewed, story, "Reviewed waveform")


def _copy_generated_audio(
    records: Sequence[DecisionRecord],
    source_root: Path,
    destination_root: Path,
    inventory: Inventory,
) -> None:
    for record in records:
        relative = _safe_relative(record["audio"], "Generated-audio state path")
        source = _contained_source(source_root, relative, "generated WAV")
        _copy_control(
            source, destination_root / Path(*relative.parts), inventory, "generated WAV"
        )


def _write_generated_manifest(
    manifest: Path,
    state: JsonDocument,
    created_at: str,
    generated: Sequence[DecisionRecord],
    fallback: Sequence[DecisionRecord],
    omissions: Sequence[DecisionRecord],
    reviewed: Sequence[DecisionRecord],
) -> None:
    try:
        write_generated_audio_manifest(
            manifest,
            {
                "game": state.get("game"),
                "language": state.get("language"),
                "source_queue_sha256": state["queue_sha256"],
                "generated_at": created_at,
                "vntts.authoring.live_fallback": {
                    "schema_version": 1,
                    "mode": "explicit",
                    "entries": fallback,
                },
                "vntts.authoring.audio_event_omission": {
                    "schema_version": 1,
                    "mode": "explicit",
                    "entries": omissions,
                },
                "vntts.authoring.reviewed_waveform_publication": {
                    "schema_version": 1,
                    "mode": "exact_reviewed_waveform",
                    "entries": reviewed,
                },
            },
            generated,
        )
    except GeneratedAudioManifestError as error:
        raise FinalGamePackError(str(error)) from error


def _write_staged_game_pack(
    request: _PublicationRequest,
    state: JsonDocument,
    queue: _Queue,
    state_sha256: str,
    queue_sha256: str,
    controls: _StagedControls,
    voices: _ValidatedVoiceControls,
    generated: _GeneratedAudioStage,
) -> tuple[str, dict[str, int]]:
    counts = _review_counts(state)
    game_id = _required_text(
        request.game_id if request.game_id is not None else state.get("game"), "game id"
    )
    pack_manifest = controls.directory / "game-pack.json"
    try:
        write_game_pack(
            pack_manifest,
            _game_pack_metadata(
                request,
                state,
                queue,
                state_sha256,
                queue_sha256,
                controls,
                voices,
                generated,
                counts,
                game_id,
            ),
            _game_pack_components(controls, generated),
        )
        load_game_pack(pack_manifest)
    except GamePackError as error:
        raise FinalGamePackError(str(error)) from error
    return game_id, counts


def _game_pack_components(
    controls: _StagedControls, generated: _GeneratedAudioStage
) -> dict[str, Path]:
    components = {
        "story_index": controls.story_copy,
        "voice_manifest": controls.voice_copy,
        "generated_audio": generated.manifest,
    }
    if controls.live_sequence_copy is not None:
        components["live_sequence_plan"] = controls.live_sequence_copy
    return components


def _game_pack_metadata(
    request: _PublicationRequest,
    state: JsonDocument,
    queue: _Queue,
    state_sha256: str,
    queue_sha256: str,
    controls: _StagedControls,
    voices: _ValidatedVoiceControls,
    generated: _GeneratedAudioStage,
    counts: dict[str, int],
    game_id: str,
) -> dict[str, object]:
    return {
        "game": {"id": game_id, "version": request.game_version},
        "producers": request.producers,
        "created_at": request.created_at,
        "vntts.authoring": {
            "source_queue_sha256": queue_sha256,
            "source_state_sha256": state_sha256,
            "selected_voice_manifest_sha256": controls.voice_sha256,
            "queue_voice_manifest_sha256": queue.metadata.get(
                "source_voice_manifest_sha256"
            ),
            "voice_manifest_override": voices.voice_override,
            "narrator_selection": voices.narrator_selection,
            "failure_reference_binding": _failure_reference_metadata(
                controls.failure_reference_document
            ),
            "source_audio_semantic_evidence": _semantic_evidence_metadata(controls),
            "reviewed_waveform_publication": _reviewed_waveform_metadata(
                state, generated
            ),
            "voice_reference_projection": voices.projection,
            **counts,
        },
    }


def _failure_reference_metadata(
    document: JsonDocument | None,
) -> dict[str, object] | None:
    if document is None:
        return None
    return {
        "path": "voices/failure-reference-binding/binding.json",
        "binding_id": document["binding_id"],
        "audit_id": document["audit_id"],
        "decision_set_id": document["decision_set_id"],
    }


def _semantic_evidence_metadata(
    controls: _StagedControls,
) -> dict[str, object] | None:
    if controls.semantic_evidence_document is None:
        return None
    entries = controls.semantic_evidence_document["entries"]
    assert isinstance(entries, list)
    return {
        "path": "story/source-audio-semantic-evidence.json",
        "sha256": controls.semantic_evidence_sha256,
        "evidence_id": controls.semantic_evidence_document["evidence_id"],
        "entry_count": len(entries),
    }


def _reviewed_waveform_metadata(
    state: JsonDocument, generated: _GeneratedAudioStage
) -> dict[str, object] | None:
    if not generated.reviewed_waveform_records:
        return None
    return {
        "batch_id": _required_mapping(
            state.get("reviewed_waveform_publication"), "reviewed-waveform publication"
        ).get("batch_id"),
        "approved_count": len(generated.reviewed_waveform_records),
        "synthesis_reproducibility": False,
    }


def _publish_staged_game_pack(
    destination: Path,
    controls: _StagedControls,
    generation_lease: GenerationLease,
    publication_lease: _PublicationLease,
) -> None:
    generation_lease.assert_owned()
    publication_lease.assert_owned()
    _assert_controls_unchanged(controls.inventory)
    if _path_exists(destination):
        raise FinalGamePackError(
            f"Final game-pack destination already exists: {destination}"
        )
    try:
        _rename_directory_no_replace(controls.directory, destination)
    except (AtomicPublicationError, OSError) as error:
        raise FinalGamePackError(
            f"Unable to atomically publish final game pack: {error}"
        ) from error
    publication_lease.mark_committed()
    generation_lease.mark_committed()


def _publication_result(
    request: _PublicationRequest,
    game_id: str,
    counts: dict[str, int],
    queue_sha256: str,
    state_sha256: str,
) -> FinalGamePackResult:
    destination = request.paths.destination
    return FinalGamePackResult(
        directory=destination,
        manifest=destination / "game-pack.json",
        live_sequence_plan=(
            None
            if request.paths.live_sequence is None
            else destination / "story" / "live-sequence.json"
        ),
        source_audio_semantic_evidence=(
            None
            if request.paths.semantic_evidence is None
            else destination / "story" / "source-audio-semantic-evidence.json"
        ),
        game_id=game_id,
        game_version=request.game_version,
        approved_count=counts["approved_count"],
        rejected_count=counts["rejected_count"],
        live_fallback_count=counts["live_fallback_count"],
        omitted_count=counts["omitted_count"],
        source_queue_sha256=queue_sha256,
        source_state_sha256=state_sha256,
    )


class _PublicationLease:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.path = destination.parent / f".{destination.name}.publication.json"
        self.guard_path = self.path.with_suffix(".guard")
        self.owner = uuid4().hex
        self.committed = False

    def __enter__(self) -> _PublicationLease:
        payload = {
            "schema": "vntts.game-pack-publication-lease",
            "schema_version": 1,
            "owner": self.owner,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process_started_at": process_started_at(os.getpid()),
            "destination": str(self.destination),
            "created_at": _now(),
        }
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        try:
            with exclusive_advisory_lock(self.guard_path):
                if _path_exists(self.destination):
                    raise FinalGamePackError(
                        "Final game-pack destination already exists: "
                        f"{self.destination}"
                    )
                if self.path.exists():
                    try:
                        existing_payload = self.path.read_bytes()
                        existing = json.loads(existing_payload.decode("utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise FinalGamePackError(
                            f"Unable to inspect publication lease: {error}"
                        ) from error
                    if self._existing_is_live(existing):
                        raise FinalGamePackError(
                            f"Another final game-pack publication owns {self.path}"
                        )
                    self._archive_stale(existing_payload)
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
        except FileExistsError as error:
            raise FinalGamePackError(
                "Another final game-pack publication acquired the destination"
            ) from error
        except AdvisoryLockBusyError as error:
            raise FinalGamePackError(
                "Another final game-pack publication is acquiring the destination"
            ) from error
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        ownership_lost = False
        try:
            with exclusive_advisory_lock(self.guard_path, blocking=True):
                try:
                    document = json.loads(self.path.read_text(encoding="utf-8"))
                except OSError, json.JSONDecodeError:
                    ownership_lost = True
                else:
                    if document.get("owner") == self.owner:
                        self.path.unlink()
                    else:
                        ownership_lost = True
        except AdvisoryLockBusyError:
            ownership_lost = True
        if ownership_lost and exc_type is None and not self.committed:
            raise FinalGamePackError(
                "Final game-pack publication lease ownership was lost"
            )
        return False

    def assert_owned(self) -> None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FinalGamePackError(
                "Final game-pack publication lease became unreadable"
            ) from error
        if document.get("owner") != self.owner:
            raise FinalGamePackError(
                "Final game-pack publication lease ownership was lost"
            )

    def mark_committed(self) -> None:
        self.committed = True

    def _existing_is_live(self, document: object) -> bool:
        if (
            not isinstance(document, dict)
            or document.get("schema") != "vntts.game-pack-publication-lease"
            or document.get("schema_version") != 1
            or not isinstance(document.get("owner"), str)
            or not document["owner"]
            or document.get("destination") != str(self.destination)
        ):
            return True
        if document.get("hostname") != socket.gethostname():
            return True
        pid = document.get("pid")
        if not process_is_alive(pid):
            return False
        expected_start = document.get("process_started_at")
        if not expected_start:
            return True
        return bool(process_started_at(pid) == expected_start)

    def _archive_stale(self, expected_payload: bytes) -> None:
        try:
            payload = self.path.read_bytes()
        except OSError as error:
            raise FinalGamePackError(
                f"Unable to inspect stale publication lease: {error}"
            ) from error
        if payload != expected_payload:
            raise FinalGamePackError(
                "Final game-pack publication lease changed during stale recovery"
            )
        digest = hashlib.sha256(payload).hexdigest()[:12]
        archive = self.path.with_name(f"{self.path.name}.interrupted-{digest}")
        if _path_exists(archive):
            raise FinalGamePackError(
                f"Stale publication lease archive already exists: {archive}"
            )
        if self.path.read_bytes() != expected_payload:
            raise FinalGamePackError(
                "Final game-pack publication lease changed during stale recovery"
            )
        os.replace(self.path, archive)


def _require_final_review_state(state: JsonDocument, queue: _Queue) -> None:
    if state.get("active") is not None:
        raise FinalGamePackError(
            "Generation state has an active or interrupted attempt; resume it first"
        )
    queue_ids = {item.queue_id for item in queue.items}
    items = _state_items(state)
    state_ids = set(items)
    if state_ids != queue_ids:
        missing = queue_ids.difference(state_ids)
        extra = state_ids.difference(queue_ids)
        if missing:
            raise FinalGamePackError(
                f"Generation state is missing {len(missing)} selected queue item(s)"
            )
        raise FinalGamePackError(
            f"Generation state contains {len(extra)} unknown queue item(s)"
        )
    pending = []
    failed = []
    for queue_id, item in items.items():
        if item.get("status") == "failed":
            failed.append(queue_id)
        elif item.get("review_status") == "pending_review":
            pending.append(queue_id)
    if failed:
        raise FinalGamePackError(
            f"Generation state has {len(failed)} failed item(s); resolve them first"
        )
    if pending:
        raise FinalGamePackError(
            f"Generation state has {len(pending)} pending review item(s)"
        )


def _review_counts(state: JsonDocument) -> dict[str, int]:
    items = _state_items(state)
    return {
        "approved_count": sum(
            item.get("status") == "approved" for item in items.values()
        ),
        "rejected_count": sum(
            item.get("review_status") == "rejected" for item in items.values()
        ),
        "live_fallback_count": sum(
            isinstance(item.get("live_fallback"), dict) for item in items.values()
        ),
        "omitted_count": sum(
            isinstance(item.get("audio_event_omission"), dict)
            for item in items.values()
        ),
        "state_item_count": len(items),
    }


def _reviewed_waveform_supersedes_legacy_authority(state: JsonDocument) -> bool:
    migrated = reviewed_waveform_publication_queue_ids(state)
    approved = {
        queue_id
        for queue_id, item in _state_items(state).items()
        if item.get("status") == "approved" and item.get("review_status") == "approved"
    }
    return bool(approved) and migrated == approved


def _decision_records(
    state: JsonDocument, queue: _Queue, field: str, label: str
) -> list[DecisionRecord]:
    queue_ids = {item.queue_id for item in queue.items}
    records = []
    for queue_id, item in _state_items(state).items():
        decision = item.get(field)
        if not isinstance(decision, dict):
            continue
        if queue_id not in queue_ids:
            raise FinalGamePackError(f"{label} {queue_id!r} is missing from the queue")
        records.append(
            {
                **copy.deepcopy(decision),
                "decision_sha256": canonical_document_sha256(decision),
            }
        )
    return sorted(records, key=lambda value: (value["line_id"], value["text_sha256"]))


def _reviewed_waveform_publication_records(
    state: JsonDocument, queue: _Queue
) -> list[DecisionRecord]:
    publication = state.get("reviewed_waveform_publication")
    if not isinstance(publication, dict):
        return []
    queue_ids = {item.queue_id for item in queue.items}
    records = []
    for ledger in publication["items"]:
        queue_id = ledger["queue_id"]
        if queue_id not in queue_ids:
            raise FinalGamePackError(
                f"Reviewed waveform {queue_id!r} is missing from the queue"
            )
        records.append(
            {
                "batch_id": publication["batch_id"],
                "queue_id": queue_id,
                "line_id": ledger["line_id"],
                "text_sha256": ledger["text_sha256"],
                "speaker": ledger["speaker"],
                "audio_sha256": ledger["file_sha256"],
                "base_result_sha256": ledger["base_result_sha256"],
                "route": copy.deepcopy(ledger["route"]),
                "synthesis_reproducibility": False,
            }
        )
    return sorted(records, key=lambda value: (value["line_id"], value["text_sha256"]))


def _copy_control(
    source: str | Path, destination: str | Path, inventory: Inventory, label: str
) -> str:
    source = Path(source).expanduser().resolve()
    destination = Path(destination)
    digest = _capture_control(source, inventory, label)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(source, destination)
        copied_digest = sha256_file(destination)
    except OSError as error:
        raise FinalGamePackError(f"Unable to copy {label} {source}: {error}") from error
    if copied_digest != digest:
        raise FinalGamePackError(f"{label.capitalize()} changed while it was copied")
    return digest


def _capture_control(source: str | Path, inventory: Inventory, label: str) -> str:
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise FinalGamePackError(f"{label.capitalize()} does not exist: {source}")
    try:
        digest = str(sha256_file(source))
    except OSError as error:
        raise FinalGamePackError(
            f"Unable to checksum {label} {source}: {error}"
        ) from error
    previous = inventory.get(source)
    if previous is not None and previous != digest:
        raise FinalGamePackError(f"{label.capitalize()} changed during staging")
    inventory[source] = digest
    return digest


def _assert_controls_unchanged(inventory: Inventory) -> None:
    for source, expected in inventory.items():
        try:
            actual = sha256_file(source)
        except OSError as error:
            raise FinalGamePackError(
                f"Publication source became unreadable: {source}: {error}"
            ) from error
        if actual != expected:
            raise FinalGamePackError(
                f"Publication source changed during staging: {source}"
            )


def _copy_portable_voice_manifest_and_references(
    source_manifest: Path,
    destination_manifest: Path,
    document: JsonDocument,
    entries: Sequence[_VoiceEntry],
    inventory: Inventory,
) -> DecisionRecord | None:
    source_root = source_manifest.parent.resolve()
    destination_root = destination_manifest.parent
    copied: dict[Path, str] = {}
    projections: list[DecisionRecord] = []
    rewritten = copy.deepcopy(document)
    raw_voices = rewritten.get("voices")
    if not isinstance(raw_voices, list) or len(raw_voices) != len(entries):
        raise FinalGamePackError("Voice manifest entries changed during staging")
    for raw_entry, entry in zip(raw_voices, entries, strict=True):
        _copy_portable_voice_entry(
            raw_entry,
            entry,
            source_root,
            destination_root,
            inventory,
            copied,
            projections,
        )
    if not projections:
        return None
    source_manifest_sha256 = sha256_file(source_manifest)
    atomic_write_json(destination_manifest, rewritten, sort_keys=True)
    try:
        _load_voices(destination_manifest)
    except FinalGamePackError as error:
        raise FinalGamePackError(
            f"Projected voice manifest is invalid: {error}"
        ) from error
    return {
        "schema": "vntts.authoring-voice-reference-projection",
        "schema_version": 1,
        "method": "decode_to_pcm16_mono_wav",
        "source_manifest_sha256": source_manifest_sha256,
        "output_manifest_sha256": sha256_file(destination_manifest),
        "entries": projections,
    }


def _copy_portable_voice_entry(
    raw_entry: object,
    entry: _VoiceEntry,
    source_root: Path,
    destination_root: Path,
    inventory: Inventory,
    copied: dict[Path, str],
    projections: list[DecisionRecord],
) -> None:
    if not isinstance(raw_entry, dict):
        raise FinalGamePackError("Voice manifest references changed during staging")
    configured_references = raw_entry.get("references")
    if (
        not isinstance(configured_references, list)
        or tuple(configured_references) != entry.references
    ):
        raise FinalGamePackError("Voice manifest references changed during staging")
    portable_references = [
        _copy_portable_voice_reference(
            configured,
            entry.character,
            source_root,
            destination_root,
            inventory,
            copied,
            projections,
        )
        for configured in entry.references
    ]
    raw_entry["references"] = portable_references


def _copy_portable_voice_reference(
    configured: str,
    character: str,
    source_root: Path,
    destination_root: Path,
    inventory: Inventory,
    copied: dict[Path, str],
    projections: list[DecisionRecord],
) -> str:
    relative = _safe_relative(configured, "Voice reference")
    source = _contained_source(source_root, relative, "voice reference")
    source_sha256 = _capture_control(source, inventory, "voice reference")
    portable = _portable_voice_reference_path(relative)
    destination = destination_root / Path(*portable.parts)
    if destination in copied:
        if copied[destination] != source_sha256:
            raise FinalGamePackError(
                f"Portable voice reference path collides: {portable.as_posix()}"
            )
        return portable.as_posix()
    _copy_or_project_voice_reference(
        source,
        relative,
        portable,
        destination,
        character,
        source_sha256,
        inventory,
        projections,
    )
    copied[destination] = source_sha256
    return portable.as_posix()


def _portable_voice_reference_path(relative: PurePosixPath) -> PurePosixPath:
    if relative.suffix.casefold() == ".wav":
        return relative
    return relative.with_name(f"{relative.stem}.vntts-pcm16.wav")


def _copy_or_project_voice_reference(
    source: Path,
    relative: PurePosixPath,
    portable: PurePosixPath,
    destination: Path,
    character: str,
    source_sha256: str,
    inventory: Inventory,
    projections: list[DecisionRecord],
) -> None:
    if portable == relative:
        _copy_control(source, destination, inventory, "voice reference")
        return
    projections.append(
        _project_voice_reference(
            source, relative, portable, destination, character, source_sha256
        )
    )


def _project_voice_reference(
    source: Path,
    relative: PurePosixPath,
    portable: PurePosixPath,
    destination: Path,
    character: str,
    source_sha256: str,
) -> DecisionRecord:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        samples, sample_rate = sf.read(source, dtype="float32", always_2d=True)
        if samples.size == 0 or sample_rate < 1:
            raise ValueError("decoded reference is empty")
        sf.write(
            destination,
            np.clip(np.mean(samples, axis=1, dtype=np.float32), -1.0, 1.0),
            sample_rate,
            format="WAV",
            subtype="PCM_16",
        )
        info = probe_pcm16_mono_wav(destination)
    except Exception as error:
        raise FinalGamePackError(
            f"Unable to project voice reference {source} to PCM16 WAV: {error}"
        ) from error
    return {
        "character": character,
        "source_reference": relative.as_posix(),
        "source_sha256": source_sha256,
        "output_reference": portable.as_posix(),
        "output_sha256": sha256_file(destination),
        "sample_rate": info.sample_rate,
        "sample_count": info.sample_count,
        "channels": 1,
        "subtype": "PCM_16",
    }


def _contained_source(root: str | Path, relative: PurePosixPath, label: str) -> Path:
    root = Path(root).resolve()
    candidate = (root / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise FinalGamePackError(
            f"{label.capitalize()} leaves its source root"
        ) from error
    if not candidate.is_file():
        raise FinalGamePackError(f"{label.capitalize()} does not exist: {candidate}")
    return candidate


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise FinalGamePackError(f"{label} must be a safe POSIX-relative path")
    relative = PurePosixPath(value.strip())
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise FinalGamePackError(f"{label} must be a safe POSIX-relative path")
    return relative


def _load_story(path: Path) -> _Story:
    try:
        story: _Story = load_story_index_document(path)
        return story
    except StoryIndexError as error:
        raise FinalGamePackError(str(error)) from error


def _load_voices(path: Path) -> tuple[JsonDocument, Sequence[_VoiceEntry]]:
    try:
        document, entries = load_voice_manifest(path, allow_legacy=False)
    except VoiceManifestError as error:
        raise FinalGamePackError(str(error)) from error
    return document, entries


def _validate_source_bindings(
    queue_metadata: Mapping[str, object],
    *,
    queue_path: Path,
    story_index_path: Path,
    voice_manifest_path: Path,
    story_sha256: str,
    voice_manifest_sha256: str,
    reviewed_waveform_publication: object | None = None,
) -> bool:
    story_path, declared_story_sha256 = _declared_source_binding(
        queue_metadata,
        queue_path,
        reviewed_waveform_publication,
        "source_story_index",
        "source_story_index_sha256",
        "story index",
        "selected_story_index_sha256",
        story_sha256,
    )
    migrated_story_authorized = _reviewed_waveform_selects(
        reviewed_waveform_publication, "selected_story_index_sha256", story_sha256
    )
    _validate_story_source_binding(
        story_path,
        declared_story_sha256,
        story_index_path,
        story_sha256,
        migrated_story_authorized,
    )
    voice_path, declared_voice_sha256 = _declared_source_binding(
        queue_metadata,
        queue_path,
        reviewed_waveform_publication,
        "source_voice_manifest",
        "source_voice_manifest_sha256",
        "voice manifest",
        "selected_voice_manifest_sha256",
        voice_manifest_sha256,
    )
    if voice_path is None:
        if declared_voice_sha256 != voice_manifest_sha256:
            raise FinalGamePackError(
                "Reviewed-waveform voice manifest checksum does not match the "
                "selected source"
            )
        return False
    return (
        voice_path != voice_manifest_path.resolve()
        or declared_voice_sha256 != voice_manifest_sha256
    )


def _declared_source_binding(
    queue_metadata: Mapping[str, object],
    queue_path: Path,
    reviewed_waveform_publication: object | None,
    path_field: str,
    hash_field: str,
    label: str,
    migration_hash_field: str,
    selected_sha256: str,
) -> tuple[Path | None, str]:
    declared_path = queue_metadata.get(path_field)
    declared_hash = queue_metadata.get(hash_field)
    if not isinstance(declared_path, str) or not declared_path.strip():
        if _reviewed_waveform_selects(
            reviewed_waveform_publication, migration_hash_field, selected_sha256
        ):
            return None, selected_sha256
        raise FinalGamePackError(
            f"Generation queue lacks required {label} source path binding; "
            "migrate it first"
        )
    if not isinstance(declared_hash, str) or len(declared_hash) != 64:
        raise FinalGamePackError(
            f"Generation queue lacks required {label} checksum binding; "
            "migrate it first"
        )
    path = Path(declared_path).expanduser()
    if not path.is_absolute():
        path = queue_path.parent / path
    return path.resolve(), declared_hash


def _reviewed_waveform_selects(
    publication: object | None, field: str, selected_sha256: str
) -> bool:
    return isinstance(publication, dict) and publication.get(field) == selected_sha256


def _validate_story_source_binding(
    declared_path: Path | None,
    declared_sha256: str,
    selected_path: Path,
    selected_sha256: str,
    migrated_authorized: bool,
) -> None:
    if declared_sha256 != selected_sha256 and not migrated_authorized:
        raise FinalGamePackError(
            "Generation queue story index checksum does not match the selected source"
        )
    if (
        declared_path is not None
        and declared_path != selected_path.resolve()
        and not migrated_authorized
    ):
        raise FinalGamePackError(
            "Generation queue story index path does not match the selected source"
        )


def _load_stable_state(
    state_path: Path, queue: _Queue, queue_sha256: str
) -> tuple[JsonDocument, str]:
    try:
        payload = state_path.read_bytes()
        state = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise FinalGamePackError(
            f"Unable to read generation state {state_path}: {error}"
        ) from error
    if not isinstance(state, dict):
        raise FinalGamePackError("Generation state must be a JSON object")
    try:
        validate_generation_state_document(
            state, state_path.parent, queue, queue_sha256
        )
    except BulkGenerationError as error:
        raise FinalGamePackError(str(error)) from error
    return state, hashlib.sha256(payload).hexdigest()


def _validate_story_identity(state: JsonDocument, story: _Story) -> None:
    for field in ("game", "language"):
        state_value = state.get(field)
        story_value = getattr(story, field)
        if (
            state_value is not None
            and story_value is not None
            and state_value != story_value
        ):
            raise FinalGamePackError(
                f"Generation state {field} does not match the story index"
            )


def _verify_voice_control_provenance(
    state: JsonDocument,
    queue: _Queue,
    voice_manifest_path: Path,
    voice_document: JsonDocument,
    voice_entries: Sequence[_VoiceEntry],
    *,
    failure_reference_binding_path: Path | None = None,
    failure_reference_document: JsonDocument | None = None,
) -> DecisionRecord | None:
    migrated = reviewed_waveform_publication_queue_ids(state)
    queue_by_id = {item.queue_id: item for item in queue.items}
    registry = _synthesis_controls_registry(state, migrated)
    requirements = _voice_control_requirements(voice_manifest_path, voice_entries)
    overrides = _voice_override_controls(
        voice_document,
        queue_by_id,
        voice_entries,
        failure_reference_binding_path,
        failure_reference_document,
    )
    migrated_selection = _migrated_narrator_selection(
        state, voice_manifest_path, requirements.narrator_reference_bindings
    )
    selections: set[tuple[str, str]] = set()
    for queue_id, result in _state_items(state).items():
        if _requires_voice_provenance(queue_id, result, migrated):
            selections.update(
                _verify_state_voice_controls(
                    queue_id,
                    result,
                    queue_by_id,
                    registry,
                    requirements,
                    overrides,
                    failure_reference_binding_path,
                )
            )
    if len(selections) > 1:
        raise FinalGamePackError("Generation state mixes multiple narrator selections")
    return _resolved_narrator_selection(selections, migrated_selection)


def _synthesis_controls_registry(
    state: JsonDocument, migrated: Set[str]
) -> dict[str, object]:
    registry = state.get("synthesis_controls")
    if isinstance(registry, dict):
        return registry
    if any(
        result.get("status") == "approved" and queue_id not in migrated
        for queue_id, result in _state_items(state).items()
    ):
        raise FinalGamePackError(
            "Generation state lacks per-control synthesis provenance; regenerate "
            "or migrate it first"
        )
    return {}


def _voice_control_requirements(
    voice_manifest_path: Path, voice_entries: Sequence[_VoiceEntry]
) -> _VoiceControlRequirements:
    required_paths: VoiceControlPaths = {
        voice_manifest_path.resolve(): (
            _source_sha256(voice_manifest_path, "voice manifest"),
            lambda role: role == "voice_manifest",
        )
    }
    bindings: dict[str, set[tuple[Path, str]]] = {}
    source_root = voice_manifest_path.parent.resolve()
    for entry in voice_entries:
        _add_voice_entry_control_requirements(
            entry, source_root, required_paths, bindings
        )
    return _VoiceControlRequirements(required_paths, bindings)


def _add_voice_entry_control_requirements(
    entry: _VoiceEntry,
    source_root: Path,
    required_paths: VoiceControlPaths,
    bindings: dict[str, set[tuple[Path, str]]],
) -> None:
    for configured in entry.references:
        relative = _safe_relative(configured, "Voice reference")
        source = _contained_source(source_root, relative, "voice reference")
        digest = _source_sha256(source, "voice reference")
        required_paths[source] = (
            digest,
            lambda role: role.startswith("voice_reference:"),
        )
        for name in (entry.character, *entry.aliases):
            bindings.setdefault(normalize_character_name(name), set()).add(
                (source, digest)
            )


def _voice_override_controls(
    voice_document: JsonDocument,
    queue_by_id: dict[str, _QueueItem],
    voice_entries: Sequence[_VoiceEntry],
    failure_reference_binding_path: Path | None,
    failure_reference_document: JsonDocument | None,
) -> _VoiceOverrideControls:
    try:
        queue_overrides = dict(
            queue_voice_overrides_from_manifest(
                voice_document, queue_ids=queue_by_id, voices=voice_entries
            )
        )
    except SourceReferenceBindingError as error:
        raise FinalGamePackError(str(error)) from error
    queue_digest = (
        queue_voice_overrides_sha256(queue_overrides) if queue_overrides else None
    )
    failure, paths = _failure_reference_override_controls(
        failure_reference_binding_path, failure_reference_document
    )
    combined = {**queue_overrides, **failure}
    combined_digest = (
        queue_voice_overrides_sha256(combined) if failure else queue_digest
    )
    return _VoiceOverrideControls(
        queue_overrides, failure, combined, queue_digest, combined_digest, paths
    )


def _failure_reference_override_controls(
    path: Path | None, document: JsonDocument | None
) -> tuple[dict[str, str], VoiceControlPaths]:
    if document is None:
        return {}, {}
    if path is None:
        raise FinalGamePackError(
            "Failure-reference binding document has no source path"
        )
    overrides = _text_mapping(
        document.get("queue_voice_overrides"), "failure-reference voice overrides"
    )
    controls: VoiceControlPaths = {
        path.resolve(): (
            _source_sha256(path, "failure-reference binding"),
            lambda role: role == "failure_reference_binding",
        )
    }
    root = path.parent.resolve()
    for group in _mapping_sequence(document.get("groups"), "failure-reference groups"):
        _add_failure_reference_control(group, root, controls)
    return overrides, controls


def _add_failure_reference_control(
    group: dict[str, object], root: Path, controls: VoiceControlPaths
) -> None:
    relative = _safe_relative(group["reference"], "Selected reference")
    source = _contained_source(root, relative, "selected reference")
    digest = _source_sha256(source, "selected reference")
    if digest != group["reference_sha256"]:
        raise FinalGamePackError("Failure-reference selected audio changed")
    controls[source] = (
        digest,
        lambda role: role.startswith("failure_reference_selected:"),
    )


def _migrated_narrator_selection(
    state: JsonDocument,
    voice_manifest_path: Path,
    bindings: dict[str, set[tuple[Path, str]]],
) -> DecisionRecord | None:
    publication = state.get("reviewed_waveform_publication")
    if not isinstance(publication, dict):
        return None
    if publication["selected_voice_manifest_sha256"] != _source_sha256(
        voice_manifest_path, "voice manifest"
    ):
        raise FinalGamePackError(
            "Reviewed-waveform publication belongs to a different voice manifest"
        )
    character = publication["narrator_character"]
    configured_digests = sorted(
        {
            digest
            for _path, digest in bindings.get(
                normalize_character_name(character), set()
            )
        }
    )
    if configured_digests != publication["narrator_reference_sha256s"]:
        raise FinalGamePackError("Reviewed-waveform narrator binding changed")
    return {"character": character, "reference_sha256s": configured_digests}


def _requires_voice_provenance(
    queue_id: str, result: dict[str, object], migrated: Set[str]
) -> bool:
    if result.get("status") in {"live_fallback", "omitted"}:
        return False
    if (
        result.get("status") == "generated"
        and result.get("review_status") == "rejected"
    ):
        return False
    return queue_id not in migrated


def _verify_state_voice_controls(
    queue_id: str,
    result: dict[str, object],
    queue_by_id: dict[str, _QueueItem],
    registry: dict[str, object],
    requirements: _VoiceControlRequirements,
    overrides: _VoiceOverrideControls,
    failure_reference_binding_path: Path | None,
) -> set[tuple[str, str]]:
    controls = _state_synthesis_controls(queue_id, result, registry)
    controls_by_path = _control_paths(controls)
    _verify_control_paths(
        requirements.required_paths,
        controls_by_path,
        queue_id,
        "Voice input",
    )
    binding_present = (
        failure_reference_binding_path is not None
        and failure_reference_binding_path.resolve() in controls_by_path
    )
    if binding_present:
        _verify_control_paths(
            overrides.failure_paths,
            controls_by_path,
            queue_id,
            "Failure-reference input",
            include_path=False,
        )
    _verify_source_reference_binding(
        queue_id,
        result,
        queue_by_id[queue_id],
        overrides,
        binding_present,
    )
    return _verify_narrator_controls(
        queue_id,
        result,
        queue_by_id[queue_id],
        controls,
        requirements.narrator_reference_bindings,
    )


def _state_synthesis_controls(
    queue_id: str, result: dict[str, object], registry: dict[str, object]
) -> list[dict[str, object]]:
    provenance = result.get("synthesis_provenance_sha256")
    controls = registry.get(provenance) if isinstance(provenance, str) else None
    if not isinstance(controls, list):
        raise FinalGamePackError(
            f"State item {queue_id!r} lacks its exact synthesis-control inventory"
        )
    provenance_document = {
        "provider": result.get("provider"),
        "model": result.get("model"),
        "generation_profile": result.get("generation_profile"),
        "text_transform": result.get("text_transform"),
        "controls": [
            {"role": control["role"], "sha256": control["sha256"]}
            for control in controls
        ],
    }
    configuration = result.get("synthesis_configuration")
    if configuration is not None:
        provenance_document.update(
            _required_mapping(configuration, "synthesis configuration")
        )
    if canonical_document_sha256(provenance_document) != provenance:
        raise FinalGamePackError(
            f"State item {queue_id!r} synthesis provenance is inconsistent"
        )
    return [_required_mapping(control, "synthesis control") for control in controls]


def _control_paths(
    controls: Sequence[dict[str, object]],
) -> dict[Path, dict[str, object]]:
    paths: dict[Path, dict[str, object]] = {}
    for control in controls:
        path = control.get("path")
        if control.get("kind") == "file" and isinstance(path, str):
            paths[Path(path).expanduser().resolve()] = control
    return paths


def _verify_control_paths(
    required: VoiceControlPaths,
    controls_by_path: dict[Path, dict[str, object]],
    queue_id: str,
    label: str,
    *,
    include_path: bool = True,
) -> None:
    for path, (digest, role_matches) in required.items():
        control = controls_by_path.get(path)
        if (
            control is None
            or not _control_role_matches(control, role_matches)
            or control.get("sha256") != digest
        ):
            detail = f"{label} {path}" if include_path else label
            raise FinalGamePackError(
                f"{detail} does not match synthesis controls for {queue_id!r}"
            )


def _control_role_matches(control: dict[str, object], matcher: RoleMatcher) -> bool:
    role = control.get("role")
    return isinstance(role, str) and matcher(role)


def _verify_source_reference_binding(
    queue_id: str,
    result: dict[str, object],
    item: _QueueItem,
    overrides: _VoiceOverrideControls,
    binding_present: bool,
) -> None:
    if queue_id in overrides.failure and not binding_present:
        raise FinalGamePackError(
            f"Failure-reference controls are missing for {queue_id!r}"
        )
    effective_character = result.get("voice_character") or synthesis_character_for_line(
        item.speaker, item.voice_character
    )
    expected = overrides.combined.get(queue_id)
    binding = result.get("source_reference_binding")
    expected_digest = (
        overrides.combined_digest if binding_present else overrides.queue_digest
    )
    if expected is None:
        if binding is not None:
            raise FinalGamePackError(
                f"State item {queue_id!r} has an unselected source-reference binding"
            )
        return
    if (
        effective_character != expected
        or not isinstance(binding, dict)
        or binding.get("queue_voice_overrides_sha256") != expected_digest
    ):
        raise FinalGamePackError(
            f"Source-reference voice binding is missing for {queue_id!r}"
        )


def _verify_narrator_controls(
    queue_id: str,
    result: dict[str, object],
    item: _QueueItem,
    controls: Sequence[dict[str, object]],
    bindings: dict[str, set[tuple[Path, str]]],
) -> set[tuple[str, str]]:
    narrator_controls = [
        control
        for control in controls
        if str(control.get("role", "")).startswith("narrator_selection:")
    ]
    effective_character = result.get("voice_character") or synthesis_character_for_line(
        item.speaker, item.voice_character
    )
    if effective_character == "Narrator" and len(narrator_controls) != 1:
        raise FinalGamePackError(
            f"Narrator item {queue_id!r} lacks one role-bound narrator selection"
        )
    return {
        _narrator_selection_from_control(queue_id, control, bindings)
        for control in narrator_controls
    }


def _narrator_selection_from_control(
    queue_id: str,
    control: dict[str, object],
    bindings: dict[str, set[tuple[Path, str]]],
) -> tuple[str, str]:
    character = str(control["role"]).removeprefix("narrator_selection:")
    try:
        path_value = control["path"]
        path = (
            Path(path_value).expanduser().resolve()
            if isinstance(path_value, str)
            else None
        )
    except KeyError, TypeError, OSError:
        path = None
    if control.get("kind") != "file" or (
        path,
        control.get("sha256"),
    ) not in bindings.get(normalize_character_name(character), set()):
        raise FinalGamePackError(
            f"Narrator selection for {queue_id!r} is not role-bound to "
            f"the selected voice manifest character {character!r}"
        )
    return character, str(control["sha256"])


def _resolved_narrator_selection(
    selections: set[tuple[str, str]], migrated: DecisionRecord | None
) -> DecisionRecord | None:
    if migrated is not None:
        if selections:
            character, digest = next(iter(selections))
            if (
                normalize_character_name(character)
                != normalize_character_name(str(migrated["character"]))
                or not isinstance(migrated["reference_sha256s"], list)
                or digest not in migrated["reference_sha256s"]
            ):
                raise FinalGamePackError(
                    "Reviewed and reproducible narrator selections conflict"
                )
        return migrated
    if not selections:
        return None
    character, digest = next(iter(selections))
    return {"character": character, "reference_sha256": digest}


def _validate_story_records(
    records: Sequence[DecisionRecord], story: _Story, label: str
) -> None:
    lines = {record.line_id: record for record in story.records}
    for record in records:
        line_id = record.get("line_id")
        if not isinstance(line_id, str):
            raise FinalGamePackError(f"{label} has no valid story line id")
        line = lines.get(line_id)
        if line is None or line.text_sha256 != record.get("text_sha256"):
            raise FinalGamePackError(
                f"{label} {record['line_id']!r} does not match the story index"
            )


def _validate_producers(producers: object) -> list[Producer]:
    if not isinstance(producers, (list, tuple)) or not producers:
        raise FinalGamePackError("At least one producer name/version is required")
    validated: list[Producer] = []
    for index, producer in enumerate(producers):
        if not isinstance(producer, dict) or set(producer) != {"name", "version"}:
            raise FinalGamePackError(
                f"Producer {index} must contain exactly name and version"
            )
        validated.append(
            {
                "name": _required_text(producer["name"], f"producer {index} name"),
                "version": _required_text(
                    producer["version"], f"producer {index} version"
                ),
            }
        )
    return validated


def _source_sha256(path: str | Path, label: str) -> str:
    try:
        return str(sha256_file(path))
    except OSError as error:
        raise FinalGamePackError(
            f"Unable to checksum {label} {path}: {error}"
        ) from error


def _new_destination(value: str | Path) -> Path:
    try:
        candidate = Path(value).expanduser()
    except TypeError as error:
        raise FinalGamePackError("Final game-pack destination is invalid") from error
    if not candidate.name or candidate.name in {".", ".."}:
        raise FinalGamePackError(
            "Final game-pack destination requires a directory name"
        )
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.parent.resolve() / candidate.name


def _path_exists(path: str | Path) -> bool:
    return os.path.lexists(path)


def _required_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise FinalGamePackError(f"{label.capitalize()} is invalid")
    return {str(key): item for key, item in value.items()}


def _mapping_sequence(value: object, label: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        raise FinalGamePackError(f"{label.capitalize()} are invalid")
    return tuple(_required_mapping(item, label) for item in value)


def _text_mapping(value: object, label: str) -> dict[str, str]:
    document = _required_mapping(value, label)
    if not all(isinstance(item, str) for item in document.values()):
        raise FinalGamePackError(f"{label.capitalize()} is invalid")
    return {key: str(item) for key, item in document.items()}


def _state_items(state: JsonDocument) -> dict[str, dict[str, object]]:
    return {
        queue_id: _required_mapping(item, "generation state item")
        for queue_id, item in _required_mapping(
            state.get("items"), "generation state items"
        ).items()
    }


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinalGamePackError(f"{label.capitalize()} must be non-empty text")
    return value.strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
