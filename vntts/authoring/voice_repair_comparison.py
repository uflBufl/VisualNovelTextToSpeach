"""Checksum-bound plans for bounded voice-cohort repair comparisons."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueueItem
from vntts_artifacts.voice_manifest import (
    VoiceManifestEntry,
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
)

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import sha256_control_path
from vntts.authoring.cohort_review import (
    CohortReviewError,
    _load_document,
    _write_document_no_replace,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    contained_workspace_path,
    create_resume_workspace,
    generation_command,
    list_review_items,
    load_workspace_authority,
    safe_workspace_relative_path,
)
from vntts.authoring.workbench_contracts import ReviewItem
from vntts.authoring.workspace_inspection import generation_failure_category
from vntts.authoring.workspace_state import load_stable_workspace_generation_state
from vntts.document_identity import is_lowercase_sha256
from vntts.speech_backend import get_moss_tts_generation_profile

JsonObject: TypeAlias = dict[str, object]
JsonList: TypeAlias = list[object]
PathDigest = tuple[Path, str]

VOICE_REPAIR_COMPARISON_SCHEMA = "vntts.authoring-voice-repair-comparison-plan"
VOICE_REPAIR_COMPARISON_VERSION = 1
LENGTH_BUCKETS = ("short", "medium", "long")
VOICE_REPAIR_CANDIDATE_BUNDLE_SCHEMA = "vntts.authoring-voice-repair-candidate-bundle"
VOICE_REPAIR_CANDIDATE_BUNDLE_VERSION = 1
VOICE_REPAIR_CANDIDATE_MANIFEST_FIELD = "vntts.authoring.voice_repair_comparison"


class VoiceRepairComparisonError(RuntimeError):
    """A repair comparison cannot be bound to exact immutable controls."""


@dataclass(frozen=True)
class VoiceRepairComparisonPlan:
    plan_id: str
    document: JsonObject

    def to_dict(self) -> JsonObject:
        return copy.deepcopy(self.document)


@dataclass(frozen=True)
class VoiceRepairCandidateWorkspace:
    plan_id: str
    candidate_id: str
    input_directory: Path
    workspace_directory: Path
    input_created: bool
    workspace_created: bool
    comparison_sample_queue_ids: tuple[str, ...]

    def to_dict(self) -> JsonObject:
        return {
            "plan_id": self.plan_id,
            "candidate_id": self.candidate_id,
            "input_directory": str(self.input_directory),
            "workspace_directory": str(self.workspace_directory),
            "input_created": self.input_created,
            "workspace_created": self.workspace_created,
            "comparison_sample_queue_ids": list(self.comparison_sample_queue_ids),
        }


@dataclass(frozen=True)
class _ComparisonPlanInputs:
    directory: Path
    workspace: JsonObject
    workspace_sha256: str
    queue_items: tuple[VoiceGenerationQueueItem, ...]
    queue_sha256: str
    state: JsonObject
    state_sha256: str
    manifest_path: Path
    manifest_sha256: str
    voices: tuple[VoiceManifestEntry, ...]
    overrides: dict[str, str]


@dataclass(frozen=True)
class _ComparisonRecords:
    approved: list[JsonObject]
    targets: list[JsonObject]
    audio_sources: tuple[PathDigest, ...]


@dataclass(frozen=True)
class _PlanSections:
    approved: list[JsonObject]
    targets: list[JsonObject]
    variants: list[JsonObject]
    candidates: list[JsonObject]
    samples: JsonList


def build_voice_repair_comparison_plan(
    workspace_directory: str | Path,
    character: object,
    *,
    generation_profiles: Iterable[object] = ("stable", "natural"),
    token_level_duration_control: bool = False,
) -> VoiceRepairComparisonPlan:
    """Plan a bounded comparison without rendering or changing review state."""
    character = _required_text(character, "Comparison character")
    if token_level_duration_control is not False:
        raise VoiceRepairComparisonError(
            "Repair comparison requires token-level duration control to stay disabled"
        )
    inputs = _comparison_plan_inputs(workspace_directory)
    wanted = normalize_character_name(character)
    selected = [
        item
        for item in inputs.queue_items
        if wanted
        in {
            normalize_character_name(item.speaker),
            normalize_character_name(item.voice_character),
        }
    ]
    if not selected:
        raise VoiceRepairComparisonError(
            f"Comparison character is absent from the queue: {character!r}"
        )
    state_items = _required_object(inputs.state.get("items"), "Generation state items")
    review_by_id = {
        item.queue_id: item
        for item in list_review_items(
            inputs.directory,
            queue_ids=tuple(
                item.queue_id for item in selected if item.queue_id in state_items
            ),
        )
    }
    voice_by_name = {
        normalize_character_name(voice.character): voice for voice in inputs.voices
    }
    selected_variants = _selected_variants(
        selected, state_items, inputs.overrides, voice_by_name
    )
    variant_names = sorted(
        {value for value in selected_variants.values() if value is not None},
        key=lambda value: normalize_character_name(value),
    )
    variants, reference_sources = _variant_controls(
        inputs.directory, inputs.manifest_path, voice_by_name, variant_names
    )
    records = _comparison_records(
        inputs.directory, selected, state_items, selected_variants, review_by_id
    )
    if not records.targets:
        raise VoiceRepairComparisonError(
            f"Comparison character has no unresolved items: {character!r}"
        )
    samples = _comparison_samples(records.targets)
    run_config = inputs.workspace.get("run_config")
    if not isinstance(run_config, dict):
        raise VoiceRepairComparisonError("Workspace run configuration is malformed")
    provider = _required_text(run_config.get("backend"), "Generation backend")
    model = _required_text(run_config.get("model"), "Generation model")
    profiles = _validated_profiles(provider, generation_profiles)
    model_path = Path(model).expanduser()
    model_control = {
        "kind": "path" if model_path.exists() else "identifier",
        "sha256": (
            sha256_control_path(model_path)
            if model_path.exists()
            else canonical_document_sha256({"model": model})
        ),
    }
    candidates = _comparison_candidates(
        provider, model, model_control, profiles, variants
    )
    source = {
        "workspace": str(inputs.directory),
        "workspace_id": inputs.workspace["workspace_id"],
        "workspace_sha256": inputs.workspace_sha256,
        "config_fingerprint": inputs.workspace.get("config_fingerprint"),
        "queue_sha256": inputs.queue_sha256,
        "state_sha256": inputs.state_sha256,
        "voice_manifest_sha256": inputs.manifest_sha256,
    }
    body = {
        "schema": VOICE_REPAIR_COMPARISON_SCHEMA,
        "schema_version": VOICE_REPAIR_COMPARISON_VERSION,
        "character": character,
        "source": source,
        "policy": {
            "authority": "plan_only_no_generation_or_review_mutation",
            "approved_items_are_immutable": True,
            "token_level_duration_control": False,
            "slow_pace_words_per_minute_below": 110,
            "internal_pause_seconds_at_least": 0.5,
            "sample_rule": "one deterministic unresolved item per available length bucket and exact voice variant",
        },
        "approved_count": len(records.approved),
        "target_count": len(records.targets),
        "comparison_ready_target_count": sum(
            value["voice_binding_status"] == "bound" for value in records.targets
        ),
        "unbound_target_count": sum(
            value["voice_binding_status"] == "exact_reference_variant_unbound"
            for value in records.targets
        ),
        "variant_count": len(variants),
        "candidate_count": len(candidates),
        "comparison_sample_count": len(samples),
        "approved": records.approved,
        "targets": records.targets,
        "variants": variants,
        "candidates": candidates,
        "comparison_sample_queue_ids": samples,
    }
    plan_id = canonical_document_sha256(body)
    plan = VoiceRepairComparisonPlan(plan_id, {**body, "plan_id": plan_id})
    _validate_plan(plan)
    _rehash_sources(
        inputs.directory,
        inputs.workspace_sha256,
        inputs.queue_sha256,
        inputs.state_sha256,
        inputs.manifest_sha256,
        (*reference_sources, *records.audio_sources),
        model_path if model_control["kind"] == "path" else None,
        model_control["sha256"],
    )
    return plan


def _comparison_plan_inputs(workspace_directory: str | Path) -> _ComparisonPlanInputs:
    try:
        directory, workspace, workspace_sha256 = load_workspace_authority(
            workspace_directory
        )
        queue, state, _state_payload, state_sha256 = (
            load_stable_workspace_generation_state(
                directory,
                workspace,
                "voice repair comparison",
                error_type=AuthoringWorkbenchError,
            )
        )
    except AuthoringWorkbenchError as error:
        raise VoiceRepairComparisonError(str(error)) from error
    queue_sha256 = hashlib.sha256(
        _read(directory / "queue.jsonl", "generation queue")
    ).hexdigest()
    manifest_path = directory / "inputs/voice/manifest.json"
    manifest_payload = _read(manifest_path, "voice manifest")
    try:
        manifest_document = json.loads(manifest_payload.decode("utf-8"))
        with tempfile.TemporaryDirectory(prefix="vntts-voice-repair-manifest-") as temp:
            snapshot = Path(temp) / "manifest.json"
            snapshot.write_bytes(manifest_payload)
            _metadata, voices = load_voice_manifest(snapshot, allow_legacy=False)
        overrides = queue_voice_overrides_from_manifest(
            manifest_document,
            queue_ids=(item.queue_id for item in queue.items),
            voices=voices,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        VoiceManifestError,
        SourceReferenceBindingError,
    ) as error:
        raise VoiceRepairComparisonError(str(error)) from error
    return _ComparisonPlanInputs(
        directory,
        workspace,
        workspace_sha256,
        tuple(queue.items),
        queue_sha256,
        state,
        state_sha256,
        manifest_path,
        hashlib.sha256(manifest_payload).hexdigest(),
        tuple(voices),
        overrides,
    )


def _selected_variants(
    selected: Sequence[VoiceGenerationQueueItem],
    state_items: JsonObject,
    overrides: dict[str, str],
    voice_by_name: dict[str, VoiceManifestEntry],
) -> dict[str, str | None]:
    variants = {
        item.queue_id: _selected_variant(
            item.queue_id,
            item.voice_character,
            _optional_object(state_items.get(item.queue_id), "Generation state item"),
            overrides,
        )
        for item in selected
    }
    return {
        queue_id: variant
        if normalize_character_name(variant) in voice_by_name
        else None
        for queue_id, variant in variants.items()
    }


def _comparison_records(
    directory: Path,
    selected: Sequence[VoiceGenerationQueueItem],
    state_items: JsonObject,
    selected_variants: dict[str, str | None],
    review_by_id: dict[str, ReviewItem],
) -> _ComparisonRecords:
    approved: list[JsonObject] = []
    targets: list[JsonObject] = []
    audio_sources: list[PathDigest] = []
    for item in selected:
        result = _optional_object(
            state_items.get(item.queue_id), "Generation state item"
        )
        record, audio_source = _item_record(
            directory, item, result, selected_variants[item.queue_id], review_by_id
        )
        if audio_source is not None:
            audio_sources.append(audio_source)
        if result is not None and (
            result.get("status"),
            result.get("review_status"),
        ) == ("approved", "approved"):
            approved.append(record)
        else:
            _require_unresolved_result(item.queue_id, result)
            targets.append(record)
    approved.sort(key=lambda value: _required_text(value.get("queue_id"), "Queue ID"))
    targets.sort(key=lambda value: _required_text(value.get("queue_id"), "Queue ID"))
    return _ComparisonRecords(approved, targets, tuple(audio_sources))


def _comparison_candidates(
    provider: str,
    model: str,
    model_control: JsonObject,
    profiles: Sequence[str],
    variants: JsonList,
) -> JsonList:
    candidates: JsonList = []
    for profile in profiles:
        body = {
            "provider": provider,
            "model": model,
            "model_control": model_control,
            "generation_profile": profile,
            "token_level_duration_control": False,
            "prompt_policy": "queue_annotations_unapplied",
            "variants": variants,
        }
        candidates.append({**body, "candidate_id": canonical_document_sha256(body)})
    return candidates


def write_voice_repair_comparison_plan(
    plan: VoiceRepairComparisonPlan, output_path: str | Path
) -> Path:
    document = _validate_plan(plan)
    try:
        return Path(
            _write_document_no_replace(
                output_path, document, "voice repair comparison plan"
            )
        )
    except CohortReviewError as error:
        raise VoiceRepairComparisonError(str(error)) from error


def load_voice_repair_comparison_plan(path: str | Path) -> VoiceRepairComparisonPlan:
    try:
        document = _load_document(path, "voice repair comparison plan")
    except CohortReviewError as error:
        raise VoiceRepairComparisonError(str(error)) from error
    document = _validate_plan(document)
    return VoiceRepairComparisonPlan(
        _required_text(document.get("plan_id"), "Comparison plan ID"), document
    )


def prepare_voice_repair_candidate_workspace(
    plan: VoiceRepairComparisonPlan,
    candidate_id: object,
    import_directory: str | Path,
    input_root: str | Path,
    workspaces_root: str | Path,
) -> VoiceRepairCandidateWorkspace:
    """Publish one exact candidate input and create its isolated workspace."""
    document = _validate_plan(plan)
    candidate = _candidate(document, candidate_id)
    _require_fresh_plan(document)
    source = _required_object(document.get("source"), "Comparison source")
    source_directory = Path(
        _required_text(source.get("workspace"), "Comparison source workspace")
    ).resolve()
    input_directory, input_created = _publish_candidate_input(
        document, candidate, source_directory, input_root
    )
    _require_fresh_plan(document)
    try:
        source_directory, source_workspace, _workspace_sha256 = (
            load_workspace_authority(source_directory)
        )
        run_config = _required_object(
            source_workspace.get("run_config"), "Source run configuration"
        )
        created = create_resume_workspace(
            import_directory,
            workspaces_root,
            story_index=source_directory / "inputs/story-index.jsonl",
            voice_manifest=input_directory / "manifest.json",
            narrator_character=_optional_text(
                source_workspace.get("narrator_character"), "Narrator character"
            ),
            backend=_optional_text(candidate.get("provider"), "Comparison provider"),
            model=_optional_text(candidate.get("model"), "Comparison model"),
            generation_profile=_optional_text(
                candidate.get("generation_profile"), "Generation profile"
            ),
            missing_voice_policy=_optional_object(
                run_config.get("missing_voice_policy"), "Missing-voice policy"
            ),
            failure_repair_policy=None,
        )
    except AuthoringWorkbenchError as error:
        raise VoiceRepairComparisonError(str(error)) from error
    return VoiceRepairCandidateWorkspace(
        _required_text(document.get("plan_id"), "Comparison plan ID"),
        _required_text(candidate.get("candidate_id"), "Comparison candidate ID"),
        input_directory,
        created.directory,
        input_created,
        created.created,
        tuple(
            _required_text(item, "Comparison sample ID")
            for item in _required_list(
                document.get("comparison_sample_queue_ids"), "Comparison samples"
            )
        ),
    )


def build_voice_repair_candidate_command(
    plan: VoiceRepairComparisonPlan,
    candidate_id: object,
    workspace_directory: str | Path,
    *,
    retries: int = 0,
    seed: int = 0,
) -> list[str]:
    """Return one exact-ID child command after independently rebinding controls."""
    document = _validate_plan(plan)
    candidate = _candidate(document, candidate_id)
    _require_fresh_plan(document)
    try:
        directory, workspace, _workspace_sha256 = load_workspace_authority(
            workspace_directory
        )
    except AuthoringWorkbenchError as error:
        raise VoiceRepairComparisonError(str(error)) from error
    run_config = workspace.get("run_config")
    expected_run = {
        "backend": _required_text(candidate.get("provider"), "Comparison provider"),
        "model": _required_text(candidate.get("model"), "Comparison model"),
        "generation_profile": _required_text(
            candidate.get("generation_profile"), "Generation profile"
        ),
    }
    if not isinstance(run_config, dict) or any(
        run_config.get(key) != value for key, value in expected_run.items()
    ):
        raise VoiceRepairComparisonError(
            "Candidate workspace run configuration differs from the plan"
        )
    manifest_path = directory / "inputs/voice/manifest.json"
    manifest_payload = _read(manifest_path, "candidate voice manifest")
    try:
        manifest = json.loads(manifest_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VoiceRepairComparisonError(str(error)) from error
    binding = manifest.get(VOICE_REPAIR_CANDIDATE_MANIFEST_FIELD)
    if binding != _candidate_manifest_binding(document, candidate):
        raise VoiceRepairComparisonError(
            "Candidate workspace manifest is not bound to the requested plan"
        )
    model = _required_text(candidate.get("model"), "Comparison model")
    model_path = Path(model).expanduser()
    model_control = _required_object(
        candidate.get("model_control"), "Comparison model control"
    )
    observed_model_sha256 = (
        sha256_control_path(model_path)
        if model_control["kind"] == "path"
        else canonical_document_sha256({"model": model})
    )
    if observed_model_sha256 != model_control["sha256"]:
        raise VoiceRepairComparisonError("Candidate model changed after planning")
    try:
        command = generation_command(
            directory,
            queue_ids=tuple(
                _required_text(item, "Comparison sample ID")
                for item in _required_list(
                    document.get("comparison_sample_queue_ids"), "Comparison samples"
                )
            ),
            retries=retries,
            seed=seed,
        )
    except AuthoringWorkbenchError as error:
        raise VoiceRepairComparisonError(str(error)) from error
    observed_ids = tuple(
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--queue-id"
    )
    if observed_ids != tuple(
        _required_text(item, "Comparison sample ID")
        for item in _required_list(
            document.get("comparison_sample_queue_ids"), "Comparison samples"
        )
    ):
        raise VoiceRepairComparisonError("Candidate child scope differs from the plan")
    return list(command)


def _candidate(document: JsonObject, candidate_id: object) -> JsonObject:
    candidate_id = _required_sha256(candidate_id, "Comparison candidate ID")
    matches = [
        value
        for value in _object_list(document.get("candidates"), "Comparison candidates")
        if value["candidate_id"] == candidate_id
    ]
    if len(matches) != 1:
        raise VoiceRepairComparisonError("Comparison candidate is absent or ambiguous")
    return matches[0]


def _require_fresh_plan(document: JsonObject) -> None:
    source = _required_object(document.get("source"), "Comparison source")
    profiles = tuple(
        _required_text(value.get("generation_profile"), "Generation profile")
        for value in _object_list(document.get("candidates"), "Comparison candidates")
    )
    fresh = build_voice_repair_comparison_plan(
        _required_text(source.get("workspace"), "Comparison source workspace"),
        _required_text(document.get("character"), "Comparison character"),
        generation_profiles=profiles,
        token_level_duration_control=False,
    )
    if fresh.plan_id != _required_text(document.get("plan_id"), "Comparison plan ID"):
        raise VoiceRepairComparisonError(
            "Voice repair comparison source changed after planning"
        )


def _candidate_manifest_binding(
    document: JsonObject, candidate: JsonObject
) -> JsonObject:
    source = _required_object(document.get("source"), "Comparison source")
    return {
        "schema": "vntts.authoring-voice-repair-candidate",
        "schema_version": 1,
        "plan_id": _required_text(document.get("plan_id"), "Comparison plan ID"),
        "candidate_id": _required_text(
            candidate.get("candidate_id"), "Comparison candidate ID"
        ),
        "character": _required_text(document.get("character"), "Comparison character"),
        "source_voice_manifest_sha256": _required_sha256(
            source.get("voice_manifest_sha256"),
            "Comparison source voice manifest SHA-256",
        ),
        "generation_profile": _required_text(
            candidate.get("generation_profile"), "Generation profile"
        ),
        "token_level_duration_control": False,
        "comparison_sample_queue_ids": list(
            _required_list(
                document.get("comparison_sample_queue_ids"), "Comparison samples"
            )
        ),
    }


def _publish_candidate_input(
    document: JsonObject,
    candidate: JsonObject,
    source_directory: Path,
    input_root: str | Path,
) -> tuple[Path, bool]:
    root, destination = _candidate_input_destination(document, candidate, input_root)
    if destination.exists():
        _validate_candidate_input(destination, document, candidate)
        return destination, False
    with staged_directory(root, prefix=".voice-repair-staging-") as staging:
        _write_candidate_input(staging, document, candidate, source_directory)
        _validate_candidate_input(staging, document, candidate)
        _require_fresh_plan(document)
        if _publish_staged_candidate(staging, destination, document, candidate):
            return destination, False
    return destination, True


def _candidate_input_destination(
    document: JsonObject, candidate: JsonObject, input_root: str | Path
) -> tuple[Path, Path]:
    root = Path(input_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    plan_id = _required_text(document.get("plan_id"), "Comparison plan ID")
    candidate_key = _required_text(
        candidate.get("candidate_id"), "Comparison candidate ID"
    )
    name = f"voice-repair-{plan_id[:24]}-{candidate_key[:16]}"
    if (root / name).is_symlink():
        raise VoiceRepairComparisonError(
            "Voice repair candidate input is a symbolic link"
        )
    return root, contained_workspace_path(
        root, Path(name), "Voice repair candidate input"
    )


def _write_candidate_input(
    staging: Path,
    document: JsonObject,
    candidate: JsonObject,
    source_directory: Path,
) -> None:
    source_manifest = source_directory / "inputs/voice/manifest.json"
    manifest, voices = _candidate_source_manifest(source_manifest, document)
    if VOICE_REPAIR_CANDIDATE_MANIFEST_FIELD in manifest:
        raise VoiceRepairComparisonError(
            "Source voice manifest already contains a repair candidate binding"
        )
    inventory = _copy_candidate_references(
        staging, source_manifest.parent.resolve(), voices
    )
    manifest[VOICE_REPAIR_CANDIDATE_MANIFEST_FIELD] = _candidate_manifest_binding(
        document, candidate
    )
    manifest_path = staging / "manifest.json"
    atomic_write_json(manifest_path, manifest, sort_keys=True)
    inventory = [
        {"path": "manifest.json", "sha256": sha256_file(manifest_path)},
        *sorted(
            inventory,
            key=lambda item: _required_text(
                item.get("path"), "Candidate artifact path"
            ),
        ),
    ]
    body = {
        "schema": VOICE_REPAIR_CANDIDATE_BUNDLE_SCHEMA,
        "schema_version": VOICE_REPAIR_CANDIDATE_BUNDLE_VERSION,
        "plan_id": _required_text(document.get("plan_id"), "Comparison plan ID"),
        "candidate_id": _required_text(
            candidate.get("candidate_id"), "Comparison candidate ID"
        ),
        "source_voice_manifest_sha256": _source_manifest_sha256(document),
        "inventory": inventory,
    }
    atomic_write_json(
        staging / "bundle.json",
        {**body, "bundle_id": canonical_document_sha256(body)},
        sort_keys=True,
    )


def _candidate_source_manifest(
    source_manifest: Path, document: JsonObject
) -> tuple[JsonObject, tuple[VoiceManifestEntry, ...]]:
    source_payload = _read(source_manifest, "source voice manifest")
    if hashlib.sha256(source_payload).hexdigest() != _source_manifest_sha256(document):
        raise VoiceRepairComparisonError("Source voice manifest changed after planning")
    try:
        manifest = json.loads(source_payload.decode("utf-8"))
        with tempfile.TemporaryDirectory(
            prefix="vntts-voice-repair-candidate-manifest-"
        ) as temp:
            snapshot = Path(temp) / "manifest.json"
            snapshot.write_bytes(source_payload)
            _metadata, voices = load_voice_manifest(snapshot, allow_legacy=False)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        VoiceManifestError,
    ) as error:
        raise VoiceRepairComparisonError(str(error)) from error
    if not isinstance(manifest, dict):
        raise VoiceRepairComparisonError("Source voice manifest is malformed")
    return manifest, tuple(voices)


def _source_manifest_sha256(document: JsonObject) -> str:
    return _required_sha256(
        _required_object(document.get("source"), "Comparison source").get(
            "voice_manifest_sha256"
        ),
        "Comparison source voice manifest SHA-256",
    )


def _copy_candidate_references(
    staging: Path, source_root: Path, voices: Sequence[VoiceManifestEntry]
) -> list[JsonObject]:
    inventory: list[JsonObject] = []
    seen: set[str] = set()
    for voice in voices:
        for value in voice.references:
            _copy_candidate_reference(staging, source_root, value, seen, inventory)
    return inventory


def _copy_candidate_reference(
    staging: Path,
    source_root: Path,
    value: object,
    seen: set[str],
    inventory: list[JsonObject],
) -> None:
    try:
        relative = safe_workspace_relative_path(value, "Candidate voice reference")
        source = _regular_contained_file(
            source_root, relative, "Candidate voice reference"
        )
    except AuthoringWorkbenchError as error:
        raise VoiceRepairComparisonError(str(error)) from error
    key = relative.as_posix()
    if key in seen:
        return
    seen.add(key)
    payload = source.read_bytes()
    target = staging / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    inventory.append({"path": key, "sha256": hashlib.sha256(payload).hexdigest()})


def _publish_staged_candidate(
    staging: Path, destination: Path, document: JsonObject, candidate: JsonObject
) -> bool:
    try:
        rename_directory_no_replace(staging, destination)
    except (AtomicPublicationError, OSError) as error:
        if destination.exists():
            _validate_candidate_input(destination, document, candidate)
            return True
        raise VoiceRepairComparisonError(
            f"Unable to publish voice repair candidate input: {error}"
        ) from error
    return False


def _validate_candidate_input(
    directory: str | Path, document: JsonObject, candidate: JsonObject
) -> None:
    directory = Path(directory)
    if directory.is_symlink():
        raise VoiceRepairComparisonError(
            "Voice repair candidate input is a symbolic link"
        )
    directory = directory.resolve()
    bundle = _candidate_bundle(directory)
    _validate_candidate_bundle_identity(bundle, document, candidate)
    _validate_candidate_bundle_inventory(directory, bundle)
    _validate_candidate_manifest(directory, document, candidate)


def _candidate_bundle(directory: Path) -> JsonObject:
    bundle_path = directory / "bundle.json"
    if bundle_path.is_symlink():
        raise VoiceRepairComparisonError("Candidate bundle document is unsafe")
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VoiceRepairComparisonError(
            f"Unable to load voice repair candidate input: {error}"
        ) from error
    if not isinstance(bundle, dict):
        raise VoiceRepairComparisonError("Voice repair candidate input conflicts")
    return bundle


def _validate_candidate_bundle_identity(
    bundle: JsonObject, document: JsonObject, candidate: JsonObject
) -> None:
    if bundle.get("schema") != VOICE_REPAIR_CANDIDATE_BUNDLE_SCHEMA:
        raise VoiceRepairComparisonError("Voice repair candidate input conflicts")
    if bundle.get("schema_version") != VOICE_REPAIR_CANDIDATE_BUNDLE_VERSION:
        raise VoiceRepairComparisonError("Voice repair candidate input conflicts")
    if bundle.get("plan_id") != _required_text(
        document.get("plan_id"), "Comparison plan ID"
    ):
        raise VoiceRepairComparisonError("Voice repair candidate input conflicts")
    if bundle.get("candidate_id") != _required_text(
        candidate.get("candidate_id"), "Comparison candidate ID"
    ):
        raise VoiceRepairComparisonError("Voice repair candidate input conflicts")
    if bundle.get("source_voice_manifest_sha256") != _source_manifest_sha256(document):
        raise VoiceRepairComparisonError("Voice repair candidate input conflicts")
    claimed = _required_sha256(bundle.get("bundle_id"), "Candidate bundle ID")
    if claimed != canonical_document_sha256(
        {key: value for key, value in bundle.items() if key != "bundle_id"}
    ):
        raise VoiceRepairComparisonError("Candidate bundle identity is invalid")


def _validate_candidate_bundle_inventory(directory: Path, bundle: JsonObject) -> None:
    inventory = bundle.get("inventory")
    if not isinstance(inventory, list) or not inventory:
        raise VoiceRepairComparisonError("Candidate bundle inventory is empty")
    declared: set[str] = set()
    ordered: list[str] = []
    for item in inventory:
        _validate_candidate_inventory_item(directory, item, declared, ordered)
    if ordered != ["manifest.json", *sorted(declared - {"manifest.json"})]:
        raise VoiceRepairComparisonError("Candidate bundle inventory is not canonical")
    paths = tuple(directory.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise VoiceRepairComparisonError("Candidate bundle contains a symbolic link")
    actual = {
        path.relative_to(directory).as_posix()
        for path in paths
        if path.is_file() and path.name != "bundle.json"
    }
    if declared != actual:
        raise VoiceRepairComparisonError("Candidate bundle inventory is incomplete")


def _validate_candidate_inventory_item(
    directory: Path, item: object, declared: set[str], ordered: list[str]
) -> None:
    if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
        raise VoiceRepairComparisonError("Candidate bundle inventory is malformed")
    try:
        relative = safe_workspace_relative_path(
            item.get("path"), "Candidate bundle artifact"
        )
        path = _regular_contained_file(directory, relative, "Candidate bundle artifact")
    except AuthoringWorkbenchError as error:
        raise VoiceRepairComparisonError(str(error)) from error
    digest = _required_sha256(item.get("sha256"), "Candidate artifact SHA-256")
    key = relative.as_posix()
    if key in declared:
        raise VoiceRepairComparisonError(
            "Candidate bundle inventory contains duplicate paths"
        )
    if sha256_file(path) != digest:
        raise VoiceRepairComparisonError("Candidate bundle artifact changed")
    declared.add(key)
    ordered.append(key)


def _validate_candidate_manifest(
    directory: Path, document: JsonObject, candidate: JsonObject
) -> None:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VoiceRepairComparisonError(
            f"Unable to load candidate voice manifest: {error}"
        ) from error
    if manifest.get(VOICE_REPAIR_CANDIDATE_MANIFEST_FIELD) != (
        _candidate_manifest_binding(document, candidate)
    ):
        raise VoiceRepairComparisonError("Candidate manifest binding changed")


def _regular_contained_file(root: str | Path, relative: str | Path, label: str) -> Path:
    root = Path(root).resolve()
    current = root
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise VoiceRepairComparisonError(f"{label} contains a symbolic link")
    path = contained_workspace_path(root, Path(relative), label)
    if not path.is_file():
        raise VoiceRepairComparisonError(f"{label} is missing or is not a file")
    return Path(path)


def _selected_variant(
    queue_id: str,
    queue_voice_character: str | None,
    result: JsonObject | None,
    overrides: dict[str, str],
) -> str:
    variant = overrides.get(queue_id, queue_voice_character)
    observed = result.get("voice_character") if isinstance(result, dict) else None
    if variant is None:
        raise VoiceRepairComparisonError(
            f"Comparison item lacks an exact selected reference variant: {queue_id}"
        )
    if observed is not None and observed != variant:
        raise VoiceRepairComparisonError(
            f"Comparison item voice differs from its exact manifest binding: {queue_id}"
        )
    return variant


def _variant_controls(
    directory: Path,
    manifest_path: Path,
    voice_by_name: dict[str, VoiceManifestEntry],
    variant_names: list[str],
) -> tuple[JsonList, list[PathDigest]]:
    variants = []
    sources = []
    root = manifest_path.parent.resolve()
    for name in variant_names:
        voice = voice_by_name.get(normalize_character_name(name))
        if voice is None or not voice.references:
            raise VoiceRepairComparisonError(
                f"Comparison voice is absent or has no references: {name!r}"
            )
        references = []
        for value in voice.references:
            try:
                relative = safe_workspace_relative_path(value, "Voice repair reference")
                path = contained_workspace_path(
                    root, relative, "Voice repair reference"
                )
            except AuthoringWorkbenchError as error:
                raise VoiceRepairComparisonError(str(error)) from error
            if path.is_symlink() or not path.is_file():
                raise VoiceRepairComparisonError(
                    f"Comparison reference is missing or unsafe: {value!r}"
                )
            digest = sha256_file(path)
            references.append({"path": value, "sha256": digest})
            sources.append((path, digest))
        variants.append(
            {
                "voice_character": name,
                "voice_speaker": _required_text(voice.speaker, "Voice speaker"),
                "ordered_references": references,
            }
        )
    return list(variants), list(sources)


def _item_record(
    directory: Path,
    item: VoiceGenerationQueueItem,
    result: JsonObject | None,
    variant: str | None,
    review_by_id: dict[str, ReviewItem],
) -> tuple[JsonObject, PathDigest | None]:
    review = review_by_id.get(item.queue_id)
    word_count = len(re.findall(r"[\w’'-]+", item.text, flags=re.UNICODE))
    bucket = "short" if word_count <= 6 else "medium" if word_count <= 14 else "long"
    status = "absent" if result is None else str(result.get("status") or "unknown")
    review_status = None if result is None else result.get("review_status")
    audio_sha256 = None
    audio_source = None
    if isinstance(result, dict) and result.get("path") is not None:
        try:
            path = contained_workspace_path(
                directory / "generated-audio",
                safe_workspace_relative_path(result.get("path"), "Comparison WAV"),
                "Comparison WAV",
            )
        except AuthoringWorkbenchError as error:
            raise VoiceRepairComparisonError(str(error)) from error
        expected = _required_sha256(result.get("file_sha256"), "Comparison WAV hash")
        if path.is_symlink() or not path.is_file() or sha256_file(path) != expected:
            raise VoiceRepairComparisonError(
                f"Comparison WAV is missing or changed: {item.queue_id}"
            )
        audio_sha256 = expected
        audio_source = (path, expected)
    failure_category = None
    if isinstance(result, dict) and result.get("status") == "failed":
        failure_category = generation_failure_category(result)
    return {
        "queue_id": item.queue_id,
        "line_id": item.line_id,
        "text": item.text,
        "text_sha256": item.text_sha256,
        "speaker": item.speaker,
        "voice_character": variant,
        "voice_binding_status": (
            "bound" if variant is not None else "exact_reference_variant_unbound"
        ),
        "status": status,
        "review_status": review_status,
        "state_item_sha256": (
            None if result is None else canonical_document_sha256(result)
        ),
        "audio_sha256": audio_sha256,
        "failure_category": failure_category,
        "word_count": word_count,
        "length_bucket": bucket,
        "technical_flags": [] if review is None else list(review.technical_flags),
    }, audio_source


def _require_unresolved_result(queue_id: str, result: JsonObject | None) -> None:
    if result is None:
        return
    combination = (result.get("status"), result.get("review_status"))
    if combination not in {
        ("generated", "pending_review"),
        ("generated", "rejected"),
        ("failed", None),
    }:
        raise VoiceRepairComparisonError(
            f"Comparison item has an unsupported authority state: {queue_id} {combination}"
        )


def _comparison_samples(targets: Sequence[JsonObject]) -> list[str]:
    selected = []
    for variant in sorted(
        {
            _required_text(value.get("voice_character"), "Comparison item voice")
            for value in targets
            if value["voice_binding_status"] == "bound"
        }
    ):
        for bucket in LENGTH_BUCKETS:
            candidates = [
                value
                for value in targets
                if value.get("voice_character") == variant
                and value.get("length_bucket") == bucket
            ]
            if not candidates:
                continue
            choice = min(
                candidates,
                key=lambda value: canonical_document_sha256(
                    {
                        "queue_id": value.get("queue_id"),
                        "text_sha256": value.get("text_sha256"),
                        "voice_character": variant,
                        "length_bucket": bucket,
                    }
                ),
            )
            selected.append(
                _required_text(choice.get("queue_id"), "Comparison queue ID")
            )
    return selected


def _validated_profiles(
    provider: str, generation_profiles: Iterable[object]
) -> list[str]:
    if not isinstance(generation_profiles, (list, tuple)) or not generation_profiles:
        raise VoiceRepairComparisonError("Comparison profiles must be non-empty")
    profiles = []
    for value in generation_profiles:
        profile = _required_text(value, "Generation profile").casefold()
        if profile in profiles:
            raise VoiceRepairComparisonError(
                f"Comparison generation profile is duplicated: {profile}"
            )
        if provider == "moss-tts":
            try:
                profile, _options = get_moss_tts_generation_profile(profile)
            except ValueError as error:
                raise VoiceRepairComparisonError(str(error)) from error
        profiles.append(profile)
    return profiles


def _validate_plan(plan: VoiceRepairComparisonPlan | JsonObject) -> JsonObject:
    document = plan.document if isinstance(plan, VoiceRepairComparisonPlan) else plan
    _validate_plan_header(document)
    sections = _plan_sections(document)
    _validate_plan_structure(document, sections)
    _validate_plan_counts(document, sections)
    _validate_plan_items(sections)
    _validate_variants(sections.variants)
    _validate_plan_variant_authority(sections)
    if sections.samples != _comparison_samples(sections.targets):
        raise VoiceRepairComparisonError("Comparison samples are not deterministic")
    _validate_candidates(sections.candidates, sections.variants)
    return copy.deepcopy(document)


def _validate_plan_header(document: object) -> None:
    if (
        not isinstance(document, dict)
        or document.get("schema") != VOICE_REPAIR_COMPARISON_SCHEMA
        or document.get("schema_version") != VOICE_REPAIR_COMPARISON_VERSION
    ):
        raise VoiceRepairComparisonError(
            "Voice repair comparison schema is unsupported"
        )
    claimed = _required_sha256(document.get("plan_id"), "Comparison plan ID")
    actual = canonical_document_sha256(
        {key: value for key, value in document.items() if key != "plan_id"}
    )
    if claimed != actual:
        raise VoiceRepairComparisonError("Voice repair comparison identity is invalid")
    _required_text(document.get("character"), "Comparison character")
    source = document.get("source")
    if not isinstance(source, dict) or set(source) != {
        "workspace",
        "workspace_id",
        "workspace_sha256",
        "config_fingerprint",
        "queue_sha256",
        "state_sha256",
        "voice_manifest_sha256",
    }:
        raise VoiceRepairComparisonError("Comparison source is malformed")
    _required_text(source.get("workspace"), "Comparison source workspace")
    _required_text(source.get("workspace_id"), "Comparison source workspace ID")
    for field in (
        "workspace_sha256",
        "config_fingerprint",
        "queue_sha256",
        "state_sha256",
        "voice_manifest_sha256",
    ):
        _required_sha256(source.get(field), f"Comparison source {field}")


def _plan_sections(document: JsonObject) -> _PlanSections:
    approved = _object_list(document.get("approved"), "Comparison approved")
    targets = _object_list(document.get("targets"), "Comparison targets")
    variants = _object_list(document.get("variants"), "Comparison variants")
    candidates = _object_list(document.get("candidates"), "Comparison candidates")
    samples = _required_list(
        document.get("comparison_sample_queue_ids"), "Comparison samples"
    )
    return _PlanSections(approved, targets, variants, candidates, samples)


def _validate_plan_structure(document: JsonObject, sections: _PlanSections) -> None:
    if (
        not sections.targets
        or not sections.variants
        or len(sections.candidates) < 2
        or not sections.samples
    ):
        raise VoiceRepairComparisonError("Comparison plan is incomplete")
    expected_policy = {
        "authority": "plan_only_no_generation_or_review_mutation",
        "approved_items_are_immutable": True,
        "token_level_duration_control": False,
        "slow_pace_words_per_minute_below": 110,
        "internal_pause_seconds_at_least": 0.5,
        "sample_rule": "one deterministic unresolved item per available length bucket and exact voice variant",
    }
    if document.get("policy") != expected_policy:
        raise VoiceRepairComparisonError("Comparison policy is unsafe")


def _validate_plan_counts(document: JsonObject, sections: _PlanSections) -> None:
    if document.get("approved_count") != len(sections.approved) or document.get(
        "target_count"
    ) != len(sections.targets):
        raise VoiceRepairComparisonError("Comparison item counts are inconsistent")
    ready_count = sum(
        value.get("voice_binding_status") == "bound" for value in sections.targets
    )
    unbound_count = sum(
        value.get("voice_binding_status") == "exact_reference_variant_unbound"
        for value in sections.targets
    )
    if (
        ready_count + unbound_count != len(sections.targets)
        or document.get("comparison_ready_target_count") != ready_count
        or document.get("unbound_target_count") != unbound_count
    ):
        raise VoiceRepairComparisonError("Comparison binding counts are inconsistent")
    if document.get("variant_count") != len(sections.variants) or document.get(
        "candidate_count"
    ) != len(sections.candidates):
        raise VoiceRepairComparisonError("Comparison control counts are inconsistent")
    if document.get("comparison_sample_count") != len(sections.samples):
        raise VoiceRepairComparisonError("Comparison sample count is inconsistent")


def _validate_plan_items(sections: _PlanSections) -> None:
    approved_ids = [
        _required_text(value.get("queue_id"), "Comparison queue ID")
        for value in sections.approved
    ]
    target_ids = [
        _required_text(value.get("queue_id"), "Comparison queue ID")
        for value in sections.targets
    ]
    if approved_ids != sorted(set(approved_ids)) or target_ids != sorted(
        set(target_ids)
    ):
        raise VoiceRepairComparisonError("Comparison item ledger is not canonical")
    if set(approved_ids) & set(target_ids) or not set(sections.samples) <= set(
        target_ids
    ):
        raise VoiceRepairComparisonError("Comparison sample authority is inconsistent")
    if any(
        (value.get("status"), value.get("review_status")) != ("approved", "approved")
        for value in sections.approved
    ):
        raise VoiceRepairComparisonError("Comparison approval ledger is unsafe")
    for item_record in sections.approved:
        _validate_item_record(item_record, approved=True)
    for item_record in sections.targets:
        _validate_item_record(item_record, approved=False)


def _validate_plan_variant_authority(sections: _PlanSections) -> None:
    variant_names = {
        _required_text(value.get("voice_character"), "Comparison variant character")
        for value in sections.variants
    }
    if any(
        value["voice_binding_status"] == "bound"
        and value["voice_character"] not in variant_names
        for value in (*sections.approved, *sections.targets)
    ):
        raise VoiceRepairComparisonError("Comparison item uses an unknown variant")


def _validate_candidates(
    candidates: Sequence[JsonObject], variants: Sequence[JsonObject]
) -> None:
    seen_profiles: set[str] = set()
    for candidate in candidates:
        if set(candidate) != {
            "candidate_id",
            "provider",
            "model",
            "model_control",
            "generation_profile",
            "token_level_duration_control",
            "prompt_policy",
            "variants",
        }:
            raise VoiceRepairComparisonError("Comparison candidate is malformed")
        candidate_id = _required_sha256(
            candidate.get("candidate_id"), "Comparison candidate ID"
        )
        _required_text(candidate.get("provider"), "Comparison provider")
        _required_text(candidate.get("model"), "Comparison model")
        profile = _required_text(
            candidate.get("generation_profile"), "Comparison generation profile"
        )
        if profile in seen_profiles:
            raise VoiceRepairComparisonError(
                "Comparison candidate profile is duplicated"
            )
        seen_profiles.add(profile)
        model_control = candidate.get("model_control")
        if (
            not isinstance(model_control, dict)
            or set(model_control) != {"kind", "sha256"}
            or model_control.get("kind") not in {"path", "identifier"}
        ):
            raise VoiceRepairComparisonError("Comparison model control is malformed")
        _required_sha256(
            model_control.get("sha256"), "Comparison model control SHA-256"
        )
        if candidate.get("token_level_duration_control") is not False:
            raise VoiceRepairComparisonError(
                "Comparison candidate enabled token-level duration control"
            )
        if candidate.get("variants") != variants:
            raise VoiceRepairComparisonError("Comparison candidate variants differ")
        if candidate.get("prompt_policy") != "queue_annotations_unapplied":
            raise VoiceRepairComparisonError("Comparison prompt policy is unsafe")
        if candidate_id != canonical_document_sha256(
            {key: value for key, value in candidate.items() if key != "candidate_id"}
        ):
            raise VoiceRepairComparisonError("Comparison candidate identity is invalid")


def _validate_item_record(value: object, *, approved: bool) -> None:
    item = _item_record_shape(value)
    _validate_item_identity(item)
    _validate_item_technical_flags(item)
    binding = _validate_item_binding(item)
    if approved:
        _validate_approved_item(item, binding)
        return
    _validate_target_item(item)


def _item_record_shape(value: object) -> JsonObject:
    fields = {
        "queue_id",
        "line_id",
        "text",
        "text_sha256",
        "speaker",
        "voice_character",
        "voice_binding_status",
        "status",
        "review_status",
        "state_item_sha256",
        "audio_sha256",
        "failure_category",
        "word_count",
        "length_bucket",
        "technical_flags",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise VoiceRepairComparisonError("Comparison item record is malformed")
    return value


def _validate_item_identity(value: JsonObject) -> None:
    for field in ("queue_id", "line_id", "text", "speaker"):
        _required_text(value.get(field), f"Comparison item {field}")
    text = value.get("text")
    if not isinstance(text, str):
        raise VoiceRepairComparisonError("Comparison item text must be non-empty text")
    text_hash = _required_sha256(value.get("text_sha256"), "Comparison text SHA-256")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_hash:
        raise VoiceRepairComparisonError("Comparison item text identity is invalid")
    word_count = value.get("word_count")
    expected_words = len(re.findall(r"[\w’'-]+", text, flags=re.UNICODE))
    if word_count != expected_words:
        raise VoiceRepairComparisonError("Comparison item word count is invalid")
    expected_bucket = (
        "short" if word_count <= 6 else "medium" if word_count <= 14 else "long"
    )
    if value.get("length_bucket") != expected_bucket:
        raise VoiceRepairComparisonError("Comparison item length bucket is invalid")


def _validate_item_technical_flags(value: JsonObject) -> None:
    flags = value.get("technical_flags")
    if not isinstance(flags, list) or any(
        not isinstance(flag, str) or not flag for flag in flags
    ):
        raise VoiceRepairComparisonError(
            "Comparison item technical flags are malformed"
        )


def _validate_item_binding(value: JsonObject) -> str:
    binding = value.get("voice_binding_status")
    if binding == "bound":
        _required_text(value.get("voice_character"), "Comparison item voice")
    elif binding == "exact_reference_variant_unbound":
        if value.get("voice_character") is not None or value.get("status") != "absent":
            raise VoiceRepairComparisonError("Comparison unbound item is unsafe")
    else:
        raise VoiceRepairComparisonError("Comparison item voice binding is malformed")
    return binding


def _validate_approved_item(value: JsonObject, binding: str) -> None:
    _required_sha256(value.get("state_item_sha256"), "Approved state item SHA-256")
    _required_sha256(value.get("audio_sha256"), "Approved WAV SHA-256")
    if binding != "bound" or value.get("failure_category") is not None:
        raise VoiceRepairComparisonError("Comparison approved item is unsafe")


def _validate_target_item(value: JsonObject) -> None:
    combination = (value.get("status"), value.get("review_status"))
    if combination == ("absent", None):
        if any(
            value.get(field) is not None
            for field in ("state_item_sha256", "audio_sha256", "failure_category")
        ):
            raise VoiceRepairComparisonError("Comparison absent item is malformed")
    elif combination == ("failed", None):
        _required_sha256(value.get("state_item_sha256"), "Failed state item SHA-256")
        _required_text(value.get("failure_category"), "Failure category")
        if value.get("audio_sha256") is not None:
            raise VoiceRepairComparisonError("Comparison failed item has a WAV")
    elif combination in {
        ("generated", "pending_review"),
        ("generated", "rejected"),
    }:
        _required_sha256(value.get("state_item_sha256"), "Generated state item SHA-256")
        _required_sha256(value.get("audio_sha256"), "Generated WAV SHA-256")
        if value.get("failure_category") is not None:
            raise VoiceRepairComparisonError("Comparison generated item has a failure")
    else:
        raise VoiceRepairComparisonError("Comparison unresolved item state is unsafe")


def _validate_variants(variants: Sequence[JsonObject]) -> None:
    seen = set()
    for variant in variants:
        if not isinstance(variant, dict) or set(variant) != {
            "voice_character",
            "voice_speaker",
            "ordered_references",
        }:
            raise VoiceRepairComparisonError("Comparison variant is malformed")
        character = _required_text(
            variant.get("voice_character"), "Comparison variant character"
        )
        _required_text(variant.get("voice_speaker"), "Comparison variant speaker")
        normalized = normalize_character_name(character)
        if normalized in seen:
            raise VoiceRepairComparisonError("Comparison variant is duplicated")
        seen.add(normalized)
        references = variant.get("ordered_references")
        if not isinstance(references, list) or not references:
            raise VoiceRepairComparisonError(
                "Comparison variant references are missing"
            )
        for reference in references:
            if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
                raise VoiceRepairComparisonError("Comparison reference is malformed")
            value = _required_text(reference.get("path"), "Comparison reference path")
            if (
                "\\" in value
                or Path(value).is_absolute()
                or any(part in {"", ".", ".."} for part in value.split("/"))
            ):
                raise VoiceRepairComparisonError("Comparison reference path is unsafe")
            _required_sha256(reference.get("sha256"), "Comparison reference SHA-256")


def _rehash_sources(
    directory: Path,
    workspace_sha256: str,
    queue_sha256: str,
    state_sha256: str,
    manifest_sha256: str,
    reference_sources: tuple[PathDigest, ...],
    model_path: Path | None,
    model_sha256: str,
) -> None:
    paths = (
        (directory / "workspace.json", workspace_sha256, "workspace"),
        (directory / "queue.jsonl", queue_sha256, "queue"),
        (
            directory / "generated-audio/generation-state.json",
            state_sha256,
            "state",
        ),
        (directory / "inputs/voice/manifest.json", manifest_sha256, "manifest"),
        *((path, digest, "reference") for path, digest in reference_sources),
    )
    for path, expected, label in paths:
        if sha256_file(path) != expected:
            raise VoiceRepairComparisonError(
                f"Voice repair comparison {label} changed during planning"
            )
    if model_path is not None and sha256_control_path(model_path) != model_sha256:
        raise VoiceRepairComparisonError(
            "Voice repair comparison model changed during planning"
        )


def _read(path: str | Path, label: str) -> bytes:
    path = Path(path)
    if path.is_symlink():
        raise VoiceRepairComparisonError(f"Comparison {label} must not be a symlink")
    try:
        return path.read_bytes()
    except OSError as error:
        raise VoiceRepairComparisonError(
            f"Unable to read comparison {label}: {error}"
        ) from error


def _required_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise VoiceRepairComparisonError(f"{label} is malformed")
    return value


def _optional_object(value: object, label: str) -> JsonObject | None:
    return None if value is None else _required_object(value, label)


def _required_list(value: object, label: str) -> JsonList:
    if not isinstance(value, list):
        raise VoiceRepairComparisonError(f"{label} is malformed")
    return value


def _object_list(value: object, label: str) -> list[JsonObject]:
    values = _required_list(value, label)
    if any(not isinstance(item, dict) for item in values):
        raise VoiceRepairComparisonError(f"{label} is malformed")
    return [item for item in values if isinstance(item, dict)]


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VoiceRepairComparisonError(f"{label} must be non-empty text")
    return value.strip()


def _required_sha256(value: object, label: str) -> str:
    if not is_lowercase_sha256(value):
        raise VoiceRepairComparisonError(f"{label} must be lowercase SHA-256")
    if not isinstance(value, str):
        raise VoiceRepairComparisonError(f"{label} must be lowercase SHA-256")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _required_text(value, label)


__all__ = [
    "VOICE_REPAIR_COMPARISON_SCHEMA",
    "VOICE_REPAIR_COMPARISON_VERSION",
    "VoiceRepairComparisonError",
    "VoiceRepairComparisonPlan",
    "VoiceRepairCandidateWorkspace",
    "build_voice_repair_candidate_command",
    "build_voice_repair_comparison_plan",
    "load_voice_repair_comparison_plan",
    "prepare_voice_repair_candidate_workspace",
    "write_voice_repair_comparison_plan",
]
