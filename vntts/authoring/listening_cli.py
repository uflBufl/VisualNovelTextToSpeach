"""Command-line presentation adapter for blind model listening."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.listening import (
    ModelListeningError,
    aggregate_listening_report,
    create_listening_session,
    create_listening_session_from_reports,
    default_session_directory,
    listening_progress,
    load_listening_session,
    next_pending_trial,
    record_trial_preference,
)
from vntts.cli import cli_error, cli_success


def create_parser():
    parser = argparse.ArgumentParser(description="Run blind, resumable model listening")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("--benchmark", type=Path, required=True)
    start.add_argument("--output", type=Path, default=default_session_directory)
    start.add_argument("--seed", type=int, default=0)
    start_reports = subparsers.add_parser("start-reports")
    start_reports.add_argument("--reports", type=Path, nargs="+", required=True)
    start_reports.add_argument("--output", type=Path, default=default_session_directory)
    start_reports.add_argument("--seed", type=int, default=0)
    start_reports.add_argument(
        "--sample-id",
        action="append",
        dest="sample_ids",
        help="Include only this exact shared complete sample ID; repeat as needed",
    )
    for command in ("status", "next", "report", "ui"):
        child = subparsers.add_parser(command)
        child.add_argument(
            "--session", type=Path, default=default_session_directory / "session.json"
        )
        if command == "report":
            child.add_argument("--output", type=Path)
    score = subparsers.add_parser("score")
    score.add_argument("trial_id")
    score.add_argument(
        "--session", type=Path, default=default_session_directory / "session.json"
    )
    score.add_argument(
        "--preference", choices=("a", "b", "tie", "neither"), required=True
    )
    score.add_argument("--overwrite", action="store_true")
    return parser


def main(argv=None):
    options = create_parser().parse_args(argv)
    try:
        if options.command == "start":
            path = create_listening_session(
                options.benchmark, options.output, seed=options.seed
            )
            return cli_success(f"Created blind listening session: {path}")
        if options.command == "start-reports":
            path = create_listening_session_from_reports(
                options.reports,
                options.output,
                seed=options.seed,
                sample_ids=options.sample_ids,
            )
            return cli_success(f"Created blind listening session: {path}")
        if options.command == "ui":
            from vntts.authoring.listening_ui import launch_listening_workbench

            return launch_listening_workbench(options.session)
        session = load_listening_session(options.session)
        if options.command == "status":
            completed, total = listening_progress(session)
            return cli_success(f"Listening progress: {completed}/{total} trials")
        if options.command == "next":
            trial = next_pending_trial(session)
            if trial is None:
                return cli_success("Listening session is complete")
            print(json.dumps(trial, ensure_ascii=False, indent=2))
            return 0
        if options.command == "score":
            updated = record_trial_preference(
                options.session,
                options.trial_id,
                options.preference,
                overwrite=options.overwrite,
                report_path=Path(options.session).resolve().with_name("report.json"),
            )
            completed, total = listening_progress(updated)
            return cli_success(
                f"Saved {options.trial_id}; progress: {completed}/{total} trials"
            )
        output = options.output or Path(options.session).resolve().with_name(
            "report.json"
        )
        report = aggregate_listening_report(options.session, output)
        return cli_success(
            f"Listening report: {output} ({report['completed_trials']} completed, "
            f"{report['pending_trials']} pending)"
        )
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("PySide6"):
            return cli_error("Qt UI is not installed")
        raise
    except (ModelListeningError, OSError, json.JSONDecodeError) as error:
        return cli_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
