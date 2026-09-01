import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

import vntts.authoring.game_pack as game_pack_module
import vntts.authoring.terminal_conflict_workspace as terminal_workspace_module
from vntts.authoring.bulk_generation import (
    SpeechQuality as BulkSpeechQuality,
)
from vntts.authoring.bulk_generation import (
    _approved_manifest_entries,
    _generated_mono_pcm,
    _GenerationLease,
    _inline_pause_matches_failure,
    _load_stable_queue,
    _process_started_at,
    _review_generation_cohort,
    _sentence_repair_matches_failure,
    _snapshot_control_files,
    _write_generated_manifest_from_state,
    generated_mono_pcm,
    inline_pause_matches_failure,
    review_generation_cohort,
    sentence_repair_matches_failure,
    snapshot_generation_control_files,
)
from vntts.authoring.bulk_generation import (
    validate_generation_state_document as bulk_validate_generation_state_document,
)
from vntts.authoring.failure_reference_binding import (
    FailureReferenceBinding,
    FailureReferenceBindingError,
)
from vntts.authoring.generation_lease import GenerationLease, process_started_at
from vntts.authoring.generation_manifest import (
    approved_manifest_entries,
    write_generated_manifest_from_state,
)
from vntts.authoring.generation_state import (
    load_stable_generation_queue,
    validate_generation_state_document,
)
from vntts.authoring.publication import rename_directory_no_replace
from vntts.authoring.reconciliation import (
    AuthoringReconciliation,
    AuthoringReconciliationError,
)
from vntts.authoring.source_reference_quality import (
    SourceReferenceQualityError,
    SourceReferenceQualityResult,
)
from vntts.authoring.speech_quality import SpeechQuality
from vntts.authoring.terminal_conflict_records import is_terminal_review_outcome
from vntts.authoring.terminal_conflict_workspace import (
    merge_terminal_conflict_resolution,
)
from vntts.authoring.workbench import (
    _selected_voice_manifest,
    _terminal_review_outcome,
    _workspace_config_fingerprint,
)
from vntts.authoring.workbench import (
    merge_terminal_conflict_resolution as compatibility_merge_terminal_conflict_resolution,
)
from vntts.authoring.workspace_config import (
    normalize_workspace_run_config,
    selected_voice_manifest_path,
    workspace_config_fingerprint,
)
from vntts.authoring.workspace_voice_runtime import load_workspace_voice_registry


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


def _production_importers(module, imported_name):
    root = Path(__file__).parents[1] / "vntts"
    importers = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == module
            and any(alias.name == imported_name for alias in node.names)
            for node in ast.walk(tree)
        ):
            importers.append(path.relative_to(root.parent).as_posix())
    return sorted(importers)


class AuthoringImportGraphTest(unittest.TestCase):
    def test_public_facade_is_lazy_and_keeps_its_export_inventory(self):
        script = """
import hashlib
import json
import sys
import vntts.authoring as authoring
print(json.dumps({
    "count": len(authoring.__all__),
    "digest": hashlib.sha256(json.dumps(authoring.__all__, separators=(",", ":")).encode()).hexdigest(),
    "workbench": "vntts.authoring.workbench" in sys.modules,
    "pyside": any(name.startswith("PySide") for name in sys.modules),
}))
"""
        observed = json.loads(
            subprocess.check_output([sys.executable, "-c", script], text=True)
        )
        self.assertEqual(observed["count"], 435)
        self.assertEqual(
            observed["digest"],
            "f14ac35bb1c82bdcadade461a175a3a39f3747371ec3ae49c2533a013e99369d",
        )
        self.assertFalse(observed["workbench"])
        self.assertFalse(observed["pyside"])

    def test_speech_quality_is_independent_from_bulk_orchestration(self):
        self.assertIs(BulkSpeechQuality, SpeechQuality)
        self.assertFalse(
            _reachable(
                _authoring_import_graph(),
                "vntts.authoring.speech_quality",
                "vntts.authoring.bulk_generation",
            )
        )

    def test_generation_state_semantics_are_owned_by_the_foundation(self):
        self.assertIs(
            bulk_validate_generation_state_document,
            validate_generation_state_document,
        )
        graph = _authoring_import_graph()
        self.assertFalse(
            _reachable(
                graph,
                "vntts.authoring.generation_state",
                "vntts.authoring.bulk_generation",
            )
        )
        bulk_path = (
            Path(__file__).parents[1] / "vntts" / "authoring" / "bulk_generation.py"
        )
        definitions = {
            node.name
            for node in ast.parse(bulk_path.read_text(encoding="utf-8")).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertNotIn("_validate_state_document", definitions)

    def test_workspace_config_and_voice_runtime_preserve_caller_error_type(self):
        class DomainError(RuntimeError):
            pass

        with self.assertRaisesRegex(
            DomainError, "Workspace run configuration is malformed"
        ):
            normalize_workspace_run_config(None, error_type=DomainError)
        with self.assertRaisesRegex(
            DomainError, "Workspace has no voice manifest snapshot"
        ):
            load_workspace_voice_registry(Path.cwd(), {}, error_type=DomainError)

    def test_canonical_document_hash_does_not_import_bulk_private_helper(self):
        self.assertEqual(
            _production_importers(
                "vntts.authoring.bulk_generation", "_canonical_sha256"
            ),
            [],
        )

    def test_workspace_foundation_primitives_have_no_private_workbench_importers(self):
        for imported_name in (
            "_copy_workspace_tree_snapshot",
            "_failure_reference_runtime_binding",
            "_load_json",
            "_load_workspace",
            "_load_workspace_snapshot",
            "_merge_workspace_outcomes",
            "_read_file_bytes",
            "_rename_directory_no_replace",
            "_require_sha256",
            "_safe_relative",
            "_selected_voice_manifest",
            "_stable_workspace_state",
            "_terminal_review_outcome",
            "_within",
            "_workspace_config_fingerprint",
            "_workspace_failure_repair_policy",
            "_workspace_missing_voice_policy",
            "_workspace_queue_voice_overrides",
            "_workspace_run_config_with_policy",
            "_workspace_voice_registry",
            "_validate_workspace_carry_forward",
            "_validate_workspace_input_config",
            "_validate_workspace_offline_fallback_state",
            "_validate_workspace_outcome_merge",
            "_validate_workspace_terminal_conflict_merge",
        ):
            with self.subTest(imported_name=imported_name):
                self.assertEqual(
                    _production_importers("vntts.authoring.workbench", imported_name),
                    [],
                )
        self.assertEqual(
            _authoring_import_graph()["vntts.authoring.workspace_foundation"],
            set(),
        )
        self.assertIs(_workspace_config_fingerprint, workspace_config_fingerprint)
        self.assertIsNone(_selected_voice_manifest(Path.cwd(), {}))
        self.assertIsNone(selected_voice_manifest_path(Path.cwd(), {}))
        terminal = {"status": "approved", "review_status": "approved"}
        self.assertIs(_terminal_review_outcome, is_terminal_review_outcome)
        self.assertTrue(_terminal_review_outcome(terminal))
        self.assertTrue(is_terminal_review_outcome(terminal))
        self.assertFalse(
            _reachable(
                _authoring_import_graph(),
                "vntts.authoring.workspace_config",
                "vntts.authoring.workbench",
            )
        )
        self.assertFalse(
            _reachable(
                _authoring_import_graph(),
                "vntts.authoring.workspace_state",
                "vntts.authoring.workbench",
            )
        )
        self.assertFalse(
            _reachable(
                _authoring_import_graph(),
                "vntts.authoring.workspace_voice_runtime",
                "vntts.authoring.workbench",
            )
        )

    def test_generation_lease_has_no_private_bulk_generation_importers(self):
        for imported_name in ("_GenerationLease", "_process_started_at"):
            with self.subTest(imported_name=imported_name):
                self.assertEqual(
                    _production_importers(
                        "vntts.authoring.bulk_generation", imported_name
                    ),
                    [],
                )
        self.assertIs(_GenerationLease, GenerationLease)
        self.assertIs(_process_started_at, process_started_at)
        graph = _authoring_import_graph()
        self.assertFalse(
            _reachable(
                graph,
                "vntts.authoring.generation_lease",
                "vntts.authoring.bulk_generation",
            )
        )

    def test_generation_manifest_has_no_private_bulk_generation_importers(self):
        for imported_name in (
            "_approved_manifest_entries",
            "_write_generated_manifest_from_state",
        ):
            with self.subTest(imported_name=imported_name):
                self.assertEqual(
                    _production_importers(
                        "vntts.authoring.bulk_generation", imported_name
                    ),
                    [],
                )
        self.assertIs(_approved_manifest_entries, approved_manifest_entries)
        self.assertIs(
            _write_generated_manifest_from_state,
            write_generated_manifest_from_state,
        )
        graph = _authoring_import_graph()
        self.assertFalse(
            _reachable(
                graph,
                "vntts.authoring.generation_manifest",
                "vntts.authoring.bulk_generation",
            )
        )

    def test_bulk_generation_compatibility_helpers_have_no_private_importers(self):
        compatibility_names = (
            "_generated_mono_pcm",
            "_inline_pause_matches_failure",
            "_load_stable_queue",
            "_review_generation_cohort",
            "_sentence_repair_matches_failure",
            "_snapshot_control_files",
            "_validate_state_document",
        )
        for imported_name in compatibility_names:
            with self.subTest(imported_name=imported_name):
                self.assertEqual(
                    _production_importers(
                        "vntts.authoring.bulk_generation", imported_name
                    ),
                    [],
                )
        self.assertIs(_load_stable_queue, load_stable_generation_queue)
        self.assertIs(_generated_mono_pcm, generated_mono_pcm)
        self.assertIs(_inline_pause_matches_failure, inline_pause_matches_failure)
        self.assertIs(_review_generation_cohort, review_generation_cohort)
        self.assertIs(_sentence_repair_matches_failure, sentence_repair_matches_failure)
        self.assertIs(_snapshot_control_files, snapshot_generation_control_files)
        graph = _authoring_import_graph()
        self.assertFalse(
            _reachable(
                graph,
                "vntts.authoring.generation_state",
                "vntts.authoring.bulk_generation",
            )
        )

    def test_authoring_module_graph_has_no_strongly_connected_components(self):
        graph = _authoring_import_graph()
        cycles = []
        modules = sorted(graph)
        for position, source in enumerate(modules):
            for target in modules[position + 1 :]:
                if _reachable(graph, source, target) and _reachable(
                    graph, target, source
                ):
                    cycles.append((source, target))

        self.assertEqual(cycles, [])

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

    def test_terminal_workspace_application_is_a_leaf_orchestration_module(self):
        graph = _authoring_import_graph()
        terminal_workspace = "vntts.authoring.terminal_conflict_workspace"
        workbench = "vntts.authoring.workbench"

        self.assertEqual(
            merge_terminal_conflict_resolution.__module__, terminal_workspace
        )
        self.assertEqual(
            compatibility_merge_terminal_conflict_resolution.__module__, workbench
        )
        self.assertIn(workbench, graph[terminal_workspace])
        self.assertNotIn(terminal_workspace, graph[workbench])
        self.assertFalse(_reachable(graph, workbench, terminal_workspace))
        self.assertIs(
            terminal_workspace_module.rename_directory_no_replace,
            rename_directory_no_replace,
        )
        self.assertIs(
            game_pack_module._rename_directory_no_replace,
            rename_directory_no_replace,
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

        self.assertEqual(graph[records], {"vntts.authoring.advisory_lock"})
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
