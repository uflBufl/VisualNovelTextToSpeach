"""Deterministic checksum-bound review plans for generated speech cohorts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    ReviewAuthority,
    _canonical_sha256,
    _review_generation_cohort,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    _load_workspace,
    inspect_workspace,
    list_review_items,
)

COHORT_REVIEW_PLAN_SCHEMA = "vntts.authoring-cohort-review-plan"
COHORT_REVIEW_PLAN_VERSION = 1
COHORT_REVIEW_POLICY_VERSION = 1
COHORT_REVIEW_DECISION_SCHEMA = "vntts.authoring-cohort-review-decision"
COHORT_REVIEW_DECISION_VERSION = 1
COHORT_REVIEW_PROVENANCE_SCHEMA = "vntts.authoring-cohort-review-provenance"
COHORT_REVIEW_PROVENANCE_VERSION = 1
DEFAULT_CLEAN_SAMPLES_PER_BUCKET = 1
MAX_CLEAN_SAMPLES_PER_BUCKET = 5
WORD_PATTERN = re.compile(r"[\w’'-]+", flags=re.UNICODE)


class CohortReviewError(RuntimeError):
    """A generated cohort cannot be represented by one safe review plan."""


@dataclass(frozen=True)
class CohortReviewPlan:
    """One immutable planning document plus its canonical identity."""

    plan_id: str
    document: dict

    def to_dict(self):
        return dict(self.document)


@dataclass(frozen=True)
class CohortReviewDecision:
    """One immutable human decision over one exact cohort plan."""

    decision_id: str
    document: dict

    def to_dict(self):
        return dict(self.document)


@dataclass(frozen=True)
class CohortReviewProjection:
    """One committed cohort decision and its exact per-item results."""

    decision_id: str
    queue_ids: tuple[str, ...]
    review_status: str

    def to_dict(self):
        return {
            "decision_id": self.decision_id,
            "queue_ids": list(self.queue_ids),
            "review_status": self.review_status,
        }


def build_cohort_review_plan(
    workspace_directory,
    *,
    clean_samples_per_bucket=DEFAULT_CLEAN_SAMPLES_PER_BUCKET,
):
    """Build a read-only exact-WAV review plan for current pending outcomes."""
    if (
        not isinstance(clean_samples_per_bucket, int)
        or isinstance(clean_samples_per_bucket, bool)
        or not 1 <= clean_samples_per_bucket <= MAX_CLEAN_SAMPLES_PER_BUCKET
    ):
        raise CohortReviewError(
            "Clean samples per length bucket must be an integer from 1 to "
            f"{MAX_CLEAN_SAMPLES_PER_BUCKET}"
        )
    try:
        directory, workspace = _load_workspace(workspace_directory)
        summary = inspect_workspace(directory)
    except AuthoringWorkbenchError as error:
        raise CohortReviewError(str(error)) from error
    if summary.state is None:
        raise CohortReviewError("Workspace has no generation state to review")
    try:
        state_payload = summary.state.read_bytes()
        state = json.loads(state_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(
            f"Unable to read generation state {summary.state}: {error}"
        ) from error
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        raise CohortReviewError("Generation state items must be an object")
    state_sha256 = hashlib.sha256(state_payload).hexdigest()
    try:
        projected = list_review_items(directory)
    except AuthoringWorkbenchError as error:
        raise CohortReviewError(str(error)) from error
    try:
        final_state_sha256 = hashlib.sha256(summary.state.read_bytes()).hexdigest()
    except OSError as error:
        raise CohortReviewError(
            f"Unable to re-read generation state {summary.state}: {error}"
        ) from error
    if final_state_sha256 != state_sha256:
        raise CohortReviewError(
            "Generation state changed while cohort review was being planned"
        )

    cohorts = {}
    blocked = []
    for item in projected:
        if item.status != "generated" or item.review_status != "pending_review":
            continue
        if item.authority is None or item.authority.state_sha256 != state_sha256:
            raise CohortReviewError(
                "Review authority changed while cohort review was being planned"
            )
        result = state["items"].get(item.queue_id)
        if not isinstance(result, dict):
            raise CohortReviewError(
                f"Generation result disappeared for {item.queue_id!r}"
            )
        try:
            identity = _cohort_identity(workspace, result)
        except CohortReviewError as error:
            blocked.append(
                {
                    "queue_id": item.queue_id,
                    "line_id": item.line_id,
                    "reason": str(error),
                }
            )
            continue
        cohort_id = _canonical_sha256(identity)
        word_count = len(WORD_PATTERN.findall(item.text))
        record = {
            "queue_id": item.queue_id,
            "line_id": item.line_id,
            "text_sha256": result.get("text_sha256"),
            "audio_sha256": item.authority.audio_sha256,
            "word_count": word_count,
            "length_bucket": _length_bucket(word_count),
            "technical_flags": list(item.technical_flags),
        }
        cohort = cohorts.setdefault(
            cohort_id,
            {
                "cohort_id": cohort_id,
                "identity": identity,
                "items": [],
            },
        )
        cohort["items"].append(record)

    planned = []
    for cohort_id in sorted(cohorts):
        cohort = cohorts[cohort_id]
        records = sorted(cohort["items"], key=lambda value: value["queue_id"])
        attention = [value for value in records if value["technical_flags"]]
        clean = [value for value in records if not value["technical_flags"]]
        sampled = {value["queue_id"] for value in attention}
        for bucket in ("short", "medium", "long"):
            eligible = [value for value in clean if value["length_bucket"] == bucket]
            eligible.sort(
                key=lambda value: (
                    hashlib.sha256(
                        f"{cohort_id}\0{value['queue_id']}".encode("utf-8")
                    ).hexdigest(),
                    value["queue_id"],
                )
            )
            sampled.update(
                value["queue_id"] for value in eligible[:clean_samples_per_bucket]
            )
        planned.append(
            {
                "cohort_id": cohort_id,
                "identity": cohort["identity"],
                "item_count": len(records),
                "attention_count": len(attention),
                "sample_queue_ids": sorted(sampled),
                "items": [
                    {**value, "sampled": value["queue_id"] in sampled}
                    for value in records
                ],
            }
        )

    body = {
        "schema": COHORT_REVIEW_PLAN_SCHEMA,
        "schema_version": COHORT_REVIEW_PLAN_VERSION,
        "policy": {
            "schema_version": COHORT_REVIEW_POLICY_VERSION,
            "clean_samples_per_bucket": clean_samples_per_bucket,
            "length_buckets": {
                "short_max_words": 6,
                "medium_max_words": 15,
            },
            "attention_rule": "all technical flags",
        },
        "workspace_id": _required_text(workspace.get("workspace_id"), "Workspace ID"),
        "workspace_config_fingerprint": _required_sha256(
            workspace.get("config_fingerprint"), "Workspace config fingerprint"
        ),
        "queue_sha256": _required_sha256(
            state.get("queue_sha256"), "Generation state queue sha256"
        ),
        "state_sha256": state_sha256,
        "cohort_count": len(planned),
        "pending_item_count": sum(value["item_count"] for value in planned),
        "sample_item_count": sum(len(value["sample_queue_ids"]) for value in planned),
        "blocked_item_count": len(blocked),
        "blocked_items": sorted(blocked, key=lambda value: value["queue_id"]),
        "cohorts": planned,
    }
    plan_id = _canonical_sha256(body)
    document = {**body, "plan_id": plan_id}
    return CohortReviewPlan(plan_id, document)


def write_cohort_review_plan(plan, output_path):
    """Publish one validated plan without replacing an existing document."""
    document = _validated_plan_document(plan)
    return _write_document_no_replace(output_path, document, "cohort review plan")


def load_cohort_review_plan(path):
    """Load and validate one exact cohort plan document."""
    return CohortReviewPlan(
        *_plan_identity_and_document(_load_document(path, "cohort review plan"))
    )


def build_cohort_review_decision(
    plan,
    cohort_id,
    decision,
    *,
    reviewed_queue_ids,
    sample_assessments=None,
    next_clean_samples_per_bucket=None,
):
    """Bind a human decision to exact sampled and projected WAV identities."""
    document = _validated_plan_document(plan)
    cohort_id = _required_sha256(cohort_id, "Cohort ID")
    if decision not in {"accepted", "rejected", "expand"}:
        raise CohortReviewError("Cohort decision must be accepted, rejected, or expand")
    cohort = next(
        (value for value in document["cohorts"] if value.get("cohort_id") == cohort_id),
        None,
    )
    if cohort is None:
        raise CohortReviewError(f"Cohort does not exist in this plan: {cohort_id}")
    if not isinstance(reviewed_queue_ids, (list, tuple)):
        raise CohortReviewError("Reviewed queue IDs must be an ordered list")
    reviewed = []
    for queue_id in reviewed_queue_ids:
        queue_id = _required_text(queue_id, "Reviewed queue ID")
        if queue_id in reviewed:
            raise CohortReviewError(f"Reviewed queue ID is duplicated: {queue_id}")
        reviewed.append(queue_id)
    sampled = cohort.get("sample_queue_ids")
    if not isinstance(sampled, list) or not sampled:
        raise CohortReviewError("Cohort has no review sample")
    unexpected = sorted(set(reviewed) - set(sampled))
    if unexpected:
        raise CohortReviewError(
            f"Reviewed queue IDs are outside the cohort sample: {unexpected}"
        )
    if decision in {"accepted", "expand"} and set(reviewed) != set(sampled):
        missing = sorted(set(sampled) - set(reviewed))
        raise CohortReviewError(
            f"Every sampled WAV must be reviewed before {decision}: {missing}"
        )
    if decision == "rejected" and not reviewed:
        raise CohortReviewError("A rejected cohort requires at least one reviewed WAV")
    assessments = _normalize_sample_assessments(reviewed, sample_assessments)
    if decision == "accepted" and any(
        value["assessment"] == "bad" for value in assessments
    ):
        raise CohortReviewError(
            "An accepted cohort cannot contain a sample marked as bad"
        )
    current_samples = document["policy"]["clean_samples_per_bucket"]
    if decision == "expand":
        if (
            not isinstance(next_clean_samples_per_bucket, int)
            or isinstance(next_clean_samples_per_bucket, bool)
            or not current_samples
            < next_clean_samples_per_bucket
            <= MAX_CLEAN_SAMPLES_PER_BUCKET
        ):
            raise CohortReviewError(
                "Expanded clean sample count must be a larger integer up to "
                f"{MAX_CLEAN_SAMPLES_PER_BUCKET}"
            )
    elif next_clean_samples_per_bucket is not None:
        raise CohortReviewError(
            "Expanded clean sample count is valid only for an expand decision"
        )
    items = cohort.get("items")
    if not isinstance(items, list):
        raise CohortReviewError("Cohort items must be a list")
    by_id = {value.get("queue_id"): value for value in items if isinstance(value, dict)}
    if len(by_id) != len(items):
        raise CohortReviewError("Cohort item queue IDs must be unique")
    reviewed_evidence = [_decision_item(by_id[queue_id]) for queue_id in reviewed]
    target_items = [_decision_item(value) for value in items]
    body = {
        "schema": COHORT_REVIEW_DECISION_SCHEMA,
        "schema_version": COHORT_REVIEW_DECISION_VERSION,
        "plan_id": document["plan_id"],
        "cohort_id": cohort_id,
        "decision": decision,
        "plan_policy": {
            "schema_version": document["policy"].get("schema_version"),
            "clean_samples_per_bucket": current_samples,
        },
        "sample_queue_ids": list(sampled),
        "reviewed_samples": reviewed_evidence,
        "sample_assessments": assessments,
        "target_items": target_items,
        "projection_review_status": (
            "approved"
            if decision == "accepted"
            else "rejected"
            if decision == "rejected"
            else None
        ),
        "next_clean_samples_per_bucket": (
            next_clean_samples_per_bucket if decision == "expand" else None
        ),
    }
    decision_id = _canonical_sha256(body)
    return CohortReviewDecision(decision_id, {**body, "decision_id": decision_id})


def write_cohort_review_decision(decision, output_path):
    """Publish one validated decision without replacing prior review evidence."""
    if isinstance(decision, CohortReviewDecision):
        document = decision.document
    elif isinstance(decision, dict):
        document = decision
    else:
        raise CohortReviewError("Cohort review decision must be a document")
    _validated_decision_document(document)
    return _write_document_no_replace(output_path, document, "cohort review decision")


def load_cohort_review_decision(path):
    """Load and validate one immutable cohort decision document."""
    document = _load_document(path, "cohort review decision")
    _validated_decision_document(document)
    return CohortReviewDecision(document["decision_id"], document)


def apply_cohort_review_decision(workspace_directory, plan, decision):
    """Project one exact terminal cohort decision in one state transaction."""
    plan_document = _validated_plan_document(plan)
    if isinstance(decision, CohortReviewDecision):
        decision_document = decision.document
    elif isinstance(decision, dict):
        decision_document = decision
    else:
        raise CohortReviewError("Cohort review decision must be a document")
    _validated_decision_document(decision_document)
    _validate_decision_against_plan(plan_document, decision_document)
    if decision_document["decision"] == "expand":
        raise CohortReviewError(
            "Expand decisions create a new plan and cannot be applied"
        )
    current = build_cohort_review_plan(
        workspace_directory,
        clean_samples_per_bucket=plan_document["policy"]["clean_samples_per_bucket"],
    )
    if current.plan_id != plan_document["plan_id"]:
        raise CohortReviewError(
            "Workspace review authority changed after the cohort plan was published"
        )
    try:
        summary = inspect_workspace(workspace_directory)
    except AuthoringWorkbenchError as error:
        raise CohortReviewError(str(error)) from error
    if summary.state is None:
        raise CohortReviewError("Workspace has no generation state to review")
    try:
        state_payload = summary.state.read_bytes()
        state = json.loads(state_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(
            f"Unable to read generation state {summary.state}: {error}"
        ) from error
    if hashlib.sha256(state_payload).hexdigest() != plan_document["state_sha256"]:
        raise CohortReviewError("Cohort review state changed before projection")
    authorities = {}
    for target in decision_document["target_items"]:
        queue_id = target["queue_id"]
        item = state.get("items", {}).get(queue_id)
        if not isinstance(item, dict):
            raise CohortReviewError(f"Cohort review item disappeared: {queue_id}")
        authorities[queue_id] = ReviewAuthority(
            queue_sha256=plan_document["queue_sha256"],
            state_sha256=plan_document["state_sha256"],
            item_sha256=_canonical_sha256(item),
            audio_sha256=target["audio_sha256"],
        )
    provenance = {
        "schema": COHORT_REVIEW_PROVENANCE_SCHEMA,
        "schema_version": COHORT_REVIEW_PROVENANCE_VERSION,
        "decision_id": decision_document["decision_id"],
        "plan_id": decision_document["plan_id"],
        "cohort_id": decision_document["cohort_id"],
        "decision": decision_document["decision"],
        "plan_policy": decision_document["plan_policy"],
        "sample_queue_ids": decision_document["sample_queue_ids"],
        "reviewed_samples": decision_document["reviewed_samples"],
        "sample_assessments": decision_document.get("sample_assessments", []),
    }
    try:
        commits = _review_generation_cohort(
            summary.state,
            summary.queue,
            authorities,
            decision_document["projection_review_status"],
            provenance=provenance,
        )
    except BulkGenerationError as error:
        raise CohortReviewError(str(error)) from error
    return CohortReviewProjection(
        decision_document["decision_id"],
        tuple(commit.queue_id for commit in commits),
        decision_document["projection_review_status"],
    )


def execute_cohort_review_decision(workspace_directory, plan, decision):
    """Persist exact evidence, then expand or project one cohort decision."""
    plan_document = _validated_plan_document(plan)
    if isinstance(decision, CohortReviewDecision):
        decision_document = decision.document
    elif isinstance(decision, dict):
        decision_document = decision
    else:
        raise CohortReviewError("Cohort review decision must be a document")
    _validated_decision_document(decision_document)
    _validate_decision_against_plan(plan_document, decision_document)
    try:
        workspace, _configuration = _load_workspace(workspace_directory)
    except AuthoringWorkbenchError as error:
        raise CohortReviewError(str(error)) from error
    evidence_directory = workspace / "cohort-reviews"
    if evidence_directory.is_symlink():
        raise CohortReviewError("Cohort review evidence directory cannot be a symlink")
    try:
        evidence_directory.mkdir(exist_ok=True)
    except OSError as error:
        raise CohortReviewError(
            f"Unable to create cohort review evidence directory: {error}"
        ) from error
    if evidence_directory.resolve() != workspace / "cohort-reviews":
        raise CohortReviewError("Cohort review evidence leaves its workspace")
    plan_path = evidence_directory / f"plan-{plan_document['plan_id']}.json"
    decision_path = evidence_directory / (
        f"decision-{decision_document['decision_id']}.json"
    )
    _write_or_validate_document(plan_path, plan_document, "cohort review plan")
    _write_or_validate_document(
        decision_path, decision_document, "cohort review decision"
    )
    if decision_document["decision"] == "expand":
        return build_cohort_review_plan(
            workspace,
            clean_samples_per_bucket=decision_document["next_clean_samples_per_bucket"],
        )
    return apply_cohort_review_decision(
        workspace,
        CohortReviewPlan(plan_document["plan_id"], plan_document),
        CohortReviewDecision(decision_document["decision_id"], decision_document),
    )


def _validate_decision_against_plan(plan_document, decision_document):
    """Validate every immutable decision identity against one exact plan."""
    if decision_document["plan_id"] != plan_document["plan_id"]:
        raise CohortReviewError("Cohort review decision belongs to a different plan")
    cohort = next(
        (
            value
            for value in plan_document["cohorts"]
            if value["cohort_id"] == decision_document["cohort_id"]
        ),
        None,
    )
    if cohort is None:
        raise CohortReviewError("Cohort decision target is absent from its plan")
    expected_targets = [_decision_item(value) for value in cohort["items"]]
    if decision_document["target_items"] != expected_targets:
        raise CohortReviewError(
            "Cohort decision target identities do not match its plan"
        )
    if decision_document["sample_queue_ids"] != cohort["sample_queue_ids"]:
        raise CohortReviewError("Cohort decision sample does not match its plan")
    expected_policy = {
        "schema_version": plan_document["policy"]["schema_version"],
        "clean_samples_per_bucket": plan_document["policy"]["clean_samples_per_bucket"],
    }
    if decision_document["plan_policy"] != expected_policy:
        raise CohortReviewError("Cohort decision policy does not match its plan")
    target_by_id = {value["queue_id"]: value for value in expected_targets}
    expected_reviewed = [
        target_by_id[value["queue_id"]]
        for value in decision_document["reviewed_samples"]
    ]
    if decision_document["reviewed_samples"] != expected_reviewed:
        raise CohortReviewError("Cohort reviewed evidence does not match its plan")
    return cohort


def _validated_plan_document(plan):
    if isinstance(plan, CohortReviewPlan):
        document = plan.document
    elif isinstance(plan, dict):
        document = plan
    else:
        raise CohortReviewError("Cohort review plan must be a document")
    _plan_identity_and_document(document)
    return document


def _plan_identity_and_document(document):
    if not isinstance(document, dict):
        raise CohortReviewError("Cohort review plan must be an object")
    if document.get("schema") != COHORT_REVIEW_PLAN_SCHEMA:
        raise CohortReviewError("Cohort review plan schema is unsupported")
    if document.get("schema_version") != COHORT_REVIEW_PLAN_VERSION:
        raise CohortReviewError("Cohort review plan version is unsupported")
    plan_id = _required_sha256(document.get("plan_id"), "Plan ID")
    actual = _canonical_sha256(
        {key: value for key, value in document.items() if key != "plan_id"}
    )
    if actual != plan_id:
        raise CohortReviewError("Cohort review plan identity is invalid")
    cohorts = document.get("cohorts")
    if not isinstance(cohorts, list):
        raise CohortReviewError("Cohort review plan cohorts must be a list")
    cohort_ids = []
    for cohort in cohorts:
        if not isinstance(cohort, dict):
            raise CohortReviewError("Cohort review plan cohort must be an object")
        cohort_ids.append(_required_sha256(cohort.get("cohort_id"), "Cohort ID"))
    if len(set(cohort_ids)) != len(cohort_ids):
        raise CohortReviewError("Cohort review plan cohort IDs must be unique")
    policy = document.get("policy")
    if not isinstance(policy, dict):
        raise CohortReviewError("Cohort review plan policy must be an object")
    if policy.get("schema_version") != COHORT_REVIEW_POLICY_VERSION:
        raise CohortReviewError("Cohort review plan policy version is unsupported")
    clean_samples = policy.get("clean_samples_per_bucket")
    if (
        not isinstance(clean_samples, int)
        or isinstance(clean_samples, bool)
        or not 1 <= clean_samples <= MAX_CLEAN_SAMPLES_PER_BUCKET
    ):
        raise CohortReviewError("Cohort review plan sample count is invalid")
    return plan_id, document


def _decision_item(item):
    if not isinstance(item, dict):
        raise CohortReviewError("Cohort decision item must be an object")
    flags = item.get("technical_flags")
    if not isinstance(flags, list) or any(
        not isinstance(value, str) or not value for value in flags
    ):
        raise CohortReviewError("Decision technical flags must be a text list")
    return {
        "queue_id": _required_text(item.get("queue_id"), "Decision queue ID"),
        "line_id": _required_text(item.get("line_id"), "Decision line ID"),
        "text_sha256": _required_sha256(
            item.get("text_sha256"), "Decision text sha256"
        ),
        "audio_sha256": _required_sha256(
            item.get("audio_sha256"), "Decision audio sha256"
        ),
        "technical_flags": list(flags),
    }


def _normalize_sample_assessments(reviewed_queue_ids, sample_assessments):
    reviewed = list(reviewed_queue_ids)
    if sample_assessments is None:
        return [{"queue_id": queue_id, "assessment": "heard"} for queue_id in reviewed]
    if not isinstance(sample_assessments, dict):
        raise CohortReviewError("Sample assessments must be a queue-ID mapping")
    if set(sample_assessments) != set(reviewed):
        raise CohortReviewError(
            "Sample assessments must cover exactly the reviewed queue IDs"
        )
    normalized = []
    for queue_id in reviewed:
        assessment = sample_assessments.get(queue_id)
        if assessment not in {"acceptable", "bad"}:
            raise CohortReviewError("Sample assessment must be acceptable or bad")
        normalized.append({"queue_id": queue_id, "assessment": assessment})
    return normalized


def _validated_decision_document(document):
    if not isinstance(document, dict):
        raise CohortReviewError("Cohort review decision must be an object")
    if document.get("schema") != COHORT_REVIEW_DECISION_SCHEMA:
        raise CohortReviewError("Cohort review decision schema is unsupported")
    if document.get("schema_version") != COHORT_REVIEW_DECISION_VERSION:
        raise CohortReviewError("Cohort review decision version is unsupported")
    claimed = _required_sha256(document.get("decision_id"), "Decision ID")
    actual = _canonical_sha256(
        {key: value for key, value in document.items() if key != "decision_id"}
    )
    if actual != claimed:
        raise CohortReviewError("Cohort review decision identity is invalid")
    _required_sha256(document.get("plan_id"), "Plan ID")
    _required_sha256(document.get("cohort_id"), "Cohort ID")
    decision = document.get("decision")
    if decision not in {"accepted", "rejected", "expand"}:
        raise CohortReviewError("Cohort review decision is unsupported")
    policy = document.get("plan_policy")
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise CohortReviewError("Cohort review decision policy is invalid")
    current_samples = policy.get("clean_samples_per_bucket")
    if (
        not isinstance(current_samples, int)
        or isinstance(current_samples, bool)
        or not 1 <= current_samples <= MAX_CLEAN_SAMPLES_PER_BUCKET
    ):
        raise CohortReviewError("Cohort review decision sample count is invalid")
    sampled = document.get("sample_queue_ids")
    if (
        not isinstance(sampled, list)
        or not sampled
        or any(not isinstance(value, str) or not value for value in sampled)
        or len(set(sampled)) != len(sampled)
    ):
        raise CohortReviewError("Cohort review decision sample IDs are invalid")
    reviewed = document.get("reviewed_samples")
    assessments = document.get("sample_assessments", [])
    targets = document.get("target_items")
    if not isinstance(reviewed, list) or not isinstance(targets, list) or not targets:
        raise CohortReviewError("Cohort review decision evidence is invalid")
    reviewed_items = [_decision_item(value) for value in reviewed]
    target_items = [_decision_item(value) for value in targets]
    reviewed_ids = [value["queue_id"] for value in reviewed_items]
    target_ids = [value["queue_id"] for value in target_items]
    if len(set(reviewed_ids)) != len(reviewed_ids) or len(set(target_ids)) != len(
        target_ids
    ):
        raise CohortReviewError("Cohort review decision item IDs must be unique")
    if not set(sampled).issubset(target_ids) or not set(reviewed_ids).issubset(sampled):
        raise CohortReviewError("Cohort review decision sample binding is invalid")
    if not isinstance(assessments, list):
        raise CohortReviewError("Cohort sample assessments must be a list")
    assessment_ids = []
    for value in assessments:
        if not isinstance(value, dict):
            raise CohortReviewError("Cohort sample assessment must be an object")
        queue_id = _required_text(
            value.get("queue_id"), "Cohort sample assessment queue ID"
        )
        if value.get("assessment") not in {"heard", "acceptable", "bad"}:
            raise CohortReviewError("Cohort sample assessment is unsupported")
        if set(value) != {"queue_id", "assessment"}:
            raise CohortReviewError("Cohort sample assessment fields are invalid")
        assessment_ids.append(queue_id)
    if assessment_ids and assessment_ids != reviewed_ids:
        raise CohortReviewError(
            "Cohort sample assessments do not match reviewed evidence"
        )
    if decision == "accepted" and any(
        value["assessment"] == "bad" for value in assessments
    ):
        raise CohortReviewError("Accepted cohort contains a bad sample assessment")
    if decision in {"accepted", "expand"} and set(reviewed_ids) != set(sampled):
        raise CohortReviewError("Cohort review decision is missing reviewed samples")
    if decision == "rejected" and not reviewed_ids:
        raise CohortReviewError("Rejected cohort decision has no reviewed evidence")
    expected_projection = (
        "approved"
        if decision == "accepted"
        else "rejected"
        if decision == "rejected"
        else None
    )
    if document.get("projection_review_status") != expected_projection:
        raise CohortReviewError("Cohort review projection status is invalid")
    next_samples = document.get("next_clean_samples_per_bucket")
    if decision == "expand":
        if (
            not isinstance(next_samples, int)
            or isinstance(next_samples, bool)
            or not current_samples < next_samples <= MAX_CLEAN_SAMPLES_PER_BUCKET
        ):
            raise CohortReviewError("Expanded cohort sample count is invalid")
    elif next_samples is not None:
        raise CohortReviewError("Terminal cohort decision cannot expand its sample")
    return document


def _load_document(path, label):
    path = Path(path).expanduser().resolve()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(f"Unable to read {label} {path}: {error}") from error


def _write_document_no_replace(output_path, document, label):
    path = Path(output_path).expanduser().resolve()
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as error:
        raise CohortReviewError(f"{label.title()} output exists: {path}") from error
    except OSError as error:
        raise CohortReviewError(f"Unable to publish {label} {path}: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _write_or_validate_document(path, document, label):
    if path.is_symlink():
        raise CohortReviewError(f"{label.title()} output cannot be a symlink: {path}")
    if path.exists():
        current = _load_document(path, label)
        if current != document:
            raise CohortReviewError(
                f"Existing {label} does not match its immutable identity: {path}"
            )
        return path
    try:
        return _write_document_no_replace(path, document, label)
    except CohortReviewError:
        if not path.is_symlink() and path.is_file():
            current = _load_document(path, label)
            if current == document:
                return path
        raise


def _cohort_identity(workspace, result):
    binding = result.get("source_reference_binding")
    if binding is not None:
        if not isinstance(binding, dict):
            raise CohortReviewError("Source-reference binding must be an object")
        if binding.get("schema_version") != 1:
            raise CohortReviewError("Source-reference binding version is unsupported")
        _required_text(
            binding.get("source_voice_character"),
            "Source-reference source voice character",
        )
        _required_text(
            binding.get("synthesis_voice_character"),
            "Source-reference synthesis voice character",
        )
        _required_sha256(
            binding.get("queue_voice_overrides_sha256"),
            "Source-reference queue overrides sha256",
        )
        binding = {
            key: value for key, value in binding.items() if key not in {"queue_id"}
        }
    repair = result.get("failure_repair")
    repair_strategy = repair.get("strategy") if isinstance(repair, dict) else None
    if repair_strategy is not None:
        _required_text(repair_strategy, "Failure-repair strategy")
    text_transform = result.get("text_transform")
    if text_transform is not None:
        _required_text(text_transform, "Text transform")
    return {
        "workspace_config_fingerprint": _required_sha256(
            workspace.get("config_fingerprint"), "Workspace config fingerprint"
        ),
        "provider": _required_text(result.get("provider"), "Generation provider"),
        "model": _required_text(result.get("model"), "Generation model"),
        "generation_profile": _required_text(
            result.get("generation_profile"), "Generation profile"
        ),
        "voice_character": _required_text(
            result.get("voice_character"), "Synthesis voice character"
        ),
        "synthesis_provenance_sha256": _required_sha256(
            result.get("synthesis_provenance_sha256"),
            "Synthesis provenance sha256",
        ),
        "prompt_sha256": _required_sha256(
            result.get("prompt_sha256"), "Synthesis prompt sha256"
        ),
        "prompt_applied": _required_bool(
            result.get("prompt_applied"), "Prompt-applied marker"
        ),
        "seed": _required_integer(result.get("seed"), "Generation seed"),
        "text_transform": text_transform,
        "repair_strategy": repair_strategy,
        "source_reference_binding": binding,
    }


def _length_bucket(word_count):
    if word_count <= 6:
        return "short"
    if word_count <= 15:
        return "medium"
    return "long"


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CohortReviewError(f"{label} must be non-empty text")
    return value


def _required_sha256(value, label):
    value = _required_text(value, label)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise CohortReviewError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _required_integer(value, label):
    if not isinstance(value, int) or isinstance(value, bool):
        raise CohortReviewError(f"{label} must be an integer")
    return value


def _required_bool(value, label):
    if not isinstance(value, bool):
        raise CohortReviewError(f"{label} must be a boolean")
    return value
