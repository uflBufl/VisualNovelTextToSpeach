"""Checksum-bound blind review of bounded missing-voice reuse candidates."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import shutil
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias, TypedDict

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.missing_voice_reuse import (
    MISSING_VOICE_REUSE_PLAN_SCHEMA,
    JsonObject,
    MissingVoiceReuseError,
    _PlanDocument,
    _validate_plan,
    load_missing_voice_reuse_plan,
)
from vntts.authoring.private_files import private_file_is_restricted
from vntts.authoring.publication import staged_directory
from vntts.authoring.source_reference_bindings import (
    MISSING_VOICE_REUSE_BINDING_FIELD,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    contained_workspace_path,
    load_workspace_authority,
    safe_workspace_relative_path,
)
from vntts.authoring.workspace_foundation import load_json_object
from vntts.authoring.workspace_state import load_stable_workspace_generation_state

REVIEW_BUNDLE_SCHEMA = "vntts.authoring-missing-voice-reuse-review-bundle"
REVIEW_SESSION_SCHEMA = "vntts.authoring-missing-voice-reuse-review-session"
REVIEW_KEY_SCHEMA = "vntts.authoring-missing-voice-reuse-review-key"
REVIEW_VERSION = 1
AUTOMATIC_UNRESOLVED_ORIGIN = "automatic_no_complete_candidate"


class ReviewSample(TypedDict, total=False):
    queue_id: str
    line_id: str
    text: str
    text_sha256: str
    length_bucket: str
    portrait: object
    cohort_id: str


class ReviewArm(TypedDict, total=False):
    queue_id: str
    status: str
    attempt_count: int
    audio: str
    audio_sha256: str
    quality: object
    repair_strategy: str | None
    failure_kind: str
    failure_summary: str


class ReviewCandidate(TypedDict, total=False):
    label: str
    candidate_id: str
    voice_character: str
    speaker: str
    samples: list[ReviewArm]
    generated_count: int


class ReviewCohort(TypedDict, total=False):
    cohort_id: str
    sample_count: int
    samples: list[ReviewSample]
    complete_candidate_labels: list[str]
    decision_options: list[str]


class ReviewDecision(TypedDict, total=False):
    cohort_id: str
    decision: str | None
    decided_at: str
    decision_origin: str


class HeardRecord(TypedDict):
    cohort_id: str
    queue_id: str
    label: str


class ReviewBundle(TypedDict, total=False):
    schema: str
    schema_version: int
    bundle_id: str
    plan: JsonObject
    target_mode: str
    character: str
    decision_context: JsonObject | None
    candidates: list[ReviewCandidate]
    cohorts: list[ReviewCohort]
    source_control: list[JsonObject]
    cohort_count: int
    candidate_count: int
    blind_key_sha256: str
    plan_id: str
    seed: int
    policy: JsonObject


class ReviewSession(TypedDict, total=False):
    schema: str
    schema_version: int
    bundle_id: str
    bundle_sha256: str
    created_at: str
    updated_at: str
    heard: list[HeardRecord]
    decisions: list[ReviewDecision]


class PlanCandidate(TypedDict, total=False):
    candidate_id: str
    voice_character: str
    speaker: str
    ordered_references: list[JsonObject]
    render_hypothesis: JsonObject | None


class PlanTarget(TypedDict, total=False):
    queue_id: str
    cohort_id: str
    line_id: str
    text: str
    text_sha256: str
    portrait: object
    failure_category: str
    source_state_item_sha256: str


class PlanSample(TypedDict, total=False):
    queue_id: str
    cohort_id: str
    line_id: str
    text: str
    text_sha256: str
    length_bucket: str
    portrait: object


class CandidateSnapshot(TypedDict):
    directory: Path
    workspace: JsonObject
    state: JsonObject
    authority: JsonObject


PlanDocument: TypeAlias = _PlanDocument


def _plan_candidates(document: PlanDocument) -> list[PlanCandidate]:
    values = document["candidates"]
    if any(not isinstance(value, dict) for value in values):
        raise MissingVoiceReuseReviewError("Plan candidates are malformed")
    candidates = [value for value in values if isinstance(value, dict)]
    if any(
        not isinstance(value.get("candidate_id"), str)
        or not isinstance(value.get("voice_character"), str)
        or not isinstance(value.get("speaker"), str)
        or not isinstance(value.get("ordered_references"), list)
        for value in candidates
    ):
        raise MissingVoiceReuseReviewError("Plan candidates are malformed")
    result: list[PlanCandidate] = []
    for value in candidates:
        candidate: PlanCandidate = {
            "candidate_id": _text(value.get("candidate_id"), "Plan candidate ID"),
            "voice_character": _text(
                value.get("voice_character"), "Plan candidate voice"
            ),
            "speaker": _text(value.get("speaker"), "Plan candidate speaker"),
            "ordered_references": _objects(
                value.get("ordered_references"), "Plan candidate references"
            ),
        }
        if isinstance(value.get("render_hypothesis"), dict):
            candidate["render_hypothesis"] = _object(
                value["render_hypothesis"], "Plan render hypothesis"
            )
        result.append(candidate)
    return result


def _plan_targets(document: PlanDocument) -> list[PlanTarget]:
    values = document["targets"]
    if any(not isinstance(value, dict) for value in values):
        raise MissingVoiceReuseReviewError("Plan targets are malformed")
    targets = [value for value in values if isinstance(value, dict)]
    if any(
        not isinstance(value.get("queue_id"), str)
        or not isinstance(value.get("cohort_id"), str)
        for value in targets
    ):
        raise MissingVoiceReuseReviewError("Plan targets are malformed")
    result: list[PlanTarget] = []
    for value in targets:
        target: PlanTarget = {
            "queue_id": _text(value.get("queue_id"), "Plan target queue ID"),
            "cohort_id": _text(value.get("cohort_id"), "Plan target cohort ID"),
        }
        line_id = value.get("line_id")
        text = value.get("text")
        text_sha256 = value.get("text_sha256")
        failure_category = value.get("failure_category")
        source_state_item_sha256 = value.get("source_state_item_sha256")
        if isinstance(line_id, str):
            target["line_id"] = line_id
        if isinstance(text, str):
            target["text"] = text
        if isinstance(text_sha256, str):
            target["text_sha256"] = text_sha256
        if isinstance(failure_category, str):
            target["failure_category"] = failure_category
        if isinstance(source_state_item_sha256, str):
            target["source_state_item_sha256"] = source_state_item_sha256
        if "portrait" in value:
            target["portrait"] = value["portrait"]
        result.append(target)
    return result


def _plan_samples(document: PlanDocument) -> list[PlanSample]:
    values = document["comparison_samples"]
    if any(not isinstance(value, dict) for value in values):
        raise MissingVoiceReuseReviewError("Plan samples are malformed")
    samples = [value for value in values if isinstance(value, dict)]
    if any(
        not isinstance(value.get("queue_id"), str)
        or not isinstance(value.get("cohort_id"), str)
        for value in samples
    ):
        raise MissingVoiceReuseReviewError("Plan samples are malformed")
    result: list[PlanSample] = []
    for value in samples:
        sample: PlanSample = {
            "queue_id": _text(value.get("queue_id"), "Plan sample queue ID"),
            "cohort_id": _text(value.get("cohort_id"), "Plan sample cohort ID"),
        }
        line_id = value.get("line_id")
        text = value.get("text")
        text_sha256 = value.get("text_sha256")
        length_bucket = value.get("length_bucket")
        if isinstance(line_id, str):
            sample["line_id"] = line_id
        if isinstance(text, str):
            sample["text"] = text
        if isinstance(text_sha256, str):
            sample["text_sha256"] = text_sha256
        if isinstance(length_bucket, str):
            sample["length_bucket"] = length_bucket
        if "portrait" in value:
            sample["portrait"] = value["portrait"]
        result.append(sample)
    return result


class MissingVoiceReuseReviewError(RuntimeError):
    """Missing-voice reuse review evidence is incomplete or has changed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shared_value(
    values: Iterable[object], *, hidden: str = "Hidden for this blind comparison"
) -> object:
    normalized = {value for value in values if value not in {None, ""}}
    if len(normalized) == 1:
        return next(iter(normalized))
    return hidden if len(normalized) > 1 else "Unknown"


def _published_decision_context(
    document: PlanDocument,
    candidates: list[PlanCandidate],
    candidate_snapshots: dict[str, list[CandidateSnapshot]],
    candidate_evidence: dict[str, dict[str, JsonObject]],
) -> JsonObject:
    """Publish shared synthesis facts without revealing differing blind arms."""
    run_configs = _candidate_run_configs(candidates, candidate_snapshots)
    outcome_items = [
        item for evidence in candidate_evidence.values() for item in evidence.values()
    ]
    mode = document.get("target_mode", "missing")

    return {
        "purpose": (
            "Choose a replacement WAV for a line whose original render failed"
            if mode == "failed"
            else "Choose a reusable voice for this unvoiced character family"
        ),
        "game_speaker": document["character"],
        "synthesis_voice": _shared_value(
            {candidate["voice_character"] for candidate in candidates}
        ),
        "reference": _published_reference(candidates),
        "backend": _synthesis_value(outcome_items, run_configs, "provider", "backend"),
        "model": _synthesis_value(outcome_items, run_configs, "model", "model"),
        "generation_profile": _synthesis_value(
            outcome_items, run_configs, "generation_profile", "generation_profile"
        ),
        "seed": _shared_value({item.get("seed") for item in outcome_items}),
        "controls": _published_controls(candidates),
        "effect": (
            "select this checksum-bound fallback WAV, or keep the line unresolved"
            if mode == "failed"
            else "bind one complete candidate to this exact cohort, or keep it unbound"
        ),
        "technical": {
            "plan_id": document["plan_id"],
            "workspace_ids": sorted(
                {
                    _text(
                        snapshot["workspace"].get("workspace_id"),
                        "Candidate workspace ID",
                    )
                    for snapshots in candidate_snapshots.values()
                    for snapshot in snapshots
                }
            ),
        },
    }


def _candidate_run_configs(
    candidates: list[PlanCandidate],
    candidate_snapshots: dict[str, list[CandidateSnapshot]],
) -> list[JsonObject]:
    return [
        _object(run_config, "Candidate run config")
        if isinstance(run_config, dict)
        else {}
        for candidate in candidates
        for snapshot in candidate_snapshots[candidate["candidate_id"]]
        for run_config in (snapshot["workspace"].get("run_config"),)
    ]


def _published_reference(candidates: list[PlanCandidate]) -> str:
    references = {
        tuple(
            _text(reference.get("path"), "Candidate reference path")
            for reference in candidate["ordered_references"]
        )
        for candidate in candidates
    }
    if len(references) != 1:
        return "Hidden for this blind comparison"
    paths = next(iter(references))
    reference = ", ".join("/".join(Path(value).parts[-2:]) for value in paths)
    return f"{len(paths)}-file composite: {reference}" if len(paths) > 1 else reference


def _published_controls(candidates: list[PlanCandidate]) -> object:
    hypotheses = [
        hypothesis
        for candidate in candidates
        if isinstance(hypothesis := candidate.get("render_hypothesis"), dict)
    ]
    controls = _shared_value(
        {
            hypothesis.get("strategy")
            if isinstance(hypothesis, dict)
            else "direct render"
            for hypothesis in (
                candidate.get("render_hypothesis") for candidate in candidates
            )
        }
    )
    pause_values = {hypothesis.get("pause_ms") for hypothesis in hypotheses}
    if (
        len(hypotheses) == len(candidates)
        and len(pause_values) == 1
        and isinstance(hypotheses[0].get("pause_ms"), int)
        and isinstance(controls, str)
    ):
        return f"{controls}, {hypotheses[0]['pause_ms']} ms inserted pause"
    return controls


def _synthesis_value(
    outcome_items: list[JsonObject],
    run_configs: list[JsonObject],
    item_field: str,
    config_field: str,
) -> object:
    values = {item.get(item_field) for item in outcome_items}
    return (
        _shared_value(values)
        if any(value not in {None, ""} for value in values)
        else _shared_value({config.get(config_field) for config in run_configs})
    )


def build_missing_voice_reuse_review(
    plan_path: str | Path,
    evidence_workspaces: Mapping[str, Sequence[str | Path]],
    output_directory: str | Path,
    *,
    seed: int = 0,
) -> Path:
    """Publish one immutable blind evidence matrix and resumable session."""
    plan_path = Path(plan_path).expanduser().resolve()
    document, candidates = _review_plan(plan_path)
    candidate_ids = [candidate["candidate_id"] for candidate in candidates]
    _validate_review_build_inputs(evidence_workspaces, candidate_ids, seed)
    ordered, labels = _blind_candidate_labels(candidate_ids, seed)
    target_by_id = {target["queue_id"]: target for target in _plan_targets(document)}
    sample_by_id = _review_samples_by_id(document, target_by_id)
    evidence, candidate_snapshots, private_candidates = _review_candidate_evidence(
        document, candidates, evidence_workspaces, labels
    )

    root = Path(output_directory).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise MissingVoiceReuseReviewError(
            f"Missing-voice review directory is not empty: {root}"
        )
    root.parent.mkdir(parents=True, exist_ok=True)
    with staged_directory(root.parent, prefix=f".{root.name}-") as staging:
        public_candidates = _publish_review_candidates(
            staging, document, ordered, labels, evidence
        )
        cohorts = _review_cohorts(document, sample_by_id, public_candidates)

        key = {
            "schema": REVIEW_KEY_SCHEMA,
            "schema_version": REVIEW_VERSION,
            "plan_id": document["plan_id"],
            "candidates": private_candidates,
        }
        key_path = staging / ".blind-key.json"
        _write_private_json(key_path, key)
        body = {
            "schema": REVIEW_BUNDLE_SCHEMA,
            "schema_version": REVIEW_VERSION,
            "plan": {
                "path": str(plan_path),
                "sha256": sha256_file(plan_path),
                "schema": MISSING_VOICE_REUSE_PLAN_SCHEMA,
                "plan_id": document["plan_id"],
            },
            "character": document["character"],
            "decision_context": _published_decision_context(
                document, candidates, candidate_snapshots, evidence
            ),
            "seed": seed,
            "policy": {
                "candidate_identity": "stable opaque labels",
                "failed_arms": "visible and never selectable",
                "candidate_gate": (
                    "all exact cohort samples generated and all available cohort "
                    "audio heard"
                ),
                "neither_gate": "all available cohort audio heard",
                "decision_scope": "one exact candidate or neither per cohort",
            },
            "blind_key_sha256": sha256_file(key_path),
            "candidate_count": len(public_candidates),
            "candidates": public_candidates,
            "cohort_count": len(cohorts),
            "cohorts": cohorts,
        }
        if document.get("target_mode", "missing") == "failed":
            body.update(
                {
                    "target_mode": "failed",
                    "source_control": [
                        {
                            "queue_id": queue_id,
                            "status": "failed",
                            "failure_category": target_by_id[queue_id][
                                "failure_category"
                            ],
                            "state_item_sha256": target_by_id[queue_id][
                                "source_state_item_sha256"
                            ],
                        }
                        for queue_id in document["comparison_sample_queue_ids"]
                    ],
                }
            )
        bundle_id = canonical_document_sha256(body)
        bundle = {**body, "bundle_id": bundle_id}
        atomic_write_json(staging / "bundle.json", bundle, sort_keys=True)
        created_at = _utc_now()
        session = {
            "schema": REVIEW_SESSION_SCHEMA,
            "schema_version": REVIEW_VERSION,
            "bundle_id": bundle_id,
            "bundle_sha256": sha256_file(staging / "bundle.json"),
            "created_at": created_at,
            "updated_at": created_at,
            "heard": [],
            "decisions": [
                (
                    {"cohort_id": cohort["cohort_id"], "decision": None}
                    if cohort["complete_candidate_labels"]
                    else _automatic_unresolved_decision(cohort["cohort_id"], created_at)
                )
                for cohort in cohorts
            ],
        }
        atomic_write_json(staging / "session.json", session, sort_keys=True)
        try:
            os.rename(staging, root)
        except OSError as error:
            if root.exists():
                raise MissingVoiceReuseReviewError(
                    f"Missing-voice review destination already exists: {root}"
                ) from error
            raise
    load_missing_voice_reuse_review(root / "session.json")
    return root / "session.json"


def _review_plan(plan_path: Path) -> tuple[PlanDocument, list[PlanCandidate]]:
    try:
        plan = load_missing_voice_reuse_plan(plan_path)
    except MissingVoiceReuseError as error:
        raise MissingVoiceReuseReviewError(str(error)) from error
    document: PlanDocument = _validate_plan(plan)
    return document, _plan_candidates(document)


def _validate_review_build_inputs(
    evidence_workspaces: Mapping[str, Sequence[str | Path]],
    candidate_ids: list[str],
    seed: int,
) -> None:
    if not isinstance(evidence_workspaces, dict) or set(evidence_workspaces) != set(
        candidate_ids
    ):
        raise MissingVoiceReuseReviewError(
            "Review evidence must name every planned candidate exactly once"
        )
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise MissingVoiceReuseReviewError("Review seed must be an integer")


def _blind_candidate_labels(
    candidate_ids: list[str], seed: int
) -> tuple[list[str], dict[str, str]]:
    ordered = list(candidate_ids)
    random.Random(seed).shuffle(ordered)
    return ordered, {
        candidate_id: _opaque_label(index) for index, candidate_id in enumerate(ordered)
    }


def _review_samples_by_id(
    document: PlanDocument, target_by_id: dict[str, PlanTarget]
) -> dict[str, PlanSample]:
    samples: dict[str, PlanSample] = {}
    for sample in _plan_samples(document):
        target = target_by_id[sample["queue_id"]]
        combined: PlanSample = {
            "queue_id": sample["queue_id"],
            "cohort_id": sample["cohort_id"],
            "length_bucket": sample["length_bucket"],
        }
        for field in ("line_id", "text", "text_sha256"):
            if isinstance(target.get(field), str):
                combined[field] = target[field]
        if "portrait" in target:
            combined["portrait"] = target["portrait"]
        samples[sample["queue_id"]] = combined
    return samples


def _review_candidate_evidence(
    document: PlanDocument,
    candidates: list[PlanCandidate],
    evidence_workspaces: Mapping[str, Sequence[str | Path]],
    labels: dict[str, str],
) -> tuple[
    dict[str, dict[str, JsonObject]],
    dict[str, list[CandidateSnapshot]],
    list[JsonObject],
]:
    evidence: dict[str, dict[str, JsonObject]] = {}
    snapshots_by_candidate: dict[str, list[CandidateSnapshot]] = {}
    private_candidates: list[JsonObject] = []
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        paths = evidence_workspaces[candidate_id]
        if not isinstance(paths, (list, tuple)) or not paths:
            raise MissingVoiceReuseReviewError(
                "Every review candidate requires at least one evidence workspace"
            )
        snapshots = [
            _load_candidate_workspace(document, candidate, path) for path in paths
        ]
        snapshots_by_candidate[candidate_id] = snapshots
        evidence[candidate_id] = _candidate_sample_evidence(
            document, candidate, snapshots
        )
        private_candidates.append(
            {
                "label": labels[candidate_id],
                "candidate_id": candidate_id,
                "voice_character": candidate["voice_character"],
                "speaker": candidate["speaker"],
                "ordered_references": copy.deepcopy(candidate["ordered_references"]),
                "render_hypothesis": copy.deepcopy(candidate.get("render_hypothesis")),
                "workspaces": [snapshot["authority"] for snapshot in snapshots],
            }
        )
    return evidence, snapshots_by_candidate, private_candidates


def _publish_review_candidates(
    staging: Path,
    document: PlanDocument,
    ordered: list[str],
    labels: dict[str, str],
    evidence: dict[str, dict[str, JsonObject]],
) -> list[ReviewCandidate]:
    return [
        _publish_review_candidate(
            staging,
            document["comparison_sample_queue_ids"],
            labels[candidate_id],
            evidence[candidate_id],
        )
        for candidate_id in ordered
    ]


def _publish_review_candidate(
    staging: Path,
    queue_ids: list[str],
    label: str,
    evidence: dict[str, JsonObject],
) -> ReviewCandidate:
    samples = [
        _publish_review_arm(staging, label, queue_id, evidence[queue_id])
        for queue_id in queue_ids
    ]
    return {
        "label": label,
        "samples": samples,
        "generated_count": sum(sample["status"] == "generated" for sample in samples),
    }


def _publish_review_arm(
    staging: Path, label: str, queue_id: str, item: JsonObject
) -> ReviewArm:
    status = _text(item.get("status"), "Candidate evidence status")
    arm: ReviewArm = {
        "queue_id": queue_id,
        "status": status,
        "attempt_count": _int(item.get("attempt_count"), "Candidate attempt count"),
    }
    if status == "generated":
        relative = Path("audio") / label / f"{_queue_digest(queue_id)}.wav"
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _link_or_copy(
            Path(_text(item.get("audio_path"), "Candidate audio path")), destination
        )
        audio_sha256 = _text(item.get("audio_sha256"), "Candidate audio SHA-256")
        if sha256_file(destination) != audio_sha256:
            raise MissingVoiceReuseReviewError("Review audio changed while publishing")
        arm.update(
            {
                "audio": relative.as_posix(),
                "audio_sha256": audio_sha256,
                "quality": item.get("quality"),
                "repair_strategy": _optional_text(
                    item.get("repair_strategy"), "Candidate repair strategy"
                ),
            }
        )
    else:
        arm.update(
            {
                "failure_kind": _text(
                    item.get("failure_kind"), "Candidate failure kind"
                ),
                "failure_summary": _text(
                    item.get("failure_summary"), "Candidate failure summary"
                ),
            }
        )
    return arm


def _review_cohorts(
    document: PlanDocument,
    sample_by_id: dict[str, PlanSample],
    candidates: list[ReviewCandidate],
) -> list[ReviewCohort]:
    return [
        _review_cohort_for_plan(document, cohort, sample_by_id, candidates)
        for cohort in _objects(document["cohorts"], "Plan cohorts")
    ]


def _review_cohort_for_plan(
    document: PlanDocument,
    cohort: JsonObject,
    sample_by_id: dict[str, PlanSample],
    candidates: list[ReviewCandidate],
) -> ReviewCohort:
    cohort_id = _text(cohort.get("cohort_id"), "Plan cohort ID")
    samples = [
        copy.deepcopy(sample)
        for sample in sample_by_id.values()
        if sample["cohort_id"] == cohort_id
    ]
    samples.sort(
        key=lambda sample: document["comparison_sample_queue_ids"].index(
            sample["queue_id"]
        )
    )
    required_ids = {sample["queue_id"] for sample in samples}
    complete = [
        candidate["label"]
        for candidate in candidates
        if all(
            {sample["queue_id"]: sample["status"] for sample in candidate["samples"]}[
                queue_id
            ]
            == "generated"
            for queue_id in required_ids
        )
    ]
    return {
        "cohort_id": cohort_id,
        "sample_count": len(samples),
        "samples": samples,
        "complete_candidate_labels": complete,
        "decision_options": [*complete, "neither"],
    }


def load_missing_voice_reuse_review(
    session_path: str | Path,
) -> tuple[ReviewBundle, ReviewSession]:
    """Validate and return the immutable bundle plus mutable review session."""
    session_path = Path(session_path).expanduser().resolve()
    root = session_path.parent
    session = load_workspace_json(session_path, "missing-voice review session")
    bundle_path = root / "bundle.json"
    bundle = load_workspace_json(bundle_path, "missing-voice review bundle")
    if (
        session.get("schema") != REVIEW_SESSION_SCHEMA
        or session.get("schema_version") != REVIEW_VERSION
        or bundle.get("schema") != REVIEW_BUNDLE_SCHEMA
        or bundle.get("schema_version") != REVIEW_VERSION
    ):
        raise MissingVoiceReuseReviewError("Missing-voice review schema is unsupported")
    claimed_bundle_id = bundle.get("bundle_id")
    if claimed_bundle_id != canonical_document_sha256(
        {key: value for key, value in bundle.items() if key != "bundle_id"}
    ):
        raise MissingVoiceReuseReviewError(
            "Missing-voice review bundle identity changed"
        )
    if session.get("bundle_id") != claimed_bundle_id or session.get(
        "bundle_sha256"
    ) != sha256_file(bundle_path):
        raise MissingVoiceReuseReviewError("Missing-voice review authority changed")
    key_path = root / ".blind-key.json"
    if not private_file_is_restricted(key_path) or bundle.get(
        "blind_key_sha256"
    ) != sha256_file(key_path):
        raise MissingVoiceReuseReviewError("Missing-voice blind key changed")
    key = load_workspace_json(key_path, "missing-voice review key")
    bundle_plan = _object(bundle.get("plan"), "Missing-voice review plan")
    if (
        key.get("schema") != REVIEW_KEY_SCHEMA
        or key.get("schema_version") != REVIEW_VERSION
        or key.get("plan_id") != bundle_plan.get("plan_id")
    ):
        raise MissingVoiceReuseReviewError("Missing-voice blind key is invalid")
    validated_bundle = _validate_public_matrix(root, bundle, key)
    _derive_automatic_unresolved_decisions(session, validated_bundle)
    validated_session = _validate_review_session(session, validated_bundle)
    return copy.deepcopy(validated_bundle), copy.deepcopy(validated_session)


def record_missing_voice_reuse_heard(
    session_path: str | Path, cohort_id: str, queue_id: str, label: str
) -> ReviewSession:
    """Record that one exact generated opaque arm has started playback."""
    bundle, session = load_missing_voice_reuse_review(session_path)
    cohort = _cohort(bundle, cohort_id)
    if queue_id not in {sample["queue_id"] for sample in cohort["samples"]}:
        raise MissingVoiceReuseReviewError("Review sample is outside the cohort")
    sample = _public_sample(bundle, label, queue_id)
    if sample["status"] != "generated":
        raise MissingVoiceReuseReviewError("A failed review arm cannot be heard")
    record: HeardRecord = {"cohort_id": cohort_id, "queue_id": queue_id, "label": label}
    if record not in session["heard"]:
        session["heard"].append(record)
        session["heard"].sort(
            key=lambda value: (
                value["cohort_id"],
                value["queue_id"],
                value["label"],
            )
        )
        session["updated_at"] = _utc_now()
        atomic_write_json(Path(session_path).resolve(), session, sort_keys=True)
    return session


def record_missing_voice_reuse_decision(
    session_path: str | Path, cohort_id: str, decision: str
) -> ReviewSession:
    """Record one candidate or neither after every available arm was heard."""
    bundle, session = load_missing_voice_reuse_review(session_path)
    cohort = _cohort(bundle, cohort_id)
    if decision not in cohort["decision_options"]:
        raise MissingVoiceReuseReviewError("Review decision is not available")
    record = next(
        value for value in session["decisions"] if value["cohort_id"] == cohort_id
    )
    if record["decision"] is not None:
        raise MissingVoiceReuseReviewError("Review cohort already has a decision")
    required_heard = _available_heard_keys(bundle, cohort)
    observed = {
        (value["queue_id"], value["label"])
        for value in session["heard"]
        if value["cohort_id"] == cohort_id
    }
    if observed != required_heard:
        raise MissingVoiceReuseReviewError(
            "Every available cohort sample must be heard before deciding"
        )
    record["decision"] = decision
    record["decided_at"] = _utc_now()
    session["updated_at"] = _utc_now()
    atomic_write_json(Path(session_path).resolve(), session, sort_keys=True)
    load_missing_voice_reuse_review(session_path)
    return session


def missing_voice_reuse_review_progress(
    bundle: Mapping[str, object], session: Mapping[str, object]
) -> tuple[int, int]:
    decisions = _objects(session.get("decisions"), "Review decisions")
    cohorts = _objects(bundle.get("cohorts"), "Review cohorts")
    completed = sum(value.get("decision") is not None for value in decisions)
    return completed, len(cohorts)


def parse_missing_voice_reuse_evidence(
    values: Iterable[object] | None,
) -> dict[str, tuple[Path, ...]]:
    """Parse repeated CANDIDATE_ID=WORKSPACE arguments without dropping order."""
    evidence: dict[str, list[Path]] = {}
    for value in values or ():
        if not isinstance(value, str) or "=" not in value:
            raise MissingVoiceReuseReviewError(
                "Candidate evidence must use CANDIDATE_ID=WORKSPACE"
            )
        candidate_id, raw_path = value.split("=", 1)
        candidate_id = candidate_id.strip()
        raw_path = raw_path.strip()
        if not candidate_id or not raw_path:
            raise MissingVoiceReuseReviewError(
                "Candidate evidence must use CANDIDATE_ID=WORKSPACE"
            )
        evidence.setdefault(candidate_id, []).append(Path(raw_path))
    return {key: tuple(paths) for key, paths in evidence.items()}


def _load_candidate_workspace(
    plan: PlanDocument, candidate: PlanCandidate, workspace_directory: str | Path
) -> CandidateSnapshot:
    try:
        directory, workspace, workspace_sha256 = load_workspace_authority(
            workspace_directory
        )
        _queue, state, _state_payload, state_sha256 = (
            load_stable_workspace_generation_state(
                directory,
                workspace,
                "missing-voice candidate evidence",
                error_type=AuthoringWorkbenchError,
            )
        )
    except AuthoringWorkbenchError as error:
        raise MissingVoiceReuseReviewError(str(error)) from error
    workspace = _object(workspace, "Candidate workspace")
    state = _object(state, "Candidate generation state")
    manifest_path = directory / "inputs/voice/manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MissingVoiceReuseReviewError(str(error)) from error
    binding = manifest.get(MISSING_VOICE_REUSE_BINDING_FIELD)
    if (
        not isinstance(binding, dict)
        or binding.get("mode") != "comparison_sample_only"
        or binding.get("plan_id") != plan["plan_id"]
        or binding.get("candidate_id") != candidate["candidate_id"]
        or binding.get("candidate_voice_character") != candidate["voice_character"]
        or binding.get("candidate_render_hypothesis")
        != candidate.get("render_hypothesis")
        or set(binding.get("queue_voice_overrides", {}))
        != set(plan["comparison_sample_queue_ids"])
    ):
        raise MissingVoiceReuseReviewError(
            "Evidence workspace does not belong to the planned candidate"
        )
    return {
        "directory": directory,
        "workspace": workspace,
        "state": state,
        "authority": {
            "path": str(directory),
            "workspace_id": _text(
                workspace.get("workspace_id"), "Candidate workspace ID"
            ),
            "workspace_sha256": workspace_sha256,
            "state_sha256": state_sha256,
            "voice_manifest_sha256": sha256_file(manifest_path),
        },
    }


def _candidate_sample_evidence(
    plan: PlanDocument,
    candidate: PlanCandidate,
    snapshots: Sequence[CandidateSnapshot],
) -> dict[str, JsonObject]:
    evidence: dict[str, JsonObject] = {}
    for queue_id in plan["comparison_sample_queue_ids"]:
        outcomes = []
        for snapshot in snapshots:
            state_items = _object(
                snapshot["state"].get("items"), "Candidate generation state items"
            )
            item = state_items.get(queue_id)
            if not isinstance(item, dict) or item.get("status") not in {
                "generated",
                "failed",
            }:
                continue
            binding = item.get("source_reference_binding")
            if (
                not isinstance(binding, dict)
                or binding.get("queue_id") != queue_id
                or binding.get("synthesis_voice_character")
                != candidate["voice_character"]
            ):
                raise MissingVoiceReuseReviewError(
                    "Candidate outcome has changed synthesis voice authority"
                )
            _validate_render_hypothesis_outcome(candidate, queue_id, item)
            outcome = {
                "status": item["status"],
                "workspace": snapshot["authority"],
                "attempts": item.get("attempts", 0),
                "item_sha256": canonical_document_sha256(item),
                "provider": item.get("provider"),
                "model": item.get("model"),
                "generation_profile": item.get("generation_profile"),
                "seed": item.get("seed"),
            }
            if item["status"] == "generated":
                relative = safe_workspace_relative_path(
                    item.get("path"), "Candidate WAV path"
                )
                audio = contained_workspace_path(
                    snapshot["directory"] / "generated-audio",
                    relative,
                    "Candidate WAV",
                )
                claimed = item.get("file_sha256")
                if not audio.is_file() or sha256_file(audio) != claimed:
                    raise MissingVoiceReuseReviewError("Candidate WAV changed")
                outcome.update(
                    {
                        "audio_path": str(audio),
                        "audio_sha256": claimed,
                        "quality": copy.deepcopy(item.get("quality")),
                        "repair_strategy": (
                            item.get("failure_repair", {}).get("strategy")
                            if isinstance(item.get("failure_repair"), dict)
                            else None
                        ),
                    }
                )
            else:
                failure = item.get("failure")
                outcome.update(
                    {
                        "failure_kind": (
                            failure.get("kind")
                            if isinstance(failure, dict)
                            else "untyped_failure"
                        ),
                        "failure_summary": str(item.get("last_error") or "Failed"),
                    }
                )
            outcomes.append(outcome)
        generated = [value for value in outcomes if value["status"] == "generated"]
        audio_hashes = {value["audio_sha256"] for value in generated}
        if len(audio_hashes) > 1:
            raise MissingVoiceReuseReviewError(
                "Candidate has conflicting generated WAVs for one exact sample"
            )
        if generated:
            selected = generated[-1]
            evidence[queue_id] = {
                "status": "generated",
                "attempt_count": max(value["attempts"] for value in outcomes),
                "audio_path": selected["audio_path"],
                "audio_sha256": selected["audio_sha256"],
                "quality": selected["quality"],
                "repair_strategy": selected["repair_strategy"],
                "provider": selected["provider"],
                "model": selected["model"],
                "generation_profile": selected["generation_profile"],
                "seed": selected["seed"],
                "outcomes": outcomes,
            }
        else:
            selected_failed = outcomes[-1] if outcomes else None
            evidence[queue_id] = {
                "status": "failed",
                "attempt_count": max(
                    (value["attempts"] for value in outcomes), default=0
                ),
                "failure_kind": (
                    selected_failed["failure_kind"]
                    if selected_failed
                    else "no_render_outcome"
                ),
                "failure_summary": (
                    selected_failed["failure_summary"]
                    if selected_failed
                    else "No generated WAV was published"
                ),
                "provider": selected_failed["provider"] if selected_failed else None,
                "model": selected_failed["model"] if selected_failed else None,
                "generation_profile": (
                    selected_failed["generation_profile"] if selected_failed else None
                ),
                "seed": selected_failed["seed"] if selected_failed else None,
                "outcomes": outcomes,
            }
    return evidence


def _validate_render_hypothesis_outcome(
    candidate: PlanCandidate, queue_id: str, item: JsonObject
) -> None:
    hypothesis = candidate.get("render_hypothesis")
    if hypothesis is None:
        return
    prompts = {
        _text(prompt.get("queue_id"), "Render hypothesis queue ID"): prompt
        for prompt in _objects(hypothesis.get("prompts"), "Render hypothesis prompts")
    }
    prompt = prompts.get(queue_id)
    repair = item.get("failure_repair")
    if (
        prompt is None
        or not isinstance(repair, dict)
        or repair.get("strategy") != hypothesis.get("strategy")
        or repair.get("pause_ms") != hypothesis.get("pause_ms")
        or repair.get("marker_count") != prompt.get("marker_count")
        or repair.get("derived_prompt_sha256") != prompt.get("derived_prompt_sha256")
        or item.get("synthesis_text_sha256") != prompt.get("derived_prompt_sha256")
    ):
        raise MissingVoiceReuseReviewError(
            "Candidate outcome changed the exact render hypothesis"
        )


def _validate_public_matrix(
    root: Path, bundle: JsonObject, key: JsonObject
) -> ReviewBundle:
    candidates = _objects(bundle.get("candidates"), "Review candidates")
    cohorts = _objects(bundle.get("cohorts"), "Review cohorts")
    private_candidates = _objects(key.get("candidates"), "Private review candidates")
    plan = _object(bundle.get("plan"), "Review plan")
    context = bundle.get("decision_context")
    _validate_decision_context(context, plan)
    _validate_review_candidate_labels(bundle, candidates, private_candidates)
    queue_ids = _review_matrix_queue_ids(cohorts)
    target_mode = _validate_review_target_mode(bundle, cohorts)
    candidate_samples = _validate_review_candidate_samples(root, candidates, queue_ids)
    _validate_review_cohort_gates(bundle, cohorts, candidate_samples)
    validated: ReviewBundle = {
        "schema": _text(bundle.get("schema"), "Review bundle schema"),
        "schema_version": _int(
            bundle.get("schema_version"), "Review bundle schema version"
        ),
        "bundle_id": _text(bundle.get("bundle_id"), "Review bundle ID"),
        "plan": plan,
        "character": _text(bundle.get("character"), "Review character"),
        "decision_context": (
            None if context is None else _object(context, "Review decision context")
        ),
        "seed": _int(bundle.get("seed"), "Review seed"),
        "policy": _object(bundle.get("policy"), "Review policy"),
        "blind_key_sha256": _text(
            bundle.get("blind_key_sha256"), "Review blind key SHA-256"
        ),
        "candidate_count": _int(
            bundle.get("candidate_count"), "Review candidate count"
        ),
        "candidates": [_review_candidate(value) for value in candidates],
        "cohort_count": _int(bundle.get("cohort_count"), "Review cohort count"),
        "cohorts": [_review_cohort(value) for value in cohorts],
    }
    if target_mode == "failed":
        validated["target_mode"] = "failed"
        validated["source_control"] = _objects(
            bundle.get("source_control"), "Review source controls"
        )
    return validated


def _validate_decision_context(context: object, plan: JsonObject) -> None:
    if context is not None:
        expected = {
            "purpose",
            "game_speaker",
            "synthesis_voice",
            "reference",
            "backend",
            "model",
            "generation_profile",
            "seed",
            "controls",
            "effect",
            "technical",
        }
        technical = context.get("technical") if isinstance(context, dict) else None
        if (
            not isinstance(context, dict)
            or set(context) != expected
            or any(
                not isinstance(context.get(field), str) or not context[field].strip()
                for field in expected - {"seed", "technical"}
            )
            or not isinstance(context.get("seed"), (str, int))
            or isinstance(context.get("seed"), bool)
            or not isinstance(technical, dict)
            or set(technical) != {"plan_id", "workspace_ids"}
            or technical.get("plan_id") != plan.get("plan_id")
            or not isinstance(technical.get("workspace_ids"), list)
            or not technical["workspace_ids"]
            or any(
                not isinstance(value, str) or not value
                for value in technical["workspace_ids"]
            )
        ):
            raise MissingVoiceReuseReviewError(
                "Missing-voice review decision context is invalid"
            )


def _validate_review_candidate_labels(
    bundle: JsonObject,
    candidates: list[JsonObject],
    private_candidates: list[JsonObject],
) -> None:
    labels = [
        _text(candidate.get("label"), "Review candidate label")
        for candidate in candidates
    ]
    private_labels = [
        _text(candidate.get("label"), "Private review candidate label")
        for candidate in private_candidates
    ]
    if (
        bundle.get("candidate_count") != len(labels)
        or not labels
        or len(set(labels)) != len(labels)
        or set(labels) != set(private_labels)
    ):
        raise MissingVoiceReuseReviewError("Missing-voice candidate labels are invalid")


def _review_matrix_queue_ids(cohorts: list[JsonObject]) -> set[str]:
    return {
        _text(sample.get("queue_id"), "Review sample queue ID")
        for cohort in cohorts
        for sample in _objects(cohort.get("samples"), "Review cohort samples")
    }


def _validate_review_target_mode(bundle: JsonObject, cohorts: list[JsonObject]) -> str:
    target_mode = bundle.get("target_mode", "missing")
    if target_mode == "failed":
        controls = bundle.get("source_control")
        if (
            not isinstance(controls, list)
            or [
                value.get("queue_id")
                for value in _objects(controls, "Review source controls")
            ]
            != [
                _text(sample.get("queue_id"), "Review sample queue ID")
                for cohort in cohorts
                for sample in _objects(cohort.get("samples"), "Review cohort samples")
            ]
            or any(
                value.get("status") != "failed"
                or not value.get("failure_category")
                or not isinstance(value.get("state_item_sha256"), str)
                or len(_text(value.get("state_item_sha256"), "State item SHA-256"))
                != 64
                for value in _objects(controls, "Review source controls")
            )
        ):
            raise MissingVoiceReuseReviewError(
                "Failed-control review authority is invalid"
            )
    elif target_mode != "missing" or "source_control" in bundle:
        raise MissingVoiceReuseReviewError(
            "Missing-voice review target mode is invalid"
        )
    return target_mode


def _validate_review_candidate_samples(
    root: Path, candidates: list[JsonObject], queue_ids: set[str]
) -> dict[str, list[JsonObject]]:
    candidate_samples: dict[str, list[JsonObject]] = {}
    for candidate in candidates:
        label = _text(candidate.get("label"), "Review candidate label")
        samples = _objects(candidate.get("samples"), "Review candidate samples")
        candidate_samples[label] = samples
        if {sample.get("queue_id") for sample in samples} != queue_ids:
            raise MissingVoiceReuseReviewError(
                "Missing-voice review matrix is incomplete"
            )
        for sample in samples:
            if sample.get("status") == "generated":
                relative = safe_workspace_relative_path(
                    sample.get("audio"), "Review audio"
                )
                audio = contained_workspace_path(root, relative, "Review audio")
                if not audio.is_file() or sha256_file(audio) != sample.get(
                    "audio_sha256"
                ):
                    raise MissingVoiceReuseReviewError(
                        "Missing-voice review audio changed"
                    )
            elif sample.get("status") != "failed" or not sample.get("failure_kind"):
                raise MissingVoiceReuseReviewError(
                    "Missing-voice review outcome is invalid"
                )
    return candidate_samples


def _validate_review_cohort_gates(
    bundle: JsonObject,
    cohorts: list[JsonObject],
    candidate_samples: dict[str, list[JsonObject]],
) -> None:
    if bundle.get("cohort_count") != len(cohorts):
        raise MissingVoiceReuseReviewError("Missing-voice review cohort count changed")
    for cohort in cohorts:
        ids = {
            _text(sample.get("queue_id"), "Review sample queue ID")
            for sample in _objects(cohort.get("samples"), "Review cohort samples")
        }
        complete: list[str] = []
        for label, samples in candidate_samples.items():
            statuses = {
                _text(sample.get("queue_id"), "Review sample queue ID"): _text(
                    sample.get("status"), "Review sample status"
                )
                for sample in samples
            }
            if all(statuses[queue_id] == "generated" for queue_id in ids):
                complete.append(label)
        if cohort.get("complete_candidate_labels") != complete or cohort.get(
            "decision_options"
        ) != [*complete, "neither"]:
            raise MissingVoiceReuseReviewError(
                "Missing-voice review decision gate changed"
            )


def _validate_review_session(
    session: JsonObject, bundle: ReviewBundle
) -> ReviewSession:
    cohort_ids = [cohort["cohort_id"] for cohort in bundle["cohorts"]]
    decisions = _objects(session.get("decisions"), "Review decisions")
    if [value.get("cohort_id") for value in decisions] != cohort_ids:
        raise MissingVoiceReuseReviewError("Missing-voice review decisions are invalid")
    heard = _objects(session.get("heard"), "Review heard ledger")
    if len(heard) != len(
        {(v.get("cohort_id"), v.get("queue_id"), v.get("label")) for v in heard}
    ):
        raise MissingVoiceReuseReviewError(
            "Missing-voice review heard ledger is invalid"
        )
    _validate_heard_records(heard, bundle)
    _validate_review_decisions(decisions, heard, bundle)
    return {
        "schema": _text(session.get("schema"), "Review session schema"),
        "schema_version": _int(
            session.get("schema_version"), "Review session schema version"
        ),
        "bundle_id": _text(session.get("bundle_id"), "Review bundle ID"),
        "bundle_sha256": _text(session.get("bundle_sha256"), "Review bundle SHA-256"),
        "created_at": _text(session.get("created_at"), "Review creation time"),
        "updated_at": _text(session.get("updated_at"), "Review update time"),
        "heard": [_heard_record(value) for value in heard],
        "decisions": [_review_decision(value) for value in decisions],
    }


def _validate_heard_records(heard: list[JsonObject], bundle: ReviewBundle) -> None:
    for value in heard:
        cohort_id = _text(value.get("cohort_id"), "Heard cohort ID")
        queue_id = _text(value.get("queue_id"), "Heard queue ID")
        label = _text(value.get("label"), "Heard candidate label")
        cohort = _cohort(bundle, cohort_id)
        if queue_id not in {sample["queue_id"] for sample in cohort["samples"]}:
            raise MissingVoiceReuseReviewError("Heard sample is outside its cohort")
        if _public_sample(bundle, label, queue_id)["status"] != "generated":
            raise MissingVoiceReuseReviewError("Heard ledger names a failed arm")


def _validate_review_decisions(
    decisions: list[JsonObject], heard: list[JsonObject], bundle: ReviewBundle
) -> None:
    for value in decisions:
        decision = value.get("decision")
        if decision is None:
            if set(value) != {"cohort_id", "decision"}:
                raise MissingVoiceReuseReviewError(
                    "Pending review decision is malformed"
                )
            continue
        cohort_id = _text(value.get("cohort_id"), "Decision cohort ID")
        cohort = _cohort(bundle, cohort_id)
        if decision not in cohort["decision_options"] or not value.get("decided_at"):
            raise MissingVoiceReuseReviewError("Completed review decision is invalid")
        if value.get("decision_origin") == AUTOMATIC_UNRESOLVED_ORIGIN:
            if (
                set(value) != {"cohort_id", "decision", "decided_at", "decision_origin"}
                or decision != "neither"
                or cohort["complete_candidate_labels"]
            ):
                raise MissingVoiceReuseReviewError(
                    "Automatic unresolved review decision is invalid"
                )
            continue
        observed = {
            (
                _text(record.get("queue_id"), "Heard queue ID"),
                _text(record.get("label"), "Heard candidate label"),
            )
            for record in heard
            if record.get("cohort_id") == cohort_id
        }
        if observed != _available_heard_keys(bundle, cohort):
            raise MissingVoiceReuseReviewError("Decision lacks complete heard evidence")


def _automatic_unresolved_decision(cohort_id: str, decided_at: str) -> ReviewDecision:
    return {
        "cohort_id": cohort_id,
        "decision": "neither",
        "decided_at": decided_at,
        "decision_origin": AUTOMATIC_UNRESOLVED_ORIGIN,
    }


def _derive_automatic_unresolved_decisions(
    session: JsonObject, bundle: ReviewBundle
) -> None:
    """Project legacy pending sessions through the immutable zero-choice rule."""
    decisions = session.get("decisions")
    if not isinstance(decisions, list):
        return
    cohorts = {cohort["cohort_id"]: cohort for cohort in bundle["cohorts"]}
    for index, value in enumerate(decisions):
        if (
            not isinstance(value, dict)
            or value.get("decision") is not None
            or set(value) != {"cohort_id", "decision"}
        ):
            continue
        cohort_id = _text(value.get("cohort_id"), "Review cohort ID")
        cohort = cohorts.get(cohort_id)
        if isinstance(cohort, dict) and not cohort.get("complete_candidate_labels"):
            decisions[index] = _automatic_unresolved_decision(
                cohort_id,
                _text(session.get("created_at"), "Review creation time"),
            )


def _available_heard_keys(
    bundle: ReviewBundle, cohort: ReviewCohort
) -> set[tuple[str, str]]:
    queue_ids = {sample["queue_id"] for sample in cohort["samples"]}
    return {
        (sample["queue_id"], candidate["label"])
        for candidate in bundle["candidates"]
        for sample in candidate["samples"]
        if sample["queue_id"] in queue_ids and sample["status"] == "generated"
    }


def _public_sample(bundle: ReviewBundle, label: str, queue_id: str) -> ReviewArm:
    matches = [
        candidate
        for candidate in bundle["candidates"]
        if candidate.get("label") == label
    ]
    if len(matches) != 1:
        raise MissingVoiceReuseReviewError("Unknown opaque candidate label")
    samples = [
        sample for sample in matches[0]["samples"] if sample["queue_id"] == queue_id
    ]
    if len(samples) != 1:
        raise MissingVoiceReuseReviewError("Unknown candidate sample")
    return samples[0]


def _cohort(bundle: ReviewBundle, cohort_id: str) -> ReviewCohort:
    matches = [
        cohort for cohort in bundle["cohorts"] if cohort.get("cohort_id") == cohort_id
    ]
    if len(matches) != 1:
        raise MissingVoiceReuseReviewError("Unknown missing-voice review cohort")
    return matches[0]


def _review_arm(value: JsonObject) -> ReviewArm:
    arm: ReviewArm = {
        "queue_id": _text(value.get("queue_id"), "Review arm queue ID"),
        "status": _text(value.get("status"), "Review arm status"),
        "attempt_count": _int(value.get("attempt_count"), "Review attempt count"),
    }
    audio = value.get("audio")
    if audio is not None:
        arm["audio"] = _text(audio, "Review audio path")
        arm["audio_sha256"] = _text(value.get("audio_sha256"), "Review audio SHA-256")
        arm["quality"] = value.get("quality")
        arm["repair_strategy"] = _optional_text(
            value.get("repair_strategy"), "Review repair strategy"
        )
    failure_kind = value.get("failure_kind")
    if failure_kind is not None:
        arm["failure_kind"] = _text(failure_kind, "Review failure kind")
        arm["failure_summary"] = _text(
            value.get("failure_summary"), "Review failure summary"
        )
    return arm


def _review_candidate(value: JsonObject) -> ReviewCandidate:
    samples = _objects(value.get("samples"), "Review candidate samples")
    return {
        "label": _text(value.get("label"), "Review candidate label"),
        "samples": [_review_arm(sample) for sample in samples],
        "generated_count": _int(value.get("generated_count"), "Review generated count"),
    }


def _review_sample(value: JsonObject) -> ReviewSample:
    sample: ReviewSample = {
        "queue_id": _text(value.get("queue_id"), "Review sample queue ID"),
        "cohort_id": _text(value.get("cohort_id"), "Review sample cohort ID"),
    }
    for field in ("line_id", "text", "text_sha256"):
        field_value = value.get(field)
        if isinstance(field_value, str):
            if field == "line_id":
                sample["line_id"] = field_value
            elif field == "text":
                sample["text"] = field_value
            else:
                sample["text_sha256"] = field_value
    length_bucket = value.get("length_bucket")
    if isinstance(length_bucket, str):
        sample["length_bucket"] = length_bucket
    if "portrait" in value:
        sample["portrait"] = value["portrait"]
    return sample


def _review_cohort(value: JsonObject) -> ReviewCohort:
    return {
        "cohort_id": _text(value.get("cohort_id"), "Review cohort ID"),
        "sample_count": _int(value.get("sample_count"), "Review sample count"),
        "samples": [
            _review_sample(sample)
            for sample in _objects(value.get("samples"), "Review cohort samples")
        ],
        "complete_candidate_labels": _strings(
            value.get("complete_candidate_labels"), "Complete candidate labels"
        ),
        "decision_options": _strings(
            value.get("decision_options"), "Review decision options"
        ),
    }


def _heard_record(value: JsonObject) -> HeardRecord:
    return {
        "cohort_id": _text(value.get("cohort_id"), "Heard cohort ID"),
        "queue_id": _text(value.get("queue_id"), "Heard queue ID"),
        "label": _text(value.get("label"), "Heard candidate label"),
    }


def _review_decision(value: JsonObject) -> ReviewDecision:
    decision_value = value.get("decision")
    decision: ReviewDecision = {
        "cohort_id": _text(value.get("cohort_id"), "Decision cohort ID"),
        "decision": (
            None if decision_value is None else _text(decision_value, "Review decision")
        ),
    }
    if value.get("decided_at") is not None:
        decision["decided_at"] = _text(value.get("decided_at"), "Review decision time")
    if value.get("decision_origin") is not None:
        decision["decision_origin"] = _text(
            value.get("decision_origin"), "Review decision origin"
        )
    return decision


def _object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise MissingVoiceReuseReviewError(f"{label} is malformed")
    return {key: item for key, item in value.items()}


def _objects(value: object, label: str) -> list[JsonObject]:
    if not isinstance(value, list):
        raise MissingVoiceReuseReviewError(f"{label} is malformed")
    return [_object(item, label) for item in value]


def _strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise MissingVoiceReuseReviewError(f"{label} is malformed")
    return [_text(item, label) for item in value]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MissingVoiceReuseReviewError(f"{label} is invalid")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MissingVoiceReuseReviewError(f"{label} is invalid")
    return value


def load_workspace_json(path: str | Path, label: str) -> JsonObject:
    return load_json_object(path, label, error_type=MissingVoiceReuseReviewError)


def _write_private_json(path: str | Path, value: JsonObject) -> None:
    atomic_write_json(path, value, sort_keys=True)
    os.chmod(path, 0o600)


def _link_or_copy(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _opaque_label(index: int) -> str:
    value = index
    label = ""
    while True:
        label = chr(ord("A") + value % 26) + label
        value = value // 26 - 1
        if value < 0:
            return label


def _queue_digest(queue_id: str) -> str:
    return hashlib.sha256(queue_id.encode("utf-8")).hexdigest()[:24]


__all__ = [
    "AUTOMATIC_UNRESOLVED_ORIGIN",
    "MissingVoiceReuseReviewError",
    "build_missing_voice_reuse_review",
    "load_missing_voice_reuse_review",
    "missing_voice_reuse_review_progress",
    "parse_missing_voice_reuse_evidence",
    "record_missing_voice_reuse_decision",
    "record_missing_voice_reuse_heard",
]
