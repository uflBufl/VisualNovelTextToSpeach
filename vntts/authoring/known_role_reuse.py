"""Publish an exact explicit mapping from one known story role to another voice."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict

from vntts_artifacts import VoiceGenerationQueueItem
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import (
    VoiceManifestEntry,
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
    write_voice_manifest,
)

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    is_spoken_queue_item,
)
from vntts.authoring.generation_state import (
    load_stable_generation_queue,
    validate_generation_state_document,
)
from vntts.authoring.missing_voice_live_fallback import (
    JsonObject,
    MissingVoiceAuthority,
    MissingVoiceLiveFallbackError,
    _load_authority,
    _validated_targets,
)
from vntts.authoring.publication import rename_directory_no_replace, staged_directory
from vntts.authoring.source_reference_bindings import (
    KNOWN_ROLE_REUSE_AUTHORITY,
    KNOWN_ROLE_REUSE_BINDING_FIELD,
    KNOWN_ROLE_REUSE_BINDING_SCHEMA,
    KNOWN_ROLE_REUSE_BINDING_VERSION,
    MISSING_VOICE_REUSE_APPROVED_BINDING_VERSION,
    MISSING_VOICE_REUSE_BINDING_FIELD,
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
    retired_source_reference_variants_from_manifest,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    contained_workspace_path,
    inspect_workspace,
    safe_workspace_relative_path,
)
from vntts.authoring.workspace_config import selected_voice_manifest_path
from vntts.authoring.workspace_foundation import load_json_object
from vntts.voices import synthesis_character_for_line

KNOWN_ROLE_REUSE_DECISION_SCHEMA = "vntts.authoring-known-role-reuse-decision"
KNOWN_ROLE_REUSE_DECISION_VERSION = 1
KNOWN_ROLE_REUSE_BUNDLE_SCHEMA = "vntts.authoring-known-role-reuse-bundle"
KNOWN_ROLE_REUSE_BUNDLE_VERSION = 1


class ReferenceRecord(TypedDict):
    relative: Path
    source: Path
    sha256: str


class TargetRecord(TypedDict):
    queue_id: str
    line_id: str
    text_sha256: str
    speaker: str
    declared_voice_character: str
    source_state: str
    source_state_item_sha256: str | None


class RetiredRecord(TypedDict):
    variant_id: str
    record_sha256: str
    queue_ids: list[str]


@dataclass(frozen=True)
class WorkspaceSnapshot:
    directory: Path
    document: JsonObject
    document_sha256: str
    queue_items: tuple[VoiceGenerationQueueItem, ...]
    queue_sha256: str
    queue_by_id: dict[str, VoiceGenerationQueueItem]
    state: JsonObject
    state_sha256: str


@dataclass(frozen=True)
class UnresolvedAuthoritySnapshot:
    directory: Path
    authority: MissingVoiceAuthority
    decision: JsonObject
    binding: JsonObject
    targets: list[JsonObject]


@dataclass(frozen=True)
class ManifestSelection:
    successor: JsonObject
    manifest_path: Path
    source_manifest_sha256: str
    authority_document: JsonObject
    voices: list[VoiceManifestEntry]
    reuse_voice: VoiceManifestEntry
    all_references: list[ReferenceRecord]
    reuse_references: list[ReferenceRecord]


@dataclass(frozen=True)
class TargetSelection:
    unresolved_ids: set[str]
    records: list[TargetRecord]
    rejected: dict[str, str]
    approved: dict[str, str]
    retired: list[RetiredRecord]


@dataclass(frozen=True)
class PublicationIdentity:
    binding: JsonObject
    decision_body: JsonObject
    decision_id: str


class KnownRoleReuseError(RuntimeError):
    """An explicit known-role reuse authority is missing or inconsistent."""


@dataclass(frozen=True)
class KnownRoleReuseResult:
    directory: Path
    source_character: str
    reuse_voice_character: str
    target_count: int
    absent_count: int
    rejected_count: int
    preserved_approved_count: int
    retired_variant_count: int
    decision_id: str
    applied: bool
    created: bool

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "directory": str(self.directory)}


def publish_known_role_reuse_binding(
    workspace: str | Path,
    unresolved_authority_directory: str | Path,
    source_character: str,
    reuse_voice_character: str,
    output_directory: str | Path,
    *,
    accept_known_role_reuse: bool = False,
) -> KnownRoleReuseResult:
    """Preflight or publish one exact absent/rejected role-to-voice overlay."""
    workspace, unresolved_directory, output, source_character, reuse_character = (
        _publish_inputs(
            workspace,
            unresolved_authority_directory,
            source_character,
            reuse_voice_character,
            output_directory,
        )
    )
    snapshot = _load_workspace_snapshot(workspace)
    unresolved = _load_unresolved_authority(
        unresolved_directory, source_character, snapshot.queue_by_id
    )
    _validate_unresolved_workspace(snapshot, unresolved)
    selection = _select_manifest(snapshot, unresolved, reuse_character)
    targets = _select_targets(
        snapshot, unresolved, selection.authority_document, source_character
    )
    identity = _publication_identity(
        snapshot,
        unresolved,
        selection,
        targets,
        source_character,
        unresolved_directory,
    )
    selection.successor[KNOWN_ROLE_REUSE_BINDING_FIELD] = identity.binding
    result = KnownRoleReuseResult(
        output,
        source_character,
        selection.reuse_voice.character,
        len(targets.records),
        len(targets.unresolved_ids),
        len(targets.rejected),
        len(targets.approved),
        len(targets.retired),
        identity.decision_id,
        bool(accept_known_role_reuse),
        False,
    )
    if not accept_known_role_reuse:
        return result
    if output.exists():
        _validate_bundle(
            output, identity.binding, identity.decision_body, snapshot.queue_by_id
        )
        return result
    _publish_bundle(output, unresolved_directory, snapshot, selection, identity)
    return KnownRoleReuseResult(**{**asdict(result), "created": True})


def _publish_inputs(
    workspace: str | Path,
    unresolved_directory: str | Path,
    source_character: str,
    reuse_character: str,
    output: str | Path,
) -> tuple[Path, Path, Path, str, str]:
    workspace_path = Path(workspace).expanduser().resolve()
    unresolved_path = Path(unresolved_directory).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    source = _required_text(source_character, "Known source character")
    reuse = _required_text(reuse_character, "Known reuse voice character")
    if normalize_character_name(source) == normalize_character_name(reuse):
        raise KnownRoleReuseError("Known-role reuse requires two different characters")
    return workspace_path, unresolved_path, output_path, source, reuse


def _load_workspace_snapshot(workspace: Path) -> WorkspaceSnapshot:
    inspect_workspace(workspace)
    workspace_path = workspace / "workspace.json"
    document = _read_json(workspace_path, "workspace")
    document_sha256 = sha256_file(workspace_path)
    queue, queue_sha256 = load_stable_generation_queue(workspace / "queue.jsonl")
    queue_by_id = {item.queue_id: item for item in queue.items}
    state_path = workspace / "generated-audio/generation-state.json"
    state_payload = state_path.read_bytes()
    state = _decode_state(state_payload)
    validate_generation_state_document(state, state_path.parent, queue, queue_sha256)
    if state.get("active") is not None or any(state_path.parent.rglob("*.partial.wav")):
        raise KnownRoleReuseError("Known-role reuse requires an inactive workspace")
    return WorkspaceSnapshot(
        workspace,
        document,
        document_sha256,
        tuple(queue.items),
        queue_sha256,
        queue_by_id,
        state,
        hashlib.sha256(state_payload).hexdigest(),
    )


def _load_unresolved_authority(
    directory: Path,
    source_character: str,
    queue_by_id: dict[str, VoiceGenerationQueueItem],
) -> UnresolvedAuthoritySnapshot:
    try:
        authority = _load_authority(directory)
        decision = authority["decision"]
        binding = _object_field(decision, "binding", "Known-role unresolved binding")
        targets = _validated_targets(
            authority["plan"], binding, source_character, queue_by_id
        )
    except MissingVoiceLiveFallbackError as error:
        raise KnownRoleReuseError(str(error)) from error
    return UnresolvedAuthoritySnapshot(directory, authority, decision, binding, targets)


def _validate_unresolved_workspace(
    snapshot: WorkspaceSnapshot, unresolved: UnresolvedAuthoritySnapshot
) -> None:
    plan = unresolved.authority["plan"]
    plan_source = _object_field(plan, "source", "Known-role unresolved plan source")
    source_workspace = Path(
        _text_field(plan_source, "workspace", "Known-role unresolved source workspace")
    ).resolve()
    source_document = _read_json(
        source_workspace / "workspace.json", "unresolved source workspace"
    )
    if (
        snapshot.document.get("source") != source_document.get("source")
        or snapshot.queue_sha256
        != _text_field(plan_source, "queue_sha256", "Known-role unresolved queue")
        or unresolved.binding.get("source_workspace_id")
        != _text_field(plan_source, "workspace_id", "Known-role unresolved source")
        or unresolved.binding.get("source_workspace_sha256")
        != _text_field(plan_source, "workspace_sha256", "Known-role unresolved source")
    ):
        raise KnownRoleReuseError(
            "Known-role workspace differs from the unresolved source authority"
        )


def _select_manifest(
    snapshot: WorkspaceSnapshot,
    unresolved: UnresolvedAuthoritySnapshot,
    reuse_character: str,
) -> ManifestSelection:
    selected_path = selected_voice_manifest_path(
        snapshot.directory, snapshot.document, error_type=AuthoringWorkbenchError
    )
    if selected_path is None:
        raise KnownRoleReuseError("Known-role selected voice manifest is unavailable")
    selected_sha256 = sha256_file(selected_path)
    workspace_manifest = _object_field(
        snapshot.document, "voice_manifest", "Known-role workspace voice manifest"
    )
    if selected_sha256 != _text_field(
        workspace_manifest, "sha256", "Known-role workspace voice manifest"
    ):
        raise KnownRoleReuseError("Known-role source voice manifest changed")
    authority_path = unresolved.directory / "manifest.json"
    try:
        selected_document = _read_json(selected_path, "selected voice manifest")
        _metadata, selected_voices = load_voice_manifest(
            selected_path, allow_legacy=False
        )
        authority_document = _read_json(authority_path, "authority voice manifest")
        _metadata, authority_voices = load_voice_manifest(
            authority_path, allow_legacy=False
        )
        queue_voice_overrides_from_manifest(
            selected_document,
            queue_ids=snapshot.queue_by_id,
            voices=selected_voices,
        )
        queue_voice_overrides_from_manifest(
            authority_document,
            queue_ids=snapshot.queue_by_id,
            voices=authority_voices,
        )
    except (VoiceManifestError, SourceReferenceBindingError) as error:
        raise KnownRoleReuseError(str(error)) from error
    successor, manifest_path, voices = _manifest_successor(
        selected_document,
        authority_document,
        selected_path,
        authority_path,
        selected_voices,
        authority_voices,
        unresolved.binding,
        selected_sha256,
    )
    if authority_document.get(MISSING_VOICE_REUSE_BINDING_FIELD) != unresolved.binding:
        raise KnownRoleReuseError(
            "Source manifest does not contain the exact unresolved authority"
        )
    reuse_voice = _resolve_exact_voice(voices, reuse_character)
    references = tuple(reference for voice in voices for reference in voice.references)
    return ManifestSelection(
        successor,
        manifest_path,
        selected_sha256,
        authority_document,
        voices,
        reuse_voice,
        _reference_records(manifest_path.parent, references),
        _reference_records(manifest_path.parent, reuse_voice.references),
    )


def _manifest_successor(
    selected_document: JsonObject,
    authority_document: JsonObject,
    selected_path: Path,
    authority_path: Path,
    selected_voices: list[VoiceManifestEntry],
    authority_voices: list[VoiceManifestEntry],
    binding: JsonObject,
    selected_sha256: str,
) -> tuple[JsonObject, Path, list[VoiceManifestEntry]]:
    if KNOWN_ROLE_REUSE_BINDING_FIELD in selected_document or (
        KNOWN_ROLE_REUSE_BINDING_FIELD in authority_document
    ):
        raise KnownRoleReuseError(
            "Source manifest already contains a known-role reuse authority"
        )
    authority_predecessor = {
        key: value
        for key, value in authority_document.items()
        if key != MISSING_VOICE_REUSE_BINDING_FIELD
    }
    selected_predecessor = {
        key: value
        for key, value in selected_document.items()
        if key != MISSING_VOICE_REUSE_BINDING_FIELD
    }
    selected_reuse = selected_document.get(MISSING_VOICE_REUSE_BINDING_FIELD)
    if authority_predecessor != selected_predecessor:
        raise KnownRoleReuseError(
            "Unresolved authority belongs to different selected voice controls"
        )
    if selected_reuse is None:
        if binding.get("source_voice_manifest_sha256") != selected_sha256:
            raise KnownRoleReuseError(
                "Unresolved authority belongs to different selected voice controls"
            )
        return copy.deepcopy(authority_document), authority_path, authority_voices
    if not isinstance(selected_reuse, dict) or (
        selected_reuse.get("schema_version")
        != MISSING_VOICE_REUSE_APPROVED_BINDING_VERSION
        or selected_reuse.get("mode") != "approved_cohort_reuse"
        or selected_reuse.get("source_voice_manifest_sha256")
        != binding.get("source_voice_manifest_sha256")
    ):
        raise KnownRoleReuseError(
            "Selected voice controls are not an additive reviewed reuse overlay"
        )
    return copy.deepcopy(selected_document), selected_path, selected_voices


def _select_targets(
    snapshot: WorkspaceSnapshot,
    unresolved: UnresolvedAuthoritySnapshot,
    authority_document: JsonObject,
    source_character: str,
) -> TargetSelection:
    unresolved_ids = {
        _text_field(target, "queue_id", "Known-role unresolved target queue ID")
        for target in unresolved.targets
    }
    role_items = [
        item
        for item in snapshot.queue_items
        if is_spoken_queue_item(item)
        and normalize_character_name(
            synthesis_character_for_line(item.speaker, item.voice_character)
        )
        == normalize_character_name(source_character)
    ]
    state_items = _object_field(snapshot.state, "items", "Generation state items")
    records, rejected, approved = _target_records(
        role_items, unresolved_ids, state_items
    )
    absent_ids = {
        record["queue_id"] for record in records if record["source_state"] == "absent"
    }
    if absent_ids != unresolved_ids:
        raise KnownRoleReuseError(
            "Known-role unresolved authority does not cover every absent role item"
        )
    retired = _retired_records(authority_document, rejected)
    return TargetSelection(unresolved_ids, records, rejected, approved, retired)


def _target_records(
    role_items: list[VoiceGenerationQueueItem],
    unresolved_ids: set[str],
    state_items: JsonObject,
) -> tuple[list[TargetRecord], dict[str, str], dict[str, str]]:
    records: list[TargetRecord] = []
    rejected: dict[str, str] = {}
    approved: dict[str, str] = {}
    for item in sorted(role_items, key=lambda value: value.queue_id):
        record = _target_record(item, unresolved_ids, state_items, rejected, approved)
        if record is not None:
            records.append(record)
    return records, rejected, approved


def _target_record(
    item: VoiceGenerationQueueItem,
    unresolved_ids: set[str],
    state_items: JsonObject,
    rejected: dict[str, str],
    approved: dict[str, str],
) -> TargetRecord | None:
    queue_id = item.queue_id
    result = state_items.get(queue_id)
    if queue_id in unresolved_ids:
        if result is not None:
            raise KnownRoleReuseError(
                f"Unresolved known-role target is no longer absent: {queue_id!r}"
            )
        source_state, state_sha256 = "absent", None
    elif isinstance(result, dict) and (
        result.get("status") == "generated"
        and result.get("review_status") == "rejected"
    ):
        source_state = "rejected"
        state_sha256 = canonical_document_sha256(result)
        rejected[queue_id] = state_sha256
    elif isinstance(result, dict) and (
        result.get("status") == "approved" and result.get("review_status") == "approved"
    ):
        approved[queue_id] = canonical_document_sha256(result)
        return None
    else:
        raise KnownRoleReuseError(
            f"Known-role queue item has unsupported current state: {queue_id!r}"
        )
    return {
        "queue_id": queue_id,
        "line_id": item.line_id,
        "text_sha256": item.text_sha256,
        "speaker": item.speaker,
        "declared_voice_character": synthesis_character_for_line(
            item.speaker, item.voice_character
        ),
        "source_state": source_state,
        "source_state_item_sha256": state_sha256,
    }


def _retired_records(
    authority_document: JsonObject, rejected: dict[str, str]
) -> list[RetiredRecord]:
    records: list[RetiredRecord] = []
    for record in retired_source_reference_variants_from_manifest(authority_document):
        queue_ids = sorted(
            _text_list(record.get("queue_ids"), "Retired source-reference queue IDs")
        )
        if set(queue_ids).issubset(rejected):
            records.append(
                {
                    "variant_id": _text_field(
                        record, "variant_id", "Retired source-reference variant ID"
                    ),
                    "record_sha256": canonical_document_sha256(record),
                    "queue_ids": queue_ids,
                }
            )
    return sorted(records, key=lambda value: value["variant_id"])


def _publication_identity(
    snapshot: WorkspaceSnapshot,
    unresolved: UnresolvedAuthoritySnapshot,
    selection: ManifestSelection,
    targets: TargetSelection,
    source_character: str,
    unresolved_directory: Path,
) -> PublicationIdentity:
    plan = unresolved.authority["plan"]
    unresolved_queue_ids = sorted(targets.unresolved_ids)
    cohort_ids = sorted(
        _text_field(decision, "cohort_id", "Known-role reuse cohort ID")
        for decision in _object_list(
            unresolved.binding.get("decisions"), "Known-role reuse decisions"
        )
    )
    overrides = {
        record["queue_id"]: selection.reuse_voice.character
        for record in targets.records
    }
    binding: JsonObject = {
        "schema": KNOWN_ROLE_REUSE_BINDING_SCHEMA,
        "schema_version": KNOWN_ROLE_REUSE_BINDING_VERSION,
        "mode": "explicit_role_reuse",
        "source_voice_manifest_sha256": selection.source_manifest_sha256,
        "source_workspace_id": _text_field(
            snapshot.document, "workspace_id", "Known-role workspace ID"
        ),
        "source_workspace_sha256": snapshot.document_sha256,
        "source_state_sha256": snapshot.state_sha256,
        "queue_sha256": snapshot.queue_sha256,
        "source_character": source_character,
        "reuse_voice_character": selection.reuse_voice.character,
        "reuse_reference_sha256s": sorted(
            record["sha256"] for record in selection.reuse_references
        ),
        "unresolved_authority": {
            "bundle_id": _text_field(
                unresolved.authority["bundle"],
                "bundle_id",
                "Known-role unresolved bundle ID",
            ),
            "bundle_sha256": unresolved.authority["bundle_sha256"],
            "decision_id": _text_field(
                unresolved.decision, "decision_id", "Known-role unresolved decision ID"
            ),
            "decision_sha256": unresolved.authority["decision_sha256"],
            "plan_id": _text_field(plan, "plan_id", "Known-role unresolved plan ID"),
            "cohort_ids": cohort_ids,
            "queue_ids": unresolved_queue_ids,
        },
        "retired_variants": targets.retired,
        "targets": targets.records,
        "preserved_approved_queue_ids": sorted(targets.approved),
        "source_rejected_state_item_sha256s": dict(sorted(targets.rejected.items())),
        "queue_voice_overrides": overrides,
        "queue_voice_overrides_sha256": queue_voice_overrides_sha256(overrides),
        "authority": KNOWN_ROLE_REUSE_AUTHORITY,
    }
    body: JsonObject = {
        "schema": KNOWN_ROLE_REUSE_DECISION_SCHEMA,
        "schema_version": KNOWN_ROLE_REUSE_DECISION_VERSION,
        "source_workspace": str(snapshot.directory),
        "unresolved_authority_directory": str(unresolved_directory),
        "binding": binding,
    }
    return PublicationIdentity(binding, body, canonical_document_sha256(body))


def _publish_bundle(
    output: Path,
    unresolved_directory: Path,
    snapshot: WorkspaceSnapshot,
    selection: ManifestSelection,
    identity: PublicationIdentity,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with staged_directory(output.parent, prefix=".known-role-reuse-") as staging:
            _copy_references(staging, selection.all_references)
            manifest_path = staging / "manifest.json"
            write_voice_manifest(manifest_path, selection.successor)
            queue_voice_overrides_from_manifest(
                selection.successor,
                queue_ids=snapshot.queue_by_id,
                voices=load_voice_manifest(manifest_path, allow_legacy=False)[1],
            )
            atomic_write_json(
                staging / "decision.json",
                {**identity.decision_body, "decision_id": identity.decision_id},
                sort_keys=True,
            )
            _copy_tree(unresolved_directory, staging / "authority" / "unresolved")
            _write_bundle(staging, identity.decision_id)
            _validate_bundle(
                staging, identity.binding, identity.decision_body, snapshot.queue_by_id
            )
            rename_directory_no_replace(staging, output)
    except (
        AuthoringWorkbenchError,
        BulkGenerationError,
        SourceReferenceBindingError,
        VoiceManifestError,
    ) as error:
        raise KnownRoleReuseError(str(error)) from error


def _copy_references(staging: Path, references: list[ReferenceRecord]) -> None:
    for reference in references:
        target = staging / reference["relative"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(reference["source"], target)
        if sha256_file(target) != reference["sha256"]:
            raise KnownRoleReuseError("Known-role voice reference changed while copied")


def _write_bundle(staging: Path, decision_id: str) -> None:
    inventory = [
        {"path": path.relative_to(staging).as_posix(), "sha256": sha256_file(path)}
        for path in sorted(staging.rglob("*"))
        if path.is_file()
    ]
    body = {
        "schema": KNOWN_ROLE_REUSE_BUNDLE_SCHEMA,
        "schema_version": KNOWN_ROLE_REUSE_BUNDLE_VERSION,
        "decision_id": decision_id,
        "inventory": inventory,
    }
    atomic_write_json(
        staging / "bundle.json",
        {**body, "bundle_id": canonical_document_sha256(body)},
        sort_keys=True,
    )


def _validate_bundle(
    directory: str | Path,
    expected_binding: dict[str, object],
    decision_body: dict[str, object],
    queue_by_id: dict[str, VoiceGenerationQueueItem],
) -> None:
    directory = Path(directory).resolve()
    bundle = _read_json(directory / "bundle.json", "known-role bundle")
    inventory = _validated_bundle_inventory(bundle, decision_body)
    _validate_inventory_files(directory, inventory)
    _validate_bundle_decision(directory, expected_binding, decision_body)
    _validate_bundle_manifest(directory, expected_binding, queue_by_id)


def _validated_bundle_inventory(
    bundle: JsonObject, decision_body: JsonObject
) -> list[object]:
    body = {key: value for key, value in bundle.items() if key != "bundle_id"}
    if (
        bundle.get("schema") != KNOWN_ROLE_REUSE_BUNDLE_SCHEMA
        or bundle.get("schema_version") != KNOWN_ROLE_REUSE_BUNDLE_VERSION
        or bundle.get("bundle_id") != canonical_document_sha256(body)
        or bundle.get("decision_id") != canonical_document_sha256(decision_body)
    ):
        raise KnownRoleReuseError("Known-role bundle identity is invalid")
    inventory = bundle.get("inventory")
    if not isinstance(inventory, list) or not inventory:
        raise KnownRoleReuseError("Known-role bundle inventory is empty")
    return inventory


def _validate_inventory_files(directory: Path, inventory: list[object]) -> None:
    declared = set()
    for record in inventory:
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise KnownRoleReuseError("Known-role bundle inventory is malformed")
        relative = safe_workspace_relative_path(
            record["path"], "Known-role bundle artifact"
        )
        artifact = contained_workspace_path(
            directory, relative, "Known-role bundle artifact"
        )
        if (
            artifact.is_symlink()
            or not artifact.is_file()
            or sha256_file(artifact) != record["sha256"]
        ):
            raise KnownRoleReuseError("Known-role bundle artifact changed")
        declared.add(relative.as_posix())
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path != directory / "bundle.json"
    }
    if declared != actual:
        raise KnownRoleReuseError("Known-role bundle inventory is incomplete")


def _validate_bundle_decision(
    directory: Path, expected_binding: JsonObject, decision_body: JsonObject
) -> None:
    decision = _read_json(directory / "decision.json", "known-role decision")
    if (
        decision
        != {**decision_body, "decision_id": canonical_document_sha256(decision_body)}
        or decision.get("binding") != expected_binding
    ):
        raise KnownRoleReuseError("Known-role decision changed")


def _validate_bundle_manifest(
    directory: Path,
    expected_binding: JsonObject,
    queue_by_id: dict[str, VoiceGenerationQueueItem],
) -> None:
    manifest_path = directory / "manifest.json"
    manifest = _read_json(manifest_path, "known-role manifest")
    try:
        _metadata, voices = load_voice_manifest(manifest_path, allow_legacy=False)
        queue_voice_overrides_from_manifest(
            manifest,
            queue_ids=queue_by_id,
            voices=voices,
        )
    except (VoiceManifestError, SourceReferenceBindingError) as error:
        raise KnownRoleReuseError(str(error)) from error
    for voice in voices:
        for value in voice.references:
            relative = safe_workspace_relative_path(
                value, "Known-role manifest reference"
            )
            reference = contained_workspace_path(
                directory, relative, "Known-role manifest reference"
            )
            if reference.is_symlink() or not reference.is_file():
                raise KnownRoleReuseError(
                    f"Known-role manifest reference is missing: {value!r}"
                )
    if manifest.get(KNOWN_ROLE_REUSE_BINDING_FIELD) != expected_binding:
        raise KnownRoleReuseError("Known-role manifest binding changed")


def _resolve_exact_voice(
    voices: list[VoiceManifestEntry], character: str
) -> VoiceManifestEntry:
    matches = [
        voice
        for voice in voices
        if normalize_character_name(voice.character)
        == normalize_character_name(character)
    ]
    if len(matches) != 1 or not matches[0].references:
        raise KnownRoleReuseError(
            f"Known-role selected voice is missing or ambiguous: {character!r}"
        )
    return matches[0]


def _reference_records(
    root: Path, references: tuple[str, ...]
) -> list[ReferenceRecord]:
    records: list[ReferenceRecord] = []
    seen: set[str] = set()
    for value in references:
        relative = safe_workspace_relative_path(value, "Known-role voice reference")
        if relative.as_posix() in seen:
            continue
        seen.add(relative.as_posix())
        source = contained_workspace_path(root, relative, "Known-role voice reference")
        if source.is_symlink() or not source.is_file():
            raise KnownRoleReuseError(
                f"Known-role voice reference is unsafe: {value!r}"
            )
        records.append(
            {
                "relative": relative,
                "source": source,
                "sha256": sha256_file(source),
            }
        )
    if not records:
        raise KnownRoleReuseError("Known-role selected voice has no references")
    return records


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise KnownRoleReuseError("Known-role unresolved authority is unsafe")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise KnownRoleReuseError("Known-role authority contains a symlink")
        if path.is_dir():
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        if sha256_file(target) != sha256_file(path):
            raise KnownRoleReuseError("Known-role authority changed while copied")


def _decode_state(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnownRoleReuseError(str(error)) from error
    if not isinstance(value, dict):
        raise KnownRoleReuseError("Generation state must be a JSON object")
    return value


def _read_json(path: str | Path, label: str) -> dict[str, object]:
    return load_json_object(path, label, error_type=KnownRoleReuseError)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnownRoleReuseError(f"{label} must be non-empty text")
    return value.strip()


def _object_field(document: Mapping[str, object], field: str, label: str) -> JsonObject:
    value = document.get(field)
    if not isinstance(value, dict):
        raise KnownRoleReuseError(f"{label} is invalid")
    return value


def _object_list(value: object, label: str) -> list[JsonObject]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise KnownRoleReuseError(f"{label} is invalid")
    return value


def _text_field(document: Mapping[str, object], field: str, label: str) -> str:
    return _required_text(document.get(field), label)


def _text_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise KnownRoleReuseError(f"{label} is invalid")
    return value


__all__ = [
    "KNOWN_ROLE_REUSE_BUNDLE_SCHEMA",
    "KNOWN_ROLE_REUSE_BUNDLE_VERSION",
    "KNOWN_ROLE_REUSE_DECISION_SCHEMA",
    "KNOWN_ROLE_REUSE_DECISION_VERSION",
    "KnownRoleReuseError",
    "KnownRoleReuseResult",
    "publish_known_role_reuse_binding",
]
