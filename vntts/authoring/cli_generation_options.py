"""Shared generation policy options for authoring command families."""

from __future__ import annotations

from vntts.authoring.bulk_generation import BulkGenerationError
from vntts.authoring.failure_repair import FailureRepairPolicy, FailureRepairPolicyError
from vntts.authoring.missing_voice_policy import (
    NARRATOR_ALL_UNRESOLVED,
    NARRATOR_ROLES,
    MissingVoicePolicy,
    MissingVoicePolicyError,
)


def missing_voice_policy(arguments) -> MissingVoicePolicy:
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


def add_missing_voice_policy_arguments(parser) -> None:
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


def failure_repair_policy(arguments) -> FailureRepairPolicy:
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


def add_failure_repair_arguments(parser) -> None:
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
