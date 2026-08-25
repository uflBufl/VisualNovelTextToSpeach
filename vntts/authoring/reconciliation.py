"""Read-only reconciliation of exact authoring review and generation authority."""

from __future__ import annotations

import hashlib
import json
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
from vntts.authoring.reconciliation_schema import (
    AUTHORING_RECONCILIATION_SCHEMA,
    AUTHORING_RECONCILIATION_VERSION,
    AuthoringReconciliationSchemaError,
)
from vntts.authoring.reconciliation_schema import (
    SHA256_PATTERN as _SHA256,
)
from vntts.authoring.reconciliation_schema import (
    WORKSPACE_NAME_PATTERN as _WORKSPACE_NAME,
)
from vntts.authoring.reconciliation_schema import (
    validate_authoring_reconciliation_document as _validate_schema_document,
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
                    if not isinstance(
                        expected_audio_sha256, str
                    ) or not _SHA256.fullmatch(expected_audio_sha256):
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
    try:
        return _validate_schema_document(document)
    except AuthoringReconciliationSchemaError as error:
        raise AuthoringReconciliationError(str(error)) from error


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
