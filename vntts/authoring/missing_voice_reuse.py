"""Immutable plans for bounded reuse of existing voices on unbound story lines."""

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
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
    write_voice_manifest,
)

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import is_spoken_queue_item
from vntts.authoring.cohort_review import (
    CohortReviewError,
    _load_document,
    _write_document_no_replace,
)
from vntts.authoring.config_rebase import rebase_workspace_config
from vntts.authoring.failed_control_carry import (
    FailedControlCarryError,
    carry_failed_controls,
)
from vntts.authoring.failure_repair import (
    INLINE_PAUSE_MARKER,
    FailureRepairPolicy,
    inline_sentence_pause_prompt,
)
from vntts.authoring.publication import rename_directory_no_replace
from vntts.authoring.source_reference_bindings import (
    MISSING_VOICE_REUSE_APPROVED_BINDING_VERSION,
    MISSING_VOICE_REUSE_BINDING_FIELD,
    MISSING_VOICE_REUSE_BINDING_SCHEMA,
    MISSING_VOICE_REUSE_BINDING_VERSION,
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
    retired_source_reference_variants_from_manifest,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    _load_workspace_snapshot,
    _stable_workspace_state,
    contained_workspace_path,
    create_resume_workspace,
    generation_command,
    generation_failure_category,
    safe_workspace_relative_path,
)

MISSING_VOICE_REUSE_PLAN_SCHEMA = "vntts.authoring-missing-voice-reuse-plan"
MISSING_VOICE_REUSE_PLAN_VERSION = 1
MISSING_VOICE_REUSE_CANDIDATE_BUNDLE_SCHEMA = (
    "vntts.authoring-missing-voice-reuse-candidate-bundle"
)
MISSING_VOICE_REUSE_CANDIDATE_BUNDLE_VERSION = 1
LENGTH_BUCKETS = ("short", "medium", "long")


class MissingVoiceReuseError(RuntimeError):
    """A missing-voice reuse plan is unsafe or no longer authoritative."""


@dataclass(frozen=True)
class MissingVoiceReusePlan:
    plan_id: str
    document: dict

    def to_dict(self):
        return copy.deepcopy(self.document)


@dataclass(frozen=True)
class MissingVoiceReuseCandidateWorkspace:
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


def build_missing_voice_reuse_plan(
    workspace_directory,
    character,
    *,
    cohorts,
    candidate_voice_characters,
    failed_queue_ids=None,
    inline_pause_ms=None,
):
    """Plan a small representative comparison without binding or rendering lines.

    ``failed_queue_ids`` switches the plan from missing-voice discovery to an
    exact exhausted-failure hypothesis.  The failed source item remains the
    immutable control; candidate workspaces render only fresh comparison
    samples and cannot consume or rewrite its attempt ledger.
    """
    character = _text(character, "Missing-voice character")
    cohort_rules = _cohort_rules(cohorts)
    target_mode = "failed" if failed_queue_ids is not None else "missing"
    if inline_pause_ms is not None and target_mode != "failed":
        raise MissingVoiceReuseError(
            "Inline-pause hypotheses require exact failed-control mode"
        )
    requested_failed_ids = _failed_queue_ids(failed_queue_ids)
    requested_candidates = _candidate_names(
        candidate_voice_characters, minimum=1 if target_mode == "failed" else 2
    )
    try:
        directory, workspace, workspace_sha256 = _load_workspace_snapshot(
            workspace_directory, "missing-voice reuse plan"
        )
        queue, state, _state_payload, state_sha256 = _stable_workspace_state(
            directory, workspace, "missing-voice reuse plan"
        )
    except AuthoringWorkbenchError as error:
        raise MissingVoiceReuseError(str(error)) from error

    queue_path = directory / "queue.jsonl"
    manifest_path = directory / "inputs/voice/manifest.json"
    story_path = directory / "inputs/story-index.jsonl"
    queue_sha256 = sha256_file(queue_path)
    manifest_sha256 = sha256_file(manifest_path)
    story_sha256 = sha256_file(story_path)
    try:
        manifest_document = json.loads(manifest_path.read_text(encoding="utf-8"))
        _metadata, voices = load_voice_manifest(manifest_path, allow_legacy=False)
        overrides = queue_voice_overrides_from_manifest(
            manifest_document,
            queue_ids=(item.queue_id for item in queue.items),
            voices=voices,
        )
        retired = retired_source_reference_variants_from_manifest(manifest_document)
        story = load_story_index_document(story_path)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        VoiceManifestError,
        SourceReferenceBindingError,
        StoryIndexError,
    ) as error:
        raise MissingVoiceReuseError(str(error)) from error

    voice_by_name = {
        normalize_character_name(voice.character): voice for voice in voices
    }
    if len(voice_by_name) != len(voices):
        raise MissingVoiceReuseError("Voice manifest contains ambiguous characters")
    retired_names = {
        normalize_character_name(record["voice_character"]) for record in retired
    }
    candidates = _candidate_controls(
        manifest_path, voice_by_name, requested_candidates, retired_names
    )

    story_by_line_id = {}
    for record in story.records:
        if record.line_id in story_by_line_id:
            raise MissingVoiceReuseError(
                f"Story index contains duplicate line ID: {record.line_id}"
            )
        story_by_line_id[record.line_id] = record

    wanted = normalize_character_name(character)
    state_items = state["items"]
    targets = []
    for item in queue.items:
        if wanted not in {
            normalize_character_name(item.speaker),
            normalize_character_name(item.voice_character),
        }:
            continue
        if not is_spoken_queue_item(item):
            continue
        result = state_items.get(item.queue_id)
        effective_voice = overrides.get(item.queue_id, item.voice_character)
        if target_mode == "missing":
            if normalize_character_name(effective_voice) in voice_by_name:
                continue
            if result is not None:
                # Rejected and failed results have separate terminal authority.
                continue
        else:
            if item.queue_id not in requested_failed_ids:
                continue
            if not isinstance(result, dict) or result.get("status") != "failed":
                raise MissingVoiceReuseError(
                    f"Failed-voice target is not an exact failed result: {item.queue_id}"
                )
        record = story_by_line_id.get(item.line_id)
        if record is None:
            raise MissingVoiceReuseError(
                f"Missing-voice line is absent from the story index: {item.line_id}"
            )
        document = record.to_record()
        if (
            record.text_sha256 != item.text_sha256
            or record.text != item.text
            or normalize_character_name(record.speaker)
            != normalize_character_name(item.speaker)
        ):
            raise MissingVoiceReuseError(
                f"Story/queue identity changed for {item.queue_id}"
            )
        portrait = _text(document.get("portrait"), f"Portrait for {item.queue_id}")
        cohort_id = _cohort_for_portrait(cohort_rules, portrait, item.queue_id)
        word_count = len(re.findall(r"[\w’'-]+", item.text, flags=re.UNICODE))
        bucket = (
            "short" if word_count <= 6 else "medium" if word_count <= 14 else "long"
        )
        target = {
            "queue_id": item.queue_id,
            "line_id": item.line_id,
            "text": item.text,
            "text_sha256": item.text_sha256,
            "speaker": item.speaker,
            "declared_voice_character": item.voice_character,
            "portrait": portrait,
            "cohort_id": cohort_id,
            "word_count": word_count,
            "length_bucket": bucket,
            "state": "absent" if target_mode == "missing" else "failed",
            "voice_binding_status": (
                "missing" if target_mode == "missing" else "source_failed"
            ),
        }
        if target_mode == "failed":
            target.update(
                {
                    "source_voice_character": effective_voice,
                    "source_state_item_sha256": canonical_document_sha256(result),
                    "failure_category": generation_failure_category(
                        result if result.get("failure") else result.get("last_error")
                    ),
                }
            )
        targets.append(target)
    targets.sort(key=lambda value: value["queue_id"])
    if not targets:
        if target_mode == "failed":
            raise MissingVoiceReuseError(
                "Failed-voice queue IDs are absent from the exact character scope: "
                + ", ".join(sorted(requested_failed_ids))
            )
        raise MissingVoiceReuseError(
            f"Character has no spoken missing-voice items: {character!r}"
        )
    if target_mode == "failed":
        observed_ids = {target["queue_id"] for target in targets}
        missing_ids = sorted(requested_failed_ids - observed_ids)
        if missing_ids:
            raise MissingVoiceReuseError(
                "Failed-voice queue IDs are absent from the exact character scope: "
                + ", ".join(missing_ids)
            )
    observed_cohorts = {target["cohort_id"] for target in targets}
    declared_cohorts = {rule["cohort_id"] for rule in cohort_rules}
    unused = sorted(declared_cohorts - observed_cohorts)
    if unused:
        raise MissingVoiceReuseError(
            "Missing-voice cohort has no matching targets: " + ", ".join(unused)
        )
    samples = _comparison_samples(targets)
    if inline_pause_ms is not None:
        candidates = _inline_pause_candidates(
            candidates, samples, targets, inline_pause_ms
        )
    source = {
        "workspace": str(directory),
        "workspace_id": workspace["workspace_id"],
        "workspace_sha256": workspace_sha256,
        "queue_sha256": queue_sha256,
        "state_sha256": state_sha256,
        "voice_manifest_sha256": manifest_sha256,
        "story_index_sha256": story_sha256,
    }
    policy = {
        "authority": "plan_only_no_binding_generation_or_review_mutation",
        "cohorts_are_review_scopes_not_portrait_identity_proof": True,
        "retired_reference_variants_are_candidates": False,
        "sample_rule": (
            "one deterministic missing-voice item per available length bucket "
            "and exact declared cohort"
        ),
        "approval_scope": "one explicit decision per exact cohort",
        "neither_keeps_cohort_unbound": True,
    }
    if target_mode == "failed":
        policy.update(
            {
                "sample_rule": (
                    "one deterministic exact failed item per available length "
                    "bucket and exact declared cohort"
                ),
                "failed_source_is_non_playable_control": True,
            }
        )
    body = {
        "schema": MISSING_VOICE_REUSE_PLAN_SCHEMA,
        "schema_version": MISSING_VOICE_REUSE_PLAN_VERSION,
        "character": character,
        "source": source,
        "policy": policy,
        "cohorts": cohort_rules,
        "cohort_count": len(cohort_rules),
        "target_count": len(targets),
        "targets": targets,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "comparison_sample_count": len(samples),
        "comparison_samples": samples,
        "comparison_sample_queue_ids": [value["queue_id"] for value in samples],
    }
    if target_mode == "failed":
        body["target_mode"] = "failed"
    if inline_pause_ms is not None:
        body["candidate_mode"] = INLINE_PAUSE_MARKER
    plan_id = canonical_document_sha256(body)
    plan = MissingVoiceReusePlan(plan_id, {**body, "plan_id": plan_id})
    _validate_plan(plan)
    _assert_sources_unchanged(
        directory,
        workspace_sha256,
        queue_sha256,
        state_sha256,
        manifest_sha256,
        story_sha256,
        candidates,
    )
    return plan


def write_missing_voice_reuse_plan(plan, output_path):
    document = _validate_plan(plan)
    try:
        return _write_document_no_replace(
            output_path, document, "missing-voice reuse plan"
        )
    except CohortReviewError as error:
        raise MissingVoiceReuseError(str(error)) from error


def load_missing_voice_reuse_plan(path):
    try:
        document = _load_document(path, "missing-voice reuse plan")
    except CohortReviewError as error:
        raise MissingVoiceReuseError(str(error)) from error
    document = _validate_plan(document)
    return MissingVoiceReusePlan(document["plan_id"], document)


def prepare_missing_voice_reuse_candidate_workspace(
    plan,
    candidate_id,
    import_directory,
    input_root,
    workspaces_root,
):
    """Create one isolated sample-only workspace for an exact reuse candidate."""
    document = _validate_plan(plan)
    candidate = _candidate(document, candidate_id)
    source_directory = Path(document["source"]["workspace"]).resolve()
    input_directory, input_created = _publish_candidate_input(
        document, candidate, source_directory, input_root
    )
    _require_fresh_plan(document)
    try:
        source_directory, source_workspace, _workspace_sha256 = (
            _load_workspace_snapshot(source_directory, "missing-voice reuse source")
        )
        _source_queue, source_state, _source_payload, _source_state_sha256 = (
            _stable_workspace_state(
                source_directory,
                source_workspace,
                "missing-voice reuse source state",
            )
        )
        run_config = source_workspace.get("run_config")
        if not isinstance(run_config, dict):
            raise MissingVoiceReuseError(
                "Missing-voice reuse source run configuration is malformed"
            )
        target = create_resume_workspace(
            import_directory,
            workspaces_root,
            story_index=source_directory / "inputs/story-index.jsonl",
            voice_manifest=input_directory / "manifest.json",
            narrator_character=source_workspace["narrator_character"],
            backend=run_config["backend"],
            model=run_config["model"],
            generation_profile=run_config["generation_profile"],
            missing_voice_policy=run_config.get("missing_voice_policy"),
            failure_repair_policy=_candidate_failure_repair_policy(document),
        )
        created = target
        if document.get("target_mode", "missing") == "missing" and source_state.get(
            "items"
        ):
            created = rebase_workspace_config(
                source_directory, target.directory, workspaces_root
            )
        elif document.get("candidate_mode") == INLINE_PAUSE_MARKER:
            carry_failed_controls(
                source_directory,
                target.directory,
                tuple(document["comparison_sample_queue_ids"]),
            )
    except (AuthoringWorkbenchError, FailedControlCarryError) as error:
        raise MissingVoiceReuseError(str(error)) from error
    return MissingVoiceReuseCandidateWorkspace(
        document["plan_id"],
        candidate["candidate_id"],
        input_directory,
        created.directory,
        input_created,
        created.created,
        tuple(document["comparison_sample_queue_ids"]),
    )


def build_missing_voice_reuse_candidate_command(
    plan,
    candidate_id,
    workspace_directory,
    *,
    retries=0,
    seed=0,
):
    """Return one exact sample-only generation command for a candidate workspace."""
    document = _validate_plan(plan)
    candidate = _candidate(document, candidate_id)
    _require_fresh_plan(document)
    try:
        directory, workspace, _workspace_sha256 = _load_workspace_snapshot(
            workspace_directory, "missing-voice reuse candidate"
        )
    except AuthoringWorkbenchError as error:
        raise MissingVoiceReuseError(str(error)) from error
    source_workspace = Path(document["source"]["workspace"])
    try:
        _source_directory, source, _source_sha256 = _load_workspace_snapshot(
            source_workspace, "missing-voice reuse source command"
        )
    except AuthoringWorkbenchError as error:
        raise MissingVoiceReuseError(str(error)) from error
    for key in ("backend", "model", "generation_profile"):
        if workspace.get("run_config", {}).get(key) != source["run_config"].get(key):
            raise MissingVoiceReuseError(
                "Missing-voice candidate run configuration differs from its source"
            )
    manifest_path = directory / "inputs/voice/manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MissingVoiceReuseError(str(error)) from error
    if manifest.get(MISSING_VOICE_REUSE_BINDING_FIELD) != _candidate_binding(
        document, candidate
    ):
        raise MissingVoiceReuseError(
            "Missing-voice candidate manifest is not bound to the requested plan"
        )
    try:
        command = generation_command(
            directory,
            queue_ids=tuple(document["comparison_sample_queue_ids"]),
            retries=retries,
            seed=seed,
        )
    except AuthoringWorkbenchError as error:
        raise MissingVoiceReuseError(str(error)) from error
    observed_ids = tuple(
        command[index + 1]
        for index, value in enumerate(command[:-1])
        if value == "--queue-id"
    )
    if observed_ids != tuple(document["comparison_sample_queue_ids"]):
        raise MissingVoiceReuseError(
            "Missing-voice candidate generation scope differs from the plan"
        )
    if "--regenerate-existing" in command:
        raise MissingVoiceReuseError(
            "Missing-voice candidate command must not regenerate existing audio"
        )
    return command


def _candidate(document, candidate_id):
    matches = [
        candidate
        for candidate in document["candidates"]
        if candidate["candidate_id"] == candidate_id
    ]
    if len(matches) != 1:
        raise MissingVoiceReuseError(
            "Missing-voice reuse candidate is absent or ambiguous"
        )
    return matches[0]


def _require_fresh_plan(document):
    cohorts = {rule["label"]: tuple(rule["portraits"]) for rule in document["cohorts"]}
    fresh = build_missing_voice_reuse_plan(
        document["source"]["workspace"],
        document["character"],
        cohorts=cohorts,
        candidate_voice_characters=tuple(
            candidate["voice_character"] for candidate in document["candidates"]
        ),
        failed_queue_ids=(
            tuple(target["queue_id"] for target in document["targets"])
            if document.get("target_mode", "missing") == "failed"
            else None
        ),
        inline_pause_ms=(
            document["candidates"][0]["render_hypothesis"]["pause_ms"]
            if document.get("candidate_mode") == INLINE_PAUSE_MARKER
            else None
        ),
    )
    if fresh.plan_id != document["plan_id"]:
        raise MissingVoiceReuseError(
            "Missing-voice reuse source changed after planning"
        )


def _candidate_binding(document, candidate):
    overrides = {
        queue_id: candidate["voice_character"]
        for queue_id in document["comparison_sample_queue_ids"]
    }
    binding = {
        "schema": MISSING_VOICE_REUSE_BINDING_SCHEMA,
        "schema_version": MISSING_VOICE_REUSE_BINDING_VERSION,
        "mode": "comparison_sample_only",
        "plan_id": document["plan_id"],
        "candidate_id": candidate["candidate_id"],
        "source_voice_manifest_sha256": document["source"]["voice_manifest_sha256"],
        "source_workspace_id": document["source"]["workspace_id"],
        "source_workspace_sha256": document["source"]["workspace_sha256"],
        "candidate_voice_character": candidate["voice_character"],
        "candidate_reference_sha256s": [
            reference["sha256"] for reference in candidate["ordered_references"]
        ],
        "cohort_ids": sorted(rule["cohort_id"] for rule in document["cohorts"]),
        "queue_voice_overrides": overrides,
        "queue_voice_overrides_sha256": queue_voice_overrides_sha256(overrides),
        "authority": (
            "Comparison-only exact sample bindings. This authority does not bind "
            "the remaining cohort or approve generated audio."
        ),
    }
    if "render_hypothesis" in candidate:
        binding["candidate_render_hypothesis"] = copy.deepcopy(
            candidate["render_hypothesis"]
        )
    if document.get("target_mode", "missing") == "failed":
        target_by_id = {target["queue_id"]: target for target in document["targets"]}
        binding.update(
            {
                "target_mode": "failed",
                "source_failed_state_item_sha256s": {
                    queue_id: target_by_id[queue_id]["source_state_item_sha256"]
                    for queue_id in document["comparison_sample_queue_ids"]
                },
            }
        )
    return binding


def _publish_candidate_input(document, candidate, source_directory, input_root):
    root = Path(input_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = f"missing-voice-reuse-{document['plan_id'][:24]}-{candidate['candidate_id'][:16]}"
    destination = contained_workspace_path(
        root, Path(name), "Missing-voice candidate input"
    )
    if destination.is_symlink():
        raise MissingVoiceReuseError(
            "Missing-voice candidate input must not be a symbolic link"
        )
    if destination.exists():
        _validate_candidate_input(destination, document, candidate)
        return destination, False
    staging = Path(
        tempfile.mkdtemp(prefix=".missing-voice-reuse-staging-", dir=root)
    ).resolve()
    try:
        source_manifest = source_directory / "inputs/voice/manifest.json"
        source_payload = source_manifest.read_bytes()
        if (
            hashlib.sha256(source_payload).hexdigest()
            != document["source"]["voice_manifest_sha256"]
        ):
            raise MissingVoiceReuseError(
                "Missing-voice source manifest changed after planning"
            )
        try:
            manifest = json.loads(source_payload.decode("utf-8"))
            _metadata, voices = load_voice_manifest(source_manifest, allow_legacy=False)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            VoiceManifestError,
        ) as error:
            raise MissingVoiceReuseError(str(error)) from error
        if not _replaceable_predecessor_reuse_binding(manifest):
            raise MissingVoiceReuseError(
                "Source manifest already contains a missing-voice reuse binding"
            )
        source_root = source_manifest.parent.resolve()
        inventory = []
        seen = set()
        for voice in voices:
            for value in voice.references:
                relative = safe_workspace_relative_path(
                    value, "Missing-voice candidate reference"
                )
                source = contained_workspace_path(
                    source_root, relative, "Missing-voice candidate reference"
                )
                if source.is_symlink() or not source.is_file():
                    raise MissingVoiceReuseError(
                        f"Missing-voice candidate reference is unsafe: {value!r}"
                    )
                key = relative.as_posix()
                if key in seen:
                    continue
                seen.add(key)
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                digest = sha256_file(source)
                if sha256_file(target) != digest:
                    raise MissingVoiceReuseError(
                        "Missing-voice candidate reference changed while copied"
                    )
                inventory.append({"path": key, "sha256": digest})
        manifest[MISSING_VOICE_REUSE_BINDING_FIELD] = _candidate_binding(
            document, candidate
        )
        manifest_path = staging / "manifest.json"
        write_voice_manifest(manifest_path, manifest)
        inventory = [
            {"path": "manifest.json", "sha256": sha256_file(manifest_path)},
            *sorted(inventory, key=lambda item: item["path"]),
        ]
        body = {
            "schema": MISSING_VOICE_REUSE_CANDIDATE_BUNDLE_SCHEMA,
            "schema_version": MISSING_VOICE_REUSE_CANDIDATE_BUNDLE_VERSION,
            "plan_id": document["plan_id"],
            "candidate_id": candidate["candidate_id"],
            "source_voice_manifest_sha256": document["source"]["voice_manifest_sha256"],
            "inventory": inventory,
        }
        atomic_write_json(
            staging / "bundle.json",
            {**body, "bundle_id": canonical_document_sha256(body)},
            sort_keys=True,
        )
        _validate_candidate_input(staging, document, candidate)
        rename_directory_no_replace(staging, destination)
        staging = None
    except Exception:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        raise
    return destination, True


def _replaceable_predecessor_reuse_binding(manifest):
    """Allow a comparison layer over an exact, zero-override negative decision."""
    predecessor = manifest.get(MISSING_VOICE_REUSE_BINDING_FIELD)
    if predecessor is None:
        return True
    return (
        isinstance(predecessor, dict)
        and predecessor.get("schema") == MISSING_VOICE_REUSE_BINDING_SCHEMA
        and predecessor.get("schema_version")
        == MISSING_VOICE_REUSE_APPROVED_BINDING_VERSION
        and predecessor.get("mode") == "approved_cohort_reuse"
        and predecessor.get("selected_candidates") == []
        and predecessor.get("queue_voice_overrides") == {}
        and predecessor.get("queue_voice_overrides_sha256")
        == queue_voice_overrides_sha256({})
        and predecessor.get("authority")
        == (
            "Exact cohort reuse binding. Candidate choices require human review; "
            "cohorts with no selectable candidate are deterministically unresolved. "
            "Neither decisions bind no voice."
        )
        and isinstance(predecessor.get("decisions"), list)
        and predecessor["decisions"]
        and all(
            isinstance(decision, dict) and decision.get("decision") == "neither"
            for decision in predecessor["decisions"]
        )
    )


def _validate_candidate_input(directory, document, candidate):
    directory = Path(directory).resolve()
    bundle_path = directory / "bundle.json"
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MissingVoiceReuseError(str(error)) from error
    claimed = bundle.get("bundle_id")
    if (
        bundle.get("schema") != MISSING_VOICE_REUSE_CANDIDATE_BUNDLE_SCHEMA
        or bundle.get("schema_version") != MISSING_VOICE_REUSE_CANDIDATE_BUNDLE_VERSION
        or bundle.get("plan_id") != document["plan_id"]
        or bundle.get("candidate_id") != candidate["candidate_id"]
        or claimed
        != canonical_document_sha256(
            {key: value for key, value in bundle.items() if key != "bundle_id"}
        )
    ):
        raise MissingVoiceReuseError(
            "Missing-voice candidate bundle identity is invalid"
        )
    inventory = bundle.get("inventory")
    if not isinstance(inventory, list) or not inventory:
        raise MissingVoiceReuseError("Missing-voice candidate inventory is empty")
    declared = set()
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise MissingVoiceReuseError(
                "Missing-voice candidate inventory is malformed"
            )
        relative = safe_workspace_relative_path(
            item["path"], "Missing-voice candidate artifact"
        )
        path = contained_workspace_path(
            directory, relative, "Missing-voice candidate artifact"
        )
        if (
            path.is_symlink()
            or not path.is_file()
            or sha256_file(path) != item["sha256"]
        ):
            raise MissingVoiceReuseError("Missing-voice candidate artifact changed")
        if relative.as_posix() in declared:
            raise MissingVoiceReuseError(
                "Missing-voice candidate inventory contains duplicate paths"
            )
        declared.add(relative.as_posix())
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path.name != "bundle.json"
    }
    if declared != actual:
        raise MissingVoiceReuseError("Missing-voice candidate inventory is incomplete")
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _metadata, voices = load_voice_manifest(manifest_path, allow_legacy=False)
        overrides = queue_voice_overrides_from_manifest(manifest, voices=voices)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        VoiceManifestError,
        SourceReferenceBindingError,
    ) as error:
        raise MissingVoiceReuseError(str(error)) from error
    binding = _candidate_binding(document, candidate)
    if manifest.get(MISSING_VOICE_REUSE_BINDING_FIELD) != binding:
        raise MissingVoiceReuseError("Missing-voice candidate manifest binding changed")
    expected = binding["queue_voice_overrides"]
    if any(overrides.get(queue_id) != voice for queue_id, voice in expected.items()):
        raise MissingVoiceReuseError("Missing-voice candidate overrides changed")


def parse_cohort_arguments(values):
    """Parse repeated LABEL=PORTRAIT[,PORTRAIT] CLI arguments."""
    cohorts = {}
    for value in values or ():
        if not isinstance(value, str) or "=" not in value:
            raise MissingVoiceReuseError(
                "Missing-voice cohort must use LABEL=PORTRAIT[,PORTRAIT]"
            )
        label, raw_portraits = value.split("=", 1)
        label = _text(label, "Missing-voice cohort label")
        portraits = tuple(
            _text(portrait, f"Portrait in cohort {label!r}")
            for portrait in raw_portraits.split(",")
            if portrait.strip()
        )
        if not portraits:
            raise MissingVoiceReuseError(
                f"Missing-voice cohort has no portraits: {label}"
            )
        if label in cohorts:
            raise MissingVoiceReuseError(
                f"Missing-voice cohort label is duplicated: {label}"
            )
        cohorts[label] = portraits
    return cohorts


def _cohort_rules(cohorts):
    if not isinstance(cohorts, dict) or not cohorts:
        raise MissingVoiceReuseError("Missing-voice cohorts must be a non-empty map")
    seen_portraits = set()
    rules = []
    for label, values in sorted(cohorts.items()):
        label = _text(label, "Missing-voice cohort label")
        if not isinstance(values, (list, tuple, set)) or not values:
            raise MissingVoiceReuseError(
                f"Missing-voice cohort has no portraits: {label}"
            )
        portraits = sorted(
            {_text(value, f"Portrait in cohort {label!r}") for value in values}
        )
        overlap = seen_portraits.intersection(portraits)
        if overlap:
            raise MissingVoiceReuseError(
                "Missing-voice cohorts overlap portraits: " + ", ".join(sorted(overlap))
            )
        seen_portraits.update(portraits)
        identity = {"label": label, "portraits": portraits}
        rules.append({**identity, "cohort_id": canonical_document_sha256(identity)})
    return rules


def _candidate_names(values, *, minimum=2):
    if not isinstance(values, (list, tuple)) or len(values) < minimum:
        if minimum == 2:
            raise MissingVoiceReuseError(
                "Missing-voice reuse requires at least two candidate voices"
            )
        raise MissingVoiceReuseError(
            "Failed-voice reuse requires at least one candidate voice"
        )
    names = [_text(value, "Missing-voice candidate voice") for value in values]
    normalized = [normalize_character_name(value) for value in names]
    if len(set(normalized)) != len(normalized):
        raise MissingVoiceReuseError("Missing-voice candidate voices must be distinct")
    return names


def _inline_pause_candidates(candidates, samples, targets, pause_ms):
    if len(samples) != 1:
        raise MissingVoiceReuseError(
            "Inline-pause hypothesis requires exactly one comparison sample"
        )
    if (
        not isinstance(pause_ms, int)
        or isinstance(pause_ms, bool)
        or not 50 <= pause_ms <= 1000
    ):
        raise MissingVoiceReuseError(
            "Inline-pause duration must be an integer from 50 to 1000 ms"
        )
    target_by_id = {target["queue_id"]: target for target in targets}
    prompts = []
    for sample in samples:
        target = target_by_id[sample["queue_id"]]
        try:
            prompt, marker_count = inline_sentence_pause_prompt(
                target["text"], pause_ms=pause_ms
            )
        except ValueError as error:
            raise MissingVoiceReuseError(
                f"Inline-pause hypothesis is invalid for {sample['queue_id']}: {error}"
            ) from error
        prompts.append(
            {
                "queue_id": sample["queue_id"],
                "source_text_sha256": target["text_sha256"],
                "derived_prompt_sha256": hashlib.sha256(
                    prompt.encode("utf-8")
                ).hexdigest(),
                "marker_count": marker_count,
            }
        )
    hypothesis = {
        "strategy": INLINE_PAUSE_MARKER,
        "pause_ms": pause_ms,
        "prompts": prompts,
    }
    enriched = []
    for candidate in candidates:
        identity = {
            key: copy.deepcopy(value)
            for key, value in candidate.items()
            if key != "candidate_id"
        }
        identity["render_hypothesis"] = copy.deepcopy(hypothesis)
        enriched.append(
            {**identity, "candidate_id": canonical_document_sha256(identity)}
        )
    return enriched


def _candidate_failure_repair_policy(document):
    if document.get("candidate_mode") != INLINE_PAUSE_MARKER:
        return None
    hypothesis = document["candidates"][0]["render_hypothesis"]
    return FailureRepairPolicy(
        inline_pause_queue_ids=tuple(document["comparison_sample_queue_ids"]),
        inline_pause_ms=hypothesis["pause_ms"],
    )


def _failed_queue_ids(values):
    if values is None:
        return set()
    if not isinstance(values, (list, tuple)) or not values:
        raise MissingVoiceReuseError("Failed-voice queue IDs must be non-empty")
    queue_ids = [_text(value, "Failed-voice queue ID") for value in values]
    if len(queue_ids) != len(set(queue_ids)):
        raise MissingVoiceReuseError("Failed-voice queue IDs must be distinct")
    return set(queue_ids)


def _candidate_controls(manifest_path, voice_by_name, names, retired_names):
    root = manifest_path.parent.resolve()
    candidates = []
    for name in names:
        normalized = normalize_character_name(name)
        voice = voice_by_name.get(normalized)
        if voice is None or not voice.references:
            raise MissingVoiceReuseError(
                f"Missing-voice candidate is absent or has no references: {name!r}"
            )
        if normalized in retired_names:
            raise MissingVoiceReuseError(
                f"Retired source-reference voice cannot be reused: {name!r}"
            )
        references = []
        for value in voice.references:
            try:
                relative = safe_workspace_relative_path(
                    value, "Missing-voice candidate reference"
                )
                path = contained_workspace_path(
                    root, relative, "Missing-voice candidate reference"
                )
            except AuthoringWorkbenchError as error:
                raise MissingVoiceReuseError(str(error)) from error
            if path.is_symlink() or not path.is_file():
                raise MissingVoiceReuseError(
                    f"Missing-voice candidate reference is unsafe: {value!r}"
                )
            references.append(
                {"path": relative.as_posix(), "sha256": sha256_file(path)}
            )
        identity = {
            "voice_character": voice.character,
            "speaker": voice.speaker,
            "ordered_references": references,
        }
        candidates.append(
            {**identity, "candidate_id": canonical_document_sha256(identity)}
        )
    return candidates


def _cohort_for_portrait(rules, portrait, queue_id):
    matches = [
        rule["cohort_id"] for rule in rules if portrait in set(rule["portraits"])
    ]
    if len(matches) != 1:
        raise MissingVoiceReuseError(
            f"Missing-voice portrait is outside the exact cohort map: {queue_id} {portrait}"
        )
    return matches[0]


def _comparison_samples(targets):
    samples = []
    for cohort_id in sorted({value["cohort_id"] for value in targets}):
        for bucket in LENGTH_BUCKETS:
            candidates = [
                value
                for value in targets
                if value["cohort_id"] == cohort_id and value["length_bucket"] == bucket
            ]
            if not candidates:
                continue
            choice = min(
                candidates,
                key=lambda value: canonical_document_sha256(
                    {
                        "queue_id": value["queue_id"],
                        "text_sha256": value["text_sha256"],
                        "cohort_id": cohort_id,
                        "length_bucket": bucket,
                    }
                ),
            )
            samples.append(
                {
                    "queue_id": choice["queue_id"],
                    "cohort_id": cohort_id,
                    "length_bucket": bucket,
                }
            )
    return samples


def _validate_plan(plan):
    document = plan.document if isinstance(plan, MissingVoiceReusePlan) else plan
    if not isinstance(document, dict):
        raise MissingVoiceReuseError("Missing-voice reuse plan must be an object")
    if (
        document.get("schema") != MISSING_VOICE_REUSE_PLAN_SCHEMA
        or document.get("schema_version") != MISSING_VOICE_REUSE_PLAN_VERSION
    ):
        raise MissingVoiceReuseError("Missing-voice reuse plan schema is unsupported")
    claimed = document.get("plan_id")
    if claimed != canonical_document_sha256(
        {key: value for key, value in document.items() if key != "plan_id"}
    ):
        raise MissingVoiceReuseError("Missing-voice reuse plan identity is invalid")
    targets = document.get("targets")
    samples = document.get("comparison_samples")
    candidates = document.get("candidates")
    cohorts = document.get("cohorts")
    if not all(
        isinstance(value, list) and value
        for value in (targets, samples, candidates, cohorts)
    ):
        raise MissingVoiceReuseError("Missing-voice reuse plan is incomplete")
    queue_ids = [value.get("queue_id") for value in targets]
    if queue_ids != sorted(set(queue_ids)):
        raise MissingVoiceReuseError("Missing-voice targets are not canonical")
    target_ids = set(queue_ids)
    sample_ids = [value.get("queue_id") for value in samples]
    if len(sample_ids) != len(set(sample_ids)) or not set(sample_ids).issubset(
        target_ids
    ):
        raise MissingVoiceReuseError("Missing-voice samples are invalid")
    if document.get("target_count") != len(targets) or document.get(
        "comparison_sample_count"
    ) != len(samples):
        raise MissingVoiceReuseError("Missing-voice reuse counts are inconsistent")
    if document.get("comparison_sample_queue_ids") != sample_ids:
        raise MissingVoiceReuseError("Missing-voice sample order changed")
    candidate_ids = [value.get("candidate_id") for value in candidates]
    target_mode = document.get("target_mode", "missing")
    if target_mode not in {"missing", "failed"}:
        raise MissingVoiceReuseError("Missing-voice target mode is invalid")
    minimum_candidates = 1 if target_mode == "failed" else 2
    if len(candidate_ids) < minimum_candidates or len(candidate_ids) != len(
        set(candidate_ids)
    ):
        raise MissingVoiceReuseError("Missing-voice candidates are invalid")
    for candidate in candidates:
        if candidate.get("candidate_id") != canonical_document_sha256(
            {key: value for key, value in candidate.items() if key != "candidate_id"}
        ):
            raise MissingVoiceReuseError("Missing-voice candidate identity changed")
    candidate_mode = document.get("candidate_mode")
    if candidate_mode is None:
        if any("render_hypothesis" in candidate for candidate in candidates):
            raise MissingVoiceReuseError(
                "Missing-voice candidate hypothesis mode is absent"
            )
    elif candidate_mode == INLINE_PAUSE_MARKER and target_mode == "failed":
        target_by_id = {target["queue_id"]: target for target in targets}
        for candidate in candidates:
            hypothesis = candidate.get("render_hypothesis")
            prompts = (
                hypothesis.get("prompts") if isinstance(hypothesis, dict) else None
            )
            if (
                not isinstance(hypothesis, dict)
                or hypothesis.get("strategy") != INLINE_PAUSE_MARKER
                or not isinstance(hypothesis.get("pause_ms"), int)
                or isinstance(hypothesis.get("pause_ms"), bool)
                or not 50 <= hypothesis["pause_ms"] <= 1000
                or not isinstance(prompts, list)
                or [prompt.get("queue_id") for prompt in prompts] != sample_ids
            ):
                raise MissingVoiceReuseError(
                    "Missing-voice inline-pause hypothesis is invalid"
                )
            for prompt in prompts:
                target = target_by_id[prompt["queue_id"]]
                if (
                    prompt.get("source_text_sha256") != target["text_sha256"]
                    or not isinstance(prompt.get("derived_prompt_sha256"), str)
                    or len(prompt["derived_prompt_sha256"]) != 64
                    or not isinstance(prompt.get("marker_count"), int)
                    or isinstance(prompt.get("marker_count"), bool)
                    or prompt["marker_count"] < 1
                ):
                    raise MissingVoiceReuseError(
                        "Missing-voice inline-pause prompt identity is invalid"
                    )
    else:
        raise MissingVoiceReuseError("Missing-voice candidate mode is invalid")
    return copy.deepcopy(document)


def _assert_sources_unchanged(
    directory,
    workspace_sha256,
    queue_sha256,
    state_sha256,
    manifest_sha256,
    story_sha256,
    candidates,
):
    checks = (
        (directory / "workspace.json", workspace_sha256, "workspace"),
        (directory / "queue.jsonl", queue_sha256, "queue"),
        (
            directory / "generated-audio/generation-state.json",
            state_sha256,
            "generation state",
        ),
        (directory / "inputs/voice/manifest.json", manifest_sha256, "voice manifest"),
        (directory / "inputs/story-index.jsonl", story_sha256, "story index"),
    )
    for path, expected, label in checks:
        if not path.is_file() or sha256_file(path) != expected:
            raise MissingVoiceReuseError(
                f"Missing-voice reuse {label} changed while planning"
            )
    root = directory / "inputs/voice"
    for candidate in candidates:
        for reference in candidate["ordered_references"]:
            path = root / reference["path"]
            if not path.is_file() or sha256_file(path) != reference["sha256"]:
                raise MissingVoiceReuseError(
                    "Missing-voice reuse reference changed while planning"
                )


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise MissingVoiceReuseError(f"{label} must be non-empty text")
    return value.strip()


__all__ = [
    "MISSING_VOICE_REUSE_CANDIDATE_BUNDLE_SCHEMA",
    "MISSING_VOICE_REUSE_CANDIDATE_BUNDLE_VERSION",
    "MISSING_VOICE_REUSE_PLAN_SCHEMA",
    "MISSING_VOICE_REUSE_PLAN_VERSION",
    "MissingVoiceReuseCandidateWorkspace",
    "MissingVoiceReuseError",
    "MissingVoiceReusePlan",
    "build_missing_voice_reuse_candidate_command",
    "build_missing_voice_reuse_plan",
    "load_missing_voice_reuse_plan",
    "parse_cohort_arguments",
    "prepare_missing_voice_reuse_candidate_workspace",
    "write_missing_voice_reuse_plan",
]
