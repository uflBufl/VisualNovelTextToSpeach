"""Wire records for exact selected failure-reference projections."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.source_reference_bindings import queue_voice_overrides_sha256
from vntts.document_identity import canonical_document_sha256
from vntts.path_safety import contained_regular_file

FAILURE_REFERENCE_BINDING_SCHEMA = "vntts.authoring-failure-reference-binding"
FAILURE_REFERENCE_BINDING_VERSION = 2
_LEGACY_FAILURE_REFERENCE_BINDING_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")


class FailureReferenceBindingError(RuntimeError):
    """A selected-reference overlay is incomplete, unsafe or inconsistent."""


@dataclass(frozen=True)
class FailureReferenceBinding:
    directory: Path
    binding_id: str
    audit_id: str
    decision_set_id: str
    group_count: int
    case_count: int
    created: bool = False

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "binding": str(self.directory / "binding.json"),
            "binding_id": self.binding_id,
            "audit_id": self.audit_id,
            "decision_set_id": self.decision_set_id,
            "group_count": self.group_count,
            "case_count": self.case_count,
            "created": self.created,
        }


# Keep public exception/result pickle and introspection identity at the original API.
FailureReferenceBindingError.__module__ = "vntts.authoring.failure_reference_binding"
FailureReferenceBinding.__module__ = "vntts.authoring.failure_reference_binding"


def load_failure_reference_binding(directory):
    """Validate one self-contained selected-reference overlay."""
    argument = Path(directory).expanduser()
    if argument.is_symlink():
        raise FailureReferenceBindingError(
            "Reference binding directory must not be a symlink"
        )
    directory = argument.resolve()
    path = directory / "binding.json"
    if path.is_symlink():
        raise FailureReferenceBindingError("Reference binding must not be a symlink")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FailureReferenceBindingError(str(error)) from error
    schema_version = document.get("schema_version")
    if document.get(
        "schema"
    ) != FAILURE_REFERENCE_BINDING_SCHEMA or schema_version not in {
        _LEGACY_FAILURE_REFERENCE_BINDING_VERSION,
        FAILURE_REFERENCE_BINDING_VERSION,
    }:
        raise FailureReferenceBindingError("Unsupported reference binding schema")
    identity = {
        key: value
        for key, value in document.items()
        if key not in {"binding_id", "published_at"}
    }
    binding_id = _sha256(document.get("binding_id"), "Reference binding ID")
    if binding_id != canonical_document_sha256(identity):
        raise FailureReferenceBindingError("Reference binding identity changed")
    try:
        published_at = datetime.fromisoformat(document["published_at"])
    except (KeyError, TypeError, ValueError) as error:
        raise FailureReferenceBindingError(
            "Reference binding publication timestamp is malformed"
        ) from error
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise FailureReferenceBindingError(
            "Reference binding publication timestamp requires a timezone"
        )
    audit_id = _sha256(document.get("audit_id"), "Reference audit ID")
    decision_set_id = _sha256(
        document.get("decision_set_id"), "Reference decision-set ID"
    )
    authority = document.get("source_authority")
    if not isinstance(authority, dict) or set(authority) != {
        "workspace_id",
        "workspace_sha256",
        "queue_sha256",
        "state_sha256",
        "voice_manifest_sha256",
        "audit_sha256",
        "blind_key_sha256",
        "decisions_sha256",
    }:
        raise FailureReferenceBindingError("Reference binding authority is malformed")
    _text(authority["workspace_id"], "Reference binding workspace ID")
    for field in set(authority) - {"workspace_id"}:
        _sha256(authority[field], f"Reference binding {field}")
    groups = document.get("groups")
    overrides = document.get("queue_voice_overrides")
    if not isinstance(groups, list) or not groups or not isinstance(overrides, dict):
        raise FailureReferenceBindingError("Reference binding inventory is malformed")
    seen_groups = set()
    seen_queue_ids = set()
    expected_overrides = {}
    voices = set()
    for group in groups:
        required_group_fields = {
            "group_id",
            "synthesis_voice_character",
            "control_character",
            "speaker",
            "candidate_id",
            "voice_character",
            "reference",
            "reference_sha256",
            "source_reference",
            "cases",
        }
        accepted_group_shapes = {frozenset(required_group_fields)}
        if schema_version == FAILURE_REFERENCE_BINDING_VERSION:
            accepted_group_shapes.add(
                frozenset({*required_group_fields, "selection_authority"})
            )
        if not isinstance(group, dict) or frozenset(group) not in accepted_group_shapes:
            raise FailureReferenceBindingError("Reference binding group is malformed")
        group_id = _sha256(group["group_id"], "Reference binding group ID")
        if group_id in seen_groups:
            raise FailureReferenceBindingError("Reference binding group is duplicated")
        seen_groups.add(group_id)
        voice = _text(group["voice_character"], "Reference binding voice")
        if voice in voices:
            raise FailureReferenceBindingError("Reference binding voice is duplicated")
        voices.add(voice)
        _text(group["synthesis_voice_character"], "Audited synthesis voice")
        _text(group["control_character"], "Audited control character")
        _text(group["speaker"], "Audited speaker")
        _text(group["candidate_id"], "Reference binding candidate")
        _safe_relative(group["source_reference"], "Audited source reference")
        relative = _safe_relative(group["reference"], "Selected reference")
        reference = _contained_regular_file(directory, relative, "selected reference")
        digest = _sha256(group["reference_sha256"], "Selected reference SHA-256")
        if sha256_file(reference) != digest:
            raise FailureReferenceBindingError("Selected reference changed")
        if "selection_authority" in group:
            _validate_selection_authority(
                group["selection_authority"],
                selected_reference_sha256=digest,
            )
        cases = group["cases"]
        if not isinstance(cases, list) or not cases:
            raise FailureReferenceBindingError("Reference binding cases are malformed")
        for case in cases:
            if not isinstance(case, dict) or set(case) != {
                "queue_id",
                "failure_sha256",
            }:
                raise FailureReferenceBindingError(
                    "Reference binding case is malformed"
                )
            queue_id = _text(case["queue_id"], "Reference binding queue ID")
            _sha256(case["failure_sha256"], "Reference failure SHA-256")
            if queue_id in seen_queue_ids:
                raise FailureReferenceBindingError(
                    "Reference binding queue ID is duplicated"
                )
            seen_queue_ids.add(queue_id)
            expected_overrides[queue_id] = voice
    if overrides != dict(sorted(expected_overrides.items())):
        raise FailureReferenceBindingError(
            "Reference binding queue override inventory changed"
        )
    if document.get("queue_voice_overrides_sha256") != queue_voice_overrides_sha256(
        overrides
    ):
        raise FailureReferenceBindingError(
            "Reference binding queue override checksum changed"
        )
    return FailureReferenceBinding(
        directory,
        binding_id,
        audit_id,
        decision_set_id,
        len(groups),
        len(seen_queue_ids),
        False,
    )


def _validate_selection_authority(value, *, selected_reference_sha256):
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
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("selected_reference_sha256") != selected_reference_sha256
    ):
        raise FailureReferenceBindingError(
            "Reference binding selection authority is malformed"
        )
    schema = value.get("schema")
    if schema == "vntts.authoring-reference-render-selection":
        if set(value) != blind_required or value.get("selected_side") not in {"a", "b"}:
            raise FailureReferenceBindingError(
                "Reference binding selection authority is malformed"
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
        excluded_text_fields = {"schema_version", "selected_side"}
    elif schema == "vntts.authoring-render-hypothesis-selection":
        if set(value) != hypothesis_required:
            raise FailureReferenceBindingError(
                "Reference binding selection authority is malformed"
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
        excluded_text_fields = {"schema_version"}
    else:
        raise FailureReferenceBindingError(
            "Reference binding selection authority is malformed"
        )
    for field in hash_fields:
        _sha256(value[field], f"Reference selection {field}")
    required = (
        blind_required
        if schema.endswith("reference-render-selection")
        else hypothesis_required
    )
    for field in required - hash_fields - excluded_text_fields:
        _text(value[field], f"Reference selection {field}")


def load_failure_reference_binding_document(directory):
    """Return a validated binding document for runtime control construction."""
    argument = Path(directory).expanduser()
    if argument.is_symlink():
        raise FailureReferenceBindingError(
            "Reference binding directory must not be a symlink"
        )
    path = argument.resolve() / "binding.json"
    try:
        payload = path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FailureReferenceBindingError(str(error)) from error
    binding = load_failure_reference_binding(argument)
    if (
        document.get("binding_id") != binding.binding_id
        or not path.is_file()
        or path.read_bytes() != payload
    ):
        raise FailureReferenceBindingError(
            "Reference binding changed while it was loaded"
        )
    return document


def _contained_regular_file(directory, relative, label):
    return contained_regular_file(
        directory, relative, label, error_type=FailureReferenceBindingError
    )


def _safe_relative(value, label):
    if isinstance(value, PurePosixPath):
        value = value.as_posix()
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise FailureReferenceBindingError(f"{label} must be a safe relative path")
    relative = PurePosixPath(value.strip())
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise FailureReferenceBindingError(f"{label} must be a safe relative path")
    return relative


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise FailureReferenceBindingError(f"{label} must be non-empty text")
    return value


def _sha256(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise FailureReferenceBindingError(f"{label} must be a lowercase SHA-256")
    return value


__all__ = [
    "FAILURE_REFERENCE_BINDING_SCHEMA",
    "FAILURE_REFERENCE_BINDING_VERSION",
    "FailureReferenceBinding",
    "FailureReferenceBindingError",
    "load_failure_reference_binding",
    "load_failure_reference_binding_document",
]
