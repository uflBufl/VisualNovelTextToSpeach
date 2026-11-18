"""Run unittest modules reached by branch or local Python changes."""

import argparse
import ast
import importlib.util
import subprocess
import sys
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from itertools import pairwise
from pathlib import Path

from scripts.run_ci_unittests import main as run_unittests
from vntts.path_safety import read_regular_text

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("vntts", "scripts", "tests")
FULL_SUITE_FILES = {"pyproject.toml", "uv.lock", ".python-version", "tests/__init__.py"}


def _git(*arguments: str) -> bytes:
    return subprocess.check_output(["git", *arguments], cwd=ROOT)


def changed_paths(base: str) -> set[str]:
    commit = _git("merge-base", "HEAD", base).decode().strip()
    tracked = _git("diff", "--name-only", "-z", "--no-renames", commit)
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z")
    return {value.decode() for value in (tracked + untracked).split(b"\0") if value}


def _module_name(path: str | Path) -> str | None:
    parts = Path(path).with_suffix("").parts
    if not parts or parts[0] not in SOURCE_DIRS:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _module_files() -> dict[str, Path]:
    return {
        name: path
        for directory in SOURCE_DIRS
        for path in (ROOT / directory).rglob("*.py")
        if (name := _module_name(path.relative_to(ROOT))) is not None
    }


def _add_known_module(found: set[str], known: set[str], name: str) -> None:
    while name:
        if name in known:
            found.add(name)
        name = name.rpartition(".")[0]


def _import_names(node: ast.AST, package: str) -> Iterable[str]:
    if isinstance(node, ast.Import):
        return (alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        name = "." * node.level + (node.module or "")
        origin = importlib.util.resolve_name(name, package) if node.level else name
        return (origin, *(f"{origin}.{alias.name}" for alias in node.names))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value.replace("/", ".").removesuffix(".py")
        return (value,) if value.startswith(SOURCE_DIRS) else ()
    return ()


_LOADERS = {
    "__import__",
    "import_module",
    "run_module",
    "run_path",
    "find_spec",
    "spec_from_file_location",
}


def _loader_calls(nodes: list[ast.AST]) -> list[ast.Call]:
    names = _LOADERS | {
        alias.asname
        for node in nodes
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in _LOADERS and alias.asname
    }
    return [
        node
        for node in nodes
        if isinstance(node, ast.Call)
        and (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", "")
        )
        in names
    ]


def _command_arguments(
    nodes: list[ast.AST], known: set[str]
) -> tuple[dict[ast.AST, str], bool]:
    flags = {
        node for node in nodes if isinstance(node, ast.Constant) and node.value == "-m"
    }
    targets: dict[ast.AST, str] = {}
    if flags:
        for node in nodes:
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            for option, argument in pairwise(node.elts):
                if option in flags and isinstance(argument, ast.Constant):
                    flags.discard(option)
                    if isinstance(argument.value, str) and argument.value in known:
                        targets[argument] = argument.value
    return targets, bool(flags)


def _command_edges(
    tree: ast.Module, module: str, arguments: dict[ast.AST, str]
) -> Iterable[tuple[str, str]]:
    # A child process can affect the APIs that launch it without changing
    # unrelated APIs provided by the same module (such as macOS permissions).
    definitions = {
        statement.name
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for statement in tree.body:
        name = getattr(statement, "name", None)
        owner = f"{module}:{name}" if name in definitions else module
        yield owner, f"{module}:*"
        for node in ast.walk(statement):
            if node in arguments:
                yield arguments[node], owner
            if isinstance(node, ast.Name) and node.id in definitions:
                yield f"{module}:{node.id}", owner


def _import_edges(
    node: ast.Import | ast.ImportFrom, package: str, module: str, known: set[str]
) -> Iterable[tuple[str, str]]:
    names = list(_import_names(node, package))
    found: set[str] = set()
    for name in names:
        _add_known_module(found, known, name)
    for source in found:
        yield source, module
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield f"{alias.name}:*", module
    else:
        origin, *members = names
        for alias, member in zip(node.names, members, strict=True):
            if member in known:
                yield f"{member}:*", module
            elif alias.name == "*":
                yield f"{origin}:*", module
            else:
                yield f"{origin}:{alias.name}", module
                yield f"{origin}:__getattr__", module


def _literal_edges(
    name: str, known: set[str], owner: str, runtime: bool
) -> Iterable[tuple[str, str]]:
    found: set[str] = set()
    _add_known_module(found, known, name)
    for source in found:
        # Data such as schema keys and pickle identities do not execute code.
        yield source if runtime else f"file:{source}", owner
    if runtime and found:
        source = max(found, key=len)
        member = name.removeprefix(source).lstrip(".").partition(".")[0] or "*"
        yield f"{source}:{member}", owner


def _lookup_owner(tree: ast.Module, module: str, calls: list[ast.Call]) -> str:
    # A lazy export is loaded by an attribute reader, not by every import of
    # an unrelated child of the package.
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            if calls and set(calls).issubset(ast.walk(node)):
                return f"{module}:__getattr__"
    return module


def _imports(path: Path, module: str, known: set[str]) -> Iterable[tuple[str, str]]:
    tree = ast.parse(read_regular_text(path, encoding="utf-8"), filename=str(path))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    nodes = list(ast.walk(tree))
    calls = _loader_calls(nodes)
    commands, indirect_command = _command_arguments(nodes, known)
    runtime = bool(calls) or indirect_command or module.startswith("tests.")
    if module.startswith("tests."):
        commands.clear()  # Collect the entire test module when it runs a child.
    owner = _lookup_owner(tree, module, calls)
    yield owner, f"{module}:*"
    if commands:
        yield from _command_edges(tree, module, commands)
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield from _import_edges(node, package, module, known)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in commands
        ):
            if node.value.startswith(".github/workflows/"):
                yield node.value, module
            for name in _import_names(node, package):
                yield from _literal_edges(name, known, owner, runtime)


def _changed_modules(changed: set[str]) -> tuple[set[str] | None, str | None]:
    full = sorted(changed & FULL_SUITE_FILES)
    if full:
        return None, f"shared configuration changed: {', '.join(full)}"
    result = set()
    for path in changed:
        if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
            result.add(path)
        elif path == "ui-catalog.json":
            result.add("scripts.render_ui_catalog")
        elif path.endswith(".py"):
            module = _module_name(path)
            if module:
                result.add(module)
            else:
                return None, f"unmapped Python file changed: {path}"
        elif path.startswith(("docs/", ".github/")) or path in {
            "AGENTS.md",
            "README.md",
            "todo.md",
            ".gitignore",
        }:
            continue
        else:
            return None, f"unmapped project file changed: {path}"
    return result, None


def _reverse_dependencies(
    modules: Mapping[str, Path], known: set[str]
) -> defaultdict[str, set[str]]:
    reverse: defaultdict[str, set[str]] = defaultdict(set)
    for module, path in modules.items():
        for imported, dependent in _imports(path, module, known):
            reverse[imported].add(dependent)
    return reverse


def _reachable_modules(
    reverse: defaultdict[str, set[str]], starts: Iterable[str]
) -> set[str]:
    reached = {key for name in starts for key in (name, f"file:{name}")}
    queue = deque(reached)
    while queue:
        for dependent in reverse[queue.popleft()] - reached:
            reached.add(dependent)
            queue.append(dependent)
    return reached


def select_test_modules(
    changed: set[str], modules: Mapping[str, Path] | None = None
) -> tuple[list[str] | None, str | None]:
    changed_modules, reason = _changed_modules(changed)
    if changed_modules is None:
        return None, reason
    if not changed_modules:
        return [], None
    modules = _module_files() if modules is None else modules

    known = set(modules) | changed_modules
    try:
        reverse = _reverse_dependencies(modules, known)
    except (OSError, SyntaxError, ImportError, ValueError) as error:
        return None, f"cannot map Python imports: {error}"

    reached = _reachable_modules(reverse, changed_modules)
    selected = sorted(
        module
        for module in reached
        if module.startswith("tests.test_") and module in modules
    )
    uncovered = []
    for module in changed_modules:
        if not module.startswith(("vntts.", "scripts.")):
            continue
        seen = _reachable_modules(reverse, (module,))
        if not seen.intersection(selected):
            uncovered.append(module)
    if uncovered:
        return None, f"no mapped tests for changed module: {', '.join(uncovered)}"
    return selected, None


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="branch comparison ref")
    parser.add_argument("--local", action="store_true", help="compare only with HEAD")
    parser.add_argument(
        "--list", action="store_true", help="show selection without running"
    )
    options = parser.parse_args(arguments)
    try:
        changed = changed_paths("HEAD" if options.local else options.base)
    except subprocess.CalledProcessError as error:
        print(f"Unable to compare Git changes: {error}", file=sys.stderr)
        return 2
    modules, reason = select_test_modules(changed)
    if modules is None:
        print(f"Running full unittest suite ({reason})")
        return 0 if options.list else run_unittests(["discover", "-s", "tests"])
    if not modules:
        print("No affected unittest modules")
        return 0
    print(f"Selected {len(modules)} unittest modules:", *modules, sep="\n", flush=True)
    selection = "--local-selected" if options.local else "--selected"
    return 0 if options.list else run_unittests([selection, *modules])


if __name__ == "__main__":
    raise SystemExit(main())
