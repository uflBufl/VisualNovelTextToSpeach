import unittest
from collections.abc import Callable
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import patch

from tests.authoring_fixtures import create_failed_reference_workspace
from tests.missing_voice_reuse_fixtures import (
    build_missing_voice_reuse_plan_fixture,
    create_missing_voice_live_fallback_fixture,
    create_missing_voice_reuse_binding_review,
    create_missing_voice_reuse_workspace,
)
from tests.source_reference_fixtures import write_experimental_composite_voice_fixture
from vntts.authoring import (
    experimental_composite_voice,
    failure_reference_audit,
    known_role_reuse,
    missing_voice_reuse,
    missing_voice_reuse_binding,
)
from vntts.authoring.publication import AtomicPublicationError


class ReferencePublicationBoundariesTest(unittest.TestCase):
    def assert_racing_output(
        self,
        module: ModuleType,
        publish: Callable[[], object],
        parent: Path,
        error_type: type[Exception],
    ) -> None:
        original_paths = set(parent.iterdir())
        destinations: list[Path] = []
        rename = module.rename_directory_no_replace

        def race(staging: Path, destination: Path) -> None:
            destinations.append(destination)
            destination.mkdir()
            (destination / "sentinel").write_bytes(b"competitor output")
            rename(staging, destination)

        with (
            patch.object(module, "rename_directory_no_replace", side_effect=race),
            self.assertRaisesRegex(
                error_type, "Publication destination already exists"
            ) as caught,
        ):
            publish()
        self.assertIsInstance(caught.exception.__cause__, AtomicPublicationError)
        self.assertEqual(len(destinations), 1)
        output = destinations[0]
        self.assertEqual(set(output.iterdir()), {output / "sentinel"})
        self.assertEqual((output / "sentinel").read_bytes(), b"competitor output")
        self.assertEqual(set(parent.iterdir()), original_paths | {output})

    def test_failure_reference_audit_preserves_racing_output_and_cleanup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace, _queue_id = create_failed_reference_workspace(root)
            self.assert_racing_output(
                failure_reference_audit,
                partial(
                    failure_reference_audit.publish_failure_reference_audit,
                    workspace,
                    root / "audit",
                ),
                root,
                failure_reference_audit.FailureReferenceAuditError,
            )

    def test_experimental_composite_voice_preserves_racing_output_and_cleanup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest, composite, review_path, _review = (
                write_experimental_composite_voice_fixture(root)
            )
            self.assert_racing_output(
                experimental_composite_voice,
                partial(
                    experimental_composite_voice.publish_experimental_composite_voice_input,
                    manifest,
                    composite,
                    review_path,
                    "Experimental Hotelier exact-bank composite",
                    root / "experimental",
                ),
                root,
                experimental_composite_voice.ExperimentalCompositeVoiceError,
            )

    def test_known_role_reuse_preserves_racing_output_and_cleanup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace, unresolved, _queue_id = (
                create_missing_voice_live_fallback_fixture(root)
            )
            self.assert_racing_output(
                known_role_reuse,
                partial(
                    known_role_reuse.publish_known_role_reuse_binding,
                    workspace,
                    unresolved,
                    "Aderyn",
                    "Rhiannon",
                    root / "known-role",
                    accept_known_role_reuse=True,
                ),
                root,
                known_role_reuse.KnownRoleReuseError,
            )

    def test_missing_voice_candidate_preserves_racing_output_and_cleanup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _fixture, imported, workspace = create_missing_voice_reuse_workspace(root)
            plan = build_missing_voice_reuse_plan_fixture(workspace)
            input_root = root / "candidate-inputs"
            input_root.mkdir()
            workspaces_root = root / "candidate-workspaces"
            self.assert_racing_output(
                missing_voice_reuse,
                partial(
                    missing_voice_reuse.prepare_missing_voice_reuse_candidate_workspace,
                    plan,
                    plan.document["candidates"][0]["candidate_id"],
                    imported,
                    input_root,
                    workspaces_root,
                ),
                input_root,
                missing_voice_reuse.MissingVoiceReuseError,
            )
            self.assertFalse(workspaces_root.exists())

    def test_missing_voice_binding_preserves_racing_output_and_cleanup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan_path, session_path, _queue_id = (
                create_missing_voice_reuse_binding_review(
                    root, statuses=("failed", "failed")
                )
            )
            self.assert_racing_output(
                missing_voice_reuse_binding,
                partial(
                    missing_voice_reuse_binding.publish_missing_voice_reuse_binding,
                    plan_path,
                    session_path,
                    root / "binding",
                ),
                root,
                missing_voice_reuse_binding.MissingVoiceReuseBindingError,
            )


if __name__ == "__main__":
    unittest.main()
