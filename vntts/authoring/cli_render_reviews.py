"""Checksum-bound render/reference review commands for the authoring CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.reference_render_comparison import (
    ReferenceRenderComparisonError as _ReferenceRenderComparisonError,
)
from vntts.authoring.reference_render_comparison import (
    create_reference_render_listening,
    import_reference_render_preference,
    load_reference_render_plan,
    publish_reference_render_comparison,
)
from vntts.authoring.render_hypothesis_review import (
    RenderHypothesisReviewError as _RenderHypothesisReviewError,
)
from vntts.authoring.render_hypothesis_review import (
    import_accepted_render_hypothesis,
    load_render_hypothesis_review,
    publish_render_hypothesis_review,
    record_render_hypothesis_decision,
)

ReferenceRenderComparisonError = _ReferenceRenderComparisonError
RenderHypothesisReviewError = _RenderHypothesisReviewError

HYPOTHESIS_COMMANDS = frozenset(
    {
        "render-hypothesis-review-publish",
        "render-hypothesis-review-decide",
        "render-hypothesis-review-status",
        "render-hypothesis-review-import",
    }
)
REFERENCE_COMMANDS = frozenset(
    {
        "failure-reference-render-comparison",
        "failure-reference-render-session",
        "failure-reference-import-listening",
    }
)
COMMANDS = HYPOTHESIS_COMMANDS | REFERENCE_COMMANDS


def configure_hypothesis_parsers(subparsers) -> None:
    publish = subparsers.add_parser(
        "render-hypothesis-review-publish",
        help="Publish one immutable unmatched render/reference review",
    )
    publish.add_argument("comparison", type=Path)
    publish.add_argument("queue_id")
    publish.add_argument("arm_id")
    publish.add_argument("--output", type=Path, required=True)
    decide = subparsers.add_parser(
        "render-hypothesis-review-decide",
        help="Accept one exact render hypothesis or require a different one",
    )
    decide.add_argument("directory", type=Path)
    decide.add_argument("decision", choices=("accept_hypothesis", "need_different"))
    status = subparsers.add_parser(
        "render-hypothesis-review-status",
        help="Validate and inspect one unmatched render/reference review",
    )
    status.add_argument("directory", type=Path)
    import_review = subparsers.add_parser(
        "render-hypothesis-review-import",
        help="Bind one accepted render hypothesis to one fresh exact audit",
    )
    import_review.add_argument("audit", type=Path)
    import_review.add_argument("comparison", type=Path)
    import_review.add_argument("review", type=Path)
    import_review.add_argument("queue_id")


def configure_reference_parsers(subparsers) -> None:
    render = subparsers.add_parser(
        "failure-reference-render-comparison",
        help="Render an immutable comparison from exact failed-reference arms",
    )
    render.add_argument("plan", type=Path)
    render.add_argument("--output", type=Path, required=True)
    listen = subparsers.add_parser(
        "failure-reference-render-session",
        help="Create a blind session for complete matched reference renders",
    )
    listen.add_argument("comparison", type=Path)
    listen.add_argument("--output", type=Path, required=True)
    listen.add_argument("--seed", type=int, default=0)
    listen.add_argument(
        "--arm-id",
        action="append",
        help="Select exactly two complete comparison arms without rerendering",
    )
    import_listening = subparsers.add_parser(
        "failure-reference-import-listening",
        help="Bind one completed blind reference preference to a fresh audit",
    )
    import_listening.add_argument("audit", type=Path)
    import_listening.add_argument("comparison", type=Path)
    import_listening.add_argument("session", type=Path)
    import_listening.add_argument("queue_id")


def handle(arguments: argparse.Namespace) -> int:
    if arguments.command == "render-hypothesis-review-publish":
        result = publish_render_hypothesis_review(
            arguments.comparison,
            arguments.queue_id,
            arguments.arm_id,
            arguments.output,
        )
    elif arguments.command == "render-hypothesis-review-decide":
        result = record_render_hypothesis_decision(
            arguments.directory, arguments.decision
        )
    elif arguments.command == "render-hypothesis-review-status":
        result = load_render_hypothesis_review(arguments.directory)
    elif arguments.command == "render-hypothesis-review-import":
        result = import_accepted_render_hypothesis(
            arguments.audit,
            arguments.comparison,
            arguments.review,
            arguments.queue_id,
        )
    elif arguments.command == "failure-reference-render-comparison":
        plan = load_reference_render_plan(arguments.plan)
        result = publish_reference_render_comparison(plan, arguments.output)
        print(
            json.dumps(
                {
                    "directory": str(result.directory),
                    "comparison_id": result.comparison_id,
                    "arm_count": result.arm_count,
                    "sample_count": result.sample_count,
                    "complete_pair_count": result.complete_pair_count,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    elif arguments.command == "failure-reference-render-session":
        session = create_reference_render_listening(
            arguments.comparison,
            arguments.output,
            seed=arguments.seed,
            arm_ids=arguments.arm_id,
        )
        print(
            json.dumps(
                {
                    "comparison": arguments.comparison.as_posix(),
                    "session": session.as_posix(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    elif arguments.command == "failure-reference-import-listening":
        result = import_reference_render_preference(
            arguments.audit,
            arguments.comparison,
            arguments.session,
            arguments.queue_id,
        )
    else:
        raise ValueError(f"Unsupported render-review command: {arguments.command!r}")
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0
