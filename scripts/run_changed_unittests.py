"""Run unittest modules reached by branch or local Python changes."""

import argparse
import ast
import importlib.util
import subprocess
import sys
from collections import defaultdict, deque
from pathlib import Path

from scripts.run_ci_unittests import main as run_unittests

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("vntts", "scripts", "tests")
FULL_SUITE_FILES = {"pyproject.toml", "uv.lock", ".python-version", "tests/__init__.py"}


def _git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT)


def changed_paths(base):
    commit = _git("merge-base", "HEAD", base).decode().strip()
    tracked = _git("diff", "--name-only", "-z", "--no-renames", commit)
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z")
    return {value.decode() for value in (tracked + untracked).split(b"\0") if value}


def _module_name(path):
    parts = Path(path).with_suffix("").parts
    if not parts or parts[0] not in SOURCE_DIRS:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _module_files():
    return {
        _module_name(path.relative_to(ROOT)): path
        for directory in SOURCE_DIRS
        for path in (ROOT / directory).rglob("*.py")
    }


def _add_known_module(found, known, name):
    while name:
        if name in known:
            found.add(name)
        name = name.rpartition(".")[0]


def _import_names(node, package):
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


def _imports(path, module, known):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    for node in ast.walk(tree):
        for name in _import_names(node, package):
            _add_known_module(found, known, name)
    return found


def _changed_modules(changed):
    full = sorted(changed & FULL_SUITE_FILES)
    if full:
        return None, f"shared configuration changed: {', '.join(full)}"
    result = set()
    for path in changed:
        if path == "ui-catalog.json":
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


def _reverse_dependencies(modules, known):
    reverse = defaultdict(set)
    for module, path in modules.items():
        for imported in _imports(path, module, known):
            reverse[imported].add(module)
    return reverse


def _reachable_modules(reverse, starts):
    reached = set(starts)
    queue = deque(starts)
    while queue:
        for dependent in reverse[queue.popleft()] - reached:
            reached.add(dependent)
            queue.append(dependent)
    return reached


def select_test_modules(changed, modules=None):
    modules = _module_files() if modules is None else modules
    changed_modules, reason = _changed_modules(changed)
    if reason:
        return None, reason

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


def main(arguments=None):
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
    return 0 if options.list else run_unittests(["--selected", *modules])


if __name__ == "__main__":
    raise SystemExit(main())
