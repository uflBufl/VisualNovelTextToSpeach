"""Checksum-bound review bundles spanning immutable authoring workspaces."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json

from vntts.authoring.bulk_generation import ReviewAuthority, _canonical_sha256
from vntts.authoring.cohort_review import (
    DEFAULT_CLEAN_SAMPLES_PER_BUCKET,
    CohortReviewError,
    _load_document,
    _validate_decision_against_plan,
    _validated_decision_document,
    _validated_plan_document,
    _write_document_no_replace,
    build_cohort_review_decision,
    build_cohort_review_plan,
    execute_cohort_review_decision,
)
from vntts.authoring.workbench import ReviewItem

COHORT_REVIEW_BUNDLE_SCHEMA = "vntts.authoring-cohort-review-bundle"
COHORT_REVIEW_BUNDLE_VERSION = 2
COHORT_REVIEW_PROGRESS_SCHEMA = "vntts.authoring-cohort-review-progress"
COHORT_REVIEW_PROGRESS_VERSION = 1


@dataclass(frozen=True)
class CohortReviewBundle:
    """One exact multi-workspace review inventory."""

    bundle_id: str
    document: dict

    def to_dict(self):
        return dict(self.document)


@dataclass(frozen=True)
class CohortBundleProjection:
    """One source-local cohort decision projected through a bundle."""

    bundle_id: str
    workspace_id: str
    cohort_id: str
    queue_ids: tuple[str, ...]
    review_status: str | None
    next_bundle: CohortReviewBundle

    def to_dict(self):
        return {
            "bundle_id": self.bundle_id,
            "workspace_id": self.workspace_id,
            "cohort_id": self.cohort_id,
            "queue_ids": list(self.queue_ids),
            "review_status": self.review_status,
            "next_bundle_id": self.next_bundle.bundle_id,
        }


@dataclass(frozen=True)
class CohortBundleSample:
    """One live source-bound sample presented by a review bundle."""

    workspace: Path
    workspace_id: str
    plan_id: str
    cohort_id: str
    required_reason: str
    item: ReviewItem


@dataclass(frozen=True)
class CohortReviewResume:
    """One published task reconciled with its exact current source evidence."""

    publication: Path
    progress: Path
    original: CohortReviewBundle
    current: CohortReviewBundle
    progress_current: bool

    def to_dict(self):
        original = self.original.document
        current = self.current.document
        return {
            "publication": str(self.publication),
            "progress": str(self.progress),
            "root_bundle_id": self.original.bundle_id,
            "current_bundle_id": self.current.bundle_id,
            "progress_current": self.progress_current,
            "original_cohorts": original["cohort_count"],
            "completed_cohorts": (original["cohort_count"] - current["cohort_count"]),
            "remaining_cohorts": current["cohort_count"],
            "original_samples": original["sample_item_count"],
            "remaining_samples": current["sample_item_count"],
            "original_items": original["pending_item_count"],
            "remaining_items": current["pending_item_count"],
        }


@dataclass(frozen=True)
class CohortReviewRecoveredAssessment:
    """One exact sample assessment recovered from expansion evidence."""

    workspace_id: str
    cohort_id: str
    queue_id: str
    assessment: str


def build_cohort_review_bundle(
    workspace_directories,
    *,
    clean_samples_per_bucket=DEFAULT_CLEAN_SAMPLES_PER_BUCKET,
    queue_ids_by_workspace=None,
):
    """Build one deterministic review inventory over distinct workspaces."""
    paths = tuple(Path(value).resolve() for value in workspace_directories)
    if not paths:
        raise CohortReviewError("A review bundle requires at least one workspace")
    if len(set(paths)) != len(paths):
        raise CohortReviewError("Review bundle workspaces must be distinct")
    selections = {
        Path(path).resolve(): queue_ids
        for path, queue_ids in (queue_ids_by_workspace or {}).items()
    }
    unexpected = sorted(set(selections) - set(paths), key=str)
    if unexpected:
        raise CohortReviewError(
            f"Review bundle selections reference unknown workspaces: {unexpected}"
        )

    source_plans = []
    for path in sorted(paths, key=str):
        plan = build_cohort_review_plan(
            path,
            clean_samples_per_bucket=clean_samples_per_bucket,
            queue_ids=selections.get(path),
        )
        source_plans.append((path, plan.document))
    return _assemble_bundle(source_plans)


def _assemble_bundle(source_plans):
    sources = []
    flattened = []
    workspace_ids = set()
    for path, plan in source_plans:
        path = Path(path).resolve()
        document = _validated_plan_document(plan)
        workspace_id = document["workspace_id"]
        if workspace_id in workspace_ids:
            raise CohortReviewError(
                f"Review bundle workspace ID is duplicated: {workspace_id}"
            )
        workspace_ids.add(workspace_id)
        source = {
            "workspace": str(path),
            "workspace_id": workspace_id,
            "plan": document,
        }
        sources.append(source)
        flattened.extend(
            _flatten_validated_sources(
                [
                    (str(path), workspace_id, document["plan_id"], cohort)
                    for cohort in document["cohorts"]
                ]
            )
        )

    body = {
        "schema": COHORT_REVIEW_BUNDLE_SCHEMA,
        "schema_version": COHORT_REVIEW_BUNDLE_VERSION,
        "policy": {
            "attention_rule": "all technical flags",
            "projection_scope": "source workspace only",
            "sample_policy": "retained by each exact source plan",
        },
        "workspace_count": len(sources),
        "cohort_count": len(flattened),
        "pending_item_count": sum(
            source["plan"]["pending_item_count"] for source in sources
        ),
        "sample_item_count": sum(
            source["plan"]["sample_item_count"] for source in sources
        ),
        "blocked_item_count": len(
            {
                item["queue_id"]
                for source in sources
                for item in source["plan"]["blocked_items"]
            }
        ),
        "blocked_source_occurrence_count": sum(
            source["plan"]["blocked_item_count"] for source in sources
        ),
        "sources": sources,
        "cohorts": sorted(
            flattened,
            key=lambda value: (
                value["workspace_id"],
                value["cohort_id"],
            ),
        ),
    }
    bundle_id = _canonical_sha256(body)
    return CohortReviewBundle(bundle_id, {**body, "bundle_id": bundle_id})


def write_cohort_review_bundle(bundle, output_path):
    """Publish a validated bundle without replacing an existing file."""
    document = _validated_bundle_document(bundle)
    return _write_document_no_replace(output_path, document, "cohort review bundle")


def load_cohort_review_bundle(path):
    """Load and validate one exact multi-workspace review bundle."""
    document = _load_document(path, "cohort review bundle")
    return validate_cohort_review_bundle_document(document)


def validate_cohort_review_bundle_document(document):
    """Validate one already captured bundle document without reopening a path."""
    validated = _validated_bundle_document(copy.deepcopy(document))
    return CohortReviewBundle(validated["bundle_id"], validated)


def validate_cohort_review_progress_document(document, original):
    """Validate one already captured progress document against its publication."""
    root = (
        original
        if isinstance(original, CohortReviewBundle)
        else validate_cohort_review_bundle_document(original)
    )
    return _validated_progress_document(copy.deepcopy(document), root)


def cohort_review_progress_path(publication):
    """Return the mutable progress sibling for one immutable publication."""
    path = Path(publication).expanduser().resolve()
    return path.with_name(f"{path.stem}.progress.json")


def reconcile_cohort_review_bundle(bundle):
    """Project exact terminal cohort evidence onto an immutable publication."""
    original = (
        bundle
        if isinstance(bundle, CohortReviewBundle)
        else load_cohort_review_bundle(bundle)
    )
    document = _validated_bundle_document(original)
    next_sources = []
    for source in document["sources"]:
        reconciled = _reconcile_cohort_source(source)
        if reconciled is not None:
            next_sources.append(reconciled)
    return _assemble_bundle(next_sources)


def _reconcile_cohort_source(source):
    workspace = Path(source["workspace"])
    current = _current_source_snapshot(source)
    decisions = _source_cohort_decisions(workspace)
    remaining = []
    clean_samples = source["plan"]["policy"]["clean_samples_per_bucket"]
    for cohort in source["plan"]["cohorts"]:
        outcome = _reconciled_cohort_outcome(cohort, current, decisions)
        if outcome["terminal"]:
            continue
        remaining.append(cohort)
        clean_samples = max(clean_samples, outcome["clean_samples_per_bucket"])
    _assert_current_source_snapshot(current)
    if not remaining:
        return None
    rebuilt = _reconciled_plan_document(
        source,
        current,
        remaining,
        clean_samples_per_bucket=clean_samples,
    )
    _validate_reconciled_source(source, remaining, rebuilt)
    return workspace, rebuilt


def load_resumable_cohort_review_bundle(publication, *, persist=False):
    """Load or recover the current exact successor of a published bundle."""
    path = Path(publication).expanduser().resolve()
    original = load_cohort_review_bundle(path)
    current = reconcile_cohort_review_bundle(original)
    progress = cohort_review_progress_path(path)
    progress_current = False
    if progress.is_symlink():
        raise CohortReviewError("Cohort review progress cannot be a symlink")
    if progress.is_file():
        saved = _load_document(progress, "cohort review progress")
        saved_bundle = validate_cohort_review_progress_document(saved, original)
        progress_current = saved_bundle.bundle_id == current.bundle_id
    if persist and not progress_current:
        write_cohort_review_progress(path, original, current)
        progress_current = True
    return CohortReviewResume(
        path,
        progress,
        original,
        current,
        progress_current,
    )


def load_resumable_cohort_review_bundle_samples(publication, *, persist=True):
    """Recover progress and load the exact remaining operator sample."""
    resume = load_resumable_cohort_review_bundle(publication, persist=persist)
    current, samples = load_cohort_review_bundle_samples(resume.current)
    return resume, current, samples


def load_resumable_cohort_review_session(publication, *, persist=True):
    """Recover remaining samples plus exact prior expansion assessments."""
    resume, current, samples = load_resumable_cohort_review_bundle_samples(
        publication,
        persist=persist,
    )
    assessments = _recovered_expansion_assessments(current)
    return resume, current, samples, assessments


def write_cohort_review_progress(publication, original, current):
    """Atomically checkpoint one source-verified successor bundle."""
    path = Path(publication).expanduser().resolve()
    root = (
        original
        if isinstance(original, CohortReviewBundle)
        else load_cohort_review_bundle(path)
    )
    candidate = CohortReviewBundle(
        _validated_bundle_document(current)["bundle_id"],
        _validated_bundle_document(current),
    )
    expected = reconcile_cohort_review_bundle(root)
    if candidate.bundle_id != expected.bundle_id:
        raise CohortReviewError(
            "Cohort review progress does not match current source evidence"
        )
    progress = cohort_review_progress_path(path)
    if progress.is_symlink():
        raise CohortReviewError("Cohort review progress cannot be a symlink")
    body = {
        "schema": COHORT_REVIEW_PROGRESS_SCHEMA,
        "schema_version": COHORT_REVIEW_PROGRESS_VERSION,
        "root_bundle_id": root.bundle_id,
        "current_bundle": candidate.document,
    }
    document = {**body, "progress_id": _canonical_sha256(body)}
    try:
        atomic_write_json(progress, document, sort_keys=True)
    except OSError as error:
        raise CohortReviewError(
            f"Unable to save cohort review progress {progress}: {error}"
        ) from error
    return progress


def refresh_cohort_review_bundle(bundle):
    """Rebuild every source plan and require the bundle to remain exact."""
    document = _validated_bundle_document(bundle)
    current = _assemble_bundle(
        [
            (
                source["workspace"],
                build_cohort_review_plan(
                    source["workspace"],
                    clean_samples_per_bucket=source["plan"]["policy"][
                        "clean_samples_per_bucket"
                    ],
                    queue_ids=source["plan"]["policy"].get("selected_queue_ids"),
                ).document,
            )
            for source in document["sources"]
        ]
    )
    if current.bundle_id != document["bundle_id"]:
        raise CohortReviewError(
            "Review bundle authority changed after the bundle was published"
        )
    return current


def load_cohort_review_bundle_samples(bundle):
    """Revalidate a bundle and project its exact samples for an operator UI."""
    document = _validated_bundle_document(bundle)
    current = CohortReviewBundle(document["bundle_id"], document)
    samples = []
    for source in document["sources"]:
        source_cohorts = [
            value
            for value in document["cohorts"]
            if value["workspace_id"] == source["workspace_id"]
        ]
        workspace, review_items = _load_source_sample_records(source, source_cohorts)
        flattened = {value["cohort_id"]: value for value in source_cohorts}
        for cohort in source["plan"]["cohorts"]:
            bundle_cohort = flattened[cohort["cohort_id"]]
            for sample in bundle_cohort["samples"]:
                item = review_items.get(sample["queue_id"])
                if (
                    item is None
                    or item.status != "generated"
                    or item.review_status != "pending_review"
                    or item.authority is None
                ):
                    raise CohortReviewError(
                        f"Bundle sample authority is unavailable: {sample['queue_id']}"
                    )
                if (
                    hashlib.sha256(item.text.encode("utf-8")).hexdigest()
                    != sample["text_sha256"]
                    or item.authority.audio_sha256 != sample["audio_sha256"]
                ):
                    raise CohortReviewError(
                        f"Bundle sample text or WAV changed: {sample['queue_id']}"
                    )
                samples.append(
                    CohortBundleSample(
                        workspace=workspace,
                        workspace_id=source["workspace_id"],
                        plan_id=source["plan"]["plan_id"],
                        cohort_id=cohort["cohort_id"],
                        required_reason=sample["required_reason"],
                        item=item,
                    )
                )
    samples.sort(
        key=lambda value: (
            value.workspace_id,
            value.cohort_id,
            value.item.queue_id,
        )
    )
    if len(samples) != current.document["sample_item_count"]:
        raise CohortReviewError("Review bundle live sample count changed")
    return current, tuple(samples)


def _current_source_snapshot(source):
    workspace = Path(source["workspace"])
    configuration_path = workspace / "workspace.json"
    if configuration_path.is_symlink():
        raise CohortReviewError("Bundle workspace configuration cannot be a symlink")
    configuration_payload = _read_bytes(
        configuration_path, "bundle workspace configuration"
    )
    configuration = _decode_json(
        configuration_payload,
        "bundle workspace configuration",
    )
    if (
        not isinstance(configuration, dict)
        or configuration.get("workspace_id") != source["workspace_id"]
        or configuration.get("config_fingerprint")
        != source["plan"]["workspace_config_fingerprint"]
    ):
        raise CohortReviewError("Bundle workspace configuration identity changed")
    queue_path = _contained_source_path(
        workspace, configuration.get("queue"), "bundle queue"
    )
    output = _contained_source_path(
        workspace, configuration.get("output"), "bundle output"
    )
    state_path = output / "generation-state.json"
    if (
        queue_path.is_symlink()
        or state_path.is_symlink()
        or output.is_symlink()
        or not output.is_dir()
    ):
        raise CohortReviewError("Bundle queue, state and output must be safe")
    queue_payload = _read_bytes(queue_path, "bundle queue")
    queue_sha256 = hashlib.sha256(queue_payload).hexdigest()
    if queue_sha256 != source["plan"]["queue_sha256"]:
        raise CohortReviewError("Bundle source queue changed")
    state_payload = _read_bytes(state_path, "bundle state")
    state = _decode_json(state_payload, "bundle state")
    if (
        not isinstance(state, dict)
        or state.get("queue_sha256") != queue_sha256
        or not isinstance(state.get("items"), dict)
    ):
        raise CohortReviewError("Bundle source state identity changed")
    return {
        "output": output,
        "configuration_path": configuration_path,
        "configuration_sha256": hashlib.sha256(configuration_payload).hexdigest(),
        "queue_path": queue_path,
        "queue_sha256": queue_sha256,
        "state_path": state_path,
        "queue": _decode_queue_records(queue_payload),
        "items": state["items"],
        "state_sha256": hashlib.sha256(state_payload).hexdigest(),
        "artifacts": [],
    }


def _source_cohort_decisions(workspace):
    evidence = workspace / "cohort-reviews"
    if not evidence.exists():
        return ()
    if evidence.is_symlink() or not evidence.is_dir():
        raise CohortReviewError("Cohort review evidence directory is unsafe")
    decisions = []
    seen = set()
    for path in sorted(evidence.iterdir(), key=lambda value: value.name):
        if not path.name.startswith("decision-") or path.suffix != ".json":
            continue
        if path.is_symlink() or not path.is_file():
            raise CohortReviewError("Cohort review decision evidence is unsafe")
        decision = _validated_decision_document(
            _load_document(path, "cohort review decision")
        )
        if path.name != f"decision-{decision['decision_id']}.json":
            raise CohortReviewError("Cohort review decision filename is inconsistent")
        if decision["decision_id"] in seen:
            raise CohortReviewError("Cohort review decision identity is duplicated")
        seen.add(decision["decision_id"])
        plan_path = evidence / f"plan-{decision['plan_id']}.json"
        if plan_path.is_symlink() or not plan_path.is_file():
            raise CohortReviewError("Cohort review decision plan is unavailable")
        plan = _validated_plan_document(_load_document(plan_path, "cohort review plan"))
        _validate_decision_against_plan(plan, decision)
        decisions.append(decision)
    return tuple(decisions)


def _recovered_expansion_assessments(bundle):
    document = _validated_bundle_document(bundle)
    recovered = {}
    for source in document["sources"]:
        decisions = _source_cohort_decisions(Path(source["workspace"]))
        clean_samples = source["plan"]["policy"]["clean_samples_per_bucket"]
        for cohort in source["plan"]["cohorts"]:
            target_identity = _cohort_target_identity(cohort["items"])
            expansions = [
                decision
                for decision in decisions
                if decision["decision"] == "expand"
                and decision["cohort_id"] == cohort["cohort_id"]
                and decision["next_clean_samples_per_bucket"] == clean_samples
                and _cohort_target_identity(decision["target_items"]) == target_identity
            ]
            for decision in expansions:
                assessed = {
                    value["queue_id"]: value["assessment"]
                    for value in decision["sample_assessments"]
                }
                for sample in decision["reviewed_samples"]:
                    queue_id = sample["queue_id"]
                    assessment = assessed.get(queue_id, "heard")
                    is_bad = assessment == "bad"
                    key = (source["workspace_id"], cohort["cohort_id"], queue_id)
                    previous = recovered.get(key)
                    if previous is not None and previous != is_bad:
                        raise CohortReviewError(
                            "Expanded cohort evidence has conflicting sample "
                            f"assessments: {queue_id}"
                        )
                    recovered[key] = is_bad
    return tuple(
        CohortReviewRecoveredAssessment(
            workspace_id=workspace_id,
            cohort_id=cohort_id,
            queue_id=queue_id,
            assessment="bad" if is_bad else "heard",
        )
        for (workspace_id, cohort_id, queue_id), is_bad in sorted(recovered.items())
    )


def _reconciled_cohort_outcome(cohort, current, decisions):
    target = cohort["items"]
    target_identity = _cohort_target_identity(target)
    matching = [
        decision
        for decision in decisions
        if decision["cohort_id"] == cohort["cohort_id"]
        and _cohort_target_identity(decision["target_items"]) == target_identity
    ]
    expansions = [value for value in matching if value["decision"] == "expand"]
    terminal = [value for value in matching if value["decision"] != "expand"]
    terminal_statuses = {value["projection_review_status"] for value in terminal}
    if len(terminal_statuses) > 1:
        raise CohortReviewError("Cohort review evidence has conflicting decisions")
    clean_samples = max(
        [
            value["next_clean_samples_per_bucket"]
            for value in expansions
            if value["next_clean_samples_per_bucket"] is not None
        ]
        or [1]
    )
    observed = []
    for item in target:
        queue_id = item["queue_id"]
        queue_item = current["queue"].get(queue_id)
        result = current["items"].get(queue_id)
        if not isinstance(queue_item, dict) or not isinstance(result, dict):
            raise CohortReviewError(f"Bundle cohort item disappeared: {queue_id}")
        text = queue_item.get("text")
        if (
            not isinstance(text, str)
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != item["text_sha256"]
            or result.get("file_sha256") != item["audio_sha256"]
        ):
            raise CohortReviewError(f"Bundle cohort item identity changed: {queue_id}")
        audio = _contained_source_path(
            current["output"], result.get("path"), f"bundle item {queue_id} WAV"
        )
        if (
            not audio.is_file()
            or hashlib.sha256(
                _read_bytes(audio, f"bundle item {queue_id} WAV")
            ).hexdigest()
            != item["audio_sha256"]
        ):
            raise CohortReviewError(f"Bundle cohort WAV changed: {queue_id}")
        current["artifacts"].append((audio, item["audio_sha256"], queue_id))
        observed.append((result.get("status"), result.get("review_status")))
    pending = set(observed) == {("generated", "pending_review")}
    if pending:
        if terminal:
            raise CohortReviewError("Terminal cohort evidence was not fully committed")
        return {
            "terminal": False,
            "clean_samples_per_bucket": clean_samples,
        }
    if not terminal or len(terminal_statuses) != 1:
        raise CohortReviewError("Cohort changed without exact terminal evidence")
    review_status = next(iter(terminal_statuses))
    expected = (
        ("approved", "approved")
        if review_status == "approved"
        else ("generated", "rejected")
    )
    if set(observed) != {expected}:
        raise CohortReviewError("Cohort terminal state is mixed or inconsistent")
    return {"terminal": True, "clean_samples_per_bucket": clean_samples}


def _assert_current_source_snapshot(current):
    if (
        hashlib.sha256(
            _read_bytes(current["configuration_path"], "bundle workspace configuration")
        ).hexdigest()
        != current["configuration_sha256"]
        or hashlib.sha256(
            _read_bytes(current["queue_path"], "bundle queue")
        ).hexdigest()
        != current["queue_sha256"]
        or hashlib.sha256(
            _read_bytes(current["state_path"], "bundle state")
        ).hexdigest()
        != current["state_sha256"]
    ):
        raise CohortReviewError("Bundle source changed during progress recovery")
    for path, expected, queue_id in current["artifacts"]:
        if (
            hashlib.sha256(_read_bytes(path, f"bundle item {queue_id} WAV")).hexdigest()
            != expected
        ):
            raise CohortReviewError(
                f"Bundle cohort WAV changed during recovery: {queue_id}"
            )


def _cohort_target_identity(items):
    return tuple(
        sorted(
            (
                value.get("queue_id"),
                value.get("line_id"),
                value.get("text_sha256"),
                value.get("audio_sha256"),
            )
            for value in items
        )
    )


def _cohort_item_authority(item):
    """Return immutable item authority without mutable sample membership."""
    return {key: value for key, value in item.items() if key != "sampled"}


def _validate_reconciled_source(original, remaining, rebuilt):
    if (
        rebuilt["workspace_id"] != original["workspace_id"]
        or rebuilt["workspace_config_fingerprint"]
        != original["plan"]["workspace_config_fingerprint"]
        or rebuilt["queue_sha256"] != original["plan"]["queue_sha256"]
    ):
        raise CohortReviewError("Reconciled source identity changed")
    expected = {value["cohort_id"]: value for value in remaining}
    actual = {value["cohort_id"]: value for value in rebuilt["cohorts"]}
    if set(actual) != set(expected):
        raise CohortReviewError("Reconciled cohort inventory changed")
    for cohort_id, old in expected.items():
        new = actual[cohort_id]
        if new["identity"] != old["identity"] or [
            _cohort_item_authority(value) for value in new["items"]
        ] != [_cohort_item_authority(value) for value in old["items"]]:
            raise CohortReviewError("Reconciled cohort authority changed")
        if not set(old["sample_queue_ids"]).issubset(new["sample_queue_ids"]):
            raise CohortReviewError("Reconciled cohort lost required samples")


def _reconciled_plan_document(
    source,
    current,
    remaining,
    *,
    clean_samples_per_bucket,
):
    cohorts = []
    selected = []
    for old in remaining:
        items = [{**value, "sampled": False} for value in old["items"]]
        sampled = {value["queue_id"] for value in items if value["technical_flags"]}
        for bucket in ("short", "medium", "long"):
            eligible = [
                value
                for value in items
                if not value["technical_flags"] and value["length_bucket"] == bucket
            ]
            eligible.sort(
                key=lambda value: (
                    hashlib.sha256(
                        f"{old['cohort_id']}\0{value['queue_id']}".encode("utf-8")
                    ).hexdigest(),
                    value["queue_id"],
                )
            )
            sampled.update(
                value["queue_id"] for value in eligible[:clean_samples_per_bucket]
            )
        selected.extend(value["queue_id"] for value in items)
        cohorts.append(
            {
                **old,
                "sample_queue_ids": sorted(sampled),
                "items": [
                    {**value, "sampled": value["queue_id"] in sampled}
                    for value in items
                ],
            }
        )
    policy = {
        **source["plan"]["policy"],
        "clean_samples_per_bucket": clean_samples_per_bucket,
        "selected_queue_ids": sorted(selected),
    }
    body = {
        "schema": source["plan"]["schema"],
        "schema_version": source["plan"]["schema_version"],
        "policy": policy,
        "workspace_id": source["workspace_id"],
        "workspace_config_fingerprint": source["plan"]["workspace_config_fingerprint"],
        "queue_sha256": source["plan"]["queue_sha256"],
        "state_sha256": current["state_sha256"],
        "cohort_count": len(cohorts),
        "pending_item_count": len(selected),
        "sample_item_count": sum(len(value["sample_queue_ids"]) for value in cohorts),
        "blocked_item_count": 0,
        "blocked_items": [],
        "cohorts": sorted(cohorts, key=lambda value: value["cohort_id"]),
    }
    document = {**body, "plan_id": _canonical_sha256(body)}
    return _validated_plan_document(document)


def _validated_progress_document(document, original):
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema",
            "schema_version",
            "root_bundle_id",
            "current_bundle",
            "progress_id",
        }
        or document.get("schema") != COHORT_REVIEW_PROGRESS_SCHEMA
        or document.get("schema_version") != COHORT_REVIEW_PROGRESS_VERSION
        or document.get("root_bundle_id") != original.bundle_id
    ):
        raise CohortReviewError("Cohort review progress is malformed")
    body = {name: value for name, value in document.items() if name != "progress_id"}
    if document.get("progress_id") != _canonical_sha256(body):
        raise CohortReviewError("Cohort review progress identity changed")
    current = _validated_bundle_document(document["current_bundle"])
    original_workspaces = {
        value["workspace_id"]: value["workspace"]
        for value in original.document["sources"]
    }
    if any(
        original_workspaces.get(value["workspace_id"]) != value["workspace"]
        for value in current["sources"]
    ):
        raise CohortReviewError("Cohort review progress source is not in publication")
    return CohortReviewBundle(current["bundle_id"], current)


def _load_source_sample_records(source, source_cohorts):
    workspace = Path(source["workspace"])
    configuration_path = workspace / "workspace.json"
    if configuration_path.is_symlink():
        raise CohortReviewError("Bundle workspace configuration cannot be a symlink")
    configuration_payload = _read_bytes(
        configuration_path, "bundle workspace configuration"
    )
    configuration = _decode_json(
        configuration_payload, "bundle workspace configuration"
    )
    if (
        not isinstance(configuration, dict)
        or configuration.get("workspace_id") != source["workspace_id"]
        or configuration.get("config_fingerprint")
        != source["plan"]["workspace_config_fingerprint"]
    ):
        raise CohortReviewError("Bundle workspace configuration identity changed")
    queue_path = _contained_source_path(
        workspace, configuration.get("queue"), "bundle queue"
    )
    output = _contained_source_path(
        workspace, configuration.get("output"), "bundle output"
    )
    if not output.is_dir() or output.is_symlink():
        raise CohortReviewError("Bundle output directory is unavailable or unsafe")
    state_path = output / "generation-state.json"
    if queue_path.is_symlink() or state_path.is_symlink():
        raise CohortReviewError("Bundle queue and state must not be symlinks")
    queue_payload = _read_bytes(queue_path, "bundle queue")
    state_payload = _read_bytes(state_path, "bundle state")
    queue_sha256 = hashlib.sha256(queue_payload).hexdigest()
    state_sha256 = hashlib.sha256(state_payload).hexdigest()
    if queue_sha256 != source["plan"]["queue_sha256"]:
        raise CohortReviewError("Bundle source queue changed")
    if state_sha256 != source["plan"]["state_sha256"]:
        raise CohortReviewError("Bundle source state changed")
    state = _decode_json(state_payload, "bundle state")
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        raise CohortReviewError("Bundle source state items are invalid")
    if state.get("queue_sha256") != queue_sha256:
        raise CohortReviewError("Bundle source state queue identity changed")
    queue_records = _decode_queue_records(queue_payload)
    sample_by_id = {
        sample["queue_id"]: (cohort, sample)
        for cohort in source_cohorts
        for sample in cohort["samples"]
    }
    if len(sample_by_id) != sum(len(cohort["samples"]) for cohort in source_cohorts):
        raise CohortReviewError("Bundle sample queue IDs are duplicated")
    review_items = {}
    for queue_id, (cohort, sample) in sample_by_id.items():
        queue_item = queue_records.get(queue_id)
        result = state["items"].get(queue_id)
        if not isinstance(queue_item, dict) or not isinstance(result, dict):
            raise CohortReviewError(f"Bundle sample disappeared: {queue_id}")
        text = queue_item.get("text")
        if (
            not isinstance(text, str)
            or hashlib.sha256(text.encode("utf-8")).hexdigest() != sample["text_sha256"]
            or result.get("status") != "generated"
            or result.get("review_status") != "pending_review"
            or result.get("file_sha256") != sample["audio_sha256"]
        ):
            raise CohortReviewError(f"Bundle sample authority changed: {queue_id}")
        audio = _contained_source_path(
            output, result.get("path"), f"bundle sample {queue_id} WAV"
        )
        if not audio.is_file():
            raise CohortReviewError(f"Bundle sample WAV is missing: {queue_id}")
        quality = result.get("quality")
        quality = quality if isinstance(quality, dict) else {}
        duration = quality.get("duration_seconds")
        duration = (
            float(duration)
            if isinstance(duration, (int, float)) and duration > 0
            else None
        )
        peak = quality.get("peak")
        peak = float(peak) if isinstance(peak, (int, float)) else None
        words = len(re.findall(r"[\w’'-]+", text, flags=re.UNICODE))
        review_items[queue_id] = ReviewItem(
            queue_id=queue_id,
            line_id=str(queue_item.get("line_id") or sample["line_id"]),
            speaker=str(queue_item.get("speaker") or "unknown"),
            voice_character=str(cohort["identity"]["voice_character"]),
            text=text,
            status="generated",
            review_status="pending_review",
            attempts=int(result.get("attempts") or 0),
            seed=result.get("seed"),
            last_error=result.get("last_error"),
            audio=audio,
            authority=ReviewAuthority(
                queue_sha256=queue_sha256,
                state_sha256=state_sha256,
                item_sha256=_canonical_sha256(result),
                audio_sha256=sample["audio_sha256"],
            ),
            state=state_path,
            queue=queue_path,
            duration_seconds=duration,
            words_per_minute=(
                None if duration is None else float(words * 60 / duration)
            ),
            peak=peak,
            technical_flags=tuple(sample["technical_flags"]),
        )
    for path, expected, label in (
        (
            configuration_path,
            hashlib.sha256(configuration_payload).hexdigest(),
            "workspace configuration",
        ),
        (queue_path, queue_sha256, "queue"),
        (state_path, state_sha256, "state"),
    ):
        if hashlib.sha256(_read_bytes(path, f"bundle {label}")).hexdigest() != expected:
            raise CohortReviewError(f"Bundle {label} changed while loading samples")
    return workspace, review_items


def _contained_source_path(root, relative, label):
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in Path(relative).parts)
    ):
        raise CohortReviewError(f"{label.title()} path is unsafe")
    canonical_root = Path(root).resolve()
    candidate = (canonical_root / relative).resolve()
    try:
        candidate.relative_to(canonical_root)
    except ValueError as error:
        raise CohortReviewError(f"{label.title()} leaves its workspace") from error
    return candidate


def _read_bytes(path, label):
    try:
        return Path(path).read_bytes()
    except OSError as error:
        raise CohortReviewError(f"Unable to read {label} {path}: {error}") from error


def _decode_json(payload, label):
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(f"Unable to decode {label}: {error}") from error


def _decode_queue_records(payload):
    try:
        rows = payload.decode("utf-8").splitlines()
        records = [json.loads(row) for row in rows]
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CohortReviewError(f"Unable to decode bundle queue: {error}") from error
    if not records or not isinstance(records[0], dict):
        raise CohortReviewError("Bundle queue metadata is invalid")
    by_id = {}
    for record in records[1:]:
        if not isinstance(record, dict):
            raise CohortReviewError("Bundle queue item is invalid")
        queue_id = record.get("queue_id")
        if not isinstance(queue_id, str) or not queue_id or queue_id in by_id:
            raise CohortReviewError("Bundle queue item identity is invalid")
        by_id[queue_id] = record
    return by_id


def execute_cohort_bundle_decision(
    bundle,
    workspace_id,
    cohort_id,
    decision,
    *,
    reviewed_queue_ids,
    sample_assessments=None,
    next_clean_samples_per_bucket=None,
):
    """Record and project one exact source-local decision from a bundle."""
    document = _validated_bundle_document(bundle)
    current = CohortReviewBundle(document["bundle_id"], document)
    source = next(
        (
            value
            for value in document["sources"]
            if value["workspace_id"] == workspace_id
        ),
        None,
    )
    if source is None:
        raise CohortReviewError(
            f"Workspace does not exist in this review bundle: {workspace_id}"
        )
    cohort = next(
        (
            value
            for value in source["plan"]["cohorts"]
            if value["cohort_id"] == cohort_id
        ),
        None,
    )
    if cohort is None:
        raise CohortReviewError(
            f"Cohort does not exist in the selected source: {cohort_id}"
        )
    source_plan = source["plan"]
    cohort_decision = build_cohort_review_decision(
        source_plan,
        cohort_id,
        decision,
        reviewed_queue_ids=reviewed_queue_ids,
        sample_assessments=sample_assessments,
        next_clean_samples_per_bucket=next_clean_samples_per_bucket,
    )
    projection = execute_cohort_review_decision(
        source["workspace"], source_plan, cohort_decision
    )
    expanded = not hasattr(projection, "queue_ids")
    if not expanded:
        next_sources = []
        for value in document["sources"]:
            if value["workspace_id"] != workspace_id:
                next_sources.append((value["workspace"], value["plan"]))
                continue
            reconciled = _reconcile_cohort_source(value)
            if reconciled is not None:
                next_sources.append(reconciled)
        next_bundle = _assemble_bundle(next_sources)
        return CohortBundleProjection(
            bundle_id=current.bundle_id,
            workspace_id=workspace_id,
            cohort_id=cohort_id,
            queue_ids=projection.queue_ids,
            review_status=projection.review_status,
            next_bundle=next_bundle,
        )
    next_sources = []
    for value in document["sources"]:
        if value["workspace_id"] != workspace_id:
            next_sources.append((value["workspace"], value["plan"]))
            continue
        next_sources.append(
            (
                value["workspace"],
                projection.document,
            )
        )
    next_bundle = _assemble_bundle(next_sources)
    return CohortBundleProjection(
        bundle_id=current.bundle_id,
        workspace_id=workspace_id,
        cohort_id=cohort_id,
        queue_ids=() if expanded else projection.queue_ids,
        review_status=None if expanded else projection.review_status,
        next_bundle=next_bundle,
    )


def _validated_bundle_document(bundle):
    document = bundle.document if isinstance(bundle, CohortReviewBundle) else bundle
    if not isinstance(document, dict):
        raise CohortReviewError("Cohort review bundle must be an object")
    if set(document) != {
        "schema",
        "schema_version",
        "policy",
        "workspace_count",
        "cohort_count",
        "pending_item_count",
        "sample_item_count",
        "blocked_item_count",
        "blocked_source_occurrence_count",
        "sources",
        "cohorts",
        "bundle_id",
    }:
        raise CohortReviewError("Cohort review bundle fields are invalid")
    if document.get("schema") != COHORT_REVIEW_BUNDLE_SCHEMA:
        raise CohortReviewError("Unsupported cohort review bundle schema")
    if document.get("schema_version") != COHORT_REVIEW_BUNDLE_VERSION:
        raise CohortReviewError("Unsupported cohort review bundle version")
    policy = document.get("policy")
    if not isinstance(policy, dict):
        raise CohortReviewError("Cohort review bundle policy must be an object")
    if set(policy) != {
        "attention_rule",
        "projection_scope",
        "sample_policy",
    }:
        raise CohortReviewError("Cohort review bundle policy fields are invalid")
    if policy.get("attention_rule") != "all technical flags":
        raise CohortReviewError("Cohort review bundle attention policy is invalid")
    if policy.get("projection_scope") != "source workspace only":
        raise CohortReviewError("Cohort review bundle projection policy is invalid")
    if policy.get("sample_policy") != "retained by each exact source plan":
        raise CohortReviewError("Cohort review bundle sample policy is invalid")
    sources = document.get("sources")
    cohorts = document.get("cohorts")
    if not isinstance(sources, list) or not isinstance(cohorts, list):
        raise CohortReviewError("Cohort review bundle inventories must be lists")
    paths = []
    workspace_ids = []
    expected_cohorts = []
    for source in sources:
        if not isinstance(source, dict):
            raise CohortReviewError("Cohort review bundle source must be an object")
        if set(source) != {"workspace", "workspace_id", "plan"}:
            raise CohortReviewError("Cohort review bundle source fields are invalid")
        path = source.get("workspace")
        if not isinstance(path, str) or not path:
            raise CohortReviewError("Cohort review bundle workspace path is invalid")
        canonical = str(Path(path).resolve())
        if canonical != path:
            raise CohortReviewError("Cohort review bundle workspace must be canonical")
        plan = _validated_plan_document(source.get("plan"))
        if source.get("workspace_id") != plan["workspace_id"]:
            raise CohortReviewError("Cohort review bundle source identity changed")
        paths.append(path)
        workspace_ids.append(plan["workspace_id"])
        for cohort in plan["cohorts"]:
            expected_cohorts.append(
                (
                    path,
                    plan["workspace_id"],
                    plan["plan_id"],
                    cohort,
                )
            )
    if len(paths) != len(set(paths)) or len(workspace_ids) != len(set(workspace_ids)):
        raise CohortReviewError("Cohort review bundle sources must be distinct")
    if document.get("workspace_count") != len(sources):
        raise CohortReviewError("Cohort review bundle workspace count is invalid")
    if document.get("cohort_count") != len(cohorts):
        raise CohortReviewError("Cohort review bundle cohort count is invalid")
    if document.get("pending_item_count") != sum(
        source["plan"]["pending_item_count"] for source in sources
    ):
        raise CohortReviewError("Cohort review bundle pending count is invalid")
    if document.get("sample_item_count") != sum(
        source["plan"]["sample_item_count"] for source in sources
    ):
        raise CohortReviewError("Cohort review bundle sample count is invalid")
    if document.get("blocked_item_count") != len(
        {
            item["queue_id"]
            for source in sources
            for item in source["plan"]["blocked_items"]
        }
    ):
        raise CohortReviewError("Cohort review bundle blocked count is invalid")
    if document.get("blocked_source_occurrence_count") != sum(
        source["plan"]["blocked_item_count"] for source in sources
    ):
        raise CohortReviewError(
            "Cohort review bundle blocked source-occurrence count is invalid"
        )

    actual = []
    for entry in cohorts:
        if not isinstance(entry, dict):
            raise CohortReviewError("Cohort review bundle cohort must be an object")
        actual.append(entry)
    rebuilt = _flatten_validated_sources(expected_cohorts)
    if actual != rebuilt:
        raise CohortReviewError("Cohort review bundle cohort inventory changed")
    body = {key: value for key, value in document.items() if key != "bundle_id"}
    bundle_id = _canonical_sha256(body)
    if document.get("bundle_id") != bundle_id:
        raise CohortReviewError("Cohort review bundle identity changed")
    return document


def _flatten_validated_sources(expected_cohorts):
    flattened = []
    for path, workspace_id, plan_id, cohort in expected_cohorts:
        sampled = set(cohort["sample_queue_ids"])
        samples = []
        for item in cohort["items"]:
            if item["queue_id"] not in sampled:
                continue
            flags = item["technical_flags"]
            samples.append(
                {
                    "queue_id": item["queue_id"],
                    "line_id": item["line_id"],
                    "text_sha256": item["text_sha256"],
                    "audio_sha256": item["audio_sha256"],
                    "length_bucket": item["length_bucket"],
                    "technical_flags": list(flags),
                    "required_reason": (
                        "technical-attention: " + "; ".join(flags)
                        if flags
                        else f"deterministic clean {item['length_bucket']} sample"
                    ),
                }
            )
        flattened.append(
            {
                "workspace": path,
                "workspace_id": workspace_id,
                "plan_id": plan_id,
                "cohort_id": cohort["cohort_id"],
                "identity": cohort["identity"],
                "item_count": cohort["item_count"],
                "attention_count": cohort["attention_count"],
                "samples": samples,
            }
        )
    return sorted(
        flattened,
        key=lambda value: (value["workspace_id"], value["cohort_id"]),
    )
