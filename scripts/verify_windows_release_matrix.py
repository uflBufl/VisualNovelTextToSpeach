import argparse

from vntts.cli import cli_messages, cli_success
from vntts.release_matrix import (
    load_evidence,
    load_release_matrix,
    validate_release_evidence,
)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--matrix",
        default="packaging/windows/release-matrix.json",
    )
    parser.add_argument("--evidence-directory", required=True)
    parser.add_argument("--allow-unsigned", action="store_true")
    arguments = parser.parse_args(argv)

    profiles = load_release_matrix(arguments.matrix)
    reports = load_evidence(arguments.evidence_directory)
    errors = validate_release_evidence(
        profiles,
        reports,
        allow_unsigned=arguments.allow_unsigned,
    )
    if errors:
        return cli_messages(errors, exit_code=1, error=True)
    return cli_success(f"All {len(profiles)} Windows release profiles passed.")


if __name__ == "__main__":
    raise SystemExit(main())
