"""Compose one strict additive successor of two generation queues."""

from __future__ import annotations

from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
    write_voice_generation_queue,
)

from vntts.authoring.authority import canonical_document_sha256

SCHEMA = "vntts.authoring-generation-queue-extension"
SCHEMA_VERSION = 1
FIELD = "vntts.authoring.queue_extension"
WORKSPACE_SCHEMA = "vntts.authoring-workspace-queue-extension"
WORKSPACE_VERSION = 1


class QueueExtensionError(ValueError):
    """Raised when an additive queue successor is unsafe or inconsistent."""


def publish_additive_generation_queue(base_queue, extension_queue, output):
    """Publish a strict queue superset while preserving exact item documents."""
    base_path = Path(base_queue).expanduser().resolve()
    extension_path = Path(extension_queue).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if base_path == extension_path or output_path in {base_path, extension_path}:
        raise QueueExtensionError("Queue extension requires three distinct paths")
    try:
        base = VoiceGenerationQueue.load(base_path)
        extension = VoiceGenerationQueue.load(extension_path)
    except VoiceGenerationQueueError as error:
        raise QueueExtensionError(str(error)) from error
    for field in ("game", "language"):
        if base.metadata.get(field) != extension.metadata.get(field):
            raise QueueExtensionError(f"Queue extension {field} differs from its base")
    base_by_id = {item.queue_id: item.document for item in base.items}
    extension_by_id = {item.queue_id: item.document for item in extension.items}
    overlap = sorted(set(base_by_id) & set(extension_by_id))
    if overlap:
        raise QueueExtensionError(
            "Queue extension collides with base queue IDs: " + ", ".join(overlap)
        )
    if not extension_by_id:
        raise QueueExtensionError("Queue extension adds no generation items")

    base_sha256 = sha256_file(base_path)
    extension_sha256 = sha256_file(extension_path)
    added = [
        {
            "queue_id": queue_id,
            "item_sha256": canonical_document_sha256(document),
        }
        for queue_id, document in sorted(extension_by_id.items())
    ]
    ledger = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "base_queue_sha256": base_sha256,
        "extension_queue_sha256": extension_sha256,
        "base_item_count": len(base_by_id),
        "added_item_count": len(added),
        "added_items": added,
    }
    ledger["extension_id"] = canonical_document_sha256(ledger)
    metadata = dict(extension.metadata)
    for derived_field in (
        "action_counts",
        "character_count",
        "filters",
        "item_count",
        "partial_source_audio_count",
        "source_audio_status_counts",
        "source_kind_counts",
    ):
        metadata.pop(derived_field, None)
    metadata["source_generation_queue_sha256"] = base_sha256
    metadata[FIELD] = ledger

    ordered = sorted(
        (*base_by_id.values(), *extension_by_id.values()),
        key=_story_order_key,
    )
    try:
        published = write_voice_generation_queue(output_path, metadata, ordered)
        result = VoiceGenerationQueue.load(published)
    except VoiceGenerationQueueError as error:
        raise QueueExtensionError(str(error)) from error
    observed = {item.queue_id: item.document for item in result.items}
    if observed != {**base_by_id, **extension_by_id}:
        raise QueueExtensionError("Published queue extension changed item documents")
    return published


def validate_additive_generation_queue(queue_path, *, base_queue=None):
    """Validate the embedded extension ledger and optional exact base queue."""
    path = Path(queue_path).expanduser().resolve()
    try:
        queue = VoiceGenerationQueue.load(path)
    except VoiceGenerationQueueError as error:
        raise QueueExtensionError(str(error)) from error
    ledger = queue.metadata.get(FIELD)
    required = {
        "schema",
        "schema_version",
        "base_queue_sha256",
        "extension_queue_sha256",
        "base_item_count",
        "added_item_count",
        "added_items",
        "extension_id",
    }
    if (
        not isinstance(ledger, dict)
        or set(ledger) != required
        or ledger.get("schema") != SCHEMA
        or ledger.get("schema_version") != SCHEMA_VERSION
    ):
        raise QueueExtensionError("Generation queue extension ledger is malformed")
    body = {key: value for key, value in ledger.items() if key != "extension_id"}
    if ledger.get("extension_id") != canonical_document_sha256(body):
        raise QueueExtensionError("Generation queue extension identity changed")
    base_sha256 = _sha256(ledger.get("base_queue_sha256"), "Base queue SHA-256")
    _sha256(ledger.get("extension_queue_sha256"), "Extension queue SHA-256")
    if queue.metadata.get("source_generation_queue_sha256") != base_sha256:
        raise QueueExtensionError("Generation queue base provenance changed")
    added = ledger.get("added_items")
    if not isinstance(added, list) or not added:
        raise QueueExtensionError("Generation queue extension adds no items")
    added_by_id = {}
    for record in added:
        if not isinstance(record, dict) or set(record) != {"queue_id", "item_sha256"}:
            raise QueueExtensionError("Generation queue added-item ledger is malformed")
        queue_id = record.get("queue_id")
        if not isinstance(queue_id, str) or not queue_id or queue_id in added_by_id:
            raise QueueExtensionError("Generation queue added IDs are invalid")
        added_by_id[queue_id] = _sha256(
            record.get("item_sha256"), "Added queue item SHA-256"
        )
    observed = {item.queue_id: item.document for item in queue.items}
    if set(added_by_id) - set(observed):
        raise QueueExtensionError("Generation queue added item is absent")
    for queue_id, digest in added_by_id.items():
        if canonical_document_sha256(observed[queue_id]) != digest:
            raise QueueExtensionError("Generation queue added item changed")
    base_ids = set(observed) - set(added_by_id)
    if ledger.get("added_item_count") != len(added_by_id) or ledger.get(
        "base_item_count"
    ) != len(base_ids):
        raise QueueExtensionError("Generation queue extension counts changed")
    if base_queue is not None:
        base_path = Path(base_queue).expanduser().resolve()
        if sha256_file(base_path) != base_sha256:
            raise QueueExtensionError("Generation queue extension base changed")
        try:
            base = VoiceGenerationQueue.load(base_path)
        except VoiceGenerationQueueError as error:
            raise QueueExtensionError(str(error)) from error
        expected = {item.queue_id: item.document for item in base.items}
        if expected != {queue_id: observed[queue_id] for queue_id in base_ids}:
            raise QueueExtensionError("Generation queue changed or removed base items")
    return queue, ledger


def workspace_queue_extension(queue_path, *, base_queue):
    """Build the compact workspace binding for one validated target queue."""
    _queue, ledger = validate_additive_generation_queue(
        queue_path, base_queue=base_queue
    )
    return {
        "schema": WORKSPACE_SCHEMA,
        "schema_version": WORKSPACE_VERSION,
        "base_queue_path": "provenance/seed-generation-queue.jsonl",
        "queue_path": "inputs/generation-queue.jsonl",
        "queue_sha256": sha256_file(queue_path),
        "base_queue_sha256": ledger["base_queue_sha256"],
        "extension_queue_sha256": ledger["extension_queue_sha256"],
        "extension_id": ledger["extension_id"],
        "added_item_count": ledger["added_item_count"],
        "added_queue_ids": sorted(
            record["queue_id"] for record in ledger["added_items"]
        ),
    }


def _story_order_key(document):
    return (
        _integer_order(document.get("collection_order")),
        _integer_order(document.get("story_order")),
        _integer_order(document.get("sequence")),
        str(document.get("line_id") or ""),
        str(document.get("queue_id") or ""),
    )


def _integer_order(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 2**63 - 1


def _sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise QueueExtensionError(f"{label} is invalid")
    return value


__all__ = [
    "FIELD",
    "QueueExtensionError",
    "publish_additive_generation_queue",
    "validate_additive_generation_queue",
    "workspace_queue_extension",
]
