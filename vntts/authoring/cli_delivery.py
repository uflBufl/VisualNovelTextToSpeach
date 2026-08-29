"""Delivery-annotation command family for the authoring CLI."""

from __future__ import annotations

import argparse
import json

from vntts.authoring.delivery import LEGACY_ENGLISH_POLICY, apply_delivery_policy

COMMANDS = frozenset({"annotate-delivery"})


def configure_parsers(subparsers) -> None:
    annotate = subparsers.add_parser(
        "annotate-delivery",
        help="Print one provenance-marked legacy English delivery annotation",
    )
    annotate.add_argument("--text", required=True)
    annotate.add_argument("--speaker", default="Narrator")
    annotate.add_argument("--previous-text")
    annotate.add_argument("--next-text")
    annotate.add_argument("--kind", default="dialogue")


def handle(arguments: argparse.Namespace) -> int:
    application = apply_delivery_policy(
        {
            "text": arguments.text,
            "speaker": arguments.speaker,
            "previous_text": arguments.previous_text,
            "next_text": arguments.next_text,
            "kind": arguments.kind,
        },
        LEGACY_ENGLISH_POLICY,
    )
    print(
        json.dumps(
            {
                "annotation": {
                    key: application.record[key]
                    for key in (
                        "annotation_version",
                        "emotion",
                        "delivery",
                        "prompt_adapters",
                    )
                },
                "provenance": application.provenance,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0
