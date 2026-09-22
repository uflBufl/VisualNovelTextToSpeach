"""Deterministic checksum-bound review plans for generated speech cohorts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from vntts.authoring.authority import (
    canonical_document_sha256,
    write_json_document_no_replace,
)
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    ReviewAuthority,
    review_generation_cohort,
)
from vntts.authoring.workbench import (
    REVIEW_ATTENTION_POLICY_VERSION,
    REVIEW_NOTABLE_INTERNAL_PAUSE_SECONDS,
    REVIEW_NOTABLE_SILENCE_RATIO,
    WORKSPACE_SCHEMA,
    WORKSPACE_VERSION,
    AuthoringWorkbenchError,
    ReviewItem,
    inspect_workspace,
    list_review_items,
    load_workspace_authority,
)
from vntts.authoring.workspace_config import workspace_config_fingerprint
from vntts.document_identity import is_lowercase_sha256

COHORT_REVIEW_PLAN_SCHEMA = "vntts.authoring-cohort-review-plan"
COHORT_REVIEW_PLAN_VERSION = 1
COHORT_REVIEW_POLICY_VERSION = REVIEW_ATTENTION_POLICY_VERSION
SUPPORTED_COHORT_REVIEW_POLICY_VERSIONS = frozenset({1, 2, 3})
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

JsonObject: TypeAlias = dict[str, object]


class CohortReviewError(RuntimeError):
    """A generated cohort cannot be represented by one safe review plan."""


@dataclass(frozen=True)
class CohortReviewPlan:
    """One immutable planning document plus its canonical identity."""

    plan_id: str
    document: JsonObject

    def to_dict(self) -> JsonObject:
        return dict(self.document)


@dataclass(frozen=True)
class CohortReviewDecision:
    """One immutable human decision over one exact cohort plan."""

    decision_id: str
    document: JsonObject

    def to_dict(self) -> JsonObject:
        return dict(self.document)


@dataclass(frozen=True)
class CohortReviewProjection:
    """One committed cohort decision and its exact per-item results."""

    decision_id: str
    queue_ids: tuple[str, ...]
    review_status: str | None
    item_review_statuses: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> JsonObject:
        return {
            "decision_id": self.decision_id,
            "queue_ids": list(self.queue_ids),
            "review_status": self.review_status,
            "item_review_statuses": [
                {"queue_id": queue_id, "review_status": review_status}
                for queue_id, review_status in self.item_review_statuses
            ],
        }


@dataclass(frozen=True)
class _PlanReviewSource:
    """The stable workspace snapshot consumed by plan construction."""

    directory: Path
    workspace: JsonObject
    state: JsonObject
    state_items: JsonObject
    state_sha256: str
    projected: tuple[ReviewItem, ...]


@dataclass(frozen=True)
class _DecisionEvidence:
    """The plan evidence copied into a checksum-bound decision."""

    reviewed: list[str]
    sampled: list[object]
    reviewed_items: list[JsonObject]
    target_items: list[JsonObject]
    target_ids: list[str]


@dataclass(frozen=True)
class _DecisionDocumentEvidence:
    """Validated decision bindings before assessment and projection checks."""

    sampled: list[object]
    reviewed_ids: list[str]
    target_ids: list[str]
    assessments: list[object]


def build_cohort_review_plan(
    workspace_directory: str | Path,
    *,
    clean_samples_per_bucket: int = DEFAULT_CLEAN_SAMPLES_PER_BUCKET,
    queue_ids: Iterable[str] | None = None,
) -> CohortReviewPlan:
    """Build a read-only exact-WAV review plan for current pending outcomes."""
    _validate_clean_samples_per_bucket(clean_samples_per_bucket)
    selected_queue_ids = _selected_queue_ids(queue_ids)
    selected_queue_id_set = (
        set(selected_queue_ids) if selected_queue_ids is not None else None
    )
    source = _load_plan_review_source(workspace_directory)
    cohorts, blocked, observed_selected = _collect_plan_cohorts(
        source, selected_queue_id_set
    )
    planned = _planned_cohorts(cohorts, clean_samples_per_bucket)
    _validate_selected_plan_items(selected_queue_id_set, observed_selected)
    document = _plan_document(
        source.workspace,
        source.state,
        source.state_sha256,
        planned,
        blocked,
        clean_samples_per_bucket,
        selected_queue_ids,
    )
    plan_id = canonical_document_sha256(document)
    return CohortReviewPlan(plan_id, {**document, "plan_id": plan_id})


def _validate_clean_samples_per_bucket(clean_samples_per_bucket: int) -> None:
    if (
        not isinstance(clean_samples_per_bucket, int)
        or isinstance(clean_samples_per_bucket, bool)
        or not 1 <= clean_samples_per_bucket <= MAX_CLEAN_SAMPLES_PER_BUCKET
    ):
        raise CohortReviewError(
            "Clean samples per length bucket must be an integer from 1 to "
            f"{MAX_CLEAN_SAMPLES_PER_BUCKET}"
        )


def _load_plan_review_source(workspace_directory: str | Path) -> _PlanReviewSource:
    try:
        directory, workspace, _workspace_sha256 = load_workspace_authority(
            workspace_directory
        )
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
    return _PlanReviewSource(
        directory, workspace, state, state["items"], state_sha256, tuple(projected)
    )


def _collect_plan_cohorts(
    source: _PlanReviewSource, selected_queue_id_set: set[str] | None
) -> tuple[dict[str, JsonObject], list[JsonObject], set[str]]:
    cohorts: dict[str, JsonObject] = {}
    blocked: list[JsonObject] = []
    observed_selected: set[str] = set()
    for item in source.projected:
        if item.status != "generated" or item.review_status != "pending_review":
            continue
        if (
            selected_queue_id_set is not None
            and item.queue_id not in selected_queue_id_set
        ):
            continue
        observed_selected.add(item.queue_id)
        authority = _plan_item_authority(item, source.state_sha256)
        result = source.state_items.get(item.queue_id)
        if not isinstance(result, dict):
            raise CohortReviewError(
                f"Generation result disappeared for {item.queue_id!r}"
            )
        try:
            identity = _cohort_identity(source.workspace, result)
        except CohortReviewError as error:
            blocked.append(
                {
                    "queue_id": item.queue_id,
                    "line_id": item.line_id,
                    "reason": str(error),
                }
            )
            continue
        cohort_id = canonical_document_sha256(identity)
        cohort = cohorts.setdefault(
            cohort_id,
            {"cohort_id": cohort_id, "identity": identity, "items": []},
        )
        items = cohort.get("items")
        if not isinstance(items, list):
            raise CohortReviewError("Cohort items must be a list")
        items.append(_plan_item_record(item, result, authority))
    return cohorts, blocked, observed_selected


def _plan_item_authority(item: ReviewItem, state_sha256: str) -> ReviewAuthority:
    if item.authority is None or item.authority.state_sha256 != state_sha256:
        raise CohortReviewError(
            "Review authority changed while cohort review was being planned"
        )
    return item.authority


def _plan_item_record(
    item: ReviewItem, result: JsonObject, authority: ReviewAuthority
) -> JsonObject:
    word_count = len(WORD_PATTERN.findall(item.text))
    return {
        "queue_id": item.queue_id,
        "line_id": item.line_id,
        "text_sha256": result.get("text_sha256"),
        "audio_sha256": authority.audio_sha256,
        "word_count": word_count,
        "length_bucket": _length_bucket(word_count),
        "technical_flags": list(item.technical_flags),
        "words_per_minute": (
            None if item.words_per_minute is None else round(item.words_per_minute, 3)
        ),
        "pace_baseline_wpm": (
            None if item.pace_baseline_wpm is None else round(item.pace_baseline_wpm, 3)
        ),
        "pace_ratio": None if item.pace_ratio is None else round(item.pace_ratio, 4),
        "pace_baseline_scope": item.pace_baseline_scope,
        "pace_advisories": list(item.pace_advisories),
    }


def _planned_cohorts(
    cohorts: Mapping[str, JsonObject], clean_samples_per_bucket: int
) -> list[JsonObject]:
    planned: list[JsonObject] = []
    for cohort_id in sorted(cohorts):
        cohort = cohorts[cohort_id]
        items = cohort.get("items")
        if not isinstance(items, list) or not all(
            isinstance(value, dict) for value in items
        ):
            raise CohortReviewError("Cohort items must be a list of objects")
        records = sorted(
            items, key=lambda value: _required_text(value.get("queue_id"), "Queue ID")
        )
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
    return planned


def _validate_selected_plan_items(
    selected_queue_id_set: set[str] | None, observed_selected: set[str]
) -> None:
    if selected_queue_id_set is not None:
        missing = sorted(selected_queue_id_set - observed_selected)
        if missing:
            raise CohortReviewError(
                f"Selected cohort review items are not pending: {missing}"
            )


def _plan_document(
    workspace: JsonObject,
    state: JsonObject,
    state_sha256: str,
    planned: Sequence[JsonObject],
    blocked: Sequence[JsonObject],
    clean_samples_per_bucket: int,
    selected_queue_ids: tuple[str, ...] | None,
) -> JsonObject:
    policy: JsonObject = {
        "schema_version": COHORT_REVIEW_POLICY_VERSION,
        "clean_samples_per_bucket": clean_samples_per_bucket,
        "length_buckets": {"short_max_words": 6, "medium_max_words": 15},
        "attention_rule": "all technical flags",
        "attention_thresholds": {
            "silence_ratio_at_least": REVIEW_NOTABLE_SILENCE_RATIO,
            "internal_pause_seconds_at_least": REVIEW_NOTABLE_INTERNAL_PAUSE_SECONDS,
        },
    }
    if selected_queue_ids is not None:
        policy["selected_queue_ids"] = list(selected_queue_ids)
    return {
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
        "pending_item_count": sum(
            _required_integer(value.get("item_count"), "Item count")
            for value in planned
        ),
        "sample_item_count": sum(
            len(_string_list(value.get("sample_queue_ids"))) for value in planned
        ),
        "blocked_item_count": len(blocked),
        "blocked_items": sorted(
            blocked, key=lambda value: _required_text(value.get("queue_id"), "Queue ID")
        ),
        "cohorts": list(planned),
    }


def write_cohort_review_plan(
    plan: CohortReviewPlan | Mapping[str, object], output_path: str | Path
) -> Path:
    """Publish one validated plan without replacing an existing document."""
    document = _validated_plan_document(plan)
    return _write_document_no_replace(output_path, document, "cohort review plan")


def load_cohort_review_plan(path: str | Path) -> CohortReviewPlan:
    """Load and validate one exact cohort plan document."""
    return CohortReviewPlan(
        *_plan_identity_and_document(_load_document(path, "cohort review plan"))
    )


def build_cohort_review_decision(
    plan: CohortReviewPlan | Mapping[str, object],
    cohort_id: str,
    decision: str,
    *,
    reviewed_queue_ids: Sequence[str],
    sample_assessments: Mapping[str, object] | None = None,
    next_clean_samples_per_bucket: int | None = None,
) -> CohortReviewDecision:
    """Bind a human decision to exact sampled and projected WAV identities."""
    document = _validated_plan_document(plan)
    cohort_id = _required_sha256(cohort_id, "Cohort ID")
    _validate_build_decision(decision)
    cohort = _decision_cohort(document, cohort_id)
    reviewed = _reviewed_queue_ids(reviewed_queue_ids)
    sampled = _validate_decision_reviewed(decision, cohort, reviewed)
    assessments = _normalize_sample_assessments(reviewed, sample_assessments)
    _validate_accepted_assessments(decision, assessments)
    policy = _object(document.get("policy"))
    current_samples = _required_integer(
        policy.get("clean_samples_per_bucket"), "Sample count"
    )
    _validate_next_clean_samples(
        decision, current_samples, next_clean_samples_per_bucket
    )
    evidence = _decision_evidence(cohort, reviewed, sampled)
    assessment_by_id = _assessment_by_id(assessments)
    _validate_split_assessments(
        decision, sampled, evidence.target_ids, assessment_by_id
    )
    item_review_statuses = _decision_item_review_statuses(
        decision, evidence.target_ids, assessment_by_id
    )
    body = _decision_document(
        document,
        cohort_id,
        decision,
        policy,
        current_samples,
        evidence,
        assessments,
        item_review_statuses,
        next_clean_samples_per_bucket,
    )
    decision_id = canonical_document_sha256(body)
    return CohortReviewDecision(decision_id, {**body, "decision_id": decision_id})


def _validate_build_decision(decision: str) -> None:
    if decision not in {"accepted", "rejected", "split", "expand"}:
        raise CohortReviewError(
            "Cohort decision must be accepted, rejected, split, or expand"
        )


def _decision_cohort(document: JsonObject, cohort_id: str) -> JsonObject:
    cohort = next(
        (
            value
            for value in _object_list(document.get("cohorts"))
            if value.get("cohort_id") == cohort_id
        ),
        None,
    )
    if cohort is None:
        raise CohortReviewError(f"Cohort does not exist in this plan: {cohort_id}")
    return cohort


def _reviewed_queue_ids(reviewed_queue_ids: Sequence[str]) -> list[str]:
    if not isinstance(reviewed_queue_ids, (list, tuple)):
        raise CohortReviewError("Reviewed queue IDs must be an ordered list")
    reviewed: list[str] = []
    for queue_id in reviewed_queue_ids:
        queue_id = _required_text(queue_id, "Reviewed queue ID")
        if queue_id in reviewed:
            raise CohortReviewError(f"Reviewed queue ID is duplicated: {queue_id}")
        reviewed.append(queue_id)
    return reviewed


def _validate_decision_reviewed(
    decision: str, cohort: JsonObject, reviewed: Sequence[str]
) -> list[object]:
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
    return sampled


def _validate_accepted_assessments(
    decision: str, assessments: Sequence[JsonObject]
) -> None:
    if decision == "accepted" and any(
        value["assessment"] == "bad" for value in assessments
    ):
        raise CohortReviewError(
            "An accepted cohort cannot contain a sample marked as bad"
        )


def _validate_next_clean_samples(
    decision: str, current_samples: int, next_clean_samples_per_bucket: int | None
) -> None:
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


def _decision_evidence(
    cohort: JsonObject, reviewed: list[str], sampled: list[object]
) -> _DecisionEvidence:
    items = cohort.get("items")
    if not isinstance(items, list):
        raise CohortReviewError("Cohort items must be a list")
    by_id = {value.get("queue_id"): value for value in items if isinstance(value, dict)}
    if len(by_id) != len(items):
        raise CohortReviewError("Cohort item queue IDs must be unique")
    reviewed_evidence = [_decision_item(by_id[queue_id]) for queue_id in reviewed]
    target_items = [_decision_item(value) for value in items]
    target_ids = [
        _required_text(value["queue_id"], "Decision queue ID") for value in target_items
    ]
    return _DecisionEvidence(
        reviewed, sampled, reviewed_evidence, target_items, target_ids
    )


def _assessment_by_id(assessments: Sequence[JsonObject]) -> dict[str, str]:
    return {
        _required_text(value["queue_id"], "Reviewed queue ID"): _required_text(
            value["assessment"], "Sample assessment"
        )
        for value in assessments
    }


def _validate_split_assessments(
    decision: str,
    sampled: Sequence[object],
    target_ids: Sequence[str],
    assessment_by_id: Mapping[str, object],
) -> None:
    if decision == "split":
        bad_count = sum(value == "bad" for value in assessment_by_id.values())
        if bad_count == 0 or (
            set(sampled) == set(target_ids) and bad_count == len(target_ids)
        ):
            raise CohortReviewError(
                "A split cohort decision requires a marked-bad WAV and at least "
                "one acceptable or unsampled WAV"
            )


def _decision_item_review_statuses(
    decision: str,
    target_ids: Sequence[str],
    assessment_by_id: Mapping[str, object],
) -> list[JsonObject]:
    if decision == "split":
        return _split_item_review_statuses(target_ids, assessment_by_id)
    if decision in {"accepted", "rejected"}:
        review_status = "approved" if decision == "accepted" else "rejected"
        return [
            {"queue_id": queue_id, "review_status": review_status}
            for queue_id in target_ids
        ]
    return []


def _split_item_review_statuses(
    target_ids: Sequence[str], assessment_by_id: Mapping[str, object]
) -> list[JsonObject]:
    return [
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


def _decision_document(
    document: JsonObject,
    cohort_id: str,
    decision: str,
    policy: JsonObject,
    current_samples: int,
    evidence: _DecisionEvidence,
    assessments: Sequence[JsonObject],
    item_review_statuses: Sequence[JsonObject],
    next_clean_samples_per_bucket: int | None,
) -> JsonObject:
    return {
        "schema": COHORT_REVIEW_DECISION_SCHEMA,
        "schema_version": COHORT_REVIEW_DECISION_VERSION,
        "plan_id": document["plan_id"],
        "cohort_id": cohort_id,
        "decision": decision,
        "plan_policy": {
            "schema_version": policy.get("schema_version"),
            "clean_samples_per_bucket": current_samples,
        },
        "sample_queue_ids": list(evidence.sampled),
        "reviewed_samples": evidence.reviewed_items,
        "sample_assessments": list(assessments),
        "target_items": evidence.target_items,
        "item_review_statuses": list(item_review_statuses),
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


def write_cohort_review_decision(
    decision: CohortReviewDecision | Mapping[str, object], output_path: str | Path
) -> Path:
    """Publish one validated decision without replacing prior review evidence."""
    if isinstance(decision, CohortReviewDecision):
        document = decision.document
    elif isinstance(decision, Mapping):
        document = dict(decision)
    else:
        raise CohortReviewError("Cohort review decision must be a document")
    _validated_decision_document(document)
    return _write_document_no_replace(output_path, document, "cohort review decision")


def load_cohort_review_decision(path: str | Path) -> CohortReviewDecision:
    """Load and validate one immutable cohort decision document."""
    document = _load_document(path, "cohort review decision")
    _validated_decision_document(document)
    return CohortReviewDecision(
        _required_text(document.get("decision_id"), "Decision ID"), document
    )


def _load_bound_review_workspace(
    workspace_directory: str | Path, plan_document: JsonObject
) -> tuple[Path, JsonObject, Path, Path, JsonObject]:
    """Load only controls bound by one exact cohort plan.

    Pregeneration input/reference validation is intentionally absent here: it
    was part of planning and cannot affect already-generated review bytes. The
    state transaction independently rechecks queue, state, item, WAV and lease
    authority before either canonical file is replaced.
    """
    directory = Path(workspace_directory).expanduser().resolve()
    workspace = _bound_workspace_document(directory)
    _validate_bound_workspace_identity(directory, workspace, plan_document)
    _validate_bound_workspace_fingerprint(workspace, plan_document)
    queue_path, state_path = _bound_workspace_paths(directory)
    queue_payload, state_payload, state = _bound_workspace_controls(
        queue_path, state_path
    )
    _validate_bound_control_identity(queue_payload, state_payload, state, plan_document)
    return directory, workspace, queue_path, state_path, state


def _bound_workspace_document(directory: Path) -> JsonObject:
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
    return workspace


def _validate_bound_workspace_identity(
    directory: Path, workspace: JsonObject, plan_document: JsonObject
) -> None:
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


def _validate_bound_workspace_fingerprint(
    workspace: JsonObject, plan_document: JsonObject
) -> None:
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
        current_fingerprint = workspace_config_fingerprint(
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
            workspace.get("audio_event_composition"),
            workspace.get("explicit_fallback_merge"),
            workspace.get("known_role_live_fallback"),
            workspace.get("audio_event_omission"),
            workspace.get("audio_event_projection_fallback"),
            workspace.get("reviewed_waveform_publication"),
            workspace.get("reviewed_rejection_live_fallback"),
            queue_extension=workspace.get("queue_extension"),
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


def _bound_workspace_paths(directory: Path) -> tuple[Path, Path]:
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
    return queue_path, state_path


def _bound_workspace_controls(
    queue_path: Path, state_path: Path
) -> tuple[bytes, bytes, JsonObject]:
    try:
        queue_payload = queue_path.read_bytes()
        state_payload = state_path.read_bytes()
        state = json.loads(state_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(
            f"Unable to read cohort review controls: {error}"
        ) from error
    return queue_payload, state_payload, state


def _validate_bound_control_identity(
    queue_payload: bytes,
    state_payload: bytes,
    state: JsonObject,
    plan_document: JsonObject,
) -> None:
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


def apply_cohort_review_decision(
    workspace_directory: str | Path,
    plan: CohortReviewPlan | Mapping[str, object],
    decision: CohortReviewDecision | Mapping[str, object],
) -> CohortReviewProjection:
    """Project one exact terminal cohort decision in one state transaction."""
    plan_document = _validated_plan_document(plan)
    if isinstance(decision, CohortReviewDecision):
        decision_document = decision.document
    elif isinstance(decision, Mapping):
        decision_document = dict(decision)
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
    for target in _object_list(decision_document.get("target_items")):
        queue_id = target["queue_id"]
        item = _object(state.get("items")).get(_required_text(queue_id, "Queue ID"))
        if not isinstance(item, dict):
            raise CohortReviewError(f"Cohort review item disappeared: {queue_id}")
        authorities[queue_id] = ReviewAuthority(
            queue_sha256=_required_sha256(
                plan_document.get("queue_sha256"), "Queue SHA-256"
            ),
            state_sha256=_required_sha256(
                plan_document.get("state_sha256"), "State SHA-256"
            ),
            item_sha256=canonical_document_sha256(item),
            audio_sha256=_required_sha256(target.get("audio_sha256"), "Audio SHA-256"),
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
    item_review_statuses = _object_list(
        decision_document.get("item_review_statuses", [])
    )
    item_decisions = {
        value["queue_id"]: value["review_status"] for value in item_review_statuses
    }
    projection_status = decision_document["projection_review_status"]
    try:
        commits = review_generation_cohort(
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
        _required_text(decision_document.get("decision_id"), "Decision ID"),
        tuple(commit.queue_id for commit in commits),
        projection_status if isinstance(projection_status, str) else None,
        tuple((commit.queue_id, commit.review_status) for commit in commits),
    )


def execute_cohort_review_decision(
    workspace_directory: str | Path,
    plan: CohortReviewPlan | Mapping[str, object],
    decision: CohortReviewDecision | Mapping[str, object],
) -> CohortReviewPlan | CohortReviewProjection:
    """Persist exact evidence, then expand or project one cohort decision."""
    plan_document = _validated_plan_document(plan)
    if isinstance(decision, CohortReviewDecision):
        decision_document = decision.document
    elif isinstance(decision, Mapping):
        decision_document = dict(decision)
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
            clean_samples_per_bucket=_required_integer(
                decision_document.get("next_clean_samples_per_bucket"), "Sample count"
            ),
            queue_ids=_selected_queue_ids(
                _object(plan_document.get("policy")).get("selected_queue_ids")
            ),
        )
    return apply_cohort_review_decision(
        workspace,
        CohortReviewPlan(
            _required_text(plan_document.get("plan_id"), "Plan ID"), plan_document
        ),
        CohortReviewDecision(
            _required_text(decision_document.get("decision_id"), "Decision ID"),
            decision_document,
        ),
    )


def _validate_decision_against_plan(
    plan_document: Mapping[str, object], decision_document: Mapping[str, object]
) -> JsonObject:
    """Validate every immutable decision identity against one exact plan."""
    if decision_document["plan_id"] != plan_document["plan_id"]:
        raise CohortReviewError("Cohort review decision belongs to a different plan")
    cohort = next(
        (
            value
            for value in _object_list(plan_document.get("cohorts"))
            if value["cohort_id"] == decision_document["cohort_id"]
        ),
        None,
    )
    if cohort is None:
        raise CohortReviewError("Cohort decision target is absent from its plan")
    expected_targets = [
        _decision_item(value) for value in _object_list(cohort.get("items"))
    ]
    if decision_document["target_items"] != expected_targets:
        raise CohortReviewError(
            "Cohort decision target identities do not match its plan"
        )
    if decision_document["sample_queue_ids"] != cohort["sample_queue_ids"]:
        raise CohortReviewError("Cohort decision sample does not match its plan")
    plan_policy = _object(plan_document.get("policy"))
    expected_policy = {
        "schema_version": plan_policy.get("schema_version"),
        "clean_samples_per_bucket": plan_policy.get("clean_samples_per_bucket"),
    }
    if decision_document["plan_policy"] != expected_policy:
        raise CohortReviewError("Cohort decision policy does not match its plan")
    target_by_id = {
        _required_text(value.get("queue_id"), "Queue ID"): value
        for value in expected_targets
    }
    expected_reviewed = [
        target_by_id[_required_text(value.get("queue_id"), "Queue ID")]
        for value in _object_list(decision_document.get("reviewed_samples"))
    ]
    if decision_document["reviewed_samples"] != expected_reviewed:
        raise CohortReviewError("Cohort reviewed evidence does not match its plan")
    return cohort


def _validated_plan_document(
    plan: CohortReviewPlan | object,
) -> JsonObject:
    if isinstance(plan, CohortReviewPlan):
        document = plan.document
    elif isinstance(plan, Mapping):
        document = dict(plan)
    else:
        raise CohortReviewError("Cohort review plan must be a document")
    _plan_identity_and_document(document)
    return document


def _plan_identity_and_document(document: object) -> tuple[str, JsonObject]:
    if not isinstance(document, dict):
        raise CohortReviewError("Cohort review plan must be an object")
    plan_id = _plan_document_identity(document)
    _plan_cohort_ids(document)
    _plan_policy(document)
    return plan_id, document


def _plan_document_identity(document: JsonObject) -> str:
    if document.get("schema") != COHORT_REVIEW_PLAN_SCHEMA:
        raise CohortReviewError("Cohort review plan schema is unsupported")
    if document.get("schema_version") != COHORT_REVIEW_PLAN_VERSION:
        raise CohortReviewError("Cohort review plan version is unsupported")
    plan_id = _required_sha256(document.get("plan_id"), "Plan ID")
    actual = canonical_document_sha256(
        {key: value for key, value in document.items() if key != "plan_id"}
    )
    if actual != plan_id:
        raise CohortReviewError("Cohort review plan identity is invalid")
    return plan_id


def _plan_cohort_ids(document: JsonObject) -> None:
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


def _plan_policy(document: JsonObject) -> None:
    policy = document.get("policy")
    if not isinstance(policy, dict):
        raise CohortReviewError("Cohort review plan policy must be an object")
    policy_version = policy.get("schema_version")
    if policy_version not in SUPPORTED_COHORT_REVIEW_POLICY_VERSIONS:
        raise CohortReviewError("Cohort review plan policy version is unsupported")
    if policy.get("attention_rule") != "all technical flags":
        raise CohortReviewError("Cohort review plan attention rule is invalid")
    _validate_plan_attention_thresholds(
        policy_version, policy.get("attention_thresholds")
    )
    _validate_plan_clean_samples(policy.get("clean_samples_per_bucket"))
    _selected_queue_ids(policy.get("selected_queue_ids"))


def _validate_plan_attention_thresholds(
    policy_version: object, thresholds: object
) -> None:
    if policy_version == 1 and thresholds is not None:
        raise CohortReviewError("Legacy cohort review plan thresholds must be implicit")
    if policy_version != 1 and thresholds != (
        {
            "silence_ratio_at_least": 0.3,
            "internal_pause_seconds_at_least": 1.0,
        }
        if policy_version == 2
        else {
            "silence_ratio_at_least": REVIEW_NOTABLE_SILENCE_RATIO,
            "internal_pause_seconds_at_least": (REVIEW_NOTABLE_INTERNAL_PAUSE_SECONDS),
        }
    ):
        raise CohortReviewError("Cohort review plan attention thresholds are invalid")


def _validate_plan_clean_samples(clean_samples: object) -> None:
    if (
        not isinstance(clean_samples, int)
        or isinstance(clean_samples, bool)
        or not 1 <= clean_samples <= MAX_CLEAN_SAMPLES_PER_BUCKET
    ):
        raise CohortReviewError("Cohort review plan sample count is invalid")


def _selected_queue_ids(queue_ids: object) -> tuple[str, ...] | None:
    if queue_ids is None:
        return None
    if not isinstance(queue_ids, (list, tuple)) or not queue_ids:
        raise CohortReviewError(
            "Selected cohort review queue IDs must be a non-empty list"
        )
    normalized: list[str] = []
    for queue_id in queue_ids:
        queue_id = _required_text(queue_id, "Selected cohort review queue ID")
        if queue_id in normalized:
            raise CohortReviewError(
                f"Selected cohort review queue ID is duplicated: {queue_id}"
            )
        normalized.append(queue_id)
    return tuple(sorted(normalized))


def _decision_item(item: object) -> JsonObject:
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


def _normalize_sample_assessments(
    reviewed_queue_ids: Sequence[str],
    sample_assessments: Mapping[str, object] | None,
) -> list[JsonObject]:
    reviewed = list(reviewed_queue_ids)
    if sample_assessments is None:
        return [
            {"queue_id": queue_id, "assessment": "heard", "defect_reasons": []}
            for queue_id in reviewed
        ]
    _validate_sample_assessment_mapping(sample_assessments, reviewed)
    normalized: list[JsonObject] = []
    for queue_id in reviewed:
        assessment, reasons = _sample_assessment_values(
            sample_assessments.get(queue_id)
        )
        normalized_reasons = _normalized_defect_reasons(reasons)
        _validate_sample_assessment_reasons(assessment, normalized_reasons)
        normalized.append(
            {
                "queue_id": queue_id,
                "assessment": assessment,
                "defect_reasons": normalized_reasons,
            }
        )
    return normalized


def _validate_sample_assessment_mapping(
    sample_assessments: Mapping[str, object], reviewed: Sequence[str]
) -> None:
    if not isinstance(sample_assessments, dict):
        raise CohortReviewError("Sample assessments must be a queue-ID mapping")
    if set(sample_assessments) != set(reviewed):
        raise CohortReviewError(
            "Sample assessments must cover exactly the reviewed queue IDs"
        )


def _sample_assessment_values(value: object) -> tuple[str, object]:
    if isinstance(value, str):
        assessment = value
        reasons: object = ["unspecified"] if value == "bad" else []
    elif isinstance(value, dict) and set(value) == {"assessment", "defect_reasons"}:
        assessment = value["assessment"]
        reasons = value["defect_reasons"]
    else:
        raise CohortReviewError(
            "Sample assessment must be text or an assessment/reasons object"
        )
    if assessment not in {"acceptable", "bad"}:
        raise CohortReviewError("Sample assessment must be acceptable or bad")
    return assessment, reasons


def _normalized_defect_reasons(reasons: object) -> list[str]:
    if not isinstance(reasons, (list, tuple, set, frozenset)) or any(
        not isinstance(reason, str) or reason not in COHORT_REVIEW_DEFECT_REASONS
        for reason in reasons
    ):
        raise CohortReviewError("Sample defect reasons are unsupported")
    return sorted({reason for reason in reasons if isinstance(reason, str)})


def _validate_sample_assessment_reasons(
    assessment: str, reasons: Sequence[str]
) -> None:
    if assessment == "bad" and not reasons:
        raise CohortReviewError("A bad sample requires at least one defect reason")
    if assessment != "bad" and reasons:
        raise CohortReviewError("Only a bad sample may carry speech defect reasons")


def _validated_decision_document(document: object) -> JsonObject:
    if not isinstance(document, dict):
        raise CohortReviewError("Cohort review decision must be an object")
    version, decision = _decision_document_header(document)
    current_samples = _decision_document_policy(document)
    evidence = _decision_document_evidence(document)
    assessments, assessment_ids = _validated_document_assessments(
        evidence.assessments, version
    )
    _validate_document_review_requirements(
        decision, evidence, assessments, assessment_ids
    )
    _validate_document_projection(document, decision)
    _validate_document_item_statuses(document, version, decision, evidence, assessments)
    _validate_document_next_samples(document, decision, current_samples)
    return document


def _decision_document_header(document: JsonObject) -> tuple[int, str]:
    if document.get("schema") != COHORT_REVIEW_DECISION_SCHEMA:
        raise CohortReviewError("Cohort review decision schema is unsupported")
    version = document.get("schema_version")
    if version not in SUPPORTED_COHORT_REVIEW_DECISION_VERSIONS:
        raise CohortReviewError("Cohort review decision version is unsupported")
    claimed = _required_sha256(document.get("decision_id"), "Decision ID")
    actual = canonical_document_sha256(
        {key: value for key, value in document.items() if key != "decision_id"}
    )
    if actual != claimed:
        raise CohortReviewError("Cohort review decision identity is invalid")
    _required_sha256(document.get("plan_id"), "Plan ID")
    _required_sha256(document.get("cohort_id"), "Cohort ID")
    decision = document.get("decision")
    if decision not in {"accepted", "rejected", "split", "expand"}:
        raise CohortReviewError("Cohort review decision is unsupported")
    return version, decision


def _decision_document_policy(document: JsonObject) -> int:
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
    return current_samples


def _decision_document_evidence(document: JsonObject) -> _DecisionDocumentEvidence:
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
    reviewed_ids = [
        _required_text(value["queue_id"], "Decision queue ID")
        for value in reviewed_items
    ]
    target_ids = [
        _required_text(value["queue_id"], "Decision queue ID") for value in target_items
    ]
    if len(set(reviewed_ids)) != len(reviewed_ids) or len(set(target_ids)) != len(
        target_ids
    ):
        raise CohortReviewError("Cohort review decision item IDs must be unique")
    if not set(sampled).issubset(target_ids) or not set(reviewed_ids).issubset(sampled):
        raise CohortReviewError("Cohort review decision sample binding is invalid")
    if not isinstance(assessments, list):
        raise CohortReviewError("Cohort sample assessments must be a list")
    return _DecisionDocumentEvidence(sampled, reviewed_ids, target_ids, assessments)


def _validated_document_assessments(
    assessments: Sequence[object], version: int
) -> tuple[list[JsonObject], list[str]]:
    normalized: list[JsonObject] = []
    assessment_ids: list[str] = []
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
        normalized.append(value)
    return normalized, assessment_ids


def _validate_document_review_requirements(
    decision: str,
    evidence: _DecisionDocumentEvidence,
    assessments: Sequence[JsonObject],
    assessment_ids: Sequence[str],
) -> None:
    if assessment_ids and list(assessment_ids) != evidence.reviewed_ids:
        raise CohortReviewError(
            "Cohort sample assessments do not match reviewed evidence"
        )
    if decision == "accepted" and any(
        value["assessment"] == "bad" for value in assessments
    ):
        raise CohortReviewError("Accepted cohort contains a bad sample assessment")
    if decision in {"accepted", "split", "expand"} and set(
        evidence.reviewed_ids
    ) != set(evidence.sampled):
        raise CohortReviewError("Cohort review decision is missing reviewed samples")
    if decision == "rejected" and not evidence.reviewed_ids:
        raise CohortReviewError("Rejected cohort decision has no reviewed evidence")


def _validate_document_projection(document: JsonObject, decision: str) -> None:
    expected_projection = (
        "approved"
        if decision == "accepted"
        else "rejected"
        if decision == "rejected"
        else None
    )
    if document.get("projection_review_status") != expected_projection:
        raise CohortReviewError("Cohort review projection status is invalid")


def _validate_document_item_statuses(
    document: JsonObject,
    version: int,
    decision: str,
    evidence: _DecisionDocumentEvidence,
    assessments: Sequence[JsonObject],
) -> None:
    item_review_statuses = document.get("item_review_statuses")
    if version < 3:
        if item_review_statuses is not None:
            raise CohortReviewError(
                "Legacy cohort decision cannot contain item review statuses"
            )
        return
    normalized_statuses = _normalized_document_item_statuses(
        item_review_statuses, version
    )
    expected_statuses = _expected_document_item_statuses(
        version, decision, evidence, assessments
    )
    if normalized_statuses != expected_statuses:
        raise CohortReviewError(
            "Cohort item review statuses do not match the exact decision"
        )


def _normalized_document_item_statuses(
    item_review_statuses: object, version: int
) -> list[JsonObject]:
    if not isinstance(item_review_statuses, list):
        raise CohortReviewError("Cohort item review statuses must be a list")
    allowed_statuses = (
        {"approved", "rejected", "pending_review"}
        if version >= 4
        else {"approved", "rejected"}
    )
    normalized: list[JsonObject] = []
    for value in item_review_statuses:
        if (
            not isinstance(value, dict)
            or set(value) != {"queue_id", "review_status"}
            or value.get("review_status") not in allowed_statuses
        ):
            raise CohortReviewError("Cohort item review status is invalid")
        normalized.append(
            {
                "queue_id": _required_text(
                    value.get("queue_id"), "Cohort item review queue ID"
                ),
                "review_status": value["review_status"],
            }
        )
    return normalized


def _expected_document_item_statuses(
    version: int,
    decision: str,
    evidence: _DecisionDocumentEvidence,
    assessments: Sequence[JsonObject],
) -> list[JsonObject]:
    if decision == "expand":
        return []
    if decision == "accepted":
        return [
            {"queue_id": queue_id, "review_status": "approved"}
            for queue_id in evidence.target_ids
        ]
    if decision == "rejected":
        return [
            {"queue_id": queue_id, "review_status": "rejected"}
            for queue_id in evidence.target_ids
        ]
    return _expected_split_item_statuses(version, evidence, assessments)


def _expected_split_item_statuses(
    version: int,
    evidence: _DecisionDocumentEvidence,
    assessments: Sequence[JsonObject],
) -> list[JsonObject]:
    if version == 3 and set(evidence.sampled) != set(evidence.target_ids):
        raise CohortReviewError(
            "Split cohort decision cannot cover unsampled target WAVs"
        )
    statuses = _split_item_review_statuses(
        evidence.target_ids, _assessment_by_id(assessments)
    )
    projected = {value["review_status"] for value in statuses}
    if version == 3 and projected != {"approved", "rejected"}:
        raise CohortReviewError(
            "Split cohort decision requires bad and acceptable WAVs"
        )
    if version >= 4 and ("rejected" not in projected or projected == {"rejected"}):
        raise CohortReviewError(
            "Split cohort decision requires a marked-bad WAV and at least "
            "one acceptable or unsampled WAV"
        )
    return statuses


def _validate_document_next_samples(
    document: JsonObject, decision: str, current_samples: int
) -> None:
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


def _load_document(path: str | Path, label: str) -> JsonObject:
    path = Path(path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise CohortReviewError(f"{label.title()} must be an object")
        return document
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(f"Unable to read {label} {path}: {error}") from error


def _write_document_no_replace(
    output_path: str | Path, document: Mapping[str, object], label: str
) -> Path:
    return Path(
        write_json_document_no_replace(
            output_path,
            document,
            label,
            error_type=CohortReviewError,
        )
    )


def _write_or_validate_document(path: Path, document: JsonObject, label: str) -> Path:
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


def _cohort_identity(workspace: JsonObject, result: JsonObject) -> JsonObject:
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


def _length_bucket(word_count: int) -> str:
    if word_count <= 6:
        return "short"
    if word_count <= 15:
        return "medium"
    return "long"


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CohortReviewError(f"{label} must be non-empty text")
    return value


def _required_sha256(value: object, label: str) -> str:
    value = _required_text(value, label)
    if not is_lowercase_sha256(value):
        raise CohortReviewError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _required_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CohortReviewError(f"{label} must be an integer")
    return value


def _required_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise CohortReviewError(f"{label} must be a boolean")
    return value


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise CohortReviewError("Expected an object")
    return value


def _object_list(value: object) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CohortReviewError("Expected a list of objects")
    return value


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CohortReviewError("Expected a list of strings")
    return value
