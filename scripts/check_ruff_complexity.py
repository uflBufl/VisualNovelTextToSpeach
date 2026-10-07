#!/usr/bin/env python3
"""Fail when Ruff complexity findings exceed the versioned baseline."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import NamedTuple

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPOSITORY_ROOT / "tests" / "fixtures" / "ruff-complexity-v2.json"
RULES = ("C901", "PLR0912", "PLR0915")


def scope_at_line(source_path: Path, line: int) -> str | None:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
    return _scope_at_line(tree, line)


def _scope_at_line(tree: ast.AST, line: int) -> str | None:
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
        for descendant in ast.iter_child_nodes(node):
            visit(descendant, parents)

    visit(tree)
    matches = [scope for start, end, scope in scopes if start <= line <= end]
    return max(matches, key=len, default=None)


def finding_identity(root: Path, finding: dict[str, object]) -> tuple[str, str, str]:
    return _finding_identity(root, finding, {})


def _finding_identity(
    root: Path, finding: dict[str, object], source_trees: dict[Path, ast.AST]
) -> tuple[str, str, str]:
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
    cache_key = path.resolve()
    if cache_key not in source_trees:
        source_trees[cache_key] = ast.parse(
            path.read_text(encoding="utf-8"), filename=path
        )
    scope = _scope_at_line(source_trees[cache_key], location["row"])
    if scope is None:
        raise ValueError(
            f"Ruff finding {code} in {relative_path} has no function scope"
        )
    return code, relative_path, scope


class _Allowance(NamedTuple):
    occurrences: int
    maximum: int


def baseline_allowances(baseline: object) -> dict[tuple[str, str, str], _Allowance]:
    if (
        not isinstance(baseline, dict)
        or type(baseline.get("schema_version")) is not int
        or baseline["schema_version"] != 2
    ):
        raise ValueError("Ruff complexity baseline schema is unsupported")
    findings = baseline.get("findings")
    if not isinstance(findings, list):
        raise ValueError("Ruff complexity baseline inventory is malformed")
    allowances: dict[tuple[str, str, str], _Allowance] = {}
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or set(finding) != {"code", "path", "scope", "count", "maximum"}
            or finding.get("code") not in RULES
            or not isinstance(finding.get("path"), str)
            or not isinstance(finding.get("scope"), str)
            or type(finding.get("count")) is not int
            or finding["count"] < 1
            or type(finding.get("maximum")) is not int
            or finding["maximum"] < 1
        ):
            raise ValueError("Ruff complexity baseline inventory is malformed")
        identity = (finding["code"], finding["path"], finding["scope"])
        if identity in allowances:
            raise ValueError("Ruff complexity baseline inventory is malformed")
        allowances[identity] = _Allowance(finding["count"], finding["maximum"])
    return allowances


def finding_metric(finding: dict[str, object]) -> int:
    """Read the numeric measurement from the three pinned Ruff diagnostics."""
    message = finding.get("message")
    match = (
        re.search(r"\(([0-9]+) > ([0-9]+)\)$", message)
        if isinstance(message, str) and finding.get("code") in RULES
        else None
    )
    if match is None:
        raise ValueError("Ruff complexity finding is missing its numeric measurement")
    metric, threshold = map(int, match.groups())
    if metric <= threshold:
        raise ValueError("Ruff complexity finding has an invalid numeric measurement")
    return metric


def check_findings(
    root: Path, baseline: object, findings: list[dict[str, object]]
) -> list[str]:
    allowed = baseline_allowances(baseline)
    source_trees: dict[Path, ast.AST] = {}
    current: Counter[tuple[str, str, str]] = Counter()
    metrics: dict[tuple[str, str, str], int] = {}
    for finding in findings:
        identity = _finding_identity(root, finding, source_trees)
        metric = finding_metric(finding)
        current[identity] += 1
        metrics[identity] = max(metric, metrics.get(identity, 0))
    unexpected = sorted(
        (identity, count, allowed.get(identity, _Allowance(0, 0)).occurrences)
        for identity, count in current.items()
        if count > allowed.get(identity, _Allowance(0, 0)).occurrences
    )
    stale = sorted(
        (identity, allowance.occurrences, current[identity])
        for identity, allowance in allowed.items()
        if current[identity] < allowance.occurrences
    )
    failures = [
        f"new Ruff complexity finding: {code} {path} {scope} ({count} > {allowed})"
        for (code, path, scope), count, allowed in unexpected
    ] + [
        f"stale Ruff complexity allowance: {code} {path} {scope} ({count} > {actual})"
        for (code, path, scope), count, actual in stale
    ]
    for identity, metric in sorted(metrics.items()):
        allowance = allowed.get(identity)
        if allowance is None or metric == allowance.maximum:
            continue
        code, path, scope = identity
        if metric > allowance.maximum:
            failures.append(
                f"Ruff complexity increased: {code} {path} {scope} "
                f"({metric} > {allowance.maximum})"
            )
        else:
            failures.append(
                f"stale Ruff complexity metric allowance: {code} {path} {scope} "
                f"({allowance.maximum} > {metric})"
            )
    return failures


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
