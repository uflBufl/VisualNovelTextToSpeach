import ast
import unittest
from pathlib import Path

from vntts.authoring.failure_reference_binding import (
    FailureReferenceBinding,
    FailureReferenceBindingError,
)
from vntts.authoring.reconciliation import (
    AuthoringReconciliation,
    AuthoringReconciliationError,
)
from vntts.authoring.source_reference_quality import (
    SourceReferenceQualityError,
    SourceReferenceQualityResult,
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
    def test_extracted_record_types_keep_their_public_module_identity(self):
        self.assertEqual(
            FailureReferenceBinding.__module__,
            "vntts.authoring.failure_reference_binding",
        )
        self.assertEqual(
            FailureReferenceBindingError.__module__,
            "vntts.authoring.failure_reference_binding",
        )
        self.assertEqual(
            SourceReferenceQualityResult.__module__,
            "vntts.authoring.source_reference_quality",
        )
        self.assertEqual(
            SourceReferenceQualityError.__module__,
            "vntts.authoring.source_reference_quality",
        )

    def test_import_destination_policy_does_not_depend_on_importers(self):
        graph = _authoring_import_graph()
        paths = "vntts.authoring.import_paths"
        legacy = "vntts.authoring.legacy_import"
        listening = "vntts.authoring.listening_import"

        self.assertIn(paths, graph[legacy])
        self.assertIn(paths, graph[listening])
        self.assertIn(listening, graph[legacy])
        self.assertFalse(_reachable(graph, listening, legacy))
        self.assertFalse(_reachable(graph, paths, legacy))
        self.assertFalse(_reachable(graph, paths, listening))

    def test_listening_core_does_not_depend_on_its_presentations(self):
        graph = _authoring_import_graph()
        core = "vntts.authoring.listening"
        cli = "vntts.authoring.listening_cli"
        ui = "vntts.authoring.listening_ui"

        self.assertIn(core, graph[cli])
        self.assertIn(ui, graph[cli])
        self.assertIn(core, graph[ui])
        self.assertFalse(_reachable(graph, core, cli))
        self.assertFalse(_reachable(graph, core, ui))

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

    def test_source_reference_quality_records_break_presentation_cycle(self):
        graph = _authoring_import_graph()
        records = "vntts.authoring.source_reference_quality_records"
        modules = {
            "vntts.authoring.reference_composite",
            "vntts.authoring.source_reference_quality",
            "vntts.authoring.source_reference_quality_ui",
            "vntts.authoring.source_reference_review",
        }

        self.assertEqual(graph[records], set())
        for module in modules:
            self.assertFalse(_reachable(graph, records, module))
            for peer in modules - {module}:
                self.assertFalse(
                    _reachable(graph, module, peer) and _reachable(graph, peer, module)
                )

    def test_failure_reference_records_break_projection_cycle(self):
        graph = _authoring_import_graph()
        records = "vntts.authoring.failure_reference_binding_records"
        modules = {
            "vntts.authoring.failure_reference_audit",
            "vntts.authoring.failure_reference_binding",
            "vntts.authoring.game_pack",
            "vntts.authoring.workbench",
        }

        self.assertEqual(graph[records], {"vntts.authoring.source_reference_bindings"})
        for module in modules:
            self.assertFalse(_reachable(graph, records, module))
            for peer in modules - {module}:
                self.assertFalse(
                    _reachable(graph, module, peer) and _reachable(graph, peer, module)
                )


if __name__ == "__main__":
    unittest.main()
