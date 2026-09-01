"""Atomic portable game-pack publication for self-service pregeneration."""

from __future__ import annotations

import copy
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import GamePackError, write_game_pack
from vntts_artifacts.generated_audio import (
    GeneratedAudioManifestError,
    load_generated_audio_document,
    write_generated_audio_manifest,
)
from vntts_artifacts.story_index import (
    StoryIndexError,
    load_story_index_document,
    write_story_index_document,
)
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
    write_voice_manifest,
)

from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.authoring.generation_manifest import approved_manifest_entries
from vntts.authoring.generation_state import (
    AUDIO_EVENT_OMISSION_REASON,
    AUDIO_EVENT_OMISSION_SCHEMA,
    AUDIO_EVENT_OMISSION_VERSION,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
)
from vntts.game_pack import GamePackImport, import_game_pack
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.pregeneration_generation import (
    OfflineGenerationCancelled,
    OfflineGenerationError,
    OfflineGenerationResult,
)
from vntts.pregeneration_queue import (
    PregenerationInput,
    project_source_audio_semantics,
)
from vntts.pregeneration_setup import PregenerationJob
from vntts.source_audio_semantics import (
    canonical_document_sha256,
    load_source_audio_semantic_evidence,
)


class OfflinePackError(OfflineGenerationError):
    """A validated self-service result could not be published portably."""


@dataclass(frozen=True)
class OfflinePackResult:
    identity: str
    directory: Path
    manifest: Path
    imported: GamePackImport
    approved: int
    live_fallbacks: int
    story_lines: int = 0
    omissions: int = 0


class OfflinePackPublisher:
    def __init__(self, *, base_pack=None):
        self.base_pack = Path(base_pack).expanduser().resolve() if base_pack else None

    def publish(
        self,
        job,
        generation_input,
        generation_result,
        cancel_event=None,
    ):
        _validate_inputs(job, generation_input, generation_result)
        _raise_if_cancelled(cancel_event)
        try:
            story = load_story_index_document(generation_input.story_index)
            state_sha256 = sha256_file(generation_result.state)
        except (
            OSError,
            StoryIndexError,
            ValueError,
        ) as error:
            raise OfflinePackError(
                f"Unable to inspect prepared audio: {error}"
            ) from error
        base, source_story = _load_incremental_base(self.base_pack, job, story)
        base_identity = (
            None
            if base is None
            else base.pack.extensions["vntts.self-service"]["identity"]
        )
        identity = _identity(generation_input, state_sha256, base_identity)
        destination = (
            generation_input.directory.parent / "game-packs" / (f"pack-{identity[:24]}")
        )
        if destination.is_dir():
            return _load_existing(destination, identity)
        try:
            state = load_generation_state(
                generation_result.state,
                generation_input.queue,
            )
            queue = VoiceGenerationQueue.load(generation_input.queue)
            voice_document, voices = load_voice_manifest(
                generation_input.voice_manifest,
                allow_legacy=False,
            )
            current_omissions = _self_service_omission_records(
                job,
                generation_input,
                state_sha256,
                queue,
            )
            _require_terminal_generation(
                state,
                queue,
                omission_queue_ids={value["queue_id"] for value in current_omissions},
            )
        except (
            BulkGenerationError,
            GamePackError,
            GeneratedAudioManifestError,
            OSError,
            StoryIndexError,
            ValueError,
            VoiceGenerationQueueError,
            VoiceManifestError,
        ) as error:
            raise OfflinePackError(
                f"Unable to inspect prepared audio: {error}"
            ) from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
        )
        try:
            story_copy = staging / "story" / "story-index.jsonl"
            voice_copy = staging / "voices" / "voice-manifest.json"
            generated_copy = staging / "generated" / "manifest.json"
            if base is None:
                _copy_file(generation_input.story_index, story_copy)
                _copy_file(generation_input.voice_manifest, voice_copy)
                _copy_voice_references(
                    generation_input.voice_manifest,
                    voice_copy,
                    voice_document,
                    voices,
                )
                published_story = story
            else:
                published_story, semantic_copy, semantic_document = (
                    _write_cumulative_story(
                        base,
                        source_story,
                        generation_input,
                        story_copy,
                    )
                )
                _write_cumulative_voices(
                    base,
                    generation_input.voice_manifest,
                    voice_document,
                    voices,
                    voice_copy,
                )
            _raise_if_cancelled(cancel_event)
            generated_records, live_fallbacks, omissions = _write_cumulative_routes(
                base,
                story,
                state,
                generation_result,
                generated_copy,
                current_omissions,
            )
            write_generated_audio_manifest(
                generated_copy,
                {
                    "game": published_story.game,
                    "language": published_story.language,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "vntts.self-service.incremental": {
                        "schema_version": 1,
                        "base_pack_identity": base_identity,
                        "current_queue_sha256": generation_input.queue_sha256,
                    },
                    "vntts.authoring.live_fallback": {
                        "schema_version": 1,
                        "mode": "explicit",
                        "entries": live_fallbacks,
                    },
                    "vntts.authoring.audio_event_omission": {
                        "schema_version": 1,
                        "mode": "explicit",
                        "entries": omissions,
                    },
                },
                generated_records,
            )
            if base is None:
                semantic_copy, semantic_document = _copy_semantic_evidence(
                    generation_input,
                    staging,
                    story_copy,
                )
            components = {
                "story_index": story_copy,
                "voice_manifest": voice_copy,
                "generated_audio": generated_copy,
            }
            pack_metadata = {
                "game": {
                    "id": published_story.game or job.game,
                    "version": job.game_version or "local",
                },
                "producers": [{"name": "vntts-self-service", "version": "1"}],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "vntts.self-service": {
                    "schema_version": 1,
                    "identity": identity,
                    "job_id": job.job_id,
                    "generation_input_identity": generation_input.identity,
                    "source_queue_sha256": generation_input.queue_sha256,
                    "source_state_sha256": state_sha256,
                    "approved_count": len(generated_records),
                    "live_fallback_count": len(live_fallbacks),
                    "omission_count": len(omissions),
                    "story_line_count": len(published_story.records),
                    "base_pack_identity": base_identity,
                },
            }
            if semantic_copy is not None:
                pack_metadata["vntts.authoring"] = {
                    "source_audio_semantic_evidence": {
                        "path": "story/source-audio-semantic-evidence.json",
                        "sha256": sha256_file(semantic_copy),
                        "evidence_id": semantic_document["evidence_id"],
                        "entry_count": len(semantic_document["entries"]),
                    }
                }
            pack_manifest = staging / "game-pack.json"
            write_game_pack(pack_manifest, pack_metadata, components)
            import_game_pack(pack_manifest)
            GeneratedAudioLibrary(load_generated_audio_document(generated_copy))
            _raise_if_cancelled(cancel_event)
            try:
                rename_directory_no_replace(staging, destination)
            except AtomicPublicationError:
                if destination.is_dir():
                    return _load_existing(destination, identity)
                raise
            staging = None
            result = _load_existing(destination, identity)
            if result.approved != len(
                generated_records
            ) or result.live_fallbacks != len(live_fallbacks):
                raise OfflinePackError("Published offline pack counts changed")
            return result
        except OfflineGenerationCancelled:
            raise
        except (
            AtomicPublicationError,
            BulkGenerationError,
            GamePackError,
            GeneratedAudioManifestError,
            OSError,
            StoryIndexError,
            ValueError,
            VoiceManifestError,
        ) as error:
            raise OfflinePackError(
                f"Unable to publish offline pack: {error}"
            ) from error
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)


def _validate_inputs(job, generation_input, generation_result):
    if not isinstance(job, PregenerationJob):
        raise OfflinePackError("Offline preparation job is invalid")
    if not isinstance(generation_input, PregenerationInput):
        raise OfflinePackError("Offline generation input is invalid")
    if not isinstance(generation_result, OfflineGenerationResult):
        raise OfflinePackError("Offline generation result is invalid")
    expected_output = generation_input.directory.parent / (
        f"generation-output-{generation_input.identity[:16]}"
    )
    if generation_result.output.resolve() != expected_output.resolve():
        raise OfflinePackError("Offline pack output identity changed")


def _require_terminal_generation(state, queue, *, omission_queue_ids=()):
    if state.get("active") is not None:
        raise OfflinePackError("Offline generation is still active")
    expected = {item.queue_id for item in queue.items if item.action == "generate"}
    actual = set(state.get("items", {}))
    omission_queue_ids = set(omission_queue_ids)
    if actual != expected - omission_queue_ids:
        raise OfflinePackError("Offline generation does not cover the selected queue")
    for queue_id in sorted(expected - omission_queue_ids):
        item = state["items"][queue_id]
        status = item.get("status") if isinstance(item, dict) else None
        review = item.get("review_status") if isinstance(item, dict) else None
        if (status, review) not in {
            ("approved", "approved"),
            ("live_fallback", "live_fallback"),
        }:
            raise OfflinePackError(
                f"Offline generation item is not terminal: {queue_id!r}"
            )


def _identity(generation_input, state_sha256, base_pack_identity=None):
    payload = {
        "schema_version": 1,
        "generation_input_identity": generation_input.identity,
        "queue_sha256": generation_input.queue_sha256,
        "state_sha256": state_sha256,
        "story_index_sha256": sha256_file(generation_input.story_index),
        "voice_manifest_sha256": sha256_file(generation_input.voice_manifest),
        "semantic_evidence_sha256": (
            None
            if generation_input.source_audio_semantic_evidence is None
            else sha256_file(generation_input.source_audio_semantic_evidence)
        ),
    }
    if base_pack_identity is not None:
        payload["base_pack_identity"] = base_pack_identity
    return canonical_document_sha256(payload)


def _load_incremental_base(path, job, selected_story):
    if path is None or not path.is_file():
        return None, None
    try:
        imported = import_game_pack(path)
        extension = imported.pack.extensions.get("vntts.self-service")
        if not isinstance(extension, dict) or not _is_sha256(extension.get("identity")):
            return None, None
        if imported.pack.game_id != (
            selected_story.game or job.game
        ) or imported.pack.game_version != (job.game_version or "local"):
            return None, None
        source_path = Path(job.story_index).expanduser().resolve()
        if sha256_file(source_path) != job.story_index_sha256:
            raise OfflinePackError("Selected source story changed")
        source = load_story_index_document(source_path)
        base_story = load_story_index_document(imported.story_index)
    except (GamePackError, OSError, StoryIndexError, ValueError) as error:
        raise OfflinePackError(
            f"Unable to inspect the active offline pack: {error}"
        ) from error
    if (
        source.game != selected_story.game
        or source.language != selected_story.language
        or base_story.game != source.game
        or base_story.language != source.language
    ):
        return None, None
    source_by_id = {record.line_id: record for record in source.records}
    if any(
        record.line_id not in source_by_id
        or source_by_id[record.line_id].text_sha256 != record.text_sha256
        for record in base_story.records
    ):
        return None, None
    return imported, source


def _write_cumulative_story(
    base,
    source_story,
    generation_input,
    story_copy,
):
    base_story = load_story_index_document(base.story_index)
    current_story = load_story_index_document(generation_input.story_index)
    selected_ids = {
        *(record.line_id for record in base_story.records),
        *(record.line_id for record in current_story.records),
    }
    records = [
        record.to_record()
        for record in source_story.records
        if record.line_id in selected_ids
    ]
    if {record["line_id"] for record in records} != selected_ids:
        raise OfflinePackError("Cumulative story selection changed")
    story_copy.parent.mkdir(parents=True, exist_ok=True)
    metadata, records, semantic_copy = project_source_audio_semantics(
        source_story.path,
        source_story.metadata,
        records,
        story_copy.parent,
    )
    published_story = write_story_index_document(story_copy, metadata, records)
    semantic_document = (
        None
        if semantic_copy is None
        else load_source_audio_semantic_evidence(semantic_copy, story_copy)
    )
    return published_story, semantic_copy, semantic_document


def _write_cumulative_voices(
    base,
    current_manifest,
    current_document,
    current_voices,
    target_manifest,
):
    base_document, base_voices = load_voice_manifest(
        base.voice_manifest,
        allow_legacy=False,
    )
    merged = _portable_voice_entries(
        base.voice_manifest,
        target_manifest,
        base_document,
        base_voices,
    )
    for candidate in _portable_voice_entries(
        current_manifest,
        target_manifest,
        current_document,
        current_voices,
    ):
        names = {
            normalize_character_name(value)
            for value in (candidate["character"], *candidate.get("aliases", ()))
        }
        merged = [
            existing
            for existing in merged
            if names.isdisjoint(
                normalize_character_name(value)
                for value in (
                    existing["character"],
                    *existing.get("aliases", ()),
                )
            )
        ]
        merged.append(candidate)
    merged.sort(key=lambda value: value["character"].casefold())
    write_voice_manifest(target_manifest, {"version": 2, "voices": merged})


def _portable_voice_entries(source_manifest, target_manifest, document, voices):
    raw_voices = document.get("voices")
    if not isinstance(raw_voices, list) or len(raw_voices) != len(voices):
        raise OfflinePackError("Offline voice manifest changed")
    result = []
    for raw, voice in zip(raw_voices, voices, strict=True):
        if tuple(raw.get("references") or ()) != voice.references:
            raise OfflinePackError("Offline voice references changed")
        candidate = copy.deepcopy(raw)
        candidate.pop("reference", None)
        candidate["references"] = []
        for configured in voice.references:
            relative = _safe_relative(configured, "Voice reference")
            source = source_manifest.parent / Path(*relative.parts)
            digest = sha256_file(source)
            portable = f"references/{digest}.wav"
            _copy_file(source, target_manifest.parent / portable)
            candidate["references"].append(portable)
        result.append(candidate)
    return result


def _write_cumulative_routes(
    base,
    current_story,
    state,
    generation_result,
    generated_copy,
    current_omissions,
):
    current_line_ids = {record.line_id for record in current_story.records}
    records = []
    live_fallbacks = []
    omissions = []
    if base is not None:
        if base.generated_audio_manifest is None:
            raise OfflinePackError("Active self-service pack has no audio routes")
        base_generated = load_generated_audio_document(base.generated_audio_manifest)
        for record in base_generated.records:
            if record.line_id not in current_line_ids:
                records.append(
                    _portable_generated_record(
                        record.to_record(),
                        record.audio,
                        generated_copy,
                    )
                )
        live_fallbacks.extend(
            value
            for value in _document_live_fallbacks(base_generated)
            if value.get("line_id") not in current_line_ids
        )
        omissions.extend(
            value
            for value in _document_audio_event_omissions(base_generated)
            if value.get("line_id") not in current_line_ids
        )
    for record in approved_manifest_entries(state, generation_result.output):
        relative = _safe_relative(record["audio"], "Generated WAV")
        records.append(
            _portable_generated_record(
                record,
                generation_result.output / Path(*relative.parts),
                generated_copy,
            )
        )
    live_fallbacks.extend(_live_fallback_records(state))
    omissions.extend(copy.deepcopy(current_omissions))
    records.sort(key=lambda value: (value["line_id"], value["text_sha256"]))
    live_fallbacks.sort(key=lambda value: (value["line_id"], value["text_sha256"]))
    omissions.sort(key=lambda value: (value["line_id"], value["text_sha256"]))
    return records, live_fallbacks, omissions


def _portable_generated_record(record, source, generated_copy):
    candidate = copy.deepcopy(record)
    portable = f"audio/{candidate['audio_sha256']}.wav"
    _copy_file(source, generated_copy.parent / portable)
    candidate["audio"] = portable
    return candidate


def _document_live_fallbacks(document):
    extension = document.producer_metadata.get("vntts.authoring.live_fallback")
    if extension is None:
        return []
    entries = extension.get("entries") if isinstance(extension, dict) else None
    if not isinstance(entries, list):
        raise OfflinePackError("Active pack live fallback ledger is malformed")
    return copy.deepcopy(entries)


def _document_audio_event_omissions(document):
    extension = document.producer_metadata.get("vntts.authoring.audio_event_omission")
    if extension is None:
        return []
    entries = extension.get("entries") if isinstance(extension, dict) else None
    if not isinstance(entries, list):
        raise OfflinePackError("Active pack audio-event omission ledger is malformed")
    return copy.deepcopy(entries)


def _self_service_omission_records(job, generation_input, state_sha256, queue):
    queue_by_id = {item.queue_id: item for item in queue.items}
    queue_ids = tuple(sorted(generation_input.audio_event_omission_queue_ids))
    batch_id = canonical_document_sha256(
        {
            "schema_version": 1,
            "generation_input_identity": generation_input.identity,
            "state_sha256": state_sha256,
            "queue_sha256": generation_input.queue_sha256,
            "queue_ids": list(queue_ids),
        }
    )
    authority = {
        "batch_id": batch_id,
        "base_workspace_id": f"self-service:{generation_input.identity}",
        "base_workspace_sha256": generation_input.identity,
        "base_state_sha256": state_sha256,
        "queue_sha256": generation_input.queue_sha256,
    }
    records = []
    for queue_id in queue_ids:
        item = queue_by_id.get(queue_id)
        plan = None if item is None else audio_event_plan_for_record(item)
        if (
            item is None
            or item.action != "generate"
            or not isinstance(plan, dict)
            or not plan.get("requires_composition")
            or plan.get("spoken_text") != ""
        ):
            raise OfflinePackError(
                f"Offline audio-event omission is invalid: {queue_id!r}"
            )
        decision = {
            "schema": AUDIO_EVENT_OMISSION_SCHEMA,
            "schema_version": AUDIO_EVENT_OMISSION_VERSION,
            "reason": AUDIO_EVENT_OMISSION_REASON,
            "queue_id": queue_id,
            "line_id": item.line_id,
            "text_sha256": item.text_sha256,
            "speaker": item.speaker,
            "plan_sha256": plan["plan_sha256"],
            "spoken_text_sha256": plan["spoken_text_sha256"],
            "decided_at": job.created_at,
            "authority": copy.deepcopy(authority),
        }
        records.append(
            {
                **decision,
                "decision_sha256": canonical_document_sha256(decision),
            }
        )
    return records


def _live_fallback_records(state):
    records = []
    for item in state["items"].values():
        decision = item.get("live_fallback") if isinstance(item, dict) else None
        if not isinstance(decision, dict):
            continue
        records.append(
            {
                **copy.deepcopy(decision),
                "decision_sha256": canonical_document_sha256(decision),
            }
        )
    return sorted(records, key=lambda value: (value["line_id"], value["text_sha256"]))


def _copy_voice_references(source_manifest, target_manifest, document, voices):
    raw_voices = document.get("voices")
    if not isinstance(raw_voices, list) or len(raw_voices) != len(voices):
        raise OfflinePackError("Offline voice manifest changed")
    for raw, voice in zip(raw_voices, voices, strict=True):
        if tuple(raw.get("references") or ()) != voice.references:
            raise OfflinePackError("Offline voice references changed")
        for configured in voice.references:
            relative = _safe_relative(configured, "Voice reference")
            _copy_file(
                source_manifest.parent / Path(*relative.parts),
                target_manifest.parent / Path(*relative.parts),
            )


def _copy_semantic_evidence(generation_input, staging, story_copy):
    source = generation_input.source_audio_semantic_evidence
    if source is None:
        return None, None
    destination = staging / "story" / "source-audio-semantic-evidence.json"
    _copy_file(source, destination)
    return destination, load_source_audio_semantic_evidence(destination, story_copy)


def _copy_file(source, destination):
    source = Path(source).resolve()
    if not source.is_file() or source.is_symlink():
        raise OfflinePackError(f"Offline pack source is unsafe: {source}")
    before = sha256_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if sha256_file(source) != before or sha256_file(destination) != before:
        raise OfflinePackError(f"Offline pack source changed: {source}")


def _safe_relative(value, label):
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise OfflinePackError(f"{label} path is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise OfflinePackError(f"{label} leaves its component directory")
    return relative


def _load_existing(destination, identity):
    imported = import_game_pack(destination / "game-pack.json")
    extension = imported.pack.extensions.get("vntts.self-service")
    if not isinstance(extension, dict) or extension.get("identity") != identity:
        raise OfflinePackError("Existing offline pack identity changed")
    story_lines = len(load_story_index_document(imported.story_index).records)
    if extension.get("story_line_count", story_lines) != story_lines:
        raise OfflinePackError("Existing offline pack coverage changed")
    generated = load_generated_audio_document(imported.generated_audio_manifest)
    library = GeneratedAudioLibrary(generated)
    approved = extension.get("approved_count")
    live_fallbacks = extension.get("live_fallback_count")
    omissions = extension.get("omission_count", 0)
    if (
        approved != len(generated.records)
        or live_fallbacks != len(library.live_fallbacks)
        or omissions != len(library.audio_event_omissions)
    ):
        raise OfflinePackError("Existing offline pack route counts changed")
    return OfflinePackResult(
        identity=identity,
        directory=destination,
        manifest=imported.pack.manifest_path,
        imported=imported,
        approved=approved,
        live_fallbacks=live_fallbacks,
        story_lines=story_lines,
        omissions=omissions,
    )


def _raise_if_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise OfflineGenerationCancelled("Offline pack publication was cancelled")


def _is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "OfflinePackError",
    "OfflinePackPublisher",
    "OfflinePackResult",
]
