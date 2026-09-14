"""Publish exact selected-reference overlays from a completed failure audit."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import NotRequired, TypeAlias, TypedDict

from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.failure_reference_binding_records import (
    FAILURE_REFERENCE_BINDING_SCHEMA,
    FAILURE_REFERENCE_BINDING_VERSION,
    FailureReferenceBinding,
    FailureReferenceBindingError,
    _contained_regular_file,
    _safe_relative,
    _sha256,
    _text,
    load_failure_reference_binding,
)
from vntts.authoring.failure_reference_binding_records import (
    load_failure_reference_binding_document as load_failure_reference_binding_document,
)
from vntts.authoring.private_files import private_file_is_restricted
from vntts.authoring.publication import rename_directory_no_replace, staged_directory
from vntts.authoring.source_reference_bindings import queue_voice_overrides_sha256
from vntts.document_identity import canonical_document_sha256

_AUDIT_SCHEMA = "vntts.authoring-failure-reference-audit"
_AUDIT_KEY_SCHEMA = "vntts.authoring-failure-reference-audit-key"
_DECISIONS_SCHEMA = "vntts.authoring-failure-reference-decisions"
_AUDIT_VERSION = 2
_DECISIONS_VERSION = 4
_LEGACY_DECISIONS_VERSIONS = frozenset({2, 3})

JsonDocument: TypeAlias = dict[str, object]


class _AuditCase(TypedDict):
    queue_id: str
    failure_sha256: str


class _AuditCandidate(TypedDict):
    candidate_id: str
    audio: str
    sha256: str


class _AuditGroup(TypedDict):
    group_id: str
    synthesis_voice_character: str
    candidates: list[_AuditCandidate]
    cases: list[_AuditCase]


class _PrivateCandidate(TypedDict):
    candidate_id: str
    source_reference: str
    source_sha256: str


class _PrivateGroup(TypedDict):
    group_id: str
    control_character: str
    speaker: str
    candidates: list[_PrivateCandidate]


class _Decision(TypedDict):
    group_id: str
    decision: str
    selected_reference_sha256: str | None
    case_queue_ids: list[str]
    selection_authority: NotRequired[object]


class _AuditSnapshot(TypedDict):
    audit_id: str
    workspace_id: str
    workspace_sha256: str
    queue_sha256: str
    state_sha256: str
    voice_manifest_sha256: str
    groups: list[_AuditGroup]


class _KeySnapshot(TypedDict):
    groups: list[_PrivateGroup]


class _DecisionSnapshot(TypedDict):
    decision_set_id: str
    decisions: list[_Decision]


class _AuditSnapshots(TypedDict):
    audit: _AuditSnapshot
    key: _KeySnapshot
    decisions: _DecisionSnapshot
    payloads: dict[str, bytes]
    audit_sha256: str
    key_sha256: str
    decisions_sha256: str


def _document(value: object, message: str) -> JsonDocument:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FailureReferenceBindingError(message)
    return {key: item for key, item in value.items()}


def _documents(value: object, message: str) -> list[JsonDocument]:
    if not isinstance(value, list):
        raise FailureReferenceBindingError(message)
    return [_document(item, message) for item in value]


def _text_list(value: object, message: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise FailureReferenceBindingError(message)
    return list(value)


def _audit_groups(value: object) -> list[_AuditGroup]:
    groups: list[_AuditGroup] = []
    for raw_group in _documents(value, "Reference audit inventory is malformed"):
        cases = [
            _AuditCase(
                queue_id=_text(case.get("queue_id"), "Reference binding queue ID"),
                failure_sha256=_sha256(
                    case.get("failure_sha256"), "Reference failure SHA-256"
                ),
            )
            for case in _documents(
                raw_group.get("cases"), "Reference audit inventory is malformed"
            )
        ]
        candidates = [
            _AuditCandidate(
                candidate_id=_text(
                    candidate.get("candidate_id"), "Reference candidate ID"
                ),
                audio=_text(candidate.get("audio"), "Reference candidate audio"),
                sha256=_sha256(candidate.get("sha256"), "Reference candidate SHA-256"),
            )
            for candidate in _documents(
                raw_group.get("candidates"), "Reference audit inventory is malformed"
            )
        ]
        groups.append(
            _AuditGroup(
                group_id=_text(raw_group.get("group_id"), "Reference audit group ID"),
                synthesis_voice_character=_text(
                    raw_group.get("synthesis_voice_character"),
                    "Audited synthesis voice",
                ),
                candidates=candidates,
                cases=cases,
            )
        )
    return groups


def _private_groups(value: object) -> list[_PrivateGroup]:
    groups: list[_PrivateGroup] = []
    for raw_group in _documents(value, "Reference audit inventory is malformed"):
        candidates = [
            _PrivateCandidate(
                candidate_id=_text(
                    candidate.get("candidate_id"), "Reference candidate ID"
                ),
                source_reference=_text(
                    candidate.get("source_reference"), "Audited source reference"
                ),
                source_sha256=_sha256(
                    candidate.get("source_sha256"), "Reference candidate SHA-256"
                ),
            )
            for candidate in _documents(
                raw_group.get("candidates"), "Reference audit inventory is malformed"
            )
        ]
        groups.append(
            _PrivateGroup(
                group_id=_text(raw_group.get("group_id"), "Reference audit group ID"),
                control_character=_text(
                    raw_group.get("control_character"), "Audited control character"
                ),
                speaker=_text(raw_group.get("speaker"), "Audited speaker"),
                candidates=candidates,
            )
        )
    return groups


def _decisions(value: object) -> list[_Decision]:
    decisions: list[_Decision] = []
    for raw_decision in _documents(value, "Reference audit inventory is malformed"):
        selected = raw_decision.get("selected_reference_sha256")
        if selected is not None:
            selected = _sha256(selected, "Reference selected SHA-256")
        decision = _Decision(
            group_id=_text(raw_decision.get("group_id"), "Reference audit group ID"),
            decision=_text(raw_decision.get("decision"), "Reference audit decision"),
            selected_reference_sha256=selected,
            case_queue_ids=_text_list(
                raw_decision.get("case_queue_ids"),
                "Reference audit inventory is malformed",
            ),
        )
        if "selection_authority" in raw_decision:
            decision["selection_authority"] = raw_decision["selection_authority"]
        decisions.append(decision)
    return decisions


def publish_failure_reference_binding(
    audit_directory: str | Path, output_directory: str | Path
) -> FailureReferenceBinding:
    """Publish one self-contained, no-replace overlay from terminal decisions."""
    audit_argument = Path(audit_directory).expanduser()
    output_argument = Path(output_directory).expanduser()
    if audit_argument.is_symlink() or output_argument.is_symlink():
        raise FailureReferenceBindingError(
            "Reference binding input and output must not be symlinks"
        )
    audit_directory = audit_argument.resolve()
    output = output_argument.resolve()
    from vntts.authoring.failure_reference_audit import (
        FailureReferenceAuditError,
        load_failure_reference_audit,
        load_failure_reference_decisions,
    )

    try:
        validated_audit = load_failure_reference_audit(audit_directory)
        validated_decisions = load_failure_reference_decisions(audit_directory)
    except FailureReferenceAuditError as error:
        raise FailureReferenceBindingError(str(error)) from error
    snapshots = _load_audit_snapshots(audit_directory)
    audit = snapshots["audit"]
    key = snapshots["key"]
    decisions = snapshots["decisions"]
    if validated_audit.audit_id != audit["audit_id"] or validated_decisions.get(
        "decision_set_id"
    ) != decisions.get("decision_set_id"):
        raise FailureReferenceBindingError(
            "Reference audit changed while binding inputs were captured"
        )
    groups = {value["group_id"]: value for value in audit["groups"]}
    private_groups = {value["group_id"]: value for value in key["groups"]}
    decision_by_group = {value["group_id"]: value for value in decisions["decisions"]}
    if set(decision_by_group) != set(groups):
        raise FailureReferenceBindingError(
            "Reference binding requires one terminal decision for every audit group"
        )

    stable_groups: list[JsonDocument] = []
    overrides: dict[str, str] = {}
    sources: list[tuple[Path, str, Path, bytes]] = []
    for group_id in sorted(groups):
        group = groups[group_id]
        private = private_groups[group_id]
        decision = decision_by_group[group_id]
        candidate_id = decision["decision"]
        if candidate_id == "neither_acceptable":
            raise FailureReferenceBindingError(
                f"Reference binding cannot publish a rejected group: {group_id}"
            )
        public_candidate = next(
            value
            for value in group["candidates"]
            if value["candidate_id"] == candidate_id
        )
        private_candidate = next(
            value
            for value in private["candidates"]
            if value["candidate_id"] == candidate_id
        )
        source = _contained_regular_file(
            audit_directory, public_candidate["audio"], "audit candidate"
        )
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if (
            digest != public_candidate["sha256"]
            or digest != private_candidate["source_sha256"]
            or digest != decision["selected_reference_sha256"]
        ):
            raise FailureReferenceBindingError(
                f"Selected reference authority changed: {group_id}"
            )
        suffix = source.suffix.lower() or ".audio"
        relative = Path("references") / group_id / f"selected{suffix}"
        synthetic_voice = f"Selected failure reference {group_id[:16]}"
        cases = []
        for case in group["cases"]:
            queue_id = _text(case.get("queue_id"), "Reference binding queue ID")
            if queue_id in overrides:
                raise FailureReferenceBindingError(
                    f"Reference binding queue ID belongs to multiple groups: {queue_id}"
                )
            overrides[queue_id] = synthetic_voice
            cases.append(
                {
                    "queue_id": queue_id,
                    "failure_sha256": _sha256(
                        case.get("failure_sha256"), "Reference failure SHA-256"
                    ),
                }
            )
        if decision["case_queue_ids"] != [value["queue_id"] for value in cases]:
            raise FailureReferenceBindingError(
                f"Reference binding case authority changed: {group_id}"
            )
        stable_group: JsonDocument = {
            "group_id": group_id,
            "synthesis_voice_character": _text(
                group.get("synthesis_voice_character"),
                "Audited synthesis voice",
            ),
            "control_character": _text(
                private.get("control_character"), "Audited control character"
            ),
            "speaker": _text(private.get("speaker"), "Audited speaker"),
            "candidate_id": candidate_id,
            "voice_character": synthetic_voice,
            "reference": relative.as_posix(),
            "reference_sha256": digest,
            "source_reference": _safe_relative(
                private_candidate.get("source_reference"),
                "Audited source reference",
            ).as_posix(),
            "cases": cases,
        }
        if "selection_authority" in decision:
            stable_group["selection_authority"] = decision["selection_authority"]
        stable_groups.append(stable_group)
        sources.append((source, digest, relative, payload))

    identity = {
        "schema": FAILURE_REFERENCE_BINDING_SCHEMA,
        "schema_version": FAILURE_REFERENCE_BINDING_VERSION,
        "audit_id": audit["audit_id"],
        "decision_set_id": decisions["decision_set_id"],
        "source_authority": {
            "workspace_id": audit["workspace_id"],
            "workspace_sha256": audit["workspace_sha256"],
            "queue_sha256": audit["queue_sha256"],
            "state_sha256": audit["state_sha256"],
            "voice_manifest_sha256": audit["voice_manifest_sha256"],
            "audit_sha256": snapshots["audit_sha256"],
            "blind_key_sha256": snapshots["key_sha256"],
            "decisions_sha256": snapshots["decisions_sha256"],
        },
        "groups": stable_groups,
        "queue_voice_overrides": dict(sorted(overrides.items())),
        "queue_voice_overrides_sha256": queue_voice_overrides_sha256(overrides),
        "authority": (
            "This overlay selects reference bytes for exact failed queue IDs only. "
            "It does not approve generated audio or rewrite the source voice manifest."
        ),
    }
    binding_id = canonical_document_sha256(identity)
    document = {
        **identity,
        "binding_id": binding_id,
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    if output.exists() or output.is_symlink():
        existing = load_failure_reference_binding(output)
        if existing.binding_id != binding_id:
            raise FailureReferenceBindingError(
                f"Reference binding output conflicts with another identity: {output}"
            )
        return FailureReferenceBinding(
            existing.directory,
            existing.binding_id,
            existing.audit_id,
            existing.decision_set_id,
            existing.group_count,
            existing.case_count,
            False,
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with staged_directory(output.parent, prefix=f".{output.name}.staging-") as staging:
        for _source, digest, relative, payload in sources:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            if sha256_file(target) != digest:
                raise FailureReferenceBindingError(
                    "Selected reference changed while it was copied"
                )
        (staging / "binding.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        load_failure_reference_binding(staging)
        _assert_audit_snapshots_unchanged(audit_directory, snapshots)
        try:
            final_audit = load_failure_reference_audit(audit_directory)
            final_decisions = load_failure_reference_decisions(audit_directory)
        except FailureReferenceAuditError as error:
            raise FailureReferenceBindingError(str(error)) from error
        if (
            final_audit.audit_id != audit["audit_id"]
            or final_decisions.get("decision_set_id") != decisions["decision_set_id"]
        ):
            raise FailureReferenceBindingError(
                "Reference audit changed before binding publication"
            )
        for source, digest, _relative, _payload in sources:
            if not source.is_file() or sha256_file(source) != digest:
                raise FailureReferenceBindingError(
                    "Selected reference changed before binding publication"
                )
        rename_directory_no_replace(staging, output)
    return FailureReferenceBinding(
        output,
        binding_id,
        audit["audit_id"],
        decisions["decision_set_id"],
        len(stable_groups),
        len(overrides),
        True,
    )


def _load_audit_snapshots(directory: Path) -> _AuditSnapshots:
    paths = {
        "audit": directory / "audit.json",
        "key": directory / ".blind-key.json",
        "decisions": directory / "decisions.json",
    }
    if not paths["decisions"].is_file():
        raise FailureReferenceBindingError(
            "Reference binding requires terminal decisions"
        )
    if not private_file_is_restricted(paths["key"]):
        raise FailureReferenceBindingError(
            "Reference audit blind key mode must be 0600"
        )
    try:
        payloads = {name: path.read_bytes() for name, path in paths.items()}
        documents: dict[str, JsonDocument] = {
            name: _document(
                json.loads(payload.decode("utf-8")),
                "Reference audit inventory is malformed",
            )
            for name, payload in payloads.items()
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FailureReferenceBindingError(str(error)) from error
    audit = documents["audit"]
    key = documents["key"]
    decisions = documents["decisions"]
    if (
        audit.get("schema") != _AUDIT_SCHEMA
        or audit.get("schema_version") != _AUDIT_VERSION
        or key.get("schema") != _AUDIT_KEY_SCHEMA
        or key.get("schema_version") != _AUDIT_VERSION
        or decisions.get("schema") != _DECISIONS_SCHEMA
        or decisions.get("schema_version")
        not in {*_LEGACY_DECISIONS_VERSIONS, _DECISIONS_VERSION}
    ):
        raise FailureReferenceBindingError("Unsupported reference audit schema")
    audit_id = _sha256(audit.get("audit_id"), "Reference audit ID")
    if (
        audit_id
        != canonical_document_sha256(
            {name: value for name, value in audit.items() if name != "audit_id"}
        )
        or key.get("audit_id") != audit_id
        or decisions.get("audit_id") != audit_id
    ):
        raise FailureReferenceBindingError("Reference audit identity changed")
    decision_set_id = _sha256(
        decisions.get("decision_set_id"), "Reference decision-set ID"
    )
    if decision_set_id != canonical_document_sha256(
        {name: value for name, value in decisions.items() if name != "decision_set_id"}
    ):
        raise FailureReferenceBindingError("Reference decision identity changed")
    groups = _documents(audit.get("groups"), "Reference audit inventory is malformed")
    private_groups = _documents(
        key.get("groups"), "Reference audit inventory is malformed"
    )
    decision_values = _documents(
        decisions.get("decisions"), "Reference audit inventory is malformed"
    )
    if audit.get("group_count") != len(groups) or canonical_document_sha256(
        private_groups
    ) != audit.get("blind_key_groups_sha256"):
        raise FailureReferenceBindingError("Reference audit inventory changed")
    group_ids = [value.get("group_id") for value in groups if isinstance(value, dict)]
    private_ids = [
        value.get("group_id") for value in private_groups if isinstance(value, dict)
    ]
    decision_ids = [
        value.get("group_id") for value in decision_values if isinstance(value, dict)
    ]
    if (
        len(group_ids) != len(groups)
        or len(private_ids) != len(private_groups)
        or len(decision_ids) != len(decision_values)
        or len(set(group_ids)) != len(group_ids)
        or set(group_ids) != set(private_ids)
        or len(set(decision_ids)) != len(decision_ids)
        or not set(decision_ids).issubset(group_ids)
    ):
        raise FailureReferenceBindingError("Reference audit group identity changed")
    for field in (
        "workspace_sha256",
        "queue_sha256",
        "state_sha256",
        "voice_manifest_sha256",
    ):
        _sha256(audit.get(field), f"Reference audit {field}")
    _text(audit.get("workspace_id"), "Reference audit workspace ID")
    return _AuditSnapshots(
        audit=_AuditSnapshot(
            audit_id=audit_id,
            workspace_id=_text(
                audit.get("workspace_id"), "Reference audit workspace ID"
            ),
            workspace_sha256=_sha256(
                audit.get("workspace_sha256"), "Reference audit workspace_sha256"
            ),
            queue_sha256=_sha256(
                audit.get("queue_sha256"), "Reference audit queue_sha256"
            ),
            state_sha256=_sha256(
                audit.get("state_sha256"), "Reference audit state_sha256"
            ),
            voice_manifest_sha256=_sha256(
                audit.get("voice_manifest_sha256"),
                "Reference audit voice_manifest_sha256",
            ),
            groups=_audit_groups(groups),
        ),
        key=_KeySnapshot(groups=_private_groups(private_groups)),
        decisions=_DecisionSnapshot(
            decision_set_id=decision_set_id,
            decisions=_decisions(decision_values),
        ),
        payloads=payloads,
        audit_sha256=hashlib.sha256(payloads["audit"]).hexdigest(),
        key_sha256=hashlib.sha256(payloads["key"]).hexdigest(),
        decisions_sha256=hashlib.sha256(payloads["decisions"]).hexdigest(),
    )


def _assert_audit_snapshots_unchanged(
    directory: Path, snapshots: _AuditSnapshots
) -> None:
    for name, filename in (
        ("audit", "audit.json"),
        ("key", ".blind-key.json"),
        ("decisions", "decisions.json"),
    ):
        path = directory / filename
        if not path.is_file() or path.read_bytes() != snapshots["payloads"][name]:
            raise FailureReferenceBindingError(
                f"Reference audit {filename} changed during binding publication"
            )
