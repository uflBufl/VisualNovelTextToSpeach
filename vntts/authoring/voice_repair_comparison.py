"""Checksum-bound plans for bounded voice-cohort repair comparisons."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import (
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
from vntts.authoring.game_pack import FinalGamePackError
from vntts.authoring.publication import rename_directory_no_replace
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    contained_workspace_path,
    create_resume_workspace,
    generation_command,
    generation_failure_category,
    list_review_items,
    load_workspace_authority,
    safe_workspace_relative_path,
)
from vntts.authoring.workspace_state import load_stable_workspace_generation_state
from vntts.document_identity import is_lowercase_sha256
from vntts.speech_backend import get_moss_tts_generation_profile

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
    document: dict

    def to_dict(self):
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

    def to_dict(self):
        return {
            "plan_id": self.plan_id,
            "candidate_id": self.candidate_id,
            "input_directory": str(self.input_directory),
            "workspace_directory": str(self.workspace_directory),
            "input_created": self.input_created,
            "workspace_created": self.workspace_created,
            "comparison_sample_queue_ids": list(self.comparison_sample_queue_ids),
        }


def build_voice_repair_comparison_plan(
    workspace_directory,
    character,
    *,
    generation_profiles=("stable", "natural"),
    token_level_duration_control=False,
):
    """Plan a bounded comparison without rendering or changing review state."""
    character = _required_text(character, "Comparison character")
    if token_level_duration_control is not False:
        raise VoiceRepairComparisonError(
            "Repair comparison requires token-level duration control to stay disabled"
        )
    try:
        directory, workspace, workspace_sha256 = load_workspace_authority(
            workspace_directory
        )
        queue, state, state_payload, state_sha256 = (
            load_stable_workspace_generation_state(
                directory,
                workspace,
                "voice repair comparison",
                error_type=AuthoringWorkbenchError,
            )
        )
    except AuthoringWorkbenchError as error:
        raise VoiceRepairComparisonError(str(error)) from error
    queue_path = directory / "queue.jsonl"
    queue_payload = _read(queue_path, "generation queue")
    queue_sha256 = hashlib.sha256(queue_payload).hexdigest()
    manifest_path = directory / "inputs/voice/manifest.json"
    manifest_payload = _read(manifest_path, "voice manifest")
    manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
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
    wanted = normalize_character_name(character)
    selected = [
        item
        for item in queue.items
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
    state_items = state["items"]
    review_by_id = {
        item.queue_id: item
        for item in list_review_items(
            directory,
            queue_ids=tuple(
                item.queue_id for item in selected if item.queue_id in state_items
            ),
        )
    }
    voice_by_name = {
        normalize_character_name(voice.character): voice for voice in voices
    }
    selected_variants = {
        item.queue_id: _selected_variant(
            item.queue_id,
            item.voice_character,
            state_items.get(item.queue_id),
            overrides,
        )
        for item in selected
    }
    selected_variants = {
        queue_id: (
            variant if normalize_character_name(variant) in voice_by_name else None
        )
        for queue_id, variant in selected_variants.items()
    }
    variant_names = sorted(
        {value for value in selected_variants.values() if value is not None},
        key=lambda value: normalize_character_name(value),
    )
    variants, reference_sources = _variant_controls(
        directory, manifest_path, voice_by_name, variant_names
    )
    approved = []
    targets = []
    audio_sources = []
    for item in selected:
        result = state_items.get(item.queue_id)
        variant = selected_variants[item.queue_id]
        record, audio_source = _item_record(
            directory, item, result, variant, review_by_id
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
    approved.sort(key=lambda value: value["queue_id"])
    targets.sort(key=lambda value: value["queue_id"])
    if not targets:
        raise VoiceRepairComparisonError(
            f"Comparison character has no unresolved items: {character!r}"
        )
    samples = _comparison_samples(targets)
    run_config = workspace.get("run_config")
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
    candidates = []
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
    source = {
        "workspace": str(directory),
        "workspace_id": workspace["workspace_id"],
        "workspace_sha256": workspace_sha256,
        "config_fingerprint": workspace.get("config_fingerprint"),
        "queue_sha256": queue_sha256,
        "state_sha256": state_sha256,
        "voice_manifest_sha256": manifest_sha256,
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
        "approved_count": len(approved),
        "target_count": len(targets),
        "comparison_ready_target_count": sum(
            value["voice_binding_status"] == "bound" for value in targets
        ),
        "unbound_target_count": sum(
            value["voice_binding_status"] == "exact_reference_variant_unbound"
            for value in targets
        ),
        "variant_count": len(variants),
        "candidate_count": len(candidates),
        "comparison_sample_count": len(samples),
        "approved": approved,
        "targets": targets,
        "variants": variants,
        "candidates": candidates,
        "comparison_sample_queue_ids": samples,
    }
    plan_id = canonical_document_sha256(body)
    plan = VoiceRepairComparisonPlan(plan_id, {**body, "plan_id": plan_id})
    _validate_plan(plan)
    _rehash_sources(
        directory,
        workspace_sha256,
        queue_sha256,
        state_sha256,
        manifest_sha256,
        (*reference_sources, *audio_sources),
        model_path if model_control["kind"] == "path" else None,
        model_control["sha256"],
    )
    return plan


def write_voice_repair_comparison_plan(plan, output_path):
    document = _validate_plan(plan)
    try:
        return _write_document_no_replace(
            output_path, document, "voice repair comparison plan"
        )
    except CohortReviewError as error:
        raise VoiceRepairComparisonError(str(error)) from error


def load_voice_repair_comparison_plan(path):
    try:
        document = _load_document(path, "voice repair comparison plan")
    except CohortReviewError as error:
        raise VoiceRepairComparisonError(str(error)) from error
    document = _validate_plan(document)
    return VoiceRepairComparisonPlan(document["plan_id"], document)


def prepare_voice_repair_candidate_workspace(
    plan,
    candidate_id,
    import_directory,
    input_root,
    workspaces_root,
):
    """Publish one exact candidate input and create its isolated workspace."""
    document = _validate_plan(plan)
    candidate = _candidate(document, candidate_id)
    _require_fresh_plan(document)
    source_directory = Path(document["source"]["workspace"]).resolve()
    input_directory, input_created = _publish_candidate_input(
        document, candidate, source_directory, input_root
    )
    _require_fresh_plan(document)
    try:
        source_directory, source_workspace, _workspace_sha256 = (
            load_workspace_authority(source_directory)
        )
        created = create_resume_workspace(
            import_directory,
            workspaces_root,
            story_index=source_directory / "inputs/story-index.jsonl",
            voice_manifest=input_directory / "manifest.json",
            narrator_character=source_workspace["narrator_character"],
            backend=candidate["provider"],
            model=candidate["model"],
            generation_profile=candidate["generation_profile"],
            missing_voice_policy=source_workspace["run_config"].get(
                "missing_voice_policy"
            ),
            failure_repair_policy=None,
        )
    except AuthoringWorkbenchError as error:
        raise VoiceRepairComparisonError(str(error)) from error
    return VoiceRepairCandidateWorkspace(
        document["plan_id"],
        candidate["candidate_id"],
        input_directory,
        created.directory,
        input_created,
        created.created,
        tuple(document["comparison_sample_queue_ids"]),
    )


def build_voice_repair_candidate_command(
    plan,
    candidate_id,
    workspace_directory,
    *,
    retries=0,
    seed=0,
):
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
        "backend": candidate["provider"],
        "model": candidate["model"],
        "generation_profile": candidate["generation_profile"],
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
    model_path = Path(candidate["model"]).expanduser()
    model_control = candidate["model_control"]
    observed_model_sha256 = (
        sha256_control_path(model_path)
        if model_control["kind"] == "path"
        else canonical_document_sha256({"model": candidate["model"]})
    )
    if observed_model_sha256 != model_control["sha256"]:
        raise VoiceRepairComparisonError("Candidate model changed after planning")
    try:
        command = generation_command(
            directory,
            queue_ids=tuple(document["comparison_sample_queue_ids"]),
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
    if observed_ids != tuple(document["comparison_sample_queue_ids"]):
        raise VoiceRepairComparisonError("Candidate child scope differs from the plan")
    return command


def _candidate(document, candidate_id):
    candidate_id = _required_sha256(candidate_id, "Comparison candidate ID")
    matches = [
        value
        for value in document["candidates"]
        if value["candidate_id"] == candidate_id
    ]
    if len(matches) != 1:
        raise VoiceRepairComparisonError("Comparison candidate is absent or ambiguous")
    return matches[0]


def _require_fresh_plan(document):
    profiles = tuple(value["generation_profile"] for value in document["candidates"])
    fresh = build_voice_repair_comparison_plan(
        document["source"]["workspace"],
        document["character"],
        generation_profiles=profiles,
        token_level_duration_control=False,
    )
    if fresh.plan_id != document["plan_id"]:
        raise VoiceRepairComparisonError(
            "Voice repair comparison source changed after planning"
        )


def _candidate_manifest_binding(document, candidate):
    return {
        "schema": "vntts.authoring-voice-repair-candidate",
        "schema_version": 1,
        "plan_id": document["plan_id"],
        "candidate_id": candidate["candidate_id"],
        "character": document["character"],
        "source_voice_manifest_sha256": document["source"]["voice_manifest_sha256"],
        "generation_profile": candidate["generation_profile"],
        "token_level_duration_control": False,
        "comparison_sample_queue_ids": list(document["comparison_sample_queue_ids"]),
    }


def _publish_candidate_input(document, candidate, source_directory, input_root):
    root = Path(input_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = f"voice-repair-{document['plan_id'][:24]}-{candidate['candidate_id'][:16]}"
    raw_destination = root / name
    if raw_destination.is_symlink():
        raise VoiceRepairComparisonError(
            "Voice repair candidate input is a symbolic link"
        )
    destination = contained_workspace_path(
        root, Path(name), "Voice repair candidate input"
    )
    if destination.exists():
        _validate_candidate_input(destination, document, candidate)
        return destination, False
    staging = Path(tempfile.mkdtemp(prefix=".voice-repair-staging-", dir=root))
    try:
        source_manifest = source_directory / "inputs/voice/manifest.json"
        source_payload = _read(source_manifest, "source voice manifest")
        if (
            hashlib.sha256(source_payload).hexdigest()
            != document["source"]["voice_manifest_sha256"]
        ):
            raise VoiceRepairComparisonError(
                "Source voice manifest changed after planning"
            )
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
        if VOICE_REPAIR_CANDIDATE_MANIFEST_FIELD in manifest:
            raise VoiceRepairComparisonError(
                "Source voice manifest already contains a repair candidate binding"
            )
        inventory = []
        seen = set()
        source_root = source_manifest.parent.resolve()
        for voice in voices:
            for value in voice.references:
                try:
                    relative = safe_workspace_relative_path(
                        value, "Candidate voice reference"
                    )
                    source = _regular_contained_file(
                        source_root, relative, "Candidate voice reference"
                    )
                except AuthoringWorkbenchError as error:
                    raise VoiceRepairComparisonError(str(error)) from error
                key = relative.as_posix()
                if key in seen:
                    continue
                seen.add(key)
                payload = source.read_bytes()
                digest = hashlib.sha256(payload).hexdigest()
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                inventory.append({"path": key, "sha256": digest})
        manifest[VOICE_REPAIR_CANDIDATE_MANIFEST_FIELD] = _candidate_manifest_binding(
            document, candidate
        )
        manifest_path = staging / "manifest.json"
        atomic_write_json(manifest_path, manifest, sort_keys=True)
        inventory = [
            {"path": "manifest.json", "sha256": sha256_file(manifest_path)},
            *sorted(inventory, key=lambda item: item["path"]),
        ]
        body = {
            "schema": VOICE_REPAIR_CANDIDATE_BUNDLE_SCHEMA,
            "schema_version": VOICE_REPAIR_CANDIDATE_BUNDLE_VERSION,
            "plan_id": document["plan_id"],
            "candidate_id": candidate["candidate_id"],
            "source_voice_manifest_sha256": document["source"]["voice_manifest_sha256"],
            "inventory": inventory,
        }
        bundle_id = canonical_document_sha256(body)
        atomic_write_json(
            staging / "bundle.json", {**body, "bundle_id": bundle_id}, sort_keys=True
        )
        _validate_candidate_input(staging, document, candidate)
        _require_fresh_plan(document)
        try:
            rename_directory_no_replace(staging, destination)
        except (FinalGamePackError, OSError) as error:
            if destination.exists():
                _validate_candidate_input(destination, document, candidate)
                return destination, False
            raise VoiceRepairComparisonError(
                f"Unable to publish voice repair candidate input: {error}"
            ) from error
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return destination, True


def _validate_candidate_input(directory, document, candidate):
    directory = Path(directory)
    if directory.is_symlink():
        raise VoiceRepairComparisonError(
            "Voice repair candidate input is a symbolic link"
        )
    directory = directory.resolve()
    bundle_path = directory / "bundle.json"
    if bundle_path.is_symlink():
        raise VoiceRepairComparisonError("Candidate bundle document is unsafe")
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VoiceRepairComparisonError(
            f"Unable to load voice repair candidate input: {error}"
        ) from error
    if (
        not isinstance(bundle, dict)
        or bundle.get("schema") != VOICE_REPAIR_CANDIDATE_BUNDLE_SCHEMA
        or bundle.get("schema_version") != VOICE_REPAIR_CANDIDATE_BUNDLE_VERSION
        or bundle.get("plan_id") != document["plan_id"]
        or bundle.get("candidate_id") != candidate["candidate_id"]
        or bundle.get("source_voice_manifest_sha256")
        != document["source"]["voice_manifest_sha256"]
    ):
        raise VoiceRepairComparisonError("Voice repair candidate input conflicts")
    claimed = _required_sha256(bundle.get("bundle_id"), "Candidate bundle ID")
    if claimed != canonical_document_sha256(
        {key: value for key, value in bundle.items() if key != "bundle_id"}
    ):
        raise VoiceRepairComparisonError("Candidate bundle identity is invalid")
    inventory = bundle.get("inventory")
    if not isinstance(inventory, list) or not inventory:
        raise VoiceRepairComparisonError("Candidate bundle inventory is empty")
    declared = set()
    ordered = []
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise VoiceRepairComparisonError("Candidate bundle inventory is malformed")
        try:
            relative = safe_workspace_relative_path(
                item.get("path"), "Candidate bundle artifact"
            )
            path = _regular_contained_file(
                directory, relative, "Candidate bundle artifact"
            )
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


def _regular_contained_file(root, relative, label):
    root = Path(root).resolve()
    current = root
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise VoiceRepairComparisonError(f"{label} contains a symbolic link")
    path = contained_workspace_path(root, Path(relative), label)
    if not path.is_file():
        raise VoiceRepairComparisonError(f"{label} is missing or is not a file")
    return path


def _selected_variant(queue_id, queue_voice_character, result, overrides):
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


def _variant_controls(directory, manifest_path, voice_by_name, variant_names):
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
    return variants, sources


def _item_record(directory, item, result, variant, review_by_id):
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


def _require_unresolved_result(queue_id, result):
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


def _comparison_samples(targets):
    selected = []
    for variant in sorted(
        {
            value["voice_character"]
            for value in targets
            if value["voice_binding_status"] == "bound"
        }
    ):
        for bucket in LENGTH_BUCKETS:
            candidates = [
                value
                for value in targets
                if value["voice_character"] == variant
                and value["length_bucket"] == bucket
            ]
            if not candidates:
                continue
            choice = min(
                candidates,
                key=lambda value: canonical_document_sha256(
                    {
                        "queue_id": value["queue_id"],
                        "text_sha256": value["text_sha256"],
                        "voice_character": variant,
                        "length_bucket": bucket,
                    }
                ),
            )
            selected.append(choice["queue_id"])
    return selected


def _validated_profiles(provider, generation_profiles):
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


def _validate_plan(plan):
    document = plan.document if isinstance(plan, VoiceRepairComparisonPlan) else plan
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
    approved = document.get("approved")
    targets = document.get("targets")
    variants = document.get("variants")
    candidates = document.get("candidates")
    samples = document.get("comparison_sample_queue_ids")
    for label, value in (
        ("approved", approved),
        ("targets", targets),
        ("variants", variants),
        ("candidates", candidates),
        ("samples", samples),
    ):
        if not isinstance(value, list):
            raise VoiceRepairComparisonError(f"Comparison {label} are malformed")
    if not targets or not variants or len(candidates) < 2 or not samples:
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
    if document.get("approved_count") != len(approved) or document.get(
        "target_count"
    ) != len(targets):
        raise VoiceRepairComparisonError("Comparison item counts are inconsistent")
    ready_count = sum(value.get("voice_binding_status") == "bound" for value in targets)
    unbound_count = sum(
        value.get("voice_binding_status") == "exact_reference_variant_unbound"
        for value in targets
    )
    if (
        ready_count + unbound_count != len(targets)
        or document.get("comparison_ready_target_count") != ready_count
        or document.get("unbound_target_count") != unbound_count
    ):
        raise VoiceRepairComparisonError("Comparison binding counts are inconsistent")
    if document.get("variant_count") != len(variants) or document.get(
        "candidate_count"
    ) != len(candidates):
        raise VoiceRepairComparisonError("Comparison control counts are inconsistent")
    if document.get("comparison_sample_count") != len(samples):
        raise VoiceRepairComparisonError("Comparison sample count is inconsistent")
    approved_ids = [value.get("queue_id") for value in approved]
    target_ids = [value.get("queue_id") for value in targets]
    if approved_ids != sorted(set(approved_ids)) or target_ids != sorted(
        set(target_ids)
    ):
        raise VoiceRepairComparisonError("Comparison item ledger is not canonical")
    if set(approved_ids) & set(target_ids) or not set(samples) <= set(target_ids):
        raise VoiceRepairComparisonError("Comparison sample authority is inconsistent")
    if any(
        (value.get("status"), value.get("review_status")) != ("approved", "approved")
        for value in approved
    ):
        raise VoiceRepairComparisonError("Comparison approval ledger is unsafe")
    for value in approved:
        _validate_item_record(value, approved=True)
    for value in targets:
        _validate_item_record(value, approved=False)
    _validate_variants(variants)
    variant_names = {value["voice_character"] for value in variants}
    if any(
        value["voice_binding_status"] == "bound"
        and value["voice_character"] not in variant_names
        for value in (*approved, *targets)
    ):
        raise VoiceRepairComparisonError("Comparison item uses an unknown variant")
    if samples != _comparison_samples(targets):
        raise VoiceRepairComparisonError("Comparison samples are not deterministic")
    seen_profiles = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {
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
    return copy.deepcopy(document)


def _validate_item_record(value, *, approved):
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
    for field in ("queue_id", "line_id", "text", "speaker"):
        _required_text(value.get(field), f"Comparison item {field}")
    text_hash = _required_sha256(value.get("text_sha256"), "Comparison text SHA-256")
    if hashlib.sha256(value["text"].encode("utf-8")).hexdigest() != text_hash:
        raise VoiceRepairComparisonError("Comparison item text identity is invalid")
    word_count = value.get("word_count")
    expected_words = len(re.findall(r"[\w’'-]+", value["text"], flags=re.UNICODE))
    if word_count != expected_words:
        raise VoiceRepairComparisonError("Comparison item word count is invalid")
    expected_bucket = (
        "short" if word_count <= 6 else "medium" if word_count <= 14 else "long"
    )
    if value.get("length_bucket") != expected_bucket:
        raise VoiceRepairComparisonError("Comparison item length bucket is invalid")
    flags = value.get("technical_flags")
    if not isinstance(flags, list) or any(
        not isinstance(flag, str) or not flag for flag in flags
    ):
        raise VoiceRepairComparisonError(
            "Comparison item technical flags are malformed"
        )
    binding = value.get("voice_binding_status")
    if binding == "bound":
        _required_text(value.get("voice_character"), "Comparison item voice")
    elif binding == "exact_reference_variant_unbound":
        if value.get("voice_character") is not None or value.get("status") != "absent":
            raise VoiceRepairComparisonError("Comparison unbound item is unsafe")
    else:
        raise VoiceRepairComparisonError("Comparison item voice binding is malformed")
    if approved:
        _required_sha256(value.get("state_item_sha256"), "Approved state item SHA-256")
        _required_sha256(value.get("audio_sha256"), "Approved WAV SHA-256")
        if binding != "bound" or value.get("failure_category") is not None:
            raise VoiceRepairComparisonError("Comparison approved item is unsafe")
        return
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


def _validate_variants(variants):
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
    directory,
    workspace_sha256,
    queue_sha256,
    state_sha256,
    manifest_sha256,
    reference_sources,
    model_path,
    model_sha256,
):
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


def _read(path, label):
    path = Path(path)
    if path.is_symlink():
        raise VoiceRepairComparisonError(f"Comparison {label} must not be a symlink")
    try:
        return path.read_bytes()
    except OSError as error:
        raise VoiceRepairComparisonError(
            f"Unable to read comparison {label}: {error}"
        ) from error


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise VoiceRepairComparisonError(f"{label} must be non-empty text")
    return value.strip()


def _required_sha256(value, label):
    if not is_lowercase_sha256(value):
        raise VoiceRepairComparisonError(f"{label} must be lowercase SHA-256")
    return value


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
