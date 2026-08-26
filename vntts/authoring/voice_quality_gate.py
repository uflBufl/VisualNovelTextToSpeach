"""Reusable, checksum-bound voice-control quality gates across stories."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
)

from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    _canonical_sha256,
    sha256_control_path,
)
from vntts.authoring.cohort_review import (
    CohortReviewDecision,
    CohortReviewError,
    _load_document,
    _validate_decision_against_plan,
    _validated_decision_document,
    _validated_plan_document,
    _write_document_no_replace,
    build_cohort_review_plan,
    load_cohort_review_decision,
    load_cohort_review_plan,
)
from vntts.authoring.workbench import AuthoringWorkbenchError, _load_workspace

VOICE_QUALITY_GATE_SCHEMA = "vntts.authoring-voice-quality-gate"
VOICE_QUALITY_GATE_VERSION = 1


class VoiceQualityGateError(RuntimeError):
    """A reusable voice-quality decision is incomplete or no longer matches."""


@dataclass(frozen=True)
class VoiceQualityGate:
    gate_id: str
    document: dict

    def to_dict(self):
        return copy.deepcopy(self.document)


@dataclass(frozen=True)
class VoiceQualityCompatibility:
    gate_id: str
    queue_id: str
    status: str
    differences: tuple[str, ...]
    story_sample_required: bool

    def to_dict(self):
        return {
            "gate_id": self.gate_id,
            "queue_id": self.queue_id,
            "status": self.status,
            "differences": list(self.differences),
            "story_sample_required": self.story_sample_required,
        }


def build_voice_quality_gate(workspace_directory, plan, decision):
    """Build one reusable control gate from an accepted exact cohort review."""
    plan_document = _plan_document(plan)
    decision_document = _decision_document(decision)
    try:
        _validate_decision_against_plan(plan_document, decision_document)
    except CohortReviewError as error:
        raise VoiceQualityGateError(str(error)) from error
    if decision_document["decision"] != "accepted":
        raise VoiceQualityGateError(
            "A reusable voice-quality gate requires an accepted cohort decision"
        )
    cohort = next(
        value
        for value in plan_document["cohorts"]
        if value["cohort_id"] == decision_document["cohort_id"]
    )
    try:
        directory, workspace = _load_workspace(workspace_directory)
    except AuthoringWorkbenchError as error:
        raise VoiceQualityGateError(str(error)) from error
    if workspace.get("workspace_id") != plan_document["workspace_id"]:
        raise VoiceQualityGateError("Cohort review belongs to another workspace")
    identity = _reusable_identity(directory, workspace, cohort["identity"])
    source = {
        "workspace_id": plan_document["workspace_id"],
        "plan_id": plan_document["plan_id"],
        "decision_id": decision_document["decision_id"],
        "cohort_id": cohort["cohort_id"],
        "source_synthesis_provenance_sha256": cohort["identity"][
            "synthesis_provenance_sha256"
        ],
        "source_seed": cohort["identity"]["seed"],
        "sample_queue_ids": list(decision_document["sample_queue_ids"]),
        "reviewed_samples": copy.deepcopy(decision_document["reviewed_samples"]),
        "sample_assessments": copy.deepcopy(
            decision_document.get("sample_assessments", [])
        ),
        "target_items": copy.deepcopy(decision_document["target_items"]),
    }
    body = {
        "schema": VOICE_QUALITY_GATE_SCHEMA,
        "schema_version": VOICE_QUALITY_GATE_VERSION,
        "reuse_policy": {
            "story_sample_required": True,
            "technical_attention_rule": "all technical flags",
            "authority": (
                "A matching gate validates reused synthesis controls only. It never "
                "approves or rejects a later story item."
            ),
        },
        "identity": identity,
        "source_review": source,
    }
    gate_id = _canonical_sha256(body)
    return VoiceQualityGate(gate_id, {**body, "gate_id": gate_id})


def write_voice_quality_gate(gate, output_path):
    """Publish a validated gate without replacing existing evidence."""
    document = _validated_gate_document(gate)
    try:
        return _write_document_no_replace(output_path, document, "voice-quality gate")
    except CohortReviewError as error:
        raise VoiceQualityGateError(str(error)) from error


def load_voice_quality_gate(path):
    """Load and validate one standalone reusable gate."""
    try:
        document = _load_document(path, "voice-quality gate")
    except CohortReviewError as error:
        raise VoiceQualityGateError(str(error)) from error
    document = _validated_gate_document(document)
    return VoiceQualityGate(document["gate_id"], document)


def inspect_voice_quality_gate(gate, workspace_directory, queue_id):
    """Compare one later pending item without projecting a review decision."""
    document = _validated_gate_document(gate)
    try:
        plan = build_cohort_review_plan(workspace_directory, queue_ids=[queue_id])
    except CohortReviewError as error:
        raise VoiceQualityGateError(str(error)) from error
    if len(plan.document["cohorts"]) != 1:
        raise VoiceQualityGateError("Selected item has no reusable review cohort")
    cohort = plan.document["cohorts"][0]
    try:
        directory, workspace = _load_workspace(workspace_directory)
    except AuthoringWorkbenchError as error:
        raise VoiceQualityGateError(str(error)) from error
    current = _reusable_identity(directory, workspace, cohort["identity"])
    expected = document["identity"]
    differences = tuple(
        key
        for key in sorted(set(expected) | set(current))
        if expected.get(key) != current.get(key)
    )
    return VoiceQualityCompatibility(
        document["gate_id"],
        queue_id,
        "control_match_story_sample_required" if not differences else "new_review",
        differences,
        True,
    )


def _reusable_identity(directory, workspace, cohort_identity):
    run_config = workspace.get("run_config")
    if not isinstance(run_config, dict):
        raise VoiceQualityGateError("Workspace run configuration is malformed")
    expected_run = {
        "provider": run_config.get("backend"),
        "model": run_config.get("model"),
        "generation_profile": run_config.get("generation_profile"),
    }
    observed_run = {
        "provider": cohort_identity.get("provider"),
        "model": cohort_identity.get("model"),
        "generation_profile": cohort_identity.get("generation_profile"),
    }
    if observed_run != expected_run:
        raise VoiceQualityGateError(
            "Reviewed cohort does not match the workspace synthesis run configuration"
        )
    voice_character = _required_text(
        cohort_identity.get("voice_character"), "Voice character"
    )
    manifest_voice_character = voice_character
    if normalize_character_name(voice_character) == "narrator":
        manifest_voice_character = _required_text(
            workspace.get("narrator_character"), "Workspace narrator character"
        )
    manifest_config = workspace.get("voice_manifest")
    if not isinstance(manifest_config, dict) or not isinstance(
        manifest_config.get("path"), str
    ):
        raise VoiceQualityGateError("Workspace voice manifest is malformed")
    manifest_path = (directory / manifest_config["path"]).resolve()
    try:
        manifest_path.relative_to(directory)
        _metadata, voices = load_voice_manifest(manifest_path, allow_legacy=False)
    except (ValueError, VoiceManifestError) as error:
        raise VoiceQualityGateError(str(error)) from error
    wanted = normalize_character_name(manifest_voice_character)
    matches = [
        voice
        for voice in voices
        if wanted
        in {
            normalize_character_name(voice.character),
            *(normalize_character_name(alias) for alias in voice.aliases),
        }
    ]
    if len(matches) != 1 or not matches[0].references:
        raise VoiceQualityGateError(
            "Voice-quality variant is absent or ambiguous: "
            f"{manifest_voice_character!r}"
        )
    reference_hashes = []
    for reference in matches[0].references:
        raw = manifest_path.parent / reference
        relative = Path(reference)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise VoiceQualityGateError("Voice reference path is unsafe")
        current = manifest_path.parent
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise VoiceQualityGateError("Voice reference is missing or unsafe")
        path = raw.resolve()
        try:
            path.relative_to(manifest_path.parent.resolve())
        except ValueError as error:
            raise VoiceQualityGateError(
                "Voice reference leaves its manifest"
            ) from error
        if not path.is_file():
            raise VoiceQualityGateError("Voice reference is missing or unsafe")
        reference_hashes.append(sha256_file(path))
    model = _required_text(cohort_identity.get("model"), "Generation model")
    model_path = Path(model).expanduser()
    if model_path.exists():
        try:
            model_control = {
                "kind": "path",
                "sha256": sha256_control_path(model_path),
            }
        except BulkGenerationError as error:
            raise VoiceQualityGateError(str(error)) from error
    else:
        model_control = {
            "kind": "identifier",
            "sha256": _canonical_sha256({"model": model}),
        }
    binding = cohort_identity.get("source_reference_binding")
    if isinstance(binding, dict):
        binding = {
            "schema_version": binding.get("schema_version"),
            "source_voice_character": binding.get("source_voice_character"),
            "synthesis_voice_character": binding.get("synthesis_voice_character"),
        }
    return {
        "provider": _required_text(
            cohort_identity.get("provider"), "Generation provider"
        ),
        "model": model,
        "model_control": model_control,
        "generation_profile": _required_text(
            cohort_identity.get("generation_profile"), "Generation profile"
        ),
        "voice_character": voice_character,
        "voice_speaker": _required_text(matches[0].speaker, "Voice speaker"),
        "ordered_reference_sha256": reference_hashes,
        "prompt_sha256": _required_sha256(
            cohort_identity.get("prompt_sha256"), "Prompt sha256"
        ),
        "prompt_applied": _required_bool(
            cohort_identity.get("prompt_applied"), "Prompt-applied marker"
        ),
        "text_transform": cohort_identity.get("text_transform"),
        "repair_strategy": cohort_identity.get("repair_strategy"),
        "source_reference_binding": binding,
    }


def _validated_gate_document(gate):
    if isinstance(gate, VoiceQualityGate):
        document = gate.document
    elif isinstance(gate, dict):
        document = gate
    else:
        raise VoiceQualityGateError("Voice-quality gate must be a document")
    if (
        not isinstance(document, dict)
        or document.get("schema") != VOICE_QUALITY_GATE_SCHEMA
        or document.get("schema_version") != VOICE_QUALITY_GATE_VERSION
    ):
        raise VoiceQualityGateError("Voice-quality gate schema is unsupported")
    claimed = _required_sha256(document.get("gate_id"), "Voice-quality gate ID")
    actual = _canonical_sha256(
        {key: value for key, value in document.items() if key != "gate_id"}
    )
    if claimed != actual:
        raise VoiceQualityGateError("Voice-quality gate identity is invalid")
    policy = document.get("reuse_policy")
    if not isinstance(policy, dict) or policy.get("story_sample_required") is not True:
        raise VoiceQualityGateError("Voice-quality reuse policy is unsafe")
    identity = document.get("identity")
    if not isinstance(identity, dict):
        raise VoiceQualityGateError("Voice-quality identity is malformed")
    if set(identity) != {
        "provider",
        "model",
        "model_control",
        "generation_profile",
        "voice_character",
        "voice_speaker",
        "ordered_reference_sha256",
        "prompt_sha256",
        "prompt_applied",
        "text_transform",
        "repair_strategy",
        "source_reference_binding",
    }:
        raise VoiceQualityGateError("Voice-quality identity fields are malformed")
    for field in ("provider", "model", "generation_profile", "voice_character"):
        _required_text(identity.get(field), field)
    references = identity.get("ordered_reference_sha256")
    if not isinstance(references, list) or not references:
        raise VoiceQualityGateError("Voice-quality references are missing")
    for digest in references:
        _required_sha256(digest, "Voice reference sha256")
    model_control = identity.get("model_control")
    if (
        not isinstance(model_control, dict)
        or set(model_control) != {"kind", "sha256"}
        or model_control.get("kind") not in {"path", "identifier"}
    ):
        raise VoiceQualityGateError("Voice-quality model control is malformed")
    _required_sha256(model_control.get("sha256"), "Model control sha256")
    _required_sha256(identity.get("prompt_sha256"), "Prompt sha256")
    _required_bool(identity.get("prompt_applied"), "Prompt-applied marker")
    for field in ("text_transform", "repair_strategy"):
        value = identity.get(field)
        if value is not None:
            _required_text(value, field)
    source = document.get("source_review")
    if not isinstance(source, dict):
        raise VoiceQualityGateError("Voice-quality source review is malformed")
    for field in (
        "plan_id",
        "decision_id",
        "cohort_id",
        "source_synthesis_provenance_sha256",
    ):
        _required_sha256(source.get(field), field)
    if not isinstance(source.get("reviewed_samples"), list) or not source.get(
        "reviewed_samples"
    ):
        raise VoiceQualityGateError("Voice-quality source samples are missing")
    assessments = source.get("sample_assessments")
    if not isinstance(assessments, list) or any(
        not isinstance(value, dict)
        or value.get("assessment") not in {"heard", "acceptable"}
        for value in assessments
    ):
        raise VoiceQualityGateError("Voice-quality source assessments are unsafe")
    return copy.deepcopy(document)


def _plan_document(plan):
    try:
        if isinstance(plan, (str, Path)):
            plan = load_cohort_review_plan(plan)
        return _validated_plan_document(plan)
    except CohortReviewError as error:
        raise VoiceQualityGateError(str(error)) from error


def _decision_document(decision):
    try:
        if isinstance(decision, (str, Path)):
            decision = load_cohort_review_decision(decision)
        if isinstance(decision, CohortReviewDecision):
            document = decision.document
        elif isinstance(decision, dict):
            document = decision
        else:
            raise CohortReviewError("Cohort review decision must be a document")
        _validated_decision_document(document)
        return copy.deepcopy(document)
    except CohortReviewError as error:
        raise VoiceQualityGateError(str(error)) from error


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise VoiceQualityGateError(f"{label} must be non-empty text")
    return value


def _required_sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise VoiceQualityGateError(f"{label} must be lowercase SHA-256")
    return value


def _required_bool(value, label):
    if not isinstance(value, bool):
        raise VoiceQualityGateError(f"{label} must be boolean")
    return value


__all__ = [
    "VOICE_QUALITY_GATE_SCHEMA",
    "VOICE_QUALITY_GATE_VERSION",
    "VoiceQualityCompatibility",
    "VoiceQualityGate",
    "VoiceQualityGateError",
    "build_voice_quality_gate",
    "inspect_voice_quality_gate",
    "load_voice_quality_gate",
    "write_voice_quality_gate",
]
