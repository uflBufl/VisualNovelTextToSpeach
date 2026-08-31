"""Reference discovery, review and binding command family."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.failure_reference_audit import publish_failure_reference_audit
from vntts.authoring.failure_reference_binding import publish_failure_reference_binding
from vntts.authoring.known_role_reuse import publish_known_role_reuse_binding
from vntts.authoring.missing_voice_live_fallback import (
    authorize_missing_voice_live_fallback,
)
from vntts.authoring.missing_voice_reuse import (
    build_missing_voice_reuse_candidate_command,
    build_missing_voice_reuse_plan,
    load_missing_voice_reuse_plan,
    parse_cohort_arguments,
    prepare_missing_voice_reuse_candidate_workspace,
    write_missing_voice_reuse_plan,
)
from vntts.authoring.missing_voice_reuse_binding import (
    publish_missing_voice_reuse_binding,
)
from vntts.authoring.missing_voice_reuse_review import (
    build_missing_voice_reuse_review,
    load_missing_voice_reuse_review,
    missing_voice_reuse_review_progress,
    parse_missing_voice_reuse_evidence,
)
from vntts.authoring.portrait_aliases import (
    build_portrait_alias_decision,
    build_portrait_alias_plan,
    load_portrait_alias_plan,
    write_portrait_alias_decision,
    write_portrait_alias_plan,
)
from vntts.authoring.reference_selection import (
    inspect_voice_reference_candidates,
    select_voice_reference,
)
from vntts.authoring.source_reference_review import (
    import_source_reference_review,
    publish_source_reference_binding_retirement,
    publish_source_reference_binding_successor,
    publish_source_reference_bindings,
    publish_source_reference_evaluation,
    publish_source_reference_listening_reports,
)
from vntts.authoring.workbench import (
    create_failure_reference_workspace,
    default_workspaces_root,
)

COMMANDS = frozenset(
    {
        "failure-reference-audit",
        "failure-reference-binding",
        "create-failure-reference-workspace",
        "missing-voice-reuse-plan",
        "missing-voice-reuse-candidate-workspace",
        "missing-voice-reuse-candidate-command",
        "missing-voice-reuse-review",
        "missing-voice-reuse-review-status",
        "missing-voice-reuse-review-ui",
        "missing-voice-reuse-binding",
        "missing-voice-live-fallback",
        "known-role-reuse-binding",
        "portrait-alias-plan",
        "portrait-alias-decision",
        "reference-report",
        "select-reference",
        "import-reference-review",
        "build-reference-evaluation",
        "build-reference-listening-reports",
        "build-reference-bindings",
        "extend-reference-bindings",
        "retire-reference-bindings",
    }
)


def configure_parsers(subparsers) -> None:
    reference_audit = subparsers.add_parser(
        "failure-reference-audit",
        help="Publish a blinded exact-reference audit for speech-quality failures",
    )
    reference_audit.add_argument("workspace", type=Path)
    reference_audit.add_argument("--output", type=Path, required=True)
    reference_audit.add_argument("--seed", type=int, default=0)
    reference_audit.add_argument("--queue-id", action="append")

    reference_binding = subparsers.add_parser(
        "failure-reference-binding",
        help="Publish terminal selected references as an immutable exact-ID overlay",
    )
    reference_binding.add_argument("audit", type=Path)
    reference_binding.add_argument("--output", type=Path, required=True)

    reference_workspace = subparsers.add_parser(
        "create-failure-reference-workspace",
        help="Preserve a workspace and attach one immutable selected-reference overlay",
    )
    reference_workspace.add_argument("base_workspace", type=Path)
    reference_workspace.add_argument("binding", type=Path)
    reference_workspace.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )

    missing_voice_reuse = subparsers.add_parser(
        "missing-voice-reuse-plan",
        help="Plan bounded existing-voice comparisons for known unbound roles",
    )
    missing_voice_reuse.add_argument("workspace", type=Path)
    missing_voice_reuse.add_argument("character")
    missing_voice_reuse.add_argument(
        "--cohort",
        action="append",
        required=True,
        help="Exact review cohort as LABEL=PORTRAIT[,PORTRAIT]",
    )
    missing_voice_reuse.add_argument(
        "--candidate-voice",
        action="append",
        required=True,
        help=(
            "Existing immutable manifest voice to compare; repeat at least twice "
            "for missing targets, or supply one or more for exact failed targets"
        ),
    )
    missing_voice_reuse.add_argument(
        "--failed-queue-id",
        action="append",
        default=None,
        help=(
            "Switch to exact failed-control mode and name one failed queue ID; "
            "repeat for additional targets"
        ),
    )
    missing_voice_reuse.add_argument(
        "--inline-pause-ms",
        type=int,
        help=(
            "Bind each failed-control candidate to the canonical MOSS inline-pause "
            "prompt at this exact duration"
        ),
    )
    missing_voice_reuse.add_argument("--output", type=Path, required=True)

    missing_voice_candidate_workspace = subparsers.add_parser(
        "missing-voice-reuse-candidate-workspace",
        help="Create one isolated candidate workspace from a reuse plan",
    )
    missing_voice_candidate_workspace.add_argument("plan", type=Path)
    missing_voice_candidate_workspace.add_argument("candidate_id")
    missing_voice_candidate_workspace.add_argument("import_directory", type=Path)
    missing_voice_candidate_workspace.add_argument(
        "--inputs-root", type=Path, required=True
    )
    missing_voice_candidate_workspace.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )

    missing_voice_candidate_command = subparsers.add_parser(
        "missing-voice-reuse-candidate-command",
        help="Validate and print an exact reuse sample generation command",
    )
    missing_voice_candidate_command.add_argument("plan", type=Path)
    missing_voice_candidate_command.add_argument("candidate_id")
    missing_voice_candidate_command.add_argument("workspace", type=Path)

    missing_voice_review = subparsers.add_parser(
        "missing-voice-reuse-review",
        help="Publish a blind candidate-by-sample reuse review",
    )
    missing_voice_review.add_argument("plan", type=Path)
    missing_voice_review.add_argument(
        "--candidate-evidence",
        action="append",
        required=True,
        help="CANDIDATE_ID=WORKSPACE; repeat for base and repair workspaces",
    )
    missing_voice_review.add_argument("--output", type=Path, required=True)
    missing_voice_review.add_argument("--seed", type=int, default=0)

    missing_voice_review_status = subparsers.add_parser(
        "missing-voice-reuse-review-status",
        help="Validate and summarize a blind missing-voice review",
    )
    missing_voice_review_status.add_argument("session", type=Path)

    missing_voice_review_ui = subparsers.add_parser(
        "missing-voice-reuse-review-ui",
        help="Open the blind missing-voice reuse review UI",
    )
    missing_voice_review_ui.add_argument("session", type=Path)

    missing_voice_binding = subparsers.add_parser(
        "missing-voice-reuse-binding",
        help="Import a completed blind review into a full-cohort manifest overlay",
    )
    missing_voice_binding.add_argument("plan", type=Path)
    missing_voice_binding.add_argument("session", type=Path)
    missing_voice_binding.add_argument("--output", type=Path, required=True)

    missing_voice_live_fallback = subparsers.add_parser(
        "missing-voice-live-fallback",
        help="Preflight or atomically authorize one audited missing-role cohort",
    )
    missing_voice_live_fallback.add_argument("workspace", type=Path)
    missing_voice_live_fallback.add_argument("authority_directory", type=Path)
    missing_voice_live_fallback.add_argument("character")
    missing_voice_live_fallback.add_argument(
        "--accept-known-role-narrator-fallback",
        action="store_true",
        help=(
            "Explicitly accept live Pocket fallback for the full known-role scope; "
            "omit for a read-only preflight"
        ),
    )

    known_role_reuse = subparsers.add_parser(
        "known-role-reuse-binding",
        help="Preflight or publish an exact known story role to existing voice binding",
    )
    known_role_reuse.add_argument("workspace", type=Path)
    known_role_reuse.add_argument("unresolved_authority_directory", type=Path)
    known_role_reuse.add_argument("source_character")
    known_role_reuse.add_argument("reuse_voice_character")
    known_role_reuse.add_argument("--output", type=Path, required=True)
    known_role_reuse.add_argument(
        "--accept-known-role-reuse",
        action="store_true",
        help=(
            "Explicitly bind every exact absent/rejected target to the selected "
            "existing character voice; omit for a read-only preflight"
        ),
    )

    portrait_alias_plan = subparsers.add_parser(
        "portrait-alias-plan",
        help="Suggest checksum-bound same-character portrait expression aliases",
    )
    portrait_alias_plan.add_argument("quality_review", type=Path)
    portrait_alias_plan.add_argument("--max-dhash-distance", type=int, default=6)
    portrait_alias_plan.add_argument("--output", type=Path, required=True)

    portrait_alias_decision = subparsers.add_parser(
        "portrait-alias-decision",
        help="Record explicit human authority over portrait alias suggestions",
    )
    portrait_alias_decision.add_argument("plan", type=Path)
    portrait_alias_decision.add_argument(
        "--accept-suggestion", action="append", required=True
    )
    portrait_alias_decision.add_argument("--output", type=Path, required=True)

    references = subparsers.add_parser(
        "reference-report",
        help="Inspect immutable objective metrics for one character's references",
    )
    references.add_argument("--voice-manifest", type=Path, required=True)
    references.add_argument("--character", required=True)

    select_reference = subparsers.add_parser(
        "select-reference",
        help="Publish a no-overwrite manifest with one explicit first reference",
    )
    select_reference.add_argument("--voice-manifest", type=Path, required=True)
    select_reference.add_argument("--character", required=True)
    select_reference.add_argument("--reference-number", type=int, required=True)
    select_reference.add_argument("--output", type=Path, required=True)

    import_reference_review = subparsers.add_parser(
        "import-reference-review",
        help="Publish an immutable variant-aware plan from extractor decisions",
    )
    import_reference_review.add_argument("--report", type=Path, required=True)
    import_reference_review.add_argument("--review", type=Path, required=True)
    import_reference_review.add_argument("--story-index", type=Path, required=True)
    import_reference_review.add_argument("--output", type=Path, required=True)

    reference_evaluation = subparsers.add_parser(
        "build-reference-evaluation",
        help="Publish fixed-corpus inputs for accepted source-reference variants",
    )
    reference_evaluation.add_argument("--plan", type=Path, required=True)
    reference_evaluation.add_argument("--output", type=Path, required=True)

    reference_listening = subparsers.add_parser(
        "build-reference-listening-reports",
        help="Publish strict reports for blind source and generated evaluation",
    )
    reference_listening.add_argument("--evaluation", type=Path, required=True)
    reference_listening.add_argument("--state", type=Path, required=True)
    reference_listening.add_argument("--output", type=Path, required=True)

    reference_bindings = subparsers.add_parser(
        "build-reference-bindings",
        help="Publish explicit queue-to-variant voice bindings",
    )
    reference_bindings.add_argument("--plan", type=Path, required=True)
    reference_bindings.add_argument("--voice-manifest", type=Path, required=True)
    reference_bindings.add_argument("--narrator-character", required=True)
    reference_bindings.add_argument(
        "--quality-review",
        type=Path,
        required=True,
        help="Completed cluster-specific review.json authorizing accepted variants",
    )
    reference_bindings.add_argument(
        "--include-base-character",
        action="append",
        default=[],
        dest="base_characters",
        help=(
            "Copy one exact character and its references from the base manifest; "
            "repeat for additional characters"
        ),
    )
    reference_bindings.add_argument("--output", type=Path, required=True)

    extend_reference_bindings = subparsers.add_parser(
        "extend-reference-bindings",
        help="Publish a lossless successor adding one reviewed source plan",
    )
    extend_reference_bindings.add_argument(
        "--base-binding-manifest", type=Path, required=True
    )
    extend_reference_bindings.add_argument("--plan", type=Path, required=True)
    extend_reference_bindings.add_argument("--quality-review", type=Path, required=True)
    extend_reference_bindings.add_argument("--narrator-character", required=True)
    extend_reference_bindings.add_argument("--output", type=Path, required=True)

    retire_reference_bindings = subparsers.add_parser(
        "retire-reference-bindings",
        help="Publish an immutable successor retiring exact source variants",
    )
    retire_reference_bindings.add_argument(
        "--base-binding-manifest", type=Path, required=True
    )
    retire_reference_bindings.add_argument(
        "--variant-id", action="append", required=True, dest="variant_ids"
    )
    retire_reference_bindings.add_argument(
        "--reason",
        choices=("real_story_quality_failure",),
        default="real_story_quality_failure",
    )
    retire_reference_bindings.add_argument("--output", type=Path, required=True)


def handle(arguments: argparse.Namespace) -> int:
    if arguments.command == "failure-reference-audit":
        result = publish_failure_reference_audit(
            arguments.workspace,
            arguments.output,
            seed=arguments.seed,
            queue_ids=arguments.queue_id,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "failure-reference-binding":
        result = publish_failure_reference_binding(arguments.audit, arguments.output)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "create-failure-reference-workspace":
        result = create_failure_reference_workspace(
            arguments.base_workspace,
            arguments.binding,
            arguments.workspaces_root,
        )
        print(
            json.dumps(
                {"workspace": str(result.directory), "created": result.created},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "missing-voice-reuse-plan":
        plan = build_missing_voice_reuse_plan(
            arguments.workspace,
            arguments.character,
            cohorts=parse_cohort_arguments(arguments.cohort),
            candidate_voice_characters=tuple(arguments.candidate_voice),
            failed_queue_ids=arguments.failed_queue_id,
            inline_pause_ms=arguments.inline_pause_ms,
        )
        write_missing_voice_reuse_plan(plan, arguments.output)
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if arguments.command == "missing-voice-reuse-candidate-workspace":
        result = prepare_missing_voice_reuse_candidate_workspace(
            load_missing_voice_reuse_plan(arguments.plan),
            arguments.candidate_id,
            arguments.import_directory,
            arguments.inputs_root,
            arguments.workspaces_root,
        )
        print(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
        return 0
    if arguments.command == "missing-voice-reuse-candidate-command":
        command = build_missing_voice_reuse_candidate_command(
            load_missing_voice_reuse_plan(arguments.plan),
            arguments.candidate_id,
            arguments.workspace,
        )
        print(json.dumps({"command": list(command)}, indent=2, sort_keys=True))
        return 0
    if arguments.command == "missing-voice-reuse-review":
        session = build_missing_voice_reuse_review(
            arguments.plan,
            parse_missing_voice_reuse_evidence(arguments.candidate_evidence),
            arguments.output,
            seed=arguments.seed,
        )
        bundle, progress = load_missing_voice_reuse_review(session)
        print(
            json.dumps(
                {
                    "session": str(session),
                    "bundle_id": bundle["bundle_id"],
                    "candidate_count": bundle["candidate_count"],
                    "cohort_count": bundle["cohort_count"],
                    "completed_count": missing_voice_reuse_review_progress(
                        bundle, progress
                    )[0],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "missing-voice-reuse-review-status":
        bundle, session = load_missing_voice_reuse_review(arguments.session)
        completed, total = missing_voice_reuse_review_progress(bundle, session)
        print(
            json.dumps(
                {
                    "bundle_id": bundle["bundle_id"],
                    "completed_count": completed,
                    "total_count": total,
                    "remaining_count": total - completed,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "missing-voice-reuse-review-ui":
        from vntts.authoring.missing_voice_reuse_review_ui import (
            launch_missing_voice_reuse_review,
        )

        return launch_missing_voice_reuse_review(arguments.session)
    if arguments.command == "missing-voice-reuse-binding":
        result = publish_missing_voice_reuse_binding(
            arguments.plan, arguments.session, arguments.output
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "missing-voice-live-fallback":
        result = authorize_missing_voice_live_fallback(
            arguments.workspace,
            arguments.authority_directory,
            arguments.character,
            accept_known_role_narrator_fallback=(
                arguments.accept_known_role_narrator_fallback
            ),
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "known-role-reuse-binding":
        result = publish_known_role_reuse_binding(
            arguments.workspace,
            arguments.unresolved_authority_directory,
            arguments.source_character,
            arguments.reuse_voice_character,
            arguments.output,
            accept_known_role_reuse=arguments.accept_known_role_reuse,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "portrait-alias-plan":
        plan = build_portrait_alias_plan(
            arguments.quality_review,
            max_dhash_distance=arguments.max_dhash_distance,
        )
        write_portrait_alias_plan(plan, arguments.output)
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if arguments.command == "portrait-alias-decision":
        decision = build_portrait_alias_decision(
            load_portrait_alias_plan(arguments.plan), arguments.accept_suggestion
        )
        write_portrait_alias_decision(decision, arguments.output)
        print(
            json.dumps(decision.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        )
        return 0
    if arguments.command == "reference-report":
        print(
            json.dumps(
                inspect_voice_reference_candidates(
                    arguments.voice_manifest, arguments.character
                ),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "select-reference":
        result = select_voice_reference(
            arguments.voice_manifest,
            arguments.character,
            arguments.reference_number,
            arguments.output,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "import-reference-review":
        result = import_source_reference_review(
            arguments.report,
            arguments.review,
            arguments.story_index,
            arguments.output,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "build-reference-evaluation":
        result = publish_source_reference_evaluation(arguments.plan, arguments.output)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "build-reference-listening-reports":
        result = publish_source_reference_listening_reports(
            arguments.evaluation, arguments.state, arguments.output
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "build-reference-bindings":
        result = publish_source_reference_bindings(
            arguments.plan,
            arguments.voice_manifest,
            arguments.narrator_character,
            None,
            arguments.output,
            quality_review=arguments.quality_review,
            base_characters=arguments.base_characters,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "extend-reference-bindings":
        result = publish_source_reference_binding_successor(
            arguments.base_binding_manifest,
            arguments.plan,
            arguments.quality_review,
            arguments.narrator_character,
            arguments.output,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    if arguments.command == "retire-reference-bindings":
        result = publish_source_reference_binding_retirement(
            arguments.base_binding_manifest,
            arguments.variant_ids,
            arguments.output,
            reason=arguments.reason,
        )
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return 0
    raise AssertionError(f"Unhandled reference command: {arguments.command}")
