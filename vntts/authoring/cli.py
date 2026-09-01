"""Thin command-line dispatcher for offline authoring workflows."""

from __future__ import annotations

import argparse

from vntts.authoring.cli_audio_events import (
    COMMANDS as AUDIO_EVENT_COMMANDS,
)
from vntts.authoring.cli_audio_events import (
    configure_review_parsers as configure_audio_event_review_parsers,
)
from vntts.authoring.cli_audio_events import (
    configure_workspace_parsers as configure_audio_event_workspace_parsers,
)
from vntts.authoring.cli_audio_events import handle as handle_audio_event_command
from vntts.authoring.cli_cohort_reviews import COMMANDS as COHORT_REVIEW_COMMANDS
from vntts.authoring.cli_cohort_reviews import (
    configure_decision_parsers as configure_cohort_decision_parsers,
)
from vntts.authoring.cli_cohort_reviews import (
    configure_planning_parsers as configure_cohort_planning_parsers,
)
from vntts.authoring.cli_cohort_reviews import handle as handle_cohort_review_command
from vntts.authoring.cli_contract import preserve_command_order
from vntts.authoring.cli_delivery import COMMANDS as DELIVERY_COMMANDS
from vntts.authoring.cli_delivery import configure_parsers as configure_delivery_parsers
from vntts.authoring.cli_delivery import handle as handle_delivery_command
from vntts.authoring.cli_dispatch import CommandFamily, dispatch_command
from vntts.authoring.cli_errors import USER_ERRORS
from vntts.authoring.cli_generation import COMMANDS as GENERATION_COMMANDS
from vntts.authoring.cli_generation import configure_parsers as configure_generation
from vntts.authoring.cli_generation import handle as handle_generation_command
from vntts.authoring.cli_legacy import COMMANDS as LEGACY_COMMANDS
from vntts.authoring.cli_legacy import configure_parsers as configure_legacy_parsers
from vntts.authoring.cli_legacy import handle as handle_legacy_command
from vntts.authoring.cli_pack import COMMANDS as PACK_COMMANDS
from vntts.authoring.cli_pack import configure_parsers as configure_pack_parsers
from vntts.authoring.cli_pack import handle as handle_pack_command
from vntts.authoring.cli_queue import COMMANDS as QUEUE_COMMANDS
from vntts.authoring.cli_queue import configure_parsers as configure_queue_parsers
from vntts.authoring.cli_queue import handle as handle_queue_command
from vntts.authoring.cli_references import COMMANDS as REFERENCE_COMMANDS
from vntts.authoring.cli_references import configure_parsers as configure_references
from vntts.authoring.cli_references import handle as handle_reference_command
from vntts.authoring.cli_render_reviews import COMMANDS as RENDER_REVIEW_COMMANDS
from vntts.authoring.cli_render_reviews import (
    configure_hypothesis_parsers as configure_render_hypothesis_parsers,
)
from vntts.authoring.cli_render_reviews import (
    configure_reference_parsers as configure_reference_render_parsers,
)
from vntts.authoring.cli_render_reviews import handle as handle_render_review_command
from vntts.authoring.cli_silence_comparison import (
    COMMANDS as SILENCE_COMPARISON_COMMANDS,
)
from vntts.authoring.cli_silence_comparison import (
    configure_parsers as configure_silence_comparison_parsers,
)
from vntts.authoring.cli_silence_comparison import (
    handle as handle_silence_comparison_command,
)
from vntts.authoring.cli_speaker_identity import (
    COMMANDS as SPEAKER_IDENTITY_COMMANDS,
)
from vntts.authoring.cli_speaker_identity import (
    configure_parsers as configure_speaker_identity_parsers,
)
from vntts.authoring.cli_speaker_identity import (
    handle as handle_speaker_identity_command,
)
from vntts.authoring.cli_speech_robustness import (
    COMMANDS as SPEECH_ROBUSTNESS_COMMANDS,
)
from vntts.authoring.cli_speech_robustness import (
    configure_parsers as configure_speech_robustness_parsers,
)
from vntts.authoring.cli_speech_robustness import (
    handle as handle_speech_robustness_command,
)
from vntts.authoring.cli_terminal_conflicts import (
    COMMANDS as TERMINAL_CONFLICT_COMMANDS,
)
from vntts.authoring.cli_terminal_conflicts import (
    configure_parsers as configure_terminal_conflict_parsers,
)
from vntts.authoring.cli_terminal_conflicts import (
    handle as handle_terminal_conflict_command,
)
from vntts.authoring.cli_voice_quality import COMMANDS as VOICE_QUALITY_COMMANDS
from vntts.authoring.cli_voice_quality import (
    configure_parsers as configure_voice_quality_parsers,
)
from vntts.authoring.cli_voice_quality import handle as handle_voice_quality_command
from vntts.authoring.cli_workspace import COMMANDS as WORKSPACE_COMMANDS
from vntts.authoring.cli_workspace import (
    configure_parsers as configure_workspace_parsers,
)
from vntts.authoring.cli_workspace import handle as handle_workspace_command
from vntts.authoring.cli_workspace_transitions import (
    COMMANDS as WORKSPACE_TRANSITION_COMMANDS,
)
from vntts.authoring.cli_workspace_transitions import (
    configure_parsers as configure_workspace_transition_parsers,
)
from vntts.authoring.cli_workspace_transitions import (
    handle as handle_workspace_transition_command,
)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="VNTTS offline pregeneration authoring"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure_legacy_parsers(subparsers)
    configure_queue_parsers(subparsers)
    configure_workspace_parsers(subparsers)
    configure_workspace_transition_parsers(subparsers)
    configure_audio_event_workspace_parsers(subparsers)
    configure_audio_event_review_parsers(subparsers)
    configure_terminal_conflict_parsers(subparsers)
    configure_generation(subparsers)
    configure_render_hypothesis_parsers(subparsers)
    configure_speech_robustness_parsers(subparsers)
    configure_reference_render_parsers(subparsers)
    configure_voice_quality_parsers(subparsers)
    configure_references(subparsers)
    configure_silence_comparison_parsers(subparsers)
    configure_speaker_identity_parsers(subparsers)
    configure_cohort_planning_parsers(subparsers)
    configure_cohort_decision_parsers(subparsers)
    configure_pack_parsers(subparsers)
    configure_delivery_parsers(subparsers)
    preserve_command_order(subparsers)
    return parser


COMMAND_FAMILIES = (
    CommandFamily(LEGACY_COMMANDS, handle_legacy_command),
    CommandFamily(QUEUE_COMMANDS, handle_queue_command),
    CommandFamily(WORKSPACE_COMMANDS, handle_workspace_command),
    CommandFamily(WORKSPACE_TRANSITION_COMMANDS, handle_workspace_transition_command),
    CommandFamily(AUDIO_EVENT_COMMANDS, handle_audio_event_command),
    CommandFamily(TERMINAL_CONFLICT_COMMANDS, handle_terminal_conflict_command),
    CommandFamily(GENERATION_COMMANDS, handle_generation_command),
    CommandFamily(RENDER_REVIEW_COMMANDS, handle_render_review_command),
    CommandFamily(SPEECH_ROBUSTNESS_COMMANDS, handle_speech_robustness_command),
    CommandFamily(VOICE_QUALITY_COMMANDS, handle_voice_quality_command),
    CommandFamily(REFERENCE_COMMANDS, handle_reference_command),
    CommandFamily(SILENCE_COMPARISON_COMMANDS, handle_silence_comparison_command),
    CommandFamily(SPEAKER_IDENTITY_COMMANDS, handle_speaker_identity_command),
    CommandFamily(COHORT_REVIEW_COMMANDS, handle_cohort_review_command),
    CommandFamily(PACK_COMMANDS, handle_pack_command),
    CommandFamily(DELIVERY_COMMANDS, handle_delivery_command),
)


def main(argv=None) -> int:
    parser = create_parser()
    arguments = parser.parse_args(argv)
    try:
        result = dispatch_command(arguments, COMMAND_FAMILIES)
    except USER_ERRORS as error:
        parser.error(str(error))
    if result is None:
        parser.error(f"No handler is registered for {arguments.command!r}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
