import argparse
import sys
from pathlib import Path

from vntts.wwise import (
    AudioConversionError,
    WwiseBankError,
    convert_audio,
    extract_and_convert_bank,
    extract_bank,
)


def create_extract_parser():
    parser = argparse.ArgumentParser(
        description="Extract embedded .wem audio from an Audiokinetic Wwise bank."
    )
    parser.add_argument("bank", type=Path, help="Input .bnk file.")
    parser.add_argument("output_directory", type=Path)
    parser.add_argument(
        "--convert",
        action="store_true",
        help="Also convert every extracted file to WAV.",
    )
    parser.add_argument("--decoder", default="vgmstream-cli")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def create_convert_parser():
    parser = argparse.ArgumentParser(
        description="Convert game audio supported by vgmstream into WAV."
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--decoder", default="vgmstream-cli")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def extract_main(arguments=None):
    arguments = create_extract_parser().parse_args(arguments)
    try:
        if arguments.convert:
            outputs = extract_and_convert_bank(
                arguments.bank,
                arguments.output_directory,
                limit=arguments.limit,
                decoder=arguments.decoder,
                overwrite=arguments.overwrite,
            )
        else:
            outputs = extract_bank(
                arguments.bank,
                arguments.output_directory,
                limit=arguments.limit,
                overwrite=arguments.overwrite,
            )
    except (WwiseBankError, AudioConversionError) as error:
        print(error, file=sys.stderr)
        return 1

    noun = "WAV files" if arguments.convert else "WEM files"
    print(f"Created {len(outputs)} {noun} in {arguments.output_directory.resolve()}")
    return 0


def convert_main(arguments=None):
    arguments = create_convert_parser().parse_args(arguments)
    try:
        output = convert_audio(
            arguments.source,
            arguments.output,
            decoder=arguments.decoder,
            overwrite=arguments.overwrite,
        )
    except AudioConversionError as error:
        print(error, file=sys.stderr)
        return 1

    print(f"Created {output}")
    return 0
