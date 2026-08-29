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

from vntts.authoring.audio_event_composition import (
    AudioEventCompositionError,
    load_audio_event_composition,
    publish_audio_event_composition,
    record_audio_event_composition_decision,
)
from vntts.authoring.audio_event_omission import (
    create_audio_event_omission_workspace,
)
from vntts.authoring.audio_event_projection_fallback import (
    create_audio_event_projection_fallback_workspace,
)
from vntts.authoring.audio_event_review import (
    AudioEventReviewError,
    load_audio_event_review,
    publish_source_audio_event_review,
    record_audio_event_review_decision,
)
from vntts.authoring.bulk_generation import (
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
from vntts.authoring.cli_delivery import (
    COMMANDS as DELIVERY_COMMANDS,
)
from vntts.authoring.cli_delivery import (
    configure_parsers as configure_delivery_parsers,
)
from vntts.authoring.cli_delivery import (
    handle as handle_delivery_command,
)
from vntts.authoring.cli_dispatch import CommandFamily, dispatch_command
from vntts.authoring.cli_generation_options import (
    add_failure_repair_arguments as _add_failure_repair_arguments,
)
from vntts.authoring.cli_generation_options import (
    add_missing_voice_policy_arguments as _add_missing_voice_policy_arguments,
)
from vntts.authoring.cli_generation_options import (
    failure_repair_policy as _generation_failure_repair_policy,
)
from vntts.authoring.cli_generation_options import (
    missing_voice_policy as _generation_missing_voice_policy,
)
from vntts.authoring.cli_legacy import (
    COMMANDS as LEGACY_COMMANDS,
)
from vntts.authoring.cli_legacy import (
    configure_parsers as configure_legacy_parsers,
)
from vntts.authoring.cli_legacy import (
    handle as handle_legacy_command,
)
from vntts.authoring.cli_queue import (
    COMMANDS as QUEUE_COMMANDS,
)
from vntts.authoring.cli_queue import (
    configure_parsers as configure_queue_parsers,
)
from vntts.authoring.cli_queue import (
    handle as handle_queue_command,
)
from vntts.authoring.cli_workspace import (
    COMMANDS as WORKSPACE_COMMANDS,
)
from vntts.authoring.cli_workspace import (
    configure_parsers as configure_workspace_parsers,
)
from vntts.authoring.cli_workspace import (
    handle as handle_workspace_command,
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
    DeliveryAnnotationError,
)
from vntts.authoring.experimental_composite_voice import (
    ExperimentalCompositeVoiceError,
    publish_experimental_composite_voice_input,
)
from vntts.authoring.failed_control_carry import (
    FailedControlCarryError,
    carry_failed_controls,
)
from vntts.authoring.failed_prompt_hypothesis import (
    FailedPromptHypothesisError,
    publish_failed_prompt_hypothesis_selection,
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
from vntts.authoring.failure_repair import DEFAULT_INTERNAL_SILENCE_TARGET_SECONDS
from vntts.authoring.game_pack import FinalGamePackError, publish_final_game_pack
from vntts.authoring.generation_state import LIVE_FALLBACK_REASONS
from vntts.authoring.known_role_live_fallback import (
    create_known_role_live_fallback_workspace,
)
from vntts.authoring.known_role_reuse import (
    KnownRoleReuseError,
    publish_known_role_reuse_binding,
)
from vntts.authoring.legacy_import import (
    LegacyAuthoringImportError,
)
from vntts.authoring.listening_import import (
    ListeningImportError,
)
from vntts.authoring.missing_voice_live_fallback import (
    MissingVoiceLiveFallbackError,
    authorize_missing_voice_live_fallback,
)
from vntts.authoring.missing_voice_reuse import (
    MissingVoiceReuseError,
    build_missing_voice_reuse_candidate_command,
    build_missing_voice_reuse_plan,
    load_missing_voice_reuse_plan,
    parse_cohort_arguments,
    prepare_missing_voice_reuse_candidate_workspace,
    write_missing_voice_reuse_plan,
)
from vntts.authoring.missing_voice_reuse_binding import (
    MissingVoiceReuseBindingError,
    publish_missing_voice_reuse_binding,
)
from vntts.authoring.missing_voice_reuse_review import (
    MissingVoiceReuseReviewError,
    build_missing_voice_reuse_review,
    load_missing_voice_reuse_review,
    missing_voice_reuse_review_progress,
    parse_missing_voice_reuse_evidence,
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
)
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
from vntts.authoring.reviewed_rejection_fallback import (
    create_reviewed_rejection_fallback_workspace,
)
from vntts.authoring.reviewed_waveform_publication import (
    create_reviewed_waveform_publication_workspace,
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
    create_audio_event_composition_workspace,
    create_failure_reference_workspace,
    default_workspaces_root,
    failure_reference_runtime_binding,
    generation_control_bindings,
    generation_output_identity,
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


def create_parser():
    parser = argparse.ArgumentParser(
        description="VNTTS offline pregeneration authoring"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure_legacy_parsers(subparsers)
    configure_queue_parsers(subparsers)
    configure_workspace_parsers(subparsers)
    known_role_fallback = subparsers.add_parser(
        "known-role-live-fallback",
        help="Route exact exhausted lines to a bound known-role Pocket voice",
    )
    known_role_fallback.add_argument("base_workspace", type=Path)
    known_role_fallback.add_argument(
        "--evidence",
        action="append",
        nargs=2,
        metavar=("QUEUE_ID", "FAILED_WORKSPACE"),
        required=True,
    )
    known_role_fallback.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    event_omission = subparsers.add_parser(
        "audio-event-omission",
        help="Omit exact pure events with no validated audio source",
    )
    event_omission.add_argument("base_workspace", type=Path)
    event_omission.add_argument(
        "--queue-id", action="append", dest="queue_ids", required=True
    )
    event_omission.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    event_projection = subparsers.add_parser(
        "audio-event-projection-fallback",
        help="Route only spoken text from exact mixed audio-event lines",
    )
    event_projection.add_argument("base_workspace", type=Path)
    event_projection.add_argument(
        "--queue-id", action="append", dest="queue_ids", required=True
    )
    event_projection.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    reviewed_waveforms = subparsers.add_parser(
        "reviewed-waveform-publication",
        help="Migrate exact approved WAVs without inventing synthesis controls",
    )
    reviewed_waveforms.add_argument("base_workspace", type=Path)
    reviewed_waveforms.add_argument(
        "--workspaces-root", type=Path, default=default_workspaces_root()
    )
    reviewed_rejections = subparsers.add_parser(
        "reviewed-rejection-live-fallback",
        help="Route exact rejected WAV identities through Pocket live synthesis",
    )
    reviewed_rejections.add_argument("base_workspace", type=Path)
    reviewed_rejections.add_argument(
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
    experimental_composite = subparsers.add_parser(
        "experimental-composite-voice-input",
        help="Publish a comparison-only exact-bank composite manifest voice",
    )
    experimental_composite.add_argument("source_manifest", type=Path)
    experimental_composite.add_argument("composite_directory", type=Path)
    experimental_composite.add_argument("quality_review", type=Path)
    experimental_composite.add_argument("voice_character")
    experimental_composite.add_argument("output_directory", type=Path)
    failed_control_carry = subparsers.add_parser(
        "carry-failed-controls",
        help="Carry exact non-playable failures onto an additive workspace config",
    )
    failed_control_carry.add_argument("source_workspace", type=Path)
    failed_control_carry.add_argument("target_workspace", type=Path)
    failed_control_carry.add_argument(
        "--queue-id", action="append", required=True, dest="queue_ids"
    )
    failed_prompt_selection = subparsers.add_parser(
        "failed-prompt-hypothesis-selection",
        help="Import a completed prompt comparison without approving speech",
    )
    failed_prompt_selection.add_argument("plan", type=Path)
    failed_prompt_selection.add_argument("session", type=Path)
    failed_prompt_selection.add_argument("output", type=Path)
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
    live_fallback.add_argument(
        "--evidence-workspace",
        action="append",
        default=[],
        type=Path,
        help=(
            "Bind one immutable failed repair workspace; repeat for "
            "generation_hypotheses_exhausted"
        ),
    )
    live_fallback.add_argument(
        "--evidence-review",
        action="append",
        default=[],
        type=Path,
        help=(
            "Bind one terminal need_different render-hypothesis review; repeat "
            "for generation_hypotheses_exhausted"
        ),
    )
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
    audio_event_composition_publish = subparsers.add_parser(
        "audio-event-composition-publish",
        help="Publish one exact accepted event-only production composition",
    )
    audio_event_composition_publish.add_argument("review", type=Path)
    audio_event_composition_publish.add_argument("--output", type=Path, required=True)
    audio_event_composition_decide = subparsers.add_parser(
        "audio-event-composition-decide",
        help="Approve or reject one exact production event composition",
    )
    audio_event_composition_decide.add_argument("directory", type=Path)
    audio_event_composition_decide.add_argument(
        "decision", choices=("approved", "rejected")
    )
    audio_event_composition_status = subparsers.add_parser(
        "audio-event-composition-status",
        help="Validate and inspect one event-only production composition",
    )
    audio_event_composition_status.add_argument("directory", type=Path)
    audio_event_workspace = subparsers.add_parser(
        "audio-event-composition-workspace",
        help="Create a reviewable successor from one approved event composition",
    )
    audio_event_workspace.add_argument("base_workspace", type=Path)
    audio_event_workspace.add_argument("composition", type=Path)
    audio_event_workspace.add_argument("--workspaces-root", type=Path)
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
    cohort_bundle.add_argument(
        "--workspace-queue-id",
        action="append",
        nargs=2,
        default=[],
        metavar=("WORKSPACE", "QUEUE_ID"),
        help=(
            "Select one exact pending queue ID from one --workspace; repeat as "
            "needed. When used, every workspace requires at least one selection"
        ),
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
        "--live-sequence-plan",
        type=Path,
        help="Exact checksum-bound live sequence plan to ship as a version-2 pack",
    )
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
    configure_delivery_parsers(subparsers)
    return parser


COMMAND_FAMILIES = (
    CommandFamily(LEGACY_COMMANDS, handle_legacy_command),
    CommandFamily(QUEUE_COMMANDS, handle_queue_command),
    CommandFamily(WORKSPACE_COMMANDS, handle_workspace_command),
    CommandFamily(DELIVERY_COMMANDS, handle_delivery_command),
)


def main(argv=None):
    arguments = create_parser().parse_args(argv)
    try:
        family_result = dispatch_command(arguments, COMMAND_FAMILIES)
        if family_result is not None:
            return family_result
        if arguments.command == "known-role-live-fallback":
            result = create_known_role_live_fallback_workspace(
                arguments.base_workspace,
                arguments.evidence,
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
        if arguments.command == "audio-event-omission":
            result = create_audio_event_omission_workspace(
                arguments.base_workspace,
                arguments.queue_ids,
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
        if arguments.command == "audio-event-projection-fallback":
            result = create_audio_event_projection_fallback_workspace(
                arguments.base_workspace,
                arguments.queue_ids,
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
        if arguments.command == "reviewed-waveform-publication":
            result = create_reviewed_waveform_publication_workspace(
                arguments.base_workspace,
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
        if arguments.command == "reviewed-rejection-live-fallback":
            result = create_reviewed_rejection_fallback_workspace(
                arguments.base_workspace,
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
        if arguments.command == "experimental-composite-voice-input":
            result = publish_experimental_composite_voice_input(
                arguments.source_manifest,
                arguments.composite_directory,
                arguments.quality_review,
                arguments.voice_character,
                arguments.output_directory,
            )
            print(
                json.dumps(
                    result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
            )
            return 0
        if arguments.command == "carry-failed-controls":
            result = carry_failed_controls(
                arguments.source_workspace,
                arguments.target_workspace,
                arguments.queue_ids,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "failed-prompt-hypothesis-selection":
            result = publish_failed_prompt_hypothesis_selection(
                arguments.plan, arguments.session, arguments.output
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
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
                evidence_workspaces=arguments.evidence_workspace,
                evidence_reviews=arguments.evidence_review,
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
                "omitted": 0,
            }
            for item in state["items"].values():
                if item["status"] != "live_fallback":
                    counts[item["status"]] += 1
            counts["live_fallback"] = sum(
                isinstance(item.get("live_fallback"), dict)
                for item in state["items"].values()
            )
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
        if arguments.command == "audio-event-composition-publish":
            result = publish_audio_event_composition(arguments.review, arguments.output)
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "audio-event-composition-decide":
            result = record_audio_event_composition_decision(
                arguments.directory, arguments.decision
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "audio-event-composition-status":
            result = load_audio_event_composition(arguments.directory)
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
        if arguments.command == "audio-event-composition-workspace":
            result = create_audio_event_composition_workspace(
                arguments.base_workspace,
                arguments.composition,
                arguments.workspaces_root,
            )
            print(
                json.dumps(
                    {
                        "created": result.created,
                        "workspace": str(result.directory),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
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
            print(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            )
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
                json.dumps(
                    result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
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
            selections = None
            if arguments.workspace_queue_id:
                selections = {
                    Path(workspace).expanduser().resolve(): []
                    for workspace in arguments.workspace
                }
                for workspace_value, queue_id in arguments.workspace_queue_id:
                    workspace = Path(workspace_value).expanduser().resolve()
                    if workspace not in selections:
                        raise CohortReviewError(
                            "Review bundle queue selection references an unknown "
                            f"workspace: {workspace}"
                        )
                    if not queue_id.strip():
                        raise CohortReviewError(
                            "Review bundle selected queue ID must be non-empty"
                        )
                    if queue_id in selections[workspace]:
                        raise CohortReviewError(
                            "Review bundle selected queue ID is duplicated for "
                            f"{workspace}: {queue_id}"
                        )
                    selections[workspace].append(queue_id)
                missing = sorted(
                    str(workspace)
                    for workspace, queue_ids in selections.items()
                    if not queue_ids
                )
                if missing:
                    raise CohortReviewError(
                        "Every review bundle workspace requires an exact queue "
                        f"selection: {missing}"
                    )
            bundle = build_cohort_review_bundle(
                arguments.workspace,
                clean_samples_per_bucket=arguments.clean_samples_per_bucket,
                queue_ids_by_workspace=selections,
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
                live_sequence_plan_path=arguments.live_sequence_plan,
                failure_reference_binding_path=arguments.failure_reference_binding,
                game_id=arguments.game_id,
                game_version=arguments.game_version,
                producers=producers,
            )
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            return 0
    except (
        AudioEventReviewError,
        AudioEventCompositionError,
        AuthoringWorkbenchError,
        GenerationQueueBuildError,
        BulkGenerationError,
        CohortReviewError,
        DeliveryAnnotationError,
        ExperimentalCompositeVoiceError,
        FinalGamePackError,
        FailureRegenerationError,
        FailureReferenceAuditError,
        FailureReferenceBindingError,
        FailedControlCarryError,
        FailedPromptHypothesisError,
        LegacyAuthoringImportError,
        ListeningImportError,
        MissingVoiceReuseError,
        MissingVoiceReuseBindingError,
        MissingVoiceReuseReviewError,
        MissingVoiceLiveFallbackError,
        KnownRoleReuseError,
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
    create_parser().error(f"No handler is registered for {arguments.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
