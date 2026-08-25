"""Read-only reconciliation of exact authoring review and generation authority."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

from vntts.authoring.bulk_generation import (
    _canonical_sha256,
    is_spoken_queue_item,
    load_generation_state,
)
from vntts.authoring.cohort_bundle import (
    COHORT_REVIEW_BUNDLE_SCHEMA,
    COHORT_REVIEW_BUNDLE_VERSION,
    cohort_review_progress_path,
    load_resumable_cohort_review_bundle,
)
from vntts.authoring.cohort_review import CohortReviewError, _write_document_no_replace
from vntts.authoring.source_reference_quality import (
    QUALITY_REVIEW_SCHEMA,
    QUALITY_REVIEW_VERSION,
    load_source_reference_quality_review,
)
from vntts.authoring.workbench import (
    _load_workspace,
    _safe_relative,
    _voice_readiness,
    _within,
    inspect_workspace,
)

AUTHORING_RECONCILIATION_SCHEMA = "vntts.authoring-authority-reconciliation"
AUTHORING_RECONCILIATION_VERSION = 1
_WORKSPACE_NAME = re.compile(r"resume-[0-9a-f]{24}-[0-9a-f]{16}")


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
                resume = load_resumable_cohort_review_bundle(path, persist=False)
            except Exception as error:
                raise AuthoringReconciliationError(
                    f"Current review bundle is invalid: {path.name}: {error}"
                ) from error
            progress = cohort_review_progress_path(path)
            progress_sha256 = None
            if progress.is_file():
                progress_payload = _read_bytes(progress, "review bundle progress")
                _remember_snapshot(snapshots, progress, progress_payload)
                progress_sha256 = hashlib.sha256(progress_payload).hexdigest()
            for source in resume.original.document["sources"]:
                workspace_paths.add(Path(source["workspace"]).resolve())
            for source in resume.current.document["sources"]:
                workspace_id = source["workspace_id"]
                for cohort in source["plan"]["cohorts"]:
                    for item in cohort["items"]:
                        key = (workspace_id, item["queue_id"])
                        bundle_workspace_queue_ids.setdefault(workspace_id, set()).add(
                            item["queue_id"]
                        )
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
            session = load_source_reference_quality_review(path)
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
        directory, workspace = _load_workspace(workspace_path)
        summary = inspect_workspace(directory)
        queue = VoiceGenerationQueue.load(summary.queue)
        queue_payload = _read_bytes(summary.queue, "workspace queue")
        _remember_snapshot(snapshots, summary.queue, queue_payload)
        configuration = directory / "workspace.json"
        configuration_payload = _read_bytes(configuration, "workspace configuration")
        _remember_snapshot(snapshots, configuration, configuration_payload)
        state = {"active": None, "items": {}}
        state_sha256 = None
        if summary.state is not None:
            state_payload = _read_bytes(summary.state, "workspace state")
            _remember_snapshot(snapshots, summary.state, state_payload)
            state_sha256 = hashlib.sha256(state_payload).hexdigest()
            state = load_generation_state(summary.state, summary.queue)
        manifest = summary.output / "manifest.json"
        manifest_sha256 = None
        if manifest.is_file():
            manifest_payload = _read_bytes(manifest, "generated manifest")
            _remember_snapshot(snapshots, manifest, manifest_payload)
            manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()

        candidates = []
        for item in queue.items:
            if item.action != "generate" or not is_spoken_queue_item(item):
                continue
            result = state["items"].get(item.queue_id)
            if not isinstance(result, dict):
                candidates.append(item)
        missing, _voice_reasons = _voice_readiness(
            workspace,
            candidates,
            set(),
            summary.voice_manifest,
            directory=directory,
        )
        missing = set(missing)
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
                if status == "approved" and review_status == "approved":
                    terminal_counts["approved"] += 1
                    occurrence_index.setdefault(item.queue_id, []).append(
                        (workspace["workspace_id"], "approved")
                    )
                    continue
                if status == "generated" and review_status == "rejected":
                    terminal_counts["rejected"] += 1
                    occurrence_index.setdefault(item.queue_id, []).append(
                        (workspace["workspace_id"], "rejected")
                    )
                    continue
                if isinstance(result.get("live_fallback"), dict):
                    terminal_counts["explicit_fallback"] += 1
                    occurrence_index.setdefault(item.queue_id, []).append(
                        (workspace["workspace_id"], "explicit_fallback")
                    )
                    continue
                if status == "generated" and review_status == "pending_review":
                    action = "review_plan_required"
                    bundle = bundle_actions.get(
                        (workspace["workspace_id"], item.queue_id)
                    )
                    if bundle is not None:
                        action = "human_cohort_review"
                    relative = _safe_relative(
                        result.get("path"), f"Pending review item {item.queue_id} WAV"
                    )
                    audio = _within(summary.output, relative, "Pending review WAV")
                    expected_audio_sha256 = result.get("file_sha256")
                    if not isinstance(expected_audio_sha256, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", expected_audio_sha256
                    ):
                        raise AuthoringReconciliationError(
                            f"Pending review item lacks WAV authority: {item.queue_id}"
                        )
                    audio_payload = _read_bytes(audio, "pending review WAV")
                    if (
                        hashlib.sha256(audio_payload).hexdigest()
                        != expected_audio_sha256
                    ):
                        raise AuthoringReconciliationError(
                            f"Pending review WAV changed: {item.queue_id}"
                        )
                    _remember_snapshot(snapshots, audio, audio_payload)
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
                    occurrence_index.setdefault(item.queue_id, []).append(
                        (workspace["workspace_id"], action)
                    )
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
                    occurrence_index.setdefault(item.queue_id, []).append(
                        (workspace["workspace_id"], action)
                    )
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
            occurrence_index.setdefault(item.queue_id, []).append(
                (workspace["workspace_id"], action)
            )
        workspace_reports.append(
            {
                "workspace": str(directory),
                "workspace_id": workspace["workspace_id"],
                "config_fingerprint": workspace["config_fingerprint"],
                "queue_sha256": hashlib.sha256(queue_payload).hexdigest(),
                "state_sha256": state_sha256,
                "manifest_sha256": manifest_sha256,
                "runtime_status": summary.runtime_status.value,
                "active": summary.active is not None,
                "report_scope": (
                    "complete_primary_workspace"
                    if scoped_queue_ids is None
                    else "current_bundle_items_only"
                ),
                "reported_queue_item_count": (
                    summary.eligible
                    if scoped_queue_ids is None
                    else len(scoped_queue_ids)
                ),
                "authoritative_counts": {
                    "eligible": summary.eligible,
                    "pending": summary.pending,
                    "generated": summary.generated,
                    "approved": summary.approved,
                    "rejected": summary.rejected,
                    "live_fallback": summary.live_fallback,
                    "failed": summary.failed,
                    "missing_voice": summary.missing_voice,
                },
                "terminal_counts": dict(sorted(terminal_counts.items())),
                "action_counts": dict(sorted(action_counts.items())),
            }
        )

    conflicts = []
    for queue_id, occurrences in sorted(occurrence_index.items()):
        terminal = {
            status
            for _workspace, status in occurrences
            if status in {"approved", "rejected"}
        }
        if len(terminal) > 1:
            conflicts.append(
                {
                    "queue_id": queue_id,
                    "reason": "parallel workspaces contain conflicting terminal decisions",
                    "occurrences": [
                        {"workspace_id": workspace_id, "authority": authority}
                        for workspace_id, authority in sorted(occurrences)
                    ],
                }
            )

    _assert_snapshots_unchanged(snapshots)
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
    report_id = _canonical_sha256(body)
    return AuthoringReconciliation(report_id, {**body, "report_id": report_id})


def write_authoring_reconciliation(report, output):
    """Publish one validated report without replacing an earlier handoff."""
    document = _validated_report(report)
    try:
        return _write_document_no_replace(output, document, "authoring reconciliation")
    except CohortReviewError as error:
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
    if (
        document.get("schema") != AUTHORING_RECONCILIATION_SCHEMA
        or document.get("schema_version") != AUTHORING_RECONCILIATION_VERSION
    ):
        raise AuthoringReconciliationError("Unsupported authoring reconciliation")
    report_id = document.get("report_id")
    if not isinstance(report_id, str) or not re.fullmatch(r"[0-9a-f]{64}", report_id):
        raise AuthoringReconciliationError("Authoring reconciliation ID is invalid")
    if (
        _canonical_sha256(
            {key: value for key, value in document.items() if key != "report_id"}
        )
        != report_id
    ):
        raise AuthoringReconciliationError("Authoring reconciliation content changed")
    return document


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
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise AuthoringReconciliationError(
            f"{label.capitalize()} is unavailable: {path}"
        )
    try:
        return path.read_bytes()
    except OSError as error:
        raise AuthoringReconciliationError(
            f"Unable to read {label}: {error}"
        ) from error


def _remember_snapshot(snapshots, path, payload):
    path = Path(path).resolve()
    digest = hashlib.sha256(payload).hexdigest()
    previous = snapshots.setdefault(path, digest)
    if previous != digest:
        raise AuthoringReconciliationError(f"Authority changed while reading: {path}")


def _assert_snapshots_unchanged(snapshots):
    for path, expected in sorted(snapshots.items(), key=lambda value: str(value[0])):
        if sha256_file(path) != expected:
            raise AuthoringReconciliationError(
                f"Authority changed during reconciliation: {path}"
            )


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
