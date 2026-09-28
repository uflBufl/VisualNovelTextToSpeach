"""Apply completed terminal-conflict resolutions to immutable workspaces."""

from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    AuthoritySnapshot,
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
    staged_directory,
)
from vntts.authoring.reconciliation_schema import (
    AuthoringReconciliationSchemaError,
    validate_authoring_reconciliation_document,
)
from vntts.authoring.terminal_conflict_records import (
    TerminalConflictWorkspaceLedger,
    TerminalConflictWorkspaceSource,
)
from vntts.authoring.terminal_conflict_resolution import (
    TerminalConflictResolutionDocument,
    TerminalConflictResolutionError,
    assert_terminal_conflict_resolution_source_authorities,
    validate_terminal_conflict_resolution_document,
)
from vntts.authoring.terminal_conflict_review import (
    TerminalConflictReviewDocument,
    TerminalConflictReviewError,
    validate_terminal_conflict_review_document,
)
from vntts.authoring.terminal_conflict_successor import (
    APPLY_APPROVED_OUTCOME,
    NEW_REPAIR_HYPOTHESIS,
    RETAIN_EXPLICIT_REJECTION,
    TerminalConflictSuccessorDocument,
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
    safe_workspace_relative_path,
    validate_workspace_provenance_extensions,
)
from vntts.authoring.workspace_config import (
    workspace_id_for_config,
    workspace_successor_config_fingerprint,
)
from vntts.authoring.workspace_foundation import (
    copy_generation_wavs,
    copy_workspace_tree_snapshot,
)
from vntts.authoring.workspace_state import load_stable_workspace_generation_state


def merge_terminal_conflict_resolution(
    base_workspace: str | Path,
    successor_directory: str | Path,
    workspaces_root: str | Path | None = None,
) -> WorkspaceCreationResult:
    """Create one immutable-config workspace from completed conflict choices."""
    inputs = _load_terminal_conflict_merge_inputs(base_workspace, successor_directory)
    selected = _select_terminal_conflict_sources(inputs)
    merge = {
        "schema": "vntts.authoring-terminal-conflict-workspace-merge",
        "schema_version": 1,
        "base_workspace_id": inputs.base_workspace_id,
        "base_state_sha256": inputs.base_state_sha256,
        "source_report_id": inputs.report["report_id"],
        "source_reconciliation_sha256": inputs.report_snapshot.sha256,
        "terminal_resolution_id": inputs.resolution["resolution_id"],
        "terminal_resolution_sha256": inputs.resolution_snapshot.sha256,
        "terminal_successor_id": inputs.successor["successor_id"],
        "terminal_successor_sha256": inputs.successor_snapshot.sha256,
        "sources": selected.sources,
        "items": selected.ledgers,
    }
    base_source = inputs.base_document.get("source")
    if not isinstance(base_source, dict):
        raise AuthoringWorkbenchError("Terminal conflict base source is malformed")
    import_id = _record_text(base_source, "import_id")
    config_fingerprint = workspace_successor_config_fingerprint(
        inputs.base_document,
        import_id,
        overlays={"terminal_conflict_merge": merge},
    )
    workspace_id = workspace_id_for_config(import_id, config_fingerprint)
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = contained_workspace_path(
        root, Path(workspace_id), "Conflict merge destination"
    )
    try:
        with staged_directory(root, prefix=".conflict-merge-staging-") as staging:
            base_snapshots = _stage_terminal_conflict_workspace(
                staging, inputs, selected, merge, workspace_id, config_fingerprint
            )
            return _publish_staged_terminal_conflict_workspace(
                _TerminalConflictPublication(
                    staging, destination, merge, selected, base_snapshots, inputs
                )
            )
    except (AuthoringAuthorityError, OSError) as error:
        raise AuthoringWorkbenchError(str(error)) from error


@dataclass(frozen=True)
class _TerminalConflictSelection:
    """Validated source material owned by the merge operation."""

    items: dict[str, dict[str, object]]
    audio: dict[str, AuthoritySnapshot]
    snapshots: list[AuthoritySnapshot | tuple[Path, str]]
    directories: set[Path]
    sources: list[TerminalConflictWorkspaceSource]
    ledgers: list[TerminalConflictWorkspaceLedger]


@dataclass(frozen=True)
class _TerminalConflictMergeInputs:
    """Validated authority inputs owned by one merge operation."""

    successor_snapshot: AuthoritySnapshot
    successor: TerminalConflictSuccessorDocument
    resolution_root: Path
    resolution_snapshot: AuthoritySnapshot
    resolution: TerminalConflictResolutionDocument
    report_snapshot: AuthoritySnapshot
    report: dict[str, object]
    review_snapshot: AuthoritySnapshot
    review: TerminalConflictReviewDocument
    base_directory: Path
    base_document: dict[str, object]
    base_workspace_sha256: str
    base_workspace_id: str
    base_state: dict[str, object]
    base_state_sha256: str
    base_queue_sha256: str
    report_workspaces: dict[str, dict[str, object]]


@dataclass(frozen=True)
class _TerminalConflictPublication:
    """Staged output and immutable authority contract for lease-bound publication."""

    staging: Path
    destination: Path
    merge: dict[str, object]
    selected: _TerminalConflictSelection
    base_snapshots: list[tuple[Path, str]]
    inputs: _TerminalConflictMergeInputs


def _load_terminal_conflict_merge_inputs(
    base_workspace: str | Path, successor_directory: str | Path
) -> _TerminalConflictMergeInputs:
    """Load one exact successor, its review chain, and the reconciled base workspace."""
    successor_snapshot, successor = _load_terminal_conflict_successor(
        successor_directory
    )
    resolution_root, resolution_snapshot, resolution = _load_terminal_resolution(
        successor
    )
    report_snapshot, report, review_snapshot, review = _load_terminal_review_chain(
        successor, resolution
    )
    _assert_terminal_conflict_chain(successor, resolution, report)
    return _load_terminal_conflict_base(
        base_workspace,
        successor_snapshot,
        successor,
        resolution_root,
        resolution_snapshot,
        resolution,
        report_snapshot,
        report,
        review_snapshot,
        review,
    )


def _load_terminal_conflict_successor(
    successor_directory: str | Path,
) -> tuple[AuthoritySnapshot, TerminalConflictSuccessorDocument]:
    try:
        successor_root = Path(successor_directory).expanduser().resolve()
        snapshot = capture_authority_file(
            successor_root / "successor.json", "terminal conflict successor"
        )
        return snapshot, validate_terminal_conflict_successor_document(
            snapshot.json_document("terminal conflict successor"), successor_root
        )
    except (AuthoringAuthorityError, TerminalConflictSuccessorError) as error:
        raise AuthoringWorkbenchError(str(error)) from error


def _load_terminal_resolution(
    successor: TerminalConflictSuccessorDocument,
) -> tuple[Path, AuthoritySnapshot, TerminalConflictResolutionDocument]:
    try:
        resolution_path = Path(successor["terminal_resolution"]).resolve()
        resolution_root = resolution_path.parent
        snapshot = capture_authority_file(
            resolution_path, "terminal conflict resolution"
        )
        if snapshot.sha256 != successor["terminal_resolution_sha256"]:
            raise AuthoringWorkbenchError(
                "Terminal conflict successor resolution changed"
            )
        resolution = validate_terminal_conflict_resolution_document(
            snapshot.json_document("terminal conflict resolution"), resolution_root
        )
        if (
            resolution["resolution_id"] != successor["terminal_resolution_id"]
            or assert_terminal_conflict_resolution_source_authorities(resolution_root)
            != resolution
        ):
            raise AuthoringWorkbenchError(
                "Terminal conflict successor resolution identity changed"
            )
        return resolution_root, snapshot, resolution
    except (AuthoringAuthorityError, TerminalConflictResolutionError) as error:
        raise AuthoringWorkbenchError(str(error)) from error


def _load_terminal_review_chain(
    successor: TerminalConflictSuccessorDocument,
    resolution: TerminalConflictResolutionDocument,
) -> tuple[
    AuthoritySnapshot,
    dict[str, object],
    AuthoritySnapshot,
    TerminalConflictReviewDocument,
]:
    try:
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
        return report_snapshot, report, review_snapshot, review
    except (
        AuthoringAuthorityError,
        AuthoringReconciliationSchemaError,
        TerminalConflictReviewError,
    ) as error:
        raise AuthoringWorkbenchError(str(error)) from error


def _assert_terminal_conflict_chain(
    successor: TerminalConflictSuccessorDocument,
    resolution: TerminalConflictResolutionDocument,
    report: dict[str, object],
) -> None:
    if (
        report["report_id"] != successor["source_report_id"]
        or report["report_id"] != resolution["source_report_id"]
    ):
        raise AuthoringWorkbenchError(
            "Terminal conflict workspace sources have different reports"
        )
    records = successor["resolved_terminal_conflicts"]
    resolution_by_id = {item["queue_id"]: item for item in resolution["resolutions"]}
    report_conflicts = {
        item["queue_id"]: item
        for item in _object_records(report["terminal_conflicts"], "terminal conflicts")
    }
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


def _load_terminal_conflict_base(
    base_workspace: str | Path,
    successor_snapshot: AuthoritySnapshot,
    successor: TerminalConflictSuccessorDocument,
    resolution_root: Path,
    resolution_snapshot: AuthoritySnapshot,
    resolution: TerminalConflictResolutionDocument,
    report_snapshot: AuthoritySnapshot,
    report: dict[str, object],
    review_snapshot: AuthoritySnapshot,
    review: TerminalConflictReviewDocument,
) -> _TerminalConflictMergeInputs:
    base_directory, base_document, base_workspace_sha256 = load_workspace_authority(
        base_workspace
    )
    base_workspace_id = _record_text(base_document, "workspace_id")
    if base_workspace_id != report["primary_workspace_id"]:
        raise AuthoringWorkbenchError(
            "Terminal conflict merge must use the reconciled primary workspace"
        )
    report_workspaces = {
        _record_text(item, "workspace_id"): item
        for item in _object_records(report["workspaces"], "workspaces")
    }
    base_report = report_workspaces.get(base_workspace_id)
    if (
        base_report is None
        or Path(_record_text(base_report, "workspace")).resolve() != base_directory
    ):
        raise AuthoringWorkbenchError(
            "Terminal conflict base workspace differs from its reconciliation"
        )
    _base_queue, base_state, _base_payload, base_state_sha256 = (
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
    return _TerminalConflictMergeInputs(
        successor_snapshot,
        successor,
        resolution_root,
        resolution_snapshot,
        resolution,
        report_snapshot,
        report,
        review_snapshot,
        review,
        base_directory,
        base_document,
        base_workspace_sha256,
        base_workspace_id,
        base_state,
        base_state_sha256,
        base_queue_sha256,
        report_workspaces,
    )


def _select_terminal_conflict_sources(
    inputs: _TerminalConflictMergeInputs,
) -> _TerminalConflictSelection:
    """Select and snapshot one unambiguous source for every resolved conflict."""
    review_cases = {item["queue_id"]: item for item in inputs.review["cases"]}
    resolution_records = {
        item["queue_id"]: item for item in inputs.resolution["resolutions"]
    }
    items: dict[str, dict[str, object]] = {}
    audio: dict[str, AuthoritySnapshot] = {}
    snapshots: list[AuthoritySnapshot | tuple[Path, str]] = []
    directories = {inputs.base_directory}
    source_records: dict[str, TerminalConflictWorkspaceSource] = {}
    source_counts: Counter[str] = Counter()
    ledgers: list[TerminalConflictWorkspaceLedger] = []
    for projected in inputs.successor["resolved_terminal_conflicts"]:
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
            if item["workspace_id"] == inputs.base_workspace_id
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
        source_record = inputs.report_workspaces.get(source["workspace_id"])
        if source_record is None:
            raise AuthoringWorkbenchError(
                f"Terminal conflict source workspace is unavailable: {queue_id}"
            )
        source_directory, source_document, source_workspace_sha256 = (
            load_workspace_authority(_record_text(source_record, "workspace"))
        )
        source_workspace_id = _record_text(source_document, "workspace_id")
        source_config_fingerprint = _record_text(source_document, "config_fingerprint")
        directories.add(source_directory)
        state_path = Path(source["state"]).resolve()
        queue_path = Path(source["queue"]).resolve()
        if (
            source_directory / "generated-audio/generation-state.json" != state_path
            or source_directory / "queue.jsonl" != queue_path
            or source_document["source"] != inputs.base_document["source"]
            or source_record["config_fingerprint"] != source_config_fingerprint
            or source_record["state_sha256"]
            != source["review_authority"]["state_sha256"]
            or source_record["queue_sha256"] != inputs.base_queue_sha256
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
            inputs.resolution_root,
            safe_workspace_relative_path(
                resolved["selected_audio"], "Terminal conflict resolution WAV"
            ),
            "Terminal conflict resolution WAV",
        )
        try:
            resolution_audio_snapshot = capture_authority_file(
                resolution_audio,
                "terminal conflict resolution WAV",
                root=inputs.resolution_root,
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
        ledger: TerminalConflictWorkspaceLedger = {
            "queue_id": queue_id,
            "source_workspace_id": source_workspace_id,
            "source_state_sha256": state_snapshot.sha256,
            "source_item_sha256": canonical_document_sha256(source_item),
            "audio_sha256": resolution_audio_snapshot.sha256,
            "status": source_item["status"],
            "review_status": source_item["review_status"],
            "selected_candidate_id": candidate["candidate_id"],
            "next_action": projected["next_action"],
        }
        items[queue_id] = copy.deepcopy(source_item)
        audio[queue_id] = resolution_audio_snapshot
        ledgers.append(ledger)
        source_counts[source_workspace_id] += 1
        source_records[source_workspace_id] = {
            "workspace_id": source_workspace_id,
            "config_fingerprint": source_config_fingerprint,
            "state_sha256": state_snapshot.sha256,
            "terminal_item_count": 0,
        }
        snapshots.extend(
            (
                state_snapshot,
                resolution_audio_snapshot,
                (source_directory / "workspace.json", source_workspace_sha256),
                (queue_path, inputs.base_queue_sha256),
            )
        )
    for workspace_id, count in source_counts.items():
        source_records[workspace_id]["terminal_item_count"] = count
    return _TerminalConflictSelection(
        items,
        audio,
        snapshots,
        directories,
        sorted(source_records.values(), key=lambda item: item["workspace_id"]),
        sorted(ledgers, key=lambda item: item["queue_id"]),
    )


def _stage_terminal_conflict_workspace(
    staging: Path,
    inputs: _TerminalConflictMergeInputs,
    selected: _TerminalConflictSelection,
    merge: dict[str, object],
    workspace_id: str,
    config_fingerprint: str,
) -> list[tuple[Path, str]]:
    """Materialize the immutable workspace before any source lease is acquired."""
    base_snapshots = [
        (inputs.base_directory / "workspace.json", inputs.base_workspace_sha256),
        (
            inputs.base_directory / "generated-audio/generation-state.json",
            inputs.base_state_sha256,
        ),
    ]
    for tree_name in ("provenance", "inputs"):
        copy_workspace_tree_snapshot(
            inputs.base_directory / tree_name,
            staging / tree_name,
            base_snapshots,
            error_type=AuthoringWorkbenchError,
        )
    queue_payload = read_workspace_file_bytes(
        inputs.base_directory / "queue.jsonl", "terminal conflict base queue"
    )
    (staging / "queue.jsonl").write_bytes(queue_payload)
    base_snapshots.append(
        (inputs.base_directory / "queue.jsonl", inputs.base_queue_sha256)
    )
    output = staging / "generated-audio"
    output.mkdir()
    target_state = copy.deepcopy(inputs.base_state)
    _generation_state_items(inputs.base_state)
    path_owners = copy_generation_wavs(
        inputs.base_directory,
        output,
        inputs.base_state,
        base_snapshots,
        "Conflict merge base WAV",
        AuthoringWorkbenchError,
        source_label="Base generation WAV",
    )
    for ledger in selected.ledgers:
        queue_id = ledger["queue_id"]
        source_item = selected.items[queue_id]
        relative = safe_workspace_relative_path(
            source_item["path"], f"Conflict result {queue_id!r} path"
        )
        previous = _generation_state_items(target_state).get(queue_id)
        previous_path = previous.get("path") if isinstance(previous, dict) else None
        if previous_path and previous_path != relative.as_posix():
            previous_target = contained_workspace_path(
                output,
                safe_workspace_relative_path(previous_path, "Replaced conflict WAV"),
                "Replaced conflict WAV",
            )
            if previous_target.is_file():
                previous_target.unlink()
        conflict_owner = path_owners.get(relative.as_posix())
        if conflict_owner not in {None, queue_id}:
            raise AuthoringWorkbenchError(
                f"Terminal conflict WAV path collides with {conflict_owner!r}"
            )
        path_owners[relative.as_posix()] = queue_id
        target = contained_workspace_path(
            output, relative, "Resolved terminal conflict WAV"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(selected.audio[queue_id].payload)
        copied = copy.deepcopy(source_item)
        copied["terminal_conflict_resolution"] = {
            key: value for key, value in ledger.items() if key != "queue_id"
        }
        _generation_state_items(target_state)[queue_id] = copied
    atomic_write_json(output / "generation-state.json", target_state, sort_keys=True)
    workspace = copy.deepcopy(inputs.base_document)
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
            target_state, output, output / "manifest.json"
        )
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(str(error)) from error
    import_snapshot = load_workspace_json(
        staging / "provenance/import.json", "conflict merge import snapshot"
    )
    validate_workspace_provenance_extensions(staging, workspace, import_snapshot)
    return base_snapshots


def _publish_staged_terminal_conflict_workspace(
    publication: _TerminalConflictPublication,
) -> WorkspaceCreationResult:
    """Revalidate immutable inputs under source leases and atomically publish staging."""
    try:
        source_lease_context = generation_publication_leases(
            (
                (
                    source_directory / "generated-audio",
                    publication.inputs.base_queue_sha256,
                )
                for source_directory in publication.selected.directories
            ),
            process_checker=process_is_alive,
        )
        with source_lease_context as held_leases:
            for source_directory in sorted(publication.selected.directories):
                source_output = source_directory / "generated-audio"
                if any(source_output.rglob("*.partial.wav")):
                    raise AuthoringWorkbenchError(
                        "Terminal conflict source became active before publication"
                    )
            for path, digest in publication.base_snapshots:
                if not path.is_file() or sha256_file(path) != digest:
                    raise AuthoringWorkbenchError(
                        "Terminal conflict base changed before workspace publication"
                    )
            for snapshot in publication.selected.snapshots:
                if isinstance(snapshot, AuthoritySnapshot):
                    assert_authority_snapshot(snapshot, "terminal conflict source")
                else:
                    path, digest = snapshot
                    if not path.is_file() or sha256_file(path) != digest:
                        raise AuthoringWorkbenchError(
                            "Terminal conflict source changed before workspace publication"
                        )
            assert_authority_snapshot(
                publication.inputs.successor_snapshot, "terminal conflict successor"
            )
            assert_authority_snapshot(
                publication.inputs.resolution_snapshot, "terminal conflict resolution"
            )
            assert_authority_snapshot(
                publication.inputs.report_snapshot,
                "terminal conflict source reconciliation",
            )
            assert_authority_snapshot(
                publication.inputs.review_snapshot, "terminal conflict source review"
            )
            if (
                assert_terminal_conflict_resolution_source_authorities(
                    publication.inputs.resolution_root
                )
                != publication.inputs.resolution
            ):
                raise AuthoringWorkbenchError(
                    "Terminal conflict sources changed before workspace publication"
                )
            for lease in held_leases:
                lease.assert_owned()
            if publication.destination.exists():
                _directory, existing, _workspace_sha256 = load_workspace_authority(
                    publication.destination
                )
                if existing.get("terminal_conflict_merge") != publication.merge:
                    raise AuthoringWorkbenchError(
                        "Terminal conflict workspace conflicts with another resolution"
                    )
                return WorkspaceCreationResult(publication.destination, False)
            try:
                rename_directory_no_replace(
                    publication.staging, publication.destination
                )
            except (AtomicPublicationError, OSError) as error:
                if publication.destination.exists():
                    _directory, existing, _workspace_sha256 = load_workspace_authority(
                        publication.destination
                    )
                    if existing.get("terminal_conflict_merge") == publication.merge:
                        for lease in held_leases:
                            lease.mark_committed()
                        return WorkspaceCreationResult(publication.destination, False)
                raise AuthoringWorkbenchError(
                    f"Unable to publish terminal conflict workspace: {error}"
                ) from error
            for lease in held_leases:
                lease.mark_committed()
    except BulkGenerationError as error:
        raise AuthoringWorkbenchError(
            f"Terminal conflict source became active before publication: {error}"
        ) from error
    return WorkspaceCreationResult(publication.destination, True)


def _object_records(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise AuthoringWorkbenchError(f"Terminal conflict {label} are malformed")
    return value


def _generation_state_items(state: dict[str, object]) -> dict[str, object]:
    items = state.get("items")
    if not isinstance(items, dict):
        raise AuthoringWorkbenchError("Generation state items are malformed")
    return items


def _record_text(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise AuthoringWorkbenchError(f"Terminal conflict record {field} is malformed")
    return value


__all__ = ["merge_terminal_conflict_resolution"]
