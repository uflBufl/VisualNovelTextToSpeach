"""Checksum-bound blinded reference audit for speech-quality failures."""

from __future__ import annotations

import hashlib
import json
import random
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias, TypedDict

from vntts_artifacts import VoiceGenerationQueue, VoiceGenerationQueueError
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import (
    VoiceManifestEntry,
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
)

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    generation_failure_repair_plan,
    normalized_failure_record,
)
from vntts.authoring.private_files import private_file_is_restricted
from vntts.authoring.publication import rename_directory_no_replace, staged_directory
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    load_workspace_authority,
)
from vntts.authoring.workspace_foundation import contained_regular_file
from vntts.reference_quality import analyze_reference_bytes

JsonDocument: TypeAlias = dict[str, object]


class _AuditCase(TypedDict):
    queue_id: str
    text: str


class _AuditCandidate(TypedDict):
    candidate_id: str
    audio: str
    sha256: str


class _AuditGroup(TypedDict):
    group_id: str
    synthesis_voice_character: str
    cases: list[_AuditCase]
    candidates: list[_AuditCandidate]
    decision_options: list[str]


class _AuditIdentity(TypedDict):
    synthesis_voice_character: str
    control_character: str
    speaker: str
    references: list[Path]
    synthesis_provenance_sha256: object


class _AuditCaseDraft(TypedDict):
    queue_id: str
    line_id: str
    text: str
    text_sha256: str
    speaker: str
    failure_sha256: str
    failure: JsonDocument


class _GeneratedGroup(TypedDict):
    group_id: str
    identity: _AuditIdentity
    cases: list[_AuditCaseDraft]


class _ReferenceCandidate(TypedDict):
    source: Path
    source_reference: Path
    sha256: str
    analysis: object
    analysis_error: str | None


def _document(value: object, message: str) -> JsonDocument:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FailureReferenceAuditError(message)
    return {key: item for key, item in value.items()}


def _documents(value: object, message: str) -> list[JsonDocument]:
    if not isinstance(value, list):
        raise FailureReferenceAuditError(message)
    return [_document(item, message) for item in value]


def _text(value: object, message: str) -> str:
    if not isinstance(value, str):
        raise FailureReferenceAuditError(message)
    return value


def _text_list(value: object, message: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise FailureReferenceAuditError(message)
    return list(value)


def _audit_groups(value: object) -> list[_AuditGroup]:
    groups: list[_AuditGroup] = []
    for raw_group in _documents(value, "Reference audit group is malformed"):
        cases: list[_AuditCase] = [
            _AuditCase(
                queue_id=_text(
                    case.get("queue_id"), "Reference audit group is malformed"
                ),
                text=_text(case.get("text"), "Reference audit group is malformed"),
            )
            for case in _documents(
                raw_group.get("cases"), "Reference audit group is malformed"
            )
        ]
        candidates: list[_AuditCandidate] = [
            _AuditCandidate(
                candidate_id=_text(
                    candidate.get("candidate_id"),
                    "Reference audit candidates are malformed",
                ),
                audio=_text(
                    candidate.get("audio"), "Reference audit candidates are malformed"
                ),
                sha256=_text(
                    candidate.get("sha256"), "Reference audit candidates are malformed"
                ),
            )
            for candidate in _documents(
                raw_group.get("candidates"), "Reference audit candidates are malformed"
            )
        ]
        options = raw_group.get("decision_options")
        if not isinstance(options, list) or not all(
            isinstance(option, str) for option in options
        ):
            raise FailureReferenceAuditError("Reference audit candidates are malformed")
        groups.append(
            {
                "group_id": _text(
                    raw_group.get("group_id"), "Reference audit group is malformed"
                ),
                "synthesis_voice_character": _text(
                    raw_group.get("synthesis_voice_character"),
                    "Reference audit group is malformed",
                ),
                "cases": cases,
                "candidates": candidates,
                "decision_options": options,
            }
        )
    return groups


FAILURE_REFERENCE_AUDIT_SCHEMA = "vntts.authoring-failure-reference-audit"
FAILURE_REFERENCE_AUDIT_KEY_SCHEMA = "vntts.authoring-failure-reference-audit-key"
FAILURE_REFERENCE_AUDIT_VERSION = 2
FAILURE_REFERENCE_DECISIONS_SCHEMA = "vntts.authoring-failure-reference-decisions"
FAILURE_REFERENCE_DECISIONS_VERSION = 4
_LEGACY_FAILURE_REFERENCE_DECISIONS_VERSIONS = frozenset({2, 3})
_SELECTION_AUTHORITY_DECISION_VERSIONS = frozenset({3, 4})


class FailureReferenceAuditError(RuntimeError):
    """Reference audit authority is unsafe or has changed."""


@dataclass(frozen=True)
class FailureReferenceAudit:
    directory: Path
    audit_id: str
    case_count: int
    group_count: int
    blinded_trial_count: int

    def to_dict(self) -> JsonDocument:
        return {
            "directory": str(self.directory),
            "audit": str(self.directory / "audit.json"),
            "audit_id": self.audit_id,
            "case_count": self.case_count,
            "group_count": self.group_count,
            "blinded_trial_count": self.blinded_trial_count,
        }


@dataclass(frozen=True)
class FailureReferenceAudio:
    group_id: str
    candidate_id: str
    path: Path
    sha256: str
    payload: bytes


def publish_failure_reference_audit(
    workspace_directory: str | Path,
    output_directory: str | Path,
    *,
    seed: int = 0,
    queue_ids: Sequence[str] | None = None,
) -> FailureReferenceAudit:
    """Publish one immutable task over every exact reference-comparison failure."""
    workspace = Path(workspace_directory).expanduser().resolve()
    output = Path(output_directory).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise FailureReferenceAuditError(f"Reference audit output exists: {output}")
    try:
        directory, configuration, _workspace_sha256 = load_workspace_authority(
            workspace
        )
    except AuthoringWorkbenchError as error:
        raise FailureReferenceAuditError(str(error)) from error
    queue_path = directory / _text(
        configuration.get("queue"), "Workspace queue path is invalid"
    )
    state_path = (
        directory
        / _text(configuration.get("output"), "Workspace output path is invalid")
        / "generation-state.json"
    )
    voice_manifest = _document(
        configuration.get("voice_manifest"), "Workspace voice manifest is invalid"
    )
    manifest_path = directory / _text(
        voice_manifest.get("path"), "Workspace voice manifest path is invalid"
    )
    snapshots = {
        "workspace": (directory / "workspace.json").read_bytes(),
        "queue": queue_path.read_bytes(),
        "state": state_path.read_bytes(),
        "voice_manifest": manifest_path.read_bytes(),
    }
    try:
        queue = VoiceGenerationQueue.load(queue_path)
        _manifest, voices = load_voice_manifest(manifest_path, allow_legacy=False)
    except (VoiceGenerationQueueError, VoiceManifestError) as error:
        raise FailureReferenceAuditError(str(error)) from error
    plan = generation_failure_repair_plan(state_path, queue_path)
    plan_records = _documents(
        plan.get("records"), "Failure repair plan records are invalid"
    )
    records_by_id = {
        _text(record.get("queue_id"), "Failure repair queue ID is invalid"): record
        for record in plan_records
    }
    if queue_ids is None:
        selected = [
            record
            for record in plan_records
            if record.get("action") == "reference_comparison"
        ]
    else:
        requested = tuple(queue_ids)
        if (
            not requested
            or any(not isinstance(value, str) or not value for value in requested)
            or len(set(requested)) != len(requested)
        ):
            raise FailureReferenceAuditError(
                "Explicit reference audit queue IDs must be unique non-empty text"
            )
        missing = sorted(set(requested) - set(records_by_id))
        if missing:
            raise FailureReferenceAuditError(
                "Explicit reference audit items are not current failures: "
                + ", ".join(missing)
            )
        selected = [records_by_id[queue_id] for queue_id in requested]
    if not selected:
        raise FailureReferenceAuditError(
            "Workspace has no reference-comparison failures"
        )
    queue_by_id = {item.queue_id: item for item in queue.items}
    state = _document(
        json.loads(snapshots["state"].decode("utf-8")),
        "Generation state is invalid",
    )
    state_items = _document(state.get("items"), "Generation state items are invalid")
    grouped: dict[str, _GeneratedGroup] = {}
    for record in selected:
        queue_id = _text(record.get("queue_id"), "Failure repair queue ID is invalid")
        result = state_items.get(queue_id)
        item = queue_by_id.get(queue_id)
        if not isinstance(result, dict) or item is None:
            raise FailureReferenceAuditError(
                f"Reference audit item disappeared: {queue_id}"
            )
        synthesis_voice_character = _text(
            record.get("synthesis_voice_character"),
            "Failure repair voice character is invalid",
        )
        control_character = (
            _text(
                configuration.get("narrator_character"),
                "Workspace narrator character is invalid",
            )
            if synthesis_voice_character == "Narrator"
            else synthesis_voice_character
        )
        entry = _resolve_voice(voices, control_character)
        identity: _AuditIdentity = {
            "synthesis_voice_character": synthesis_voice_character,
            "control_character": entry.character,
            "speaker": entry.speaker,
            "references": list(entry.references),
            "synthesis_provenance_sha256": result.get("synthesis_provenance_sha256"),
        }
        group_id = canonical_document_sha256(identity)
        group = grouped.get(group_id)
        if group is None:
            group = _GeneratedGroup(group_id=group_id, identity=identity, cases=[])
            grouped[group_id] = group
        group["cases"].append(
            {
                "queue_id": queue_id,
                "line_id": item.line_id,
                "text": item.text,
                "text_sha256": item.text_sha256,
                "speaker": item.speaker,
                "failure_sha256": canonical_document_sha256(result),
                "failure": normalized_failure_record(result, text=item.text),
            }
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    source_files: list[tuple[Path, str]] = []
    public_groups: list[JsonDocument] = []
    private_groups: list[JsonDocument] = []
    blinded_trial_count = 0
    with staged_directory(output.parent, prefix=f".{output.name}.staging-") as staging:
        for group_id, group in sorted(grouped.items()):
            candidates: list[_ReferenceCandidate] = []
            for reference in group["identity"]["references"]:
                source = (manifest_path.parent / reference).resolve()
                try:
                    source.relative_to(manifest_path.parent.resolve())
                except ValueError as error:
                    raise FailureReferenceAuditError(
                        f"Reference leaves the workspace manifest root: {reference}"
                    ) from error
                if not source.is_file() or source.is_symlink():
                    raise FailureReferenceAuditError(
                        f"Reference is missing or unsafe: {reference}"
                    )
                payload = source.read_bytes()
                digest = hashlib.sha256(payload).hexdigest()
                try:
                    analysis: object = analyze_reference_bytes(payload, path=source)
                    analysis_error = None
                except ValueError as error:
                    analysis = None
                    analysis_error = str(error)
                source_files.append((source, digest))
                candidates.append(
                    {
                        "source": source,
                        "source_reference": reference,
                        "sha256": digest,
                        "analysis": analysis,
                        "analysis_error": analysis_error,
                    }
                )
            order = list(range(len(candidates)))
            random.Random(f"{seed}:{group_id}").shuffle(order)
            public_candidates: list[JsonDocument] = []
            private_candidates: list[JsonDocument] = []
            for position, candidate_index in enumerate(order, start=1):
                candidate = candidates[candidate_index]
                suffix = candidate["source"].suffix.lower() or ".audio"
                relative = (
                    Path("audio") / group_id / f"candidate-{position:02d}{suffix}"
                )
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate["source"], target)
                if sha256_file(target) != candidate["sha256"]:
                    raise FailureReferenceAuditError(
                        "Copied reference checksum changed"
                    )
                public_candidates.append(
                    {
                        "candidate_id": f"candidate-{position:02d}",
                        "audio": relative.as_posix(),
                        "sha256": candidate["sha256"],
                        "analysis": candidate["analysis"],
                        "analysis_error": candidate["analysis_error"],
                    }
                )
                private_candidates.append(
                    {
                        "candidate_id": f"candidate-{position:02d}",
                        "source_reference": candidate["source_reference"],
                        "source_sha256": candidate["sha256"],
                    }
                )
            cases = sorted(group["cases"], key=lambda value: value["queue_id"])
            public_groups.append(
                {
                    "group_id": group_id,
                    "synthesis_voice_character": group["identity"][
                        "synthesis_voice_character"
                    ],
                    "case_count": len(cases),
                    "cases": cases,
                    "candidate_count": len(public_candidates),
                    "candidates": public_candidates,
                    "decision_options": [
                        *(value["candidate_id"] for value in public_candidates),
                        "neither_acceptable",
                    ],
                }
            )
            private_groups.append(
                {
                    "group_id": group_id,
                    "control_character": group["identity"]["control_character"],
                    "speaker": group["identity"]["speaker"],
                    "candidates": private_candidates,
                }
            )
            blinded_trial_count += len(candidates) * (len(candidates) - 1) // 2
        blind_key_groups_sha256 = canonical_document_sha256(private_groups)
        body = {
            "schema": FAILURE_REFERENCE_AUDIT_SCHEMA,
            "schema_version": FAILURE_REFERENCE_AUDIT_VERSION,
            "workspace": str(directory),
            "workspace_id": configuration["workspace_id"],
            "workspace_sha256": hashlib.sha256(snapshots["workspace"]).hexdigest(),
            "queue_sha256": hashlib.sha256(snapshots["queue"]).hexdigest(),
            "state_sha256": hashlib.sha256(snapshots["state"]).hexdigest(),
            "voice_manifest_sha256": hashlib.sha256(
                snapshots["voice_manifest"]
            ).hexdigest(),
            "case_count": len(selected),
            "group_count": len(public_groups),
            "blinded_trial_count": blinded_trial_count,
            "blind_key_groups_sha256": blind_key_groups_sha256,
            "groups": public_groups,
            "authority": (
                "A candidate decision audits exact reference bytes only. It does not "
                "approve a failed line or mutate a voice manifest. Neither acceptable "
                "must remain available."
            ),
        }
        audit_id = canonical_document_sha256(body)
        document = {**body, "audit_id": audit_id}
        key = {
            "schema": FAILURE_REFERENCE_AUDIT_KEY_SCHEMA,
            "schema_version": FAILURE_REFERENCE_AUDIT_VERSION,
            "audit_id": audit_id,
            "groups": private_groups,
        }
        (staging / "audit.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        key_path = staging / ".blind-key.json"
        key_path.write_text(
            json.dumps(key, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        key_path.chmod(0o600)
        for label, path in (
            ("workspace", directory / "workspace.json"),
            ("queue", queue_path),
            ("state", state_path),
            ("voice_manifest", manifest_path),
        ):
            if path.read_bytes() != snapshots[label]:
                raise FailureReferenceAuditError(
                    f"Reference audit {label} changed during publication"
                )
        for source, digest in source_files:
            if sha256_file(source) != digest:
                raise FailureReferenceAuditError(
                    f"Reference changed during publication: {source}"
                )
        rename_directory_no_replace(staging, output)
        return FailureReferenceAudit(
            output,
            audit_id,
            len(selected),
            len(public_groups),
            blinded_trial_count,
        )


def load_failure_reference_audit(directory: str | Path) -> FailureReferenceAudit:
    """Validate one self-contained audit and its exact source authority."""
    directory = Path(directory).expanduser().resolve()
    audit_path = directory / "audit.json"
    key_path = directory / ".blind-key.json"
    if not private_file_is_restricted(key_path):
        raise FailureReferenceAuditError("Reference audit blind key mode must be 0600")
    try:
        document = json.loads(audit_path.read_text(encoding="utf-8"))
        key = json.loads(key_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FailureReferenceAuditError(str(error)) from error
    if (
        document.get("schema") != FAILURE_REFERENCE_AUDIT_SCHEMA
        or document.get("schema_version") != FAILURE_REFERENCE_AUDIT_VERSION
        or key.get("schema") != FAILURE_REFERENCE_AUDIT_KEY_SCHEMA
        or key.get("schema_version") != FAILURE_REFERENCE_AUDIT_VERSION
    ):
        raise FailureReferenceAuditError("Unsupported reference audit schema")
    claimed = document.get("audit_id")
    if (
        claimed
        != canonical_document_sha256(
            {name: value for name, value in document.items() if name != "audit_id"}
        )
        or key.get("audit_id") != claimed
    ):
        raise FailureReferenceAuditError("Reference audit identity changed")
    groups = document.get("groups")
    if not isinstance(groups, list) or document.get("group_count") != len(groups):
        raise FailureReferenceAuditError("Reference audit groups are malformed")
    private_groups = key.get("groups")
    if not isinstance(private_groups, list) or canonical_document_sha256(
        private_groups
    ) != document.get("blind_key_groups_sha256"):
        raise FailureReferenceAuditError("Reference audit blind key changed")
    private_by_group = {}
    for private_group in private_groups:
        if not isinstance(private_group, dict):
            raise FailureReferenceAuditError("Reference audit blind key is malformed")
        group_id = private_group.get("group_id")
        if not isinstance(group_id, str) or group_id in private_by_group:
            raise FailureReferenceAuditError("Reference audit blind key is malformed")
        private_by_group[group_id] = private_group
    cases = 0
    group_ids = set()
    blinded_trial_count = 0
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("cases"), list):
            raise FailureReferenceAuditError("Reference audit group is malformed")
        group_id = group.get("group_id")
        if not isinstance(group_id, str) or group_id in group_ids:
            raise FailureReferenceAuditError("Reference audit group is malformed")
        group_ids.add(group_id)
        candidates = group.get("candidates")
        if (
            not isinstance(candidates, list)
            or group.get("candidate_count") != len(candidates)
            or not candidates
        ):
            raise FailureReferenceAuditError("Reference audit candidates are malformed")
        candidate_ids = [value.get("candidate_id") for value in candidates]
        if (
            any(not isinstance(value, str) or not value for value in candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
            or group.get("decision_options") != [*candidate_ids, "neither_acceptable"]
        ):
            raise FailureReferenceAuditError("Reference audit candidates are malformed")
        private_group = private_by_group.get(group_id)
        private_candidates = (
            private_group.get("candidates") if isinstance(private_group, dict) else None
        )
        if not isinstance(private_candidates, list) or [
            (value.get("candidate_id"), value.get("source_sha256"))
            for value in private_candidates
            if isinstance(value, dict)
        ] != [(value["candidate_id"], value.get("sha256")) for value in candidates]:
            raise FailureReferenceAuditError("Reference audit blind key is malformed")
        cases += len(group["cases"])
        if group.get("case_count") != len(group["cases"]):
            raise FailureReferenceAuditError("Reference audit case count changed")
        blinded_trial_count += len(candidates) * (len(candidates) - 1) // 2
        for candidate in candidates:
            relative = candidate.get("audio")
            path = _contained_regular_file(directory, relative)
            if sha256_file(path) != candidate.get("sha256"):
                raise FailureReferenceAuditError("Reference audit audio changed")
    if set(private_by_group) != group_ids:
        raise FailureReferenceAuditError("Reference audit blind key is malformed")
    if cases != document.get("case_count"):
        raise FailureReferenceAuditError("Reference audit case count changed")
    if blinded_trial_count != document.get("blinded_trial_count"):
        raise FailureReferenceAuditError("Reference audit trial count changed")
    workspace = Path(document.get("workspace", "")).expanduser().resolve()
    for field, path in (
        ("workspace_sha256", workspace / "workspace.json"),
        ("queue_sha256", workspace / "queue.jsonl"),
        ("voice_manifest_sha256", workspace / "inputs/voice/manifest.json"),
    ):
        if not path.is_file() or sha256_file(path) != document.get(field):
            raise FailureReferenceAuditError(
                f"Reference audit source authority changed: {field}"
            )
    try:
        state = json.loads(
            (workspace / "generated-audio/generation-state.json").read_text()
        )
    except (OSError, json.JSONDecodeError) as error:
        raise FailureReferenceAuditError(str(error)) from error
    for group in groups:
        for case in group["cases"]:
            result = state.get("items", {}).get(case["queue_id"])
            if not isinstance(result, dict) or canonical_document_sha256(
                result
            ) != case.get("failure_sha256"):
                raise FailureReferenceAuditError(
                    f"Reference audit failure authority changed: {case['queue_id']}"
                )
    return FailureReferenceAudit(
        directory,
        claimed,
        document["case_count"],
        document["group_count"],
        document["blinded_trial_count"],
    )


def load_failure_reference_decisions(directory: str | Path) -> JsonDocument:
    """Load the current exact decision set, or an empty set when not started."""
    audit = load_failure_reference_audit(directory)
    path = audit.directory / "decisions.json"
    if not path.is_file():
        return {
            "schema": FAILURE_REFERENCE_DECISIONS_SCHEMA,
            "schema_version": FAILURE_REFERENCE_DECISIONS_VERSION,
            "audit_id": audit.audit_id,
            "decisions": [],
            "decision_set_id": None,
            "updated_at": None,
        }
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FailureReferenceAuditError(str(error)) from error
    if (
        document.get("schema") != FAILURE_REFERENCE_DECISIONS_SCHEMA
        or document.get("schema_version")
        not in {
            *_LEGACY_FAILURE_REFERENCE_DECISIONS_VERSIONS,
            FAILURE_REFERENCE_DECISIONS_VERSION,
        }
        or document.get("audit_id") != audit.audit_id
        or not isinstance(document.get("decisions"), list)
        or not isinstance(document.get("updated_at"), str)
    ):
        raise FailureReferenceAuditError("Reference audit decisions are malformed")
    try:
        updated_at = datetime.fromisoformat(document["updated_at"])
    except ValueError as error:
        raise FailureReferenceAuditError(
            "Reference audit decision timestamp is malformed"
        ) from error
    if updated_at.tzinfo is None or updated_at.utcoffset() is None:
        raise FailureReferenceAuditError(
            "Reference audit decision timestamp must include a timezone"
        )
    claimed = document.get("decision_set_id")
    actual = canonical_document_sha256(
        {name: value for name, value in document.items() if name != "decision_set_id"}
    )
    if claimed != actual:
        raise FailureReferenceAuditError("Reference audit decision identity changed")
    _validate_decision_inventory(
        audit.directory,
        document["decisions"],
        schema_version=document["schema_version"],
    )
    return _document(document, "Reference audit decisions are malformed")


def record_failure_reference_decision(
    directory: str | Path,
    group_id: str,
    decision: str,
    *,
    selection_authority: JsonDocument | None = None,
) -> JsonDocument:
    """Atomically record one exact candidate or neither-acceptable decision."""
    audit = load_failure_reference_audit(directory)
    audit_document = _document(
        json.loads((audit.directory / "audit.json").read_text()),
        "Reference audit group is malformed",
    )
    groups = _audit_groups(audit_document.get("groups"))
    group = next(
        (value for value in groups if value["group_id"] == group_id),
        None,
    )
    if group is None:
        raise FailureReferenceAuditError(
            f"Reference audit group is unknown: {group_id}"
        )
    if decision not in group["decision_options"]:
        raise FailureReferenceAuditError("Reference audit decision is unsupported")
    candidate = next(
        (value for value in group["candidates"] if value["candidate_id"] == decision),
        None,
    )
    current = load_failure_reference_decisions(audit.directory)
    decisions = {
        _text(value.get("group_id"), "Reference audit decision is malformed"): value
        for value in _documents(
            current.get("decisions"), "Reference audit decision is malformed"
        )
    }
    selected_reference_sha256 = candidate["sha256"] if candidate is not None else None
    case_queue_ids = [value["queue_id"] for value in group["cases"]]
    recorded: JsonDocument = {
        "group_id": group_id,
        "decision": decision,
        "selected_reference_sha256": selected_reference_sha256,
        "case_queue_ids": case_queue_ids,
    }
    if selection_authority is not None:
        recorded["selection_authority"] = _validate_selection_authority(
            selection_authority,
            queue_ids=case_queue_ids,
            selected_reference_sha256=selected_reference_sha256,
        )
    decisions[group_id] = recorded
    updated_at = datetime.now(timezone.utc).isoformat()
    body = {
        "schema": FAILURE_REFERENCE_DECISIONS_SCHEMA,
        "schema_version": FAILURE_REFERENCE_DECISIONS_VERSION,
        "audit_id": audit.audit_id,
        "decisions": [decisions[key] for key in sorted(decisions)],
        "updated_at": updated_at,
    }
    document = {**body, "decision_set_id": canonical_document_sha256(body)}
    _validate_decision_inventory(
        audit.directory,
        _documents(document["decisions"], "Reference audit decision is malformed"),
        schema_version=FAILURE_REFERENCE_DECISIONS_VERSION,
    )
    final_audit = load_failure_reference_audit(audit.directory)
    if final_audit.audit_id != audit.audit_id:
        raise FailureReferenceAuditError("Reference audit changed before decision save")
    atomic_write_json(audit.directory / "decisions.json", document, sort_keys=True)
    return document


def prepare_failure_reference_audio(
    directory: str | Path, group_id: str, candidate_id: str
) -> FailureReferenceAudio:
    """Read and checksum one copied candidate once for immutable Qt playback."""
    audit = load_failure_reference_audit(directory)
    document = _document(
        json.loads((audit.directory / "audit.json").read_text()),
        "Reference audit group is malformed",
    )
    groups = _audit_groups(document.get("groups"))
    group = next(
        (value for value in groups if value["group_id"] == group_id),
        None,
    )
    if group is None:
        raise FailureReferenceAuditError(
            f"Reference audit group is unknown: {group_id}"
        )
    candidate = next(
        (
            value
            for value in group["candidates"]
            if value["candidate_id"] == candidate_id
        ),
        None,
    )
    if candidate is None:
        raise FailureReferenceAuditError(
            f"Reference audit candidate is unknown: {candidate_id}"
        )
    path = _contained_regular_file(audit.directory, candidate["audio"])
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != candidate["sha256"]:
        raise FailureReferenceAuditError("Reference audit audio changed")
    return FailureReferenceAudio(group_id, candidate_id, path, digest, payload)


def _validate_decision_inventory(
    directory: str | Path, decisions: Sequence[JsonDocument], *, schema_version: int
) -> None:
    audit = _document(
        json.loads((Path(directory) / "audit.json").read_text()),
        "Reference audit group is malformed",
    )
    groups = {value["group_id"]: value for value in _audit_groups(audit.get("groups"))}
    seen = set()
    for value in decisions:
        required = {
            "group_id",
            "decision",
            "selected_reference_sha256",
            "case_queue_ids",
        }
        accepted_shapes = {frozenset(required)}
        if schema_version in _SELECTION_AUTHORITY_DECISION_VERSIONS:
            accepted_shapes.add(frozenset({*required, "selection_authority"}))
        if not isinstance(value, dict) or frozenset(value) not in accepted_shapes:
            raise FailureReferenceAuditError("Reference audit decision is malformed")
        group_id = _text(value["group_id"], "Reference audit decision group is invalid")
        group = groups.get(group_id)
        if group is None or group_id in seen:
            raise FailureReferenceAuditError(
                "Reference audit decision group is invalid"
            )
        seen.add(group_id)
        decision = _text(value["decision"], "Reference audit decision is unsupported")
        if decision not in group["decision_options"]:
            raise FailureReferenceAuditError("Reference audit decision is unsupported")
        candidate = next(
            (item for item in group["candidates"] if item["candidate_id"] == decision),
            None,
        )
        expected_hash = candidate["sha256"] if candidate is not None else None
        if value["selected_reference_sha256"] != expected_hash or value[
            "case_queue_ids"
        ] != [item["queue_id"] for item in group["cases"]]:
            raise FailureReferenceAuditError(
                "Reference audit decision authority changed"
            )
        if "selection_authority" in value:
            _validate_selection_authority(
                _document(
                    value["selection_authority"],
                    "Reference audit selection authority is malformed",
                ),
                queue_ids=_text_list(
                    value["case_queue_ids"],
                    "Reference audit decision authority changed",
                ),
                selected_reference_sha256=(
                    value["selected_reference_sha256"]
                    if isinstance(value["selected_reference_sha256"], str)
                    else None
                ),
            )


def _validate_selection_authority(
    value: JsonDocument,
    *,
    queue_ids: list[str],
    selected_reference_sha256: str | None,
) -> JsonDocument:
    blind_required = {
        "schema",
        "schema_version",
        "comparison_id",
        "comparison_sha256",
        "source_audit_id",
        "source_audit_sha256",
        "listening_session_sha256",
        "listening_key_sha256",
        "listening_report_sha256",
        "trial_id",
        "selected_side",
        "selected_arm_id",
        "selected_render_sha256",
        "source_candidate_group_id",
        "source_candidate_id",
        "source_reference",
        "selected_reference_sha256",
        "queue_id",
        "text_sha256",
    }
    hypothesis_required = {
        "schema",
        "schema_version",
        "review_id",
        "review_sha256",
        "decision_sha256",
        "comparison_id",
        "comparison_sha256",
        "source_audit_id",
        "source_audit_sha256",
        "selected_arm_id",
        "selected_arm_report_sha256",
        "selected_render_sha256",
        "source_candidate_group_id",
        "source_candidate_id",
        "source_reference",
        "selected_reference_sha256",
        "queue_id",
        "text_sha256",
    }
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise FailureReferenceAuditError(
            "Reference audit selection authority is malformed"
        )
    schema = value.get("schema")
    if schema == "vntts.authoring-reference-render-selection":
        if set(value) != blind_required or value.get("selected_side") not in {"a", "b"}:
            raise FailureReferenceAuditError(
                "Reference audit selection authority is malformed"
            )
        hash_fields = {
            "comparison_id",
            "comparison_sha256",
            "source_audit_id",
            "source_audit_sha256",
            "listening_session_sha256",
            "listening_key_sha256",
            "listening_report_sha256",
            "selected_render_sha256",
            "source_candidate_group_id",
            "selected_reference_sha256",
            "text_sha256",
        }
        text_fields = {
            "trial_id",
            "selected_arm_id",
            "source_candidate_id",
            "source_reference",
            "queue_id",
        }
    elif schema == "vntts.authoring-render-hypothesis-selection":
        if set(value) != hypothesis_required:
            raise FailureReferenceAuditError(
                "Reference audit selection authority is malformed"
            )
        hash_fields = {
            "review_id",
            "review_sha256",
            "decision_sha256",
            "comparison_id",
            "comparison_sha256",
            "source_audit_id",
            "source_audit_sha256",
            "selected_arm_report_sha256",
            "selected_render_sha256",
            "source_candidate_group_id",
            "selected_reference_sha256",
            "text_sha256",
        }
        text_fields = {
            "selected_arm_id",
            "source_candidate_id",
            "source_reference",
            "queue_id",
        }
    else:
        raise FailureReferenceAuditError(
            "Reference audit selection authority is malformed"
        )
    for field in hash_fields:
        digest = value[field]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise FailureReferenceAuditError(
                "Reference audit selection authority hash is malformed"
            )
    for field in text_fields:
        text = value[field]
        if not isinstance(text, str) or not text or text != text.strip():
            raise FailureReferenceAuditError(
                "Reference audit selection authority text is malformed"
            )
    if (
        queue_ids != [value["queue_id"]]
        or value["selected_reference_sha256"] != selected_reference_sha256
    ):
        raise FailureReferenceAuditError("Reference audit selection authority changed")
    return dict(value)


def _resolve_voice(
    voices: Sequence[VoiceManifestEntry], character: str
) -> VoiceManifestEntry:
    wanted = normalize_character_name(character)
    matches = [
        voice
        for voice in voices
        if wanted
        in {
            normalize_character_name(voice.character),
            *(normalize_character_name(alias) for alias in voice.aliases),
        }
    ]
    if len(matches) != 1 or not matches[0].references:
        raise FailureReferenceAuditError(
            f"Reference audit voice is absent or ambiguous: {character!r}"
        )
    return matches[0]


def _contained_regular_file(directory: str | Path, relative: object) -> Path:
    return Path(
        contained_regular_file(
            directory,
            relative,
            "reference audit audio",
            error_type=FailureReferenceAuditError,
        )
    )
