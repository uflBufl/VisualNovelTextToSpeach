"""Audit complete visible-chapter coverage across sealed replay evidence."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from vntts.cli import cli_error, cli_messages
from vntts.live_replay_sequence_seal import (
    _decode_json,
    _next_visible_events,
    _read_regular_file,
    _write_json,
)
from vntts.live_sequence import LiveSequencePlan


class LiveReplayCoverageError(RuntimeError):
    """Sealed replay evidence cannot prove the requested chapter coverage."""


def audit_live_replay_coverage(
    output,
    *,
    story_index,
    sequence_plan,
    reviews,
):
    """Publish a checksum-bound union report for immutable sealed reviews."""
    output_path = Path(output).expanduser()
    if output_path.exists() or output_path.is_symlink():
        raise LiveReplayCoverageError(f"Coverage report already exists: {output_path}")
    if not output_path.parent.resolve().is_dir():
        raise LiveReplayCoverageError(
            f"Coverage report parent does not exist: {output_path.parent}"
        )
    story_path, story_payload = _read_regular_file(story_index, "Story index")
    plan_path, plan_payload = _read_regular_file(sequence_plan, "Sequence plan")
    story_sha256 = hashlib.sha256(story_payload).hexdigest()
    plan_sha256 = hashlib.sha256(plan_payload).hexdigest()
    try:
        plan = LiveSequencePlan.load(plan_path, story_path)
    except Exception as error:
        raise LiveReplayCoverageError(
            f"Story index and sequence plan are incompatible: {error}"
        ) from error
    visible = tuple(
        sorted(
            (
                event
                for event in plan.events.values()
                if event.kind in {"speech", "silent"}
            ),
            key=lambda event: (str(event.chapter), event.sequence, event.event_id),
        )
    )
    if len({event.chapter for event in visible}) != 1:
        raise LiveReplayCoverageError(
            "Visible chapter coverage requires a one-chapter sequence plan"
        )
    _validate_visible_path(plan, visible)
    expected_ids = tuple(event.event_id for event in visible)
    covered = set()
    review_required = set()
    accepted_review = set()
    sources = []
    selected_reviews = tuple(reviews)
    if not selected_reviews:
        raise LiveReplayCoverageError("At least one sealed sequence review is required")
    for value in selected_reviews:
        review_path, payload = _read_regular_file(value, "Sealed sequence review")
        document = _decode_json(payload, "Sealed sequence review")
        if (
            document.get("schema") != "vntts.sequence-replay-seal-review"
            or document.get("schema_version") != 1
            or document.get("sealed_replay_successful") is not True
        ):
            raise LiveReplayCoverageError(
                f"Review is not successful sealed replay evidence: {review_path}"
            )
        authority = document.get("authority")
        if not isinstance(authority, dict):
            raise LiveReplayCoverageError(f"Review authority is missing: {review_path}")
        if authority.get("story_index_sha256") != story_sha256:
            raise LiveReplayCoverageError(
                f"Review uses a different story index: {review_path}"
            )
        if authority.get("sequence_plan_sha256") != plan_sha256:
            raise LiveReplayCoverageError(
                f"Review uses a different sequence plan: {review_path}"
            )
        mappings = document.get("mappings")
        if not isinstance(mappings, list) or not mappings:
            raise LiveReplayCoverageError(f"Review has no mappings: {review_path}")
        event_ids = []
        source_review_required = []
        for mapping in mappings:
            if not isinstance(mapping, dict):
                raise LiveReplayCoverageError(
                    f"Review mapping is invalid: {review_path}"
                )
            event_id = str(mapping.get("event_id") or "")
            if event_id not in plan.events or event_id not in expected_ids:
                raise LiveReplayCoverageError(
                    f"Review maps an unknown or non-visible event: {event_id!r}"
                )
            event = plan.events[event_id]
            if mapping.get("event_kind") not in {None, event.kind}:
                raise LiveReplayCoverageError(
                    f"Review event kind disagrees with the plan: {event_id!r}"
                )
            if mapping.get("line_id") != event.line_id:
                raise LiveReplayCoverageError(
                    f"Review line identity disagrees with the plan: {event_id!r}"
                )
            event_ids.append(event_id)
            if mapping.get("mapping_method") != "exact-line-id":
                source_review_required.append(event_id)
        expected_positions = [expected_ids.index(event_id) for event_id in event_ids]
        if expected_positions != sorted(set(expected_positions)):
            raise LiveReplayCoverageError(
                f"Review mappings are duplicated or out of plan order: {review_path}"
            )
        covered.update(event_ids)
        if document.get("capture_boundary_review_required") is True:
            source_review_required = list(event_ids)
        review_required.update(source_review_required)
        human_accepted = document.get("human_acceptance_recorded") is True
        if human_accepted:
            accepted_review.update(source_review_required)
        sources.append(
            {
                "path": str(review_path),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "event_count": len(event_ids),
                "first_event_id": event_ids[0],
                "last_event_id": event_ids[-1],
                "human_acceptance_recorded": human_accepted,
                "human_review_required_event_ids": source_review_required,
            }
        )
    missing = [event_id for event_id in expected_ids if event_id not in covered]
    human_pending = [
        event_id
        for event_id in expected_ids
        if event_id in review_required and event_id not in accepted_review
    ]
    document = {
        "schema": "vntts.live-replay-visible-chapter-coverage",
        "schema_version": 1,
        "authority": {
            "story_index_path": str(story_path),
            "story_index_sha256": story_sha256,
            "sequence_plan_path": str(plan_path),
            "sequence_plan_sha256": plan_sha256,
        },
        "expected_visible_event_count": len(expected_ids),
        "covered_visible_event_count": len(covered),
        "speech_event_count": sum(event.kind == "speech" for event in visible),
        "silent_event_count": sum(event.kind == "silent" for event in visible),
        "missing_event_ids": missing,
        "technical_coverage_complete": not missing,
        "human_acceptance_complete": not human_pending,
        "human_acceptance_pending_event_ids": human_pending,
        "sources": sources,
    }
    _write_json(output_path, document)
    return output_path, document


def _validate_visible_path(plan, visible):
    for current, following in zip(visible, visible[1:]):
        frontier = _next_visible_events(plan, current)
        if len(frontier) != 1 or frontier[0].event_id != following.event_id:
            raise LiveReplayCoverageError(
                "Complete visible chapter coverage requires one deterministic "
                f"path; {current.event_id!r} does not uniquely reach "
                f"{following.event_id!r}"
            )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Audit full visible-chapter coverage across sealed replays"
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--story-index", type=Path, required=True)
    parser.add_argument("--sequence-plan", type=Path, required=True)
    parser.add_argument(
        "--review",
        type=Path,
        action="append",
        required=True,
        help="Sealed sequence-review.json; repeat for multiple evidence segments",
    )
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        path, report = audit_live_replay_coverage(
            arguments.output,
            story_index=arguments.story_index,
            sequence_plan=arguments.sequence_plan,
            reviews=arguments.review,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        return cli_error(error)
    return cli_messages(
        (
            (
                "Complete technical visible-chapter coverage"
                if report["technical_coverage_complete"]
                else "Visible-chapter coverage remains incomplete"
            ),
            f"Covered {report['covered_visible_event_count']}/"
            f"{report['expected_visible_event_count']} visible events",
            path,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
