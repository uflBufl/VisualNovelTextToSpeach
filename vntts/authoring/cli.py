"""Command-line entry point for offline authoring workflows."""

import argparse
import hashlib
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts import (
    StoryIndexError,
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)
from vntts_artifacts.voice_manifest import VoiceManifestError, load_voice_manifest

from vntts.authoring.audio_event_review import (
    AudioEventReviewError,
    load_audio_event_review,
    publish_source_audio_event_review,
    record_audio_event_review_decision,
)
from vntts.authoring.bulk_generation import (
    LIVE_FALLBACK_REASONS,
    BulkGenerationError,
    authorize_live_fallback,
    generation_failure_repair_plan,
    generation_failure_report,
    is_spoken_queue_item,
    load_generation_state,
    normalize_short_trailing_ellipsis,
    publish_generated_manifest,
    review_generation_item,
    run_bulk_generation,
    sha256_control_path,
)
from vntts.authoring.cohort_bundle import (
    build_cohort_review_bundle,
    execute_cohort_bundle_decision,
    load_cohort_review_bundle,
    write_cohort_review_bundle,
)
from vntts.authoring.cohort_review import (
    CohortReviewError,
    apply_cohort_review_decision,
    build_cohort_review_decision,
    build_cohort_review_plan,
    load_cohort_review_decision,
    load_cohort_review_plan,
    write_cohort_review_decision,
    write_cohort_review_plan,
)
from vntts.authoring.config_rebase import rebase_workspace_config
from vntts.authoring.delivery import (
    LEGACY_ENGLISH_POLICY,
    PRESERVE_DELIVERY_POLICY,
    DeliveryAnnotationError,
    apply_delivery_policy,
)
from vntts.authoring.failure_reference_audit import (
    FailureReferenceAuditError,
    publish_failure_reference_audit,
)
from vntts.authoring.failure_reference_binding import (
    FailureReferenceBindingError,
    publish_failure_reference_binding,
)
from vntts.authoring.failure_regeneration import (
    FailureRegenerationError,
    build_failure_regeneration_command,
    build_failure_regeneration_plan,
    load_failure_regeneration_plan,
    write_failure_regeneration_plan,
)
from vntts.authoring.failure_repair import (
    DEFAULT_INTERNAL_SILENCE_TARGET_SECONDS,
    FailureRepairPolicy,
    FailureRepairPolicyError,
)
from vntts.authoring.game_pack import FinalGamePackError, publish_final_game_pack
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
from vntts.authoring.missing_voice_policy import (
    NARRATOR_ALL_UNRESOLVED,
    NARRATOR_ROLES,
    MissingVoicePolicy,
    MissingVoicePolicyError,
)
from vntts.authoring.pending_resolution import (
    build_pending_regeneration_command,
    build_pending_resolution_plan,
    load_pending_resolution_plan,
    write_pending_resolution_plan,
)
from vntts.authoring.portrait_aliases import (
    PortraitAliasError,
    build_portrait_alias_decision,
    build_portrait_alias_plan,
    load_portrait_alias_plan,
    write_portrait_alias_decision,
    write_portrait_alias_plan,
)
from vntts.authoring.queue_builder import (
    GenerationQueueBuildError,
    inspect_generation_queue,
    publish_generation_queue,
)
from vntts.authoring.reconciliation_merge import merge_reconciled_terminal_outcomes
from vntts.authoring.reference_render_comparison import (
    ReferenceRenderComparisonError,
    create_reference_render_listening,
    import_reference_render_preference,
    load_reference_render_plan,
    publish_reference_render_comparison,
)
from vntts.authoring.reference_selection import (
    ReferenceSelectionError,
    inspect_voice_reference_candidates,
    select_voice_reference,
)
from vntts.authoring.render_hypothesis_review import (
    RenderHypothesisReviewError,
    import_accepted_render_hypothesis,
    load_render_hypothesis_review,
    publish_render_hypothesis_review,
    record_render_hypothesis_decision,
)
from vntts.authoring.robustness_asr import (
    SpeechRobustnessAsrError,
    build_speech_robustness_asr_report,
    write_speech_robustness_asr_report,
)
from vntts.authoring.robustness_corpus import (
    SpeechRobustnessCorpusError,
    load_speech_robustness_corpus,
    publish_speech_robustness_corpus,
)
from vntts.authoring.silence_comparison import (
    SilenceComparisonError,
    create_silence_comparison_session,
    load_silence_comparison,
    load_silence_comparison_input_plan,
    publish_silence_comparison,
)
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
)
from vntts.authoring.source_reference_quality import SourceReferenceQualityError
from vntts.authoring.source_reference_review import (
    SourceReferenceReviewError,
    import_source_reference_review,
    publish_source_reference_binding_retirement,
    publish_source_reference_binding_successor,
    publish_source_reference_bindings,
    publish_source_reference_evaluation,
    publish_source_reference_listening_reports,
)
from vntts.authoring.specialist_failure_plan import (
    build_specialist_failure_plan,
    write_specialist_failure_plan,
)
from vntts.authoring.terminal_conflict_resolution import (
    TerminalConflictResolutionError,
    publish_terminal_conflict_resolution,
)
from vntts.authoring.terminal_conflict_review import (
    TerminalConflictReviewError,
    carry_approved_cohort_terminal_conflict_decisions,
    carry_terminal_conflict_decisions,
)
from vntts.authoring.terminal_conflict_successor import (
    TerminalConflictSuccessorError,
    publish_terminal_conflict_successor,
)
from vntts.authoring.terminal_conflict_workspace import (
    merge_terminal_conflict_resolution,
)
from vntts.authoring.voice_quality_gate import (
    VoiceQualityGateError,
    build_voice_quality_gate,
    inspect_voice_quality_gate,
    load_voice_quality_gate,
    write_voice_quality_gate,
)
from vntts.authoring.voice_repair_comparison import (
    VoiceRepairComparisonError,
    build_voice_repair_candidate_command,
    build_voice_repair_comparison_plan,
    load_voice_repair_comparison_plan,
    prepare_voice_repair_candidate_workspace,
    write_voice_repair_comparison_plan,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    create_failure_reference_workspace,
    create_resume_workspace,
    default_workspaces_root,
    failure_reference_runtime_binding,
    generation_control_bindings,
    generation_output_identity,
    merge_workspace_outcomes,
)
from vntts.tts_benchmark import create_backend
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    synthesis_character_for_line,
)


def _load_stable_voice_registry(manifest_path):
    manifest_path = Path(manifest_path).expanduser().resolve()
    try:
        payload = manifest_path.read_bytes()
    except OSError as error:
        raise BulkGenerationError(
            f"Unable to read voice manifest {manifest_path}: {error}"
        ) from error
    digest = hashlib.sha256(payload).hexdigest()
    with TemporaryDirectory() as directory:
        snapshot = Path(directory) / "manifest.json"
        snapshot.write_bytes(payload)
        document, entries = load_voice_manifest(snapshot)
    voices = [
        CharacterVoice(
            character=entry.character,
            speaker=entry.speaker,
            aliases=entry.aliases,
            references=tuple(
                (manifest_path.parent / reference).resolve()
                for reference in entry.references
            ),
        )
        for entry in entries
    ]
    return CharacterVoiceRegistry(voices), digest, document, entries


def _producer_record(value):
    name, separator, producer_version = value.partition("=")
    if not separator or not name.strip() or not producer_version.strip():
        raise argparse.ArgumentTypeError("producer must use NAME=VERSION")
    return {"name": name.strip(), "version": producer_version.strip()}


def _vntts_version():
    try:
        return version("visual-novel-text-to-speech")
    except PackageNotFoundError:
        return "0.1.0"


def _generation_missing_voice_policy(arguments):
    try:
        if arguments.narrator_fallback_all:
            return MissingVoicePolicy(NARRATOR_ALL_UNRESOLVED)
        if arguments.narrator_fallback_roles:
            return MissingVoicePolicy(
                NARRATOR_ROLES, tuple(arguments.narrator_fallback_roles)
            )
        return MissingVoicePolicy()
    except MissingVoicePolicyError as error:
        raise BulkGenerationError(str(error)) from error


def _add_missing_voice_policy_arguments(parser):
    fallback = parser.add_mutually_exclusive_group()
    fallback.add_argument(
        "--narrator-fallback-role",
        action="append",
        dest="narrator_fallback_roles",
        help=(
            "Use Narrator only when this exact requested role still has no "
            "configured reference; repeat for multiple roles"
        ),
    )
    fallback.add_argument(
        "--narrator-fallback-all",
        action="store_true",
        help="Use Narrator for every still-unresolved named role in this exact queue",
    )


def _generation_failure_repair_policy(arguments):
    try:
        return FailureRepairPolicy(
            tuple(arguments.sentence_segment_failed or ()),
            tuple(arguments.trim_edge_silence_failed or ()),
            arguments.segment_pause_ms,
            tuple(arguments.bounded_seed_failed or ()),
            tuple(arguments.offline_fallback_failed or ()),
            tuple(arguments.inline_pause_failed or ()),
            arguments.inline_pause_ms,
        )
    except FailureRepairPolicyError as error:
        raise BulkGenerationError(str(error)) from error


def _add_failure_repair_arguments(parser):
    parser.add_argument(
        "--sentence-segment-failed",
        action="append",
        help=(
            "Repair this exact current missed-EOS or internal-silence failure "
            "at safe sentence boundaries"
        ),
    )
    parser.add_argument(
        "--trim-edge-silence-failed",
        action="append",
        help="Repair this exact current edge-only silence failure before validation",
    )
    parser.add_argument(
        "--bounded-seed-failed",
        action="append",
        help="Retry this exact current missed-EOS failure up to three total attempts",
    )
    parser.add_argument(
        "--offline-fallback-failed",
        action="append",
        help=(
            "Generate this exact carried exhausted backend failure with the "
            "config-addressed Pocket TTS fallback"
        ),
    )
    parser.add_argument(
        "--inline-pause-failed",
        action="append",
        help=(
            "Compare one exact current internal-silence failure with a derived "
            "MOSS inline pause prompt"
        ),
    )
    parser.add_argument(
        "--segment-pause-ms",
        type=int,
        default=180,
        help="Bounded silence inserted only between authorized sentence segments",
    )
    parser.add_argument(
        "--inline-pause-ms",
        type=int,
        default=180,
        help="MOSS inline pause duration for exact authorized comparison items",
    )


def create_parser():
    parser = argparse.ArgumentParser(
        description="VNTTS offline pregeneration authoring"
    )
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
    for command, help_text in (
        ("preflight-queue", "Summarize a collection-driven generation queue"),
        ("build-queue", "Publish a validated collection-driven generation queue"),
    ):
        queue = subparsers.add_parser(command, help=help_text)
        queue.add_argument("--story-index", type=Path, required=True)
        queue.add_argument("--voice-manifest", type=Path, required=True)
        queue.add_argument(
            "--collection",
            action="append",
            dest="collection_ids",
            help="Include one declared collection; repeat to include more",
        )
        queue.add_argument(
            "--unknown-action",
            choices=("resolve_audio", "manual_review"),
            help="Required policy when a selected source-audio status is unknown",
        )
        queue.add_argument(
            "--delivery-policy",
            choices=(PRESERVE_DELIVERY_POLICY, LEGACY_ENGLISH_POLICY),
            default=PRESERVE_DELIVERY_POLICY,
            help="Preserve source annotations or opt into the legacy English heuristic",
        )
        if command == "build-queue":
            queue.add_argument("--output", type=Path, required=True)
    workspace = subparsers.add_parser(
        "create-workspace",
        help="Create an immutable config-addressed resume workspace",
    )
    workspace.add_argument("import_directory", type=Path)
    workspace.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    workspace.add_argument("--story-index", type=Path)
    workspace.add_argument("--voice-manifest", type=Path)
    workspace.add_argument("--narrator-character")
    workspace.add_argument(
        "--backend", choices=("pocket-tts", "chatterbox-nano", "moss-tts")
    )
    workspace.add_argument("--model")
    workspace.add_argument("--generation-profile")
    workspace.add_argument("--carry-forward-from", type=Path)
    workspace.add_argument(
        "--carry-forward-character", action="append", dest="carry_forward_characters"
    )
    _add_missing_voice_policy_arguments(workspace)
    _add_failure_repair_arguments(workspace)
    merge = subparsers.add_parser(
        "merge-workspace-outcomes",
        help="Create a successor from exact reviewed repair outcomes",
    )
    merge.add_argument("base_workspace", type=Path)
    merge.add_argument(
        "--source-workspace",
        action="append",
        dest="source_workspaces",
        type=Path,
        required=True,
    )
    merge.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    reconciled_merge = subparsers.add_parser(
        "merge-reconciled-outcomes",
        help="Create a successor from exact terminal outcomes in a reconciliation",
    )
    reconciled_merge.add_argument("base_workspace", type=Path)
    reconciled_merge.add_argument("reconciliation", type=Path)
    reconciled_merge.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    config_rebase = subparsers.add_parser(
        "rebase-workspace-config",
        help="Carry exact terminal decisions onto one additive immutable config",
    )
    config_rebase.add_argument("source_workspace", type=Path)
    config_rebase.add_argument("target_workspace", type=Path)
    config_rebase.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    terminal_resolution = subparsers.add_parser(
        "terminal-conflict-resolution",
        help="Publish immutable completed terminal-conflict decisions",
    )
    terminal_resolution.add_argument("review_directory", type=Path)
    terminal_resolution.add_argument("output", type=Path)
    terminal_carry = subparsers.add_parser(
        "terminal-conflict-carry",
        help="Carry unchanged completed decisions into a refreshed review",
    )
    terminal_carry.add_argument("source_review_directory", type=Path)
    terminal_carry.add_argument("target_review_directory", type=Path)
    terminal_cohort_carry = subparsers.add_parser(
        "terminal-conflict-cohort-carry",
        help="Carry exact approved cohort decisions into a current conflict review",
    )
    terminal_cohort_carry.add_argument("review_directory", type=Path)
    terminal_successor = subparsers.add_parser(
        "terminal-conflict-successor",
        help="Publish a resolution-aware reconciliation successor",
    )
    terminal_successor.add_argument("reconciliation", type=Path)
    terminal_successor.add_argument("resolution_directory", type=Path)
    terminal_successor.add_argument("output", type=Path)
    terminal_merge = subparsers.add_parser(
        "terminal-conflict-merge",
        help="Create a config-addressed workspace from terminal decisions",
    )
    terminal_merge.add_argument("base_workspace", type=Path)
    terminal_merge.add_argument("successor_directory", type=Path)
    terminal_merge.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    reference_workspace = subparsers.add_parser(
        "create-failure-reference-workspace",
        help="Preserve a workspace and attach one immutable selected-reference overlay",
    )
    reference_workspace.add_argument("base_workspace", type=Path)
    reference_workspace.add_argument("binding", type=Path)
    reference_workspace.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    generate = subparsers.add_parser(
        "generate", help="Resume typed device-independent generation from a queue"
    )
    generate.add_argument("--queue", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument(
        "--workspace",
        type=Path,
        help="Canonical config-addressed workspace that binds this child run",
    )
    generate.add_argument("--voice-manifest", type=Path, required=True)
    generate.add_argument(
        "--backend",
        required=True,
        choices=("pocket-tts", "chatterbox-nano", "moss-tts"),
    )
    generate.add_argument("--model")
    generate.add_argument(
        "--narrator-character",
        default="Narrator",
        help="Manifest character whose first reference voices queue Narrator lines",
    )
    generate.add_argument("--generation-profile")
    _add_missing_voice_policy_arguments(generate)
    _add_failure_repair_arguments(generate)
    generate.add_argument("--limit", type=int)
    generate.add_argument("--retries", type=int, default=2)
    generate.add_argument("--seed", type=int, default=0)
    generate.add_argument(
        "--capture-silence-failure",
        type=Path,
        help=(
            "Publish one selected, one-attempt speech-silence rejection as "
            "non-reviewable checksum-bound evidence outside generated output"
        ),
    )
    generate.add_argument("--include-prefer-source", action="store_true")
    generate.add_argument("--character", action="append", dest="characters")
    generate.add_argument(
        "--regenerate-existing",
        action="store_true",
        help=(
            "Re-render existing pending-review outcomes only within an explicit "
            "--character or --queue-id scope; approved/rejected items remain protected"
        ),
    )
    generate.add_argument(
        "--queue-id",
        action="append",
        dest="queue_ids",
        help="Generate only one exact queue ID; repeat for a focused retry",
    )
    review = subparsers.add_parser(
        "review", help="Approve or reject one generated queue item"
    )
    review.add_argument("--state", type=Path, required=True)
    review.add_argument("queue_id")
    review.add_argument("decision", choices=("approved", "rejected"))
    live_fallback = subparsers.add_parser(
        "live-fallback",
        help="Authorize exact terminal Pocket live synthesis for one queue item",
    )
    live_fallback.add_argument("--state", type=Path, required=True)
    live_fallback.add_argument("--queue", type=Path, required=True)
    live_fallback.add_argument("queue_id")
    live_fallback.add_argument(
        "--reason", choices=tuple(sorted(LIVE_FALLBACK_REASONS)), required=True
    )
    live_fallback.add_argument("--provider", default="pocket-tts")
    live_fallback.add_argument("--model", required=True)
    live_fallback.add_argument("--generation-profile", default="default")
    publish = subparsers.add_parser(
        "publish", help="Rebuild the approved-only manifest from generation state"
    )
    publish.add_argument("--state", type=Path, required=True)
    status = subparsers.add_parser("status", help="Inspect resumable generation state")
    status.add_argument("--state", type=Path, required=True)
    status.add_argument("--queue", type=Path)
    audio_event_publish = subparsers.add_parser(
        "audio-event-review-publish",
        help="Publish one immutable source-backed non-verbal event review",
    )
    audio_event_publish.add_argument("queue", type=Path)
    audio_event_publish.add_argument("queue_id")
    audio_event_publish.add_argument("source_story_index", type=Path)
    audio_event_publish.add_argument("audio", type=Path)
    audio_event_publish.add_argument("--output", type=Path, required=True)
    audio_event_publish.add_argument("--source-line-id", required=True)
    audio_event_publish.add_argument("--source-speaker", required=True)
    audio_event_publish.add_argument("--source-event", required=True)
    audio_event_publish.add_argument("--source-bank", required=True)
    audio_event_publish.add_argument("--source-media-id", type=int, required=True)
    audio_event_publish.add_argument("--source-audio-id", required=True)
    audio_event_decide = subparsers.add_parser(
        "audio-event-review-decide",
        help="Record one terminal accept/reject audio-event decision",
    )
    audio_event_decide.add_argument("directory", type=Path)
    audio_event_decide.add_argument("decision", choices=("accept", "reject"))
    audio_event_status = subparsers.add_parser(
        "audio-event-review-status",
        help="Validate and inspect one audio-event review",
    )
    audio_event_status.add_argument("directory", type=Path)
    render_hypothesis_publish = subparsers.add_parser(
        "render-hypothesis-review-publish",
        help="Publish one immutable unmatched render/reference review",
    )
    render_hypothesis_publish.add_argument("comparison", type=Path)
    render_hypothesis_publish.add_argument("queue_id")
    render_hypothesis_publish.add_argument("arm_id")
    render_hypothesis_publish.add_argument("--output", type=Path, required=True)
    render_hypothesis_decide = subparsers.add_parser(
        "render-hypothesis-review-decide",
        help="Accept one exact render hypothesis or require a different one",
    )
    render_hypothesis_decide.add_argument("directory", type=Path)
    render_hypothesis_decide.add_argument(
        "decision", choices=("accept_hypothesis", "need_different")
    )
    render_hypothesis_status = subparsers.add_parser(
        "render-hypothesis-review-status",
        help="Validate and inspect one unmatched render/reference review",
    )
    render_hypothesis_status.add_argument("directory", type=Path)
    render_hypothesis_import = subparsers.add_parser(
        "render-hypothesis-review-import",
        help="Bind one accepted render hypothesis to one fresh exact audit",
    )
    render_hypothesis_import.add_argument("audit", type=Path)
    render_hypothesis_import.add_argument("comparison", type=Path)
    render_hypothesis_import.add_argument("review", type=Path)
    render_hypothesis_import.add_argument("queue_id")
    failures = subparsers.add_parser(
        "failure-report",
        help="Group failed generation outcomes into stable typed cohorts",
    )
    failures.add_argument("--state", type=Path, required=True)
    failures.add_argument("--queue", type=Path, required=True)
    robustness_corpus = subparsers.add_parser(
        "speech-robustness-corpus",
        help="Publish immutable human-labelled WAV and typed failure evidence",
    )
    robustness_corpus.add_argument("output", type=Path)
    robustness_corpus.add_argument(
        "--decision-root",
        action="append",
        type=Path,
        required=True,
        help="Cohort decision file or directory; repeat as needed",
    )
    robustness_corpus.add_argument(
        "--failure-workspace",
        action="append",
        type=Path,
        default=[],
        help="Stable workspace whose typed failed items should be included",
    )
    robustness_check = subparsers.add_parser(
        "speech-robustness-check",
        help="Validate a published speech robustness corpus and every artifact",
    )
    robustness_check.add_argument("directory", type=Path)
    robustness_asr = subparsers.add_parser(
        "speech-robustness-asr",
        help="Compare a v2 robustness corpus with one local ASR model",
    )
    robustness_asr.add_argument("corpus", type=Path)
    robustness_asr.add_argument("model", type=Path)
    robustness_asr.add_argument("--output", type=Path, required=True)
    robustness_asr.add_argument("--device", default="cpu")
    robustness_asr.add_argument(
        "--progress",
        type=Path,
        help="Checksum-bound resumable per-sample progress document",
    )
    specialist_failures = subparsers.add_parser(
        "specialist-failure-plan",
        help="Cluster terminal repair failures into checksum-bound next actions",
    )
    specialist_failures.add_argument(
        "--workspace", action="append", type=Path, required=True
    )
    specialist_failures.add_argument("--output", type=Path)
    reference_audit = subparsers.add_parser(
        "failure-reference-audit",
        help="Publish a blinded exact-reference audit for speech-quality failures",
    )
    reference_audit.add_argument("workspace", type=Path)
    reference_audit.add_argument("--output", type=Path, required=True)
    reference_audit.add_argument("--seed", type=int, default=0)
    reference_audit.add_argument("--queue-id", action="append")
    reference_render = subparsers.add_parser(
        "failure-reference-render-comparison",
        help="Render an immutable comparison from exact failed-reference arms",
    )
    reference_render.add_argument("plan", type=Path)
    reference_render.add_argument("--output", type=Path, required=True)
    reference_render_listen = subparsers.add_parser(
        "failure-reference-render-session",
        help="Create a blind session for complete matched reference renders",
    )
    reference_render_listen.add_argument("comparison", type=Path)
    reference_render_listen.add_argument("--output", type=Path, required=True)
    reference_render_listen.add_argument("--seed", type=int, default=0)
    reference_render_listen.add_argument(
        "--arm-id",
        action="append",
        help="Select exactly two complete comparison arms without rerendering",
    )
    reference_render_import = subparsers.add_parser(
        "failure-reference-import-listening",
        help="Bind one completed blind reference preference to a fresh audit",
    )
    reference_render_import.add_argument("audit", type=Path)
    reference_render_import.add_argument("comparison", type=Path)
    reference_render_import.add_argument("session", type=Path)
    reference_render_import.add_argument("queue_id")
    reference_binding = subparsers.add_parser(
        "failure-reference-binding",
        help="Publish terminal selected references as an immutable exact-ID overlay",
    )
    reference_binding.add_argument("audit", type=Path)
    reference_binding.add_argument("--output", type=Path, required=True)
    voice_gate = subparsers.add_parser(
        "voice-quality-gate",
        help="Publish a reusable accepted voice-control quality gate",
    )
    voice_gate.add_argument("workspace", type=Path)
    voice_gate.add_argument("plan", type=Path)
    voice_gate.add_argument("decision", type=Path)
    voice_gate.add_argument("--output", type=Path, required=True)
    voice_gate_check = subparsers.add_parser(
        "voice-quality-check",
        help="Compare one later pending item with a reusable voice-quality gate",
    )
    voice_gate_check.add_argument("gate", type=Path)
    voice_gate_check.add_argument("workspace", type=Path)
    voice_gate_check.add_argument("queue_id")
    voice_repair = subparsers.add_parser(
        "voice-repair-comparison-plan",
        help="Plan a checksum-bound profile comparison for one unresolved voice",
    )
    voice_repair.add_argument("workspace", type=Path)
    voice_repair.add_argument("character")
    voice_repair.add_argument(
        "--generation-profile",
        action="append",
        dest="generation_profiles",
        help="Bounded profile to compare; repeat for each candidate",
    )
    voice_repair.add_argument("--output", type=Path, required=True)
    voice_repair_workspace = subparsers.add_parser(
        "voice-repair-candidate-workspace",
        help="Create one self-contained candidate workspace from an immutable plan",
    )
    voice_repair_workspace.add_argument("plan", type=Path)
    voice_repair_workspace.add_argument("candidate_id")
    voice_repair_workspace.add_argument("import_directory", type=Path)
    voice_repair_workspace.add_argument("--inputs-root", type=Path, required=True)
    voice_repair_workspace.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    voice_repair_command = subparsers.add_parser(
        "voice-repair-candidate-command",
        help="Validate and print one exact sample-only generation command",
    )
    voice_repair_command.add_argument("plan", type=Path)
    voice_repair_command.add_argument("candidate_id")
    voice_repair_command.add_argument("workspace", type=Path)
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
    repairs = subparsers.add_parser(
        "failure-repair-plan",
        help="Plan exact-ID bounded repairs without changing generation state",
    )
    repairs.add_argument("--state", type=Path, required=True)
    repairs.add_argument("--queue", type=Path, required=True)
    silence_publish = subparsers.add_parser(
        "silence-comparison-publish",
        help="Publish a checksum-bound segmentation/compression comparison",
    )
    silence_publish.add_argument("plan", type=Path)
    silence_publish.add_argument("--output", type=Path, required=True)
    silence_publish.add_argument(
        "--target-seconds",
        type=float,
        default=DEFAULT_INTERNAL_SILENCE_TARGET_SECONDS,
        help="Silent boundary retained in the comparison-only compressed candidate",
    )
    silence_check = subparsers.add_parser(
        "silence-comparison-check",
        help="Validate a published comparison and every bound artifact",
    )
    silence_check.add_argument("comparison", type=Path)
    silence_session = subparsers.add_parser(
        "silence-comparison-session",
        help="Create a blinded A/B session from a verified comparison",
    )
    silence_session.add_argument("comparison", type=Path)
    silence_session.add_argument("--output", type=Path, required=True)
    silence_session.add_argument("--seed", type=int, default=0)
    cohort_review = subparsers.add_parser(
        "cohort-review-plan",
        help="Plan checksum-bound technical-attention and clean review samples",
    )
    cohort_review.add_argument("workspace", type=Path)
    cohort_review.add_argument(
        "--clean-samples-per-bucket",
        type=int,
        default=1,
        help="Deterministic clean samples for each short/medium/long bucket",
    )
    cohort_review.add_argument(
        "--output",
        type=Path,
        help="Publish the immutable plan without replacing an existing file",
    )
    cohort_review.add_argument(
        "--queue-id",
        action="append",
        default=None,
        dest="queue_ids",
        help="Restrict the plan to one exact pending queue ID; repeat as needed",
    )
    cohort_bundle = subparsers.add_parser(
        "cohort-review-bundle",
        help="Plan one checksum-bound review inventory across workspaces",
    )
    cohort_bundle.add_argument(
        "--workspace",
        action="append",
        type=Path,
        required=True,
        help="Immutable source workspace; repeat for each source",
    )
    cohort_bundle.add_argument(
        "--clean-samples-per-bucket",
        type=int,
        default=1,
        help="Deterministic clean samples for each short/medium/long bucket",
    )
    cohort_bundle.add_argument("--output", type=Path)
    cohort_bundle_apply = subparsers.add_parser(
        "cohort-review-bundle-apply",
        help="Apply one exact source-local cohort decision from a bundle",
    )
    cohort_bundle_apply.add_argument("bundle", type=Path)
    cohort_bundle_apply.add_argument("workspace_id")
    cohort_bundle_apply.add_argument("cohort_id")
    cohort_bundle_apply.add_argument(
        "decision", choices=("accepted", "rejected", "split", "expand")
    )
    cohort_bundle_apply.add_argument("--reviewed-queue-id", action="append", default=[])
    cohort_bundle_apply.add_argument("--bad-queue-id", action="append", default=[])
    cohort_bundle_apply.add_argument("--next-clean-samples-per-bucket", type=int)
    pending_resolution = subparsers.add_parser(
        "pending-resolution-plan",
        help="Bind cohort-blocked pending WAVs to conservative next actions",
    )
    pending_resolution.add_argument("workspace", type=Path)
    pending_resolution.add_argument(
        "--output",
        type=Path,
        help="Publish the immutable plan without replacing an existing file",
    )
    pending_regeneration = subparsers.add_parser(
        "pending-regeneration-command",
        help="Print one bounded exact-ID command from a current pending plan",
    )
    pending_regeneration.add_argument("workspace", type=Path)
    pending_regeneration.add_argument("plan", type=Path)
    pending_regeneration.add_argument("--batch-index", type=int, required=True)
    pending_regeneration.add_argument("--batch-size", type=int, default=10)
    failure_regeneration = subparsers.add_parser(
        "failure-regeneration-plan",
        help="Bind provenance-unbound failures to exact-ID regeneration",
    )
    failure_regeneration.add_argument("workspace", type=Path)
    failure_regeneration.add_argument("--output", type=Path)
    failure_command = subparsers.add_parser(
        "failure-regeneration-command",
        help="Print one bounded exact-ID command from a current failure plan",
    )
    failure_command.add_argument("workspace", type=Path)
    failure_command.add_argument("plan", type=Path)
    failure_command.add_argument("--batch-index", type=int, required=True)
    failure_command.add_argument("--batch-size", type=int, default=10)
    cohort_decision = subparsers.add_parser(
        "cohort-review-decision",
        help="Record an immutable human decision over one exact cohort sample",
    )
    cohort_decision.add_argument("plan", type=Path)
    cohort_decision.add_argument("cohort_id")
    cohort_decision.add_argument(
        "decision", choices=("accepted", "rejected", "split", "expand")
    )
    cohort_decision.add_argument(
        "--reviewed-queue-id",
        action="append",
        default=[],
        help="Exact sampled queue ID actually reviewed; repeat for each WAV",
    )
    cohort_decision.add_argument(
        "--bad-queue-id",
        action="append",
        default=[],
        help="Reviewed queue ID marked bad; repeat as needed",
    )
    cohort_decision.add_argument("--next-clean-samples-per-bucket", type=int)
    cohort_decision.add_argument("--output", type=Path, required=True)
    cohort_apply = subparsers.add_parser(
        "cohort-review-apply",
        help="Atomically project one recorded terminal cohort decision",
    )
    cohort_apply.add_argument("workspace", type=Path)
    cohort_apply.add_argument("plan", type=Path)
    cohort_apply.add_argument("decision", type=Path)
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
    pack = subparsers.add_parser(
        "publish-pack", help="Atomically publish a fully verified final game pack"
    )
    pack.add_argument("--state", type=Path, required=True)
    pack.add_argument("--queue", type=Path, required=True)
    pack.add_argument("--story-index", type=Path, required=True)
    pack.add_argument("--voice-manifest", type=Path, required=True)
    pack.add_argument(
        "--failure-reference-binding",
        type=Path,
        help="Exact immutable selected-reference binding used by mixed provenance state",
    )
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--game-id")
    pack.add_argument("--game-version", required=True)
    pack.add_argument(
        "--producer",
        action="append",
        type=_producer_record,
        help="Producer identity as NAME=VERSION; repeat for upstream producers",
    )
    annotate = subparsers.add_parser(
        "annotate-delivery",
        help="Print one provenance-marked legacy English delivery annotation",
    )
    annotate.add_argument("--text", required=True)
    annotate.add_argument("--speaker", default="Narrator")
    annotate.add_argument("--previous-text")
    annotate.add_argument("--next-text")
    annotate.add_argument("--kind", default="dialogue")
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
        if arguments.command == "annotate-delivery":
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
        if arguments.command == "create-workspace":
            missing_voice_policy = _generation_missing_voice_policy(arguments)
            failure_repair_policy = _generation_failure_repair_policy(arguments)
            result = create_resume_workspace(
                arguments.import_directory,
                arguments.workspaces_root,
                story_index=arguments.story_index,
                voice_manifest=arguments.voice_manifest,
                narrator_character=arguments.narrator_character,
                backend=arguments.backend,
                model=arguments.model,
                generation_profile=arguments.generation_profile,
                missing_voice_policy=missing_voice_policy,
                failure_repair_policy=failure_repair_policy,
                carry_forward_from=arguments.carry_forward_from,
                carry_forward_characters=arguments.carry_forward_characters,
            )
            print(
                json.dumps(
                    {
                        "directory": str(result.directory),
                        "created": result.created,
                        "missing_voice_policy": missing_voice_policy.to_document(),
                        "failure_repair_policy": failure_repair_policy.to_document(),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "merge-workspace-outcomes":
            result = merge_workspace_outcomes(
                arguments.base_workspace,
                arguments.source_workspaces,
                arguments.workspaces_root,
            )
            print(
                json.dumps(
                    {
                        "directory": str(result.directory),
                        "created": result.created,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "merge-reconciled-outcomes":
            result = merge_reconciled_terminal_outcomes(
                arguments.base_workspace,
                arguments.reconciliation,
                arguments.workspaces_root,
            )
            print(
                json.dumps(
                    {
                        "directory": str(result.directory),
                        "created": result.created,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "rebase-workspace-config":
            result = rebase_workspace_config(
                arguments.source_workspace,
                arguments.target_workspace,
                arguments.workspaces_root,
            )
            print(
                json.dumps(
                    {"directory": str(result.directory), "created": result.created},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "terminal-conflict-resolution":
            result = publish_terminal_conflict_resolution(
                arguments.review_directory, arguments.output
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "terminal-conflict-carry":
            progress = carry_terminal_conflict_decisions(
                arguments.source_review_directory,
                arguments.target_review_directory,
            )
            print(json.dumps(progress, indent=2, sort_keys=True))
            return 0
        if arguments.command == "terminal-conflict-cohort-carry":
            progress = carry_approved_cohort_terminal_conflict_decisions(
                arguments.review_directory
            )
            print(json.dumps(progress, indent=2, sort_keys=True))
            return 0
        if arguments.command == "terminal-conflict-successor":
            result = publish_terminal_conflict_successor(
                arguments.reconciliation,
                arguments.resolution_directory,
                arguments.output,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "terminal-conflict-merge":
            result = merge_terminal_conflict_resolution(
                arguments.base_workspace,
                arguments.successor_directory,
                arguments.workspaces_root,
            )
            print(
                json.dumps(
                    {"directory": str(result.directory), "created": result.created},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "generate":
            missing_voice_policy = _generation_missing_voice_policy(arguments)
            failure_repair_policy = _generation_failure_repair_policy(arguments)
            voice_manifest = arguments.voice_manifest.expanduser().resolve()
            expected_workspace_controls = None
            workspace_output_identity = None
            if arguments.workspace is not None:
                try:
                    expected_workspace_controls = generation_control_bindings(
                        arguments.workspace,
                        queue=arguments.queue,
                        output=arguments.output,
                        voice_manifest=voice_manifest,
                        backend=arguments.backend,
                        model=arguments.model,
                        generation_profile=arguments.generation_profile,
                        narrator_character=arguments.narrator_character,
                        missing_voice_policy=missing_voice_policy,
                        failure_repair_policy=failure_repair_policy,
                    )
                    workspace_output_identity = generation_output_identity(
                        arguments.workspace
                    )
                except AuthoringWorkbenchError as error:
                    raise BulkGenerationError(str(error)) from error
            (
                registry,
                voice_manifest_sha256,
                voice_manifest_document,
                voice_manifest_entries,
            ) = _load_stable_voice_registry(voice_manifest)
            runtime_reference_binding = (
                failure_reference_runtime_binding(arguments.workspace)
                if arguments.workspace is not None
                else None
            )
            if runtime_reference_binding is not None:
                registry = CharacterVoiceRegistry(
                    (*registry.unique_voices(), *runtime_reference_binding.voices)
                )
            try:
                policy_queue = VoiceGenerationQueue.load(arguments.queue)
            except VoiceGenerationQueueError as error:
                raise BulkGenerationError(str(error)) from error
            synthesis_character_overrides = {}
            try:
                queue_voice_overrides = queue_voice_overrides_from_manifest(
                    voice_manifest_document,
                    queue_ids=(item.queue_id for item in policy_queue.items),
                    voices=voice_manifest_entries,
                )
            except SourceReferenceBindingError as error:
                raise BulkGenerationError(str(error)) from error
            if runtime_reference_binding is not None:
                queue_voice_overrides = {
                    **queue_voice_overrides,
                    **runtime_reference_binding.queue_voice_overrides,
                }
            for item in policy_queue.items:
                requested = synthesis_character_for_line(
                    item.speaker, item.voice_character
                )
                voice = registry.resolve(requested)
                if (
                    requested != "Narrator"
                    and (
                        voice is None
                        or not voice.references
                        or any(
                            not reference.is_file() for reference in voice.references
                        )
                    )
                    and missing_voice_policy.applies_to(requested)
                ):
                    synthesis_character_overrides[requested] = "Narrator"
            if (
                expected_workspace_controls is not None
                and expected_workspace_controls.get(voice_manifest)
                != voice_manifest_sha256
            ):
                raise BulkGenerationError(
                    "Workspace voice manifest changed before backend construction"
                )
            control_files = {"voice_manifest": (voice_manifest, voice_manifest_sha256)}
            for index, reference in enumerate(
                sorted(
                    {
                        path.resolve()
                        for voice in registry.unique_voices()
                        for path in voice.references
                    },
                    key=str,
                ),
                start=1,
            ):
                reference_sha256 = sha256_control_path(reference)
                if (
                    expected_workspace_controls is not None
                    and expected_workspace_controls.get(reference) != reference_sha256
                ):
                    raise BulkGenerationError(
                        f"Workspace voice reference changed: {reference}"
                    )
                control_files[f"voice_reference:{index:04d}"] = (
                    reference,
                    reference_sha256,
                )
            if runtime_reference_binding is not None:
                binding_path = (
                    runtime_reference_binding.directory / "binding.json"
                ).resolve()
                control_files["failure_reference_binding"] = (
                    binding_path,
                    runtime_reference_binding.controls[binding_path],
                )
                selected_paths = sorted(
                    (
                        path
                        for path in runtime_reference_binding.controls
                        if path != binding_path
                    ),
                    key=str,
                )
                for index, path in enumerate(selected_paths, start=1):
                    control_files[f"failure_reference_selected:{index:04d}"] = (
                        path,
                        runtime_reference_binding.controls[path],
                    )
            if expected_workspace_controls is not None:
                observed_paths = {
                    Path(value[0]).resolve() for value in control_files.values()
                }
                if observed_paths != set(expected_workspace_controls):
                    raise BulkGenerationError(
                        "Workspace voice control inventory differs from the manifest"
                    )
            if arguments.model:
                model_path = Path(arguments.model).expanduser()
                if model_path.exists():
                    model_path = model_path.resolve()
                    control_files["model_artifact"] = (
                        model_path,
                        sha256_control_path(model_path),
                    )
            narrator_voice = registry.resolve(arguments.narrator_character)
            narrator_reference = (
                narrator_voice.references[0]
                if narrator_voice is not None and narrator_voice.references
                else None
            )
            if narrator_reference is not None:
                control_files[f"narrator_selection:{arguments.narrator_character}"] = (
                    narrator_reference,
                    sha256_control_path(narrator_reference),
                )
            if arguments.backend == "moss-tts" and narrator_reference is None:
                raise BulkGenerationError(
                    f"Narrator voice {arguments.narrator_character!r} has no reference"
                )

            def ready_spoken_item(item):
                if not is_spoken_queue_item(item):
                    return False
                requested = synthesis_character_for_line(
                    item.speaker, item.voice_character
                )
                character = queue_voice_overrides.get(
                    item.queue_id,
                    synthesis_character_overrides.get(requested, requested),
                )
                if character == "Narrator":
                    return narrator_reference is not None
                voice = registry.resolve(character)
                return voice is not None and bool(voice.references)

            with TemporaryDirectory() as cache_directory:
                backend = create_backend(
                    arguments.backend,
                    registry,
                    cache_directory,
                    model_name=arguments.model,
                    narrator_reference=narrator_reference,
                )
                try:
                    profile = arguments.generation_profile or getattr(
                        backend, "generation_profile", "stable"
                    )
                    model = arguments.model or str(
                        getattr(backend, "model_name", arguments.backend)
                    )
                    result = run_bulk_generation(
                        arguments.queue,
                        arguments.output,
                        backend,
                        provider=arguments.backend,
                        model=model,
                        generation_profile=profile,
                        limit=arguments.limit,
                        retries=arguments.retries,
                        include_prefer_source=arguments.include_prefer_source,
                        include_characters=arguments.characters,
                        include_queue_ids=arguments.queue_ids,
                        regenerate_existing=arguments.regenerate_existing,
                        item_filter=ready_spoken_item,
                        seed=arguments.seed,
                        control_files=control_files,
                        text_transform=(
                            normalize_short_trailing_ellipsis
                            if arguments.backend == "moss-tts"
                            else None
                        ),
                        text_transform_id=(
                            "short-trailing-ellipsis-v1"
                            if arguments.backend == "moss-tts"
                            else None
                        ),
                        workspace_output_identity=workspace_output_identity,
                        synthesis_character_overrides=synthesis_character_overrides,
                        queue_voice_overrides=queue_voice_overrides,
                        missing_voice_policy=missing_voice_policy.to_document(),
                        narrator_character=arguments.narrator_character,
                        failure_repair_policy=failure_repair_policy.to_document(),
                        silence_failure_evidence=arguments.capture_silence_failure,
                    )
                finally:
                    stop = getattr(backend, "stop", None)
                    if callable(stop):
                        stop()
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "review":
            review_generation_item(
                arguments.state, arguments.queue_id, arguments.decision
            )
            print(
                json.dumps(
                    {
                        "queue_id": arguments.queue_id,
                        "decision": arguments.decision,
                        "state": str(arguments.state.expanduser().resolve()),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "live-fallback":
            decision = authorize_live_fallback(
                arguments.state,
                arguments.queue,
                arguments.queue_id,
                reason=arguments.reason,
                provider=arguments.provider,
                model=arguments.model,
                generation_profile=arguments.generation_profile,
            )
            print(json.dumps(decision, indent=2, sort_keys=True))
            return 0
        if arguments.command == "publish":
            manifest = publish_generated_manifest(arguments.state)
            print(json.dumps({"manifest": str(manifest)}, indent=2, sort_keys=True))
            return 0
        if arguments.command == "status":
            state = load_generation_state(arguments.state, arguments.queue)
            counts = {
                "failed": 0,
                "generated": 0,
                "approved": 0,
                "live_fallback": 0,
            }
            for item in state["items"].values():
                counts[item["status"]] += 1
            print(
                json.dumps(
                    {
                        **counts,
                        "active": state.get("active"),
                        "queue_sha256": state["queue_sha256"],
                        "schema": state["schema"],
                        "state": str(arguments.state.expanduser().resolve()),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "failure-report":
            print(
                json.dumps(
                    generation_failure_report(arguments.state, arguments.queue),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "audio-event-review-publish":
            result = publish_source_audio_event_review(
                arguments.queue,
                arguments.queue_id,
                arguments.source_story_index,
                arguments.audio,
                arguments.output,
                source_line_id=arguments.source_line_id,
                source_speaker=arguments.source_speaker,
                source_event=arguments.source_event,
                source_bank=arguments.source_bank,
                source_media_id=arguments.source_media_id,
                source_audio_id=arguments.source_audio_id,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "audio-event-review-decide":
            result = record_audio_event_review_decision(
                arguments.directory, arguments.decision
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "audio-event-review-status":
            result = load_audio_event_review(arguments.directory)
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "render-hypothesis-review-publish":
            result = publish_render_hypothesis_review(
                arguments.comparison,
                arguments.queue_id,
                arguments.arm_id,
                arguments.output,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "render-hypothesis-review-decide":
            result = record_render_hypothesis_decision(
                arguments.directory, arguments.decision
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "render-hypothesis-review-status":
            result = load_render_hypothesis_review(arguments.directory)
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "render-hypothesis-review-import":
            result = import_accepted_render_hypothesis(
                arguments.audit,
                arguments.comparison,
                arguments.review,
                arguments.queue_id,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "speech-robustness-corpus":
            result = publish_speech_robustness_corpus(
                arguments.decision_root,
                arguments.failure_workspace,
                arguments.output,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "speech-robustness-check":
            corpus = load_speech_robustness_corpus(arguments.directory)
            print(
                json.dumps(
                    {
                        "directory": str(corpus.directory),
                        "corpus_id": corpus.corpus_id,
                        "sample_count": corpus.sample_count,
                        "failure_count": corpus.failure_count,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "speech-robustness-asr":
            corpus_root = arguments.corpus.expanduser().resolve()
            output = arguments.output.expanduser().resolve()
            try:
                output.relative_to(corpus_root)
            except ValueError:
                pass
            else:
                raise SpeechRobustnessAsrError(
                    "ASR report must be outside the immutable corpus directory"
                )
            report = build_speech_robustness_asr_report(
                arguments.corpus,
                arguments.model,
                device=arguments.device,
                progress_path=(
                    arguments.progress
                    if arguments.progress is not None
                    else arguments.output.with_suffix(
                        arguments.output.suffix + ".progress.json"
                    )
                ),
            )
            write_speech_robustness_asr_report(report, output)
            print(
                json.dumps(
                    {
                        "output": str(output),
                        "report_id": report.report_id,
                        "summary": report.document["summary"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "specialist-failure-plan":
            plan = build_specialist_failure_plan(arguments.workspace)
            if arguments.output is not None:
                write_specialist_failure_plan(plan, arguments.output)
            print(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            )
            return 0
        if arguments.command == "failure-reference-audit":
            result = publish_failure_reference_audit(
                arguments.workspace,
                arguments.output,
                seed=arguments.seed,
                queue_ids=arguments.queue_id,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "failure-reference-render-comparison":
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
        if arguments.command == "failure-reference-render-session":
            session = create_reference_render_listening(
                arguments.comparison,
                arguments.output,
                seed=arguments.seed,
                arm_ids=arguments.arm_id,
            )
            print(
                json.dumps(
                    {
                        "comparison": str(arguments.comparison),
                        "session": str(session),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "failure-reference-import-listening":
            result = import_reference_render_preference(
                arguments.audit,
                arguments.comparison,
                arguments.session,
                arguments.queue_id,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "failure-reference-binding":
            result = publish_failure_reference_binding(
                arguments.audit,
                arguments.output,
            )
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
                    {
                        "workspace": str(result.directory),
                        "created": result.created,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "voice-quality-gate":
            gate = build_voice_quality_gate(
                arguments.workspace,
                load_cohort_review_plan(arguments.plan),
                load_cohort_review_decision(arguments.decision),
            )
            write_voice_quality_gate(gate, arguments.output)
            print(json.dumps(gate.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "voice-quality-check":
            result = inspect_voice_quality_gate(
                load_voice_quality_gate(arguments.gate),
                arguments.workspace,
                arguments.queue_id,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "voice-repair-comparison-plan":
            plan = build_voice_repair_comparison_plan(
                arguments.workspace,
                arguments.character,
                generation_profiles=(
                    ("stable", "natural")
                    if arguments.generation_profiles is None
                    else tuple(arguments.generation_profiles)
                ),
            )
            write_voice_repair_comparison_plan(plan, arguments.output)
            print(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            )
            return 0
        if arguments.command == "voice-repair-candidate-workspace":
            result = prepare_voice_repair_candidate_workspace(
                load_voice_repair_comparison_plan(arguments.plan),
                arguments.candidate_id,
                arguments.import_directory,
                arguments.inputs_root,
                arguments.workspaces_root,
            )
            print(
                json.dumps(
                    result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
            )
            return 0
        if arguments.command == "voice-repair-candidate-command":
            command = build_voice_repair_candidate_command(
                load_voice_repair_comparison_plan(arguments.plan),
                arguments.candidate_id,
                arguments.workspace,
            )
            print(json.dumps({"command": list(command)}, indent=2, sort_keys=True))
            return 0
        if arguments.command == "portrait-alias-plan":
            plan = build_portrait_alias_plan(
                arguments.quality_review,
                max_dhash_distance=arguments.max_dhash_distance,
            )
            write_portrait_alias_plan(plan, arguments.output)
            print(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            )
            return 0
        if arguments.command == "portrait-alias-decision":
            decision = build_portrait_alias_decision(
                load_portrait_alias_plan(arguments.plan),
                arguments.accept_suggestion,
            )
            write_portrait_alias_decision(decision, arguments.output)
            print(
                json.dumps(
                    decision.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
            )
            return 0
        if arguments.command == "failure-repair-plan":
            print(
                json.dumps(
                    generation_failure_repair_plan(arguments.state, arguments.queue),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "cohort-review-plan":
            plan = build_cohort_review_plan(
                arguments.workspace,
                clean_samples_per_bucket=arguments.clean_samples_per_bucket,
                queue_ids=arguments.queue_ids,
            )
            if arguments.output is not None:
                write_cohort_review_plan(plan, arguments.output)
            print(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            )
            return 0
        if arguments.command == "cohort-review-bundle":
            bundle = build_cohort_review_bundle(
                arguments.workspace,
                clean_samples_per_bucket=arguments.clean_samples_per_bucket,
            )
            if arguments.output is not None:
                write_cohort_review_bundle(bundle, arguments.output)
            print(
                json.dumps(
                    bundle.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
            )
            return 0
        if arguments.command == "cohort-review-bundle-apply":
            bad = set(arguments.bad_queue_id)
            unexpected = sorted(bad - set(arguments.reviewed_queue_id))
            if unexpected:
                raise CohortReviewError(
                    f"Bad queue IDs were not reviewed: {unexpected}"
                )
            assessments = {
                queue_id: "bad" if queue_id in bad else "acceptable"
                for queue_id in arguments.reviewed_queue_id
            }
            projection = execute_cohort_bundle_decision(
                load_cohort_review_bundle(arguments.bundle),
                arguments.workspace_id,
                arguments.cohort_id,
                arguments.decision,
                reviewed_queue_ids=arguments.reviewed_queue_id,
                sample_assessments=assessments,
                next_clean_samples_per_bucket=(arguments.next_clean_samples_per_bucket),
            )
            print(
                json.dumps(
                    projection.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
            )
            return 0
        if arguments.command == "pending-resolution-plan":
            plan = build_pending_resolution_plan(arguments.workspace)
            if arguments.output is not None:
                write_pending_resolution_plan(plan, arguments.output)
            print(
                json.dumps(
                    plan.to_dict(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "pending-regeneration-command":
            print(
                json.dumps(
                    build_pending_regeneration_command(
                        arguments.workspace,
                        load_pending_resolution_plan(arguments.plan),
                        batch_index=arguments.batch_index,
                        batch_size=arguments.batch_size,
                    ).to_dict(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "failure-regeneration-plan":
            plan = build_failure_regeneration_plan(arguments.workspace)
            if arguments.output is not None:
                write_failure_regeneration_plan(plan, arguments.output)
            print(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            )
            return 0
        if arguments.command == "failure-regeneration-command":
            print(
                json.dumps(
                    build_failure_regeneration_command(
                        arguments.workspace,
                        load_failure_regeneration_plan(arguments.plan),
                        batch_index=arguments.batch_index,
                        batch_size=arguments.batch_size,
                    ).to_dict(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "silence-comparison-publish":
            plan = load_silence_comparison_input_plan(arguments.plan)
            result = publish_silence_comparison(
                plan.samples,
                arguments.output,
                target_seconds=arguments.target_seconds,
                input_plan_sha256=plan.sha256,
            )
            print(
                json.dumps(
                    {
                        "directory": str(result.directory),
                        "input_plan": str(plan.path),
                        "input_plan_sha256": plan.sha256,
                        "sample_count": result.sample_count,
                        "reports": [str(path) for path in result.report_paths],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "silence-comparison-check":
            document = load_silence_comparison(arguments.comparison)
            print(
                json.dumps(
                    {
                        "comparison": str(arguments.comparison.expanduser().resolve()),
                        "input_plan_sha256": document.get("input_plan_sha256"),
                        "production_enabled": document["policy"]["production_enabled"],
                        "requires_blind_review": document["policy"][
                            "requires_blind_review"
                        ],
                        "sample_count": len(document["samples"]),
                        "target_seconds": document["policy"]["target_seconds"],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "silence-comparison-session":
            session = create_silence_comparison_session(
                arguments.comparison,
                arguments.output,
                seed=arguments.seed,
            )
            print(
                json.dumps(
                    {"session": str(session), "comparison": str(arguments.comparison)},
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if arguments.command == "cohort-review-decision":
            bad = set(arguments.bad_queue_id)
            unexpected = sorted(bad - set(arguments.reviewed_queue_id))
            if unexpected:
                raise CohortReviewError(
                    f"Bad queue IDs were not reviewed: {unexpected}"
                )
            decision = build_cohort_review_decision(
                load_cohort_review_plan(arguments.plan),
                arguments.cohort_id,
                arguments.decision,
                reviewed_queue_ids=arguments.reviewed_queue_id,
                sample_assessments=(
                    {
                        queue_id: "bad" if queue_id in bad else "acceptable"
                        for queue_id in arguments.reviewed_queue_id
                    }
                    if bad or arguments.decision == "split"
                    else None
                ),
                next_clean_samples_per_bucket=(arguments.next_clean_samples_per_bucket),
            )
            write_cohort_review_decision(decision, arguments.output)
            print(
                json.dumps(
                    decision.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
            )
            return 0
        if arguments.command == "cohort-review-apply":
            result = apply_cohort_review_decision(
                arguments.workspace,
                load_cohort_review_plan(arguments.plan),
                load_cohort_review_decision(arguments.decision),
            )
            print(
                json.dumps(
                    result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
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
            result = publish_source_reference_evaluation(
                arguments.plan, arguments.output
            )
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
        if arguments.command == "publish-pack":
            producers = arguments.producer or [
                {
                    "name": "visual-novel-text-to-speech",
                    "version": _vntts_version(),
                }
            ]
            result = publish_final_game_pack(
                arguments.output,
                state_path=arguments.state,
                queue_path=arguments.queue,
                story_index_path=arguments.story_index,
                voice_manifest_path=arguments.voice_manifest,
                failure_reference_binding_path=arguments.failure_reference_binding,
                game_id=arguments.game_id,
                game_version=arguments.game_version,
                producers=producers,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command in {"preflight-queue", "build-queue"}:
            plan = inspect_generation_queue(
                arguments.story_index,
                arguments.voice_manifest,
                collection_ids=None
                if arguments.collection_ids is None
                else tuple(arguments.collection_ids),
                unknown_action=arguments.unknown_action,
                delivery_policy=arguments.delivery_policy,
            )
            payload = {"summary": plan.summary.to_dict()}
            if arguments.command == "build-queue":
                output = publish_generation_queue(plan, arguments.output)
                payload["output"] = str(output)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
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
    except (
        AudioEventReviewError,
        AuthoringWorkbenchError,
        GenerationQueueBuildError,
        BulkGenerationError,
        CohortReviewError,
        DeliveryAnnotationError,
        FinalGamePackError,
        FailureRegenerationError,
        FailureReferenceAuditError,
        FailureReferenceBindingError,
        LegacyAuthoringImportError,
        ListeningImportError,
        PortraitAliasError,
        ReferenceSelectionError,
        ReferenceRenderComparisonError,
        RenderHypothesisReviewError,
        SilenceComparisonError,
        SpeechRobustnessCorpusError,
        SpeechRobustnessAsrError,
        SourceReferenceReviewError,
        SourceReferenceQualityError,
        SourceReferenceBindingError,
        StoryIndexError,
        TerminalConflictResolutionError,
        TerminalConflictReviewError,
        TerminalConflictSuccessorError,
        VoiceGenerationQueueError,
        VoiceManifestError,
        VoiceQualityGateError,
        VoiceRepairComparisonError,
    ) as error:
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


if __name__ == "__main__":
    raise SystemExit(main())
