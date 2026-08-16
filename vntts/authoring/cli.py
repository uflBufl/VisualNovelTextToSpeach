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
    import_standalone_generation,
    inspect_standalone_generation,
)
from vntts.authoring.listening_import import (
    ListeningImportError,
    import_listening_session,
    inspect_listening_session,
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
    inspect_standalone = subparsers.add_parser(
        "inspect-standalone",
        help="Validate one explicitly paired standalone queue and output",
    )
    inspect_standalone.add_argument("--queue", type=Path, required=True)
    inspect_standalone.add_argument("--output", type=Path, required=True)
    import_standalone = subparsers.add_parser(
        "import-standalone",
        help="Non-destructively import one explicitly paired queue and output",
    )
    import_standalone.add_argument("--queue", type=Path, required=True)
    import_standalone.add_argument("--output", type=Path, required=True)
    import_standalone.add_argument(
        "--destination-root", type=Path, default=default_import_root()
    )
    inspect_listening = subparsers.add_parser(
        "inspect-listening",
        help="Validate one selected legacy blind-listening session",
    )
    inspect_listening.add_argument("session_directory", type=Path)
    import_listening = subparsers.add_parser(
        "import-listening",
        help="Non-destructively preserve one selected blind-listening session",
    )
    import_listening.add_argument("session_directory", type=Path)
    import_listening.add_argument(
        "--destination-root", type=Path, default=default_import_root()
    )
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
        if arguments.command == "inspect-standalone":
            plan = inspect_standalone_generation(arguments.queue, arguments.output)
            print(json.dumps(plan.summary, indent=2, sort_keys=True))
            return 0
        if arguments.command == "import-standalone":
            result = import_standalone_generation(
                arguments.queue,
                arguments.output,
                arguments.destination_root,
            )
        elif arguments.command == "inspect-listening":
            inspection = inspect_listening_session(arguments.session_directory)
            print(
                json.dumps(
                    {
                        "session_directory": str(inspection.session_directory),
                        "trial_count": inspection.trial_count,
                        "completed_count": inspection.completed_count,
                        "audio_count": inspection.audio_count,
                        "report_present": inspection.report_present,
                        "logical_identity": inspection.logical_identity,
                        "source_fingerprint": inspection.source_fingerprint,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        elif arguments.command == "import-listening":
            result = import_listening_session(
                arguments.session_directory,
                arguments.destination_root,
            )
        else:
            result = import_legacy_job(
                arguments.job_directory,
                arguments.destination_root,
            )
    except (LegacyAuthoringImportError, ListeningImportError) as error:
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
