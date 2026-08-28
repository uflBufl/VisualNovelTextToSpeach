"""Publish an exact explicit mapping from one known story role to another voice."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
    write_voice_manifest,
)

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    is_spoken_queue_item,
    validate_generation_state_document,
)
from vntts.authoring.generation_state import load_stable_generation_queue
from vntts.authoring.missing_voice_live_fallback import (
    MissingVoiceLiveFallbackError,
    _load_authority,
    _validated_targets,
)
from vntts.authoring.publication import rename_directory_no_replace
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
from vntts.voices import synthesis_character_for_line

KNOWN_ROLE_REUSE_DECISION_SCHEMA = "vntts.authoring-known-role-reuse-decision"
KNOWN_ROLE_REUSE_DECISION_VERSION = 1
KNOWN_ROLE_REUSE_BUNDLE_SCHEMA = "vntts.authoring-known-role-reuse-bundle"
KNOWN_ROLE_REUSE_BUNDLE_VERSION = 1


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

    def to_dict(self):
        return {**asdict(self), "directory": str(self.directory)}


def publish_known_role_reuse_binding(
    workspace,
    unresolved_authority_directory,
    source_character,
    reuse_voice_character,
    output_directory,
    *,
    accept_known_role_reuse=False,
):
    """Preflight or publish one exact absent/rejected role-to-voice overlay."""
    workspace = Path(workspace).expanduser().resolve()
    unresolved_authority_directory = (
        Path(unresolved_authority_directory).expanduser().resolve()
    )
    output = Path(output_directory).expanduser().resolve()
    source_character = _required_text(source_character, "Known source character")
    reuse_voice_character = _required_text(
        reuse_voice_character, "Known reuse voice character"
    )
    if normalize_character_name(source_character) == normalize_character_name(
        reuse_voice_character
    ):
        raise KnownRoleReuseError("Known-role reuse requires two different characters")

    inspect_workspace(workspace)
    workspace_path = workspace / "workspace.json"
    workspace_document = _read_json(workspace_path, "workspace")
    workspace_sha256 = sha256_file(workspace_path)
    queue_path = workspace / "queue.jsonl"
    queue, queue_sha256 = load_stable_generation_queue(queue_path)
    queue_by_id = {item.queue_id: item for item in queue.items}
    state_path = workspace / "generated-audio/generation-state.json"
    state_payload = state_path.read_bytes()
    state_sha256 = hashlib.sha256(state_payload).hexdigest()
    state = _decode_state(state_payload)
    validate_generation_state_document(state, state_path.parent, queue, queue_sha256)
    if state.get("active") is not None or any(state_path.parent.rglob("*.partial.wav")):
        raise KnownRoleReuseError("Known-role reuse requires an inactive workspace")

    try:
        unresolved = _load_authority(unresolved_authority_directory)
        unresolved_targets = _validated_targets(
            unresolved["plan"],
            unresolved["decision"]["binding"],
            source_character,
            queue_by_id,
        )
    except MissingVoiceLiveFallbackError as error:
        raise KnownRoleReuseError(str(error)) from error
    plan = unresolved["plan"]
    plan_source_workspace = Path(plan["source"]["workspace"]).resolve()
    plan_source_document = _read_json(
        plan_source_workspace / "workspace.json", "unresolved source workspace"
    )
    binding = unresolved["decision"]["binding"]
    if (
        workspace_document.get("source") != plan_source_document.get("source")
        or queue_sha256 != plan["source"]["queue_sha256"]
        or binding.get("source_workspace_id") != plan["source"]["workspace_id"]
        or binding.get("source_workspace_sha256") != plan["source"]["workspace_sha256"]
    ):
        raise KnownRoleReuseError(
            "Known-role workspace differs from the unresolved source authority"
        )

    selected_voice_manifest = selected_voice_manifest_path(
        workspace,
        workspace_document,
        error_type=AuthoringWorkbenchError,
    )
    selected_voice_sha256 = sha256_file(selected_voice_manifest)
    if selected_voice_sha256 != workspace_document["voice_manifest"]["sha256"]:
        raise KnownRoleReuseError("Known-role source voice manifest changed")
    authority_voice_manifest = unresolved_authority_directory / "manifest.json"
    try:
        selected_voice_document = _read_json(
            selected_voice_manifest, "selected voice manifest"
        )
        _selected_metadata, selected_voices = load_voice_manifest(
            selected_voice_manifest, allow_legacy=False
        )
        voice_document = _read_json(
            authority_voice_manifest, "authority voice manifest"
        )
        _metadata, voices = load_voice_manifest(
            authority_voice_manifest, allow_legacy=False
        )
        queue_voice_overrides_from_manifest(
            selected_voice_document,
            queue_ids=queue_by_id,
            voices=selected_voices,
        )
        queue_voice_overrides_from_manifest(
            voice_document,
            queue_ids=queue_by_id,
            voices=voices,
        )
    except (VoiceManifestError, SourceReferenceBindingError) as error:
        raise KnownRoleReuseError(str(error)) from error
    if KNOWN_ROLE_REUSE_BINDING_FIELD in selected_voice_document or (
        KNOWN_ROLE_REUSE_BINDING_FIELD in voice_document
    ):
        raise KnownRoleReuseError(
            "Source manifest already contains a known-role reuse authority"
        )
    authority_predecessor = {
        key: value
        for key, value in voice_document.items()
        if key != MISSING_VOICE_REUSE_BINDING_FIELD
    }
    selected_predecessor = {
        key: value
        for key, value in selected_voice_document.items()
        if key != MISSING_VOICE_REUSE_BINDING_FIELD
    }
    selected_reuse_binding = selected_voice_document.get(
        MISSING_VOICE_REUSE_BINDING_FIELD
    )
    if authority_predecessor != selected_predecessor:
        raise KnownRoleReuseError(
            "Unresolved authority belongs to different selected voice controls"
        )
    if selected_reuse_binding is None:
        if binding.get("source_voice_manifest_sha256") != selected_voice_sha256:
            raise KnownRoleReuseError(
                "Unresolved authority belongs to different selected voice controls"
            )
        successor = copy.deepcopy(voice_document)
        successor_voice_manifest = authority_voice_manifest
        successor_voices = voices
    else:
        if (
            selected_reuse_binding.get("schema_version")
            != MISSING_VOICE_REUSE_APPROVED_BINDING_VERSION
            or selected_reuse_binding.get("mode") != "approved_cohort_reuse"
            or selected_reuse_binding.get("source_voice_manifest_sha256")
            != binding.get("source_voice_manifest_sha256")
        ):
            raise KnownRoleReuseError(
                "Selected voice controls are not an additive reviewed reuse overlay"
            )
        successor = copy.deepcopy(selected_voice_document)
        successor_voice_manifest = selected_voice_manifest
        successor_voices = selected_voices
    if voice_document.get(MISSING_VOICE_REUSE_BINDING_FIELD) != binding:
        raise KnownRoleReuseError(
            "Source manifest does not contain the exact unresolved authority"
        )
    reuse_voice = _resolve_exact_voice(successor_voices, reuse_voice_character)
    all_reference_records = _reference_records(
        successor_voice_manifest.parent,
        [reference for voice in successor_voices for reference in voice.references],
    )
    reuse_reference_records = _reference_records(
        successor_voice_manifest.parent, reuse_voice.references
    )

    unresolved_ids = {target["queue_id"] for target in unresolved_targets}
    role_items = [
        item
        for item in queue.items
        if is_spoken_queue_item(item)
        and normalize_character_name(
            synthesis_character_for_line(item.speaker, item.voice_character)
        )
        == normalize_character_name(source_character)
    ]
    rejected = {}
    approved = {}
    target_records = []
    for item in sorted(role_items, key=lambda value: value.queue_id):
        queue_id = item.queue_id
        result = state["items"].get(queue_id)
        source_state = None
        state_item_sha256 = None
        if queue_id in unresolved_ids:
            if result is not None:
                raise KnownRoleReuseError(
                    f"Unresolved known-role target is no longer absent: {queue_id!r}"
                )
            source_state = "absent"
        elif (
            isinstance(result, dict)
            and result.get("status") == "generated"
            and result.get("review_status") == "rejected"
        ):
            source_state = "rejected"
            state_item_sha256 = canonical_document_sha256(result)
            rejected[queue_id] = state_item_sha256
        elif (
            isinstance(result, dict)
            and result.get("status") == "approved"
            and result.get("review_status") == "approved"
        ):
            approved[queue_id] = canonical_document_sha256(result)
            continue
        else:
            raise KnownRoleReuseError(
                f"Known-role queue item has unsupported current state: {queue_id!r}"
            )
        target_records.append(
            {
                "queue_id": queue_id,
                "line_id": item.line_id,
                "text_sha256": item.text_sha256,
                "speaker": item.speaker,
                "declared_voice_character": synthesis_character_for_line(
                    item.speaker, item.voice_character
                ),
                "source_state": source_state,
                "source_state_item_sha256": state_item_sha256,
            }
        )
    if {
        record["queue_id"]
        for record in target_records
        if record["source_state"] == "absent"
    } != unresolved_ids:
        raise KnownRoleReuseError(
            "Known-role unresolved authority does not cover every absent role item"
        )
    retired_records = []
    for record in retired_source_reference_variants_from_manifest(voice_document):
        queue_ids = sorted(record["queue_ids"])
        if set(queue_ids).issubset(rejected):
            retired_records.append(
                {
                    "variant_id": record["variant_id"],
                    "record_sha256": canonical_document_sha256(record),
                    "queue_ids": queue_ids,
                }
            )
    retired_records.sort(key=lambda value: value["variant_id"])

    unresolved_queue_ids = sorted(unresolved_ids)
    cohort_ids = sorted(decision["cohort_id"] for decision in binding["decisions"])
    known_binding = {
        "schema": KNOWN_ROLE_REUSE_BINDING_SCHEMA,
        "schema_version": KNOWN_ROLE_REUSE_BINDING_VERSION,
        "mode": "explicit_role_reuse",
        "source_voice_manifest_sha256": selected_voice_sha256,
        "source_workspace_id": workspace_document["workspace_id"],
        "source_workspace_sha256": workspace_sha256,
        "source_state_sha256": state_sha256,
        "queue_sha256": queue_sha256,
        "source_character": source_character,
        "reuse_voice_character": reuse_voice.character,
        "reuse_reference_sha256s": sorted(
            record["sha256"] for record in reuse_reference_records
        ),
        "unresolved_authority": {
            "bundle_id": unresolved["bundle"]["bundle_id"],
            "bundle_sha256": unresolved["bundle_sha256"],
            "decision_id": unresolved["decision"]["decision_id"],
            "decision_sha256": unresolved["decision_sha256"],
            "plan_id": plan["plan_id"],
            "cohort_ids": cohort_ids,
            "queue_ids": unresolved_queue_ids,
        },
        "retired_variants": retired_records,
        "targets": target_records,
        "preserved_approved_queue_ids": sorted(approved),
        "source_rejected_state_item_sha256s": dict(sorted(rejected.items())),
        "queue_voice_overrides": {
            record["queue_id"]: reuse_voice.character for record in target_records
        },
        "queue_voice_overrides_sha256": queue_voice_overrides_sha256(
            {record["queue_id"]: reuse_voice.character for record in target_records}
        ),
        "authority": KNOWN_ROLE_REUSE_AUTHORITY,
    }
    successor[KNOWN_ROLE_REUSE_BINDING_FIELD] = known_binding
    decision_body = {
        "schema": KNOWN_ROLE_REUSE_DECISION_SCHEMA,
        "schema_version": KNOWN_ROLE_REUSE_DECISION_VERSION,
        "source_workspace": str(workspace),
        "unresolved_authority_directory": str(unresolved_authority_directory),
        "binding": known_binding,
    }
    decision_id = canonical_document_sha256(decision_body)
    result = KnownRoleReuseResult(
        output,
        source_character,
        reuse_voice.character,
        len(target_records),
        len(unresolved_ids),
        len(rejected),
        len(approved),
        len(retired_records),
        decision_id,
        bool(accept_known_role_reuse),
        False,
    )
    if not accept_known_role_reuse:
        return result
    if output.exists():
        _validate_bundle(output, known_binding, decision_body, queue_by_id)
        return result

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".known-role-reuse-", dir=output.parent)
    ).resolve()
    try:
        for reference in all_reference_records:
            source = reference["source"]
            target = staging / reference["relative"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if sha256_file(target) != reference["sha256"]:
                raise KnownRoleReuseError(
                    "Known-role voice reference changed while copied"
                )
        manifest_path = staging / "manifest.json"
        write_voice_manifest(manifest_path, successor)
        queue_voice_overrides_from_manifest(
            successor,
            queue_ids=queue_by_id,
            voices=load_voice_manifest(manifest_path, allow_legacy=False)[1],
        )
        decision = {**decision_body, "decision_id": decision_id}
        atomic_write_json(staging / "decision.json", decision, sort_keys=True)
        authority_target = staging / "authority" / "unresolved"
        _copy_tree(unresolved_authority_directory, authority_target)
        inventory = [
            {
                "path": path.relative_to(staging).as_posix(),
                "sha256": sha256_file(path),
            }
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
        _validate_bundle(staging, known_binding, decision_body, queue_by_id)
        rename_directory_no_replace(staging, output)
        staging = None
    except (
        AuthoringWorkbenchError,
        BulkGenerationError,
        SourceReferenceBindingError,
        VoiceManifestError,
    ) as error:
        raise KnownRoleReuseError(str(error)) from error
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return KnownRoleReuseResult(**{**asdict(result), "created": True})


def _validate_bundle(directory, expected_binding, decision_body, queue_by_id):
    directory = Path(directory).resolve()
    bundle = _read_json(directory / "bundle.json", "known-role bundle")
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
    decision = _read_json(directory / "decision.json", "known-role decision")
    if (
        decision
        != {**decision_body, "decision_id": canonical_document_sha256(decision_body)}
        or decision.get("binding") != expected_binding
    ):
        raise KnownRoleReuseError("Known-role decision changed")
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


def _resolve_exact_voice(voices, character):
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


def _reference_records(root, references):
    records = []
    seen = set()
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


def _copy_tree(source, destination):
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


def _decode_state(payload):
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnownRoleReuseError(str(error)) from error
    if not isinstance(value, dict):
        raise KnownRoleReuseError("Generation state must be a JSON object")
    return value


def _read_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnownRoleReuseError(f"Unable to read {label}: {error}") from error
    if not isinstance(value, dict):
        raise KnownRoleReuseError(f"{label.capitalize()} must be an object")
    return value


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise KnownRoleReuseError(f"{label} must be non-empty text")
    return value.strip()


__all__ = [
    "KNOWN_ROLE_REUSE_BUNDLE_SCHEMA",
    "KNOWN_ROLE_REUSE_BUNDLE_VERSION",
    "KNOWN_ROLE_REUSE_DECISION_SCHEMA",
    "KNOWN_ROLE_REUSE_DECISION_VERSION",
    "KnownRoleReuseError",
    "KnownRoleReuseResult",
    "publish_known_role_reuse_binding",
]
