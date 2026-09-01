"""Publish exact live fallbacks for speech mixed with unsupported audio events."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
    process_is_alive,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.generation_state import (
    AUDIO_EVENT_PROJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA,
    LIVE_FALLBACK_AUDIO_EVENT_PROJECTION_VERSION,
    LIVE_FALLBACK_SCHEMA,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    generation_publication_leases,
    rename_directory_no_replace,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    WorkspaceCreationResult,
    contained_workspace_path,
    default_workspaces_root,
    load_workspace_authority,
    load_workspace_json,
    read_workspace_file_bytes,
    require_workspace_sha256,
    safe_workspace_relative_path,
    validate_workspace_provenance_extensions,
)
from vntts.authoring.workspace_config import (
    workspace_config_fingerprint,
    workspace_missing_voice_policy,
)
from vntts.authoring.workspace_foundation import (
    copy_generation_wavs,
    copy_workspace_tree_snapshot,
)
from vntts.authoring.workspace_state import load_stable_workspace_generation_state

_copy_base_wavs = partial(
    copy_generation_wavs,
    target_label="Audio-event projection WAV",
    error_type=AuthoringWorkbenchError,
)

SCHEMA = "vntts.authoring-audio-event-projection-fallback-batch"
SCHEMA_VERSION = 1
REASON = "generated_audio_rejected"
SYNTHESIS_CHARACTER = "Narrator"


def create_audio_event_projection_fallback_workspace(
    base_workspace,
    queue_ids,
    workspaces_root=None,
):
    """Authorize Pocket synthesis of only the spoken projection of mixed events."""
    base_directory, base_document, base_workspace_sha256 = load_workspace_authority(
        base_workspace
    )
    selected = tuple(sorted(str(value) for value in queue_ids))
    if not selected or len(selected) != len(set(selected)):
        raise AuthoringWorkbenchError(
            "Audio-event projection fallback requires unique exact queue IDs"
        )
    queue, state, _payload, state_sha256 = load_stable_workspace_generation_state(
        base_directory,
        base_document,
        "audio-event projection fallback base",
        error_type=AuthoringWorkbenchError,
    )
    if state.get("active") is not None:
        raise AuthoringWorkbenchError("Audio-event projection fallback base is active")
    queue_path = base_directory / "queue.jsonl"
    queue_sha256 = sha256_file(queue_path)
    queue_by_id = {item.queue_id: item for item in queue.items}
    policy = workspace_missing_voice_policy(
        base_document, error_type=AuthoringWorkbenchError
    )
    ledgers = []
    for queue_id in selected:
        queue_item = queue_by_id.get(queue_id)
        base_result = state["items"].get(queue_id)
        if queue_item is None or queue_item.action != "generate":
            raise AuthoringWorkbenchError(
                f"Audio-event projection queue ID is unavailable: {queue_id!r}"
            )
        if (
            not isinstance(base_result, dict)
            or base_result.get("status") != "generated"
            or base_result.get("review_status") != "rejected"
            or isinstance(base_result.get("live_fallback"), dict)
        ):
            raise AuthoringWorkbenchError(
                f"Audio-event projection requires one rejected result: {queue_id!r}"
            )
        fallback = base_result.get("synthesis_fallback")
        if (
            not policy.applies_to(queue_item.speaker)
            or base_result.get("requested_voice_character") != queue_item.speaker
            or base_result.get("voice_character") != SYNTHESIS_CHARACTER
            or not isinstance(fallback, dict)
            or fallback.get("kind") != "missing_voice_to_narrator"
            or fallback.get("source_voice_character") != queue_item.speaker
            or fallback.get("synthesis_voice_character") != SYNTHESIS_CHARACTER
        ):
            raise AuthoringWorkbenchError(
                f"Audio-event projection is not routed through Narrator: {queue_id!r}"
            )
        try:
            plan = audio_event_plan_for_record(queue_item)
        except ValueError as error:
            raise AuthoringWorkbenchError(str(error)) from error
        if (
            not isinstance(plan, dict)
            or not plan.get("requires_composition")
            or not isinstance(plan.get("events"), list)
            or not plan["events"]
            or not isinstance(plan.get("spoken_text"), str)
            or not plan["spoken_text"].strip()
            or plan["spoken_text"] == queue_item.text
        ):
            raise AuthoringWorkbenchError(
                f"Audio-event projection requires mixed speech and events: {queue_id!r}"
            )
        ledgers.append(
            {
                "queue_id": queue_id,
                "line_id": queue_item.line_id,
                "text_sha256": queue_item.text_sha256,
                "speaker": queue_item.speaker,
                "plan_sha256": plan["plan_sha256"],
                "spoken_text": plan["spoken_text"],
                "spoken_text_sha256": plan["spoken_text_sha256"],
                "base_result_sha256": canonical_document_sha256(base_result),
            }
        )

    batch_body = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "base_workspace_id": base_document["workspace_id"],
        "base_workspace_path": "inputs/audio-event-projection/base-workspace.json",
        "base_workspace_sha256": base_workspace_sha256,
        "base_state_path": "inputs/audio-event-projection/base-generation-state.json",
        "base_state_sha256": state_sha256,
        "queue_sha256": queue_sha256,
        "provider": "pocket-tts",
        "model": "pocket-tts",
        "generation_profile": "default",
        "synthesis_character": SYNTHESIS_CHARACTER,
        "items": ledgers,
    }
    batch_id = canonical_document_sha256(batch_body)
    batch = {**batch_body, "batch_id": batch_id}
    config_fingerprint = workspace_config_fingerprint(
        base_document["source"]["import_id"],
        base_document.get("story_index"),
        base_document.get("voice_manifest"),
        base_document["narrator_character"],
        base_document["run_config"],
        base_document.get("carry_forward"),
        base_document.get("outcome_merge"),
        base_document.get("failure_reference_binding"),
        base_document.get("terminal_conflict_merge"),
        base_document.get("config_rebase"),
        base_document.get("audio_event_composition"),
        base_document.get("explicit_fallback_merge"),
        base_document.get("known_role_live_fallback"),
        base_document.get("audio_event_omission"),
        batch,
        base_document.get("reviewed_waveform_publication"),
        base_document.get("reviewed_rejection_live_fallback"),
        queue_extension=base_document.get("queue_extension"),
    )
    workspace_id = (
        f"resume-{base_document['source']['import_id'].removeprefix('legacy-')}-"
        f"{config_fingerprint[:16]}"
    )
    root = Path(workspaces_root or default_workspaces_root()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = contained_workspace_path(
        root, Path(workspace_id), "Audio-event projection fallback destination"
    )
    if destination.exists():
        _directory, existing, _sha256 = load_workspace_authority(destination)
        if existing.get("audio_event_projection_fallback") != batch:
            raise AuthoringWorkbenchError(
                "Audio-event projection fallback destination conflicts"
            )
        return WorkspaceCreationResult(destination, False)

    staging_owner = TemporaryDirectory(prefix=".audio-event-projection-", dir=root)
    staging = Path(staging_owner.name).resolve()
    snapshots = [
        (base_directory / "workspace.json", base_workspace_sha256),
        (base_directory / "generated-audio/generation-state.json", state_sha256),
        (queue_path, queue_sha256),
    ]
    try:
        for tree_name in ("provenance", "inputs"):
            copy_workspace_tree_snapshot(
                base_directory / tree_name,
                staging / tree_name,
                snapshots,
                error_type=AuthoringWorkbenchError,
            )
        projection_inputs = staging / "inputs/audio-event-projection"
        projection_inputs.mkdir(parents=True)
        (projection_inputs / "base-workspace.json").write_bytes(
            read_workspace_file_bytes(
                base_directory / "workspace.json",
                "audio-event projection base workspace",
            )
        )
        (projection_inputs / "base-generation-state.json").write_bytes(
            read_workspace_file_bytes(
                base_directory / "generated-audio/generation-state.json",
                "audio-event projection base state",
            )
        )
        (staging / "queue.jsonl").write_bytes(
            read_workspace_file_bytes(queue_path, "audio-event projection queue")
        )
        output = staging / "generated-audio"
        output.mkdir()
        target_state = copy.deepcopy(state)
        _copy_base_wavs(base_directory, output, state, snapshots)
        decided_at = datetime.now(timezone.utc).isoformat()
        for ledger in ledgers:
            queue_id = ledger["queue_id"]
            queue_item = queue_by_id[queue_id]
            base_result = state["items"][queue_id]
            evidence = {
                "schema": AUDIO_EVENT_PROJECTION_LIVE_FALLBACK_EVIDENCE_SCHEMA,
                "schema_version": 1,
                "batch_id": batch_id,
                "base_workspace_id": base_document["workspace_id"],
                "base_workspace_sha256": base_workspace_sha256,
                "base_state_sha256": state_sha256,
                "queue_sha256": queue_sha256,
                "queue_id": queue_id,
                "base_result_sha256": ledger["base_result_sha256"],
                "base_result": copy.deepcopy(base_result),
                "plan_sha256": ledger["plan_sha256"],
                "spoken_text": ledger["spoken_text"],
                "spoken_text_sha256": ledger["spoken_text_sha256"],
                "source_character": queue_item.speaker,
                "synthesis_character": SYNTHESIS_CHARACTER,
            }
            decision = {
                "schema": LIVE_FALLBACK_SCHEMA,
                "schema_version": LIVE_FALLBACK_AUDIO_EVENT_PROJECTION_VERSION,
                "reason": REASON,
                "provider": "pocket-tts",
                "model": "pocket-tts",
                "generation_profile": "default",
                "queue_id": queue_id,
                "line_id": queue_item.line_id,
                "text_sha256": queue_item.text_sha256,
                "speaker": queue_item.speaker,
                "requested_voice_character": SYNTHESIS_CHARACTER,
                "previous_result_sha256": ledger["base_result_sha256"],
                "decided_at": decided_at,
                "evidence": evidence,
            }
            projected = copy.deepcopy(base_result)
            projected["live_fallback"] = decision
            projected["updated_at"] = decided_at
            target_state["items"][queue_id] = projected
        target_state["active"] = None
        atomic_write_json(
            output / "generation-state.json", target_state, sort_keys=True
        )
        write_generated_manifest_from_state(
            target_state, output, output / "manifest.json"
        )
        workspace = copy.deepcopy(base_document)
        workspace.update(
            {
                "workspace_id": workspace_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "audio_event_projection_fallback": batch,
                "config_fingerprint": config_fingerprint,
            }
        )
        atomic_write_json(staging / "workspace.json", workspace, sort_keys=True)
        import_snapshot = load_workspace_json(
            staging / "provenance/import.json", "audio-event projection import"
        )
        validate_workspace_provenance_extensions(staging, workspace, import_snapshot)
        load_generation_state(output / "generation-state.json", staging / "queue.jsonl")
        try:
            with generation_publication_leases(
                ((base_directory / "generated-audio", queue_sha256),),
                process_checker=process_is_alive,
            ) as leases:
                if any((base_directory / "generated-audio").rglob("*.partial.wav")):
                    raise AuthoringWorkbenchError(
                        "Audio-event projection base became active"
                    )
                for path, digest in snapshots:
                    if not path.is_file() or sha256_file(path) != digest:
                        raise AuthoringWorkbenchError(
                            "Audio-event projection authority changed before publication"
                        )
                leases[0].assert_owned()
                try:
                    rename_directory_no_replace(staging, destination)
                except (AtomicPublicationError, OSError) as error:
                    raise AuthoringWorkbenchError(
                        f"Unable to publish audio-event projection workspace: {error}"
                    ) from error
                leases[0].mark_committed()
        except BulkGenerationError as error:
            raise AuthoringWorkbenchError(str(error)) from error
    except (BulkGenerationError, OSError, ValueError) as error:
        raise AuthoringWorkbenchError(str(error)) from error
    finally:
        staging_owner.cleanup()
    return WorkspaceCreationResult(destination, True)


def validate_audio_event_projection_fallback_workspace(directory, workspace):
    """Validate the self-contained mixed-event fallback authority."""
    batch = workspace.get("audio_event_projection_fallback")
    if batch is None:
        return
    fields = {
        "schema",
        "schema_version",
        "batch_id",
        "base_workspace_id",
        "base_workspace_path",
        "base_workspace_sha256",
        "base_state_path",
        "base_state_sha256",
        "queue_sha256",
        "provider",
        "model",
        "generation_profile",
        "synthesis_character",
        "items",
    }
    if (
        not isinstance(batch, dict)
        or set(batch) != fields
        or batch.get("schema") != SCHEMA
        or batch.get("schema_version") != SCHEMA_VERSION
        or batch.get("provider") != "pocket-tts"
        or batch.get("model") != "pocket-tts"
        or batch.get("generation_profile") != "default"
        or batch.get("synthesis_character") != SYNTHESIS_CHARACTER
        or batch.get("batch_id")
        != canonical_document_sha256(
            {key: value for key, value in batch.items() if key != "batch_id"}
        )
    ):
        raise AuthoringWorkbenchError(
            "Audio-event projection fallback batch is malformed"
        )
    for field in (
        "batch_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
    ):
        require_workspace_sha256(
            batch.get(field), f"Audio-event projection fallback {field}"
        )
    root = Path(directory)
    snapshots = {}
    for path_field, hash_field, label in (
        ("base_workspace_path", "base_workspace_sha256", "base workspace"),
        ("base_state_path", "base_state_sha256", "base state"),
    ):
        relative = safe_workspace_relative_path(
            batch.get(path_field), f"Audio-event projection {label}"
        )
        source = contained_workspace_path(
            root, relative, f"Audio-event projection {label}"
        )
        if not source.is_file() or sha256_file(source) != batch[hash_field]:
            raise AuthoringWorkbenchError(
                f"Audio-event projection {label} authority changed"
            )
        snapshots[path_field] = source
    base_state = load_workspace_json(
        snapshots["base_state_path"], "audio-event projection base state"
    )
    queue, state, _payload, _state_sha256 = load_stable_workspace_generation_state(
        root,
        workspace,
        "audio-event projection workspace",
        error_type=AuthoringWorkbenchError,
    )
    if sha256_file(root / "queue.jsonl") != batch["queue_sha256"]:
        raise AuthoringWorkbenchError("Audio-event projection queue changed")
    queue_by_id = {item.queue_id: item for item in queue.items}
    items = batch.get("items")
    if not isinstance(items, list) or not items:
        raise AuthoringWorkbenchError("Audio-event projection ledger is empty")
    observed = []
    for ledger in items:
        ledger_fields = {
            "queue_id",
            "line_id",
            "text_sha256",
            "speaker",
            "plan_sha256",
            "spoken_text",
            "spoken_text_sha256",
            "base_result_sha256",
        }
        if not isinstance(ledger, dict) or set(ledger) != ledger_fields:
            raise AuthoringWorkbenchError("Audio-event projection item is malformed")
        queue_id = ledger.get("queue_id")
        queue_item = queue_by_id.get(queue_id)
        base_result = base_state.get("items", {}).get(queue_id)
        result = state["items"].get(queue_id)
        decision = result.get("live_fallback") if isinstance(result, dict) else None
        evidence = decision.get("evidence") if isinstance(decision, dict) else None
        if (
            queue_item is None
            or not isinstance(base_result, dict)
            or canonical_document_sha256(base_result) != ledger["base_result_sha256"]
            or not isinstance(evidence, dict)
            or evidence.get("base_result") != base_result
            or evidence.get("batch_id") != batch["batch_id"]
            or evidence.get("plan_sha256") != ledger["plan_sha256"]
            or evidence.get("spoken_text") != ledger["spoken_text"]
            or evidence.get("spoken_text_sha256") != ledger["spoken_text_sha256"]
            or decision.get("previous_result_sha256") != ledger["base_result_sha256"]
            or decision.get("requested_voice_character") != SYNTHESIS_CHARACTER
        ):
            raise AuthoringWorkbenchError(
                f"Audio-event projection result changed for {queue_id!r}"
            )
        observed.append(queue_id)
    if observed != sorted(set(observed)):
        raise AuthoringWorkbenchError("Audio-event projection items are not canonical")


__all__ = [
    "create_audio_event_projection_fallback_workspace",
    "validate_audio_event_projection_fallback_workspace",
]
