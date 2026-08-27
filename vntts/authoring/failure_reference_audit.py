"""Checksum-bound blinded reference audit for speech-quality failures."""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts import VoiceGenerationQueue, VoiceGenerationQueueError
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
)

from vntts.authoring.bulk_generation import (
    _canonical_sha256,
    generation_failure_repair_plan,
    normalized_failure_record,
)
from vntts.authoring.game_pack import _rename_directory_no_replace
from vntts.authoring.workbench import AuthoringWorkbenchError, _load_workspace
from vntts.reference_quality import analyze_reference_bytes

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

    def to_dict(self):
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
    workspace_directory, output_directory, *, seed=0, queue_ids=None
):
    """Publish one immutable task over every exact reference-comparison failure."""
    workspace = Path(workspace_directory).expanduser().resolve()
    output = Path(output_directory).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise FailureReferenceAuditError(f"Reference audit output exists: {output}")
    try:
        directory, configuration = _load_workspace(workspace)
    except AuthoringWorkbenchError as error:
        raise FailureReferenceAuditError(str(error)) from error
    queue_path = directory / configuration["queue"]
    state_path = directory / configuration["output"] / "generation-state.json"
    manifest_path = directory / configuration["voice_manifest"]["path"]
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
    records_by_id = {record["queue_id"]: record for record in plan["records"]}
    if queue_ids is None:
        selected = [
            record
            for record in plan["records"]
            if record["action"] == "reference_comparison"
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
    state = json.loads(snapshots["state"].decode("utf-8"))
    grouped = {}
    for record in selected:
        queue_id = record["queue_id"]
        result = state["items"].get(queue_id)
        item = queue_by_id.get(queue_id)
        if not isinstance(result, dict) or item is None:
            raise FailureReferenceAuditError(
                f"Reference audit item disappeared: {queue_id}"
            )
        control_character = (
            configuration["narrator_character"]
            if record["synthesis_voice_character"] == "Narrator"
            else record["synthesis_voice_character"]
        )
        entry = _resolve_voice(voices, control_character)
        identity = {
            "synthesis_voice_character": record["synthesis_voice_character"],
            "control_character": entry.character,
            "speaker": entry.speaker,
            "references": list(entry.references),
            "synthesis_provenance_sha256": result.get("synthesis_provenance_sha256"),
        }
        group_id = _canonical_sha256(identity)
        group = grouped.setdefault(
            group_id,
            {"group_id": group_id, "identity": identity, "cases": []},
        )
        group["cases"].append(
            {
                "queue_id": queue_id,
                "line_id": item.line_id,
                "text": item.text,
                "text_sha256": item.text_sha256,
                "speaker": item.speaker,
                "failure_sha256": _canonical_sha256(result),
                "failure": normalized_failure_record(result, text=item.text),
            }
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    source_files = []
    public_groups = []
    private_groups = []
    try:
        for group_id, group in sorted(grouped.items()):
            candidates = []
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
                    analysis = analyze_reference_bytes(payload, path=source)
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
            public_candidates = []
            private_candidates = []
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
        blind_key_groups_sha256 = _canonical_sha256(private_groups)
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
            "blinded_trial_count": sum(
                len(group["candidates"]) * (len(group["candidates"]) - 1) // 2
                for group in public_groups
            ),
            "blind_key_groups_sha256": blind_key_groups_sha256,
            "groups": public_groups,
            "authority": (
                "A candidate decision audits exact reference bytes only. It does not "
                "approve a failed line or mutate a voice manifest. Neither acceptable "
                "must remain available."
            ),
        }
        audit_id = _canonical_sha256(body)
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
        _rename_directory_no_replace(staging, output)
        return FailureReferenceAudit(
            output,
            audit_id,
            len(selected),
            len(public_groups),
            body["blinded_trial_count"],
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def load_failure_reference_audit(directory):
    """Validate one self-contained audit and its exact source authority."""
    directory = Path(directory).expanduser().resolve()
    audit_path = directory / "audit.json"
    key_path = directory / ".blind-key.json"
    try:
        document = json.loads(audit_path.read_text(encoding="utf-8"))
        key = json.loads(key_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FailureReferenceAuditError(str(error)) from error
    if stat.S_IMODE(key_path.stat().st_mode) != 0o600:
        raise FailureReferenceAuditError("Reference audit blind key mode must be 0600")
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
        != _canonical_sha256(
            {name: value for name, value in document.items() if name != "audit_id"}
        )
        or key.get("audit_id") != claimed
    ):
        raise FailureReferenceAuditError("Reference audit identity changed")
    groups = document.get("groups")
    if not isinstance(groups, list) or document.get("group_count") != len(groups):
        raise FailureReferenceAuditError("Reference audit groups are malformed")
    private_groups = key.get("groups")
    if not isinstance(private_groups, list) or _canonical_sha256(
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
            if not isinstance(result, dict) or _canonical_sha256(result) != case.get(
                "failure_sha256"
            ):
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


def load_failure_reference_decisions(directory):
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
    actual = _canonical_sha256(
        {name: value for name, value in document.items() if name != "decision_set_id"}
    )
    if claimed != actual:
        raise FailureReferenceAuditError("Reference audit decision identity changed")
    _validate_decision_inventory(
        audit.directory,
        document["decisions"],
        schema_version=document["schema_version"],
    )
    return document


def record_failure_reference_decision(
    directory,
    group_id,
    decision,
    *,
    selection_authority=None,
):
    """Atomically record one exact candidate or neither-acceptable decision."""
    audit = load_failure_reference_audit(directory)
    audit_document = json.loads((audit.directory / "audit.json").read_text())
    group = next(
        (value for value in audit_document["groups"] if value["group_id"] == group_id),
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
    decisions = {value["group_id"]: value for value in current["decisions"]}
    recorded = {
        "group_id": group_id,
        "decision": decision,
        "selected_reference_sha256": (
            candidate["sha256"] if candidate is not None else None
        ),
        "case_queue_ids": [value["queue_id"] for value in group["cases"]],
    }
    if selection_authority is not None:
        recorded["selection_authority"] = _validate_selection_authority(
            selection_authority,
            queue_ids=recorded["case_queue_ids"],
            selected_reference_sha256=recorded["selected_reference_sha256"],
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
    document = {**body, "decision_set_id": _canonical_sha256(body)}
    _validate_decision_inventory(
        audit.directory,
        document["decisions"],
        schema_version=FAILURE_REFERENCE_DECISIONS_VERSION,
    )
    final_audit = load_failure_reference_audit(audit.directory)
    if final_audit.audit_id != audit.audit_id:
        raise FailureReferenceAuditError("Reference audit changed before decision save")
    atomic_write_json(audit.directory / "decisions.json", document, sort_keys=True)
    return document


def prepare_failure_reference_audio(directory, group_id, candidate_id):
    """Read and checksum one copied candidate once for immutable Qt playback."""
    audit = load_failure_reference_audit(directory)
    document = json.loads((audit.directory / "audit.json").read_text())
    group = next(
        (value for value in document["groups"] if value["group_id"] == group_id),
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


def _validate_decision_inventory(directory, decisions, *, schema_version):
    audit = json.loads((Path(directory) / "audit.json").read_text())
    groups = {value["group_id"]: value for value in audit["groups"]}
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
        group_id = value["group_id"]
        group = groups.get(group_id)
        if group is None or group_id in seen:
            raise FailureReferenceAuditError(
                "Reference audit decision group is invalid"
            )
        seen.add(group_id)
        decision = value["decision"]
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
                value["selection_authority"],
                queue_ids=value["case_queue_ids"],
                selected_reference_sha256=value["selected_reference_sha256"],
            )


def _validate_selection_authority(
    value,
    *,
    queue_ids,
    selected_reference_sha256,
):
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


def _resolve_voice(voices, character):
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


def _contained_regular_file(directory, relative):
    directory = Path(directory).resolve()
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise FailureReferenceAuditError("Reference audit audio path is malformed")
    relative_path = Path(relative)
    if relative_path.is_absolute() or any(
        part in {"", ".", ".."} for part in relative_path.parts
    ):
        raise FailureReferenceAuditError("Reference audit audio path is malformed")
    raw = directory / relative_path
    current = directory
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise FailureReferenceAuditError("Reference audit audio is unsafe")
    path = raw.resolve()
    try:
        path.relative_to(directory)
    except ValueError as error:
        raise FailureReferenceAuditError(
            "Reference audit audio leaves its directory"
        ) from error
    if not path.is_file():
        raise FailureReferenceAuditError("Reference audit audio changed")
    return path
