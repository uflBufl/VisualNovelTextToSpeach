"""Validate explicit zero-choice authority for a non-MOSS fallback attempt."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file

from vntts.document_identity import canonical_document_sha256, is_lowercase_sha256

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
class _AuthorityDefinition:
    """Schema-specific fields needed to validate one authority artifact."""

    kind: str
    authority_id: str
    decisions: object
    source_hashes: object
    expected_outcome: str


@dataclass(frozen=True)
class OfflineFallbackAuthority:
    source: Path
    payload: bytes
    source_sha256: str
    kind: str
    authority_id: str
    queue_ids: tuple[str, ...]
    source_item_sha256s: dict[str, str]

    def snapshot_record(self, path: str | Path) -> dict[str, object]:
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

    def reference_record(self, queue_id: str) -> dict[str, object]:
        return {
            "schema": OFFLINE_FALLBACK_AUTHORITY_REFERENCE_SCHEMA,
            "schema_version": OFFLINE_FALLBACK_AUTHORITY_REFERENCE_VERSION,
            "kind": self.kind,
            "authority_id": self.authority_id,
            "source_sha256": self.source_sha256,
            "queue_id": queue_id,
            "source_item_sha256": self.source_item_sha256s[queue_id],
        }


def load_offline_fallback_authorities(
    paths: Iterable[str | Path] | None,
    source_items: Mapping[str, object],
    selected_queue_ids: Iterable[object],
) -> tuple[OfflineFallbackAuthority, ...]:
    """Load exact automatic-unresolved artifacts for every selected source item."""
    selected = _selected_queue_ids(selected_queue_ids)
    if not selected:
        return _empty_selection_authorities(paths)
    if not paths:
        return ()
    loaded = tuple(_load_authority(path) for path in paths)
    by_queue_id = _authorities_by_queue_id(loaded)
    if set(by_queue_id) != selected:
        raise OfflineFallbackAuthorityError(
            "Offline fallback authorities must cover every selected queue ID exactly"
        )
    _validate_failed_source_items(by_queue_id, source_items)
    return _sorted_authorities(loaded)


def validate_offline_fallback_authority_records(
    records: object,
    directory: str | Path,
    source_items: Mapping[str, object],
) -> tuple[OfflineFallbackAuthority, ...]:
    """Revalidate copied authority snapshots bound into a workspace ledger."""
    expected_by_id = _load_snapshot_authorities(records, directory)
    by_queue_id = _snapshot_authorities_by_queue_id(expected_by_id.values())
    _validate_snapshot_source_items(by_queue_id, source_items)
    return _sorted_authorities(expected_by_id.values())


def _load_authority(path: str | Path) -> OfflineFallbackAuthority:
    source = _authority_source(path)
    payload, document = _authority_document(source)
    definition = _authority_definition(document)
    source_item_sha256s = _authority_source_item_sha256s(definition)
    _verify_authority_source(source, payload)
    return OfflineFallbackAuthority(
        source=source,
        payload=payload,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        kind=definition.kind,
        authority_id=definition.authority_id,
        queue_ids=tuple(source_item_sha256s),
        source_item_sha256s=source_item_sha256s,
    )


def _selected_queue_ids(selected_queue_ids: Iterable[object]) -> set[str]:
    return {
        _required_text(value, "Offline fallback queue ID")
        for value in selected_queue_ids
    }


def _empty_selection_authorities(
    paths: Iterable[str | Path] | None,
) -> tuple[OfflineFallbackAuthority, ...]:
    if paths:
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority requires selected queue IDs"
        )
    return ()


def _authorities_by_queue_id(
    authorities: Iterable[OfflineFallbackAuthority],
) -> dict[str, OfflineFallbackAuthority]:
    by_queue_id: dict[str, OfflineFallbackAuthority] = {}
    authority_ids: set[str] = set()
    for authority in authorities:
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
    return by_queue_id


def _validate_failed_source_items(
    authorities_by_queue_id: Mapping[str, OfflineFallbackAuthority],
    source_items: Mapping[str, object],
) -> None:
    for queue_id, authority in authorities_by_queue_id.items():
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


def _sorted_authorities(
    authorities: Iterable[OfflineFallbackAuthority],
) -> tuple[OfflineFallbackAuthority, ...]:
    return tuple(sorted(authorities, key=lambda value: value.authority_id))


def _load_snapshot_authorities(
    records: object,
    directory: str | Path,
) -> dict[str, OfflineFallbackAuthority]:
    if not isinstance(records, list) or not records:
        raise OfflineFallbackAuthorityError(
            "Workspace offline fallback authority ledger is missing"
        )
    expected_by_id: dict[str, OfflineFallbackAuthority] = {}
    for record in records:
        relative, authority = _snapshot_authority(record, directory)
        if authority.authority_id in expected_by_id:
            raise OfflineFallbackAuthorityError(
                "Workspace offline fallback authority is duplicated"
            )
        if authority.snapshot_record(relative.as_posix()) != record:
            raise OfflineFallbackAuthorityError(
                "Workspace offline fallback authority snapshot changed"
            )
        expected_by_id[authority.authority_id] = authority
    return expected_by_id


def _snapshot_authority(
    record: object,
    directory: str | Path,
) -> tuple[Path, OfflineFallbackAuthority]:
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
    return relative, _snapshot_authority_at_path(relative, directory)


def _snapshot_authority_at_path(
    relative: Path,
    directory: str | Path,
) -> OfflineFallbackAuthority:
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
    return _load_authority(path)


def _snapshot_authorities_by_queue_id(
    authorities: Iterable[OfflineFallbackAuthority],
) -> dict[str, OfflineFallbackAuthority]:
    by_queue_id: dict[str, OfflineFallbackAuthority] = {}
    for authority in authorities:
        for queue_id in authority.queue_ids:
            if queue_id in by_queue_id:
                raise OfflineFallbackAuthorityError(
                    "Workspace offline fallback authority queue IDs overlap"
                )
            by_queue_id[queue_id] = authority
    return by_queue_id


def _validate_snapshot_source_items(
    authorities_by_queue_id: Mapping[str, OfflineFallbackAuthority],
    source_items: Mapping[str, object],
) -> None:
    for queue_id, authority in authorities_by_queue_id.items():
        source_item = source_items.get(queue_id)
        if _source_item_sha256(source_item) != authority.source_item_sha256s[queue_id]:
            raise OfflineFallbackAuthorityError(
                f"Workspace offline fallback authority is stale for {queue_id!r}"
            )


def _source_item_sha256(source_item: object) -> str | None:
    if isinstance(source_item, str) and is_lowercase_sha256(source_item):
        return source_item
    if isinstance(source_item, dict):
        return canonical_document_sha256(source_item)
    return None


def _authority_source(path: str | Path) -> Path:
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
    return source


def _authority_document(source: Path) -> tuple[bytes, dict[str, object]]:
    try:
        payload = source.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OfflineFallbackAuthorityError(str(error)) from error
    if not isinstance(document, dict):
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority must be a JSON object"
        )
    return payload, document


def _authority_definition(document: dict[str, object]) -> _AuthorityDefinition:
    schema = document.get("schema")
    if schema == FAILED_VOICE_DECISION_SCHEMA:
        return _failed_voice_authority_definition(document)
    if schema == FAILED_PROMPT_SELECTION_SCHEMA:
        return _failed_prompt_authority_definition(document)
    raise OfflineFallbackAuthorityError(
        "Offline fallback authority schema is unsupported"
    )


def _failed_voice_authority_definition(
    document: dict[str, object],
) -> _AuthorityDefinition:
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
    return _AuthorityDefinition(
        kind="failed_voice_review",
        authority_id=authority_id,
        decisions=binding.get("decisions"),
        source_hashes=binding.get("source_failed_state_item_sha256s"),
        expected_outcome="neither",
    )


def _failed_prompt_authority_definition(
    document: dict[str, object],
) -> _AuthorityDefinition:
    authority_id = _canonical_id(document, "selection_id")
    if document.get("schema_version") != 1:
        raise OfflineFallbackAuthorityError(
            "Failed-prompt fallback authority schema is unsupported"
        )
    return _AuthorityDefinition(
        kind="failed_prompt_review",
        authority_id=authority_id,
        decisions=document.get("decisions"),
        source_hashes=None,
        expected_outcome="keep_unresolved",
    )


def _authority_source_item_sha256s(
    definition: _AuthorityDefinition,
) -> dict[str, str]:
    if not isinstance(definition.decisions, list) or not definition.decisions:
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority decisions are empty"
        )
    queue_ids: list[str] = []
    decision_hashes: dict[str, object] = {}
    for decision in definition.decisions:
        decision_document, current_ids = _authority_decision(
            decision, definition.expected_outcome
        )
        if definition.kind == "failed_prompt_review":
            decision_hashes.update(
                _prompt_decision_hashes(decision_document, current_ids)
            )
        queue_ids.extend(current_ids)
    if queue_ids != sorted(set(queue_ids)):
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority queue IDs overlap or are not canonical"
        )
    source_hashes = (
        definition.source_hashes
        if definition.kind == "failed_voice_review"
        else decision_hashes
    )
    return _complete_source_hashes(source_hashes, queue_ids)


def _authority_decision(
    decision: object,
    expected_outcome: str,
) -> tuple[dict[str, object], list[str]]:
    if (
        not isinstance(decision, dict)
        or decision.get("decision") != expected_outcome
        or decision.get("review_decision_origin") != AUTOMATIC_UNRESOLVED_ORIGIN
    ):
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority is not automatically unresolved"
        )
    return decision, _canonical_queue_ids(decision.get("queue_ids"))


def _canonical_queue_ids(queue_ids: object) -> list[str]:
    if (
        not isinstance(queue_ids, list)
        or not queue_ids
        or not all(isinstance(queue_id, str) for queue_id in queue_ids)
        or queue_ids != sorted(set(queue_ids))
    ):
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority queue IDs are not canonical"
        )
    return queue_ids


def _prompt_decision_hashes(
    decision: dict[str, object],
    queue_ids: list[str],
) -> dict[str, object]:
    source_hashes = decision.get("source_state_item_sha256s")
    if not isinstance(source_hashes, dict) or set(source_hashes) != set(queue_ids):
        raise OfflineFallbackAuthorityError(
            "Failed-prompt fallback authority source hashes are incomplete"
        )
    return source_hashes


def _complete_source_hashes(
    source_hashes: object,
    queue_ids: list[str],
) -> dict[str, str]:
    if (
        not isinstance(source_hashes, dict)
        or set(source_hashes) != set(queue_ids)
        or any(not is_lowercase_sha256(value) for value in source_hashes.values())
    ):
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority source hashes are incomplete"
        )
    return {
        queue_id: source_hashes[queue_id]
        for queue_id in sorted(source_hashes)
        if is_lowercase_sha256(source_hashes[queue_id])
    }


def _verify_authority_source(source: Path, payload: bytes) -> None:
    if sha256_file(source) != hashlib.sha256(payload).hexdigest():
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority changed while it was loaded"
        )


def _canonical_id(document: dict[str, object], field: str) -> str:
    claimed = document.get(field)
    if (
        not isinstance(claimed, str)
        or not is_lowercase_sha256(claimed)
        or claimed
        != canonical_document_sha256(
            {key: value for key, value in document.items() if key != field}
        )
    ):
        raise OfflineFallbackAuthorityError(
            "Offline fallback authority identity changed"
        )
    return claimed


def _required_text(value: object, label: str) -> str:
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
