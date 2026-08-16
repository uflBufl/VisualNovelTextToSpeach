"""Command-line entry point for offline authoring workflows."""

import argparse
import json
from pathlib import Path

from vntts.authoring.legacy_import import (
    LegacyAuthoringImportError,
    default_import_root,
    default_legacy_jobs_root,
    discover_legacy_jobs,
    import_legacy_job,
)


def create_parser():
    parser = argparse.ArgumentParser(description="VNTTS offline pregeneration authoring")
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover = subparsers.add_parser(
        "discover-legacy",
        help="Inspect Reverse: 1999 pregeneration jobs without changing them",
    )
    discover.add_argument("--jobs-root", type=Path, default=default_legacy_jobs_root())
    migrate = subparsers.add_parser(
        "import-legacy",
        help="Validate and non-destructively import one legacy job",
    )
    migrate.add_argument("job_directory", type=Path)
    migrate.add_argument("--destination-root", type=Path, default=default_import_root())
    return parser


def main(argv=None):
    arguments = create_parser().parse_args(argv)
    if arguments.command == "discover-legacy":
        candidates = discover_legacy_jobs(arguments.jobs_root)
        print(
            json.dumps(
                [
                    {
                        "job_directory": str(candidate.job_directory),
                        "kind": candidate.kind,
                        "title": candidate.title,
                        "status": candidate.status,
                        "queue_items": candidate.queue_items,
                        "generated_items": candidate.generated_items,
                        "compatible": candidate.compatible,
                        "compatibility_error": candidate.compatibility_error,
                        "diagnostics": list(candidate.diagnostics),
                    }
                    for candidate in candidates
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    try:
        result = import_legacy_job(
            arguments.job_directory,
            arguments.destination_root,
        )
    except LegacyAuthoringImportError as error:
        create_parser().error(str(error))
    print(
        json.dumps(
            {
                "destination": str(result.destination),
                "created": result.created,
                "summary": result.manifest["summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0
