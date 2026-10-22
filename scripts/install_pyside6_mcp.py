"""Build the pinned screenshot-safe developer MCP tool, independently of VNTTS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

UPSTREAM = "https://github.com/com55/pyside6-mcp.git"
REVISION = "6d3a4f25e1f98378204f1340f477f9dda8c89dfe"
PATCH = Path(__file__).with_name("pyside6-mcp") / "screenshot-safety.patch"
TESTS = (
    "tests/test_screenshot_capture.py",
    "tests/test_screenshot_server.py",
    "tests/test_server_pure.py",
    "tests/test_integration_ready.py::test_screenshot_returns_png_base64",
)


def run(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, env=env, text=True).strip()


def prepare_source(source: Path) -> None:
    """Never reset or modify an existing checkout; create a task-owned worktree."""
    if not PATCH.is_file():
        raise ValueError("The tracked MCP patch is missing")
    repository = source.with_name(source.name + ".git")
    if source.exists() or repository.exists():
        raise ValueError("Source and adjacent .git repository must both be new paths")
    if not source.parent.is_dir():
        raise ValueError("Source parent directory must exist")
    run("git", "clone", "--bare", UPSTREAM, str(repository))
    run(
        "git",
        "--git-dir",
        str(repository),
        "worktree",
        "add",
        "--detach",
        str(source),
        REVISION,
    )
    run("git", "apply", "--check", str(PATCH), cwd=source)
    run("git", "apply", str(PATCH), cwd=source)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        required=True,
        type=Path,
        help="New permanent MCP source worktree path",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Replace the uv tool after all checks pass",
    )
    args = parser.parse_args(argv)
    source = args.source_dir.expanduser().resolve()
    prepare_source(source)
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    env.pop("VIRTUAL_ENV", None)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    # Verify the actual source and injected bridge before touching the installed tool.
    print(run("uv", "run", "--locked", "pytest", *TESTS, cwd=source, env=env))
    print(run("uv", "build", "--wheel", cwd=source, env=env))
    requirements = source / "dist" / "requirements.txt"
    run(
        "uv",
        "export",
        "--locked",
        "--no-dev",
        "--no-emit-project",
        "--no-hashes",
        "--output-file",
        str(requirements),
        cwd=source,
        env=env,
    )
    wheels = list((source / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("Expected exactly one built MCP wheel")
    wheel = wheels[0]
    provenance = {
        "upstream": UPSTREAM,
        "revision": REVISION,
        "patch_sha256": hashlib.sha256(PATCH.read_bytes()).hexdigest(),
        "lock_sha256": hashlib.sha256((source / "uv.lock").read_bytes()).hexdigest(),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "source": str(source),
        "wheel": str(wheel),
        "installed": False,
    }
    if args.install:
        print(
            run(
                "uv",
                "tool",
                "install",
                "--force",
                "--from",
                str(wheel),
                "--constraints",
                str(requirements),
                "pyside6-mcp",
                env=env,
            )
        )
        provenance["installed"] = True
    (source / "dist" / "capture-build.json").write_text(
        json.dumps(provenance, indent=2) + "\n"
    )
    print(json.dumps(provenance, indent=2))
    print(
        "Restart the MCP client/server and relaunch bridged apps before capture; existing processes keep old code."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
