"""Validate explicit zero-choice authority for a non-MOSS fallback attempt."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file

from vntts.document_identity import canonical_document_sha256

FAILED_VOICE_DECISION_SCHEMA = "vntts.authoring-missing-voice-reuse-decision"
FAILED_PROMPT_SELECTION_SCHEMA = "vntts.authoring-failed-prompt-selection"
AUTOMATIC_UNRESOLVED_ORIGIN = "automatic_no_complete_candidate"
OFFLINE_FALLBACK_AUTHORITY_SCHEMA = "vntts.authoring-offline-fallback-authority"
OFFLINE_FALLBACK_AUTHORITY_VERSION = 1
OFFLINE_FALLBACK_AUTHORITY_REFERENCE_SCHEMA = (
    "vntts.authoring-offline-fallback-authority-reference"
)
OFFLINE_FALLBACK_AUTHORITY_REFERENCE_VERSION = 1


class OfflineFallbackAuthorityError(ValueError):
    """An unresolved review artifact cannot authorize an offline fallback."""


@dataclass(frozen=True)
class OfflineFallbackAuthority:
    source: Path
    payload: bytes
    source_sha256: str
    kind: str
    authority_id: str
    queue_ids: tuple[str, ...]
    source_item_sha256s: dict[str, str]

    def snapshot_record(self, path):
        return {
            "schema": OFFLINE_FALLBACK_AUTHORITY_SCHEMA,
            "schema_version": OFFLINE_FALLBACK_AUTHORITY_VERSION,
            "kind": self.kind,
            "authority_id": self.authority_id,
            "source_sha256": self.source_sha256,
            "path": str(path),
            "queue_ids": list(self.queue_ids),
            "source_item_sha256s": dict(self.source_item_sha256s),
        }

    def reference_record(self, queue_id):
        return {
            "schema": OFFLINE_FALLBACK_AUTHORITY_REFERENCE_SCHEMA,
            "schema_version": OFFLINE_FALLBACK_AUTHORITY_REFERENCE_VERSION,
            "kind": self.kind,
            "authority_id": self.authority_id,
            "source_sha256": self.source_sha256,
            "queue_id": queue_id,
            "source_item_sha256": self.source_item_sha256s[queue_id],
        }


def load_offline_fallback_authorities(paths, source_items, selected_queue_ids):
    """Load exact automatic-unresolved artifacts for every selected source item."""
    selected = {
        _required_text(value, "Offline fallback queue ID")
        for value in selected_queue_ids
    }
    if not selected:
        if paths:
            raise OfflineFallbackAuthorityError(
                "Offline fallback authority requires selected queue IDs"
            )
        return ()
    if not paths:
        return ()
    loaded = tuple(_load_authority(path) for path in paths or ())
    by_queue_id = {}
    authority_ids = set()
    for authority in loaded:
        if authority.authority_id in authority_ids:
            raise OfflineFallbackAuthorityError(
                "Offline fallback authority is duplicated"
            )
        authority_ids.add(authority.authority_id)
        for queue_id in authority.queue_ids:
            if queue_id in by_queue_id:
                raise OfflineFallbackAuthorityError(
                    f"Offline fallback queue has multiple authorities: {queue_id!r}"
                )
            by_queue_id[queue_id] = authority
    if set(by_queue_id) != selected:
        raise OfflineFallbackAuthorityError(
            "Offline fallback authorities must cover every selected queue ID exactly"
        )
    for queue_id, authority in by_queue_id.items():
        source_item = source_items.get(queue_id)
        if not isinstance(source_item, dict) or source_item.get("status") != "failed":
            raise OfflineFallbackAuthorityError(
                f"Offline fallback authority source is not failed: {queue_id!r}"
            )
        if (
            canonical_document_sha256(source_item)
            != authority.source_item_sha256s[queue_id]
        ):
            raise OfflineFallbackAuthorityError(
                f"Offline fallback authority is stale for {queue_id!r}"
            )
    return tuple(sorted(loaded, key=lambda value: value.authority_id))


def validate_offline_fallback_authority_records(records, directory, source_items):
    """Revalidate copied authority snapshots bound into a workspace ledger."""
    if not isinstance(records, list) or not records:
        raise OfflineFallbackAuthorityError(
            "Workspace offline fallback authority ledger is missing"
        )
    expected_by_id = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "schema",
            "schema_version",
            "kind",
            "authority_id",
            "source_sha256",
            "path",
            "queue_ids",
            "source_item_sha256s",
        }:
            raise OfflineFallbackAuthorityError(
                "Workspace offline fallback authority ledger is malformed"
            )
        if (
            record.get("schema") != OFFLINE_FALLBACK_AUTHORITY_SCHEMA
            or record.get("schema_version") != OFFLINE_FALLBACK_AUTHORITY_VERSION
        ):
            raise OfflineFallbackAuthorityError(
                "Workspace offline fallback authority schema is unsupported"
            )
        relative = Path(_required_text(record.get("path"), "Authority snapshot path"))
        if relative.is_absolute() or ".." in relative.parts:
            raise OfflineFallbackAuthorityError(
                "Workspace offline fallback authority path is unsafe"
            )
        candidate = Path(directory) / relative
        if candidate.is_symlink():
            raise OfflineFallbackAuthorityError(
                "Workspace offline fallback authority path is unsafe"
            )
        path = candidate.resolve()
        try:
            path.relative_to(Path(directory).resolve())
        except ValueError as error:
            raise OfflineFallbackAuthorityError(
                "Workspace offline fallback authority leaves its root"
            ) from error
        authority = _load_authority(path)
        if authority.authority_id in expected_by_id:
            raise OfflineFallbackAuthorityError(
                "Workspace offline fallback authority is duplicated"
            )
        expected = authority.snapshot_record(relative.as_posix())
        if expected != record:
            raise OfflineFallbackAuthorityError(
                "Workspace offline fallback authority snapshot changed"
            )
        expected_by_id[authority.authority_id] = authority
    by_queue_id = {}
    for authority in expected_by_id.values():
        for queue_id in authority.queue_ids:
            if queue_id in by_queue_id:
                raise OfflineFallbackAuthorityError(
                    "Workspace offline fallback authority queue IDs overlap"
                )
            by_queue_id[queue_id] = authority
    for queue_id, authority in by_queue_id.items():
        source_item = source_items.get(queue_id)
        source_item_sha256 = (
            source_item
            if _is_sha256(source_item)
            else canonical_document_sha256(source_item)
            if isinstance(source_item, dict)
            else None
        )
        if source_item_sha256 != authority.source_item_sha256s[queue_id]:
            raise OfflineFallbackAuthorityError(
                f"Workspace offline fallback authority is stale for {queue_id!r}"
            )
    loaded = tuple(
        sorted(expected_by_id.values(), key=lambda value: value.authority_id)
    )
    if [value.authority_id for value in loaded] != sorted(expected_by_id):
        raise OfflineFallbackAuthorityError(
            "Workspace offline fallback authority ledger is not canonical"
        )
    return loaded


def _load_authority(path):
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise OfflineFallbackAuthorityError(
            f"Offline fallback authority is missing or unsafe: {candidate}"
        )
    source = candidate.resolve()
    if not source.is_file():
        raise OfflineFallbackAuthorityError(
            f"Offline fallback authority is missing or unsafe: {source}"
        )
    try:
        payload = source.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OfflineFallbackAuthorityError(str(error)) from error
    if not isinstance(document, dict):
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority must be a JSON object"
        )
    schema = document.get("schema")
    if schema == FAILED_VOICE_DECISION_SCHEMA:
        kind = "failed_voice_review"
        authority_id = _canonical_id(document, "decision_id")
        binding = document.get("binding")
        if (
            document.get("schema_version") != 1
            or not isinstance(binding, dict)
            or binding.get("target_mode") != "failed"
            or binding.get("queue_voice_overrides") != {}
            or binding.get("selected_candidates") != []
        ):
            raise OfflineFallbackAuthorityError(
                "Failed-voice fallback authority is not a zero-override decision"
            )
        decisions = binding.get("decisions")
        source_hashes = binding.get("source_failed_state_item_sha256s")
        expected_outcome = "neither"
    elif schema == FAILED_PROMPT_SELECTION_SCHEMA:
        kind = "failed_prompt_review"
        authority_id = _canonical_id(document, "selection_id")
        if document.get("schema_version") != 1:
            raise OfflineFallbackAuthorityError(
                "Failed-prompt fallback authority schema is unsupported"
            )
        decisions = document.get("decisions")
        source_hashes = None
        expected_outcome = "keep_unresolved"
    else:
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority schema is unsupported"
        )
    if not isinstance(decisions, list) or not decisions:
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority decisions are empty"
        )
    queue_ids = []
    decision_hashes = {}
    for decision in decisions:
        if (
            not isinstance(decision, dict)
            or decision.get("decision") != expected_outcome
            or decision.get("review_decision_origin") != AUTOMATIC_UNRESOLVED_ORIGIN
        ):
            raise OfflineFallbackAuthorityError(
                "Offline fallback authority is not automatically unresolved"
            )
        current_ids = decision.get("queue_ids")
        current_hashes = decision.get("source_state_item_sha256s")
        if (
            not isinstance(current_ids, list)
            or not current_ids
            or current_ids != sorted(set(current_ids))
        ):
            raise OfflineFallbackAuthorityError(
                "Offline fallback authority queue IDs are not canonical"
            )
        if kind == "failed_prompt_review":
            if not isinstance(current_hashes, dict) or set(current_hashes) != set(
                current_ids
            ):
                raise OfflineFallbackAuthorityError(
                    "Failed-prompt fallback authority source hashes are incomplete"
                )
            decision_hashes.update(current_hashes)
        queue_ids.extend(current_ids)
    if queue_ids != sorted(set(queue_ids)):
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority queue IDs overlap or are not canonical"
        )
    if kind == "failed_voice_review":
        decision_hashes = source_hashes
    if (
        not isinstance(decision_hashes, dict)
        or set(decision_hashes) != set(queue_ids)
        or any(not _is_sha256(value) for value in decision_hashes.values())
    ):
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority source hashes are incomplete"
        )
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    if sha256_file(source) != payload_sha256:
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority changed while it was loaded"
        )
    return OfflineFallbackAuthority(
        source=source,
        payload=payload,
        source_sha256=payload_sha256,
        kind=kind,
        authority_id=authority_id,
        queue_ids=tuple(queue_ids),
        source_item_sha256s=dict(sorted(decision_hashes.items())),
    )


def _canonical_id(document, field):
    claimed = document.get(field)
    if not _is_sha256(claimed) or claimed != canonical_document_sha256(
        {key: value for key, value in document.items() if key != field}
    ):
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority identity changed"
        )
    return claimed


def _is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise OfflineFallbackAuthorityError(f"{label} must be non-empty text")
    return value.strip()


__all__ = [
    "AUTOMATIC_UNRESOLVED_ORIGIN",
    "OFFLINE_FALLBACK_AUTHORITY_SCHEMA",
    "OFFLINE_FALLBACK_AUTHORITY_VERSION",
    "OFFLINE_FALLBACK_AUTHORITY_REFERENCE_SCHEMA",
    "OFFLINE_FALLBACK_AUTHORITY_REFERENCE_VERSION",
    "OfflineFallbackAuthority",
    "OfflineFallbackAuthorityError",
    "load_offline_fallback_authorities",
    "validate_offline_fallback_authority_records",
]
