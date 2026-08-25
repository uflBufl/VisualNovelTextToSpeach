"""Versioned wire schema for authoring reconciliation reports."""

from __future__ import annotations

import copy
import re
from collections import Counter
from pathlib import Path

from vntts.authoring.authority import canonical_document_sha256

AUTHORING_RECONCILIATION_SCHEMA = "vntts.authoring-authority-reconciliation"
AUTHORING_RECONCILIATION_VERSION = 1
WORKSPACE_NAME_PATTERN = re.compile(r"resume-[0-9a-f]{24}-[0-9a-f]{16}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
RECONCILIATION_ACTIONS = {
    "generation_ready_unselected",
    "human_cohort_review",
    "human_source_quality_review",
    "new_hypothesis_required",
    "review_plan_required",
    "source_reference_or_explicit_fallback",
    "workspace_blocked",
}
TERMINAL_AUTHORITIES = {"approved", "rejected", "explicit_fallback"}
_RUNTIME_STATUSES = {
    "ready",
    "running_here",
    "running_external",
    "interrupted",
    "needs_review",
    "needs_attention",
    "complete",
    "blocked",
}
_STATE_STATUSES = {"generated", "approved", "failed"}
_REVIEW_STATUSES = {"pending_review", "approved", "rejected"}


class AuthoringReconciliationSchemaError(RuntimeError):
    """A reconciliation wire document is malformed or inconsistent."""


def validate_authoring_reconciliation_document(report):
    """Return a deep-validated version-1 reconciliation wire document."""
    document = report
    if not isinstance(document, dict):
        raise AuthoringReconciliationSchemaError(
            "Authoring reconciliation must be an object"
        )
    document = copy.deepcopy(document)
    if (
        document.get("schema") != AUTHORING_RECONCILIATION_SCHEMA
        or document.get("schema_version") != AUTHORING_RECONCILIATION_VERSION
    ):
        raise AuthoringReconciliationSchemaError("Unsupported authoring reconciliation")
    report_id = document.get("report_id")
    if not isinstance(report_id, str) or not SHA256_PATTERN.fullmatch(report_id):
        raise AuthoringReconciliationSchemaError(
            "Authoring reconciliation ID is invalid"
        )
    if (
        canonical_document_sha256(
            {key: value for key, value in document.items() if key != "report_id"}
        )
        != report_id
    ):
        raise AuthoringReconciliationSchemaError(
            "Authoring reconciliation content changed"
        )
    _require_fields(
        document,
        {
            "schema",
            "schema_version",
            "report_id",
            "policy",
            "authoring_root",
            "primary_workspace_id",
            "summary",
            "workspaces",
            "review_bundles",
            "quality_reviews",
            "actions",
            "terminal_conflicts",
        },
        "Authoring reconciliation",
    )
    policy = _required_object(document.get("policy"), "Reconciliation policy")
    expected_policy = {
        "authority_scope": "workspace-local",
        "cross_workspace_merge": "explicit terminal evidence only",
        "approval_inference": "forbidden",
        "mutation": "read-only",
    }
    if policy != expected_policy:
        raise AuthoringReconciliationSchemaError("Reconciliation policy is invalid")
    authoring_root = _required_text(document.get("authoring_root"), "Authoring root")
    if not Path(authoring_root).is_absolute():
        raise AuthoringReconciliationSchemaError("Authoring root must be absolute")
    primary_workspace_id = _required_text(
        document.get("primary_workspace_id"), "Primary workspace ID"
    )
    if not WORKSPACE_NAME_PATTERN.fullmatch(primary_workspace_id):
        raise AuthoringReconciliationSchemaError("Primary workspace ID is invalid")

    workspaces = _validated_report_workspaces(document.get("workspaces"))
    workspace_ids = {value["workspace_id"] for value in workspaces}
    if primary_workspace_id not in workspace_ids:
        raise AuthoringReconciliationSchemaError(
            "Primary workspace is absent from the reconciliation"
        )
    bundles = _validated_report_bundles(document.get("review_bundles"))
    quality_reviews = _validated_report_quality_reviews(document.get("quality_reviews"))
    actions = _validated_report_actions(document.get("actions"), workspace_ids)
    conflicts = _validated_report_conflicts(
        document.get("terminal_conflicts"), workspace_ids
    )
    summary = _required_object(document.get("summary"), "Reconciliation summary")
    _require_fields(
        summary,
        {
            "workspace_count",
            "bundle_count",
            "quality_review_count",
            "nonterminal_action_count",
            "action_counts",
            "terminal_conflict_count",
        },
        "Reconciliation summary",
    )
    expected_counts = {
        "workspace_count": len(workspaces),
        "bundle_count": len(bundles),
        "quality_review_count": len(quality_reviews),
        "nonterminal_action_count": len(actions),
        "terminal_conflict_count": len(conflicts),
    }
    for field, expected in expected_counts.items():
        if _nonnegative_integer(summary.get(field), field) != expected:
            raise AuthoringReconciliationSchemaError(
                f"Reconciliation summary {field} is inconsistent"
            )
    action_counts = _validated_count_map(
        summary.get("action_counts"),
        "Reconciliation action counts",
        RECONCILIATION_ACTIONS,
    )
    if action_counts != dict(
        sorted(Counter(item["action"] for item in actions).items())
    ):
        raise AuthoringReconciliationSchemaError(
            "Reconciliation summary action counts are inconsistent"
        )
    return document


def _validated_report_workspaces(value):
    workspaces = _required_list(value, "Reconciliation workspaces")
    seen = set()
    for workspace in workspaces:
        workspace = _required_object(workspace, "Reconciliation workspace")
        _require_fields(
            workspace,
            {
                "workspace",
                "workspace_id",
                "config_fingerprint",
                "queue_sha256",
                "state_sha256",
                "manifest_sha256",
                "runtime_status",
                "active",
                "report_scope",
                "reported_queue_item_count",
                "authoritative_counts",
                "terminal_counts",
                "action_counts",
            },
            "Reconciliation workspace",
        )
        path = _required_text(workspace.get("workspace"), "Workspace path")
        if not Path(path).is_absolute():
            raise AuthoringReconciliationSchemaError("Workspace path must be absolute")
        workspace_id = _required_text(workspace.get("workspace_id"), "Workspace ID")
        if (
            not WORKSPACE_NAME_PATTERN.fullmatch(workspace_id)
            or Path(path).name != workspace_id
            or workspace_id in seen
        ):
            raise AuthoringReconciliationSchemaError(
                "Reconciliation workspace identity is invalid or duplicated"
            )
        seen.add(workspace_id)
        _required_sha256(workspace.get("config_fingerprint"), "Config fingerprint")
        _required_sha256(workspace.get("queue_sha256"), "Queue SHA-256")
        _optional_sha256(workspace.get("state_sha256"), "State SHA-256")
        _optional_sha256(workspace.get("manifest_sha256"), "Manifest SHA-256")
        if workspace.get("runtime_status") not in _RUNTIME_STATUSES:
            raise AuthoringReconciliationSchemaError(
                "Workspace runtime status is invalid"
            )
        if not isinstance(workspace.get("active"), bool):
            raise AuthoringReconciliationSchemaError(
                "Workspace active flag must be boolean"
            )
        scope = workspace.get("report_scope")
        if scope not in {
            "complete_primary_workspace",
            "current_bundle_items_only",
            "original_bundle_items_only",
        }:
            raise AuthoringReconciliationSchemaError(
                "Workspace report scope is invalid"
            )
        reported = _nonnegative_integer(
            workspace.get("reported_queue_item_count"), "Reported queue item count"
        )
        authoritative = _validated_count_map(
            workspace.get("authoritative_counts"),
            "Workspace authoritative counts",
            {
                "eligible",
                "pending",
                "generated",
                "approved",
                "rejected",
                "live_fallback",
                "failed",
                "missing_voice",
            },
            exact=True,
        )
        if reported > authoritative["eligible"]:
            raise AuthoringReconciliationSchemaError(
                "Reported workspace scope exceeds eligible queue items"
            )
        terminal = _validated_count_map(
            workspace.get("terminal_counts"),
            "Workspace terminal counts",
            TERMINAL_AUTHORITIES,
        )
        action = _validated_count_map(
            workspace.get("action_counts"),
            "Workspace action counts",
            RECONCILIATION_ACTIONS,
        )
        if sum(terminal.values()) + sum(action.values()) != reported:
            raise AuthoringReconciliationSchemaError(
                "Workspace scoped terminal/action counts are inconsistent"
            )
    return workspaces


def _validated_report_bundles(value):
    bundles = _required_list(value, "Reconciliation review bundles")
    seen = set()
    required = {
        "publication",
        "publication_sha256",
        "progress",
        "progress_sha256",
        "root_bundle_id",
        "current_bundle_id",
        "progress_current",
        "original_cohorts",
        "completed_cohorts",
        "remaining_cohorts",
        "original_samples",
        "remaining_samples",
        "original_items",
        "remaining_items",
    }
    for bundle in bundles:
        bundle = _required_object(bundle, "Reconciliation review bundle")
        _require_fields(bundle, required, "Reconciliation review bundle")
        publication = _required_text(
            bundle.get("publication"), "Review bundle publication"
        )
        if not Path(publication).is_absolute() or publication in seen:
            raise AuthoringReconciliationSchemaError(
                "Review bundle publication is invalid or duplicated"
            )
        seen.add(publication)
        progress = _required_text(bundle.get("progress"), "Review bundle progress")
        if not Path(progress).is_absolute():
            raise AuthoringReconciliationSchemaError(
                "Review bundle progress must be absolute"
            )
        for field in ("publication_sha256", "root_bundle_id", "current_bundle_id"):
            _required_sha256(bundle.get(field), field)
        _optional_sha256(bundle.get("progress_sha256"), "Progress SHA-256")
        if not isinstance(bundle.get("progress_current"), bool):
            raise AuthoringReconciliationSchemaError(
                "Review bundle progress-current flag must be boolean"
            )
        counts = {
            field: _nonnegative_integer(bundle.get(field), field)
            for field in required
            if field.startswith(("original_", "completed_", "remaining_"))
        }
        if counts["original_cohorts"] != (
            counts["completed_cohorts"] + counts["remaining_cohorts"]
        ):
            raise AuthoringReconciliationSchemaError(
                "Review bundle cohort counts are invalid"
            )
        for noun in ("samples", "items"):
            if counts[f"remaining_{noun}"] > counts[f"original_{noun}"]:
                raise AuthoringReconciliationSchemaError(
                    f"Review bundle remaining {noun} count is invalid"
                )
    return bundles


def _validated_report_quality_reviews(value):
    reviews = _required_list(value, "Reconciliation quality reviews")
    seen = set()
    for review in reviews:
        review = _required_object(review, "Reconciliation quality review")
        _require_fields(
            review,
            {
                "review",
                "review_sha256",
                "variant_count",
                "completed_count",
                "pending_variant_ids",
                "decision_counts",
            },
            "Reconciliation quality review",
        )
        path = _required_text(review.get("review"), "Quality review path")
        if not Path(path).is_absolute() or path in seen:
            raise AuthoringReconciliationSchemaError(
                "Quality review path is invalid or duplicated"
            )
        seen.add(path)
        _required_sha256(review.get("review_sha256"), "Quality review SHA-256")
        total = _nonnegative_integer(review.get("variant_count"), "Variant count")
        completed = _nonnegative_integer(
            review.get("completed_count"), "Completed variant count"
        )
        pending = _required_list(
            review.get("pending_variant_ids"), "Pending quality variants"
        )
        if any(not isinstance(item, str) or not item.strip() for item in pending):
            raise AuthoringReconciliationSchemaError(
                "Pending quality variant ID is invalid"
            )
        if len(pending) != len(set(pending)) or completed + len(pending) != total:
            raise AuthoringReconciliationSchemaError(
                "Quality review progress is inconsistent"
            )
        decisions = _validated_count_map(
            review.get("decision_counts"),
            "Quality decision counts",
            {"accept", "reject", "needs_sample"},
        )
        if sum(decisions.values()) != completed:
            raise AuthoringReconciliationSchemaError(
                "Quality decision counts are inconsistent"
            )
    return reviews


def _validated_report_actions(value, workspace_ids):
    actions = _required_list(value, "Reconciliation actions")
    seen = set()
    for action in actions:
        action = _required_object(action, "Reconciliation action")
        kind = action.get("action")
        if kind not in RECONCILIATION_ACTIONS:
            raise AuthoringReconciliationSchemaError("Reconciliation action is invalid")
        if kind == "human_source_quality_review":
            required = {
                "action",
                "review",
                "variant_id",
                "character",
                "reference_kind",
                "generated_sample_count",
                "excluded_result_count",
            }
            _require_fields(action, required, "Source quality action")
            identity = (
                kind,
                _required_text(action.get("review"), "Quality action review"),
                _required_text(action.get("variant_id"), "Quality variant ID"),
            )
            _required_text(action.get("character"), "Quality action character")
            _required_text(action.get("reference_kind"), "Reference kind")
            _nonnegative_integer(
                action.get("generated_sample_count"), "Generated sample count"
            )
            _nonnegative_integer(
                action.get("excluded_result_count"), "Excluded result count"
            )
        else:
            required = {
                "action",
                "workspace_id",
                "queue_id",
                "line_id",
                "text_sha256",
                "speaker",
                "voice_character",
                "status",
                "review_status",
                "reason",
            }
            _require_fields(action, required, "Workspace action")
            workspace_id = _required_text(
                action.get("workspace_id"), "Action workspace ID"
            )
            if workspace_id not in workspace_ids:
                raise AuthoringReconciliationSchemaError(
                    "Action references an unknown workspace"
                )
            queue_id = _required_text(action.get("queue_id"), "Action queue ID")
            identity = (kind, workspace_id, queue_id)
            _required_text(action.get("line_id"), "Action line ID")
            _required_sha256(action.get("text_sha256"), "Action text SHA-256")
            for field in ("speaker", "voice_character"):
                _optional_text(action.get(field), f"Action {field}")
            status = _optional_text(action.get("status"), "Action status")
            review_status = _optional_text(
                action.get("review_status"), "Action review status"
            )
            if status is not None and status not in _STATE_STATUSES:
                raise AuthoringReconciliationSchemaError("Action status is invalid")
            if review_status is not None and review_status not in _REVIEW_STATUSES:
                raise AuthoringReconciliationSchemaError(
                    "Action review status is invalid"
                )
            _required_text(action.get("reason"), "Action reason")
            if kind in {"human_cohort_review", "review_plan_required"}:
                _required_sha256(action.get("audio_sha256"), "Action audio SHA-256")
            if kind == "human_cohort_review":
                cohort = _required_object(action.get("cohort"), "Action cohort")
                _require_fields(
                    cohort,
                    {
                        "publication",
                        "root_bundle_id",
                        "current_bundle_id",
                        "cohort_id",
                        "sampled",
                        "audio_sha256",
                    },
                    "Action cohort",
                )
                _required_text(cohort.get("publication"), "Cohort publication")
                _required_sha256(cohort.get("root_bundle_id"), "Root bundle ID")
                _required_sha256(cohort.get("current_bundle_id"), "Current bundle ID")
                _required_sha256(cohort.get("cohort_id"), "Cohort ID")
                _required_sha256(cohort.get("audio_sha256"), "Cohort audio SHA-256")
                if not isinstance(cohort.get("sampled"), bool):
                    raise AuthoringReconciliationSchemaError(
                        "Action cohort sampled flag must be boolean"
                    )
        if identity in seen:
            raise AuthoringReconciliationSchemaError(
                "Reconciliation action is duplicated"
            )
        seen.add(identity)
    return actions


def _validated_report_conflicts(value, workspace_ids):
    conflicts = _required_list(value, "Reconciliation terminal conflicts")
    seen = set()
    for conflict in conflicts:
        conflict = _required_object(conflict, "Terminal conflict")
        _require_fields(
            conflict, {"queue_id", "reason", "occurrences"}, "Terminal conflict"
        )
        queue_id = _required_text(conflict.get("queue_id"), "Conflict queue ID")
        if queue_id in seen:
            raise AuthoringReconciliationSchemaError("Terminal conflict is duplicated")
        seen.add(queue_id)
        _required_text(conflict.get("reason"), "Conflict reason")
        occurrences = _required_list(
            conflict.get("occurrences"), "Conflict occurrences"
        )
        if len(occurrences) < 2:
            raise AuthoringReconciliationSchemaError(
                "Terminal conflict must contain multiple occurrences"
            )
        occurrence_ids = set()
        for occurrence in occurrences:
            occurrence = _required_object(occurrence, "Conflict occurrence")
            _require_fields(
                occurrence,
                {
                    "workspace_id",
                    "authority",
                    "line_id",
                    "text_sha256",
                    "queue_record_sha256",
                },
                "Conflict occurrence",
            )
            workspace_id = _required_text(
                occurrence.get("workspace_id"), "Conflict workspace ID"
            )
            if workspace_id not in workspace_ids:
                raise AuthoringReconciliationSchemaError(
                    "Conflict references an unknown workspace"
                )
            authority = _required_text(
                occurrence.get("authority"), "Conflict authority"
            )
            if authority not in RECONCILIATION_ACTIONS | TERMINAL_AUTHORITIES:
                raise AuthoringReconciliationSchemaError(
                    "Conflict authority is invalid"
                )
            _required_text(occurrence.get("line_id"), "Conflict line ID")
            _required_sha256(occurrence.get("text_sha256"), "Conflict text SHA-256")
            _required_sha256(
                occurrence.get("queue_record_sha256"), "Conflict queue-record SHA-256"
            )
            identity = (workspace_id, authority)
            if identity in occurrence_ids:
                raise AuthoringReconciliationSchemaError(
                    "Conflict occurrence is duplicated"
                )
            occurrence_ids.add(identity)
    return conflicts


def _require_fields(document, fields, label):
    missing = sorted(set(fields) - set(document))
    if missing:
        raise AuthoringReconciliationSchemaError(
            f"{label} is missing required fields: {', '.join(missing)}"
        )


def _required_object(value, label):
    if not isinstance(value, dict):
        raise AuthoringReconciliationSchemaError(f"{label} must be an object")
    return value


def _required_list(value, label):
    if not isinstance(value, list):
        raise AuthoringReconciliationSchemaError(f"{label} must be a list")
    return value


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AuthoringReconciliationSchemaError(f"{label} must be non-empty text")
    return value


def _optional_text(value, label):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise AuthoringReconciliationSchemaError(f"{label} must be text or null")
    return value


def _required_sha256(value, label):
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise AuthoringReconciliationSchemaError(f"{label} must be lowercase SHA-256")
    return value


def _optional_sha256(value, label):
    if value is not None:
        _required_sha256(value, label)
    return value


def _nonnegative_integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthoringReconciliationSchemaError(f"{label} must be non-negative")
    return value


def _validated_count_map(value, label, allowed, *, exact=False):
    counts = _required_object(value, label)
    keys = set(counts)
    if (exact and keys != set(allowed)) or not keys <= set(allowed):
        raise AuthoringReconciliationSchemaError(f"{label} contains unsupported keys")
    return {
        key: _nonnegative_integer(counts[key], f"{label} {key}")
        for key in sorted(counts)
    }


__all__ = [
    "AUTHORING_RECONCILIATION_SCHEMA",
    "AUTHORING_RECONCILIATION_VERSION",
    "AuthoringReconciliationSchemaError",
    "RECONCILIATION_ACTIONS",
    "SHA256_PATTERN",
    "TERMINAL_AUTHORITIES",
    "WORKSPACE_NAME_PATTERN",
    "validate_authoring_reconciliation_document",
]
