#!/usr/bin/env python3
"""Fail when Ruff complexity findings exceed the versioned baseline."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from collections import Counter
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPOSITORY_ROOT / "tests" / "fixtures" / "ruff-complexity-v1.json"
RULES = ("C901", "PLR0912", "PLR0915")


def scope_at_line(source_path: Path, line: int) -> str | None:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
    scopes: list[tuple[int, int, str]] = []

    def visit(node: ast.AST, parents: tuple[str, ...] = ()) -> None:
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                visit(child, (*parents, node.name))
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_line = getattr(node, "end_lineno", node.lineno)
            scopes.append((node.lineno, end_line, ".".join((*parents, node.name))))
            for child in node.body:
                visit(child, (*parents, node.name))
            return
        for child in ast.iter_child_nodes(node):
            visit(child, parents)

    visit(tree)
    matches = [scope for start, end, scope in scopes if start <= line <= end]
    return max(matches, key=len, default=None)


def finding_identity(root: Path, finding: dict[str, object]) -> tuple[str, str, str]:
    code = finding.get("code")
    filename = finding.get("filename")
    location = finding.get("location")
    if not isinstance(code, str) or not isinstance(filename, str):
        raise ValueError("Ruff finding is missing its code or filename")
    if not isinstance(location, dict) or not isinstance(location.get("row"), int):
        raise ValueError(f"Ruff finding {code} in {filename} is missing its row")
    path = Path(filename)
    if not path.is_absolute():
        path = root / path
    try:
        relative_path = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise ValueError(
            f"Ruff finding is outside the repository: {filename}"
        ) from error
    scope = scope_at_line(path, location["row"])
    if scope is None:
        raise ValueError(
            f"Ruff finding {code} in {relative_path} has no function scope"
        )
    return code, relative_path, scope


def baseline_counts(baseline: object) -> Counter[tuple[str, str, str]]:
    if not isinstance(baseline, dict) or baseline.get("schema_version") != 1:
        raise ValueError("Ruff complexity baseline schema is unsupported")
    findings = baseline.get("findings")
    if not isinstance(findings, list):
        raise ValueError("Ruff complexity baseline inventory is malformed")
    counts: Counter[tuple[str, str, str]] = Counter()
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or finding.get("code") not in RULES
            or not isinstance(finding.get("path"), str)
            or not isinstance(finding.get("scope"), str)
            or not isinstance(finding.get("count"), int)
            or isinstance(finding.get("count"), bool)
            or finding["count"] < 1
        ):
            raise ValueError("Ruff complexity baseline inventory is malformed")
        identity = (finding["code"], finding["path"], finding["scope"])
        if identity in counts:
            raise ValueError("Ruff complexity baseline inventory is malformed")
        counts[identity] = finding["count"]
    if not counts and findings:
        raise ValueError("Ruff complexity baseline inventory is malformed")
    return counts


def check_findings(
    root: Path, baseline: object, findings: list[dict[str, object]]
) -> list[str]:
    allowed = baseline_counts(baseline)
    current = Counter(finding_identity(root, finding) for finding in findings)
    unexpected = sorted(
        (identity, count, allowed[identity])
        for identity, count in current.items()
        if count > allowed[identity]
    )
    return [
        f"new Ruff complexity finding: {code} {path} {scope} ({count} > {allowed})"
        for (code, path, scope), count, allowed in unexpected
    ]


def ruff_findings(root: Path) -> list[dict[str, object]]:
    completed = subprocess.run(
        [
            "ruff",
            "check",
            ".",
            "--select",
            ",".join(RULES),
            "--output-format",
            "json",
            "--exit-zero",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    findings = json.loads(completed.stdout)
    if not isinstance(findings, list) or any(
        not isinstance(finding, dict) for finding in findings
    ):
        raise ValueError("Ruff returned malformed JSON")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    arguments = parser.parse_args(argv)
    try:
        baseline = json.loads(arguments.baseline.read_text(encoding="utf-8"))
        failures = check_findings(
            arguments.root, baseline, ruff_findings(arguments.root)
        )
    except (
        OSError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(f"Ruff complexity ratchet failed: {error}")
        return 1
    if failures:
        print("Ruff complexity ratchet failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Ruff complexity ratchet passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
