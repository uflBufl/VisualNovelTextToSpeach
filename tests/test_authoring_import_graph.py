import ast
import unittest
from pathlib import Path

from vntts.authoring.reconciliation import (
    AuthoringReconciliation,
    AuthoringReconciliationError,
)


def _authoring_import_graph():
    root = Path(__file__).parents[1] / "vntts" / "authoring"
    paths = {
        f"vntts.authoring.{'.'.join(path.relative_to(root).with_suffix('').parts)}": path
        for path in root.rglob("*.py")
        if path.name != "__init__.py"
    }
    modules = set(paths)
    graph = {module: set() for module in modules}
    for module, path in paths.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                graph[module].update(
                    alias.name for alias in node.names if alias.name in modules
                )
            elif isinstance(node, ast.ImportFrom) and node.module in modules:
                graph[module].add(node.module)
    return graph


def _reachable(graph, source, target):
    pending = list(graph[source])
    visited = set()
    while pending:
        module = pending.pop()
        if module == target:
            return True
        if module in visited:
            continue
        visited.add(module)
        pending.extend(graph[module] - visited)
    return False


class AuthoringImportGraphTest(unittest.TestCase):
    def test_public_reconciliation_types_keep_their_module_identity(self):
        self.assertEqual(
            AuthoringReconciliation.__module__, "vntts.authoring.reconciliation"
        )
        self.assertEqual(
            AuthoringReconciliationError.__module__, "vntts.authoring.reconciliation"
        )

    def test_reconciliation_schema_dependency_is_one_way(self):
        graph = _authoring_import_graph()
        reconciliation = "vntts.authoring.reconciliation"
        schema = "vntts.authoring.reconciliation_schema"
        authority = "vntts.authoring.authority"

        self.assertIn(schema, graph[reconciliation])
        self.assertEqual(graph[schema], {authority})
        self.assertFalse(_reachable(graph, schema, reconciliation))


if __name__ == "__main__":
    unittest.main()
