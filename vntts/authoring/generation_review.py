"""Exact human-review snapshots and leased state/manifest transactions."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias, TypeGuard

from durable_file import atomic_write_json

from vntts.authoring.authority import canonical_document_sha256 as _canonical_sha256
from vntts.authoring.generation_lease import (
    BulkGenerationError,
    BulkGenerationSourceChangedError,
    GenerationLease,
    process_is_alive,
)
from vntts.authoring.generation_manifest import (
    approved_manifest_entries as _approved_manifest_entries,
)
from vntts.authoring.generation_manifest import (
    validate_success_file as _validate_success_file,
)
from vntts.authoring.generation_manifest import (
    write_generated_manifest_from_state as _write_generated_manifest_from_state,
)
from vntts.authoring.generation_state import (
    StateItems,
    load_generation_state,
    validate_generation_state_document,
)
from vntts.authoring.generation_state import contained_state_path as _within
from vntts.authoring.generation_state import generation_state_items as _state_items
from vntts.authoring.generation_state import required_state_text as _generation_text
from vntts.authoring.generation_state import safe_state_relative_path as _safe_relative
from vntts.authoring.publication import publication_errors
from vntts.authoring.workspace_foundation import load_json_object_snapshot
from vntts.document_identity import sha256_file
from vntts.json_types import decode_json
from vntts.json_types import is_json_object as _is_json_document
from vntts.path_safety import read_regular_bytes

JsonDocument: TypeAlias = dict[str, object]
_GenerationLease = GenerationLease


def _is_review_authorities(
    value: object,
) -> TypeGuard[dict[str, ReviewAuthority]]:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(queue_id, str) and isinstance(authority, ReviewAuthority)
            for queue_id, authority in value.items()
        )
    )


def _is_review_decisions(value: object) -> TypeGuard[dict[str, str]]:
    return isinstance(value, dict) and all(
        isinstance(queue_id, str) and isinstance(decision, str)
        for queue_id, decision in value.items()
    )


@dataclass(frozen=True)
class ReviewAuthority:
    """Exact immutable inputs that one human review decision applies to."""

    queue_sha256: str
    state_sha256: str
    item_sha256: str
    audio_sha256: str


@dataclass(frozen=True)
class ReviewCommit:
    """Durable result of one compare-and-swap review decision."""

    queue_id: str
    status: str
    review_status: str
    updated_at: str
    authority: ReviewAuthority


def generation_review_authority(
    state_path: str | Path, queue_id: str
) -> ReviewAuthority:
    """Snapshot one reviewable state item and its exact validated WAV."""
    authority = generation_review_authorities(state_path, (queue_id,)).get(queue_id)
    if authority is None:
        raise BulkGenerationError(f"Generated queue item does not exist: {queue_id}")
    return authority


@publication_errors(BulkGenerationError)
def generation_review_authorities(
    state_path: str | Path, queue_ids: Sequence[object]
) -> dict[str, ReviewAuthority]:
    """Snapshot several generated WAV authorities from one immutable state read."""
    state_path = Path(state_path).expanduser().resolve()
    selected_queue_ids = tuple(
        sorted({_generation_text(value, "Queue ID") for value in queue_ids})
    )
    if not selected_queue_ids:
        return {}
    document, state_sha256, _payload = load_json_object_snapshot(
        state_path, "generation state", error_type=BulkGenerationError
    )
    state = validate_generation_state_document(document, state_path.parent, None, None)
    items = _state_items(state)
    authorities: dict[str, ReviewAuthority] = {}
    for queue_id in selected_queue_ids:
        item = items.get(queue_id)
        if item is None or item.get("status") not in {
            "generated",
            "approved",
        }:
            raise BulkGenerationError(
                f"Generated queue item does not exist: {queue_id}"
            )
        relative = _safe_relative(item.get("path"), f"State item {queue_id!r} path")
        audio = _within(state_path.parent, relative, "Generated WAV")
        _validate_success_file(queue_id, item, audio)
        authorities[queue_id] = ReviewAuthority(
            queue_sha256=_generation_text(state.get("queue_sha256"), "Queue SHA-256"),
            state_sha256=state_sha256,
            item_sha256=_canonical_sha256(item),
            audio_sha256=sha256_file(audio),
        )
    if sha256_file(state_path) != state_sha256:
        raise BulkGenerationSourceChangedError(
            "Generation state changed while review authorities were captured"
        )
    return authorities


def _assert_review_authority(
    state_path: str | Path,
    queue_id: str,
    expected_authority: object,
    queue_path: str | Path | None,
) -> tuple[JsonDocument, JsonDocument, bytes]:
    if not isinstance(expected_authority, ReviewAuthority):
        raise BulkGenerationError("Review authority snapshot is invalid")
    state, item, audio_bytes = _load_review_snapshot(
        state_path,
        queue_id,
        expected_authority,
        queue_path,
        capture_audio=True,
    )
    actual = ReviewAuthority(
        queue_sha256=_generation_text(state.get("queue_sha256"), "Queue SHA-256"),
        state_sha256=expected_authority.state_sha256,
        item_sha256=_canonical_sha256(item),
        audio_sha256=hashlib.sha256(audio_bytes).hexdigest(),
    )
    if actual != expected_authority:
        raise BulkGenerationError(
            "Review authority changed after the item was displayed; refresh before deciding"
        )
    return state, item, audio_bytes


def assert_review_authorities(
    state_path: str | Path,
    authorities: object,
    queue_path: str | Path,
) -> tuple[JsonDocument, dict[str, tuple[JsonDocument, bytes]]]:
    """Validate one cohort against one shared state and queue snapshot."""
    if not _is_review_authorities(authorities):
        raise BulkGenerationError("Cohort review authorities must be a non-empty map")
    state_sha256, queue_sha256 = _cohort_review_snapshot(authorities)
    state_path = Path(state_path).expanduser().resolve()
    queue_path = Path(queue_path).expanduser().resolve()
    state = _read_review_state(state_path, queue_path, state_sha256, queue_sha256)
    snapshots, audio_paths = _cohort_review_audio_snapshots(
        state, state_path, authorities
    )
    _assert_cohort_review_snapshot_stable(
        state_path, queue_path, state_sha256, queue_sha256, authorities, audio_paths
    )
    return state, snapshots


def _cohort_review_snapshot(authorities: object) -> tuple[str, str]:
    if not isinstance(authorities, dict) or not authorities:
        raise BulkGenerationError("Cohort review authorities must be a non-empty map")
    if any(not isinstance(value, ReviewAuthority) for value in authorities.values()):
        raise BulkGenerationError("Cohort review authority snapshot is invalid")
    state = {value.state_sha256 for value in authorities.values()}
    queue = {value.queue_sha256 for value in authorities.values()}
    if len(state) != 1 or len(queue) != 1:
        raise BulkGenerationError("Cohort review authorities do not share one snapshot")
    return next(iter(state)), next(iter(queue))


def _read_review_state(
    state_path: Path, queue_path: Path, state_sha256: str, queue_sha256: str
) -> JsonDocument:
    try:
        state_payload = read_regular_bytes(state_path)
        queue_payload = read_regular_bytes(queue_path)
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to read cohort review controls: {error}"
        ) from error
    if hashlib.sha256(state_payload).hexdigest() != state_sha256:
        raise BulkGenerationError(
            "Review authority changed after the cohort was displayed; refresh before deciding"
        )
    if hashlib.sha256(queue_payload).hexdigest() != queue_sha256:
        raise BulkGenerationError(
            "Review queue changed after the cohort was displayed; refresh before deciding"
        )
    return _review_state_document(state_payload, state_path, queue_sha256)


def _review_state_document(
    payload: bytes, state_path: Path, queue_sha256: str
) -> JsonDocument:
    try:
        state = decode_json(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BulkGenerationError(
            f"Unable to read generation state {state_path}: {error}"
        ) from error
    if (
        not _is_json_document(state)
        or not isinstance(state.get("items"), dict)
        or state.get("queue_sha256") != queue_sha256
    ):
        raise BulkGenerationError("Generation state items or queue identity changed")
    return state


def _cohort_review_audio_snapshots(
    state: JsonDocument, state_path: Path, authorities: Mapping[str, ReviewAuthority]
) -> tuple[dict[str, tuple[JsonDocument, bytes]], dict[str, Path]]:
    snapshots: dict[str, tuple[JsonDocument, bytes]] = {}
    paths: dict[str, Path] = {}
    items = _state_items(state)
    for raw_queue_id, authority in authorities.items():
        queue_id = _generation_text(raw_queue_id, "Cohort review queue ID")
        item = _review_snapshot_item(items, queue_id, authority)
        audio = _within(
            state_path.parent,
            _safe_relative(item.get("path"), f"State item {queue_id!r} path"),
            "Generated WAV",
        )
        audio_bytes = _review_snapshot_audio(audio, queue_id, authority)
        snapshots[queue_id], paths[queue_id] = (item, audio_bytes), audio
    return snapshots, paths


def _review_snapshot_item(
    items: StateItems, queue_id: str, authority: ReviewAuthority
) -> JsonDocument:
    item = items.get(queue_id)
    if item is None or item.get("status") not in {"generated", "approved"}:
        raise BulkGenerationError(f"Generated queue item does not exist: {queue_id}")
    if _canonical_sha256(item) != authority.item_sha256:
        raise BulkGenerationError(
            "Review authority changed after the cohort was displayed; refresh before deciding"
        )
    return item


def _review_snapshot_audio(
    audio: Path, queue_id: str, authority: ReviewAuthority
) -> bytes:
    try:
        payload = read_regular_bytes(audio)
    except OSError as error:
        raise BulkGenerationError(
            f"Generated WAV is unreadable for {queue_id!r}: {error}"
        ) from error
    if hashlib.sha256(payload).hexdigest() != authority.audio_sha256:
        raise BulkGenerationError(
            "Review authority changed after the cohort was displayed; refresh before deciding"
        )
    return payload


@publication_errors(BulkGenerationError)
def _assert_cohort_review_snapshot_stable(
    state_path: Path,
    queue_path: Path,
    state_sha256: str,
    queue_sha256: str,
    authorities: Mapping[str, ReviewAuthority],
    audio_paths: Mapping[str, Path],
) -> None:
    if (
        sha256_file(state_path) != state_sha256
        or sha256_file(queue_path) != queue_sha256
    ):
        raise BulkGenerationError(
            "Review authority changed while the cohort was being validated"
        )
    for raw_queue_id, authority in authorities.items():
        queue_id = _generation_text(raw_queue_id, "Cohort review queue ID")
        if sha256_file(audio_paths[queue_id]) != authority.audio_sha256:
            raise BulkGenerationError(
                "Review authority changed while the cohort was being validated"
            )


def _load_review_snapshot(
    state_path: str | Path,
    queue_id: str,
    expected_authority: object,
    queue_path: str | Path | None,
    *,
    capture_audio: bool,
) -> tuple[JsonDocument, JsonDocument, bytes]:
    """Revalidate only the exact state/item/WAV snapshot displayed by the UI."""
    if not isinstance(expected_authority, ReviewAuthority):
        raise BulkGenerationError("Review authority snapshot is invalid")
    state_path = Path(state_path).expanduser().resolve()
    queue_path = None if queue_path is None else Path(queue_path).expanduser().resolve()
    state = _load_displayed_review_state(state_path, queue_path, expected_authority)
    item = _review_snapshot_item(_state_items(state), queue_id, expected_authority)
    relative = _safe_relative(item.get("path"), f"State item {queue_id!r} path")
    audio = _within(state_path.parent, relative, "Generated WAV")
    audio_bytes = _review_snapshot_audio(audio, queue_id, expected_authority)
    return state, item, audio_bytes if capture_audio else b""


def _load_displayed_review_state(
    state_path: Path, queue_path: Path | None, authority: ReviewAuthority
) -> JsonDocument:
    try:
        payload = read_regular_bytes(state_path)
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to read generation state {state_path}: {error}"
        ) from error
    if hashlib.sha256(payload).hexdigest() != authority.state_sha256:
        raise BulkGenerationError(
            "Review authority changed after the item was displayed; refresh before deciding"
        )
    state = _review_state_document(payload, state_path, authority.queue_sha256)
    if queue_path is None or sha256_file(queue_path) != authority.queue_sha256:
        raise BulkGenerationError(
            "Review queue changed after the item was displayed; refresh before deciding"
        )
    return state


def load_review_audio_bytes(
    state_path: str | Path,
    queue_path: str | Path,
    queue_id: str,
    expected_authority: object,
) -> bytes:
    """Read the exact selected WAV bytes without rescanning unrelated outcomes."""
    _state, _item, audio_bytes = _load_review_snapshot(
        state_path,
        queue_id,
        expected_authority,
        queue_path,
        capture_audio=True,
    )
    return audio_bytes


def review_generation_item(
    state_path: str | Path,
    queue_id: str,
    decision: str,
    *,
    expected_authority: object = None,
    queue_path: str | Path | None = None,
) -> ReviewCommit | JsonDocument:
    """Persist approval/rejection, then rebuild the derived manifest."""
    if decision not in {"approved", "rejected"}:
        raise BulkGenerationError("Review decision must be approved or rejected")
    state_path = Path(state_path).expanduser().resolve()
    if expected_authority is None:
        initial = load_generation_state(state_path)
    else:
        initial, _item, _audio_bytes = _load_review_snapshot(
            state_path,
            queue_id,
            expected_authority,
            queue_path,
            capture_audio=False,
        )
    with _GenerationLease(
        state_path.parent,
        _generation_text(initial.get("queue_sha256"), "Queue SHA-256"),
        process_checker=process_is_alive,
    ) as lease:
        return _review_generation_item_locked(
            state_path,
            queue_id,
            decision,
            expected_authority=expected_authority,
            queue_path=queue_path,
            lease=lease,
        )


def review_generation_cohort(
    state_path: str | Path,
    queue_path: str | Path,
    authorities: object,
    decision: object,
    *,
    provenance: object,
) -> tuple[ReviewCommit, ...]:
    """Commit one exact cohort decision in a single state transaction."""
    if not _is_review_authorities(authorities):
        raise BulkGenerationError("Cohort review authorities must be a non-empty map")
    decisions = _cohort_review_decisions(authorities, decision)
    provenance = _cohort_review_provenance(provenance)
    state_path = Path(state_path).expanduser().resolve()
    queue_path = Path(queue_path).expanduser().resolve()
    authority_values = _cohort_review_authority_values(authorities)
    return _commit_review_generation_cohort(
        state_path, queue_path, authorities, decisions, provenance, authority_values
    )


def _cohort_review_decisions(authorities: object, decision: object) -> dict[str, str]:
    if not _is_review_authorities(authorities):
        raise BulkGenerationError("Cohort review authorities must be a non-empty map")
    if isinstance(decision, str):
        decisions = {queue_id: decision for queue_id in authorities}
    elif _is_review_decisions(decision):
        decisions = dict(decision)
    else:
        raise BulkGenerationError(
            "Cohort review decision must be approved, rejected, or an exact item map"
        )
    if set(decisions) != set(authorities) or any(
        value not in {"approved", "rejected", "pending_review"}
        for value in decisions.values()
    ):
        raise BulkGenerationError(
            "Cohort review item decisions must bind every authority to approved, "
            "rejected, or unchanged pending review"
        )
    if set(decisions.values()) == {"pending_review"}:
        raise BulkGenerationError("Cohort review decision does not change any item")
    return decisions


def _cohort_review_provenance(provenance: object) -> JsonDocument:
    if not _is_json_document(provenance):
        raise BulkGenerationError("Cohort review provenance must be an object")
    return provenance


def _cohort_review_authority_values(
    authorities: Mapping[str, ReviewAuthority],
) -> list[ReviewAuthority]:
    authority_values = list(authorities.values())
    if len({value.queue_sha256 for value in authority_values}) != 1:
        raise BulkGenerationError("Cohort review queue authorities do not match")
    if len({value.state_sha256 for value in authority_values}) != 1:
        raise BulkGenerationError("Cohort review state authorities do not match")
    return authority_values


def _commit_review_generation_cohort(
    state_path: Path,
    queue_path: Path,
    authorities: Mapping[str, ReviewAuthority],
    decisions: Mapping[str, str],
    provenance: JsonDocument,
    authority_values: Sequence[ReviewAuthority],
) -> tuple[ReviewCommit, ...]:
    with _GenerationLease(
        state_path.parent,
        authority_values[0].queue_sha256,
        process_checker=process_is_alive,
    ) as lease:
        state, snapshots = assert_review_authorities(
            state_path, authorities, queue_path
        )
        _assert_cohort_items_pending(snapshots)
        proposed, updated_at = _cohort_review_proposed_state(
            state, authorities, decisions, provenance
        )
        manifest_path = state_path.parent / "manifest.json"
        entries = _approved_manifest_entries(
            proposed, state_path.parent, validate_files=False
        )
        _validate_cohort_approved_wavs(proposed, state_path.parent, decisions)
        transaction_id = secrets.token_hex(16)
        staged_state = state_path.with_name(f".{state_path.name}.{transaction_id}.tmp")
        staged_manifest = manifest_path.with_name(
            f".{manifest_path.name}.{transaction_id}.tmp"
        )
        staged_conservative_manifest = manifest_path.with_name(
            f".{manifest_path.name}.{transaction_id}.conservative.tmp"
        )
        is_mixed = len(set(decisions.values())) > 1
        try:
            atomic_write_json(staged_state, proposed, sort_keys=True)
            proposed_state_sha256 = sha256_file(staged_state)
            _write_generated_manifest_from_state(
                proposed,
                state_path.parent,
                staged_manifest,
                entries=entries,
                validate_files=False,
            )
            _write_conservative_cohort_manifest(
                is_mixed, state, state_path.parent, staged_conservative_manifest
            )
            _assert_cohort_commit_authority(
                state_path, queue_path, authorities, authority_values, proposed, entries
            )
            _publish_cohort_review(
                is_mixed,
                decisions,
                staged_conservative_manifest,
                staged_state,
                staged_manifest,
                manifest_path,
                state_path,
                queue_path,
                authorities,
                authority_values,
                proposed_state_sha256,
                proposed,
                entries,
                lease,
            )
        finally:
            for staged in (
                staged_state,
                staged_manifest,
                staged_conservative_manifest,
            ):
                staged.unlink(missing_ok=True)
        lease.mark_committed()
    committed_state_sha256 = sha256_file(state_path)
    proposed_items = _state_items(proposed)
    return tuple(
        ReviewCommit(
            queue_id=queue_id,
            status=_generation_text(
                proposed_items[queue_id].get("status"), "Review status"
            ),
            review_status=decisions[queue_id],
            updated_at=updated_at,
            authority=ReviewAuthority(
                queue_sha256=_generation_text(
                    proposed.get("queue_sha256"), "Queue SHA-256"
                ),
                state_sha256=committed_state_sha256,
                item_sha256=_canonical_sha256(proposed_items[queue_id]),
                audio_sha256=authority.audio_sha256,
            ),
        )
        for queue_id, authority in authorities.items()
        if decisions[queue_id] != "pending_review"
    )


def _assert_cohort_items_pending(
    snapshots: Mapping[str, tuple[JsonDocument, bytes]],
) -> None:
    for queue_id, (item, _audio) in snapshots.items():
        if (item.get("status"), item.get("review_status")) != (
            "generated",
            "pending_review",
        ):
            raise BulkGenerationError(
                f"Cohort item is no longer pending review: {queue_id}"
            )


def _cohort_review_proposed_state(
    state: JsonDocument,
    authorities: Mapping[str, ReviewAuthority],
    decisions: Mapping[str, str],
    provenance: JsonDocument,
) -> tuple[JsonDocument, str]:
    proposed, updated_at = copy.deepcopy(state), datetime.now(timezone.utc).isoformat()
    proposed_items = _state_items(proposed)
    for queue_id, authority in authorities.items():
        decision = decisions[queue_id]
        if decision == "pending_review":
            continue
        item = proposed_items[queue_id]
        item["review_status"] = decision
        item["status"] = "approved" if decision == "approved" else "generated"
        item["updated_at"] = updated_at
        item["cohort_review"] = {
            **copy.deepcopy(provenance),
            "projection_review_status": decision,
            "target_audio_sha256": authority.audio_sha256,
        }
    return proposed, updated_at


def _write_conservative_cohort_manifest(
    is_mixed: bool, state: JsonDocument, output_directory: Path, path: Path
) -> None:
    if is_mixed:
        _write_generated_manifest_from_state(
            state,
            output_directory,
            path,
            entries=_approved_manifest_entries(
                state, output_directory, validate_files=False
            ),
            validate_files=False,
        )


@publication_errors(BulkGenerationError)
def _assert_cohort_commit_authority(
    state_path: Path,
    queue_path: Path,
    authorities: Mapping[str, ReviewAuthority],
    authority_values: Sequence[ReviewAuthority],
    proposed: JsonDocument,
    entries: list[dict[str, object]],
) -> None:
    assert_review_authorities(state_path, authorities, queue_path)
    if (
        _approved_manifest_entries(proposed, state_path.parent, validate_files=False)
        != entries
    ):
        raise BulkGenerationError(
            "Approved cohort manifest authority changed before the final commit"
        )
    if sha256_file(state_path) != authority_values[0].state_sha256:
        raise BulkGenerationError("Cohort review state changed before the final commit")
    if sha256_file(queue_path) != authority_values[0].queue_sha256:
        raise BulkGenerationError("Cohort review queue changed before the final commit")


def _publish_cohort_review(
    is_mixed: bool,
    decisions: Mapping[str, str],
    conservative_manifest: Path,
    staged_state: Path,
    staged_manifest: Path,
    manifest_path: Path,
    state_path: Path,
    queue_path: Path,
    authorities: Mapping[str, ReviewAuthority],
    authority_values: Sequence[ReviewAuthority],
    proposed_state_sha256: str,
    proposed: JsonDocument,
    entries: object,
    lease: GenerationLease,
) -> None:
    lease.assert_owned()
    if is_mixed:
        _publish_mixed_cohort_review(
            conservative_manifest,
            staged_state,
            staged_manifest,
            manifest_path,
            state_path,
            queue_path,
            authorities,
            authority_values,
            proposed_state_sha256,
            proposed,
            decisions,
            entries,
            lease,
        )
    elif next(iter(decisions.values())) == "rejected":
        _publish_rejected_cohort_review(
            staged_manifest, staged_state, manifest_path, state_path, lease
        )
    else:
        _publish_approved_cohort_review(
            staged_state,
            staged_manifest,
            manifest_path,
            state_path,
            proposed,
            decisions,
            state_path.parent,
            lease,
        )


def _publish_mixed_cohort_review(
    conservative_manifest: Path,
    staged_state: Path,
    staged_manifest: Path,
    manifest_path: Path,
    state_path: Path,
    queue_path: Path,
    authorities: Mapping[str, ReviewAuthority],
    authority_values: Sequence[ReviewAuthority],
    proposed_state_sha256: str,
    proposed: JsonDocument,
    decisions: Mapping[str, str],
    entries: object,
    lease: GenerationLease,
) -> None:
    try:
        os.replace(conservative_manifest, manifest_path)
        lease.assert_owned()
        assert_review_authorities(state_path, authorities, queue_path)
        lease.assert_owned()
        os.replace(staged_state, state_path)
        lease.assert_owned()
        if sha256_file(state_path) != proposed_state_sha256:
            raise BulkGenerationError(
                "Mixed cohort state changed before manifest publication"
            )
        if sha256_file(queue_path) != authority_values[0].queue_sha256:
            raise BulkGenerationError(
                "Cohort review queue changed before manifest publication"
            )
        _validate_cohort_approved_wavs(proposed, state_path.parent, decisions)
        if (
            _approved_manifest_entries(
                proposed, state_path.parent, validate_files=False
            )
            != entries
        ):
            raise BulkGenerationError(
                "Mixed cohort manifest authority changed before publication"
            )
        lease.assert_owned()
        os.replace(staged_manifest, manifest_path)
    except OSError as error:
        raise BulkGenerationError(
            "Mixed cohort review did not fully commit; the published manifest remains fail-closed: "
            f"{error}"
        ) from error


def _publish_rejected_cohort_review(
    staged_manifest: Path,
    staged_state: Path,
    manifest_path: Path,
    state_path: Path,
    lease: GenerationLease,
) -> None:
    try:
        os.replace(staged_manifest, manifest_path)
        lease.assert_owned()
        os.replace(staged_state, state_path)
    except OSError as error:
        raise BulkGenerationError(
            "Cohort rejection did not fully commit; the published manifest remains fail-closed: "
            f"{error}"
        ) from error


def _publish_approved_cohort_review(
    staged_state: Path,
    staged_manifest: Path,
    manifest_path: Path,
    state_path: Path,
    proposed: JsonDocument,
    decisions: Mapping[str, str],
    output_directory: Path,
    lease: GenerationLease,
) -> None:
    try:
        os.replace(staged_state, state_path)
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to save cohort review decision: {error}"
        ) from error
    try:
        lease.assert_owned()
        _validate_cohort_approved_wavs(proposed, output_directory, decisions)
    except BulkGenerationError as error:
        raise BulkGenerationError(
            "Cohort review decision was saved, but manifest rebuild was blocked: "
            f"{error}"
        ) from error
    try:
        os.replace(staged_manifest, manifest_path)
    except OSError as error:
        raise BulkGenerationError(
            f"Cohort review decision was saved, but manifest rebuild failed: {error}"
        ) from error


def _validate_cohort_approved_wavs(
    state: JsonDocument,
    output_directory: Path,
    decisions: Mapping[str, str],
) -> None:
    """Validate only WAVs whose approval is introduced by this transaction."""
    items = _state_items(state)
    for queue_id, decision in decisions.items():
        if decision != "approved":
            continue
        item = items[queue_id]
        relative = _safe_relative(item.get("path"), f"State item {queue_id!r} path")
        audio = _within(output_directory, relative, "Generated WAV")
        _validate_success_file(queue_id, item, audio)


def _review_generation_item_locked(
    state_path: Path,
    queue_id: str,
    decision: str,
    *,
    expected_authority: object = None,
    queue_path: str | Path | None = None,
    lease: GenerationLease | None = None,
) -> ReviewCommit | JsonDocument:
    state = _review_locked_state(state_path, queue_id, expected_authority, queue_path)
    if expected_authority is not None and not isinstance(
        expected_authority, ReviewAuthority
    ):
        raise BulkGenerationError("Review authority snapshot is invalid")
    item, audio = _review_locked_item(state, state_path, queue_id)
    _validate_review_locked_authority(
        state_path, queue_id, expected_authority, queue_path, item, audio
    )
    if lease is not None:
        lease.assert_owned()
    return _commit_review_item_decision(
        state_path, state, queue_id, decision, expected_authority, queue_path, lease
    )


def _review_locked_state(
    state_path: Path, queue_id: str, authority: object, queue_path: str | Path | None
) -> JsonDocument:
    if authority is None:
        return load_generation_state(state_path)
    state, _item, _audio = _load_review_snapshot(
        state_path, queue_id, authority, queue_path, capture_audio=False
    )
    return state


def _review_locked_item(
    state: JsonDocument, state_path: Path, queue_id: str
) -> tuple[JsonDocument, Path]:
    item = _state_items(state).get(queue_id)
    if item is None or item.get("status") not in {"generated", "approved"}:
        raise BulkGenerationError(f"Generated queue item does not exist: {queue_id}")
    relative = _safe_relative(item.get("path"), f"State item {queue_id!r} path")
    audio = _within(state_path.parent, relative, "Generated WAV")
    return item, audio


def _validate_review_locked_authority(
    state_path: Path,
    queue_id: str,
    authority: object,
    queue_path: str | Path | None,
    item: JsonDocument,
    audio: Path,
) -> None:
    if authority is None:
        _validate_success_file(queue_id, item, audio)
    else:
        _assert_review_authority(state_path, queue_id, authority, queue_path)


def _commit_review_item_decision(
    state_path: Path,
    state: JsonDocument,
    queue_id: str,
    decision: str,
    expected_authority: ReviewAuthority | None,
    queue_path: str | Path | None,
    lease: GenerationLease | None,
) -> ReviewCommit | JsonDocument:
    proposed = copy.deepcopy(state)
    proposed_item = _state_items(proposed)[queue_id]
    proposed_item["review_status"] = decision
    proposed_item["status"] = "approved" if decision == "approved" else "generated"
    proposed_item["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest_path = state_path.parent / "manifest.json"
    entries = _approved_manifest_entries(
        proposed,
        state_path.parent,
        validate_files=expected_authority is None,
    )
    transaction_id = secrets.token_hex(16)
    staged_state = state_path.with_name(f".{state_path.name}.{transaction_id}.tmp")
    staged_manifest = manifest_path.with_name(
        f".{manifest_path.name}.{transaction_id}.tmp"
    )
    _write_review_item_transaction(
        staged_state,
        staged_manifest,
        state_path,
        manifest_path,
        proposed,
        entries,
        queue_id,
        expected_authority,
        queue_path,
        lease,
    )
    if lease is not None:
        lease.mark_committed()
    return _review_commit_result(proposed, state_path, queue_id, expected_authority)


def _write_review_item_transaction(
    staged_state: Path,
    staged_manifest: Path,
    state_path: Path,
    manifest_path: Path,
    proposed: JsonDocument,
    entries: list[dict[str, object]],
    queue_id: str,
    authority: ReviewAuthority | None,
    queue_path: str | Path | None,
    lease: GenerationLease | None,
) -> None:
    try:
        atomic_write_json(staged_state, proposed, sort_keys=True)
        _write_generated_manifest_from_state(
            proposed,
            state_path.parent,
            staged_manifest,
            entries=entries,
            validate_files=authority is None,
        )
        if authority is not None:
            _assert_review_authority(state_path, queue_id, authority, queue_path)
        if lease is not None:
            lease.assert_owned()
        _replace_review_state(staged_state, state_path)
        _replace_review_manifest(staged_manifest, manifest_path, lease)
    finally:
        for staged in (staged_state, staged_manifest):
            try:
                staged.unlink()
            except FileNotFoundError, OSError:
                pass


def _replace_review_state(staged_state: Path, state_path: Path) -> None:
    try:
        os.replace(staged_state, state_path)
    except OSError as error:
        raise BulkGenerationError(f"Unable to save review decision: {error}") from error


def _replace_review_manifest(
    staged_manifest: Path, manifest_path: Path, lease: GenerationLease | None
) -> None:
    try:
        if lease is not None:
            try:
                lease.assert_owned()
            except BulkGenerationError as error:
                raise BulkGenerationError(
                    "Review decision was saved, but manifest rebuild was blocked: "
                    f"{error}"
                ) from error
        os.replace(staged_manifest, manifest_path)
    except OSError as error:
        raise BulkGenerationError(
            f"Review decision was saved, but manifest rebuild failed: {error}"
        ) from error


def _review_commit_result(
    proposed: JsonDocument,
    state_path: Path,
    queue_id: str,
    authority: ReviewAuthority | None,
) -> ReviewCommit | JsonDocument:
    if authority is None:
        return proposed
    committed_item = _state_items(proposed)[queue_id]
    return ReviewCommit(
        queue_id=queue_id,
        status=_generation_text(committed_item.get("status"), "Review status"),
        review_status=_generation_text(
            committed_item.get("review_status"), "Review status"
        ),
        updated_at=_generation_text(committed_item.get("updated_at"), "Review time"),
        authority=ReviewAuthority(
            queue_sha256=_generation_text(
                proposed.get("queue_sha256"), "Queue SHA-256"
            ),
            state_sha256=sha256_file(state_path),
            item_sha256=_canonical_sha256(committed_item),
            audio_sha256=authority.audio_sha256,
        ),
    )


# Preserve existing public record identities for imports and stored pickles.
ReviewAuthority.__module__ = "vntts.authoring.bulk_generation"
ReviewCommit.__module__ = "vntts.authoring.bulk_generation"
