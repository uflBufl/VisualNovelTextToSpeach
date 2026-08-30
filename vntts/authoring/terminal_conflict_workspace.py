"""Apply completed terminal-conflict resolutions to immutable workspaces."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    assert_authority_snapshot,
    canonical_document_sha256,
    capture_authority_file,
)
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    ReviewAuthority,
    load_review_audio_bytes,
    process_is_alive,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.publication import (
    AtomicPublicationError,
    generation_publication_leases,
    rename_directory_no_replace,
)
from vntts.authoring.reconciliation_schema import (
    AuthoringReconciliationSchemaError,
    validate_authoring_reconciliation_document,
)
from vntts.authoring.terminal_conflict_resolution import (
    TerminalConflictResolutionError,
    assert_terminal_conflict_resolution_source_authorities,
    validate_terminal_conflict_resolution_document,
)
from vntts.authoring.terminal_conflict_review import (
    TerminalConflictReviewError,
    validate_terminal_conflict_review_document,
)
from vntts.authoring.terminal_conflict_successor import (
    APPLY_APPROVED_OUTCOME,
    NEW_REPAIR_HYPOTHESIS,
    RETAIN_EXPLICIT_REJECTION,
    TerminalConflictSuccessorError,
    validate_terminal_conflict_successor_document,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    WorkspaceCreationResult,
    contained_workspace_path,
    default_workspaces_root,
    load_workspace_authority,
    load_workspace_json,
    read_workspace_file_bytes,
    require_workspace_sha256,
    safe_workspace_relative_path,
    validate_workspace_provenance_extensions,
)
from vntts.authoring.workspace_config import workspace_config_fingerprint
from vntts.authoring.workspace_foundation import copy_workspace_tree_snapshot
from vntts.authoring.workspace_state import load_stable_workspace_generation_state


def merge_terminal_conflict_resolution(
    base_workspace,
    successor_directory,
    workspaces_root=None,
):
    """Create one immutable-config workspace from completed conflict choices."""
    successor_root = Path(successor_directory).expanduser().resolve()
    try:
        successor_snapshot = capture_authority_file(
            successor_root / "successor.json", "terminal conflict successor"
        )
        successor = validate_terminal_conflict_successor_document(
            successor_snapshot.json_document("terminal conflict successor"),
            successor_root,
        )
        resolution_path = Path(successor["terminal_resolution"]).resolve()
        resolution_root = resolution_path.parent
        resolution_snapshot = capture_authority_file(
            resolution_path, "terminal conflict resolution"
        )
        if resolution_snapshot.sha256 != successor["terminal_resolution_sha256"]:
            raise AuthoringWorkbenchError(
                "Terminal conflict successor resolution changed"
            )
        resolution = validate_terminal_conflict_resolution_document(
            resolution_snapshot.json_document("terminal conflict resolution"),
            resolution_root,
        )
        if (
            resolution["resolution_id"] != successor["terminal_resolution_id"]
            or assert_terminal_conflict_resolution_source_authorities(resolution_root)
            != resolution
        ):
            raise AuthoringWorkbenchError(
                "Terminal conflict successor resolution identity changed"
            )
        report_snapshot = capture_authority_file(
            successor["source_reconciliation"],
            "terminal conflict source reconciliation",
        )
        if report_snapshot.sha256 != successor["source_reconciliation_sha256"]:
            raise AuthoringWorkbenchError(
                "Terminal conflict source reconciliation changed"
            )
        report = validate_authoring_reconciliation_document(
            report_snapshot.json_document("terminal conflict source reconciliation")
        )
        review_snapshot = capture_authority_file(
            resolution["source_review"], "terminal conflict source review"
        )
        review = validate_terminal_conflict_review_document(
            review_snapshot.json_document("terminal conflict source review"),
            review_snapshot.path.parent,
        )
    except (
        AuthoringAuthorityError,
        AuthoringReconciliationSchemaError,
        TerminalConflictResolutionError,
        TerminalConflictReviewError,
        TerminalConflictSuccessorError,
    ) as error:
        raise AuthoringWorkbenchError(str(error)) from error

    if (
        report["report_id"] != successor["source_report_id"]
        or report["report_id"] != resolution["source_report_id"]
    ):
        raise AuthoringWorkbenchError(
            "Terminal conflict workspace sources have different reports"
        )
    records = successor["resolved_terminal_conflicts"]
    resolution_by_id = {item["queue_id"]: item for item in resolution["resolutions"]}
    report_conflicts = {item["queue_id"]: item for item in report["terminal_conflicts"]}
    if (
        {item["queue_id"] for item in records} != set(resolution_by_id)
        or set(resolution_by_id) != set(report_conflicts)
        or any(
            item["resolution"] != resolution_by_id[item["queue_id"]]
            or item["historical_conflict"] != report_conflicts[item["queue_id"]]
            for item in records
        )
    ):
        raise AuthoringWorkbenchError(
            "Terminal conflict successor no longer matches its exact sources"
        )
    if any(item["next_action"] == NEW_REPAIR_HYPOTHESIS for item in records):
        raise AuthoringWorkbenchError(
            "A neither-acceptable conflict requires a new repair hypothesis"
        )
    base_directory, base_document, base_workspace_sha256 = load_workspace_authority(
        base_workspace
    )
    if base_document["workspace_id"] != report["primary_workspace_id"]:
        raise AuthoringWorkbenchError(
            "Terminal conflict merge must use the reconciled primary workspace"
        )
    report_workspaces = {item["workspace_id"]: item for item in report["workspaces"]}
    base_report = report_workspaces.get(base_document["workspace_id"])
    if (
        base_report is None
        or Path(base_report["workspace"]).resolve() != base_directory
    ):
        raise AuthoringWorkbenchError(
            "Terminal conflict base workspace differs from its reconciliation"
        )
    base_queue, base_state, _base_payload, base_state_sha256 = (
        load_stable_workspace_generation_state(
            base_directory,
            base_document,
            "terminal conflict base",
            error_type=AuthoringWorkbenchError,
        )
    )
    base_queue_sha256 = sha256_file(base_directory / "queue.jsonl")
    if (
        base_report["state_sha256"] != base_state_sha256
        or base_report["queue_sha256"] != base_queue_sha256
    ):
        raise AuthoringWorkbenchError(
            "Terminal conflict base authority changed after reconciliation"
        )

    review_cases = {item["queue_id"]: item for item in review["cases"]}
    resolution_records = {item["queue_id"]: item for item in resolution["resolutions"]}
    selected_items = {}
    selected_audio = {}
    selected_snapshots = []
    source_directories = {base_directory}
    source_records = {}
    source_counts = Counter()
    ledgers = []
    for projected in records:
        queue_id = projected["queue_id"]
        resolved = resolution_records[queue_id]
        case = review_cases.get(queue_id)
        if case is None or resolved["selected_candidate_id"] is None:
            raise AuthoringWorkbenchError(
                f"Terminal conflict resolution is not applicable: {queue_id}"
            )
        candidate = next(
            (
                item
                for item in case["candidates"]
                if item["candidate_id"] == resolved["selected_candidate_id"]
            ),
            None,
        )
        if candidate is None:
            raise AuthoringWorkbenchError(
                f"Terminal conflict candidate disappeared: {queue_id}"
            )
        authorities = sorted(
            candidate["source_authorities"], key=lambda item: item["workspace_id"]
        )
        base_authorities = [
            item
            for item in authorities
            if item["workspace_id"] == base_document["workspace_id"]
        ]
        if base_authorities:
            source = base_authorities[0]
        elif len(authorities) == 1:
            source = authorities[0]
        elif (
            len({item["review_authority"]["item_sha256"] for item in authorities}) == 1
        ):
            source = authorities[0]
        else:
            raise AuthoringWorkbenchError(
                f"Selected conflict has ambiguous state provenance: {queue_id}"
            )
        source_record = report_workspaces.get(source["workspace_id"])
        if source_record is None:
            raise AuthoringWorkbenchError(
                f"Terminal conflict source workspace is unavailable: {queue_id}"
            )
        source_directory, source_document, source_workspace_sha256 = (
            load_workspace_authority(source_record["workspace"])
        )
        source_directories.add(source_directory)
        state_path = Path(source["state"]).resolve()
        queue_path = Path(source["queue"]).resolve()
        if (
            source_directory / "generated-audio/generation-state.json" != state_path
            or source_directory / "queue.jsonl" != queue_path
            or source_document["source"] != base_document["source"]
            or source_record["config_fingerprint"]
            != source_document["config_fingerprint"]
            or source_record["state_sha256"]
            != source["review_authority"]["state_sha256"]
            or source_record["queue_sha256"] != base_queue_sha256
        ):
            raise AuthoringWorkbenchError(
                f"Terminal conflict source authority is inconsistent: {queue_id}"
            )
        authority = ReviewAuthority(**source["review_authority"])
        try:
            audio_payload = load_review_audio_bytes(
                state_path, queue_path, queue_id, authority
            )
            state_snapshot = capture_authority_file(
                state_path, "terminal conflict source state"
            )
        except (AuthoringAuthorityError, BulkGenerationError) as error:
            raise AuthoringWorkbenchError(str(error)) from error
        if state_snapshot.sha256 != authority.state_sha256:
            raise AuthoringWorkbenchError(
                f"Terminal conflict source state changed: {queue_id}"
            )
        try:
            source_state = json.loads(state_snapshot.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AuthoringWorkbenchError(str(error)) from error
        source_item = source_state.get("items", {}).get(queue_id)
        if (
            not isinstance(source_item, dict)
            or canonical_document_sha256(source_item) != authority.item_sha256
            or source_item.get("file_sha256") != candidate["audio_sha256"]
        ):
            raise AuthoringWorkbenchError(
                f"Terminal conflict source item changed: {queue_id}"
            )
        expected_status = (
            ("approved", "approved")
            if projected["next_action"] == APPLY_APPROVED_OUTCOME
            else ("generated", "rejected")
        )
        if (
            projected["next_action"]
            not in {APPLY_APPROVED_OUTCOME, RETAIN_EXPLICIT_REJECTION}
            or (source_item.get("status"), source_item.get("review_status"))
            != expected_status
        ):
            raise AuthoringWorkbenchError(
                f"Terminal conflict selected authority changed: {queue_id}"
            )
        resolution_audio = contained_workspace_path(
            resolution_root,
            safe_workspace_relative_path(
                resolved["selected_audio"], "Terminal conflict resolution WAV"
            ),
            "Terminal conflict resolution WAV",
        )
        try:
            resolution_audio_snapshot = capture_authority_file(
                resolution_audio,
                "terminal conflict resolution WAV",
                root=resolution_root,
            )
        except AuthoringAuthorityError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        if (
            resolution_audio_snapshot.sha256 != candidate["audio_sha256"]
            or resolution_audio_snapshot.payload != audio_payload
        ):
            raise AuthoringWorkbenchError(
                f"Terminal conflict selected WAV changed: {queue_id}"
            )
        if "terminal_conflict_resolution" in source_item:
            raise AuthoringWorkbenchError(
                f"Terminal conflict source was already resolved: {queue_id}"
            )
        ledger = {
            "queue_id": queue_id,
            "source_workspace_id": source_document["workspace_id"],
            "source_state_sha256": state_snapshot.sha256,
            "source_item_sha256": canonical_document_sha256(source_item),
            "audio_sha256": resolution_audio_snapshot.sha256,
            "status": source_item["status"],
            "review_status": source_item["review_status"],
            "selected_candidate_id": candidate["candidate_id"],
            "next_action": projected["next_action"],
        }
        selected_items[queue_id] = copy.deepcopy(source_item)
        selected_audio[queue_id] = resolution_audio_snapshot
        ledgers.append(ledger)
        source_counts[source_document["workspace_id"]] += 1
        source_records[source_document["workspace_id"]] = {
            "workspace_id": source_document["workspace_id"],
            "config_fingerprint": source_document["config_fingerprint"],
            "state_sha256": state_snapshot.sha256,
            "terminal_item_count": 0,
        }
        selected_snapshots.extend(
            (
                state_snapshot,
                resolution_audio_snapshot,
                (source_directory / "workspace.json", source_workspace_sha256),
                (queue_path, base_queue_sha256),
            )
        )

    for workspace_id, count in source_counts.items():
        source_records[workspace_id]["terminal_item_count"] = count
    ledgers.sort(key=lambda item: item["queue_id"])
    merge = {
        "schema": "vntts.authoring-terminal-conflict-workspace-merge",
        "schema_version": 1,
        "base_workspace_id": base_document["workspace_id"],
        "base_state_sha256": base_state_sha256,
        "source_report_id": report["report_id"],
        "source_reconciliation_sha256": report_snapshot.sha256,
        "terminal_resolution_id": resolution["resolution_id"],
        "terminal_resolution_sha256": resolution_snapshot.sha256,
        "terminal_successor_id": successor["successor_id"],
        "terminal_successor_sha256": successor_snapshot.sha256,
        "sources": sorted(
            source_records.values(), key=lambda item: item["workspace_id"]
        ),
        "items": ledgers,
    }
    config_fingerprint = workspace_config_fingerprint(
        base_document["source"]["import_id"],
        base_document.get("story_index"),
        base_document.get("voice_manifest"),
        base_document["narrator_character"],
        base_document["run_config"],
        base_document.get("carry_forward"),
        base_document.get("outcome_merge"),
        base_document.get("failure_reference_binding"),
        merge,
        base_document.get("config_rebase"),
        base_document.get("audio_event_composition"),
        base_document.get("explicit_fallback_merge"),
        base_document.get("known_role_live_fallback"),
        base_document.get("audio_event_omission"),
        base_document.get("audio_event_projection_fallback"),
        base_document.get("reviewed_waveform_publication"),
        base_document.get("reviewed_rejection_live_fallback"),
        queue_extension=base_document.get("queue_extension"),
    )
    workspace_id = (
        f"resume-{base_document['source']['import_id'].removeprefix('legacy-')}-"
        f"{config_fingerprint[:16]}"
    )
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = contained_workspace_path(
        root, Path(workspace_id), "Conflict merge destination"
    )
    staging = Path(
        tempfile.mkdtemp(prefix=".conflict-merge-staging-", dir=root)
    ).resolve()
    base_snapshots = [
        (base_directory / "workspace.json", base_workspace_sha256),
        (
            base_directory / "generated-audio/generation-state.json",
            base_state_sha256,
        ),
    ]
    try:
        for tree_name in ("provenance", "inputs"):
            copy_workspace_tree_snapshot(
                base_directory / tree_name,
                staging / tree_name,
                base_snapshots,
                error_type=AuthoringWorkbenchError,
            )
        queue_payload = read_workspace_file_bytes(
            base_directory / "queue.jsonl", "terminal conflict base queue"
        )
        (staging / "queue.jsonl").write_bytes(queue_payload)
        base_snapshots.append((base_directory / "queue.jsonl", base_queue_sha256))
        output = staging / "generated-audio"
        output.mkdir()
        target_state = copy.deepcopy(base_state)
        path_owners = {}
        for queue_id, result in base_state["items"].items():
            if not isinstance(result, dict) or not isinstance(result.get("path"), str):
                continue
            relative = safe_workspace_relative_path(
                result["path"], f"Base generation item {queue_id!r} path"
            )
            owner = path_owners.setdefault(relative.as_posix(), queue_id)
            if owner != queue_id:
                raise AuthoringWorkbenchError(
                    f"Base generation WAV path collides with {owner!r}"
                )
            source_audio_path = contained_workspace_path(
                base_directory / "generated-audio",
                relative,
                "Base generation WAV",
            )
            payload = read_workspace_file_bytes(
                source_audio_path, "base generation WAV"
            )
            digest = hashlib.sha256(payload).hexdigest()
            if digest != require_workspace_sha256(
                result.get("file_sha256"),
                f"Base item {queue_id!r} WAV SHA-256",
            ):
                raise AuthoringWorkbenchError(
                    f"Base generation WAV changed for {queue_id!r}"
                )
            target = contained_workspace_path(
                output, relative, "Conflict merge base WAV"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            base_snapshots.append((source_audio_path, digest))
        for ledger in ledgers:
            queue_id = ledger["queue_id"]
            source_item = selected_items[queue_id]
            relative = safe_workspace_relative_path(
                source_item["path"], f"Conflict result {queue_id!r} path"
            )
            previous = target_state["items"].get(queue_id)
            previous_path = previous.get("path") if isinstance(previous, dict) else None
            if previous_path and previous_path != relative.as_posix():
                previous_target = contained_workspace_path(
                    output,
                    safe_workspace_relative_path(
                        previous_path, "Replaced conflict WAV"
                    ),
                    "Replaced conflict WAV",
                )
                if previous_target.is_file():
                    previous_target.unlink()
            owner = path_owners.get(relative.as_posix())
            if owner not in {None, queue_id}:
                raise AuthoringWorkbenchError(
                    f"Terminal conflict WAV path collides with {owner!r}"
                )
            target = contained_workspace_path(
                output, relative, "Resolved terminal conflict WAV"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(selected_audio[queue_id].payload)
            copied = copy.deepcopy(source_item)
            copied["terminal_conflict_resolution"] = {
                key: value for key, value in ledger.items() if key != "queue_id"
            }
            target_state["items"][queue_id] = copied
        atomic_write_json(
            output / "generation-state.json", target_state, sort_keys=True
        )
        workspace = copy.deepcopy(base_document)
        workspace.update(
            {
                "workspace_id": workspace_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "terminal_conflict_merge": merge,
                "config_fingerprint": config_fingerprint,
            }
        )
        atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
        try:
            write_generated_manifest_from_state(
                target_state,
                output,
                output / "manifest.json",
            )
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        import_snapshot = load_workspace_json(
            staging / "provenance/import.json", "conflict merge import snapshot"
        )
        validate_workspace_provenance_extensions(staging, workspace, import_snapshot)
        try:
            source_lease_context = generation_publication_leases(
                (
                    (source_directory / "generated-audio", base_queue_sha256)
                    for source_directory in source_directories
                ),
                process_checker=process_is_alive,
            )
            with source_lease_context as held_leases:
                for source_directory in sorted(source_directories):
                    source_output = source_directory / "generated-audio"
                    if any(source_output.rglob("*.partial.wav")):
                        raise AuthoringWorkbenchError(
                            "Terminal conflict source became active before publication"
                        )
                for path, digest in base_snapshots:
                    if not path.is_file() or sha256_file(path) != digest:
                        raise AuthoringWorkbenchError(
                            "Terminal conflict base changed before workspace publication"
                        )
                for snapshot in selected_snapshots:
                    if hasattr(snapshot, "payload"):
                        assert_authority_snapshot(snapshot, "terminal conflict source")
                    else:
                        path, digest = snapshot
                        if not path.is_file() or sha256_file(path) != digest:
                            raise AuthoringWorkbenchError(
                                "Terminal conflict source changed before workspace publication"
                            )
                assert_authority_snapshot(
                    successor_snapshot, "terminal conflict successor"
                )
                assert_authority_snapshot(
                    resolution_snapshot, "terminal conflict resolution"
                )
                assert_authority_snapshot(
                    report_snapshot, "terminal conflict source reconciliation"
                )
                assert_authority_snapshot(
                    review_snapshot, "terminal conflict source review"
                )
                if (
                    assert_terminal_conflict_resolution_source_authorities(
                        resolution_root
                    )
                    != resolution
                ):
                    raise AuthoringWorkbenchError(
                        "Terminal conflict sources changed before workspace publication"
                    )
                for lease in held_leases:
                    lease.assert_owned()
                if destination.exists():
                    _directory, existing, _workspace_sha256 = load_workspace_authority(
                        destination
                    )
                    if existing.get("terminal_conflict_merge") != merge:
                        raise AuthoringWorkbenchError(
                            "Terminal conflict workspace conflicts with another resolution"
                        )
                    return WorkspaceCreationResult(destination, False)
                try:
                    rename_directory_no_replace(staging, destination)
                except (AtomicPublicationError, OSError) as error:
                    if destination.exists():
                        _directory, existing, _workspace_sha256 = (
                            load_workspace_authority(destination)
                        )
                        if existing.get("terminal_conflict_merge") == merge:
                            for lease in held_leases:
                                lease.mark_committed()
                            return WorkspaceCreationResult(destination, False)
                    raise AuthoringWorkbenchError(
                        f"Unable to publish terminal conflict workspace: {error}"
                    ) from error
                for lease in held_leases:
                    lease.mark_committed()
                staging = None
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(
                f"Terminal conflict source became active before publication: {error}"
            ) from error
    except (AuthoringAuthorityError, OSError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return WorkspaceCreationResult(destination, True)


__all__ = ["merge_terminal_conflict_resolution"]
