"""Resumable reason labels for legacy cohort decisions already marked bad."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from vntts_artifacts.atomic_io import atomic_write_json

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.cohort_review import (
    COHORT_REVIEW_DEFECT_REASONS,
    CohortReviewError,
    JsonObject,
    build_cohort_review_decision,
    load_cohort_review_decision,
    load_cohort_review_plan,
    write_cohort_review_decision,
)
from vntts.authoring.robustness_corpus import (
    SpeechRobustnessCorpusError,
    load_speech_robustness_corpus,
)
from vntts.authoring.workbench import AuthoringWorkbenchError, load_workspace_authority

PROGRESS_SCHEMA = "vntts.legacy-reason-review-progress"
PROGRESS_VERSION = 1
_UNCLASSIFIED_REASONS: tuple[frozenset[str], frozenset[str]] = (
    frozenset(),
    frozenset({"unspecified"}),
)
_ALLOWED_REASONS: frozenset[str] = frozenset(COHORT_REVIEW_DEFECT_REASONS) - {
    "unspecified"
}
PathInput: TypeAlias = str | Path
ReasonSelection: TypeAlias = tuple[str, ...]
ReasonSelections: TypeAlias = dict[str, ReasonSelection]


class LegacyReasonReviewError(RuntimeError):
    """Legacy bad-WAV reason evidence is unavailable or inconsistent."""


@dataclass(frozen=True)
class LegacyReasonReviewItem:
    item_id: str
    workspace_id: str
    queue_id: str
    line_id: str
    speaker: str
    text: str
    audio_sha256: str
    audio: Path
    decision_ids: tuple[str, ...]


@dataclass(frozen=True)
class LegacyReasonReview:
    corpus_id: str
    corpus_directory: Path
    decision_root: Path
    items: tuple[LegacyReasonReviewItem, ...]
    known_reasons: dict[tuple[str, str, str], tuple[str, ...]]


def build_legacy_reason_review(
    corpus_directory: PathInput, decision_root: PathInput
) -> LegacyReasonReview:
    """Load only bad corpus samples that predate explicit defect reasons."""
    try:
        corpus = load_speech_robustness_corpus(corpus_directory)
    except SpeechRobustnessCorpusError as error:
        raise LegacyReasonReviewError(str(error)) from error
    root = Path(decision_root).expanduser().resolve()
    if not root.is_dir():
        raise LegacyReasonReviewError(f"Cohort decision root is unavailable: {root}")
    known: dict[tuple[str, str, str], tuple[str, ...]] = {}
    items: list[LegacyReasonReviewItem] = []
    for sample in _object_list(
        corpus.document.get("samples"), "Robustness corpus samples"
    ):
        if sample["human_label"] != "bad":
            continue
        key = (
            _required_text(sample.get("workspace_id"), "Sample workspace ID"),
            _required_text(sample.get("queue_id"), "Sample queue ID"),
            _required_text(sample.get("audio_sha256"), "Sample audio SHA-256"),
        )
        sample_reasons = _string_values(
            sample.get("human_defect_reasons", ()), "Sample defect reasons"
        )
        reasons = tuple(sorted(set(sample_reasons) - {"unspecified"}))
        known[key] = reasons
        if frozenset(sample_reasons) not in _UNCLASSIFIED_REASONS:
            continue
        identity = {
            "workspace_id": key[0],
            "queue_id": key[1],
            "audio_sha256": key[2],
        }
        audio_value = _required_text(sample.get("audio"), "Sample audio path")
        audio = (corpus.directory / audio_value).resolve()
        try:
            audio.relative_to(corpus.directory)
        except ValueError as error:
            raise LegacyReasonReviewError(
                f"Corpus audio leaves its directory: {audio_value!r}"
            ) from error
        items.append(
            LegacyReasonReviewItem(
                canonical_document_sha256(identity),
                key[0],
                key[1],
                _required_text(sample.get("line_id"), "Sample line ID"),
                _required_text(sample.get("speaker"), "Sample speaker"),
                _required_text(sample.get("text"), "Sample text"),
                key[2],
                audio,
                tuple(
                    _string_values(sample.get("decision_ids"), "Sample decision IDs")
                ),
            )
        )
    return LegacyReasonReview(
        corpus.corpus_id,
        corpus.directory,
        root,
        tuple(sorted(items, key=lambda item: (item.speaker, item.line_id))),
        known,
    )


def load_reason_review_progress(
    review: LegacyReasonReview, path: PathInput
) -> ReasonSelections:
    path = Path(path).expanduser()
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LegacyReasonReviewError(
            f"Reason-review progress is invalid: {error}"
        ) from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != PROGRESS_SCHEMA
        or document.get("schema_version") != PROGRESS_VERSION
        or document.get("corpus_id") != review.corpus_id
        or not isinstance(document.get("items"), list)
    ):
        raise LegacyReasonReviewError(
            "Reason-review progress belongs to different corpus evidence"
        )
    expected = {item.item_id: item for item in review.items}
    selections = {}
    for row in document["items"]:
        if not isinstance(row, dict) or set(row) != {
            "item_id",
            "audio_sha256",
            "defect_reasons",
        }:
            raise LegacyReasonReviewError("Reason-review progress item is malformed")
        item = expected.get(row["item_id"])
        reasons = row["defect_reasons"]
        if item is None or row["audio_sha256"] != item.audio_sha256:
            raise LegacyReasonReviewError(
                "Reason-review progress references changed audio evidence"
            )
        selections[item.item_id] = _validated_reasons(reasons)
    return selections


def write_reason_review_progress(
    review: LegacyReasonReview, path: PathInput, selections: object
) -> dict[str, object]:
    selections = _validated_selections(review, selections, complete=False)
    document = {
        "schema": PROGRESS_SCHEMA,
        "schema_version": PROGRESS_VERSION,
        "corpus_id": review.corpus_id,
        "items": [
            {
                "item_id": item.item_id,
                "audio_sha256": item.audio_sha256,
                "defect_reasons": list(selections[item.item_id]),
            }
            for item in review.items
            if item.item_id in selections
        ],
    }
    atomic_write_json(Path(path).expanduser(), document)
    return document


def publish_reason_review_decisions(
    review: LegacyReasonReview, selections: object
) -> tuple[Path, ...]:
    """Write additive v4 reassessments; never rewrite or reapply old review state."""
    selections = _validated_selections(review, selections, complete=True)
    reasons_by_key = dict(review.known_reasons)
    labels_by_key = {}
    for item in review.items:
        key = (item.workspace_id, item.queue_id, item.audio_sha256)
        reasons_by_key[key] = selections[item.item_id]
        labels_by_key[key] = "bad" if selections[item.item_id] else "acceptable"
    source_paths = _source_decision_paths(review)
    selected_decision_ids = {
        decision_id
        for item in review.items
        for decision_id in item.decision_ids
        if decision_id in source_paths
    }
    if not selected_decision_ids:
        raise LegacyReasonReviewError(
            "No original cohort decisions were found below the selected root"
        )
    published: list[Path] = []
    covered: set[tuple[str, str, str]] = set()
    for decision_id in sorted(selected_decision_ids):
        path = source_paths[decision_id]
        decision = load_cohort_review_decision(path).document
        workspace_id = _decision_workspace_id(path)
        reviewed_rows = _object_list(
            decision.get("reviewed_samples"), "Reviewed samples"
        )
        reviewed = {
            _required_text(row.get("queue_id"), "Reviewed sample queue ID"): row
            for row in reviewed_rows
        }
        assessments: dict[str, JsonObject] = {}
        for row in _object_list(
            decision.get("sample_assessments", []), "Sample assessments"
        ):
            queue_id = _required_text(row.get("queue_id"), "Assessment queue ID")
            assessment = _required_text(row.get("assessment"), "Assessment")
            reasons = tuple(
                _string_values(row.get("defect_reasons", ()), "Defect reasons")
            )
            if assessment == "bad" and frozenset(reasons) in _UNCLASSIFIED_REASONS:
                evidence = reviewed[queue_id]
                key = (
                    workspace_id,
                    queue_id,
                    _required_text(
                        evidence.get("audio_sha256"), "Reviewed audio SHA-256"
                    ),
                )
                if key not in labels_by_key:
                    raise LegacyReasonReviewError(
                        f"No current assessment was supplied for {queue_id!r}"
                    )
                assessment = labels_by_key[key]
                reasons = reasons_by_key[key]
                covered.add(key)
            assessments[queue_id] = {
                "assessment": assessment,
                "defect_reasons": list(reasons),
            }
        plan_id = _required_text(decision.get("plan_id"), "Plan ID")
        plan_path = path.parent / f"plan-{plan_id}.json"
        try:
            supplement = build_cohort_review_decision(
                load_cohort_review_plan(plan_path),
                _required_text(decision.get("cohort_id"), "Cohort ID"),
                _required_text(decision.get("decision"), "Decision"),
                reviewed_queue_ids=[
                    _required_text(row.get("queue_id"), "Reviewed sample queue ID")
                    for row in reviewed_rows
                ],
                sample_assessments=assessments,
                next_clean_samples_per_bucket=_optional_integer(
                    decision.get("next_clean_samples_per_bucket"),
                    "Next clean samples per bucket",
                ),
            )
            destination = path.parent / f"decision-{supplement.decision_id}.json"
            if destination.is_file():
                if load_cohort_review_decision(destination) != supplement:
                    raise CohortReviewError(
                        f"Reason-review decision conflicts: {destination}"
                    )
            else:
                write_cohort_review_decision(supplement, destination)
        except (OSError, CohortReviewError) as error:
            raise LegacyReasonReviewError(
                f"Unable to publish reason labels for {decision_id}: {error}"
            ) from error
        published.append(destination)
    expected = {
        (item.workspace_id, item.queue_id, item.audio_sha256) for item in review.items
    }
    if not expected.issubset(covered):
        missing = sorted(key[1] for key in expected - covered)
        raise LegacyReasonReviewError(
            "Original decisions did not cover every reviewed WAV: " + ", ".join(missing)
        )
    return tuple(published)


def _source_decision_paths(review: LegacyReasonReview) -> dict[str, Path]:
    wanted = {decision_id for item in review.items for decision_id in item.decision_ids}
    found: dict[str, Path] = {}
    for path in review.decision_root.rglob("decision-*.json"):
        decision_id = path.stem.removeprefix("decision-")
        if (
            decision_id in wanted
            and path.parent.name == "cohort-reviews"
            and (path.parent.parent / "workspace.json").is_file()
        ):
            found.setdefault(decision_id, path.resolve())
    missing = sorted(wanted - set(found))
    if missing:
        raise LegacyReasonReviewError(
            f"Original cohort decisions are missing: {', '.join(missing)}"
        )
    return found


def _decision_workspace_id(decision_path: Path) -> str:
    workspace = decision_path.parent.parent
    try:
        _directory, document, _sha256 = load_workspace_authority(workspace)
    except AuthoringWorkbenchError as error:
        raise LegacyReasonReviewError(
            f"Unable to load decision workspace {workspace}: {error}"
        ) from error
    return _required_text(document.get("workspace_id"), "Workspace ID")


def _object_list(value: object, label: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise LegacyReasonReviewError(f"{label} must be a list of objects")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LegacyReasonReviewError(f"{label} must be non-empty text")
    return value


def _string_values(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)) or not all(
        isinstance(item, str) for item in value
    ):
        raise LegacyReasonReviewError(f"{label} must contain text")
    return tuple(value)


def _optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise LegacyReasonReviewError(f"{label} must be an integer")
    return value


def _validated_reasons(values: object) -> ReasonSelection:
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise LegacyReasonReviewError("Defect reasons must be a list")
    reasons = tuple(sorted(set(values)))
    if not set(reasons).issubset(_ALLOWED_REASONS):
        raise LegacyReasonReviewError("Choose only supported defect reasons")
    return reasons


def _validated_selections(
    review: LegacyReasonReview, selections: object, *, complete: bool
) -> ReasonSelections:
    if not isinstance(selections, dict):
        raise LegacyReasonReviewError("Reason-review selections must be an object")
    expected = {item.item_id for item in review.items}
    if not set(selections).issubset(expected):
        raise LegacyReasonReviewError("Reason review references an unknown WAV")
    if complete and set(selections) != expected:
        raise LegacyReasonReviewError("Every legacy bad WAV needs a defect reason")
    return {
        item_id: _validated_reasons(values) for item_id, values in selections.items()
    }


__all__ = [
    "LegacyReasonReview",
    "LegacyReasonReviewError",
    "LegacyReasonReviewItem",
    "build_legacy_reason_review",
    "load_reason_review_progress",
    "publish_reason_review_decisions",
    "write_reason_review_progress",
]
