"""Import blind failed-control prompt choices without approving or binding speech."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    canonical_document_sha256,
    write_json_document_no_replace,
)
from vntts.authoring.failure_repair import INLINE_PAUSE_MARKER
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

FAILED_PROMPT_SELECTION_SCHEMA = "vntts.authoring-failed-prompt-selection"
FAILED_PROMPT_SELECTION_VERSION = 1
JsonObject = dict[str, object]


class FailedPromptHypothesisError(RuntimeError):
    """A prompt-hypothesis selection is incomplete or changed."""


@dataclass(frozen=True)
class FailedPromptHypothesisResult:
    output: Path
    selection_id: str
    selected_count: int
    unresolved_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "output": str(self.output),
            "selection_id": self.selection_id,
            "selected_count": self.selected_count,
            "unresolved_count": self.unresolved_count,
        }


def publish_failed_prompt_hypothesis_selection(
    plan_path: str | Path,
    session_path: str | Path,
    output: str | Path,
) -> FailedPromptHypothesisResult:
    """Publish selection authority only; never mutate a manifest or audio state."""
    plan_path = Path(plan_path).expanduser().resolve()
    session_path = Path(session_path).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    try:
        plan = load_missing_voice_reuse_plan(plan_path)
        document = _validate_plan(plan)
        _require_fresh_plan(document)
        bundle, session = load_missing_voice_reuse_review(session_path)
    except (MissingVoiceReuseError, MissingVoiceReuseReviewError) as error:
        raise FailedPromptHypothesisError(str(error)) from error
    if document.get("candidate_mode") != INLINE_PAUSE_MARKER:
        raise FailedPromptHypothesisError(
            "Selection requires an inline-pause failed-control plan"
        )
    if bundle.get("plan", {}).get("plan_id") != document["plan_id"] or bundle[
        "plan"
    ].get("sha256") != sha256_file(plan_path):
        raise FailedPromptHypothesisError(
            "Prompt review belongs to a different immutable plan"
        )
    if any(decision.get("decision") is None for decision in session["decisions"]):
        raise FailedPromptHypothesisError(
            "Every failed-prompt cohort requires a completed decision"
        )
    private_by_label = _load_private_candidates(
        session_path.with_name(".blind-key.json")
    )
    planned_by_id = {
        _record_text(candidate, "candidate_id", "Prompt candidate ID"): candidate
        for candidate in document["candidates"]
    }
    cohort_by_id = {cohort["cohort_id"]: cohort for cohort in bundle["cohorts"]}
    targets_by_cohort: dict[str, list[dict[str, object]]] = {}
    for target in document["targets"]:
        cohort_id = _record_text(target, "cohort_id", "Prompt cohort ID")
        targets_by_cohort.setdefault(cohort_id, []).append(target)
    decisions = []
    for record in sorted(session["decisions"], key=_cohort_id):
        cohort_id = _cohort_id(record)
        cohort = cohort_by_id.get(cohort_id)
        targets = sorted(
            targets_by_cohort.get(cohort_id, []),
            key=lambda value: _record_text(value, "queue_id", "Prompt queue ID"),
        )
        if cohort is None or not targets:
            raise FailedPromptHypothesisError(
                "Reviewed prompt cohort is absent from the plan"
            )
        decision = record["decision"]
        if decision == "neither":
            decisions.append(
                {
                    "cohort_id": cohort_id,
                    "decision": "keep_unresolved",
                    "review_decision_origin": record.get(
                        "decision_origin", "human_review"
                    ),
                    "queue_ids": [target["queue_id"] for target in targets],
                    "source_state_item_sha256s": {
                        target["queue_id"]: target["source_state_item_sha256"]
                        for target in targets
                    },
                }
            )
            continue
        if decision not in cohort["complete_candidate_labels"]:
            raise FailedPromptHypothesisError(
                "Prompt review selected an incomplete candidate"
            )
        candidate = _selected_candidate(private_by_label, planned_by_id, decision)
        decisions.append(
            {
                "cohort_id": cohort_id,
                "decision": "select_hypothesis",
                "queue_ids": [target["queue_id"] for target in targets],
                "source_state_item_sha256s": {
                    target["queue_id"]: target["source_state_item_sha256"]
                    for target in targets
                },
                "candidate_id": candidate["candidate_id"],
                "voice_character": candidate["voice_character"],
                "review_decision_origin": record.get("decision_origin", "human_review"),
                "reference_sha256s": _reference_sha256s(candidate),
                "render_hypothesis": copy.deepcopy(candidate["render_hypothesis"]),
            }
        )
    body = {
        "schema": FAILED_PROMPT_SELECTION_SCHEMA,
        "schema_version": FAILED_PROMPT_SELECTION_VERSION,
        "plan_id": document["plan_id"],
        "plan_sha256": sha256_file(plan_path),
        "review_bundle_id": bundle["bundle_id"],
        "review_bundle_sha256": session["bundle_sha256"],
        "review_session_sha256": sha256_file(session_path),
        "blind_key_sha256": bundle["blind_key_sha256"],
        "decisions": decisions,
        "authority": (
            "Exact render-hypothesis selection only. Candidate choices require "
            "human review; cohorts with no selectable candidate are deterministically "
            "unresolved. This artifact does not bind a voice, mutate generation "
            "state, or approve speech."
        ),
    }
    selection = {**body, "selection_id": canonical_document_sha256(body)}
    try:
        write_json_document_no_replace(
            output, selection, "failed prompt hypothesis selection"
        )
    except AuthoringAuthorityError as error:
        raise FailedPromptHypothesisError(str(error)) from error
    return FailedPromptHypothesisResult(
        output,
        selection["selection_id"],
        sum(value["decision"] == "select_hypothesis" for value in decisions),
        sum(value["decision"] == "keep_unresolved" for value in decisions),
    )


def _cohort_id(record: dict[str, object]) -> str:
    return _record_text(record, "cohort_id", "Prompt review cohort ID")


def _record_text(record: dict[str, object], field: str, label: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise FailedPromptHypothesisError(f"{label} is invalid")
    return value


def _load_private_candidates(path: Path) -> dict[object, object]:
    try:
        key = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FailedPromptHypothesisError(str(error)) from error
    if not isinstance(key, dict):
        raise FailedPromptHypothesisError("Prompt review key must be an object")
    candidates = key.get("candidates")
    if not isinstance(candidates, list):
        raise FailedPromptHypothesisError("Prompt review candidates must be a list")
    result: dict[object, object] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or "label" not in candidate:
            raise FailedPromptHypothesisError("Prompt review candidate is invalid")
        result[candidate["label"]] = candidate
    return result


def _selected_candidate(
    private_by_label: dict[object, object],
    planned_by_id: Mapping[str, object],
    decision: object,
) -> JsonObject:
    private = private_by_label.get(decision)
    if not isinstance(private, dict):
        raise FailedPromptHypothesisError(
            "Prompt candidate identity differs from the immutable plan"
        )
    candidate_id = private.get("candidate_id")
    candidate = (
        planned_by_id.get(candidate_id) if isinstance(candidate_id, str) else None
    )
    if (
        not isinstance(candidate, dict)
        or private.get("voice_character") != candidate.get("voice_character")
        or private.get("ordered_references") != candidate.get("ordered_references")
        or private.get("render_hypothesis") != candidate.get("render_hypothesis")
    ):
        raise FailedPromptHypothesisError(
            "Prompt candidate identity differs from the immutable plan"
        )
    return candidate


def _reference_sha256s(candidate: JsonObject) -> list[str]:
    references = candidate.get("ordered_references")
    if not isinstance(references, list):
        raise FailedPromptHypothesisError("Prompt candidate references are invalid")
    checksums: list[str] = []
    for reference in references:
        if not isinstance(reference, dict):
            raise FailedPromptHypothesisError("Prompt candidate reference is invalid")
        checksum = reference.get("sha256")
        if not isinstance(checksum, str):
            raise FailedPromptHypothesisError(
                "Prompt candidate reference checksum is invalid"
            )
        checksums.append(checksum)
    return checksums


__all__ = [
    "FAILED_PROMPT_SELECTION_SCHEMA",
    "FAILED_PROMPT_SELECTION_VERSION",
    "FailedPromptHypothesisError",
    "FailedPromptHypothesisResult",
    "publish_failed_prompt_hypothesis_selection",
]
