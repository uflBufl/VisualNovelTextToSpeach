"""Collection-driven voice-generation queue planning and publication."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts import (
    VOICE_GENERATION_QUEUE_SCHEMA,
    VOICE_GENERATION_QUEUE_SCHEMA_VERSION,
    StoryIndexDocument,
    StoryIndexRecord,
    expected_voice_generation_queue_id,
    voice_generation_action,
    write_voice_generation_queue,
)
from vntts_artifacts.audio import probe_pcm16_mono_wav
from vntts_artifacts.voice_manifest import (
    VoiceManifestEntry,
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
)

from vntts.authoring.delivery import (
    DELIVERY_ANNOTATION_VERSION,
    LEGACY_ENGLISH_POLICY,
    PRESERVE_DELIVERY_POLICY,
    DeliveryAnnotationError,
    apply_delivery_policy,
)
from vntts.voices import synthesis_character_for_line


class GenerationQueueBuildError(RuntimeError):
    """A story index cannot be turned into an unambiguous generation queue."""


@dataclass(frozen=True)
class GenerationQueueSummary:
    story_records: int
    selected_records: int
    queue_items: int
    character_count: int
    ready: int
    missing_reference: int
    recoverable_source_audio: int
    manual_review: int
    skipped_available: int
    skipped_unspeakable: int
    skipped_unselected: int
    action_counts: dict[str, int]
    source_audio_status_counts: dict[str, int]
    missing_reference_characters: tuple[str, ...]

    def to_dict(self):
        return {
            "story_records": self.story_records,
            "selected_records": self.selected_records,
            "queue_items": self.queue_items,
            "character_count": self.character_count,
            "ready": self.ready,
            "missing_reference": self.missing_reference,
            "recoverable_source_audio": self.recoverable_source_audio,
            "manual_review": self.manual_review,
            "skipped_available": self.skipped_available,
            "skipped_unspeakable": self.skipped_unspeakable,
            "skipped_unselected": self.skipped_unselected,
            "action_counts": dict(self.action_counts),
            "source_audio_status_counts": dict(self.source_audio_status_counts),
            "missing_reference_characters": list(self.missing_reference_characters),
        }


@dataclass(frozen=True)
class GenerationQueuePlan:
    metadata: dict[str, object]
    items: tuple[dict[str, object], ...]
    summary: GenerationQueueSummary


_QUEUE_OWNED_FIELDS = frozenset(
    {
        "record_type",
        "queue_id",
        "line_id",
        "text_sha256",
        "text",
        "speaker",
        "voice_character",
        "kind",
        "previous_text",
        "next_text",
        "context",
        "source_kind",
        "chapter",
        "sequence",
        "collection_id",
        "source_audio_id",
        "source_audio_status",
        "source_audio_reason",
        "action",
        "state",
    }
)


def plan_generation_queue(
    document: StoryIndexDocument,
    voice_manifest: tuple[VoiceManifestEntry, ...],
    voice_manifest_path,
    *,
    collection_ids: tuple[str, ...] | None = None,
    unknown_action: str | None = None,
    delivery_policy: str | None = None,
    generated_at: str | None = None,
):
    """Plan a queue from typed shared artifacts without reading producer JSON."""
    if not isinstance(document, StoryIndexDocument):
        raise GenerationQueueBuildError("story_index must be a StoryIndexDocument")
    entries = tuple(voice_manifest)
    if not all(isinstance(entry, VoiceManifestEntry) for entry in entries):
        raise GenerationQueueBuildError(
            "voice_manifest must contain validated VoiceManifestEntry values"
        )
    document_path = Path(document.path).expanduser().resolve()
    voice_manifest_path = Path(voice_manifest_path).expanduser().resolve()
    if not document_path.is_file():
        raise GenerationQueueBuildError(
            "StoryIndexDocument.path must identify the exact readable source file"
        )
    if not voice_manifest_path.is_file():
        raise GenerationQueueBuildError(
            "voice_manifest_path must identify the exact readable source file"
        )

    selected_collection_ids = _selected_collection_ids(document, collection_ids)
    selected = tuple(
        record
        for record in document.records
        if selected_collection_ids is None
        or record.collection_id in selected_collection_ids
    )
    voice_index = _voice_index(entries)
    manifest_directory = voice_manifest_path.parent
    reference_availability = {
        entry: _has_local_reference(entry, manifest_directory) for entry in entries
    }
    delivery_policy = (
        PRESERVE_DELIVERY_POLICY
        if delivery_policy is None
        else str(delivery_policy).strip()
    )
    if delivery_policy not in {PRESERVE_DELIVERY_POLICY, LEGACY_ENGLISH_POLICY}:
        raise GenerationQueueBuildError(
            f"Unsupported delivery_policy: {delivery_policy!r}"
        )
    annotation_origins = Counter()
    policy_generated_items = []

    items = []
    skipped_available = 0
    skipped_unspeakable = 0
    ready = 0
    missing_reference = 0
    recoverable = 0
    manual_review = 0
    missing_characters = set()
    for record in selected:
        if not record.speakable:
            skipped_unspeakable += 1
            continue
        action = voice_generation_action(
            record.source_audio_status,
            unknown_action=unknown_action,
        )
        if action is None:
            skipped_available += 1
            continue
        requested_character = synthesis_character_for_line(
            record.speaker, record.voice_character
        )
        entry = voice_index.get(normalize_character_name(requested_character))
        voice_character = entry.character if entry is not None else requested_character
        if action == "generate":
            if entry is not None and reference_availability[entry]:
                ready += 1
            else:
                missing_reference += 1
                missing_characters.add(voice_character)
        elif action in {"prefer_source_audio", "resolve_audio"}:
            recoverable += 1
        else:
            manual_review += 1
        item = _queue_item(record, voice_character, action)
        try:
            application = apply_delivery_policy(item, delivery_policy)
        except DeliveryAnnotationError as error:
            raise GenerationQueueBuildError(
                f"Unable to annotate story line {record.line_id!r}: {error}"
            ) from error
        annotation_origins[application.origin] += 1
        items.append(application.record)
        if application.provenance is not None:
            policy_generated_items.append(
                {
                    "queue_id": application.record["queue_id"],
                    **application.provenance,
                }
            )

    action_counts = _counts(item["action"] for item in items)
    status_counts = _counts(item["source_audio_status"] for item in items)
    characters = {str(item["voice_character"]) for item in items}
    summary = GenerationQueueSummary(
        story_records=len(document.records),
        selected_records=len(selected),
        queue_items=len(items),
        character_count=len(characters),
        ready=ready,
        missing_reference=missing_reference,
        recoverable_source_audio=recoverable,
        manual_review=manual_review,
        skipped_available=skipped_available,
        skipped_unspeakable=skipped_unspeakable,
        skipped_unselected=len(document.records) - len(selected),
        action_counts=action_counts,
        source_audio_status_counts=status_counts,
        missing_reference_characters=tuple(
            sorted(missing_characters, key=str.casefold)
        ),
    )
    metadata = {
        "record_type": "metadata",
        "schema": VOICE_GENERATION_QUEUE_SCHEMA,
        "schema_version": VOICE_GENERATION_QUEUE_SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "item_count": len(items),
        "character_count": len(characters),
        "source_audio_status_counts": status_counts,
        "action_counts": action_counts,
        "source_kind_counts": _counts(item["source_kind"] for item in items),
        "filters": {
            "collection_ids": []
            if selected_collection_ids is None
            else sorted(selected_collection_ids),
            "unknown_action": unknown_action,
        },
        "source_story_index_document_sha256": _json_sha256(
            {
                "metadata": document.metadata,
                "records": [record.to_record() for record in document.records],
            }
        ),
        "source_voice_manifest_entries_sha256": _json_sha256(
            [
                {
                    "character": entry.character,
                    "speaker": entry.speaker,
                    "aliases": list(entry.aliases),
                    "references": list(entry.references),
                }
                for entry in entries
            ]
        ),
    }
    if document.game is not None:
        metadata["game"] = document.game
    if document.language is not None:
        metadata["language"] = document.language
    metadata["source_story_index"] = str(document_path)
    metadata["source_story_index_sha256"] = _sha256_file(document_path)
    metadata["source_voice_manifest"] = str(voice_manifest_path)
    metadata["source_voice_manifest_sha256"] = _sha256_file(voice_manifest_path)
    if delivery_policy != PRESERVE_DELIVERY_POLICY:
        metadata["delivery_annotation_policy"] = {
            "name": delivery_policy,
            "version": DELIVERY_ANNOTATION_VERSION,
            "mode": "missing-only",
            "policy_generated_count": annotation_origins["policy"],
            "source_complete_count": annotation_origins["source_complete"],
            "source_partial_count": annotation_origins["source_partial"],
            "unannotated_count": annotation_origins["none"],
            "generated_items": policy_generated_items,
        }
    return GenerationQueuePlan(metadata, tuple(items), summary)


def inspect_generation_queue(
    story_index_path,
    voice_manifest_path,
    *,
    collection_ids: tuple[str, ...] | None = None,
    unknown_action: str | None = None,
    delivery_policy: str | None = None,
    generated_at: str | None = None,
):
    """Load public shared artifacts and return a non-mutating queue plan."""
    story_index_path = Path(story_index_path).expanduser().resolve()
    voice_manifest_path = Path(voice_manifest_path).expanduser().resolve()
    document = StoryIndexDocument.load(story_index_path)
    try:
        _manifest, entries = load_voice_manifest(
            voice_manifest_path, allow_legacy=False
        )
    except VoiceManifestError as error:
        raise GenerationQueueBuildError(
            "Voice manifest reference leaves its canonical manifest directory or "
            f"is otherwise unsafe: {error}"
        ) from error
    return plan_generation_queue(
        document,
        entries,
        voice_manifest_path,
        collection_ids=collection_ids,
        unknown_action=unknown_action,
        delivery_policy=delivery_policy,
        generated_at=generated_at,
    )


def publish_generation_queue(plan: GenerationQueuePlan, output_path):
    """Atomically publish a previously inspected queue plan."""
    if not isinstance(plan, GenerationQueuePlan):
        raise GenerationQueueBuildError("plan must be a GenerationQueuePlan")
    return write_voice_generation_queue(output_path, plan.metadata, plan.items)


def _selected_collection_ids(document, collection_ids):
    if collection_ids is None:
        return None
    normalized = tuple(dict.fromkeys(str(value).strip() for value in collection_ids))
    if not normalized or any(not value for value in normalized):
        raise GenerationQueueBuildError("collection_ids must contain non-empty values")
    for collection_id in normalized:
        document.records_for_collection(collection_id)
    return frozenset(normalized)


def _voice_index(entries):
    result = {}
    for entry in entries:
        for name in (entry.character, *entry.aliases):
            result[normalize_character_name(name)] = entry
    return result


def _has_local_reference(entry, manifest_directory):
    if entry is None or not entry.references:
        return False
    candidates = []
    root = Path(manifest_directory).resolve()
    for reference in entry.references:
        if not isinstance(reference, str) or not reference or "\\" in reference:
            raise GenerationQueueBuildError(
                "Voice reference must be a non-empty POSIX-relative path"
            )
        pure = PurePosixPath(reference)
        if (
            pure.is_absolute()
            or any(part in {"", ".", ".."} for part in reference.split("/"))
            or (pure.parts and ":" in pure.parts[0])
        ):
            raise GenerationQueueBuildError(
                f"Voice reference must stay inside the manifest directory: {reference!r}"
            )
        candidate = (root / Path(*pure.parts)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise GenerationQueueBuildError(
                f"Voice reference leaves the manifest directory: {reference!r}"
            ) from error
        candidates.append(candidate)
    if not candidates or not all(candidate.is_file() for candidate in candidates):
        return False
    try:
        for candidate in candidates:
            probe_pcm16_mono_wav(candidate)
    except (OSError, ValueError):
        return False
    return True


def _queue_item(record: StoryIndexRecord, voice_character, action):
    item = {
        key: value
        for key, value in record.producer_fields.items()
        if key not in _QUEUE_OWNED_FIELDS
    }
    item.update(
        {
            "record_type": "generation_item",
            "queue_id": expected_voice_generation_queue_id(
                record.line_id, record.text_sha256
            ),
            "line_id": record.line_id,
            "text_sha256": record.text_sha256,
            "text": record.text,
            "speaker": record.speaker,
            "voice_character": voice_character,
            "kind": record.kind,
            "previous_text": record.previous_text,
            "next_text": record.next_text,
            "context": record.context,
            "source_kind": record.source_kind,
            "chapter": record.chapter,
            "sequence": record.sequence,
            "collection_id": record.collection_id,
            "source_audio_id": record.source_audio_id,
            "source_audio_status": record.source_audio_status,
            "source_audio_reason": record.source_audio_reason or "not_reported",
            "action": action,
            "state": "pending",
        }
    )
    return item


def _counts(values):
    return dict(sorted(Counter(str(value) for value in values).items()))


def _sha256_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json_sha256(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
