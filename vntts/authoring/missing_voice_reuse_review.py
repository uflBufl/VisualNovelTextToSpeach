"""Checksum-bound blind review of bounded missing-voice reuse candidates."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.bulk_generation import _canonical_sha256
from vntts.authoring.missing_voice_reuse import (
    MISSING_VOICE_REUSE_PLAN_SCHEMA,
    MissingVoiceReuseError,
    _validate_plan,
    load_missing_voice_reuse_plan,
)
from vntts.authoring.source_reference_bindings import (
    MISSING_VOICE_REUSE_BINDING_FIELD,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    _load_workspace_snapshot,
    _safe_relative,
    _stable_workspace_state,
    _within,
)

REVIEW_BUNDLE_SCHEMA = "vntts.authoring-missing-voice-reuse-review-bundle"
REVIEW_SESSION_SCHEMA = "vntts.authoring-missing-voice-reuse-review-session"
REVIEW_KEY_SCHEMA = "vntts.authoring-missing-voice-reuse-review-key"
REVIEW_VERSION = 1
AUTOMATIC_UNRESOLVED_ORIGIN = "automatic_no_complete_candidate"


class MissingVoiceReuseReviewError(RuntimeError):
    """Missing-voice reuse review evidence is incomplete or has changed."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def build_missing_voice_reuse_review(
    plan_path,
    evidence_workspaces,
    output_directory,
    *,
    seed=0,
):
    """Publish one immutable blind evidence matrix and resumable session."""
    plan_path = Path(plan_path).expanduser().resolve()
    try:
        plan = load_missing_voice_reuse_plan(plan_path)
    except MissingVoiceReuseError as error:
        raise MissingVoiceReuseReviewError(str(error)) from error
    document = _validate_plan(plan)
    candidates = document["candidates"]
    candidate_ids = [candidate["candidate_id"] for candidate in candidates]
    if not isinstance(evidence_workspaces, dict) or set(evidence_workspaces) != set(
        candidate_ids
    ):
        raise MissingVoiceReuseReviewError(
            "Review evidence must name every planned candidate exactly once"
        )
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise MissingVoiceReuseReviewError("Review seed must be an integer")

    ordered = list(candidate_ids)
    random.Random(seed).shuffle(ordered)
    labels = {
        candidate_id: _opaque_label(index) for index, candidate_id in enumerate(ordered)
    }
    target_by_id = {target["queue_id"]: target for target in document["targets"]}
    sample_by_id = {
        sample["queue_id"]: {
            **sample,
            **{
                key: target_by_id[sample["queue_id"]][key]
                for key in ("line_id", "text", "text_sha256", "portrait")
            },
        }
        for sample in document["comparison_samples"]
    }
    evidence = {}
    private_candidates = []
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        paths = evidence_workspaces[candidate_id]
        if not isinstance(paths, (list, tuple)) or not paths:
            raise MissingVoiceReuseReviewError(
                "Every review candidate requires at least one evidence workspace"
            )
        snapshots = [
            _load_candidate_workspace(document, candidate, path) for path in paths
        ]
        evidence[candidate_id] = _candidate_sample_evidence(
            document, candidate, snapshots
        )
        private_candidates.append(
            {
                "label": labels[candidate_id],
                "candidate_id": candidate_id,
                "voice_character": candidate["voice_character"],
                "speaker": candidate["speaker"],
                "ordered_references": copy.deepcopy(candidate["ordered_references"]),
                "render_hypothesis": copy.deepcopy(candidate.get("render_hypothesis")),
                "workspaces": [snapshot["authority"] for snapshot in snapshots],
            }
        )

    root = Path(output_directory).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise MissingVoiceReuseReviewError(
            f"Missing-voice review directory is not empty: {root}"
        )
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{root.name}-", dir=root.parent))
    try:
        public_candidates = []
        for candidate_id in ordered:
            label = labels[candidate_id]
            samples = []
            for queue_id in document["comparison_sample_queue_ids"]:
                item = evidence[candidate_id][queue_id]
                public = {
                    "queue_id": queue_id,
                    "status": item["status"],
                    "attempt_count": item["attempt_count"],
                }
                if item["status"] == "generated":
                    relative = Path("audio") / label / f"{_queue_digest(queue_id)}.wav"
                    destination = staging / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _link_or_copy(Path(item["audio_path"]), destination)
                    if sha256_file(destination) != item["audio_sha256"]:
                        raise MissingVoiceReuseReviewError(
                            "Review audio changed while publishing"
                        )
                    public.update(
                        {
                            "audio": relative.as_posix(),
                            "audio_sha256": item["audio_sha256"],
                            "quality": item["quality"],
                            "repair_strategy": item["repair_strategy"],
                        }
                    )
                else:
                    public.update(
                        {
                            "failure_kind": item["failure_kind"],
                            "failure_summary": item["failure_summary"],
                        }
                    )
                samples.append(public)
            public_candidates.append(
                {
                    "label": label,
                    "samples": samples,
                    "generated_count": sum(
                        sample["status"] == "generated" for sample in samples
                    ),
                }
            )

        cohorts = []
        for cohort in document["cohorts"]:
            cohort_samples = [
                copy.deepcopy(sample)
                for sample in sample_by_id.values()
                if sample["cohort_id"] == cohort["cohort_id"]
            ]
            cohort_samples.sort(
                key=lambda sample: document["comparison_sample_queue_ids"].index(
                    sample["queue_id"]
                )
            )
            required_ids = {sample["queue_id"] for sample in cohort_samples}
            complete = []
            for public_candidate in public_candidates:
                statuses = {
                    sample["queue_id"]: sample["status"]
                    for sample in public_candidate["samples"]
                }
                if all(statuses[queue_id] == "generated" for queue_id in required_ids):
                    complete.append(public_candidate["label"])
            cohorts.append(
                {
                    "cohort_id": cohort["cohort_id"],
                    "sample_count": len(cohort_samples),
                    "samples": cohort_samples,
                    "complete_candidate_labels": complete,
                    "decision_options": [*complete, "neither"],
                }
            )

        key = {
            "schema": REVIEW_KEY_SCHEMA,
            "schema_version": REVIEW_VERSION,
            "plan_id": document["plan_id"],
            "candidates": private_candidates,
        }
        key_path = staging / ".blind-key.json"
        _write_private_json(key_path, key)
        body = {
            "schema": REVIEW_BUNDLE_SCHEMA,
            "schema_version": REVIEW_VERSION,
            "plan": {
                "path": str(plan_path),
                "sha256": sha256_file(plan_path),
                "schema": MISSING_VOICE_REUSE_PLAN_SCHEMA,
                "plan_id": document["plan_id"],
            },
            "character": document["character"],
            "seed": seed,
            "policy": {
                "candidate_identity": "stable opaque labels",
                "failed_arms": "visible and never selectable",
                "candidate_gate": (
                    "all exact cohort samples generated and all available cohort "
                    "audio heard"
                ),
                "neither_gate": "all available cohort audio heard",
                "decision_scope": "one exact candidate or neither per cohort",
            },
            "blind_key_sha256": sha256_file(key_path),
            "candidate_count": len(public_candidates),
            "candidates": public_candidates,
            "cohort_count": len(cohorts),
            "cohorts": cohorts,
        }
        if document.get("target_mode", "missing") == "failed":
            body.update(
                {
                    "target_mode": "failed",
                    "source_control": [
                        {
                            "queue_id": queue_id,
                            "status": "failed",
                            "failure_category": target_by_id[queue_id][
                                "failure_category"
                            ],
                            "state_item_sha256": target_by_id[queue_id][
                                "source_state_item_sha256"
                            ],
                        }
                        for queue_id in document["comparison_sample_queue_ids"]
                    ],
                }
            )
        bundle_id = _canonical_sha256(body)
        bundle = {**body, "bundle_id": bundle_id}
        atomic_write_json(staging / "bundle.json", bundle, sort_keys=True)
        created_at = _utc_now()
        session = {
            "schema": REVIEW_SESSION_SCHEMA,
            "schema_version": REVIEW_VERSION,
            "bundle_id": bundle_id,
            "bundle_sha256": sha256_file(staging / "bundle.json"),
            "created_at": created_at,
            "updated_at": created_at,
            "heard": [],
            "decisions": [
                (
                    {"cohort_id": cohort["cohort_id"], "decision": None}
                    if cohort["complete_candidate_labels"]
                    else _automatic_unresolved_decision(cohort["cohort_id"], created_at)
                )
                for cohort in cohorts
            ],
        }
        atomic_write_json(staging / "session.json", session, sort_keys=True)
        try:
            os.rename(staging, root)
        except OSError as error:
            if root.exists():
                raise MissingVoiceReuseReviewError(
                    f"Missing-voice review destination already exists: {root}"
                ) from error
            raise
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    load_missing_voice_reuse_review(root / "session.json")
    return root / "session.json"


def load_missing_voice_reuse_review(session_path):
    """Validate and return the immutable bundle plus mutable review session."""
    session_path = Path(session_path).expanduser().resolve()
    root = session_path.parent
    session = _load_json(session_path, "missing-voice review session")
    bundle_path = root / "bundle.json"
    bundle = _load_json(bundle_path, "missing-voice review bundle")
    if (
        session.get("schema") != REVIEW_SESSION_SCHEMA
        or session.get("schema_version") != REVIEW_VERSION
        or bundle.get("schema") != REVIEW_BUNDLE_SCHEMA
        or bundle.get("schema_version") != REVIEW_VERSION
    ):
        raise MissingVoiceReuseReviewError("Missing-voice review schema is unsupported")
    claimed_bundle_id = bundle.get("bundle_id")
    if claimed_bundle_id != _canonical_sha256(
        {key: value for key, value in bundle.items() if key != "bundle_id"}
    ):
        raise MissingVoiceReuseReviewError(
            "Missing-voice review bundle identity changed"
        )
    if session.get("bundle_id") != claimed_bundle_id or session.get(
        "bundle_sha256"
    ) != sha256_file(bundle_path):
        raise MissingVoiceReuseReviewError("Missing-voice review authority changed")
    key_path = root / ".blind-key.json"
    if (
        not key_path.is_file()
        or stat.S_IMODE(key_path.stat().st_mode) != 0o600
        or bundle.get("blind_key_sha256") != sha256_file(key_path)
    ):
        raise MissingVoiceReuseReviewError("Missing-voice blind key changed")
    key = _load_json(key_path, "missing-voice review key")
    if (
        key.get("schema") != REVIEW_KEY_SCHEMA
        or key.get("schema_version") != REVIEW_VERSION
        or key.get("plan_id") != bundle.get("plan", {}).get("plan_id")
    ):
        raise MissingVoiceReuseReviewError("Missing-voice blind key is invalid")
    _validate_public_matrix(root, bundle, key)
    _derive_automatic_unresolved_decisions(session, bundle)
    _validate_review_session(session, bundle)
    return copy.deepcopy(bundle), copy.deepcopy(session)


def record_missing_voice_reuse_heard(session_path, cohort_id, queue_id, label):
    """Record that one exact generated opaque arm has started playback."""
    bundle, session = load_missing_voice_reuse_review(session_path)
    cohort = _cohort(bundle, cohort_id)
    if queue_id not in {sample["queue_id"] for sample in cohort["samples"]}:
        raise MissingVoiceReuseReviewError("Review sample is outside the cohort")
    sample = _public_sample(bundle, label, queue_id)
    if sample["status"] != "generated":
        raise MissingVoiceReuseReviewError("A failed review arm cannot be heard")
    record = {"cohort_id": cohort_id, "queue_id": queue_id, "label": label}
    if record not in session["heard"]:
        session["heard"].append(record)
        session["heard"].sort(
            key=lambda value: (
                value["cohort_id"],
                value["queue_id"],
                value["label"],
            )
        )
        session["updated_at"] = _utc_now()
        atomic_write_json(Path(session_path).resolve(), session, sort_keys=True)
    return session


def record_missing_voice_reuse_decision(session_path, cohort_id, decision):
    """Record one candidate or neither after every available arm was heard."""
    bundle, session = load_missing_voice_reuse_review(session_path)
    cohort = _cohort(bundle, cohort_id)
    if decision not in cohort["decision_options"]:
        raise MissingVoiceReuseReviewError("Review decision is not available")
    record = next(
        value for value in session["decisions"] if value["cohort_id"] == cohort_id
    )
    if record["decision"] is not None:
        raise MissingVoiceReuseReviewError("Review cohort already has a decision")
    required_heard = _available_heard_keys(bundle, cohort)
    observed = {
        (value["queue_id"], value["label"])
        for value in session["heard"]
        if value["cohort_id"] == cohort_id
    }
    if observed != required_heard:
        raise MissingVoiceReuseReviewError(
            "Every available cohort sample must be heard before deciding"
        )
    record["decision"] = decision
    record["decided_at"] = _utc_now()
    session["updated_at"] = _utc_now()
    atomic_write_json(Path(session_path).resolve(), session, sort_keys=True)
    load_missing_voice_reuse_review(session_path)
    return session


def missing_voice_reuse_review_progress(bundle, session):
    completed = sum(value["decision"] is not None for value in session["decisions"])
    return completed, len(bundle["cohorts"])


def parse_missing_voice_reuse_evidence(values):
    """Parse repeated CANDIDATE_ID=WORKSPACE arguments without dropping order."""
    evidence = {}
    for value in values or ():
        if not isinstance(value, str) or "=" not in value:
            raise MissingVoiceReuseReviewError(
                "Candidate evidence must use CANDIDATE_ID=WORKSPACE"
            )
        candidate_id, raw_path = value.split("=", 1)
        candidate_id = candidate_id.strip()
        raw_path = raw_path.strip()
        if not candidate_id or not raw_path:
            raise MissingVoiceReuseReviewError(
                "Candidate evidence must use CANDIDATE_ID=WORKSPACE"
            )
        evidence.setdefault(candidate_id, []).append(Path(raw_path))
    return {key: tuple(paths) for key, paths in evidence.items()}


def _load_candidate_workspace(plan, candidate, workspace_directory):
    try:
        directory, workspace, workspace_sha256 = _load_workspace_snapshot(
            workspace_directory, "missing-voice candidate evidence"
        )
        _queue, state, _state_payload, state_sha256 = _stable_workspace_state(
            directory, workspace, "missing-voice candidate evidence"
        )
    except AuthoringWorkbenchError as error:
        raise MissingVoiceReuseReviewError(str(error)) from error
    manifest_path = directory / "inputs/voice/manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MissingVoiceReuseReviewError(str(error)) from error
    binding = manifest.get(MISSING_VOICE_REUSE_BINDING_FIELD)
    if (
        not isinstance(binding, dict)
        or binding.get("mode") != "comparison_sample_only"
        or binding.get("plan_id") != plan["plan_id"]
        or binding.get("candidate_id") != candidate["candidate_id"]
        or binding.get("candidate_voice_character") != candidate["voice_character"]
        or binding.get("candidate_render_hypothesis")
        != candidate.get("render_hypothesis")
        or set(binding.get("queue_voice_overrides", {}))
        != set(plan["comparison_sample_queue_ids"])
    ):
        raise MissingVoiceReuseReviewError(
            "Evidence workspace does not belong to the planned candidate"
        )
    return {
        "directory": directory,
        "workspace": workspace,
        "state": state,
        "authority": {
            "path": str(directory),
            "workspace_id": workspace["workspace_id"],
            "workspace_sha256": workspace_sha256,
            "state_sha256": state_sha256,
            "voice_manifest_sha256": sha256_file(manifest_path),
        },
    }


def _candidate_sample_evidence(plan, candidate, snapshots):
    evidence = {}
    for queue_id in plan["comparison_sample_queue_ids"]:
        outcomes = []
        for snapshot in snapshots:
            item = snapshot["state"]["items"].get(queue_id)
            if not isinstance(item, dict) or item.get("status") not in {
                "generated",
                "failed",
            }:
                continue
            binding = item.get("source_reference_binding")
            if (
                not isinstance(binding, dict)
                or binding.get("queue_id") != queue_id
                or binding.get("synthesis_voice_character")
                != candidate["voice_character"]
            ):
                raise MissingVoiceReuseReviewError(
                    "Candidate outcome has changed synthesis voice authority"
                )
            _validate_render_hypothesis_outcome(candidate, queue_id, item)
            outcome = {
                "status": item["status"],
                "workspace": snapshot["authority"],
                "attempts": item.get("attempts", 0),
                "item_sha256": _canonical_sha256(item),
            }
            if item["status"] == "generated":
                relative = _safe_relative(item.get("path"), "Candidate WAV path")
                audio = _within(
                    snapshot["directory"] / "generated-audio",
                    relative,
                    "Candidate WAV",
                )
                claimed = item.get("file_sha256")
                if not audio.is_file() or sha256_file(audio) != claimed:
                    raise MissingVoiceReuseReviewError("Candidate WAV changed")
                outcome.update(
                    {
                        "audio_path": str(audio),
                        "audio_sha256": claimed,
                        "quality": copy.deepcopy(item.get("quality")),
                        "repair_strategy": (
                            item.get("failure_repair", {}).get("strategy")
                            if isinstance(item.get("failure_repair"), dict)
                            else None
                        ),
                    }
                )
            else:
                failure = item.get("failure")
                outcome.update(
                    {
                        "failure_kind": (
                            failure.get("kind")
                            if isinstance(failure, dict)
                            else "untyped_failure"
                        ),
                        "failure_summary": str(item.get("last_error") or "Failed"),
                    }
                )
            outcomes.append(outcome)
        generated = [value for value in outcomes if value["status"] == "generated"]
        audio_hashes = {value["audio_sha256"] for value in generated}
        if len(audio_hashes) > 1:
            raise MissingVoiceReuseReviewError(
                "Candidate has conflicting generated WAVs for one exact sample"
            )
        if generated:
            selected = generated[-1]
            evidence[queue_id] = {
                "status": "generated",
                "attempt_count": max(value["attempts"] for value in outcomes),
                "audio_path": selected["audio_path"],
                "audio_sha256": selected["audio_sha256"],
                "quality": selected["quality"],
                "repair_strategy": selected["repair_strategy"],
                "outcomes": outcomes,
            }
        else:
            selected = outcomes[-1] if outcomes else None
            evidence[queue_id] = {
                "status": "failed",
                "attempt_count": max(
                    (value["attempts"] for value in outcomes), default=0
                ),
                "failure_kind": (
                    selected["failure_kind"] if selected else "no_render_outcome"
                ),
                "failure_summary": (
                    selected["failure_summary"]
                    if selected
                    else "No generated WAV was published"
                ),
                "outcomes": outcomes,
            }
    return evidence


def _validate_render_hypothesis_outcome(candidate, queue_id, item):
    hypothesis = candidate.get("render_hypothesis")
    if hypothesis is None:
        return
    prompts = {prompt["queue_id"]: prompt for prompt in hypothesis.get("prompts", [])}
    prompt = prompts.get(queue_id)
    repair = item.get("failure_repair")
    if (
        prompt is None
        or not isinstance(repair, dict)
        or repair.get("strategy") != hypothesis.get("strategy")
        or repair.get("pause_ms") != hypothesis.get("pause_ms")
        or repair.get("marker_count") != prompt.get("marker_count")
        or repair.get("derived_prompt_sha256") != prompt.get("derived_prompt_sha256")
        or item.get("synthesis_text_sha256") != prompt.get("derived_prompt_sha256")
    ):
        raise MissingVoiceReuseReviewError(
            "Candidate outcome changed the exact render hypothesis"
        )


def _validate_public_matrix(root, bundle, key):
    labels = [candidate.get("label") for candidate in bundle.get("candidates", [])]
    private_labels = [candidate.get("label") for candidate in key.get("candidates", [])]
    if (
        bundle.get("candidate_count") != len(labels)
        or not labels
        or len(set(labels)) != len(labels)
        or set(labels) != set(private_labels)
    ):
        raise MissingVoiceReuseReviewError("Missing-voice candidate labels are invalid")
    queue_ids = {
        sample["queue_id"]
        for cohort in bundle.get("cohorts", [])
        for sample in cohort.get("samples", [])
    }
    target_mode = bundle.get("target_mode", "missing")
    if target_mode == "failed":
        controls = bundle.get("source_control")
        if (
            not isinstance(controls, list)
            or [value.get("queue_id") for value in controls]
            != [
                sample["queue_id"]
                for cohort in bundle.get("cohorts", [])
                for sample in cohort.get("samples", [])
            ]
            or any(
                value.get("status") != "failed"
                or not value.get("failure_category")
                or not isinstance(value.get("state_item_sha256"), str)
                or len(value["state_item_sha256"]) != 64
                for value in controls
            )
        ):
            raise MissingVoiceReuseReviewError(
                "Failed-control review authority is invalid"
            )
    elif target_mode != "missing" or "source_control" in bundle:
        raise MissingVoiceReuseReviewError(
            "Missing-voice review target mode is invalid"
        )
    for candidate in bundle["candidates"]:
        samples = candidate.get("samples")
        if (
            not isinstance(samples, list)
            or {sample.get("queue_id") for sample in samples} != queue_ids
        ):
            raise MissingVoiceReuseReviewError(
                "Missing-voice review matrix is incomplete"
            )
        for sample in samples:
            if sample.get("status") == "generated":
                relative = _safe_relative(sample.get("audio"), "Review audio")
                audio = _within(root, relative, "Review audio")
                if not audio.is_file() or sha256_file(audio) != sample.get(
                    "audio_sha256"
                ):
                    raise MissingVoiceReuseReviewError(
                        "Missing-voice review audio changed"
                    )
            elif sample.get("status") != "failed" or not sample.get("failure_kind"):
                raise MissingVoiceReuseReviewError(
                    "Missing-voice review outcome is invalid"
                )
    if bundle.get("cohort_count") != len(bundle.get("cohorts", [])):
        raise MissingVoiceReuseReviewError("Missing-voice review cohort count changed")
    for cohort in bundle["cohorts"]:
        ids = {sample["queue_id"] for sample in cohort["samples"]}
        complete = []
        for candidate in bundle["candidates"]:
            statuses = {
                sample["queue_id"]: sample["status"] for sample in candidate["samples"]
            }
            if all(statuses[queue_id] == "generated" for queue_id in ids):
                complete.append(candidate["label"])
        if cohort.get("complete_candidate_labels") != complete or cohort.get(
            "decision_options"
        ) != [*complete, "neither"]:
            raise MissingVoiceReuseReviewError(
                "Missing-voice review decision gate changed"
            )


def _validate_review_session(session, bundle):
    cohort_ids = [cohort["cohort_id"] for cohort in bundle["cohorts"]]
    decisions = session.get("decisions")
    if (
        not isinstance(decisions, list)
        or [value.get("cohort_id") for value in decisions] != cohort_ids
    ):
        raise MissingVoiceReuseReviewError("Missing-voice review decisions are invalid")
    heard = session.get("heard")
    if not isinstance(heard, list) or len(heard) != len(
        {(v.get("cohort_id"), v.get("queue_id"), v.get("label")) for v in heard}
    ):
        raise MissingVoiceReuseReviewError(
            "Missing-voice review heard ledger is invalid"
        )
    for value in heard:
        cohort = _cohort(bundle, value.get("cohort_id"))
        if value.get("queue_id") not in {
            sample["queue_id"] for sample in cohort["samples"]
        }:
            raise MissingVoiceReuseReviewError("Heard sample is outside its cohort")
        if (
            _public_sample(bundle, value.get("label"), value["queue_id"])["status"]
            != "generated"
        ):
            raise MissingVoiceReuseReviewError("Heard ledger names a failed arm")
    for value in decisions:
        decision = value.get("decision")
        if decision is None:
            if set(value) != {"cohort_id", "decision"}:
                raise MissingVoiceReuseReviewError(
                    "Pending review decision is malformed"
                )
            continue
        cohort = _cohort(bundle, value["cohort_id"])
        if decision not in cohort["decision_options"] or not value.get("decided_at"):
            raise MissingVoiceReuseReviewError("Completed review decision is invalid")
        if value.get("decision_origin") == AUTOMATIC_UNRESOLVED_ORIGIN:
            if (
                set(value) != {"cohort_id", "decision", "decided_at", "decision_origin"}
                or decision != "neither"
                or cohort["complete_candidate_labels"]
            ):
                raise MissingVoiceReuseReviewError(
                    "Automatic unresolved review decision is invalid"
                )
            continue
        observed = {
            (record["queue_id"], record["label"])
            for record in heard
            if record["cohort_id"] == value["cohort_id"]
        }
        if observed != _available_heard_keys(bundle, cohort):
            raise MissingVoiceReuseReviewError("Decision lacks complete heard evidence")


def _automatic_unresolved_decision(cohort_id, decided_at):
    return {
        "cohort_id": cohort_id,
        "decision": "neither",
        "decided_at": decided_at,
        "decision_origin": AUTOMATIC_UNRESOLVED_ORIGIN,
    }


def _derive_automatic_unresolved_decisions(session, bundle):
    """Project legacy pending sessions through the immutable zero-choice rule."""
    decisions = session.get("decisions")
    if not isinstance(decisions, list):
        return
    cohorts = {
        cohort.get("cohort_id"): cohort
        for cohort in bundle.get("cohorts", [])
        if isinstance(cohort, dict)
    }
    for index, value in enumerate(decisions):
        if (
            not isinstance(value, dict)
            or value.get("decision") is not None
            or set(value) != {"cohort_id", "decision"}
        ):
            continue
        cohort = cohorts.get(value.get("cohort_id"))
        if isinstance(cohort, dict) and not cohort.get("complete_candidate_labels"):
            decisions[index] = _automatic_unresolved_decision(
                value["cohort_id"], session.get("created_at")
            )


def _available_heard_keys(bundle, cohort):
    queue_ids = {sample["queue_id"] for sample in cohort["samples"]}
    return {
        (sample["queue_id"], candidate["label"])
        for candidate in bundle["candidates"]
        for sample in candidate["samples"]
        if sample["queue_id"] in queue_ids and sample["status"] == "generated"
    }


def _public_sample(bundle, label, queue_id):
    matches = [
        candidate
        for candidate in bundle["candidates"]
        if candidate.get("label") == label
    ]
    if len(matches) != 1:
        raise MissingVoiceReuseReviewError("Unknown opaque candidate label")
    samples = [
        sample for sample in matches[0]["samples"] if sample["queue_id"] == queue_id
    ]
    if len(samples) != 1:
        raise MissingVoiceReuseReviewError("Unknown candidate sample")
    return samples[0]


def _cohort(bundle, cohort_id):
    matches = [
        cohort for cohort in bundle["cohorts"] if cohort.get("cohort_id") == cohort_id
    ]
    if len(matches) != 1:
        raise MissingVoiceReuseReviewError("Unknown missing-voice review cohort")
    return matches[0]


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MissingVoiceReuseReviewError(
            f"Unable to load {label}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise MissingVoiceReuseReviewError(f"{label.capitalize()} must be an object")
    return value


def _write_private_json(path, value):
    atomic_write_json(path, value, sort_keys=True)
    os.chmod(path, 0o600)


def _link_or_copy(source, destination):
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _opaque_label(index):
    value = index
    label = ""
    while True:
        label = chr(ord("A") + value % 26) + label
        value = value // 26 - 1
        if value < 0:
            return label


def _queue_digest(queue_id):
    return hashlib.sha256(queue_id.encode("utf-8")).hexdigest()[:24]


__all__ = [
    "AUTOMATIC_UNRESOLVED_ORIGIN",
    "MissingVoiceReuseReviewError",
    "build_missing_voice_reuse_review",
    "load_missing_voice_reuse_review",
    "missing_voice_reuse_review_progress",
    "parse_missing_voice_reuse_evidence",
    "record_missing_voice_reuse_decision",
    "record_missing_voice_reuse_heard",
]
