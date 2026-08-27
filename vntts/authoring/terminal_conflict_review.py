"""Publish and record bounded human review of terminal authority conflicts."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import socket
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav

from vntts.authoring.advisory_lock import (
    AdvisoryLockBusyError,
    exclusive_advisory_lock,
)
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
    process_started_at,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
)
from vntts.authoring.reconciliation import (
    AuthoringReconciliationError,
    load_authoring_reconciliation,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    list_review_items,
    prepare_review_audio,
)

TERMINAL_CONFLICT_REVIEW_SCHEMA = "vntts.authoring-terminal-conflict-review"
TERMINAL_CONFLICT_REVIEW_VERSION = 1
TERMINAL_CONFLICT_PROGRESS_SCHEMA = "vntts.authoring-terminal-conflict-review-progress"
TERMINAL_CONFLICT_PROGRESS_VERSION = 1
TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION = 2
SUPPORTED_TERMINAL_CONFLICT_PROGRESS_VERSIONS = frozenset(
    {TERMINAL_CONFLICT_PROGRESS_VERSION, TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION}
)
NEITHER_ACCEPTABLE = "neither_acceptable"
PROGRESS_LEASE_SCHEMA = "vntts.authoring-terminal-conflict-progress-lease"
PROGRESS_LEASE_VERSION = 1


class TerminalConflictReviewError(RuntimeError):
    """Terminal authority conflict evidence is invalid or changed."""


@dataclass(frozen=True)
class TerminalConflictReview:
    directory: Path
    review_id: str
    case_count: int
    candidate_count: int
    completed_count: int = 0
    created: bool = False

    @property
    def review(self):
        return self.directory / "review.json"

    @property
    def progress(self):
        return self.directory / "progress.json"

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "review": str(self.review),
            "progress": str(self.progress),
            "review_id": self.review_id,
            "case_count": self.case_count,
            "candidate_count": self.candidate_count,
            "completed_count": self.completed_count,
            "created": self.created,
        }


def publish_terminal_conflict_review(reconciliation_path, output_directory):
    """Publish exact distinct WAV choices for every current terminal conflict."""
    reconciliation_path = Path(reconciliation_path).expanduser().resolve()
    output = Path(output_directory).expanduser().resolve()
    try:
        report_snapshot = capture_authority_file(
            reconciliation_path, "authoring reconciliation"
        )
        report = load_authoring_reconciliation(report_snapshot.path).document
    except (
        AuthoringAuthorityError,
        AuthoringReconciliationError,
        OSError,
        ValueError,
    ) as error:
        raise TerminalConflictReviewError(str(error)) from error
    if report_snapshot.path.read_bytes() != report_snapshot.payload:
        raise TerminalConflictReviewError(
            "Authoring reconciliation changed while it was loaded"
        )
    conflicts = report["terminal_conflicts"]
    if not conflicts:
        raise TerminalConflictReviewError(
            "Authoring reconciliation has no terminal conflicts"
        )
    workspace_records = {value["workspace_id"]: value for value in report["workspaces"]}
    workspace_paths = {
        workspace_id: Path(value["workspace"]).resolve()
        for workspace_id, value in workspace_records.items()
    }
    if len(workspace_records) != len(report["workspaces"]):
        raise TerminalConflictReviewError("Reconciliation workspaces are duplicated")

    output.parent.mkdir(parents=True, exist_ok=True)
    output_exists = output.exists() or output.is_symlink()
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    try:
        workspace_queue_ids = {}
        for conflict in conflicts:
            for occurrence in conflict["occurrences"]:
                workspace_queue_ids.setdefault(occurrence["workspace_id"], set()).add(
                    conflict["queue_id"]
                )
        review_rows = {}
        for workspace_id, queue_ids in sorted(workspace_queue_ids.items()):
            workspace = workspace_paths.get(workspace_id)
            if workspace is None:
                raise TerminalConflictReviewError(
                    f"Conflict references an unknown workspace: {workspace_id}"
                )
            try:
                rows = list_review_items(workspace, tuple(sorted(queue_ids)))
            except AuthoringWorkbenchError as error:
                raise TerminalConflictReviewError(str(error)) from error
            indexed = {row.queue_id: row for row in rows}
            if set(indexed) != queue_ids:
                raise TerminalConflictReviewError(
                    f"Conflict outcomes are unavailable: {workspace_id}"
                )
            for queue_id, row in indexed.items():
                review_rows[(workspace_id, queue_id)] = row

        cases = []
        candidate_total = 0
        for position, conflict in enumerate(conflicts, start=1):
            queue_id = conflict["queue_id"]
            occurrences = conflict["occurrences"]
            if len({value["queue_record_sha256"] for value in occurrences}) != 1:
                raise TerminalConflictReviewError(
                    f"Conflict changes queue content and cannot be audio-reviewed: {queue_id}"
                )
            if len({value["text_sha256"] for value in occurrences}) != 1:
                raise TerminalConflictReviewError(
                    f"Conflict changes text and cannot be audio-reviewed: {queue_id}"
                )
            candidates = {}
            shared = None
            for occurrence in occurrences:
                workspace_id = occurrence["workspace_id"]
                workspace = workspace_paths.get(workspace_id)
                if workspace is None:
                    raise TerminalConflictReviewError(
                        f"Conflict references an unknown workspace: {workspace_id}"
                    )
                row = review_rows[(workspace_id, queue_id)]
                expected_review = {
                    "approved": "approved",
                    "rejected": "rejected",
                }.get(occurrence["authority"])
                if (
                    expected_review is None
                    or row.review_status != expected_review
                    or row.line_id != occurrence["line_id"]
                    or hashlib.sha256(row.text.encode("utf-8")).hexdigest()
                    != occurrence["text_sha256"]
                    or row.authority is None
                ):
                    raise TerminalConflictReviewError(
                        f"Conflict authority changed: {workspace_id}/{queue_id}"
                    )
                workspace_record = workspace_records[workspace_id]
                if (
                    row.authority.state_sha256 != workspace_record["state_sha256"]
                    or row.authority.queue_sha256 != workspace_record["queue_sha256"]
                ):
                    raise TerminalConflictReviewError(
                        f"Conflict source changed after reconciliation: {workspace_id}"
                    )
                try:
                    audio = prepare_review_audio(row)
                except AuthoringWorkbenchError as error:
                    raise TerminalConflictReviewError(str(error)) from error
                digest = hashlib.sha256(audio).hexdigest()
                if digest != row.authority.audio_sha256:
                    raise TerminalConflictReviewError(
                        f"Conflict WAV changed: {workspace_id}/{queue_id}"
                    )
                identity = (occurrence["authority"], digest)
                candidate = candidates.setdefault(
                    identity,
                    {
                        "authority": occurrence["authority"],
                        "audio_sha256": digest,
                        "audio_bytes": audio,
                        "workspace_ids": [],
                        "source_authorities": [],
                    },
                )
                candidate["workspace_ids"].append(workspace_id)
                candidate["source_authorities"].append(
                    {
                        "workspace_id": workspace_id,
                        "state": str(row.state.resolve()),
                        "queue": str(row.queue.resolve()),
                        "review_authority": {
                            "queue_sha256": row.authority.queue_sha256,
                            "state_sha256": row.authority.state_sha256,
                            "item_sha256": row.authority.item_sha256,
                            "audio_sha256": row.authority.audio_sha256,
                        },
                    }
                )
                row_shared = (row.line_id, row.speaker, row.voice_character, row.text)
                if shared is None:
                    shared = row_shared
                elif shared != row_shared:
                    raise TerminalConflictReviewError(
                        f"Conflict display identity changed: {queue_id}"
                    )
            if len(candidates) != 2:
                raise TerminalConflictReviewError(
                    "Terminal conflict review requires exactly two distinct WAVs: "
                    f"{queue_id}"
                )
            line_id, speaker, voice_character, text = shared
            stable_candidates = []
            for candidate_position, ((_authority, digest), candidate) in enumerate(
                sorted(candidates.items()), start=1
            ):
                candidate_id = canonical_document_sha256(
                    {
                        "queue_id": queue_id,
                        "authority": candidate["authority"],
                        "audio_sha256": digest,
                    }
                )
                relative = (
                    Path("audio")
                    / f"{position:02d}"
                    / f"candidate-{candidate_position}.wav"
                )
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(candidate.pop("audio_bytes"))
                if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                    raise TerminalConflictReviewError(
                        f"Conflict WAV changed while copied: {queue_id}"
                    )
                try:
                    info = probe_pcm16_mono_wav(destination)
                except Pcm16MonoWavError as error:
                    raise TerminalConflictReviewError(str(error)) from error
                stable_candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "audio": relative.as_posix(),
                        **candidate,
                        "sample_rate": info.sample_rate,
                        "sample_count": info.sample_count,
                        "workspace_ids": sorted(candidate["workspace_ids"]),
                        "source_authorities": sorted(
                            candidate["source_authorities"],
                            key=lambda value: value["workspace_id"],
                        ),
                    }
                )
            candidate_total += len(stable_candidates)
            cases.append(
                {
                    "case_id": canonical_document_sha256(
                        {
                            "queue_id": queue_id,
                            "queue_record_sha256": occurrences[0][
                                "queue_record_sha256"
                            ],
                            "text_sha256": occurrences[0]["text_sha256"],
                            "candidate_ids": [
                                value["candidate_id"] for value in stable_candidates
                            ],
                        }
                    ),
                    "queue_id": queue_id,
                    "line_id": line_id,
                    "queue_record_sha256": occurrences[0]["queue_record_sha256"],
                    "text_sha256": occurrences[0]["text_sha256"],
                    "speaker": speaker,
                    "voice_character": voice_character,
                    "text": text,
                    "candidates": stable_candidates,
                }
            )
        body = {
            "schema": TERMINAL_CONFLICT_REVIEW_SCHEMA,
            "schema_version": TERMINAL_CONFLICT_REVIEW_VERSION,
            "source_reconciliation": str(report_snapshot.path),
            "source_reconciliation_sha256": report_snapshot.sha256,
            "source_report_id": report["report_id"],
            "policy": {
                "candidate_order": "stable opaque digest order",
                "decision_scope": "one explicit winner or neither per exact queue ID",
                "workspace_mutation": "forbidden",
            },
            "case_count": len(cases),
            "candidate_count": candidate_total,
            "cases": cases,
        }
        review_id = canonical_document_sha256(body)
        document = {**body, "review_id": review_id}
        atomic_write_json(staging / "review.json", document, sort_keys=True)
        load_terminal_conflict_review(staging)
        assert_authority_snapshot(report_snapshot, "authoring reconciliation")
        _assert_source_authorities(document)
        if output_exists:
            existing = load_terminal_conflict_review(output)
            if existing.review_id != review_id:
                raise TerminalConflictReviewError(
                    f"Terminal conflict review output has another identity: {output}"
                )
            shutil.rmtree(staging)
            staging = None
            return existing
        try:
            rename_directory_no_replace(staging, output)
        except (AtomicPublicationError, OSError) as error:
            raise TerminalConflictReviewError(
                f"Unable to publish terminal conflict review: {error}"
            ) from error
        staging = None
    except BaseException:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        raise
    return TerminalConflictReview(
        output, review_id, len(cases), candidate_total, 0, True
    )


def load_terminal_conflict_review(directory):
    """Load one immutable conflict review and its optional decision progress."""
    directory = _review_directory(directory)
    review_path = directory / "review.json"
    try:
        payload = review_path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TerminalConflictReviewError(str(error)) from error
    document = validate_terminal_conflict_review_document(document, directory)
    if review_path.read_bytes() != payload:
        raise TerminalConflictReviewError(
            "Terminal conflict review changed while loaded"
        )
    completed = 0
    progress_path = directory / "progress.json"
    if progress_path.exists() or progress_path.is_symlink():
        completed = len(load_terminal_conflict_review_progress(directory)["decisions"])
    return TerminalConflictReview(
        directory,
        document["review_id"],
        document["case_count"],
        document["candidate_count"],
        completed,
        False,
    )


def load_terminal_conflict_review_document(directory):
    """Return one exact validated immutable review document."""
    directory = _review_directory(directory)
    try:
        snapshot = capture_authority_file(
            directory / "review.json", "terminal conflict review"
        )
        document = validate_terminal_conflict_review_document(
            snapshot.json_document("terminal conflict review"), directory
        )
        assert_authority_snapshot(snapshot, "terminal conflict review")
    except AuthoringAuthorityError as error:
        raise TerminalConflictReviewError(str(error)) from error
    return document


def load_terminal_conflict_candidate_audio(directory, case_id, candidate_id):
    """Return exact copied WAV bytes for one displayed blind candidate."""
    directory = _review_directory(directory)
    document = load_terminal_conflict_review_document(directory)
    case = next(
        (item for item in document["cases"] if item["case_id"] == case_id), None
    )
    if case is None:
        raise TerminalConflictReviewError(f"Unknown terminal conflict: {case_id}")
    candidate = next(
        (item for item in case["candidates"] if item["candidate_id"] == candidate_id),
        None,
    )
    if candidate is None:
        raise TerminalConflictReviewError(
            f"Unknown terminal conflict candidate: {candidate_id}"
        )
    path = _contained_file(directory, candidate["audio"], "candidate WAV")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != candidate["audio_sha256"]:
        raise TerminalConflictReviewError("Terminal conflict WAV changed")
    return payload


def validate_terminal_conflict_review_document(document, directory):
    value = copy.deepcopy(document)
    if (
        not isinstance(value, dict)
        or value.get("schema") != TERMINAL_CONFLICT_REVIEW_SCHEMA
        or value.get("schema_version") != TERMINAL_CONFLICT_REVIEW_VERSION
    ):
        raise TerminalConflictReviewError("Unsupported terminal conflict review")
    expected_fields = {
        "schema",
        "schema_version",
        "review_id",
        "source_reconciliation",
        "source_reconciliation_sha256",
        "source_report_id",
        "policy",
        "case_count",
        "candidate_count",
        "cases",
    }
    if set(value) != expected_fields:
        raise TerminalConflictReviewError("Terminal conflict review fields changed")
    review_id = _sha256(value["review_id"], "Terminal conflict review ID")
    body = {key: item for key, item in value.items() if key != "review_id"}
    if canonical_document_sha256(body) != review_id:
        raise TerminalConflictReviewError("Terminal conflict review identity changed")
    _sha256(value["source_reconciliation_sha256"], "Source reconciliation hash")
    _sha256(value["source_report_id"], "Source reconciliation report ID")
    source_reconciliation = _text(
        value["source_reconciliation"], "Source reconciliation path"
    )
    if not Path(source_reconciliation).is_absolute():
        raise TerminalConflictReviewError("Source reconciliation path must be absolute")
    if value["policy"] != {
        "candidate_order": "stable opaque digest order",
        "decision_scope": "one explicit winner or neither per exact queue ID",
        "workspace_mutation": "forbidden",
    }:
        raise TerminalConflictReviewError("Terminal conflict review policy changed")
    cases = value["cases"]
    if not isinstance(cases, list) or not cases:
        raise TerminalConflictReviewError("Terminal conflict review cases are empty")
    if value["case_count"] != len(cases):
        raise TerminalConflictReviewError("Terminal conflict case count changed")
    root = Path(directory).resolve()
    seen_cases = set()
    seen_queue_ids = set()
    candidate_count = 0
    for case in cases:
        if not isinstance(case, dict) or set(case) != {
            "case_id",
            "queue_id",
            "line_id",
            "queue_record_sha256",
            "text_sha256",
            "speaker",
            "voice_character",
            "text",
            "candidates",
        }:
            raise TerminalConflictReviewError("Terminal conflict case is malformed")
        case_id = _sha256(case["case_id"], "Terminal conflict case ID")
        queue_id = _text(case["queue_id"], "Terminal conflict queue ID")
        if case_id in seen_cases or queue_id in seen_queue_ids:
            raise TerminalConflictReviewError("Terminal conflict case is duplicated")
        seen_cases.add(case_id)
        seen_queue_ids.add(queue_id)
        _text(case["line_id"], "Terminal conflict line ID")
        queue_record_sha256 = _sha256(
            case["queue_record_sha256"], "Terminal conflict queue-record hash"
        )
        text_sha256 = _sha256(case["text_sha256"], "Terminal conflict text hash")
        text = _text(case["text"], "Terminal conflict text")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
            raise TerminalConflictReviewError("Terminal conflict text changed")
        _text(case["speaker"], "Terminal conflict speaker")
        _text(case["voice_character"], "Terminal conflict voice character")
        candidates = case["candidates"]
        if not isinstance(candidates, list) or len(candidates) != 2:
            raise TerminalConflictReviewError(
                "Terminal conflict requires exactly two candidates"
            )
        candidate_ids = []
        identities = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or set(candidate) != {
                "candidate_id",
                "authority",
                "audio",
                "audio_sha256",
                "sample_rate",
                "sample_count",
                "source_authorities",
                "workspace_ids",
            }:
                raise TerminalConflictReviewError(
                    "Terminal conflict candidate is malformed"
                )
            candidate_id = _sha256(
                candidate["candidate_id"], "Terminal conflict candidate ID"
            )
            authority = candidate["authority"]
            if authority not in {"approved", "rejected"}:
                raise TerminalConflictReviewError(
                    "Terminal conflict candidate authority is invalid"
                )
            digest = _sha256(
                candidate["audio_sha256"], "Terminal conflict candidate WAV hash"
            )
            expected_id = canonical_document_sha256(
                {
                    "queue_id": queue_id,
                    "authority": authority,
                    "audio_sha256": digest,
                }
            )
            if candidate_id != expected_id or (authority, digest) in identities:
                raise TerminalConflictReviewError(
                    "Terminal conflict candidate identity changed"
                )
            identities.add((authority, digest))
            candidate_ids.append(candidate_id)
            audio = _contained_file(root, candidate["audio"], "candidate WAV")
            payload = audio.read_bytes()
            if hashlib.sha256(payload).hexdigest() != digest:
                raise TerminalConflictReviewError("Terminal conflict WAV changed")
            try:
                info = probe_pcm16_mono_wav(audio)
            except Pcm16MonoWavError as error:
                raise TerminalConflictReviewError(str(error)) from error
            if (
                candidate["sample_rate"] != info.sample_rate
                or candidate["sample_count"] != info.sample_count
            ):
                raise TerminalConflictReviewError(
                    "Terminal conflict WAV metadata changed"
                )
            workspace_ids = candidate["workspace_ids"]
            if (
                not isinstance(workspace_ids, list)
                or not workspace_ids
                or workspace_ids != sorted(set(workspace_ids))
                or any(not isinstance(item, str) or not item for item in workspace_ids)
            ):
                raise TerminalConflictReviewError(
                    "Terminal conflict candidate workspaces changed"
                )
            source_authorities = candidate["source_authorities"]
            if (
                not isinstance(source_authorities, list)
                or len(source_authorities) != len(workspace_ids)
                or any(not isinstance(item, dict) for item in source_authorities)
                or [item.get("workspace_id") for item in source_authorities]
                != workspace_ids
            ):
                raise TerminalConflictReviewError(
                    "Terminal conflict source authorities changed"
                )
            for source in source_authorities:
                if not isinstance(source, dict) or set(source) != {
                    "workspace_id",
                    "state",
                    "queue",
                    "review_authority",
                }:
                    raise TerminalConflictReviewError(
                        "Terminal conflict source authority is malformed"
                    )
                _text(source["workspace_id"], "Terminal conflict source workspace")
                state_path = Path(
                    _text(source["state"], "Terminal conflict source state")
                )
                queue_path = Path(
                    _text(source["queue"], "Terminal conflict source queue")
                )
                if not state_path.is_absolute() or not queue_path.is_absolute():
                    raise TerminalConflictReviewError(
                        "Terminal conflict source paths must be absolute"
                    )
                authority = source["review_authority"]
                if not isinstance(authority, dict) or set(authority) != {
                    "queue_sha256",
                    "state_sha256",
                    "item_sha256",
                    "audio_sha256",
                }:
                    raise TerminalConflictReviewError(
                        "Terminal conflict review authority is malformed"
                    )
                for key, authority_digest in authority.items():
                    _sha256(
                        authority_digest,
                        f"Terminal conflict review authority {key}",
                    )
        expected_case_id = canonical_document_sha256(
            {
                "queue_id": queue_id,
                "queue_record_sha256": queue_record_sha256,
                "text_sha256": text_sha256,
                "candidate_ids": candidate_ids,
            }
        )
        if case_id != expected_case_id:
            raise TerminalConflictReviewError("Terminal conflict case identity changed")
        candidate_count += len(candidates)
    if value["candidate_count"] != candidate_count:
        raise TerminalConflictReviewError("Terminal conflict candidate count changed")
    return value


def load_terminal_conflict_review_progress(directory):
    directory = _review_directory(directory)
    try:
        review_snapshot = capture_authority_file(
            directory / "review.json", "terminal conflict review"
        )
        review = validate_terminal_conflict_review_document(
            review_snapshot.json_document("terminal conflict review"), directory
        )
        progress_snapshot = capture_authority_file(
            directory / "progress.json", "terminal conflict progress"
        )
        progress = progress_snapshot.json_document("terminal conflict progress")
        validated = _validate_progress(progress, review)
        _assert_progress_carry_forward(validated, review)
        assert_authority_snapshot(review_snapshot, "terminal conflict review")
        assert_authority_snapshot(progress_snapshot, "terminal conflict progress")
    except AuthoringAuthorityError as error:
        raise TerminalConflictReviewError(str(error)) from error
    return validated


def record_terminal_conflict_decision(directory, case_id, decision, *, overwrite=False):
    """Atomically record one human winner without changing source workspaces."""
    directory = _review_directory(directory)
    with _progress_lock(directory):
        review_snapshot = capture_authority_file(
            directory / "review.json", "terminal conflict review"
        )
        review = validate_terminal_conflict_review_document(
            review_snapshot.json_document("terminal conflict review"), directory
        )
        case = next(
            (value for value in review["cases"] if value["case_id"] == case_id), None
        )
        if case is None:
            raise TerminalConflictReviewError(f"Unknown terminal conflict: {case_id}")
        allowed = {value["candidate_id"] for value in case["candidates"]}
        allowed.add(NEITHER_ACCEPTABLE)
        if decision not in allowed:
            raise TerminalConflictReviewError(
                "Terminal conflict decision is not a candidate or neither"
            )
        progress_path = directory / "progress.json"
        if progress_path.exists() or progress_path.is_symlink():
            progress_snapshot = capture_authority_file(
                progress_path, "terminal conflict progress"
            )
            progress = _validate_progress(
                progress_snapshot.json_document("terminal conflict progress"), review
            )
            _assert_progress_carry_forward(progress, review)
            original_progress = progress_snapshot.payload
        else:
            progress = {
                "schema": TERMINAL_CONFLICT_PROGRESS_SCHEMA,
                "schema_version": TERMINAL_CONFLICT_PROGRESS_VERSION,
                "review_id": review["review_id"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "decisions": [],
            }
            original_progress = None
        existing = next(
            (value for value in progress["decisions"] if value["case_id"] == case_id),
            None,
        )
        if existing is not None and not overwrite:
            raise TerminalConflictReviewError("Terminal conflict is already decided")
        now = datetime.now(timezone.utc).isoformat()
        replacement = {
            "case_id": case_id,
            "decision": decision,
            "reviewed_at": now,
        }
        if existing is None:
            progress["decisions"].append(replacement)
        else:
            progress["decisions"][progress["decisions"].index(existing)] = replacement
        progress["decisions"].sort(key=lambda value: value["case_id"])
        progress["updated_at"] = now
        _validate_progress(progress, review)
        _assert_progress_carry_forward(progress, review)
        assert_authority_snapshot(review_snapshot, "terminal conflict review")
        _assert_source_authorities(review)
        if (
            original_progress is not None
            and progress_path.read_bytes() != original_progress
        ):
            raise TerminalConflictReviewError(
                "Terminal conflict progress changed before save"
            )
        atomic_write_json(progress_path, progress, sort_keys=True)
        return load_terminal_conflict_review_progress(directory)


def carry_terminal_conflict_decisions(source_directory, target_directory):
    """Carry content-identical decisions into a current-authority review.

    A completed decision belongs to the immutable candidate copies in the
    source review, not to the continued immutability of every unrelated item in
    its source workspace.  The target review independently binds the current
    workspace authorities; the carry ledger binds the exact predecessor review,
    progress and candidate identities that the operator actually heard.
    """
    source_directory = _review_directory(source_directory)
    target_directory = _review_directory(target_directory)
    if source_directory == target_directory:
        raise TerminalConflictReviewError(
            "Terminal conflict carry requires distinct review directories"
        )
    with _progress_lock(target_directory):
        target_progress = target_directory / "progress.json"
        if target_progress.exists() or target_progress.is_symlink():
            raise TerminalConflictReviewError(
                "Target terminal conflict review already has progress"
            )
        try:
            source_review_snapshot = capture_authority_file(
                source_directory / "review.json", "source terminal conflict review"
            )
            source_progress_snapshot = capture_authority_file(
                source_directory / "progress.json",
                "source terminal conflict progress",
            )
            target_review_snapshot = capture_authority_file(
                target_directory / "review.json", "target terminal conflict review"
            )
            source_review = validate_terminal_conflict_review_document(
                source_review_snapshot.json_document("source terminal conflict review"),
                source_directory,
            )
            source_progress = _validate_progress(
                source_progress_snapshot.json_document(
                    "source terminal conflict progress"
                ),
                source_review,
            )
            target_review = validate_terminal_conflict_review_document(
                target_review_snapshot.json_document("target terminal conflict review"),
                target_directory,
            )
        except AuthoringAuthorityError as error:
            raise TerminalConflictReviewError(str(error)) from error
        _assert_progress_carry_forward(source_progress, source_review)
        source_cases = {case["case_id"]: case for case in source_review["cases"]}
        target_cases = {case["case_id"]: case for case in target_review["cases"]}
        carried = []
        for decision in source_progress["decisions"]:
            case_id = decision["case_id"]
            source_case = source_cases.get(case_id)
            target_case = target_cases.get(case_id)
            if source_case is None or target_case is None:
                continue
            source_candidates = [
                candidate["candidate_id"] for candidate in source_case["candidates"]
            ]
            target_candidates = [
                candidate["candidate_id"] for candidate in target_case["candidates"]
            ]
            if source_candidates != target_candidates:
                continue
            carried.append(copy.deepcopy(decision))
        if not carried:
            raise TerminalConflictReviewError(
                "No content-identical terminal conflict decisions can be carried"
            )
        carried.sort(key=lambda value: value["case_id"])
        now = datetime.now(timezone.utc).isoformat()
        progress = {
            "schema": TERMINAL_CONFLICT_PROGRESS_SCHEMA,
            "schema_version": TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION,
            "review_id": target_review["review_id"],
            "updated_at": now,
            "decisions": carried,
            "carry_forward": {
                "source_review": str(source_review_snapshot.path),
                "source_review_sha256": source_review_snapshot.sha256,
                "source_progress": str(source_progress_snapshot.path),
                "source_progress_sha256": source_progress_snapshot.sha256,
                "source_review_id": source_review["review_id"],
                "case_ids": [decision["case_id"] for decision in carried],
            },
        }
        _validate_progress(progress, target_review)
        assert_authority_snapshot(
            source_review_snapshot, "source terminal conflict review"
        )
        assert_authority_snapshot(
            source_progress_snapshot, "source terminal conflict progress"
        )
        assert_authority_snapshot(
            target_review_snapshot, "target terminal conflict review"
        )
        _assert_source_authorities(target_review)
        if target_progress.exists() or target_progress.is_symlink():
            raise TerminalConflictReviewError(
                "Target terminal conflict progress appeared before carry"
            )
        atomic_write_json(target_progress, progress, sort_keys=True)
        return load_terminal_conflict_review_progress(target_directory)


def carry_approved_cohort_terminal_conflict_decisions(directory):
    """Reuse exact human cohort approvals for matching current candidates.

    Rejections are intentionally not promoted: rejecting one cohort WAV does
    not establish that another historical candidate is acceptable.  Every
    selected candidate must be approved in the current state, carry the exact
    cohort sample assessment, and match the review's queue ID and WAV digest.
    Decisions are saved through the normal progress transaction, so a crash can
    leave only a valid prefix and a concurrent authority change still fails
    closed.
    """
    directory = _review_directory(directory)
    review = load_terminal_conflict_review_document(directory)
    _assert_source_authorities(review)
    if (directory / "progress.json").exists():
        progress = load_terminal_conflict_review_progress(directory)
    else:
        progress = {"decisions": []}
    completed = {decision["case_id"] for decision in progress["decisions"]}
    carried = []
    for case in review["cases"]:
        if case["case_id"] in completed:
            continue
        approved = [
            candidate
            for candidate in case["candidates"]
            if candidate["authority"] == "approved"
            and _candidate_has_exact_cohort_approval(case, candidate)
        ]
        if len(approved) > 1:
            raise TerminalConflictReviewError(
                f"Multiple cohort-approved candidates exist: {case['queue_id']}"
            )
        if not approved:
            continue
        progress = record_terminal_conflict_decision(
            directory,
            case["case_id"],
            approved[0]["candidate_id"],
        )
        completed.add(case["case_id"])
        carried.append(case["case_id"])
    if not carried:
        raise TerminalConflictReviewError(
            "No exact approved cohort decisions can be carried"
        )
    return progress


def _candidate_has_exact_cohort_approval(case, candidate):
    for source in candidate["source_authorities"]:
        try:
            snapshot = capture_authority_file(
                source["state"], "cohort-approved terminal conflict state"
            )
            authority = source["review_authority"]
            if snapshot.sha256 != authority["state_sha256"]:
                continue
            state = snapshot.json_document("cohort-approved terminal conflict state")
            item = state.get("items", {}).get(case["queue_id"])
            if (
                not isinstance(item, dict)
                or canonical_document_sha256(item) != authority["item_sha256"]
                or item.get("status") != "approved"
                or item.get("review_status") != "approved"
                or item.get("file_sha256") != candidate["audio_sha256"]
            ):
                continue
            cohort = item.get("cohort_review")
            if not isinstance(cohort, dict) or cohort.get("decision") not in {
                "accepted",
                "split",
            }:
                continue
            samples = cohort.get("reviewed_samples")
            assessments = cohort.get("sample_assessments")
            if (
                not isinstance(samples, list)
                or not any(
                    sample.get("queue_id") == case["queue_id"]
                    and sample.get("audio_sha256") == candidate["audio_sha256"]
                    for sample in samples
                    if isinstance(sample, dict)
                )
                or not isinstance(assessments, list)
                or not any(
                    assessment.get("queue_id") == case["queue_id"]
                    and assessment.get("assessment")
                    in (
                        {"heard", "acceptable"}
                        if cohort["decision"] == "accepted"
                        else {"acceptable"}
                    )
                    for assessment in assessments
                    if isinstance(assessment, dict)
                )
            ):
                continue
            if cohort["decision"] == "split":
                statuses = cohort.get("item_review_statuses")
                if not isinstance(statuses, list) or not any(
                    status.get("queue_id") == case["queue_id"]
                    and status.get("review_status") == "approved"
                    for status in statuses
                    if isinstance(status, dict)
                ):
                    continue
            assert_authority_snapshot(
                snapshot, "cohort-approved terminal conflict state"
            )
            return True
        except AuthoringAuthorityError as error:
            raise TerminalConflictReviewError(str(error)) from error
    return False


def validate_terminal_conflict_review_progress_document(progress, review):
    """Return validated mutable decisions for an already validated review."""
    return _validate_progress(progress, review)


def assert_terminal_conflict_progress_carry_forward(progress, review):
    """Recheck an optional predecessor decision ledger and its authorities."""
    _assert_progress_carry_forward(progress, review)


def assert_terminal_conflict_review_source_authorities(review):
    """Require every source state, queue, item and WAV to match the review."""
    _assert_source_authorities(review)


def _assert_source_authorities(review):
    try:
        report_snapshot = capture_authority_file(
            review["source_reconciliation"], "source reconciliation"
        )
        report = load_authoring_reconciliation(report_snapshot.path).document
    except (AuthoringAuthorityError, AuthoringReconciliationError) as error:
        raise TerminalConflictReviewError(str(error)) from error
    if (
        report_snapshot.sha256 != review["source_reconciliation_sha256"]
        or report["report_id"] != review["source_report_id"]
    ):
        raise TerminalConflictReviewError(
            "Source reconciliation changed after conflict review publication"
        )
    workspace_records = {value["workspace_id"]: value for value in report["workspaces"]}
    for case in review["cases"]:
        for candidate in case["candidates"]:
            for source in candidate["source_authorities"]:
                workspace_id = source["workspace_id"]
                workspace_record = workspace_records.get(workspace_id)
                if workspace_record is None:
                    raise TerminalConflictReviewError(
                        "Terminal conflict workspace disappeared from reconciliation"
                    )
                workspace = Path(workspace_record["workspace"]).resolve()
                expected_state = (
                    workspace / "generated-audio" / "generation-state.json"
                ).resolve()
                expected_queue = (workspace / "queue.jsonl").resolve()
                state_path = Path(source["state"])
                queue_path = Path(source["queue"])
                if state_path != expected_state or queue_path != expected_queue:
                    raise TerminalConflictReviewError(
                        f"Terminal conflict source paths changed: {workspace_id}"
                    )
                try:
                    authority = ReviewAuthority(**source["review_authority"])
                    if (
                        authority.state_sha256 != workspace_record["state_sha256"]
                        or authority.queue_sha256 != workspace_record["queue_sha256"]
                    ):
                        raise TerminalConflictReviewError(
                            f"Terminal conflict reconciliation authority changed: {workspace_id}"
                        )
                    payload = load_review_audio_bytes(
                        state_path,
                        queue_path,
                        case["queue_id"],
                        authority,
                    )
                except (BulkGenerationError, TypeError) as error:
                    raise TerminalConflictReviewError(
                        f"Terminal conflict authority changed: {workspace_id}"
                    ) from error
                if hashlib.sha256(payload).hexdigest() != candidate["audio_sha256"]:
                    raise TerminalConflictReviewError(
                        f"Terminal conflict authority changed: {workspace_id}"
                    )
    assert_authority_snapshot(report_snapshot, "source reconciliation")


def _validate_progress(progress, review):
    value = copy.deepcopy(progress)
    version = value.get("schema_version") if isinstance(value, dict) else None
    required = {"schema", "schema_version", "review_id", "updated_at", "decisions"}
    if version == TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION:
        required.add("carry_forward")
    if (
        not isinstance(value, dict)
        or value.get("schema") != TERMINAL_CONFLICT_PROGRESS_SCHEMA
        or version not in SUPPORTED_TERMINAL_CONFLICT_PROGRESS_VERSIONS
        or value.get("review_id") != review["review_id"]
        or set(value) != required
    ):
        raise TerminalConflictReviewError("Terminal conflict progress is invalid")
    _aware_timestamp(value["updated_at"], "Terminal conflict progress timestamp")
    cases = {item["case_id"]: item for item in review["cases"]}
    decisions = value["decisions"]
    if not isinstance(decisions, list):
        raise TerminalConflictReviewError("Terminal conflict decisions are invalid")
    seen = set()
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {
            "case_id",
            "decision",
            "reviewed_at",
        }:
            raise TerminalConflictReviewError("Terminal conflict decision is malformed")
        case_id = decision["case_id"]
        if case_id in seen or case_id not in cases:
            raise TerminalConflictReviewError(
                "Terminal conflict decision is duplicated"
            )
        seen.add(case_id)
        allowed = {item["candidate_id"] for item in cases[case_id]["candidates"]}
        allowed.add(NEITHER_ACCEPTABLE)
        if decision["decision"] not in allowed:
            raise TerminalConflictReviewError("Terminal conflict winner is invalid")
        _aware_timestamp(
            decision["reviewed_at"], "Terminal conflict decision timestamp"
        )
    if decisions != sorted(decisions, key=lambda item: item["case_id"]):
        raise TerminalConflictReviewError("Terminal conflict decisions are not sorted")
    if version == TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION:
        carry = value["carry_forward"]
        if not isinstance(carry, dict) or set(carry) != {
            "source_review",
            "source_review_sha256",
            "source_progress",
            "source_progress_sha256",
            "source_review_id",
            "case_ids",
        }:
            raise TerminalConflictReviewError(
                "Terminal conflict carry-forward ledger is malformed"
            )
        for field in ("source_review", "source_progress"):
            path = carry.get(field)
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise TerminalConflictReviewError(
                    "Terminal conflict carry-forward path is invalid"
                )
        for field in ("source_review_sha256", "source_progress_sha256"):
            _sha256(carry.get(field), f"Terminal conflict carry-forward {field}")
        _sha256(carry.get("source_review_id"), "Terminal conflict source review ID")
        case_ids = carry.get("case_ids")
        if (
            not isinstance(case_ids, list)
            or not case_ids
            or case_ids != sorted(set(case_ids))
            or not set(case_ids).issubset(seen)
        ):
            raise TerminalConflictReviewError(
                "Terminal conflict carried case identities are invalid"
            )
    return value


def _assert_progress_carry_forward(progress, review, seen=None):
    if progress.get("schema_version") != TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION:
        return
    carry = progress["carry_forward"]
    key = (carry["source_review"], carry["source_progress"])
    observed = set() if seen is None else set(seen)
    if key in observed:
        raise TerminalConflictReviewError(
            "Terminal conflict carry-forward ledger contains a cycle"
        )
    observed.add(key)
    try:
        review_snapshot = capture_authority_file(
            carry["source_review"], "carried terminal conflict review"
        )
        progress_snapshot = capture_authority_file(
            carry["source_progress"], "carried terminal conflict progress"
        )
        if (
            review_snapshot.sha256 != carry["source_review_sha256"]
            or progress_snapshot.sha256 != carry["source_progress_sha256"]
        ):
            raise TerminalConflictReviewError(
                "Carried terminal conflict authority changed"
            )
        source_review = validate_terminal_conflict_review_document(
            review_snapshot.json_document("carried terminal conflict review"),
            review_snapshot.path.parent,
        )
        source_progress = _validate_progress(
            progress_snapshot.json_document("carried terminal conflict progress"),
            source_review,
        )
    except AuthoringAuthorityError as error:
        raise TerminalConflictReviewError(str(error)) from error
    if source_review["review_id"] != carry["source_review_id"]:
        raise TerminalConflictReviewError(
            "Carried terminal conflict review identity changed"
        )
    source_cases = {case["case_id"]: case for case in source_review["cases"]}
    target_cases = {case["case_id"]: case for case in review["cases"]}
    source_decisions = {
        decision["case_id"]: decision for decision in source_progress["decisions"]
    }
    target_decisions = {
        decision["case_id"]: decision for decision in progress["decisions"]
    }
    for case_id in carry["case_ids"]:
        source_case = source_cases.get(case_id)
        target_case = target_cases.get(case_id)
        if (
            source_case is None
            or target_case is None
            or [candidate["candidate_id"] for candidate in source_case["candidates"]]
            != [candidate["candidate_id"] for candidate in target_case["candidates"]]
            or source_decisions.get(case_id) != target_decisions.get(case_id)
        ):
            raise TerminalConflictReviewError(
                "Carried terminal conflict decision identity changed"
            )
    _assert_progress_carry_forward(source_progress, source_review, observed)
    assert_authority_snapshot(review_snapshot, "carried terminal conflict review")
    assert_authority_snapshot(progress_snapshot, "carried terminal conflict progress")


def _contained_file(root, value, label):
    if not isinstance(value, str) or not value or "\\" in value:
        raise TerminalConflictReviewError(f"{label.title()} path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise TerminalConflictReviewError(f"{label.title()} path is invalid")
    root = Path(root).resolve()
    candidate = root / Path(*relative.parts)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise TerminalConflictReviewError(f"{label.title()} is unavailable")
    path = candidate.resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise TerminalConflictReviewError(f"{label.title()} leaves its root") from error
    if not path.is_file():
        raise TerminalConflictReviewError(f"{label.title()} is unavailable")
    return path


def _review_directory(directory):
    argument = Path(directory).expanduser()
    if argument.is_symlink():
        raise TerminalConflictReviewError(
            "Terminal conflict review directory must not be a symlink"
        )
    try:
        resolved = argument.resolve()
    except OSError as error:
        raise TerminalConflictReviewError(
            f"Unable to resolve terminal conflict review directory: {error}"
        ) from error
    if not resolved.is_dir():
        raise TerminalConflictReviewError(
            f"Terminal conflict review directory is unavailable: {resolved}"
        )
    return resolved


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise TerminalConflictReviewError(f"{label} must be non-empty text")
    return value


def _sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TerminalConflictReviewError(f"{label} must be lowercase SHA-256")
    return value


def _aware_timestamp(value, label):
    try:
        parsed = datetime.fromisoformat(_text(value, label))
    except ValueError as error:
        raise TerminalConflictReviewError(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TerminalConflictReviewError(f"{label} requires a timezone")
    return parsed


@contextmanager
def _progress_lock(directory):
    path = directory / ".progress.lock"
    guard_path = directory / ".progress.lock.guard"
    lease = {
        "schema": PROGRESS_LEASE_SCHEMA,
        "schema_version": PROGRESS_LEASE_VERSION,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "process_started_at": process_started_at(os.getpid()),
        "lease_id": uuid.uuid4().hex,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with exclusive_advisory_lock(guard_path):
            if path.exists():
                try:
                    existing_payload = path.read_bytes()
                    existing = json.loads(existing_payload.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise TerminalConflictReviewError(
                        "Unrecognized terminal conflict progress lock blocks review"
                    ) from error
                if (
                    not isinstance(existing, dict)
                    or existing.get("schema") != PROGRESS_LEASE_SCHEMA
                    or existing.get("schema_version") != PROGRESS_LEASE_VERSION
                    or not isinstance(existing.get("pid"), int)
                    or existing["pid"] <= 0
                    or not isinstance(existing.get("hostname"), str)
                    or not existing["hostname"]
                    or not isinstance(existing.get("lease_id"), str)
                    or not existing["lease_id"]
                ):
                    raise TerminalConflictReviewError(
                        "Unrecognized terminal conflict progress lock blocks review"
                    )
                if existing["hostname"] != socket.gethostname():
                    raise TerminalConflictReviewError(
                        "Another terminal conflict decision is being saved"
                    )
                live = process_is_alive(existing["pid"])
                if live:
                    recorded_start = existing.get("process_started_at")
                    actual_start = process_started_at(existing["pid"])
                    if recorded_start is None or actual_start is None:
                        raise TerminalConflictReviewError(
                            "Another terminal conflict decision is being saved"
                        )
                    live = recorded_start == actual_start
                if live:
                    raise TerminalConflictReviewError(
                        "Another terminal conflict decision is being saved"
                    )
                interrupted = directory / (
                    ".progress.lock.interrupted-" + uuid.uuid4().hex
                )
                if path.read_bytes() != existing_payload:
                    raise TerminalConflictReviewError(
                        "Terminal conflict progress lock changed during recovery"
                    )
                try:
                    path.rename(interrupted)
                except OSError as error:
                    raise TerminalConflictReviewError(
                        "Unable to recover an interrupted terminal conflict save"
                    ) from error
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as error:
                raise TerminalConflictReviewError(
                    "Another terminal conflict decision is being saved"
                ) from error
    except AdvisoryLockBusyError as error:
        raise TerminalConflictReviewError(
            "Another terminal conflict decision is being saved"
        ) from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(lease, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        try:
            with exclusive_advisory_lock(guard_path, blocking=True):
                if json.loads(path.read_text(encoding="utf-8")) == lease:
                    path.unlink()
        except (
            OSError,
            json.JSONDecodeError,
            AdvisoryLockBusyError,
        ):
            pass


__all__ = [
    "NEITHER_ACCEPTABLE",
    "TERMINAL_CONFLICT_PROGRESS_SCHEMA",
    "TERMINAL_CONFLICT_PROGRESS_VERSION",
    "TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION",
    "SUPPORTED_TERMINAL_CONFLICT_PROGRESS_VERSIONS",
    "TERMINAL_CONFLICT_REVIEW_SCHEMA",
    "TERMINAL_CONFLICT_REVIEW_VERSION",
    "TerminalConflictReview",
    "TerminalConflictReviewError",
    "assert_terminal_conflict_review_source_authorities",
    "assert_terminal_conflict_progress_carry_forward",
    "carry_approved_cohort_terminal_conflict_decisions",
    "carry_terminal_conflict_decisions",
    "load_terminal_conflict_review",
    "load_terminal_conflict_candidate_audio",
    "load_terminal_conflict_review_document",
    "load_terminal_conflict_review_progress",
    "publish_terminal_conflict_review",
    "record_terminal_conflict_decision",
    "validate_terminal_conflict_review_document",
    "validate_terminal_conflict_review_progress_document",
]
