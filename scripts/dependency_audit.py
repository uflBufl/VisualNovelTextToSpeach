"""Audit every lock, including Git revisions unsupported by ``uv audit``."""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
OSV_QUERY_BATCH = "https://api.osv.dev/v1/querybatch"
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")

# These paths are absent from VNTTS runtime execution. The pinned model stacks
# cannot yet take the published fixes without breaking their qualified runtimes.
IGNORES = {
    ".": (
        "GHSA-h35f-9h28-mq5c",  # setuptools sdist creation
        "GHSA-rrmf-rvhw-rf47",  # torch.jit.script
        "GHSA-29pf-2h5f-8g72",  # untrusted AutoModel configuration
        "GHSA-69w3-r845-3855",  # Trainer checkpoint loading
        "GHSA-fgcw-684q-jj6r",  # LightGlue model loading
        "GHSA-xrqw-3rrv-vx5w",  # save_pretrained chat templates
        "PYSEC-2025-217",  # no fixed release; affected model path unused
    ),
    "backends/chatterbox-nano": (
        "GHSA-7wx4-6vff-v64p",  # custom DiffusionPipeline loading
        "GHSA-98h9-4798-4q5v",  # custom DiffusionPipeline loading
        "PYSEC-2026-41",  # custom DiffusionPipeline loading
        "GHSA-h35f-9h28-mq5c",  # setuptools sdist creation
        "GHSA-rrmf-rvhw-rf47",  # torch.jit.script
        "GHSA-29pf-2h5f-8g72",  # untrusted AutoModel configuration
        "GHSA-fgcw-684q-jj6r",  # LightGlue model loading
        "GHSA-xrqw-3rrv-vx5w",  # save_pretrained chat templates
    ),
    "backends/moss-soundeffect-v2": (
        "GHSA-7wx4-6vff-v64p",  # custom DiffusionPipeline loading
        "GHSA-98h9-4798-4q5v",  # custom DiffusionPipeline loading
        "PYSEC-2026-41",  # custom DiffusionPipeline loading
        "GHSA-qfhq-4f3w-5fph",  # torch.lstm_cell
        "GHSA-rrmf-rvhw-rf47",  # torch.jit.script
        "PYSEC-2026-139",  # no fixed release; affected torch path unused
        "PYSEC-2026-2286",  # torch.lstm_cell alias not linked by the feed
        "GHSA-29pf-2h5f-8g72",  # untrusted AutoModel configuration
        "GHSA-69w3-r845-3855",  # Trainer checkpoint loading
        "GHSA-fgcw-684q-jj6r",  # LightGlue model loading
        "GHSA-xrqw-3rrv-vx5w",  # save_pretrained chat templates
        "PYSEC-2025-217",  # no fixed release; affected model path unused
        "PYSEC-2025-218",  # no fixed release; affected model path unused
    ),
    "backends/moss-tts-delay": (
        "GHSA-4j2p-28q2-5m79",  # loading an untrusted sharded checkpoint
        "GHSA-h35f-9h28-mq5c",  # setuptools sdist creation
        "GHSA-rrmf-rvhw-rf47",  # torch.jit.script
        "GHSA-29pf-2h5f-8g72",  # untrusted AutoModel configuration
        "GHSA-fgcw-684q-jj6r",  # LightGlue model loading
        "GHSA-xrqw-3rrv-vx5w",  # save_pretrained chat templates
    ),
}


def projects() -> tuple[Path, ...]:
    return (
        ROOT,
        *(path.parent for path in sorted((ROOT / "backends").glob("*/uv.lock"))),
    )


def project_label(project: Path) -> str:
    return "." if project == ROOT else project.relative_to(ROOT).as_posix()


def locked_git_commits(lock_paths: tuple[Path, ...]) -> tuple[str, ...]:
    commits: set[str] = set()
    for lock_path in lock_paths:
        document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        for package in document.get("package", ()):
            git = package.get("source", {}).get("git")
            if git is None:
                continue
            commit = urlsplit(git).fragment
            if COMMIT_PATTERN.fullmatch(commit) is None:
                raise ValueError(f"Git dependency is not locked to a commit: {git}")
            commits.add(commit)
    return tuple(sorted(commits))


def query_osv(commits: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    if not commits:
        return {}
    request = urllib.request.Request(
        OSV_QUERY_BATCH,
        data=json.dumps({"queries": [{"commit": value} for value in commits]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    return {
        commit: tuple(item["id"] for item in result.get("vulns", ()))
        for commit, result in zip(commits, payload["results"], strict=True)
        if result.get("vulns")
    }


def main() -> None:
    resolved_projects = projects()
    for project in resolved_projects:
        label = project_label(project)
        command = [
            "uv",
            "audit",
            "--project",
            str(project),
            "--frozen",
            "--preview-features",
            "audit-command",
        ]
        for advisory in IGNORES.get(label, ()):
            command.extend(("--ignore", advisory))
        subprocess.run(command, cwd=ROOT, check=True)

    commits = locked_git_commits(
        tuple(project / "uv.lock" for project in resolved_projects)
    )
    if vulnerabilities := query_osv(commits):
        details = ", ".join(
            f"{commit}: {', '.join(advisories)}"
            for commit, advisories in vulnerabilities.items()
        )
        raise SystemExit(f"Vulnerable Git dependencies: {details}")
    print(f"Audited {len(resolved_projects)} locks and {len(commits)} Git revisions")


if __name__ == "__main__":
    main()
