"""Import completed blind reuse decisions into an exact no-replace manifest."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    write_voice_manifest,
)

from vntts.authoring.bulk_generation import _canonical_sha256
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
    _rename_directory_no_replace,
    _safe_relative,
    _within,
)

MISSING_VOICE_REUSE_DECISION_SCHEMA = "vntts.authoring-missing-voice-reuse-decision"
MISSING_VOICE_REUSE_DECISION_VERSION = 1
MISSING_VOICE_REUSE_BINDING_BUNDLE_SCHEMA = (
    "vntts.authoring-missing-voice-reuse-binding-bundle"
)
MISSING_VOICE_REUSE_BINDING_BUNDLE_VERSION = 1


class MissingVoiceReuseBindingError(RuntimeError):
    """A completed reuse review cannot safely authorize a manifest overlay."""


@dataclass(frozen=True)
class MissingVoiceReuseBindingResult:
    directory: Path
    created: bool
    selected_cohort_count: int
    neither_cohort_count: int
    bound_queue_count: int

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "created": self.created,
            "selected_cohort_count": self.selected_cohort_count,
            "neither_cohort_count": self.neither_cohort_count,
            "bound_queue_count": self.bound_queue_count,
        }


def publish_missing_voice_reuse_binding(plan_path, session_path, output_directory):
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
    if bundle.get("plan", {}).get("plan_id") != document["plan_id"] or bundle[
        "plan"
    ].get("sha256") != sha256_file(plan_path):
        raise MissingVoiceReuseBindingError(
            "Missing-voice review belongs to a different immutable plan"
        )
    if any(value["decision"] is None for value in session["decisions"]):
        raise MissingVoiceReuseBindingError(
            "Every missing-voice cohort requires a completed review decision"
        )
    key_path = session_path.with_name(".blind-key.json")
    try:
        key = json.loads(key_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MissingVoiceReuseBindingError(str(error)) from error
    candidate_by_label = {value["label"]: value for value in key.get("candidates", [])}
    planned_candidate_by_id = {
        value["candidate_id"]: value for value in document["candidates"]
    }
    target_by_cohort = {}
    for target in document["targets"]:
        target_by_cohort.setdefault(target["cohort_id"], []).append(target["queue_id"])

    review_cohort_by_id = {value["cohort_id"]: value for value in bundle["cohorts"]}
    decisions = []
    selected_by_id = {}
    overrides = {}
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
        candidate = planned_candidate_by_id.get(private.get("candidate_id"))
        if (
            candidate is None
            or private.get("voice_character") != candidate["voice_character"]
            or private.get("ordered_references") != candidate["ordered_references"]
        ):
            raise MissingVoiceReuseBindingError(
                "Review candidate identity differs from the immutable plan"
            )
        selected_by_id[candidate["candidate_id"]] = candidate
        decisions.append(
            {
                "cohort_id": cohort_id,
                "decision": "candidate",
                "candidate_id": candidate["candidate_id"],
                "voice_character": candidate["voice_character"],
                "queue_ids": queue_ids,
            }
        )
        overrides.update(
            {queue_id: candidate["voice_character"] for queue_id in queue_ids}
        )

    selected_candidates = [
        {
            "candidate_id": candidate_id,
            "voice_character": selected_by_id[candidate_id]["voice_character"],
            "reference_sha256s": [
                reference["sha256"]
                for reference in selected_by_id[candidate_id]["ordered_references"]
            ],
        }
        for candidate_id in sorted(selected_by_id)
    ]
    source_manifest = (
        Path(document["source"]["workspace"]) / "inputs/voice/manifest.json"
    ).resolve()
    if (
        not source_manifest.is_file()
        or sha256_file(source_manifest) != document["source"]["voice_manifest_sha256"]
    ):
        raise MissingVoiceReuseBindingError(
            "Missing-voice source manifest changed after planning"
        )
    try:
        source_document = json.loads(source_manifest.read_text(encoding="utf-8"))
        _metadata, source_voices = load_voice_manifest(
            source_manifest, allow_legacy=False
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        VoiceManifestError,
    ) as error:
        raise MissingVoiceReuseBindingError(str(error)) from error
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
        "review_session_sha256": sha256_file(session_path),
        "blind_key_sha256": bundle["blind_key_sha256"],
        "cohort_ids": sorted(target_by_cohort),
        "selected_candidates": selected_candidates,
        "decisions": decisions,
        "queue_voice_overrides": dict(sorted(overrides.items())),
        "queue_voice_overrides_sha256": queue_voice_overrides_sha256(overrides),
        "authority": (
            "Human-reviewed exact cohort reuse binding. Neither decisions bind no voice."
        ),
    }
    if document.get("target_mode", "missing") == "failed":
        target_by_id = {target["queue_id"]: target for target in document["targets"]}
        binding.update(
            {
                "target_mode": "failed",
                "source_failed_state_item_sha256s": {
                    queue_id: target_by_id[queue_id]["source_state_item_sha256"]
                    for queue_id in sorted(overrides)
                },
            }
        )
    successor = copy.deepcopy(source_document)
    successor[MISSING_VOICE_REUSE_BINDING_FIELD] = binding

    output = Path(output_directory).expanduser().resolve()
    if output.exists():
        try:
            _validate_binding_bundle(output, document, binding)
        except (AuthoringWorkbenchError, SourceReferenceBindingError) as error:
            raise MissingVoiceReuseBindingError(str(error)) from error
        return _result(output, binding, created=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".missing-voice-binding-", dir=output.parent)
    ).resolve()
    try:
        inventory = []
        source_root = source_manifest.parent.resolve()
        seen = set()
        for voice in source_voices:
            for value in voice.references:
                relative = _safe_relative(value, "Missing-voice binding reference")
                key_name = relative.as_posix()
                if key_name in seen:
                    continue
                seen.add(key_name)
                source = _within(
                    source_root, relative, "Missing-voice binding reference"
                )
                if source.is_symlink() or not source.is_file():
                    raise MissingVoiceReuseBindingError(
                        f"Missing-voice binding reference is unsafe: {value!r}"
                    )
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                digest = sha256_file(source)
                if sha256_file(target) != digest:
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
            "plan_sha256": sha256_file(plan_path),
            "session_path": str(session_path),
            "binding": binding,
        }
        decision = {**decision_body, "decision_id": _canonical_sha256(decision_body)}
        atomic_write_json(staging / "decision.json", decision, sort_keys=True)
        inventory = [
            {"path": "decision.json", "sha256": sha256_file(staging / "decision.json")},
            {"path": "manifest.json", "sha256": sha256_file(manifest_path)},
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
            {**body, "bundle_id": _canonical_sha256(body)},
            sort_keys=True,
        )
        _validate_binding_bundle(staging, document, binding)
        _rename_directory_no_replace(staging, output)
        staging = None
    except (AuthoringWorkbenchError, SourceReferenceBindingError) as error:
        raise MissingVoiceReuseBindingError(str(error)) from error
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return _result(output, binding, created=True)


def _validate_binding_bundle(directory, plan, expected_binding):
    directory = Path(directory).resolve()
    try:
        bundle = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MissingVoiceReuseBindingError(str(error)) from error
    if (
        bundle.get("schema") != MISSING_VOICE_REUSE_BINDING_BUNDLE_SCHEMA
        or bundle.get("schema_version") != MISSING_VOICE_REUSE_BINDING_BUNDLE_VERSION
        or bundle.get("plan_id") != plan["plan_id"]
        or bundle.get("bundle_id")
        != _canonical_sha256(
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
        relative = _safe_relative(item["path"], "Missing-voice binding artifact")
        artifact = _within(directory, relative, "Missing-voice binding artifact")
        if (
            artifact.is_symlink()
            or not artifact.is_file()
            or sha256_file(artifact) != item["sha256"]
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
    try:
        decision = json.loads((directory / "decision.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MissingVoiceReuseBindingError(str(error)) from error
    if (
        decision.get("schema") != MISSING_VOICE_REUSE_DECISION_SCHEMA
        or decision.get("schema_version") != MISSING_VOICE_REUSE_DECISION_VERSION
        or decision.get("binding") != expected_binding
        or decision.get("decision_id")
        != _canonical_sha256(
            {key: value for key, value in decision.items() if key != "decision_id"}
        )
    ):
        raise MissingVoiceReuseBindingError(
            "Missing-voice binding decision identity changed"
        )
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _metadata, voices = load_voice_manifest(manifest_path, allow_legacy=False)
        overrides = queue_voice_overrides_from_manifest(
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
    if overrides != expected_binding["queue_voice_overrides"]:
        raise MissingVoiceReuseBindingError("Missing-voice binding overrides changed")


def _result(directory, binding, *, created):
    selected = sum(value["decision"] == "candidate" for value in binding["decisions"])
    neither = sum(value["decision"] == "neither" for value in binding["decisions"])
    return MissingVoiceReuseBindingResult(
        Path(directory).resolve(),
        created,
        selected,
        neither,
        len(binding["queue_voice_overrides"]),
    )


__all__ = [
    "MISSING_VOICE_REUSE_BINDING_BUNDLE_SCHEMA",
    "MISSING_VOICE_REUSE_BINDING_BUNDLE_VERSION",
    "MISSING_VOICE_REUSE_DECISION_SCHEMA",
    "MISSING_VOICE_REUSE_DECISION_VERSION",
    "MissingVoiceReuseBindingError",
    "MissingVoiceReuseBindingResult",
    "publish_missing_voice_reuse_binding",
]
