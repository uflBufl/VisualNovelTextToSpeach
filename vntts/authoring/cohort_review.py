"""Deterministic checksum-bound review plans for generated speech cohorts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from vntts.authoring.bulk_generation import _canonical_sha256
from vntts.authoring.workbench import (
    _load_workspace,
    inspect_workspace,
    list_review_items,
)

COHORT_REVIEW_PLAN_SCHEMA = "vntts.authoring-cohort-review-plan"
COHORT_REVIEW_PLAN_VERSION = 1
COHORT_REVIEW_POLICY_VERSION = 1
DEFAULT_CLEAN_SAMPLES_PER_BUCKET = 1
MAX_CLEAN_SAMPLES_PER_BUCKET = 5
WORD_PATTERN = re.compile(r"[\w’'-]+", flags=re.UNICODE)


class CohortReviewError(RuntimeError):
    """A generated cohort cannot be represented by one safe review plan."""


@dataclass(frozen=True)
class CohortReviewPlan:
    """One immutable planning document plus its canonical identity."""

    plan_id: str
    document: dict

    def to_dict(self):
        return dict(self.document)


def build_cohort_review_plan(
    workspace_directory,
    *,
    clean_samples_per_bucket=DEFAULT_CLEAN_SAMPLES_PER_BUCKET,
):
    """Build a read-only exact-WAV review plan for current pending outcomes."""
    if (
        not isinstance(clean_samples_per_bucket, int)
        or isinstance(clean_samples_per_bucket, bool)
        or not 1 <= clean_samples_per_bucket <= MAX_CLEAN_SAMPLES_PER_BUCKET
    ):
        raise CohortReviewError(
            "Clean samples per length bucket must be an integer from 1 to "
            f"{MAX_CLEAN_SAMPLES_PER_BUCKET}"
        )
    directory, workspace = _load_workspace(workspace_directory)
    summary = inspect_workspace(directory)
    if summary.state is None:
        raise CohortReviewError("Workspace has no generation state to review")
    try:
        state_payload = summary.state.read_bytes()
        state = json.loads(state_payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(
            f"Unable to read generation state {summary.state}: {error}"
        ) from error
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        raise CohortReviewError("Generation state items must be an object")
    state_sha256 = hashlib.sha256(state_payload).hexdigest()
    projected = list_review_items(directory)
    try:
        final_state_sha256 = hashlib.sha256(summary.state.read_bytes()).hexdigest()
    except OSError as error:
        raise CohortReviewError(
            f"Unable to re-read generation state {summary.state}: {error}"
        ) from error
    if final_state_sha256 != state_sha256:
        raise CohortReviewError(
            "Generation state changed while cohort review was being planned"
        )

    cohorts = {}
    blocked = []
    for item in projected:
        if item.status != "generated" or item.review_status != "pending_review":
            continue
        if item.authority is None or item.authority.state_sha256 != state_sha256:
            raise CohortReviewError(
                "Review authority changed while cohort review was being planned"
            )
        result = state["items"].get(item.queue_id)
        if not isinstance(result, dict):
            raise CohortReviewError(
                f"Generation result disappeared for {item.queue_id!r}"
            )
        try:
            identity = _cohort_identity(workspace, result)
        except CohortReviewError as error:
            blocked.append(
                {
                    "queue_id": item.queue_id,
                    "line_id": item.line_id,
                    "reason": str(error),
                }
            )
            continue
        cohort_id = _canonical_sha256(identity)
        word_count = len(WORD_PATTERN.findall(item.text))
        record = {
            "queue_id": item.queue_id,
            "line_id": item.line_id,
            "text_sha256": result.get("text_sha256"),
            "audio_sha256": item.authority.audio_sha256,
            "word_count": word_count,
            "length_bucket": _length_bucket(word_count),
            "technical_flags": list(item.technical_flags),
        }
        cohort = cohorts.setdefault(
            cohort_id,
            {
                "cohort_id": cohort_id,
                "identity": identity,
                "items": [],
            },
        )
        cohort["items"].append(record)

    planned = []
    for cohort_id in sorted(cohorts):
        cohort = cohorts[cohort_id]
        records = sorted(cohort["items"], key=lambda value: value["queue_id"])
        attention = [value for value in records if value["technical_flags"]]
        clean = [value for value in records if not value["technical_flags"]]
        sampled = {value["queue_id"] for value in attention}
        for bucket in ("short", "medium", "long"):
            eligible = [value for value in clean if value["length_bucket"] == bucket]
            eligible.sort(
                key=lambda value: (
                    hashlib.sha256(
                        f"{cohort_id}\0{value['queue_id']}".encode("utf-8")
                    ).hexdigest(),
                    value["queue_id"],
                )
            )
            sampled.update(
                value["queue_id"] for value in eligible[:clean_samples_per_bucket]
            )
        planned.append(
            {
                "cohort_id": cohort_id,
                "identity": cohort["identity"],
                "item_count": len(records),
                "attention_count": len(attention),
                "sample_queue_ids": sorted(sampled),
                "items": [
                    {**value, "sampled": value["queue_id"] in sampled}
                    for value in records
                ],
            }
        )

    body = {
        "schema": COHORT_REVIEW_PLAN_SCHEMA,
        "schema_version": COHORT_REVIEW_PLAN_VERSION,
        "policy": {
            "schema_version": COHORT_REVIEW_POLICY_VERSION,
            "clean_samples_per_bucket": clean_samples_per_bucket,
            "length_buckets": {
                "short_max_words": 6,
                "medium_max_words": 15,
            },
            "attention_rule": "all technical flags",
        },
        "workspace_id": _required_text(workspace.get("workspace_id"), "Workspace ID"),
        "workspace_config_fingerprint": _required_sha256(
            workspace.get("config_fingerprint"), "Workspace config fingerprint"
        ),
        "queue_sha256": _required_sha256(
            state.get("queue_sha256"), "Generation state queue sha256"
        ),
        "state_sha256": state_sha256,
        "cohort_count": len(planned),
        "pending_item_count": sum(value["item_count"] for value in planned),
        "sample_item_count": sum(len(value["sample_queue_ids"]) for value in planned),
        "blocked_item_count": len(blocked),
        "blocked_items": sorted(blocked, key=lambda value: value["queue_id"]),
        "cohorts": planned,
    }
    plan_id = _canonical_sha256(body)
    document = {**body, "plan_id": plan_id}
    return CohortReviewPlan(plan_id, document)


def _cohort_identity(workspace, result):
    binding = result.get("source_reference_binding")
    if binding is not None:
        if not isinstance(binding, dict):
            raise CohortReviewError("Source-reference binding must be an object")
        if binding.get("schema_version") != 1:
            raise CohortReviewError("Source-reference binding version is unsupported")
        _required_text(
            binding.get("source_voice_character"),
            "Source-reference source voice character",
        )
        _required_text(
            binding.get("synthesis_voice_character"),
            "Source-reference synthesis voice character",
        )
        _required_sha256(
            binding.get("queue_voice_overrides_sha256"),
            "Source-reference queue overrides sha256",
        )
        binding = {
            key: value for key, value in binding.items() if key not in {"queue_id"}
        }
    repair = result.get("failure_repair")
    repair_strategy = repair.get("strategy") if isinstance(repair, dict) else None
    if repair_strategy is not None:
        _required_text(repair_strategy, "Failure-repair strategy")
    text_transform = result.get("text_transform")
    if text_transform is not None:
        _required_text(text_transform, "Text transform")
    return {
        "workspace_config_fingerprint": _required_sha256(
            workspace.get("config_fingerprint"), "Workspace config fingerprint"
        ),
        "provider": _required_text(result.get("provider"), "Generation provider"),
        "model": _required_text(result.get("model"), "Generation model"),
        "generation_profile": _required_text(
            result.get("generation_profile"), "Generation profile"
        ),
        "voice_character": _required_text(
            result.get("voice_character"), "Synthesis voice character"
        ),
        "synthesis_provenance_sha256": _required_sha256(
            result.get("synthesis_provenance_sha256"),
            "Synthesis provenance sha256",
        ),
        "prompt_sha256": _required_sha256(
            result.get("prompt_sha256"), "Synthesis prompt sha256"
        ),
        "prompt_applied": _required_bool(
            result.get("prompt_applied"), "Prompt-applied marker"
        ),
        "seed": _required_integer(result.get("seed"), "Generation seed"),
        "text_transform": text_transform,
        "repair_strategy": repair_strategy,
        "source_reference_binding": binding,
    }


def _length_bucket(word_count):
    if word_count <= 6:
        return "short"
    if word_count <= 15:
        return "medium"
    return "long"


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CohortReviewError(f"{label} must be non-empty text")
    return value


def _required_sha256(value, label):
    value = _required_text(value, label)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise CohortReviewError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _required_integer(value, label):
    if not isinstance(value, int) or isinstance(value, bool):
        raise CohortReviewError(f"{label} must be an integer")
    return value


def _required_bool(value, label):
    if not isinstance(value, bool):
        raise CohortReviewError(f"{label} must be a boolean")
    return value
