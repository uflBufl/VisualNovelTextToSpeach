"""Stable semantic snapshot for argparse compatibility checks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

AUTHORING_COMMAND_ORDER = (
    "discover-legacy",
    "import-legacy",
    "inspect-standalone",
    "import-standalone",
    "inspect-listening",
    "import-listening",
    "extend-queue",
    "preflight-queue",
    "build-queue",
    "create-workspace",
    "merge-workspace-outcomes",
    "merge-reconciled-outcomes",
    "merge-explicit-fallbacks",
    "known-role-live-fallback",
    "audio-event-omission",
    "audio-event-projection-fallback",
    "reviewed-waveform-publication",
    "reviewed-rejection-live-fallback",
    "rebase-workspace-config",
    "experimental-composite-voice-input",
    "carry-failed-controls",
    "failed-prompt-hypothesis-selection",
    "terminal-conflict-resolution",
    "terminal-conflict-carry",
    "terminal-conflict-cohort-carry",
    "terminal-conflict-successor",
    "terminal-conflict-merge",
    "create-failure-reference-workspace",
    "generate",
    "review",
    "live-fallback",
    "publish",
    "status",
    "audio-event-review-publish",
    "audio-event-review-decide",
    "audio-event-review-status",
    "audio-event-composition-publish",
    "audio-event-composition-decide",
    "audio-event-composition-status",
    "audio-event-composition-workspace",
    "render-hypothesis-review-publish",
    "render-hypothesis-review-decide",
    "render-hypothesis-review-status",
    "render-hypothesis-review-import",
    "failure-report",
    "speech-robustness-corpus",
    "speech-robustness-check",
    "speech-robustness-asr",
    "asr-model-install",
    "asr-model-status",
    "specialist-failure-plan",
    "failure-reference-audit",
    "failure-reference-render-comparison",
    "failure-reference-render-session",
    "failure-reference-import-listening",
    "failure-reference-binding",
    "voice-quality-gate",
    "voice-quality-check",
    "voice-repair-comparison-plan",
    "voice-repair-candidate-workspace",
    "voice-repair-candidate-command",
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
    "failure-repair-plan",
    "silence-comparison-publish",
    "silence-comparison-check",
    "silence-comparison-session",
    "cohort-review-plan",
    "cohort-review-bundle",
    "cohort-review-bundle-apply",
    "pending-resolution-plan",
    "pending-regeneration-command",
    "failure-regeneration-plan",
    "failure-regeneration-command",
    "cohort-review-decision",
    "cohort-review-apply",
    "reference-report",
    "select-reference",
    "import-reference-review",
    "build-reference-evaluation",
    "build-reference-listening-reports",
    "build-reference-bindings",
    "extend-reference-bindings",
    "retire-reference-bindings",
    "publish-pack",
    "annotate-delivery",
)


def preserve_command_order(subparsers: argparse._SubParsersAction) -> None:
    """Restore the captured public help order after family composition."""

    observed = set(subparsers.choices)
    expected = set(AUTHORING_COMMAND_ORDER)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise ValueError(
            f"Authoring command inventory changed: missing={missing}, extra={extra}"
        )
    parser_by_name = dict(subparsers.choices)
    subparsers._name_parser_map.clear()
    subparsers._name_parser_map.update(
        (name, parser_by_name[name]) for name in AUTHORING_COMMAND_ORDER
    )
    choice_by_name = {action.dest: action for action in subparsers._choices_actions}
    subparsers._choices_actions[:] = [
        choice_by_name[name] for name in AUTHORING_COMMAND_ORDER
    ]


def _normalized(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    if isinstance(value, set):
        return sorted(_normalized(item) for item in value)
    return value


def parser_contract(parser: argparse.ArgumentParser) -> dict:
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    helps = {action.dest: action.help for action in subparsers._choices_actions}
    commands = {}
    for name, command_parser in subparsers.choices.items():
        commands[name] = {
            "help": helps.get(name),
            "description": command_parser.description,
            "actions": [
                {
                    "dest": action.dest,
                    "options": list(action.option_strings),
                    "required": bool(getattr(action, "required", False)),
                    "nargs": action.nargs,
                    "default": _normalized(action.default),
                    "choices": _normalized(
                        list(action.choices) if action.choices is not None else None
                    ),
                    "type": getattr(action.type, "__name__", None),
                    "help": action.help,
                    "action": type(action).__name__,
                }
                for action in command_parser._actions
                if action.dest != "help"
            ],
        }
    return commands


def parser_contract_sha256(parser: argparse.ArgumentParser) -> str:
    contract = parser_contract(parser)
    payload = json.dumps(
        {"command_order": list(contract), "commands": contract},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
