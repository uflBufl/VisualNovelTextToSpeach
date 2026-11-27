"""Find a representative original clip in a MOSS game voice candidate catalog.

Run with python -m scripts.study_reference_typicality MANIFEST CHARACTER.
Use --holdout-bank BANK to reserve independent originals for measuring similarity
of both selections and optional --render WAV samples. Requires the managed
speaker-identity model; the application installs it on first selection use.
Use --list-characters to find small catalogs, then --select for a timed fresh run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from vntts.authoring.speaker_identity import make_speechbrain_embedder
from vntts.authoring.speaker_identity_model import (
    resolve_managed_speaker_identity_model,
)
from vntts.cli import cli_error, print_json
from vntts.reference_selection import catalog_characters, select_reference, study


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("character", nargs="?")
    parser.add_argument(
        "--select",
        action="store_true",
        help="Select a steady reference with a typical reference as fallback",
    )
    parser.add_argument(
        "--list-characters",
        action="store_true",
        help="List eligible catalog sizes without loading models",
    )
    parser.add_argument(
        "--holdout-bank", help="Reserve every clip from this game audio bank"
    )
    parser.add_argument(
        "--model-root", type=Path, help="Managed speaker-identity model root"
    )
    parser.add_argument("--render", type=Path, action="append", default=[])
    args = parser.parse_args()
    if not args.list_characters and not args.character:
        parser.error("character is required unless --list-characters is used")
    if args.select and args.render:
        parser.error("--render is only available when measuring speaker similarity")
    try:
        if args.list_characters:
            print_json(catalog_characters(args.manifest))
            return 0
        encoder = None

        def embed(payload: bytes) -> object:
            nonlocal encoder
            if encoder is None:
                directory = resolve_managed_speaker_identity_model(root=args.model_root)
                encoder = make_speechbrain_embedder(directory)
            return encoder(payload)

        report = (
            select_reference(
                args.manifest, args.character, embed, holdout_bank=args.holdout_bank
            )
            if args.select
            else study(
                args.manifest,
                args.character,
                args.holdout_bank,
                embed,
                args.render,
            )
        )
    except (ImportError, OSError, ValueError, RuntimeError) as error:
        return cli_error(error)
    print_json(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
