"""Checksum-bound review bundles spanning immutable authoring workspaces."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from vntts.authoring.bulk_generation import _canonical_sha256
from vntts.authoring.cohort_review import (
    DEFAULT_CLEAN_SAMPLES_PER_BUCKET,
    CohortReviewError,
    _load_document,
    _validated_plan_document,
    _write_document_no_replace,
    build_cohort_review_decision,
    build_cohort_review_plan,
    execute_cohort_review_decision,
)
from vntts.authoring.workbench import ReviewItem, list_review_items

COHORT_REVIEW_BUNDLE_SCHEMA = "vntts.authoring-cohort-review-bundle"
COHORT_REVIEW_BUNDLE_VERSION = 1


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


def build_cohort_review_bundle(
    workspace_directories,
    *,
    clean_samples_per_bucket=DEFAULT_CLEAN_SAMPLES_PER_BUCKET,
):
    """Build one deterministic review inventory over distinct workspaces."""
    paths = tuple(Path(value).resolve() for value in workspace_directories)
    if not paths:
        raise CohortReviewError("A review bundle requires at least one workspace")
    if len(set(paths)) != len(paths):
        raise CohortReviewError("Review bundle workspaces must be distinct")

    source_plans = []
    for path in sorted(paths, key=str):
        plan = build_cohort_review_plan(
            path,
            clean_samples_per_bucket=clean_samples_per_bucket,
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
        "blocked_item_count": sum(
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
    document = _validated_bundle_document(document)
    return CohortReviewBundle(document["bundle_id"], document)


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
    current = refresh_cohort_review_bundle(bundle)
    samples = []
    for source in current.document["sources"]:
        workspace = Path(source["workspace"])
        review_items = {value.queue_id: value for value in list_review_items(workspace)}
        flattened = {
            value["cohort_id"]: value
            for value in current.document["cohorts"]
            if value["workspace_id"] == source["workspace_id"]
        }
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
    current = refresh_cohort_review_bundle(bundle)
    document = current.document
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
    next_sources = []
    for value in document["sources"]:
        if value["workspace_id"] != workspace_id:
            next_sources.append((value["workspace"], value["plan"]))
            continue
        next_sources.append(
            (
                value["workspace"],
                projection.document
                if expanded
                else build_cohort_review_plan(
                    value["workspace"],
                    clean_samples_per_bucket=value["plan"]["policy"][
                        "clean_samples_per_bucket"
                    ],
                ).document,
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
    if document.get("blocked_item_count") != sum(
        source["plan"]["blocked_item_count"] for source in sources
    ):
        raise CohortReviewError("Cohort review bundle blocked count is invalid")

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
