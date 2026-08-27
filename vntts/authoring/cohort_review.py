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
    REVIEW_ATTENTION_POLICY_VERSION,
    REVIEW_NOTABLE_INTERNAL_PAUSE_SECONDS,
    REVIEW_NOTABLE_SILENCE_RATIO,
    WORKSPACE_SCHEMA,
    WORKSPACE_VERSION,
    AuthoringWorkbenchError,
    _load_workspace,
    _workspace_config_fingerprint,
    inspect_workspace,
    list_review_items,
)

COHORT_REVIEW_PLAN_SCHEMA = "vntts.authoring-cohort-review-plan"
COHORT_REVIEW_PLAN_VERSION = 1
COHORT_REVIEW_POLICY_VERSION = REVIEW_ATTENTION_POLICY_VERSION
SUPPORTED_COHORT_REVIEW_POLICY_VERSIONS = frozenset({1, 2})
COHORT_REVIEW_DECISION_SCHEMA = "vntts.authoring-cohort-review-decision"
COHORT_REVIEW_DECISION_VERSION = 4
SUPPORTED_COHORT_REVIEW_DECISION_VERSIONS = frozenset({1, 2, 3, 4})
COHORT_REVIEW_DEFECT_REASONS = (
    "pause_or_pacing",
    "repetition",
    "truncation_or_missing_words",
    "pronunciation_or_wrong_words",
    "timbre_or_audio_artifact",
    "speaker_identity",
    "other_or_unclear",
    "unspecified",
)
COHORT_REVIEW_PROVENANCE_SCHEMA = "vntts.authoring-cohort-review-provenance"
COHORT_REVIEW_PROVENANCE_VERSION = 2
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
    review_status: str | None
    item_review_statuses: tuple[tuple[str, str], ...] = ()

    def to_dict(self):
        return {
            "decision_id": self.decision_id,
            "queue_ids": list(self.queue_ids),
            "review_status": self.review_status,
            "item_review_statuses": [
                {"queue_id": queue_id, "review_status": review_status}
                for queue_id, review_status in self.item_review_statuses
            ],
        }


def build_cohort_review_plan(
    workspace_directory,
    *,
    clean_samples_per_bucket=DEFAULT_CLEAN_SAMPLES_PER_BUCKET,
    queue_ids=None,
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
    selected_queue_ids = _selected_queue_ids(queue_ids)
    selected_queue_id_set = (
        set(selected_queue_ids) if selected_queue_ids is not None else None
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
    observed_selected = set()
    for item in projected:
        if item.status != "generated" or item.review_status != "pending_review":
            continue
        if (
            selected_queue_id_set is not None
            and item.queue_id not in selected_queue_id_set
        ):
            continue
        observed_selected.add(item.queue_id)
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

    if selected_queue_id_set is not None:
        missing = sorted(selected_queue_id_set - observed_selected)
        if missing:
            raise CohortReviewError(
                f"Selected cohort review items are not pending: {missing}"
            )
    policy = {
        "schema_version": COHORT_REVIEW_POLICY_VERSION,
        "clean_samples_per_bucket": clean_samples_per_bucket,
        "length_buckets": {
            "short_max_words": 6,
            "medium_max_words": 15,
        },
        "attention_rule": "all technical flags",
        "attention_thresholds": {
            "silence_ratio_at_least": REVIEW_NOTABLE_SILENCE_RATIO,
            "internal_pause_seconds_at_least": (REVIEW_NOTABLE_INTERNAL_PAUSE_SECONDS),
        },
    }
    if selected_queue_ids is not None:
        policy["selected_queue_ids"] = list(selected_queue_ids)
    body = {
        "schema": COHORT_REVIEW_PLAN_SCHEMA,
        "schema_version": COHORT_REVIEW_PLAN_VERSION,
        "policy": policy,
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
    if decision not in {"accepted", "rejected", "split", "expand"}:
        raise CohortReviewError(
            "Cohort decision must be accepted, rejected, split, or expand"
        )
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
    if decision in {"accepted", "split", "expand"} and set(reviewed) != set(sampled):
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
    target_ids = [value["queue_id"] for value in target_items]
    assessment_by_id = {value["queue_id"]: value["assessment"] for value in assessments}
    if decision == "split":
        bad_count = sum(value == "bad" for value in assessment_by_id.values())
        if bad_count == 0 or (
            set(sampled) == set(target_ids) and bad_count == len(target_ids)
        ):
            raise CohortReviewError(
                "A split cohort decision requires a marked-bad WAV and at least "
                "one acceptable or unsampled WAV"
            )
    item_review_statuses = (
        [
            {
                "queue_id": queue_id,
                "review_status": (
                    "rejected"
                    if assessment_by_id.get(queue_id) == "bad"
                    else "approved"
                    if queue_id in assessment_by_id
                    else "pending_review"
                ),
            }
            for queue_id in target_ids
        ]
        if decision == "split"
        else [
            {
                "queue_id": queue_id,
                "review_status": ("approved" if decision == "accepted" else "rejected"),
            }
            for queue_id in target_ids
        ]
        if decision in {"accepted", "rejected"}
        else []
    )
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
        "item_review_statuses": item_review_statuses,
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


def _load_bound_review_workspace(workspace_directory, plan_document):
    """Load only controls bound by one exact cohort plan.

    Pregeneration input/reference validation is intentionally absent here: it
    was part of planning and cannot affect already-generated review bytes. The
    state transaction independently rechecks queue, state, item, WAV and lease
    authority before either canonical file is replaced.
    """
    directory = Path(workspace_directory).expanduser().resolve()
    workspace_path = directory / "workspace.json"
    if workspace_path.is_symlink() or not workspace_path.is_file():
        raise CohortReviewError("Workspace document is missing or unsafe")
    try:
        workspace_payload = workspace_path.read_bytes()
        workspace = json.loads(workspace_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(
            f"Unable to read authoring workspace {workspace_path}: {error}"
        ) from error
    if not isinstance(workspace, dict):
        raise CohortReviewError("Authoring workspace must be an object")
    if (
        workspace.get("schema") != WORKSPACE_SCHEMA
        or workspace.get("schema_version") != WORKSPACE_VERSION
    ):
        raise CohortReviewError("Unsupported authoring workspace")
    if workspace.get("workspace_id") != plan_document["workspace_id"]:
        raise CohortReviewError(
            "Workspace identity changed after the cohort plan was published"
        )
    if directory.name != workspace["workspace_id"]:
        raise CohortReviewError("Workspace identity does not match its directory")
    if workspace.get("queue") != "queue.jsonl" or workspace.get("output") != (
        "generated-audio"
    ):
        raise CohortReviewError("Workspace core paths were modified")
    source = workspace.get("source")
    import_id = source.get("import_id") if isinstance(source, dict) else None
    narrator = workspace.get("narrator_character")
    run_config = workspace.get("run_config")
    if (
        not isinstance(import_id, str)
        or not import_id
        or not isinstance(narrator, str)
        or not narrator.strip()
        or not isinstance(run_config, dict)
    ):
        raise CohortReviewError("Workspace configuration is malformed")
    try:
        current_fingerprint = _workspace_config_fingerprint(
            import_id,
            workspace.get("story_index"),
            workspace.get("voice_manifest"),
            narrator.strip(),
            run_config,
            workspace.get("carry_forward"),
            workspace.get("outcome_merge"),
            workspace.get("failure_reference_binding"),
            workspace.get("terminal_conflict_merge"),
            workspace.get("config_rebase"),
        )
    except (TypeError, ValueError) as error:
        raise CohortReviewError("Workspace configuration is malformed") from error
    if (
        workspace.get("config_fingerprint") != current_fingerprint
        or current_fingerprint != plan_document["workspace_config_fingerprint"]
    ):
        raise CohortReviewError(
            "Workspace configuration changed after the cohort plan was published"
        )
    queue_path = directory / "queue.jsonl"
    output = directory / "generated-audio"
    state_path = output / "generation-state.json"
    if (
        queue_path.is_symlink()
        or not queue_path.is_file()
        or queue_path.resolve().parent != directory
    ):
        raise CohortReviewError("Workspace immutable queue is missing or unsafe")
    if (
        output.is_symlink()
        or not output.is_dir()
        or output.resolve().parent != directory
    ):
        raise CohortReviewError(
            "Workspace generated-audio directory leaves its canonical root"
        )
    if state_path.is_symlink() or not state_path.is_file():
        raise CohortReviewError("Workspace has no generation state to review")
    try:
        queue_payload = queue_path.read_bytes()
        state_payload = state_path.read_bytes()
        state = json.loads(state_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(
            f"Unable to read cohort review controls: {error}"
        ) from error
    if hashlib.sha256(queue_payload).hexdigest() != plan_document["queue_sha256"]:
        raise CohortReviewError("Cohort review queue changed before projection")
    if hashlib.sha256(state_payload).hexdigest() != plan_document["state_sha256"]:
        raise CohortReviewError("Cohort review state changed before projection")
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("items"), dict)
        or state.get("queue_sha256") != plan_document["queue_sha256"]
    ):
        raise CohortReviewError(
            "Cohort review queue identity changed before projection"
        )
    return directory, workspace, queue_path, state_path, state


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
    _directory, _workspace, queue_path, state_path, state = (
        _load_bound_review_workspace(workspace_directory, plan_document)
    )
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
        "item_review_statuses": decision_document.get("item_review_statuses", []),
    }
    item_review_statuses = decision_document.get("item_review_statuses", [])
    item_decisions = {
        value["queue_id"]: value["review_status"] for value in item_review_statuses
    }
    projection_status = decision_document["projection_review_status"]
    try:
        commits = _review_generation_cohort(
            state_path,
            queue_path,
            authorities,
            item_decisions
            if decision_document["decision"] == "split"
            else projection_status,
            provenance=provenance,
        )
    except BulkGenerationError as error:
        raise CohortReviewError(str(error)) from error
    return CohortReviewProjection(
        decision_document["decision_id"],
        tuple(commit.queue_id for commit in commits),
        projection_status,
        tuple((commit.queue_id, commit.review_status) for commit in commits),
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
    workspace, _configuration, _queue, _state_path, _state = (
        _load_bound_review_workspace(workspace_directory, plan_document)
    )
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
            queue_ids=plan_document["policy"].get("selected_queue_ids"),
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
    policy_version = policy.get("schema_version")
    if policy_version not in SUPPORTED_COHORT_REVIEW_POLICY_VERSIONS:
        raise CohortReviewError("Cohort review plan policy version is unsupported")
    if policy.get("attention_rule") != "all technical flags":
        raise CohortReviewError("Cohort review plan attention rule is invalid")
    thresholds = policy.get("attention_thresholds")
    if policy_version == 1:
        if thresholds is not None:
            raise CohortReviewError(
                "Legacy cohort review plan thresholds must be implicit"
            )
    elif thresholds != {
        "silence_ratio_at_least": REVIEW_NOTABLE_SILENCE_RATIO,
        "internal_pause_seconds_at_least": (REVIEW_NOTABLE_INTERNAL_PAUSE_SECONDS),
    }:
        raise CohortReviewError("Cohort review plan attention thresholds are invalid")
    clean_samples = policy.get("clean_samples_per_bucket")
    if (
        not isinstance(clean_samples, int)
        or isinstance(clean_samples, bool)
        or not 1 <= clean_samples <= MAX_CLEAN_SAMPLES_PER_BUCKET
    ):
        raise CohortReviewError("Cohort review plan sample count is invalid")
    _selected_queue_ids(policy.get("selected_queue_ids"))
    return plan_id, document


def _selected_queue_ids(queue_ids):
    if queue_ids is None:
        return None
    if not isinstance(queue_ids, (list, tuple)) or not queue_ids:
        raise CohortReviewError(
            "Selected cohort review queue IDs must be a non-empty list"
        )
    normalized = []
    for queue_id in queue_ids:
        queue_id = _required_text(queue_id, "Selected cohort review queue ID")
        if queue_id in normalized:
            raise CohortReviewError(
                f"Selected cohort review queue ID is duplicated: {queue_id}"
            )
        normalized.append(queue_id)
    return tuple(sorted(normalized))


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
        return [
            {"queue_id": queue_id, "assessment": "heard", "defect_reasons": []}
            for queue_id in reviewed
        ]
    if not isinstance(sample_assessments, dict):
        raise CohortReviewError("Sample assessments must be a queue-ID mapping")
    if set(sample_assessments) != set(reviewed):
        raise CohortReviewError(
            "Sample assessments must cover exactly the reviewed queue IDs"
        )
    normalized = []
    for queue_id in reviewed:
        value = sample_assessments.get(queue_id)
        if isinstance(value, str):
            assessment = value
            reasons = ["unspecified"] if value == "bad" else []
        elif isinstance(value, dict) and set(value) == {
            "assessment",
            "defect_reasons",
        }:
            assessment = value["assessment"]
            reasons = value["defect_reasons"]
        else:
            raise CohortReviewError(
                "Sample assessment must be text or an assessment/reasons object"
            )
        if assessment not in {"acceptable", "bad"}:
            raise CohortReviewError("Sample assessment must be acceptable or bad")
        if not isinstance(reasons, (list, tuple, set, frozenset)) or any(
            reason not in COHORT_REVIEW_DEFECT_REASONS for reason in reasons
        ):
            raise CohortReviewError("Sample defect reasons are unsupported")
        reasons = sorted(set(reasons))
        if assessment == "bad" and not reasons:
            raise CohortReviewError("A bad sample requires at least one defect reason")
        if assessment != "bad" and reasons:
            raise CohortReviewError("Only a bad sample may carry speech defect reasons")
        normalized.append(
            {
                "queue_id": queue_id,
                "assessment": assessment,
                "defect_reasons": reasons,
            }
        )
    return normalized


def _validated_decision_document(document):
    if not isinstance(document, dict):
        raise CohortReviewError("Cohort review decision must be an object")
    if document.get("schema") != COHORT_REVIEW_DECISION_SCHEMA:
        raise CohortReviewError("Cohort review decision schema is unsupported")
    version = document.get("schema_version")
    if version not in SUPPORTED_COHORT_REVIEW_DECISION_VERSIONS:
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
    if decision not in {"accepted", "rejected", "split", "expand"}:
        raise CohortReviewError("Cohort review decision is unsupported")
    policy = document.get("plan_policy")
    if (
        not isinstance(policy, dict)
        or policy.get("schema_version") not in SUPPORTED_COHORT_REVIEW_POLICY_VERSIONS
    ):
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
        expected_fields = {"queue_id", "assessment"}
        if version >= 2:
            expected_fields.add("defect_reasons")
            reasons = value.get("defect_reasons")
            if (
                not isinstance(reasons, list)
                or reasons != sorted(set(reasons))
                or any(reason not in COHORT_REVIEW_DEFECT_REASONS for reason in reasons)
                or (value.get("assessment") == "bad" and not reasons)
                or (value.get("assessment") != "bad" and reasons)
            ):
                raise CohortReviewError(
                    "Cohort sample assessment defect reasons are invalid"
                )
        if set(value) != expected_fields:
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
    if decision in {"accepted", "split", "expand"} and set(reviewed_ids) != set(
        sampled
    ):
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
    item_review_statuses = document.get("item_review_statuses")
    if version < 3:
        if item_review_statuses is not None:
            raise CohortReviewError(
                "Legacy cohort decision cannot contain item review statuses"
            )
    else:
        if not isinstance(item_review_statuses, list):
            raise CohortReviewError("Cohort item review statuses must be a list")
        normalized_statuses = []
        for value in item_review_statuses:
            if (
                not isinstance(value, dict)
                or set(value) != {"queue_id", "review_status"}
                or value.get("review_status")
                not in (
                    {"approved", "rejected", "pending_review"}
                    if version >= 4
                    else {"approved", "rejected"}
                )
            ):
                raise CohortReviewError("Cohort item review status is invalid")
            normalized_statuses.append(
                {
                    "queue_id": _required_text(
                        value.get("queue_id"), "Cohort item review queue ID"
                    ),
                    "review_status": value["review_status"],
                }
            )
        if decision == "expand":
            expected_statuses = []
        elif decision == "accepted":
            expected_statuses = [
                {"queue_id": queue_id, "review_status": "approved"}
                for queue_id in target_ids
            ]
        elif decision == "rejected":
            expected_statuses = [
                {"queue_id": queue_id, "review_status": "rejected"}
                for queue_id in target_ids
            ]
        else:
            if version == 3 and set(sampled) != set(target_ids):
                raise CohortReviewError(
                    "Split cohort decision cannot cover unsampled target WAVs"
                )
            assessment_by_id = {
                value["queue_id"]: value["assessment"] for value in assessments
            }
            expected_statuses = [
                {
                    "queue_id": queue_id,
                    "review_status": (
                        "rejected"
                        if assessment_by_id.get(queue_id) == "bad"
                        else "approved"
                        if queue_id in assessment_by_id
                        else "pending_review"
                    ),
                }
                for queue_id in target_ids
            ]
            projected = {value["review_status"] for value in expected_statuses}
            if version == 3 and projected != {"approved", "rejected"}:
                raise CohortReviewError(
                    "Split cohort decision requires bad and acceptable WAVs"
                )
            if version >= 4 and (
                "rejected" not in projected or projected == {"rejected"}
            ):
                raise CohortReviewError(
                    "Split cohort decision requires a marked-bad WAV and at least "
                    "one acceptable or unsampled WAV"
                )
        if normalized_statuses != expected_statuses:
            raise CohortReviewError(
                "Cohort item review statuses do not match the exact decision"
            )
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
