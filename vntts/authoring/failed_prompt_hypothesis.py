"""Import blind failed-control prompt choices without approving or binding speech."""

from __future__ import annotations

import copy
import json
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


class FailedPromptHypothesisError(RuntimeError):
    """A prompt-hypothesis selection is incomplete or changed."""


@dataclass(frozen=True)
class FailedPromptHypothesisResult:
    output: Path
    selection_id: str
    selected_count: int
    unresolved_count: int

    def to_dict(self):
        return {
            "output": str(self.output),
            "selection_id": self.selection_id,
            "selected_count": self.selected_count,
            "unresolved_count": self.unresolved_count,
        }


def publish_failed_prompt_hypothesis_selection(plan_path, session_path, output):
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
    key_path = session_path.with_name(".blind-key.json")
    try:
        key = json.loads(key_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FailedPromptHypothesisError(str(error)) from error
    private_by_label = {
        candidate["label"]: candidate for candidate in key.get("candidates", [])
    }
    planned_by_id = {
        candidate["candidate_id"]: candidate for candidate in document["candidates"]
    }
    cohort_by_id = {cohort["cohort_id"]: cohort for cohort in bundle["cohorts"]}
    targets_by_cohort = {}
    for target in document["targets"]:
        targets_by_cohort.setdefault(target["cohort_id"], []).append(target)
    decisions = []
    for record in sorted(session["decisions"], key=lambda value: value["cohort_id"]):
        cohort_id = record["cohort_id"]
        cohort = cohort_by_id.get(cohort_id)
        targets = sorted(
            targets_by_cohort.get(cohort_id, []), key=lambda value: value["queue_id"]
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
        private = private_by_label.get(decision)
        candidate = planned_by_id.get(
            private.get("candidate_id") if isinstance(private, dict) else None
        )
        if (
            candidate is None
            or private.get("voice_character") != candidate["voice_character"]
            or private.get("ordered_references") != candidate["ordered_references"]
            or private.get("render_hypothesis") != candidate["render_hypothesis"]
        ):
            raise FailedPromptHypothesisError(
                "Prompt candidate identity differs from the immutable plan"
            )
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
                "reference_sha256s": [
                    reference["sha256"] for reference in candidate["ordered_references"]
                ],
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


__all__ = [
    "FAILED_PROMPT_SELECTION_SCHEMA",
    "FAILED_PROMPT_SELECTION_VERSION",
    "FailedPromptHypothesisError",
    "FailedPromptHypothesisResult",
    "publish_failed_prompt_hypothesis_selection",
]
