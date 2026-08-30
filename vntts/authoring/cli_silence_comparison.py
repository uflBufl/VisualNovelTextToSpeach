"""Silence comparison command family for the authoring CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.failure_repair import DEFAULT_INTERNAL_SILENCE_TARGET_SECONDS
from vntts.authoring.silence_comparison import (
    SilenceComparisonError as _SilenceComparisonError,
)
from vntts.authoring.silence_comparison import (
    create_silence_comparison_session,
    load_silence_comparison,
    load_silence_comparison_input_plan,
    publish_silence_comparison,
)

SilenceComparisonError = _SilenceComparisonError

COMMANDS = frozenset(
    {
        "silence-comparison-publish",
        "silence-comparison-check",
        "silence-comparison-session",
    }
)


def configure_parsers(subparsers) -> None:
    publish = subparsers.add_parser(
        "silence-comparison-publish",
        help="Publish a checksum-bound segmentation/compression comparison",
    )
    publish.add_argument("plan", type=Path)
    publish.add_argument("--output", type=Path, required=True)
    publish.add_argument(
        "--target-seconds",
        type=float,
        default=DEFAULT_INTERNAL_SILENCE_TARGET_SECONDS,
        help="Silent boundary retained in the comparison-only compressed candidate",
    )
    check = subparsers.add_parser(
        "silence-comparison-check",
        help="Validate a published comparison and every bound artifact",
    )
    check.add_argument("comparison", type=Path)
    session = subparsers.add_parser(
        "silence-comparison-session",
        help="Create a blinded A/B session from a verified comparison",
    )
    session.add_argument("comparison", type=Path)
    session.add_argument("--output", type=Path, required=True)
    session.add_argument("--seed", type=int, default=0)


def handle(arguments: argparse.Namespace) -> int:
    if arguments.command == "silence-comparison-publish":
        plan = load_silence_comparison_input_plan(arguments.plan)
        result = publish_silence_comparison(
            plan.samples,
            arguments.output,
            target_seconds=arguments.target_seconds,
            input_plan_sha256=plan.sha256,
        )
        payload = {
            "directory": str(result.directory),
            "input_plan": str(plan.path),
            "input_plan_sha256": plan.sha256,
            "sample_count": result.sample_count,
            "reports": [str(path) for path in result.report_paths],
        }
    elif arguments.command == "silence-comparison-check":
        document = load_silence_comparison(arguments.comparison)
        payload = {
            "comparison": str(arguments.comparison.expanduser().resolve()),
            "input_plan_sha256": document.get("input_plan_sha256"),
            "production_enabled": document["policy"]["production_enabled"],
            "requires_blind_review": document["policy"]["requires_blind_review"],
            "sample_count": len(document["samples"]),
            "target_seconds": document["policy"]["target_seconds"],
        }
    elif arguments.command == "silence-comparison-session":
        session = create_silence_comparison_session(
            arguments.comparison,
            arguments.output,
            seed=arguments.seed,
        )
        payload = {
            "session": str(session),
            "comparison": str(arguments.comparison),
        }
    else:
        raise ValueError(
            f"Unsupported silence-comparison command: {arguments.command!r}"
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0
