"""Checksum-bound cohort planning and decision commands for the authoring CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.cohort_bundle import (
    build_cohort_review_bundle,
    execute_cohort_bundle_decision,
    load_cohort_review_bundle,
    write_cohort_review_bundle,
)
from vntts.authoring.cohort_review import CohortReviewError as _CohortReviewError
from vntts.authoring.cohort_review import (
    apply_cohort_review_decision,
    build_cohort_review_decision,
    build_cohort_review_plan,
    load_cohort_review_decision,
    load_cohort_review_plan,
    write_cohort_review_decision,
    write_cohort_review_plan,
)

CohortReviewError = _CohortReviewError

PLANNING_COMMANDS = frozenset(
    {
        "cohort-review-plan",
        "cohort-review-bundle",
        "cohort-review-bundle-apply",
    }
)
DECISION_COMMANDS = frozenset(
    {
        "cohort-review-decision",
        "cohort-review-apply",
    }
)
COMMANDS = PLANNING_COMMANDS | DECISION_COMMANDS


def configure_planning_parsers(subparsers) -> None:
    review = subparsers.add_parser(
        "cohort-review-plan",
        help="Plan checksum-bound technical-attention and clean review samples",
    )
    review.add_argument("workspace", type=Path)
    review.add_argument(
        "--clean-samples-per-bucket",
        type=int,
        default=1,
        help="Deterministic clean samples for each short/medium/long bucket",
    )
    review.add_argument(
        "--output",
        type=Path,
        help="Publish the immutable plan without replacing an existing file",
    )
    review.add_argument(
        "--queue-id",
        action="append",
        default=None,
        dest="queue_ids",
        help="Restrict the plan to one exact pending queue ID; repeat as needed",
    )
    bundle = subparsers.add_parser(
        "cohort-review-bundle",
        help="Plan one checksum-bound review inventory across workspaces",
    )
    bundle.add_argument(
        "--workspace",
        action="append",
        type=Path,
        required=True,
        help="Immutable source workspace; repeat for each source",
    )
    bundle.add_argument(
        "--clean-samples-per-bucket",
        type=int,
        default=1,
        help="Deterministic clean samples for each short/medium/long bucket",
    )
    bundle.add_argument(
        "--workspace-queue-id",
        action="append",
        nargs=2,
        default=[],
        metavar=("WORKSPACE", "QUEUE_ID"),
        help=(
            "Select one exact pending queue ID from one --workspace; repeat as "
            "needed. When used, every workspace requires at least one selection"
        ),
    )
    bundle.add_argument("--output", type=Path)
    bundle_apply = subparsers.add_parser(
        "cohort-review-bundle-apply",
        help="Apply one exact source-local cohort decision from a bundle",
    )
    bundle_apply.add_argument("bundle", type=Path)
    bundle_apply.add_argument("workspace_id")
    bundle_apply.add_argument("cohort_id")
    bundle_apply.add_argument(
        "decision", choices=("accepted", "rejected", "split", "expand")
    )
    bundle_apply.add_argument("--reviewed-queue-id", action="append", default=[])
    bundle_apply.add_argument("--bad-queue-id", action="append", default=[])
    bundle_apply.add_argument("--next-clean-samples-per-bucket", type=int)


def configure_decision_parsers(subparsers) -> None:
    decision = subparsers.add_parser(
        "cohort-review-decision",
        help="Record an immutable human decision over one exact cohort sample",
    )
    decision.add_argument("plan", type=Path)
    decision.add_argument("cohort_id")
    decision.add_argument(
        "decision", choices=("accepted", "rejected", "split", "expand")
    )
    decision.add_argument(
        "--reviewed-queue-id",
        action="append",
        default=[],
        help="Exact sampled queue ID actually reviewed; repeat for each WAV",
    )
    decision.add_argument(
        "--bad-queue-id",
        action="append",
        default=[],
        help="Reviewed queue ID marked bad; repeat as needed",
    )
    decision.add_argument("--next-clean-samples-per-bucket", type=int)
    decision.add_argument("--output", type=Path, required=True)
    apply_decision = subparsers.add_parser(
        "cohort-review-apply",
        help="Atomically project one recorded terminal cohort decision",
    )
    apply_decision.add_argument("workspace", type=Path)
    apply_decision.add_argument("plan", type=Path)
    apply_decision.add_argument("decision", type=Path)


def _selected_queue_ids_by_workspace(arguments: argparse.Namespace):
    if not arguments.workspace_queue_id:
        return None
    selections = {
        Path(workspace).expanduser().resolve(): [] for workspace in arguments.workspace
    }
    for workspace_value, queue_id in arguments.workspace_queue_id:
        workspace = Path(workspace_value).expanduser().resolve()
        if workspace not in selections:
            raise CohortReviewError(
                "Review bundle queue selection references an unknown "
                f"workspace: {workspace}"
            )
        if not queue_id.strip():
            raise CohortReviewError("Review bundle selected queue ID must be non-empty")
        if queue_id in selections[workspace]:
            raise CohortReviewError(
                "Review bundle selected queue ID is duplicated for "
                f"{workspace}: {queue_id}"
            )
        selections[workspace].append(queue_id)
    missing = sorted(
        str(workspace) for workspace, queue_ids in selections.items() if not queue_ids
    )
    if missing:
        raise CohortReviewError(
            "Every review bundle workspace requires an exact queue "
            f"selection: {missing}"
        )
    return selections


def _sample_assessments(arguments: argparse.Namespace):
    bad = set(arguments.bad_queue_id)
    unexpected = sorted(bad - set(arguments.reviewed_queue_id))
    if unexpected:
        raise CohortReviewError(f"Bad queue IDs were not reviewed: {unexpected}")
    return {
        queue_id: "bad" if queue_id in bad else "acceptable"
        for queue_id in arguments.reviewed_queue_id
    }


def _print_document(value) -> None:
    print(json.dumps(value.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


def handle(arguments: argparse.Namespace) -> int:
    if arguments.command == "cohort-review-plan":
        plan = build_cohort_review_plan(
            arguments.workspace,
            clean_samples_per_bucket=arguments.clean_samples_per_bucket,
            queue_ids=arguments.queue_ids,
        )
        if arguments.output is not None:
            write_cohort_review_plan(plan, arguments.output)
        _print_document(plan)
        return 0
    if arguments.command == "cohort-review-bundle":
        bundle = build_cohort_review_bundle(
            arguments.workspace,
            clean_samples_per_bucket=arguments.clean_samples_per_bucket,
            queue_ids_by_workspace=_selected_queue_ids_by_workspace(arguments),
        )
        if arguments.output is not None:
            write_cohort_review_bundle(bundle, arguments.output)
        _print_document(bundle)
        return 0
    if arguments.command == "cohort-review-bundle-apply":
        assessments = _sample_assessments(arguments)
        projection = execute_cohort_bundle_decision(
            load_cohort_review_bundle(arguments.bundle),
            arguments.workspace_id,
            arguments.cohort_id,
            arguments.decision,
            reviewed_queue_ids=arguments.reviewed_queue_id,
            sample_assessments=assessments,
            next_clean_samples_per_bucket=arguments.next_clean_samples_per_bucket,
        )
        _print_document(projection)
        return 0
    if arguments.command == "cohort-review-decision":
        assessments = _sample_assessments(arguments)
        decision = build_cohort_review_decision(
            load_cohort_review_plan(arguments.plan),
            arguments.cohort_id,
            arguments.decision,
            reviewed_queue_ids=arguments.reviewed_queue_id,
            sample_assessments=(
                assessments
                if arguments.bad_queue_id or arguments.decision == "split"
                else None
            ),
            next_clean_samples_per_bucket=arguments.next_clean_samples_per_bucket,
        )
        write_cohort_review_decision(decision, arguments.output)
        _print_document(decision)
        return 0
    if arguments.command == "cohort-review-apply":
        result = apply_cohort_review_decision(
            arguments.workspace,
            load_cohort_review_plan(arguments.plan),
            load_cohort_review_decision(arguments.decision),
        )
        _print_document(result)
        return 0
    raise ValueError(f"Unsupported cohort-review command: {arguments.command!r}")
