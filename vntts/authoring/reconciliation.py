"""Read-only reconciliation of exact authoring review and generation authority."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    canonical_document_sha256,
    capture_authority_file,
    write_json_document_no_replace,
)
from vntts.authoring.bulk_generation import (
    is_spoken_queue_item,
    validate_generation_state_document,
)
from vntts.authoring.cohort_bundle import (
    COHORT_REVIEW_BUNDLE_SCHEMA,
    COHORT_REVIEW_BUNDLE_VERSION,
    CohortReviewResume,
    cohort_review_progress_path,
    reconcile_cohort_review_bundle,
    validate_cohort_review_bundle_document,
    validate_cohort_review_progress_document,
)
from vntts.authoring.source_reference_quality import (
    QUALITY_REVIEW_SCHEMA,
    QUALITY_REVIEW_VERSION,
    validate_source_reference_quality_review_document,
)
from vntts.authoring.workbench import (
    contained_workspace_path,
    inspect_voice_readiness,
    inspect_workspace,
    load_workspace_authority,
    safe_workspace_relative_path,
)

AUTHORING_RECONCILIATION_SCHEMA = "vntts.authoring-authority-reconciliation"
AUTHORING_RECONCILIATION_VERSION = 1
_WORKSPACE_NAME = re.compile(r"resume-[0-9a-f]{24}-[0-9a-f]{16}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ACTIONS = {
    "generation_ready_unselected",
    "human_cohort_review",
    "human_source_quality_review",
    "new_hypothesis_required",
    "review_plan_required",
    "source_reference_or_explicit_fallback",
    "workspace_blocked",
}
_TERMINAL_AUTHORITIES = {"approved", "rejected", "explicit_fallback"}
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


class AuthoringReconciliationError(RuntimeError):
    """Current authoring authorities cannot be reconciled safely."""


@dataclass(frozen=True)
class AuthoringReconciliation:
    """One immutable report over exact read-only authoring snapshots."""

    report_id: str
    document: dict

    def to_dict(self):
        return dict(self.document)


def build_authoring_reconciliation(
    primary_workspace,
    bundle_root,
    *,
    quality_reviews=(),
):
    """Build an exact report without choosing or mutating review authority."""
    primary = Path(primary_workspace).expanduser().resolve()
    bundle_root = Path(bundle_root).expanduser().resolve()
    workspaces_root = primary.parent.resolve()
    authoring_root = workspaces_root.parent.resolve()
    _require_contained_directory(workspaces_root, primary, "Primary workspace")
    if not _WORKSPACE_NAME.fullmatch(primary.name):
        raise AuthoringReconciliationError("Primary workspace name is not canonical")
    _require_contained_directory(authoring_root, bundle_root, "Review bundle root")
    bundle_inventory = _json_inventory(bundle_root)

    snapshots = {}
    workspace_paths = {primary}
    bundle_reports = []
    bundle_actions = {}
    bundle_workspace_queue_ids = {}
    if bundle_root.is_dir():
        for path in sorted(bundle_root.glob("*.json")):
            if path.name.endswith(".progress.json"):
                continue
            payload, candidate = _read_json_snapshot(path, "review bundle")
            if (
                candidate.get("schema") != COHORT_REVIEW_BUNDLE_SCHEMA
                or candidate.get("schema_version") != COHORT_REVIEW_BUNDLE_VERSION
            ):
                continue
            _remember_snapshot(snapshots, path, payload)
            try:
                original = validate_cohort_review_bundle_document(candidate)
                current = reconcile_cohort_review_bundle(original)
            except Exception as error:
                raise AuthoringReconciliationError(
                    f"Current review bundle is invalid: {path.name}: {error}"
                ) from error
            progress = cohort_review_progress_path(path)
            progress_sha256 = None
            progress_current = False
            if progress.is_file():
                progress_payload, progress_document = _read_json_snapshot(
                    progress, "review bundle progress"
                )
                _remember_snapshot(snapshots, progress, progress_payload)
                progress_sha256 = hashlib.sha256(progress_payload).hexdigest()
                try:
                    saved = validate_cohort_review_progress_document(
                        progress_document, original
                    )
                except Exception as error:
                    raise AuthoringReconciliationError(
                        f"Current review progress is invalid: {progress.name}: {error}"
                    ) from error
                progress_current = saved.bundle_id == current.bundle_id
            else:
                _remember_absence(snapshots, progress)
            resume = CohortReviewResume(
                path,
                progress,
                original,
                current,
                progress_current,
            )
            for source in original.document["sources"]:
                workspace_paths.add(Path(source["workspace"]).resolve())
                workspace_id = source["workspace_id"]
                for cohort in source["plan"]["cohorts"]:
                    for item in cohort["items"]:
                        bundle_workspace_queue_ids.setdefault(workspace_id, set()).add(
                            item["queue_id"]
                        )
            for source in current.document["sources"]:
                workspace_id = source["workspace_id"]
                for cohort in source["plan"]["cohorts"]:
                    for item in cohort["items"]:
                        key = (workspace_id, item["queue_id"])
                        authority = {
                            "publication": path.name,
                            "root_bundle_id": resume.original.bundle_id,
                            "current_bundle_id": resume.current.bundle_id,
                            "cohort_id": cohort["cohort_id"],
                            "sampled": bool(item["sampled"]),
                            "audio_sha256": item["audio_sha256"],
                        }
                        previous = bundle_actions.setdefault(key, authority)
                        if previous != authority:
                            raise AuthoringReconciliationError(
                                "One workspace item has ambiguous current review "
                                f"bundles: {workspace_id}/{item['queue_id']}"
                            )
            bundle_reports.append(
                {
                    "publication": path.name,
                    "publication_sha256": hashlib.sha256(payload).hexdigest(),
                    "progress_sha256": progress_sha256,
                    **resume.to_dict(),
                }
            )

    quality_reports = []
    quality_actions = []
    quality_review_paths = tuple(
        sorted(
            (Path(value).expanduser().resolve() for value in quality_reviews),
            key=str,
        )
    )
    if len(set(quality_review_paths)) != len(quality_review_paths):
        raise AuthoringReconciliationError("Quality review path is duplicated")
    for path in quality_review_paths:
        _require_contained_file(authoring_root, path, "Source-reference quality review")
        if path.name != "review.json":
            raise AuthoringReconciliationError(
                "Source-reference quality review must be a review.json document"
            )
        payload, candidate = _read_json_snapshot(path, "quality review")
        if (
            candidate.get("schema") != QUALITY_REVIEW_SCHEMA
            or candidate.get("schema_version") != QUALITY_REVIEW_VERSION
        ):
            raise AuthoringReconciliationError(
                f"Unsupported source-reference quality review: {path}"
            )
        try:
            session = validate_source_reference_quality_review_document(
                candidate, path.parent
            )
        except Exception as error:
            raise AuthoringReconciliationError(
                f"Source-reference quality review is invalid: {path}: {error}"
            ) from error
        _remember_snapshot(snapshots, path, payload)
        pending = []
        decisions = Counter()
        for card in session["variants"]:
            decision = card.get("decision")
            if decision is None:
                pending.append(card["variant_id"])
                quality_actions.append(
                    {
                        "action": "human_source_quality_review",
                        "review": str(path),
                        "variant_id": card["variant_id"],
                        "character": card["character"],
                        "reference_kind": card.get("reference_kind", "single_media"),
                        "generated_sample_count": len(card["generated_samples"]),
                        "excluded_result_count": len(card["excluded_results"]),
                    }
                )
            else:
                decisions[decision["decision"]] += 1
            _snapshot_quality_card(path.parent, card, snapshots)
        quality_reports.append(
            {
                "review": str(path),
                "review_sha256": hashlib.sha256(payload).hexdigest(),
                "variant_count": session["variant_count"],
                "completed_count": session["completed_count"],
                "pending_variant_ids": pending,
                "decision_counts": dict(sorted(decisions.items())),
            }
        )

    workspace_reports = []
    actions = []
    occurrence_index = {}
    for workspace_path in sorted(workspace_paths, key=str):
        _require_contained_directory(
            workspaces_root, workspace_path, "Review source workspace"
        )
        if not _WORKSPACE_NAME.fullmatch(workspace_path.name):
            raise AuthoringReconciliationError(
                f"Review source workspace name is not canonical: {workspace_path.name}"
            )
        directory, workspace, workspace_sha256 = load_workspace_authority(
            workspace_path
        )
        summary = inspect_workspace(directory)
        queue_payload = _read_bytes(summary.queue, "workspace queue")
        queue = _load_queue_snapshot(queue_payload)
        _remember_snapshot(snapshots, summary.queue, queue_payload)
        configuration = directory / "workspace.json"
        configuration_payload = _read_bytes(configuration, "workspace configuration")
        if hashlib.sha256(configuration_payload).hexdigest() != workspace_sha256:
            raise AuthoringReconciliationError(
                "Workspace configuration changed after validation"
            )
        _remember_snapshot(snapshots, configuration, configuration_payload)
        _snapshot_workspace_voice_controls(directory, workspace, snapshots)
        state = {"active": None, "items": {}}
        state_sha256 = None
        if summary.state is not None:
            state_payload, state_document = _read_json_snapshot(
                summary.state, "workspace state"
            )
            _remember_snapshot(snapshots, summary.state, state_payload)
            state_sha256 = hashlib.sha256(state_payload).hexdigest()
            try:
                state = validate_generation_state_document(
                    state_document,
                    summary.output,
                    queue,
                    hashlib.sha256(queue_payload).hexdigest(),
                )
            except Exception as error:
                raise AuthoringReconciliationError(str(error)) from error
        else:
            _remember_absence(snapshots, summary.output / "generation-state.json")
        manifest = summary.output / "manifest.json"
        manifest_sha256 = None
        if manifest.is_file():
            manifest_payload = _read_bytes(manifest, "generated manifest")
            _remember_snapshot(snapshots, manifest, manifest_payload)
            manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
        else:
            _remember_absence(snapshots, manifest)

        spoken = [
            item
            for item in queue.items
            if item.action == "generate" and is_spoken_queue_item(item)
        ]
        spoken_ids = {item.queue_id for item in spoken}
        relevant = {
            queue_id: value
            for queue_id, value in state["items"].items()
            if queue_id in spoken_ids
        }
        approved_ids = {
            queue_id
            for queue_id, value in relevant.items()
            if value.get("status") == "approved"
            and value.get("review_status") == "approved"
        }
        rejected_ids = {
            queue_id
            for queue_id, value in relevant.items()
            if value.get("status") == "generated"
            and value.get("review_status") == "rejected"
        }
        generated_ids = {
            queue_id
            for queue_id, value in relevant.items()
            if value.get("status") == "generated"
            and value.get("review_status") == "pending_review"
        }
        failed_ids = {
            queue_id
            for queue_id, value in relevant.items()
            if value.get("status") == "failed"
        }
        live_fallback_ids = {
            queue_id
            for queue_id, value in relevant.items()
            if isinstance(value.get("live_fallback"), dict)
        }
        completed_ids = (
            approved_ids | rejected_ids | generated_ids | failed_ids | live_fallback_ids
        )
        candidates = [item for item in spoken if item.queue_id not in completed_ids]
        missing, _voice_reasons = inspect_voice_readiness(
            workspace,
            candidates,
            set(),
            summary.voice_manifest,
            directory=directory,
        )
        missing = set(missing)
        pending_ids = spoken_ids - completed_ids - missing
        action_counts = Counter()
        terminal_counts = Counter()
        scoped_queue_ids = (
            None
            if workspace["workspace_id"] == primary.name
            else bundle_workspace_queue_ids.get(workspace["workspace_id"], set())
        )
        for item in queue.items:
            if item.action != "generate" or not is_spoken_queue_item(item):
                continue
            if scoped_queue_ids is not None and item.queue_id not in scoped_queue_ids:
                continue
            result = state["items"].get(item.queue_id)
            if isinstance(result, dict):
                status = result.get("status")
                review_status = result.get("review_status")
                audio_authority = _snapshot_state_audio(
                    summary.output, item, result, snapshots
                )
                if isinstance(result.get("live_fallback"), dict):
                    terminal_counts["explicit_fallback"] += 1
                    _remember_occurrence(
                        occurrence_index, workspace, item, "explicit_fallback"
                    )
                    continue
                if status == "approved" and review_status == "approved":
                    terminal_counts["approved"] += 1
                    _remember_occurrence(occurrence_index, workspace, item, "approved")
                    continue
                if status == "generated" and review_status == "rejected":
                    terminal_counts["rejected"] += 1
                    _remember_occurrence(occurrence_index, workspace, item, "rejected")
                    continue
                if status == "generated" and review_status == "pending_review":
                    action = "review_plan_required"
                    bundle = bundle_actions.get(
                        (workspace["workspace_id"], item.queue_id)
                    )
                    if bundle is not None:
                        action = "human_cohort_review"
                    expected_audio_sha256 = result.get("file_sha256")
                    if not isinstance(expected_audio_sha256, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", expected_audio_sha256
                    ):
                        raise AuthoringReconciliationError(
                            f"Pending review item lacks WAV authority: {item.queue_id}"
                        )
                    if (
                        audio_authority is None
                        or audio_authority[1] != expected_audio_sha256
                    ):
                        raise AuthoringReconciliationError(
                            f"Pending review WAV changed: {item.queue_id}"
                        )
                    record = _action_record(
                        workspace,
                        item,
                        action,
                        status=status,
                        review_status=review_status,
                        reason=(
                            "exact current cohort evidence"
                            if bundle is not None
                            else "pending WAV needs a risk-based cohort review plan"
                        ),
                    )
                    if bundle is not None:
                        record["cohort"] = bundle
                    record["audio_sha256"] = expected_audio_sha256
                    actions.append(record)
                    action_counts[action] += 1
                    _remember_occurrence(occurrence_index, workspace, item, action)
                    continue
                if status == "failed":
                    action = "new_hypothesis_required"
                    actions.append(
                        _action_record(
                            workspace,
                            item,
                            action,
                            status=status,
                            review_status=review_status,
                            reason=str(result.get("last_error") or "generation failed"),
                        )
                    )
                    action_counts[action] += 1
                    _remember_occurrence(occurrence_index, workspace, item, action)
                    continue
                raise AuthoringReconciliationError(
                    f"Unsupported nonterminal state for {item.queue_id}: "
                    f"{status}/{review_status}"
                )
            if item.queue_id in missing:
                action = "source_reference_or_explicit_fallback"
                reason = "selected workspace manifest has no usable voice"
            elif summary.blocked_reasons:
                action = "workspace_blocked"
                reason = "; ".join(summary.blocked_reasons)
            else:
                action = "generation_ready_unselected"
                reason = "voice and immutable controls are ready"
            actions.append(
                _action_record(
                    workspace,
                    item,
                    action,
                    status=None,
                    review_status=None,
                    reason=reason,
                )
            )
            action_counts[action] += 1
            _remember_occurrence(occurrence_index, workspace, item, action)
        workspace_reports.append(
            {
                "workspace": str(directory),
                "workspace_id": workspace["workspace_id"],
                "config_fingerprint": workspace["config_fingerprint"],
                "queue_sha256": hashlib.sha256(queue_payload).hexdigest(),
                "state_sha256": state_sha256,
                "manifest_sha256": manifest_sha256,
                "runtime_status": summary.runtime_status.value,
                "active": state.get("active") is not None,
                "report_scope": (
                    "complete_primary_workspace"
                    if scoped_queue_ids is None
                    else "original_bundle_items_only"
                ),
                "reported_queue_item_count": (
                    len(spoken) if scoped_queue_ids is None else len(scoped_queue_ids)
                ),
                "authoritative_counts": {
                    "eligible": len(spoken),
                    "pending": len(pending_ids),
                    "generated": len(generated_ids),
                    "approved": len(approved_ids),
                    "rejected": len(rejected_ids),
                    "live_fallback": len(live_fallback_ids),
                    "failed": len(failed_ids),
                    "missing_voice": len(missing),
                },
                "terminal_counts": dict(sorted(terminal_counts.items())),
                "action_counts": dict(sorted(action_counts.items())),
            }
        )

    conflicts = _terminal_conflicts(occurrence_index)

    _assert_snapshots_unchanged(snapshots)
    if _json_inventory(bundle_root) != bundle_inventory:
        raise AuthoringReconciliationError(
            "Review bundle directory changed during reconciliation"
        )
    action_counts = Counter(value["action"] for value in actions)
    action_counts.update(value["action"] for value in quality_actions)
    body = {
        "schema": AUTHORING_RECONCILIATION_SCHEMA,
        "schema_version": AUTHORING_RECONCILIATION_VERSION,
        "policy": {
            "authority_scope": "workspace-local",
            "cross_workspace_merge": "explicit terminal evidence only",
            "approval_inference": "forbidden",
            "mutation": "read-only",
        },
        "authoring_root": str(authoring_root),
        "primary_workspace_id": primary.name,
        "summary": {
            "workspace_count": len(workspace_reports),
            "bundle_count": len(bundle_reports),
            "quality_review_count": len(quality_reports),
            "nonterminal_action_count": len(actions) + len(quality_actions),
            "action_counts": dict(sorted(action_counts.items())),
            "terminal_conflict_count": len(conflicts),
        },
        "workspaces": sorted(
            workspace_reports, key=lambda value: value["workspace_id"]
        ),
        "review_bundles": sorted(
            bundle_reports, key=lambda value: value["publication"]
        ),
        "quality_reviews": sorted(quality_reports, key=lambda value: value["review"]),
        "actions": sorted(
            [*actions, *quality_actions],
            key=lambda value: (
                value["action"],
                value.get("workspace_id", ""),
                value.get("queue_id", value.get("variant_id", "")),
            ),
        ),
        "terminal_conflicts": conflicts,
    }
    report_id = canonical_document_sha256(body)
    return AuthoringReconciliation(report_id, {**body, "report_id": report_id})


def _terminal_conflicts(occurrence_index):
    conflicts = []
    for queue_id, occurrences in sorted(occurrence_index.items()):
        terminal = {
            occurrence["authority"]
            for occurrence in occurrences
            if occurrence["authority"] in {"approved", "rejected", "explicit_fallback"}
        }
        queue_records = {
            occurrence["queue_record_sha256"] for occurrence in occurrences
        }
        reasons = []
        if len(queue_records) > 1:
            reasons.append("parallel workspaces contain different queue records")
        if len(terminal) > 1:
            reasons.append("parallel workspaces contain conflicting terminal decisions")
        if reasons:
            conflicts.append(
                {
                    "queue_id": queue_id,
                    "reason": "; ".join(reasons),
                    "occurrences": sorted(
                        occurrences,
                        key=lambda value: (
                            value["workspace_id"],
                            value["authority"],
                            value["queue_record_sha256"],
                        ),
                    ),
                }
            )

    return conflicts


def write_authoring_reconciliation(report, output):
    """Publish one validated report without replacing an earlier handoff."""
    document = _validated_report(report)
    try:
        return write_json_document_no_replace(
            output, document, "authoring reconciliation"
        )
    except AuthoringAuthorityError as error:
        raise AuthoringReconciliationError(str(error)) from error


def load_authoring_reconciliation(path):
    payload, document = _read_json_snapshot(path, "authoring reconciliation")
    del payload
    validated = _validated_report(document)
    return AuthoringReconciliation(validated["report_id"], validated)


def _validated_report(report):
    document = (
        report.document if isinstance(report, AuthoringReconciliation) else report
    )
    if not isinstance(document, dict):
        raise AuthoringReconciliationError("Authoring reconciliation must be an object")
    document = copy.deepcopy(document)
    if (
        document.get("schema") != AUTHORING_RECONCILIATION_SCHEMA
        or document.get("schema_version") != AUTHORING_RECONCILIATION_VERSION
    ):
        raise AuthoringReconciliationError("Unsupported authoring reconciliation")
    report_id = document.get("report_id")
    if not isinstance(report_id, str) or not re.fullmatch(r"[0-9a-f]{64}", report_id):
        raise AuthoringReconciliationError("Authoring reconciliation ID is invalid")
    if (
        canonical_document_sha256(
            {key: value for key, value in document.items() if key != "report_id"}
        )
        != report_id
    ):
        raise AuthoringReconciliationError("Authoring reconciliation content changed")
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
        raise AuthoringReconciliationError("Reconciliation policy is invalid")
    authoring_root = _required_text(document.get("authoring_root"), "Authoring root")
    if not Path(authoring_root).is_absolute():
        raise AuthoringReconciliationError("Authoring root must be absolute")
    primary_workspace_id = _required_text(
        document.get("primary_workspace_id"), "Primary workspace ID"
    )
    if not _WORKSPACE_NAME.fullmatch(primary_workspace_id):
        raise AuthoringReconciliationError("Primary workspace ID is invalid")

    workspaces = _validated_report_workspaces(document.get("workspaces"))
    workspace_ids = {value["workspace_id"] for value in workspaces}
    if primary_workspace_id not in workspace_ids:
        raise AuthoringReconciliationError(
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
            raise AuthoringReconciliationError(
                f"Reconciliation summary {field} is inconsistent"
            )
    action_counts = _validated_count_map(
        summary.get("action_counts"), "Reconciliation action counts", _ACTIONS
    )
    if action_counts != dict(
        sorted(Counter(item["action"] for item in actions).items())
    ):
        raise AuthoringReconciliationError(
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
            raise AuthoringReconciliationError("Workspace path must be absolute")
        workspace_id = _required_text(workspace.get("workspace_id"), "Workspace ID")
        if (
            not _WORKSPACE_NAME.fullmatch(workspace_id)
            or Path(path).name != workspace_id
            or workspace_id in seen
        ):
            raise AuthoringReconciliationError(
                "Reconciliation workspace identity is invalid or duplicated"
            )
        seen.add(workspace_id)
        _required_sha256(workspace.get("config_fingerprint"), "Config fingerprint")
        _required_sha256(workspace.get("queue_sha256"), "Queue SHA-256")
        _optional_sha256(workspace.get("state_sha256"), "State SHA-256")
        _optional_sha256(workspace.get("manifest_sha256"), "Manifest SHA-256")
        if workspace.get("runtime_status") not in _RUNTIME_STATUSES:
            raise AuthoringReconciliationError("Workspace runtime status is invalid")
        if not isinstance(workspace.get("active"), bool):
            raise AuthoringReconciliationError("Workspace active flag must be boolean")
        scope = workspace.get("report_scope")
        if scope not in {
            "complete_primary_workspace",
            "current_bundle_items_only",
            "original_bundle_items_only",
        }:
            raise AuthoringReconciliationError("Workspace report scope is invalid")
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
            raise AuthoringReconciliationError(
                "Reported workspace scope exceeds eligible queue items"
            )
        terminal = _validated_count_map(
            workspace.get("terminal_counts"),
            "Workspace terminal counts",
            _TERMINAL_AUTHORITIES,
        )
        action = _validated_count_map(
            workspace.get("action_counts"), "Workspace action counts", _ACTIONS
        )
        if sum(terminal.values()) + sum(action.values()) != reported:
            raise AuthoringReconciliationError(
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
            raise AuthoringReconciliationError(
                "Review bundle publication is invalid or duplicated"
            )
        seen.add(publication)
        progress = _required_text(bundle.get("progress"), "Review bundle progress")
        if not Path(progress).is_absolute():
            raise AuthoringReconciliationError(
                "Review bundle progress must be absolute"
            )
        for field in ("publication_sha256", "root_bundle_id", "current_bundle_id"):
            _required_sha256(bundle.get(field), field)
        _optional_sha256(bundle.get("progress_sha256"), "Progress SHA-256")
        if not isinstance(bundle.get("progress_current"), bool):
            raise AuthoringReconciliationError(
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
            raise AuthoringReconciliationError(
                "Review bundle cohort counts are invalid"
            )
        for noun in ("samples", "items"):
            if counts[f"remaining_{noun}"] > counts[f"original_{noun}"]:
                raise AuthoringReconciliationError(
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
            raise AuthoringReconciliationError(
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
            raise AuthoringReconciliationError("Pending quality variant ID is invalid")
        if len(pending) != len(set(pending)) or completed + len(pending) != total:
            raise AuthoringReconciliationError(
                "Quality review progress is inconsistent"
            )
        decisions = _validated_count_map(
            review.get("decision_counts"),
            "Quality decision counts",
            {"accept", "reject", "needs_sample"},
        )
        if sum(decisions.values()) != completed:
            raise AuthoringReconciliationError(
                "Quality decision counts are inconsistent"
            )
    return reviews


def _validated_report_actions(value, workspace_ids):
    actions = _required_list(value, "Reconciliation actions")
    seen = set()
    for action in actions:
        action = _required_object(action, "Reconciliation action")
        kind = action.get("action")
        if kind not in _ACTIONS:
            raise AuthoringReconciliationError("Reconciliation action is invalid")
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
                raise AuthoringReconciliationError(
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
                raise AuthoringReconciliationError("Action status is invalid")
            if review_status is not None and review_status not in _REVIEW_STATUSES:
                raise AuthoringReconciliationError("Action review status is invalid")
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
                    raise AuthoringReconciliationError(
                        "Action cohort sampled flag must be boolean"
                    )
        if identity in seen:
            raise AuthoringReconciliationError("Reconciliation action is duplicated")
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
            raise AuthoringReconciliationError("Terminal conflict is duplicated")
        seen.add(queue_id)
        _required_text(conflict.get("reason"), "Conflict reason")
        occurrences = _required_list(
            conflict.get("occurrences"), "Conflict occurrences"
        )
        if len(occurrences) < 2:
            raise AuthoringReconciliationError(
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
                raise AuthoringReconciliationError(
                    "Conflict references an unknown workspace"
                )
            authority = _required_text(
                occurrence.get("authority"), "Conflict authority"
            )
            if authority not in _ACTIONS | _TERMINAL_AUTHORITIES:
                raise AuthoringReconciliationError("Conflict authority is invalid")
            _required_text(occurrence.get("line_id"), "Conflict line ID")
            _required_sha256(occurrence.get("text_sha256"), "Conflict text SHA-256")
            _required_sha256(
                occurrence.get("queue_record_sha256"), "Conflict queue-record SHA-256"
            )
            identity = (workspace_id, authority)
            if identity in occurrence_ids:
                raise AuthoringReconciliationError("Conflict occurrence is duplicated")
            occurrence_ids.add(identity)
    return conflicts


def _require_fields(document, fields, label):
    missing = sorted(set(fields) - set(document))
    if missing:
        raise AuthoringReconciliationError(
            f"{label} is missing required fields: {', '.join(missing)}"
        )


def _required_object(value, label):
    if not isinstance(value, dict):
        raise AuthoringReconciliationError(f"{label} must be an object")
    return value


def _required_list(value, label):
    if not isinstance(value, list):
        raise AuthoringReconciliationError(f"{label} must be a list")
    return value


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AuthoringReconciliationError(f"{label} must be non-empty text")
    return value


def _optional_text(value, label):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise AuthoringReconciliationError(f"{label} must be text or null")
    return value


def _required_sha256(value, label):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise AuthoringReconciliationError(f"{label} must be lowercase SHA-256")
    return value


def _optional_sha256(value, label):
    if value is not None:
        _required_sha256(value, label)
    return value


def _nonnegative_integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthoringReconciliationError(f"{label} must be non-negative")
    return value


def _validated_count_map(value, label, allowed, *, exact=False):
    counts = _required_object(value, label)
    keys = set(counts)
    if (exact and keys != set(allowed)) or not keys <= set(allowed):
        raise AuthoringReconciliationError(f"{label} contains unsupported keys")
    return {
        key: _nonnegative_integer(counts[key], f"{label} {key}")
        for key in sorted(counts)
    }


def _action_record(workspace, item, action, *, status, review_status, reason):
    return {
        "action": action,
        "workspace_id": workspace["workspace_id"],
        "queue_id": item.queue_id,
        "line_id": item.line_id,
        "text_sha256": item.text_sha256,
        "speaker": item.speaker,
        "voice_character": item.voice_character,
        "status": status,
        "review_status": review_status,
        "reason": reason,
    }


def _remember_occurrence(index, workspace, item, authority):
    index.setdefault(item.queue_id, []).append(
        {
            "workspace_id": workspace["workspace_id"],
            "authority": authority,
            "line_id": item.line_id,
            "text_sha256": item.text_sha256,
            "queue_record_sha256": canonical_document_sha256(item.to_record()),
        }
    )


def _snapshot_workspace_voice_controls(directory, workspace, snapshots):
    voice = workspace.get("voice_manifest")
    if voice is None:
        return
    if not isinstance(voice, dict):
        raise AuthoringReconciliationError("Workspace voice manifest is malformed")
    records = [(voice.get("path"), voice.get("sha256"), "voice manifest")]
    controls = voice.get("controls")
    if not isinstance(controls, list):
        raise AuthoringReconciliationError("Workspace voice controls are malformed")
    records.extend(
        (control.get("path"), control.get("sha256"), "voice reference")
        for control in controls
        if isinstance(control, dict)
    )
    if len(records) != len(controls) + 1:
        raise AuthoringReconciliationError("Workspace voice control is malformed")
    for value, expected, label in records:
        if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
            raise AuthoringReconciliationError(
                f"Workspace {label} SHA-256 is malformed"
            )
        relative = safe_workspace_relative_path(value, f"Workspace {label}")
        path = contained_workspace_path(directory, relative, f"Workspace {label}")
        payload = _read_bytes(path, f"workspace {label}")
        if hashlib.sha256(payload).hexdigest() != expected:
            raise AuthoringReconciliationError(f"Workspace {label} changed")
        _remember_snapshot(snapshots, path, payload)


def _snapshot_state_audio(output, item, result, snapshots):
    if result.get("status") not in {"generated", "approved"}:
        return None
    expected = result.get("file_sha256")
    if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
        raise AuthoringReconciliationError(
            f"Generated item lacks WAV authority: {item.queue_id}"
        )
    relative = safe_workspace_relative_path(
        result.get("path"), f"Generated item {item.queue_id} WAV"
    )
    path = contained_workspace_path(output, relative, "Generated WAV")
    payload = _read_bytes(path, "generated WAV")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != expected:
        raise AuthoringReconciliationError(f"Generated WAV changed: {item.queue_id}")
    _remember_snapshot(snapshots, path, payload)
    return path, digest


def _snapshot_quality_card(root, card, snapshots):
    records = [
        (card["reference"], "audio", "audio_sha256"),
        *((sample, "audio", "audio_sha256") for sample in card["generated_samples"]),
    ]
    portrait = card.get("portrait_image")
    if isinstance(portrait, dict):
        records.append((portrait, "image", "image_sha256"))
    for record, path_field, digest_field in records:
        relative = record.get(path_field) if isinstance(record, dict) else None
        expected = record.get(digest_field) if isinstance(record, dict) else None
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise AuthoringReconciliationError("Quality review artifact is malformed")
        path = (root / relative).resolve()
        _require_contained_file(root, path, "Quality review artifact")
        payload = _read_bytes(path, "quality review artifact")
        if hashlib.sha256(payload).hexdigest() != expected:
            raise AuthoringReconciliationError(
                f"Quality review artifact changed: {path}"
            )
        _remember_snapshot(snapshots, path, payload)


def _load_queue_snapshot(payload):
    try:
        with TemporaryDirectory(prefix="vntts-reconciliation-queue-") as directory:
            path = Path(directory) / "queue.jsonl"
            path.write_bytes(payload)
            return VoiceGenerationQueue.load(path)
    except (OSError, VoiceGenerationQueueError) as error:
        raise AuthoringReconciliationError(str(error)) from error


def _read_json_snapshot(path, label):
    payload = _read_bytes(path, label)
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthoringReconciliationError(
            f"Unable to read {label}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise AuthoringReconciliationError(f"{label.capitalize()} must be an object")
    return payload, document


def _read_bytes(path, label):
    try:
        return capture_authority_file(path, label).payload
    except AuthoringAuthorityError as error:
        raise AuthoringReconciliationError(str(error)) from error


def _remember_snapshot(snapshots, path, payload):
    path = Path(path).resolve()
    digest = hashlib.sha256(payload).hexdigest()
    previous = snapshots.setdefault(path, digest)
    if previous != digest:
        raise AuthoringReconciliationError(f"Authority changed while reading: {path}")


def _remember_absence(snapshots, path):
    path = Path(path).resolve()
    previous = snapshots.setdefault(path, None)
    if previous is not None:
        raise AuthoringReconciliationError(f"Authority changed while reading: {path}")


def _assert_snapshots_unchanged(snapshots):
    for path, expected in sorted(snapshots.items(), key=lambda value: str(value[0])):
        if expected is None:
            if path.exists() or path.is_symlink():
                raise AuthoringReconciliationError(
                    f"Authority appeared during reconciliation: {path}"
                )
            continue
        if path.is_symlink() or not path.is_file() or sha256_file(path) != expected:
            raise AuthoringReconciliationError(
                f"Authority changed during reconciliation: {path}"
            )


def _json_inventory(root):
    if not root.is_dir() or root.is_symlink():
        raise AuthoringReconciliationError("Review bundle root is unavailable")
    return tuple(sorted(path.name for path in root.glob("*.json")))


def _require_contained_directory(root, path, label):
    root = Path(root).resolve()
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise AuthoringReconciliationError(f"{label} is unavailable: {path}")
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise AuthoringReconciliationError(
            f"{label} leaves its canonical root"
        ) from error
    return resolved


def _require_contained_file(root, path, label):
    root = Path(root).resolve()
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise AuthoringReconciliationError(f"{label} is unavailable: {path}")
    try:
        path.resolve().relative_to(root)
    except ValueError as error:
        raise AuthoringReconciliationError(
            f"{label} leaves its canonical root"
        ) from error


__all__ = [
    "AUTHORING_RECONCILIATION_SCHEMA",
    "AUTHORING_RECONCILIATION_VERSION",
    "AuthoringReconciliation",
    "AuthoringReconciliationError",
    "build_authoring_reconciliation",
    "load_authoring_reconciliation",
    "write_authoring_reconciliation",
]
