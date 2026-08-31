#!/usr/bin/env python3
"""Fail when dependency or complexity debt grows beyond its checked-in baseline."""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_ROOT = REPOSITORY_ROOT / "vntts"
DEFAULT_BASELINE = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "maintainability-baseline-v1.json"
)


@dataclass(frozen=True)
class Inventory:
    private_imports: frozenset[str]
    module_lines: dict[str, int]
    function_lines: dict[str, int]
    function_complexity: dict[str, int]

    def as_document(self, thresholds: dict[str, int]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "thresholds": thresholds,
            "private_imports": sorted(self.private_imports),
            "module_lines": dict(sorted(self.module_lines.items())),
            "function_lines": dict(sorted(self.function_lines.items())),
            "function_complexity": dict(sorted(self.function_complexity.items())),
        }


def _module_name(source_root: Path, path: Path) -> str:
    relative = path.relative_to(source_root.parent).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolved_import(current_package: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = current_package.split(".")
    retained = package[: len(package) - (node.level - 1)]
    if node.module:
        retained.extend(node.module.split("."))
    return ".".join(retained)


class _FunctionInventory(ast.NodeVisitor):
    def __init__(self, module: str):
        self.module = module
        self.scope: list[str] = []
        self.lines: dict[str, int] = {}
        self.complexity: dict[str, int] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_function(node)

    def _record_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self.scope.append(node.name)
        qualified = ".".join((self.module, *self.scope))
        self.lines[qualified] = (node.end_lineno or node.lineno) - node.lineno + 1
        self.complexity[qualified] = _cyclomatic_complexity(node)
        self.generic_visit(node)
        self.scope.pop()


class _ComplexityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.value = 1
        self.root: ast.AST | None = None

    def measure(self, node: ast.AST) -> int:
        self.root = node
        self.generic_visit(node)
        return self.value

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node is self.root:
            self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self.value += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self.value += 1
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.value += 1
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self.value += 1
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.value += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.value += max(0, len(node.values) - 1)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self.value += len(node.handlers) + bool(node.orelse)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        self.value += max(0, len(node.cases) - 1)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.value += 1 + len(node.ifs)
        self.generic_visit(node)


def _cyclomatic_complexity(node: ast.AST) -> int:
    return _ComplexityVisitor().measure(node)


def build_inventory(source_root: Path, thresholds: dict[str, int]) -> Inventory:
    private_imports: set[str] = set()
    module_lines: dict[str, int] = {}
    function_lines: dict[str, int] = {}
    function_complexity: dict[str, int] = {}
    for path in sorted(source_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        module = _module_name(source_root, path)
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        tree = ast.parse(source, filename=str(path))
        line_count = len(source.splitlines())
        if line_count > thresholds["module_lines"]:
            module_lines[module] = line_count
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported_module = _resolved_import(package, node)
            if not imported_module.startswith("vntts") or imported_module == module:
                continue
            for alias in node.names:
                if alias.name.startswith("_") and alias.name != "__future__":
                    private_imports.add(f"{module} -> {imported_module}:{alias.name}")
        functions = _FunctionInventory(module)
        functions.visit(tree)
        function_lines.update(
            (name, value)
            for name, value in functions.lines.items()
            if value > thresholds["function_lines"]
        )
        function_complexity.update(
            (name, value)
            for name, value in functions.complexity.items()
            if value > thresholds["function_complexity"]
        )
    return Inventory(
        private_imports=frozenset(private_imports),
        module_lines=module_lines,
        function_lines=function_lines,
        function_complexity=function_complexity,
    )


def check_repository(source_root: Path, baseline_path: Path) -> list[str]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if baseline.get("schema_version") != 1:
        return ["Maintainability baseline schema is unsupported"]
    thresholds = baseline.get("thresholds")
    if not isinstance(thresholds, dict) or set(thresholds) != {
        "module_lines",
        "function_lines",
        "function_complexity",
    }:
        return ["Maintainability thresholds are malformed"]
    inventory = build_inventory(source_root, thresholds)
    failures: list[str] = []
    allowed_imports = set(baseline.get("private_imports", ()))
    for dependency in sorted(inventory.private_imports - allowed_imports):
        failures.append(f"new cross-module private import: {dependency}")
    for category in ("module_lines", "function_lines", "function_complexity"):
        allowed = baseline.get(category, {})
        current = getattr(inventory, category)
        for name, value in sorted(current.items()):
            ceiling = allowed.get(name)
            if ceiling is None:
                failures.append(
                    f"new {category} debt: {name} is {value} "
                    f"(threshold {thresholds[category]})"
                )
            elif value > ceiling:
                failures.append(
                    f"grown {category} debt: {name} is {value} (baseline {ceiling})"
                )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="Print the current baseline-shaped inventory without writing it",
    )
    arguments = parser.parse_args(argv)
    baseline = json.loads(arguments.baseline.read_text(encoding="utf-8"))
    if arguments.inventory:
        inventory = build_inventory(arguments.source_root, baseline["thresholds"])
        print(json.dumps(inventory.as_document(baseline["thresholds"]), indent=2))
        return 0
    failures = check_repository(arguments.source_root, arguments.baseline)
    if failures:
        print("Maintainability ratchet failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("Maintainability ratchet passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
