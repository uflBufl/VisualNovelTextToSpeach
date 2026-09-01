"""Diagnostic speaker-identity command family."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vntts.authoring.speaker_identity import (
    SpeakerIdentityError,
    build_labelled_pairs,
    build_reference_inventory,
    build_speaker_identity_report,
    installed_model_descriptor,
    load_labelled_pairs,
    load_reference_inventory,
    make_speechbrain_embedder,
    require_speechbrain_runtime,
    write_labelled_pairs,
    write_reference_inventory,
    write_speaker_identity_report,
)
from vntts.authoring.speaker_identity_model import (
    SpeakerIdentityModelError,
    install_managed_speaker_identity_model,
    managed_speaker_identity_status,
    resolve_managed_speaker_identity_model,
)

COMMANDS = frozenset(
    {
        "speaker-identity-inventory",
        "speaker-identity-labels",
        "speaker-identity-model-install",
        "speaker-identity-model-status",
        "speaker-identity-evaluate",
    }
)


def configure_parsers(subparsers) -> None:
    inventory = subparsers.add_parser(
        "speaker-identity-inventory",
        help="Publish a checksum-bound inventory of voice references",
    )
    inventory.add_argument("manifest", type=Path)
    inventory.add_argument("--output", type=Path, required=True)
    labels = subparsers.add_parser(
        "speaker-identity-labels",
        help="Validate and bind fit/held-out pair labels to an inventory",
    )
    labels.add_argument("inventory", type=Path)
    labels.add_argument("pairs", type=Path)
    labels.add_argument("--output", type=Path, required=True)
    install = subparsers.add_parser(
        "speaker-identity-model-install",
        help="Install and verify the pinned diagnostic ECAPA model",
    )
    install.add_argument(
        "--source", type=Path, help="Import an existing snapshot without downloading"
    )
    subparsers.add_parser(
        "speaker-identity-model-status",
        help="Inspect the pinned diagnostic ECAPA model without downloading",
    )
    evaluate = subparsers.add_parser(
        "speaker-identity-evaluate",
        help="Fit and test a diagnostic cosine-distance threshold",
    )
    evaluate.add_argument("inventory", type=Path)
    evaluate.add_argument("labels", type=Path)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument(
        "--offline",
        action="store_true",
        help="Require the managed model to be installed already",
    )
    evaluate.add_argument("--device", default="cpu", choices=("cpu",))


def handle(arguments: argparse.Namespace) -> int:
    if arguments.command == "speaker-identity-inventory":
        document = build_reference_inventory(arguments.manifest)
        write_reference_inventory(document, arguments.output)
        _print({"output": str(arguments.output.resolve()), **_summary(document)})
        return 0
    if arguments.command == "speaker-identity-labels":
        inventory = load_reference_inventory(arguments.inventory)
        draft = _read_pair_draft(arguments.pairs)
        document = build_labelled_pairs(inventory, draft)
        write_labelled_pairs(document, arguments.output)
        _print({"output": str(arguments.output.resolve()), **_summary(document)})
        return 0
    if arguments.command == "speaker-identity-model-install":
        _print(install_managed_speaker_identity_model(source=arguments.source))
        return 0
    if arguments.command == "speaker-identity-model-status":
        _print(managed_speaker_identity_status())
        return 0
    if arguments.command == "speaker-identity-evaluate":
        inventory = load_reference_inventory(arguments.inventory)
        labels = load_labelled_pairs(arguments.labels, inventory)
        require_speechbrain_runtime(device=arguments.device)
        model_directory = (
            resolve_managed_speaker_identity_model()
            if arguments.offline
            else Path(install_managed_speaker_identity_model()["model_directory"])
        )
        document = build_speaker_identity_report(
            inventory,
            labels,
            make_speechbrain_embedder(model_directory, device=arguments.device),
            installed_model_descriptor(),
        )
        write_speaker_identity_report(document, arguments.output)
        _print({"output": str(arguments.output.resolve()), **_summary(document)})
        return 0
    raise SpeakerIdentityError(f"No speaker-identity handler for {arguments.command!r}")


def _read_pair_draft(path):
    try:
        document = json.loads(Path(path).expanduser().resolve().read_text("utf-8"))
    except (OSError, ValueError) as error:
        raise SpeakerIdentityError(
            f"Unable to read labelled-pair draft: {error}"
        ) from error
    pairs = document.get("pairs") if isinstance(document, dict) else document
    if not isinstance(pairs, list):
        raise SpeakerIdentityError(
            "Labelled-pair draft must be a list or contain pairs"
        )
    return pairs


def _summary(document):
    return {
        key: document[key]
        for key in (
            "inventory_id",
            "labels_id",
            "report_id",
            "reference_count",
            "pair_count",
            "threshold",
            "threshold_eligible",
        )
        if key in document
    }


def _print(document):
    print(json.dumps(document, indent=2, sort_keys=True))


__all__ = [
    "COMMANDS",
    "SpeakerIdentityError",
    "SpeakerIdentityModelError",
    "configure_parsers",
    "handle",
]
