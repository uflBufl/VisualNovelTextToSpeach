import json
import pickle
import subprocess
import sys
import unittest
from pathlib import Path

import vntts.authoring.bulk_generation as bulk_generation_module
import vntts.authoring.generation_failures as failure_module
import vntts.authoring.generation_lease as lease_module
import vntts.authoring.generation_review as review_module
from vntts.authoring.bulk_generation import (
    SpeechQuality as BulkSpeechQuality,
)
from vntts.authoring.bulk_generation import (
    _GenerationLease,
)
from vntts.authoring.failure_reference_binding import (
    FailureReferenceBinding,
    FailureReferenceBindingError,
)
from vntts.authoring.generation_lease import GenerationLease
from vntts.authoring.reconciliation import (
    AuthoringReconciliation,
    AuthoringReconciliationError,
)
from vntts.authoring.source_reference_quality import (
    SourceReferenceQualityError,
    SourceReferenceQualityResult,
)
from vntts.authoring.speech_quality import SpeechQuality
from vntts.authoring.workspace_config import (
    normalize_workspace_run_config,
)
from vntts.authoring.workspace_voice_runtime import load_workspace_voice_registry


class AuthoringImportGraphTest(unittest.TestCase):
    def test_public_facade_import_is_lazy(self):
        script = """
import json
import sys
import vntts.authoring
print(json.dumps({
    "workbench": "vntts.authoring.workbench" in sys.modules,
    "pyside": any(name.startswith("PySide") for name in sys.modules),
}))
"""
        observed = json.loads(
            subprocess.check_output([sys.executable, "-c", script], text=True)
        )
        self.assertFalse(observed["workbench"])
        self.assertFalse(observed["pyside"])

    def test_player_generation_import_excludes_expert_workflows(self):
        script = """
import json
import sys
import vntts.authoring.cli_generation
blocked = (
    "vntts.authoring.cli",
    "vntts.authoring.cli_workspace",
    "vntts.authoring.legacy_import",
    "vntts.authoring.reconciliation",
    "vntts.authoring.reconciliation_merge",
    "vntts.authoring.workspace_outcome_merge",
)
print(json.dumps([name for name in blocked if name in sys.modules]))
"""
        loaded = json.loads(
            subprocess.check_output([sys.executable, "-c", script], text=True)
        )
        self.assertEqual(loaded, [])

    def test_speech_quality_compatibility_export(self):
        self.assertIs(BulkSpeechQuality, SpeechQuality)

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

    def test_generation_lease_compatibility_exports(self):
        self.assertIs(_GenerationLease, GenerationLease)

    def test_generation_fallback_import_does_not_load_bulk_execution(self):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import vntts.authoring.generation_fallback; assert 'vntts.authoring.bulk_generation' not in sys.modules",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_generation_failures_compatibility_exports(self):
        self.assertIs(
            bulk_generation_module.IncompleteSynthesisError,
            failure_module.IncompleteSynthesisError,
        )
        self.assertEqual(
            failure_module.IncompleteSynthesisError.__module__,
            "vntts.authoring.bulk_generation",
        )

    def test_generation_failures_import_does_not_load_bulk_execution(self):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import vntts.authoring.generation_failures; assert 'vntts.authoring.bulk_generation' not in sys.modules",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_generation_review_compatibility_exports_and_pickle_records(self):
        names = (
            "ReviewAuthority",
            "ReviewCommit",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(bulk_generation_module, name), getattr(review_module, name)
                )
        self.assertIs(
            bulk_generation_module.BulkGenerationSourceChangedError,
            lease_module.BulkGenerationSourceChangedError,
        )
        authority = review_module.ReviewAuthority("queue", "state", "item", "audio")
        commit = review_module.ReviewCommit(
            "queue-id", "approved", "approved", "time", authority
        )
        for value in (
            authority,
            commit,
            lease_module.BulkGenerationSourceChangedError("changed"),
        ):
            with self.subTest(record=type(value).__name__):
                self.assertEqual(
                    type(value).__module__, "vntts.authoring.bulk_generation"
                )
                restored = pickle.loads(pickle.dumps(value))
                self.assertIs(type(restored), type(value))
                self.assertEqual(
                    restored.args if isinstance(value, Exception) else restored,
                    value.args if isinstance(value, Exception) else value,
                )

    def test_generation_review_import_does_not_load_bulk_execution(self):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import vntts.authoring.generation_review; assert 'vntts.authoring.bulk_generation' not in sys.modules",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

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

    def test_public_reconciliation_types_keep_their_module_identity(self):
        self.assertEqual(
            AuthoringReconciliation.__module__, "vntts.authoring.reconciliation"
        )
        self.assertEqual(
            AuthoringReconciliationError.__module__, "vntts.authoring.reconciliation"
        )


if __name__ == "__main__":
    unittest.main()
