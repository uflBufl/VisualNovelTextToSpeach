import os
import subprocess
import sys


def escape_workflow_command(value):
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(completed.stdout, end="")
    if completed.returncode and os.environ.get("GITHUB_ACTIONS"):
        details = completed.stdout[-12_000:] or "Unit tests exited without output."
        print(
            f"::error title=Unit tests failed::{escape_workflow_command(details)}",
            file=sys.stderr,
        )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
