"""Atomically authorize an audited known-role live fallback cohort."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    LIVE_FALLBACK_MISSING_VOICE_EVIDENCE_VERSION,
    LIVE_FALLBACK_SCHEMA,
    MISSING_VOICE_LIVE_FALLBACK_EVIDENCE_SCHEMA,
    BulkGenerationError,
    BulkGenerationSourceChangedError,
    _validate_state_document,
)
from vntts.authoring.generation_lease import GenerationLease, process_is_alive
from vntts.authoring.generation_manifest import (
    approved_manifest_entries,
    write_generated_manifest_from_state,
)
from vntts.authoring.generation_state import load_stable_generation_queue
from vntts.authoring.missing_voice_reuse import (
    _validate_plan,
    load_missing_voice_reuse_plan,
)
from vntts.authoring.missing_voice_reuse_binding import (
    MISSING_VOICE_REUSE_DECISION_SCHEMA,
    MissingVoiceReuseBindingError,
    _validate_binding_bundle,
)
from vntts.authoring.workbench import inspect_workspace
from vntts.voices import synthesis_character_for_line

AUTOMATIC_UNRESOLVED_ORIGIN = "automatic_no_complete_candidate"


class MissingVoiceLiveFallbackError(RuntimeError):
    """An audited missing-voice cohort cannot become one live fallback batch."""


@dataclass(frozen=True)
class MissingVoiceLiveFallbackResult:
    workspace: Path
    character: str
    narrator_character: str
    target_count: int
    cohort_count: int
    batch_id: str
    authority_decision_id: str
    state_sha256_before: str
    state_sha256_after: str
    applied: bool
    created: bool

    def to_dict(self):
        return {**asdict(self), "workspace": str(self.workspace)}


def authorize_missing_voice_live_fallback(
    workspace,
    authority_directory,
    character,
    *,
    accept_known_role_narrator_fallback=False,
):
    """Validate, then optionally commit, one exact zero-override cohort batch."""
    workspace = Path(workspace).expanduser().resolve()
    authority_directory = Path(authority_directory).expanduser().resolve()
    character = _required_text(character, "Missing-voice fallback character")
    state_path = workspace / "generated-audio/generation-state.json"
    queue_path = workspace / "queue.jsonl"
    workspace_path = workspace / "workspace.json"
    inspect_workspace(workspace)
    workspace_document = _read_json(workspace_path, "workspace")
    workspace_sha256 = sha256_file(workspace_path)
    narrator_character = _required_text(
        workspace_document.get("narrator_character"), "Configured narrator character"
    )
    queue, queue_sha256 = load_stable_generation_queue(queue_path)
    queue_by_id = {item.queue_id: item for item in queue.items}
    state_payload = state_path.read_bytes()
    state_sha256 = hashlib.sha256(state_payload).hexdigest()
    state = _decode_state(state_payload)
    _validate_state_document(state, state_path.parent, queue, queue_sha256)
    if state.get("active") is not None or any(state_path.parent.rglob("*.partial.wav")):
        raise MissingVoiceLiveFallbackError(
            "Missing-voice fallback requires an inactive workspace"
        )

    authority = _load_authority(authority_directory)
    plan = authority["plan"]
    binding = authority["decision"]["binding"]
    targets = _validated_targets(plan, binding, character, queue_by_id)
    source_workspace = Path(plan["source"]["workspace"]).resolve()
    source_document = _read_json(
        source_workspace / "workspace.json", "source workspace"
    )
    if (
        workspace_document.get("source") != source_document.get("source")
        or queue_sha256 != plan["source"]["queue_sha256"]
        or binding.get("source_workspace_id") != plan["source"]["workspace_id"]
        or binding.get("source_workspace_sha256") != plan["source"]["workspace_sha256"]
    ):
        raise MissingVoiceLiveFallbackError(
            "Missing-voice fallback workspace differs from its audited source"
        )

    existing = [state["items"].get(target["queue_id"]) for target in targets]
    already_applied = _existing_batch_id(
        existing,
        targets,
        authority,
        character,
        narrator_character,
    )
    if already_applied is not None:
        return MissingVoiceLiveFallbackResult(
            workspace,
            character,
            narrator_character,
            len(targets),
            len({target["cohort_id"] for target in targets}),
            already_applied,
            authority["decision"]["decision_id"],
            state_sha256,
            state_sha256,
            applied=True,
            created=False,
        )
    if any(value is not None for value in existing):
        raise MissingVoiceLiveFallbackError(
            "Missing-voice fallback target scope is stale or partially applied"
        )

    batch_body = {
        "schema": "vntts.authoring-missing-voice-live-fallback-batch",
        "schema_version": 1,
        "workspace_id": workspace_document["workspace_id"],
        "workspace_sha256": workspace_sha256,
        "state_sha256": state_sha256,
        "queue_sha256": queue_sha256,
        "authority_bundle_id": authority["bundle"]["bundle_id"],
        "authority_bundle_sha256": authority["bundle_sha256"],
        "authority_decision_id": authority["decision"]["decision_id"],
        "authority_decision_sha256": authority["decision_sha256"],
        "plan_id": plan["plan_id"],
        "character": character,
        "configured_narrator_character": narrator_character,
        "provider": "pocket-tts",
        "model": "pocket-tts",
        "generation_profile": "default",
        "queue_ids": [target["queue_id"] for target in targets],
    }
    batch_id = canonical_document_sha256(batch_body)
    if not accept_known_role_narrator_fallback:
        return MissingVoiceLiveFallbackResult(
            workspace,
            character,
            narrator_character,
            len(targets),
            len({target["cohort_id"] for target in targets}),
            batch_id,
            authority["decision"]["decision_id"],
            state_sha256,
            state_sha256,
            applied=False,
            created=False,
        )

    decided_at = datetime.now(timezone.utc).isoformat()
    proposed = copy.deepcopy(state)
    for target in targets:
        queue_id = target["queue_id"]
        queue_item = queue_by_id[queue_id]
        evidence = {
            "schema": MISSING_VOICE_LIVE_FALLBACK_EVIDENCE_SCHEMA,
            "schema_version": 1,
            "authority_bundle_id": authority["bundle"]["bundle_id"],
            "authority_bundle_sha256": authority["bundle_sha256"],
            "authority_decision_id": authority["decision"]["decision_id"],
            "authority_decision_sha256": authority["decision_sha256"],
            "plan_id": plan["plan_id"],
            "source_workspace_id": plan["source"]["workspace_id"],
            "source_workspace_sha256": plan["source"]["workspace_sha256"],
            "cohort_id": target["cohort_id"],
            "queue_id": queue_id,
            "decision_origin": AUTOMATIC_UNRESOLVED_ORIGIN,
            "requested_voice_character": character,
            "configured_narrator_character": narrator_character,
            "batch_id": batch_id,
        }
        decision = {
            "schema": LIVE_FALLBACK_SCHEMA,
            "schema_version": LIVE_FALLBACK_MISSING_VOICE_EVIDENCE_VERSION,
            "reason": "reference_unavailable_after_audit",
            "provider": "pocket-tts",
            "model": "pocket-tts",
            "generation_profile": "default",
            "queue_id": queue_id,
            "line_id": queue_item.line_id,
            "text_sha256": queue_item.text_sha256,
            "speaker": queue_item.speaker,
            "requested_voice_character": character,
            "previous_result_sha256": None,
            "decided_at": decided_at,
            "evidence": evidence,
        }
        proposed["items"][queue_id] = {
            "status": "live_fallback",
            "review_status": "live_fallback",
            "attempts": 0,
            "line_id": queue_item.line_id,
            "text_sha256": queue_item.text_sha256,
            "speaker": queue_item.speaker,
            "requested_voice_character": character,
            "voice_character": character,
            "live_fallback": decision,
            "updated_at": decided_at,
        }
    _validate_state_document(proposed, state_path.parent, queue, queue_sha256)
    entries = approved_manifest_entries(proposed, state_path.parent)
    transaction_id = secrets.token_hex(16)
    staged_state = state_path.with_name(f".{state_path.name}.{transaction_id}.tmp")
    manifest_path = state_path.parent / "manifest.json"
    staged_manifest = manifest_path.with_name(
        f".{manifest_path.name}.{transaction_id}.tmp"
    )
    try:
        with GenerationLease(
            state_path.parent,
            queue_sha256,
            process_checker=process_is_alive,
        ) as lease:
            atomic_write_json(staged_state, proposed, sort_keys=True)
            write_generated_manifest_from_state(
                proposed,
                state_path.parent,
                staged_manifest,
                entries=entries,
            )
            if sha256_file(queue_path) != queue_sha256:
                raise BulkGenerationSourceChangedError(
                    "Generation queue changed before missing-voice fallback commit"
                )
            if sha256_file(state_path) != state_sha256:
                raise BulkGenerationSourceChangedError(
                    "Generation state changed before missing-voice fallback commit"
                )
            for path, digest in authority["snapshots"]:
                if not path.is_file() or sha256_file(path) != digest:
                    raise BulkGenerationSourceChangedError(
                        "Missing-voice fallback authority changed before commit"
                    )
            if sha256_file(workspace_path) != workspace_sha256:
                raise BulkGenerationSourceChangedError(
                    "Workspace changed before missing-voice fallback commit"
                )
            lease.assert_owned()
            os.replace(staged_state, state_path)
            lease.assert_owned()
            os.replace(staged_manifest, manifest_path)
            lease.mark_committed()
    except BulkGenerationError as error:
        raise MissingVoiceLiveFallbackError(str(error)) from error
    finally:
        for path in (staged_state, staged_manifest):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
    return MissingVoiceLiveFallbackResult(
        workspace,
        character,
        narrator_character,
        len(targets),
        len({target["cohort_id"] for target in targets}),
        batch_id,
        authority["decision"]["decision_id"],
        state_sha256,
        sha256_file(state_path),
        applied=True,
        created=True,
    )


def _load_authority(directory):
    if not directory.is_dir():
        raise MissingVoiceLiveFallbackError(
            "Missing-voice fallback authority directory is missing"
        )
    decision_path = directory / "decision.json"
    bundle_path = directory / "bundle.json"
    decision = _read_json(decision_path, "missing-voice decision")
    expected_fields = {
        "schema",
        "schema_version",
        "plan_path",
        "plan_sha256",
        "session_path",
        "binding",
        "decision_id",
    }
    if (
        set(decision) != expected_fields
        or decision.get("schema") != MISSING_VOICE_REUSE_DECISION_SCHEMA
        or decision.get("schema_version") != 1
        or decision.get("decision_id")
        != canonical_document_sha256(
            {key: value for key, value in decision.items() if key != "decision_id"}
        )
    ):
        raise MissingVoiceLiveFallbackError(
            "Missing-voice fallback decision identity is invalid"
        )
    plan_path = Path(decision["plan_path"]).expanduser().resolve()
    session_path = Path(decision["session_path"]).expanduser().resolve()
    if not plan_path.is_file() or sha256_file(plan_path) != decision["plan_sha256"]:
        raise MissingVoiceLiveFallbackError(
            "Missing-voice fallback plan authority changed"
        )
    binding = decision.get("binding")
    if (
        not session_path.is_file()
        or not isinstance(binding, dict)
        or sha256_file(session_path) != binding.get("review_session_sha256")
    ):
        raise MissingVoiceLiveFallbackError(
            "Missing-voice fallback review session authority changed"
        )
    plan = _validate_plan(load_missing_voice_reuse_plan(plan_path))
    try:
        _validate_binding_bundle(directory, plan, binding)
    except MissingVoiceReuseBindingError as error:
        raise MissingVoiceLiveFallbackError(str(error)) from error
    bundle = _read_json(bundle_path, "missing-voice binding bundle")
    snapshots = []
    for record in bundle.get("inventory", []):
        path = directory / record["path"]
        snapshots.append((path, record["sha256"]))
    snapshots.append((bundle_path, sha256_file(bundle_path)))
    return {
        "decision": decision,
        "decision_sha256": sha256_file(decision_path),
        "bundle": bundle,
        "bundle_sha256": sha256_file(bundle_path),
        "plan": plan,
        "snapshots": tuple(snapshots),
    }


def _validated_targets(plan, binding, character, queue_by_id):
    if (
        plan.get("target_mode") is not None
        or binding.get("mode") != "approved_cohort_reuse"
        or binding.get("queue_voice_overrides") != {}
        or binding.get("selected_candidates") != []
        or binding.get("plan_id") != plan["plan_id"]
    ):
        raise MissingVoiceLiveFallbackError(
            "Missing-voice fallback requires an unresolved missing-role authority"
        )
    decision_by_cohort = {}
    authority_ids = []
    for decision in binding.get("decisions", []):
        if (
            not isinstance(decision, dict)
            or decision.get("decision") != "neither"
            or decision.get("review_decision_origin") != AUTOMATIC_UNRESOLVED_ORIGIN
        ):
            raise MissingVoiceLiveFallbackError(
                "Missing-voice fallback authority is not automatically unresolved"
            )
        cohort_id = decision.get("cohort_id")
        if cohort_id in decision_by_cohort:
            raise MissingVoiceLiveFallbackError(
                "Missing-voice fallback authority cohort is duplicated"
            )
        queue_ids = decision.get("queue_ids")
        if queue_ids != sorted(set(queue_ids or [])) or not queue_ids:
            raise MissingVoiceLiveFallbackError(
                "Missing-voice fallback authority scope is malformed"
            )
        decision_by_cohort[cohort_id] = set(queue_ids)
        authority_ids.extend(queue_ids)
    targets = sorted(plan["targets"], key=lambda value: value["queue_id"])
    target_ids = [target["queue_id"] for target in targets]
    if (
        len(authority_ids) != len(set(authority_ids))
        or sorted(authority_ids) != target_ids
    ):
        raise MissingVoiceLiveFallbackError(
            "Missing-voice fallback authority does not cover the exact plan scope"
        )
    for target in targets:
        queue_id = target["queue_id"]
        queue_item = queue_by_id.get(queue_id)
        if (
            target.get("state") != "absent"
            or target.get("voice_binding_status") != "missing"
            or target.get("declared_voice_character") != character
            or queue_id not in decision_by_cohort.get(target["cohort_id"], set())
            or queue_item is None
            or queue_item.line_id != target["line_id"]
            or queue_item.text_sha256 != target["text_sha256"]
            or queue_item.speaker != target["speaker"]
            or synthesis_character_for_line(
                queue_item.speaker, queue_item.voice_character
            )
            != character
        ):
            raise MissingVoiceLiveFallbackError(
                f"Missing-voice fallback target is stale or has the wrong role: {queue_id!r}"
            )
    return targets


def _existing_batch_id(existing, targets, authority, character, narrator_character):
    present = [value is not None for value in existing]
    if not any(present):
        return None
    if not all(present):
        raise MissingVoiceLiveFallbackError(
            "Missing-voice fallback target scope is partially applied"
        )
    batch_ids = set()
    for item, target in zip(existing, targets, strict=True):
        decision = item.get("live_fallback") if isinstance(item, dict) else None
        evidence = decision.get("evidence") if isinstance(decision, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(decision, dict)
            or not isinstance(evidence, dict)
            or item.get("status") != "live_fallback"
            or item.get("review_status") != "live_fallback"
            or decision.get("schema_version")
            != LIVE_FALLBACK_MISSING_VOICE_EVIDENCE_VERSION
            or decision.get("queue_id") != target["queue_id"]
            or decision.get("requested_voice_character") != character
            or evidence.get("authority_bundle_id") != authority["bundle"]["bundle_id"]
            or evidence.get("authority_bundle_sha256") != authority["bundle_sha256"]
            or evidence.get("authority_decision_id")
            != authority["decision"]["decision_id"]
            or evidence.get("authority_decision_sha256") != authority["decision_sha256"]
            or evidence.get("plan_id") != authority["plan"]["plan_id"]
            or evidence.get("source_workspace_id")
            != authority["plan"]["source"]["workspace_id"]
            or evidence.get("source_workspace_sha256")
            != authority["plan"]["source"]["workspace_sha256"]
            or evidence.get("cohort_id") != target["cohort_id"]
            or evidence.get("decision_origin") != AUTOMATIC_UNRESOLVED_ORIGIN
            or evidence.get("requested_voice_character") != character
            or evidence.get("configured_narrator_character") != narrator_character
        ):
            raise MissingVoiceLiveFallbackError(
                "Missing-voice fallback target has conflicting terminal authority"
            )
        batch_ids.add(evidence.get("batch_id"))
    if len(batch_ids) != 1:
        raise MissingVoiceLiveFallbackError(
            "Missing-voice fallback target scope contains multiple batches"
        )
    return next(iter(batch_ids))


def _decode_state(payload):
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MissingVoiceLiveFallbackError(str(error)) from error
    if not isinstance(value, dict):
        raise MissingVoiceLiveFallbackError("Generation state must be a JSON object")
    return value


def _read_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MissingVoiceLiveFallbackError(
            f"Unable to read {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise MissingVoiceLiveFallbackError(f"{label.capitalize()} must be an object")
    return value


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise MissingVoiceLiveFallbackError(f"{label} must be non-empty text")
    return value.strip()


__all__ = [
    "MissingVoiceLiveFallbackError",
    "MissingVoiceLiveFallbackResult",
    "authorize_missing_voice_live_fallback",
]
