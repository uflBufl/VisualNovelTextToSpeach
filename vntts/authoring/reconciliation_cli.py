"""CLI for read-only authoring authority reconciliation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.reconciliation import (
    AuthoringReconciliationError,
    build_authoring_reconciliation,
    write_authoring_reconciliation,
)


def create_parser():
    parser = argparse.ArgumentParser(
        description="Reconcile exact authoring authorities without changing them"
    )
    parser.add_argument("--primary-workspace", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument(
        "--bundle",
        type=Path,
        action="append",
        default=None,
        help=(
            "Reconcile only this exact cohort publication; repeat as needed. "
            "Without this option every publication in --bundle-root is scanned."
        ),
    )
    parser.add_argument(
        "--quality-review",
        type=Path,
        action="append",
        default=[],
        help="Include one exact current source-quality review; repeat as needed",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv=None):
    options = create_parser().parse_args(argv)
    try:
        report = build_authoring_reconciliation(
            options.primary_workspace,
            options.bundle_root,
            bundle_publications=options.bundle,
            quality_reviews=options.quality_review,
        )
        if options.output is not None:
            write_authoring_reconciliation(report, options.output)
    except AuthoringReconciliationError as error:
        create_parser().error(str(error))
    payload = report.document
    if options.output is not None:
        payload = {
            "output": str(options.output.expanduser().resolve()),
            "report_id": report.report_id,
            "summary": report.document["summary"],
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
