"""Speech-robustness and managed-ASR command family."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.asr_model import (
    ManagedAsrModelError,
    install_managed_asr_model,
    managed_asr_status,
    resolve_managed_asr_model,
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

COMMANDS = frozenset(
    {
        "speech-robustness-corpus",
        "speech-robustness-check",
        "speech-robustness-asr",
        "asr-model-install",
        "asr-model-status",
    }
)


class SpeechRobustnessCommandError(RuntimeError):
    """A speech-robustness command failed its authoring contract."""


def configure_parsers(subparsers) -> None:
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
        help="Compare a v2 robustness corpus with managed or explicit local ASR",
    )
    robustness_asr.add_argument("corpus", type=Path)
    robustness_asr.add_argument(
        "model",
        nargs="?",
        type=Path,
        help="Optional explicit local model; managed Whisper is used by default",
    )
    robustness_asr.add_argument("--output", type=Path, required=True)
    robustness_asr.add_argument("--device", default="cpu")
    robustness_asr.add_argument(
        "--offline",
        action="store_true",
        help="Require an already installed managed model; never download it",
    )
    robustness_asr.add_argument(
        "--progress",
        type=Path,
        help="Checksum-bound resumable per-sample progress document",
    )
    asr_model_install = subparsers.add_parser(
        "asr-model-install",
        help="Atomically install and verify the pinned authoring Whisper model",
    )
    asr_model_install.add_argument(
        "--source",
        type=Path,
        help="Import an existing snapshot instead of downloading it",
    )
    subparsers.add_parser(
        "asr-model-status",
        help="Inspect the pinned authoring Whisper model without downloading it",
    )


def handle(arguments: argparse.Namespace) -> int:
    try:
        return _handle(arguments)
    except (
        ManagedAsrModelError,
        SpeechRobustnessAsrError,
        SpeechRobustnessCorpusError,
    ) as error:
        raise SpeechRobustnessCommandError(str(error)) from error


def _handle(arguments: argparse.Namespace) -> int:
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
        model = arguments.model
        if model is None:
            model = (
                resolve_managed_asr_model()
                if arguments.offline
                else Path(install_managed_asr_model()["model_directory"])
            )
        report = build_speech_robustness_asr_report(
            arguments.corpus,
            model,
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
    if arguments.command == "asr-model-install":
        print(
            json.dumps(
                install_managed_asr_model(source=arguments.source),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if arguments.command == "asr-model-status":
        print(json.dumps(managed_asr_status(), indent=2, sort_keys=True))
        return 0
    raise SpeechRobustnessCommandError(
        f"No speech-robustness handler for {arguments.command!r}"
    )


__all__ = [
    "COMMANDS",
    "SpeechRobustnessCommandError",
    "configure_parsers",
    "handle",
]
