"""Import completed blind reuse decisions into an exact no-replace manifest."""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from durable_file import atomic_write_json
from vntts_artifacts.voice_manifest import (
    VoiceManifestEntry,
    VoiceManifestError,
    write_voice_manifest,
)

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    AuthoritySnapshot,
    assert_authority_snapshot,
    canonical_document_sha256,
    capture_authority_file,
)
from vntts.authoring.missing_voice_reuse import (
    MissingVoiceReuseError,
    _require_fresh_plan,
    _validate_plan,
    load_missing_voice_reuse_plan,
)
from vntts.authoring.missing_voice_reuse_review import (
    MissingVoiceReuseReviewError,
    load_missing_voice_reuse_review,
)
from vntts.authoring.publication import (
    no_replace_destination,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.source_reference_bindings import (
    MISSING_VOICE_REUSE_APPROVED_BINDING_VERSION,
    MISSING_VOICE_REUSE_BINDING_FIELD,
    MISSING_VOICE_REUSE_BINDING_SCHEMA,
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    contained_workspace_path,
    safe_workspace_relative_path,
)
from vntts.authoring.workspace_foundation import load_json_object
from vntts.document_identity import file_sha256
from vntts.voices import voice_manifest_entries_at_path

MISSING_VOICE_REUSE_DECISION_SCHEMA = "vntts.authoring-missing-voice-reuse-decision"
MISSING_VOICE_REUSE_DECISION_VERSION = 1
MISSING_VOICE_REUSE_BINDING_BUNDLE_SCHEMA = (
    "vntts.authoring-missing-voice-reuse-binding-bundle"
)
MISSING_VOICE_REUSE_BINDING_BUNDLE_VERSION = 1
JsonObject = dict[str, object]


class MissingVoiceReuseBindingError(RuntimeError):
    """A completed reuse review cannot safely authorize a manifest overlay."""


@dataclass(frozen=True)
class MissingVoiceReuseBindingResult:
    directory: Path
    created: bool
    selected_cohort_count: int
    neither_cohort_count: int
    bound_queue_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "created": self.created,
            "selected_cohort_count": self.selected_cohort_count,
            "neither_cohort_count": self.neither_cohort_count,
            "bound_queue_count": self.bound_queue_count,
        }


_file_sha256 = partial(file_sha256, error_type=MissingVoiceReuseBindingError)


def publish_missing_voice_reuse_binding(
    plan_path: str | Path,
    session_path: str | Path,
    output_directory: str | Path,
) -> MissingVoiceReuseBindingResult:
    """Publish a full-cohort binding overlay from one completed blind review."""
    plan_path = Path(plan_path).expanduser().resolve()
    session_path = Path(session_path).expanduser().resolve()
    try:
        plan = load_missing_voice_reuse_plan(plan_path)
        document = _validate_plan(plan)
        _require_fresh_plan(document)
        bundle, session = load_missing_voice_reuse_review(session_path)
    except (MissingVoiceReuseError, MissingVoiceReuseReviewError) as error:
        raise MissingVoiceReuseBindingError(str(error)) from error
    if document.get("candidate_mode") is not None:
        raise MissingVoiceReuseBindingError(
            "Render hypotheses require a selection artifact, not a voice binding"
        )
    if bundle["plan"].get("plan_id") != document["plan_id"] or bundle["plan"].get(
        "sha256"
    ) != _file_sha256(plan_path):
        raise MissingVoiceReuseBindingError(
            "Missing-voice review belongs to a different immutable plan"
        )
    if any(value["decision"] is None for value in session["decisions"]):
        raise MissingVoiceReuseBindingError(
            "Every missing-voice cohort requires a completed review decision"
        )
    key_path = session_path.with_name(".blind-key.json")
    key = load_json_object(
        key_path,
        "Missing-voice blind key",
        error_type=MissingVoiceReuseBindingError,
        object_label="Missing-voice blind key",
    )
    candidate_by_label = {
        _text_field(value, "label", "Missing-voice blind candidate label"): value
        for value in _object_list(
            key.get("candidates", []), "Missing-voice blind key candidates"
        )
    }
    planned_candidate_by_id = {
        _text_field(value, "candidate_id", "Missing-voice candidate ID"): value
        for value in document["candidates"]
    }
    target_by_cohort: dict[str, list[str]] = {}
    for target in document["targets"]:
        cohort_id = _text_field(target, "cohort_id", "Missing-voice cohort ID")
        target_by_cohort.setdefault(cohort_id, []).append(
            _text_field(target, "queue_id", "Missing-voice queue ID")
        )

    review_cohort_by_id = {value["cohort_id"]: value for value in bundle["cohorts"]}
    decisions = []
    selected_by_id: dict[str, JsonObject] = {}
    overrides: dict[str, str] = {}
    for record in sorted(session["decisions"], key=lambda value: value["cohort_id"]):
        cohort_id = record["cohort_id"]
        queue_ids = sorted(target_by_cohort.get(cohort_id, ()))
        if not queue_ids or cohort_id not in review_cohort_by_id:
            raise MissingVoiceReuseBindingError(
                "Reviewed missing-voice cohort is absent from the plan"
            )
        decision = record["decision"]
        if decision == "neither":
            decisions.append(
                {
                    "cohort_id": cohort_id,
                    "decision": "neither",
                    "review_decision_origin": record.get(
                        "decision_origin", "human_review"
                    ),
                    "queue_ids": queue_ids,
                }
            )
            continue
        review_cohort = review_cohort_by_id[cohort_id]
        if decision not in review_cohort["complete_candidate_labels"]:
            raise MissingVoiceReuseBindingError(
                "Review selected a candidate without complete exact sample evidence"
            )
        private = candidate_by_label.get(decision)
        if not isinstance(private, dict):
            raise MissingVoiceReuseBindingError(
                "Review decision does not resolve through the blind key"
            )
        candidate = planned_candidate_by_id.get(
            _text_field(private, "candidate_id", "Missing-voice candidate ID")
        )
        if (
            candidate is None
            or private.get("voice_character") != candidate["voice_character"]
            or private.get("ordered_references") != candidate["ordered_references"]
        ):
            raise MissingVoiceReuseBindingError(
                "Review candidate identity differs from the immutable plan"
            )
        candidate_id = _text_field(
            candidate, "candidate_id", "Missing-voice candidate ID"
        )
        voice_character = _text_field(
            candidate, "voice_character", "Missing-voice candidate voice"
        )
        selected_by_id[candidate_id] = candidate
        decisions.append(
            {
                "cohort_id": cohort_id,
                "decision": "candidate",
                "candidate_id": candidate_id,
                "voice_character": voice_character,
                "review_decision_origin": record.get("decision_origin", "human_review"),
                "queue_ids": queue_ids,
            }
        )
        overrides.update({queue_id: voice_character for queue_id in queue_ids})

    selected_candidates = [
        {
            "candidate_id": candidate_id,
            "voice_character": _text_field(
                selected_by_id[candidate_id],
                "voice_character",
                "Missing-voice candidate voice",
            ),
            "reference_sha256s": [
                _text_field(reference, "sha256", "Missing-voice reference checksum")
                for reference in _object_list(
                    selected_by_id[candidate_id].get("ordered_references"),
                    "Missing-voice candidate references",
                )
            ],
        }
        for candidate_id in sorted(selected_by_id)
    ]
    source_manifest = (
        Path(document["source"]["workspace"]) / "inputs/voice/manifest.json"
    ).resolve()
    source_snapshot, source_document, source_voices = _capture_binding_source_manifest(
        source_manifest, document["source"]["voice_manifest_sha256"]
    )
    if MISSING_VOICE_REUSE_BINDING_FIELD in source_document:
        raise MissingVoiceReuseBindingError(
            "Source manifest already contains a missing-voice reuse authority"
        )
    binding = {
        "schema": MISSING_VOICE_REUSE_BINDING_SCHEMA,
        "schema_version": MISSING_VOICE_REUSE_APPROVED_BINDING_VERSION,
        "mode": "approved_cohort_reuse",
        "plan_id": document["plan_id"],
        "source_voice_manifest_sha256": document["source"]["voice_manifest_sha256"],
        "source_workspace_id": document["source"]["workspace_id"],
        "source_workspace_sha256": document["source"]["workspace_sha256"],
        "review_bundle_id": bundle["bundle_id"],
        "review_bundle_sha256": session["bundle_sha256"],
        "review_session_sha256": _file_sha256(session_path),
        "blind_key_sha256": bundle["blind_key_sha256"],
        "cohort_ids": sorted(target_by_cohort),
        "selected_candidates": selected_candidates,
        "decisions": decisions,
        "queue_voice_overrides": dict(sorted(overrides.items())),
        "queue_voice_overrides_sha256": queue_voice_overrides_sha256(overrides),
        "authority": (
            "Exact cohort reuse binding. Candidate choices require human review; "
            "cohorts with no selectable candidate are deterministically unresolved. "
            "Neither decisions bind no voice."
        ),
    }
    if document.get("target_mode", "missing") == "failed":
        target_by_id = {
            _text_field(target, "queue_id", "Missing-voice queue ID"): target
            for target in document["targets"]
        }
        binding.update(
            {
                "target_mode": "failed",
                "source_failed_state_item_sha256s": {
                    queue_id: target_by_id[queue_id]["source_state_item_sha256"]
                    for queue_id in sorted(target_by_id)
                },
            }
        )
    successor = copy.deepcopy(source_document)
    successor[MISSING_VOICE_REUSE_BINDING_FIELD] = binding

    output = no_replace_destination(output_directory)
    if output.is_symlink():
        raise MissingVoiceReuseBindingError(
            f"Missing-voice binding output exists: {output}"
        )
    if output.exists():
        try:
            _validate_binding_bundle(output, document, binding)
            assert_authority_snapshot(source_snapshot, "missing-voice source manifest")
        except (
            AuthoringWorkbenchError,
            SourceReferenceBindingError,
            AuthoringAuthorityError,
        ) as error:
            raise MissingVoiceReuseBindingError(str(error)) from error
        return _result(output, binding, created=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with staged_directory(
            output.parent, prefix=".missing-voice-binding-"
        ) as staging:
            inventory = []
            source_root = source_manifest.parent.resolve()
            seen = set()
            for voice in source_voices:
                for value in voice.references:
                    relative = safe_workspace_relative_path(
                        value, "Missing-voice binding reference"
                    )
                    key_name = relative.as_posix()
                    if key_name in seen:
                        continue
                    seen.add(key_name)
                    source_path = contained_workspace_path(
                        source_root, relative, "Missing-voice binding reference"
                    )
                    if source_path.is_symlink() or not source_path.is_file():
                        raise MissingVoiceReuseBindingError(
                            f"Missing-voice binding reference is unsafe: {value!r}"
                        )
                    target_path = staging / relative
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_path, target_path)
                    digest = _file_sha256(source_path)
                    if _file_sha256(target_path) != digest:
                        raise MissingVoiceReuseBindingError(
                            "Missing-voice binding reference changed while copied"
                        )
                    inventory.append({"path": key_name, "sha256": digest})
            manifest_path = staging / "manifest.json"
            write_voice_manifest(manifest_path, successor)
            queue_voice_overrides_from_manifest(
                successor,
                voices=source_voices,
            )
            decision_body = {
                "schema": MISSING_VOICE_REUSE_DECISION_SCHEMA,
                "schema_version": MISSING_VOICE_REUSE_DECISION_VERSION,
                "plan_path": str(plan_path),
                "plan_sha256": _file_sha256(plan_path),
                "session_path": str(session_path),
                "binding": binding,
            }
            decision_artifact = {
                **decision_body,
                "decision_id": canonical_document_sha256(decision_body),
            }
            atomic_write_json(
                staging / "decision.json", decision_artifact, sort_keys=True
            )
            inventory = [
                {
                    "path": "decision.json",
                    "sha256": _file_sha256(staging / "decision.json"),
                },
                {"path": "manifest.json", "sha256": _file_sha256(manifest_path)},
                *sorted(inventory, key=lambda value: value["path"]),
            ]
            body = {
                "schema": MISSING_VOICE_REUSE_BINDING_BUNDLE_SCHEMA,
                "schema_version": MISSING_VOICE_REUSE_BINDING_BUNDLE_VERSION,
                "plan_id": document["plan_id"],
                "review_bundle_id": bundle["bundle_id"],
                "inventory": inventory,
            }
            atomic_write_json(
                staging / "bundle.json",
                {**body, "bundle_id": canonical_document_sha256(body)},
                sort_keys=True,
            )
            _validate_binding_bundle(staging, document, binding)
            assert_authority_snapshot(source_snapshot, "missing-voice source manifest")
            rename_directory_no_replace(staging, output)
    except (
        AuthoringWorkbenchError,
        SourceReferenceBindingError,
        AuthoringAuthorityError,
    ) as error:
        raise MissingVoiceReuseBindingError(str(error)) from error
    return _result(output, binding, created=True)


def _capture_binding_source_manifest(
    source_manifest: Path, expected_sha256: object
) -> tuple[AuthoritySnapshot, JsonObject, tuple[VoiceManifestEntry, ...]]:
    try:
        source_snapshot = capture_authority_file(
            source_manifest, "missing-voice source manifest"
        )
        if source_snapshot.sha256 != expected_sha256:
            raise MissingVoiceReuseBindingError(
                "Missing-voice source manifest changed after planning"
            )
        source_document = source_snapshot.json_document("missing-voice source manifest")
        source_voices = voice_manifest_entries_at_path(
            source_document, source_manifest, allow_legacy=False
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        VoiceManifestError,
        AuthoringAuthorityError,
    ) as error:
        raise MissingVoiceReuseBindingError(str(error)) from error
    return source_snapshot, source_document, source_voices


def _validate_binding_bundle(
    directory: str | Path,
    plan: Mapping[str, object],
    expected_binding: JsonObject,
) -> None:
    directory = Path(directory).resolve()
    bundle = load_json_object(
        directory / "bundle.json",
        "Missing-voice binding bundle",
        error_type=MissingVoiceReuseBindingError,
        object_label="Missing-voice binding bundle",
    )
    if (
        bundle.get("schema") != MISSING_VOICE_REUSE_BINDING_BUNDLE_SCHEMA
        or type(bundle.get("schema_version")) is not int
        or bundle.get("schema_version") != MISSING_VOICE_REUSE_BINDING_BUNDLE_VERSION
        or bundle.get("plan_id") != plan["plan_id"]
        or bundle.get("bundle_id")
        != canonical_document_sha256(
            {key: value for key, value in bundle.items() if key != "bundle_id"}
        )
    ):
        raise MissingVoiceReuseBindingError(
            "Missing-voice binding bundle identity is invalid"
        )
    inventory = bundle.get("inventory")
    if not isinstance(inventory, list) or not inventory:
        raise MissingVoiceReuseBindingError("Missing-voice binding inventory is empty")
    declared = set()
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise MissingVoiceReuseBindingError(
                "Missing-voice binding inventory is malformed"
            )
        relative = safe_workspace_relative_path(
            item["path"], "Missing-voice binding artifact"
        )
        artifact = contained_workspace_path(
            directory, relative, "Missing-voice binding artifact"
        )
        if (
            artifact.is_symlink()
            or not artifact.is_file()
            or _file_sha256(artifact) != item["sha256"]
        ):
            raise MissingVoiceReuseBindingError(
                "Missing-voice binding artifact changed"
            )
        key = relative.as_posix()
        if key in declared:
            raise MissingVoiceReuseBindingError(
                "Missing-voice binding inventory contains duplicate paths"
            )
        declared.add(key)
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path.name != "bundle.json"
    }
    if declared != actual:
        raise MissingVoiceReuseBindingError(
            "Missing-voice binding inventory is incomplete"
        )
    decision = load_json_object(
        directory / "decision.json",
        "Missing-voice binding decision",
        error_type=MissingVoiceReuseBindingError,
        object_label="Missing-voice binding decision",
    )
    if (
        decision.get("schema") != MISSING_VOICE_REUSE_DECISION_SCHEMA
        or type(decision.get("schema_version")) is not int
        or decision.get("schema_version") != MISSING_VOICE_REUSE_DECISION_VERSION
        or decision.get("binding") != expected_binding
        or decision.get("decision_id")
        != canonical_document_sha256(
            {key: value for key, value in decision.items() if key != "decision_id"}
        )
    ):
        raise MissingVoiceReuseBindingError(
            "Missing-voice binding decision identity changed"
        )
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        voices = voice_manifest_entries_at_path(
            manifest, manifest_path, allow_legacy=False
        )
        combined_overrides = queue_voice_overrides_from_manifest(
            manifest,
            voices=voices,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        VoiceManifestError,
        SourceReferenceBindingError,
    ) as error:
        raise MissingVoiceReuseBindingError(str(error)) from error
    if manifest.get(MISSING_VOICE_REUSE_BINDING_FIELD) != expected_binding:
        raise MissingVoiceReuseBindingError("Missing-voice binding manifest changed")
    expected_overrides = _object_field(
        expected_binding,
        "queue_voice_overrides",
        "Missing-voice binding overrides",
    )
    if {
        queue_id: combined_overrides.get(queue_id) for queue_id in expected_overrides
    } != expected_overrides:
        raise MissingVoiceReuseBindingError("Missing-voice binding overrides changed")


def _result(
    directory: str | Path,
    binding: JsonObject,
    *,
    created: bool,
) -> MissingVoiceReuseBindingResult:
    decisions = _object_list(
        binding.get("decisions"), "Missing-voice binding decisions"
    )
    overrides = _object_field(
        binding, "queue_voice_overrides", "Missing-voice binding overrides"
    )
    selected = sum(value.get("decision") == "candidate" for value in decisions)
    neither = sum(value.get("decision") == "neither" for value in decisions)
    return MissingVoiceReuseBindingResult(
        Path(directory).resolve(),
        created,
        selected,
        neither,
        len(overrides),
    )


def _object_field(document: JsonObject, field: str, label: str) -> JsonObject:
    value = document.get(field)
    if not isinstance(value, dict):
        raise MissingVoiceReuseBindingError(f"{label} is invalid")
    return value


def _object_list(value: object, label: str) -> list[JsonObject]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise MissingVoiceReuseBindingError(f"{label} is invalid")
    return value


def _text_field(document: Mapping[str, object], field: str, label: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise MissingVoiceReuseBindingError(f"{label} is invalid")
    return value


__all__ = [
    "MISSING_VOICE_REUSE_BINDING_BUNDLE_SCHEMA",
    "MISSING_VOICE_REUSE_BINDING_BUNDLE_VERSION",
    "MISSING_VOICE_REUSE_DECISION_SCHEMA",
    "MISSING_VOICE_REUSE_DECISION_VERSION",
    "MissingVoiceReuseBindingError",
    "MissingVoiceReuseBindingResult",
    "publish_missing_voice_reuse_binding",
]
