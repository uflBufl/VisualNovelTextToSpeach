import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.authoring.workbench as workbench_module
import vntts.authoring.workspace_creation as workspace_creation_module
from tests.authoring_fixtures import create_failed_reference_workspace, tree_hashes
from tests.bulk_generation_fixtures import SyntheticRenderer
from tests.symlink_support import symlink_or_skip
from vntts.authoring import (
    failure_reference_audit as failure_audit_module,
)
from vntts.authoring import (
    failure_reference_binding as binding_module,
)
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.cohort_review import (
    apply_cohort_review_decision,
    build_cohort_review_decision,
    build_cohort_review_plan,
)
from vntts.authoring.config_rebase import rebase_workspace_config
from vntts.authoring.failure_reference_audit import (
    publish_failure_reference_audit,
    record_failure_reference_decision,
)
from vntts.authoring.failure_reference_binding import (
    FailureReferenceBindingError,
    load_failure_reference_binding,
    load_failure_reference_binding_document,
    publish_failure_reference_binding,
)
from vntts.authoring.failure_repair import FailureRepairPolicy
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    create_failure_reference_workspace,
    create_resume_workspace,
    failure_reference_runtime_binding,
    generation_command,
    generation_control_bindings,
    inspect_generation_readiness,
    inspect_workspace,
    review_workspace_item,
)


class FailureReferenceBindingTest(unittest.TestCase):
    def test_final_source_recheck_translates_io_error_and_cleans_up(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, *_rest = self.create_decided_audit(root)
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            before = {
                path: digest
                for path, digest in tree_hashes(root).items()
                if not path.endswith(".guard")
            }
            verifier = (
                workspace_creation_module._assert_failure_reference_sources_unchanged
            )
            failure = PermissionError("final source is unreadable")

            def fail_final_hash(*args, **kwargs):
                with patch.object(
                    workspace_creation_module, "sha256_file", side_effect=failure
                ):
                    return verifier(*args, **kwargs)

            with (
                patch.object(
                    workspace_creation_module,
                    "_assert_failure_reference_sources_unchanged",
                    side_effect=fail_final_hash,
                ),
                self.assertRaisesRegex(
                    AuthoringWorkbenchError, "final source is unreadable"
                ) as caught,
            ):
                create_failure_reference_workspace(
                    workspace, binding, root / "successors"
                )

            self.assertIs(caught.exception.__cause__, failure)
            self.assertEqual(
                {
                    path: digest
                    for path, digest in tree_hashes(root).items()
                    if not path.endswith(".guard")
                },
                before,
            )
            self.assertEqual(list((root / "successors").iterdir()), [])
            self.assertFalse(list(root.rglob(".generation-lease.json")))

    def create_decided_audit(self, root):
        workspace, queue_id = create_failed_reference_workspace(root)
        state_path = workspace / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text())
        state["active"] = None
        state_path.write_text(json.dumps(state, sort_keys=True))
        audit = root / "audit"
        publish_failure_reference_audit(workspace, audit, seed=7)
        document = json.loads((audit / "audit.json").read_text())
        group = document["groups"][0]
        candidate = group["candidates"][0]
        decisions = record_failure_reference_decision(
            audit, group["group_id"], candidate["candidate_id"]
        )
        return audit, workspace, queue_id, group, candidate, decisions

    def test_publish_is_self_contained_exact_and_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, _workspace, queue_id, group, candidate, decisions = (
                self.create_decided_audit(root)
            )
            output = root / "binding"
            source_before = {
                path.relative_to(audit).as_posix(): path.read_bytes()
                for path in audit.rglob("*")
                if path.is_file()
            }

            created = publish_failure_reference_binding(audit, output)
            repeated = publish_failure_reference_binding(audit, output)
            loaded = load_failure_reference_binding(output)
            document = load_failure_reference_binding_document(output)

            self.assertTrue(created.created)
            self.assertFalse(repeated.created)
            self.assertEqual(created.binding_id, repeated.binding_id)
            self.assertEqual(created.binding_id, loaded.binding_id)
            self.assertEqual(created.decision_set_id, decisions["decision_set_id"])
            self.assertEqual(created.case_count, 1)
            selected = document["groups"][0]
            self.assertEqual(selected["candidate_id"], candidate["candidate_id"])
            self.assertEqual(selected["reference_sha256"], candidate["sha256"])
            self.assertEqual(
                document["queue_voice_overrides"][queue_id],
                selected["voice_character"],
            )
            self.assertEqual(
                (output / selected["reference"]).read_bytes(),
                (audit / candidate["audio"]).read_bytes(),
            )
            self.assertEqual(
                source_before,
                {
                    path.relative_to(audit).as_posix(): path.read_bytes()
                    for path in audit.rglob("*")
                    if path.is_file()
                },
            )
            self.assertEqual(selected["cases"][0]["queue_id"], queue_id)
            self.assertEqual(selected["group_id"], group["group_id"])

    def test_binding_loaders_normalize_encoding_and_canonical_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, *_rest = self.create_decided_audit(root)
            output = root / "binding"
            publish_failure_reference_binding(audit, output)
            path = output / "binding.json"
            original = path.read_bytes()
            payloads = [b"\xff"]
            for value in (float("nan"), float("inf"), float("-inf"), "\ud800"):
                document = json.loads(original)
                document["authority"] = value
                payloads.append(json.dumps(document).encode())
            for payload in payloads:
                for loader in (
                    load_failure_reference_binding,
                    load_failure_reference_binding_document,
                ):
                    with self.subTest(payload=payload, loader=loader.__name__):
                        path.write_bytes(payload)
                        with self.assertRaises(FailureReferenceBindingError):
                            loader(output)
            path.write_bytes(original)
            self.assertEqual(
                load_failure_reference_binding_document(output)["binding_id"],
                json.loads(original)["binding_id"],
            )

    def test_document_loader_validates_captured_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, *_rest = self.create_decided_audit(root)
            output = root / "binding"
            publish_failure_reference_binding(audit, output)
            path = output / "binding.json"
            original = path.read_bytes()
            altered = {
                **json.loads(original),
                "authority": "changed without a new identity",
            }
            original_open = open
            for value in (altered, [], None):
                changed = json.dumps(value).encode()
                reads = 0

                @contextmanager
                def capture_changed_binding(candidate, *args, **kwargs):
                    nonlocal reads
                    candidate = Path(candidate)
                    if candidate.resolve() != path.resolve():
                        with original_open(candidate, *args, **kwargs) as stream:
                            yield stream
                        return
                    reads += 1
                    path.write_bytes(changed)
                    try:
                        with original_open(candidate, *args, **kwargs) as stream:
                            yield stream
                    finally:
                        if reads == 1:
                            path.write_bytes(original)

                try:
                    with (
                        self.subTest(value=value),
                        patch(
                            "vntts.path_safety.open",
                            side_effect=capture_changed_binding,
                            create=True,
                        ),
                    ):
                        with self.assertRaises(FailureReferenceBindingError):
                            load_failure_reference_binding_document(output)
                finally:
                    path.write_bytes(original)

    def test_idempotent_publish_rechecks_audit_inputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, _workspace, _queue_id, _group, candidate, _decisions = (
                self.create_decided_audit(root)
            )
            output = root / "binding"
            publish_failure_reference_binding(audit, output)
            load_existing = binding_module.load_failure_reference_binding
            for source in (
                audit / "audit.json",
                audit / ".blind-key.json",
                audit / "decisions.json",
                audit / candidate["audio"],
            ):
                original = source.read_bytes()

                def replace_source_after_existing(path):
                    result = load_existing(path)
                    source.write_bytes(original + b"\n")
                    return result

                try:
                    with (
                        self.subTest(source=source.name),
                        patch.object(
                            binding_module,
                            "load_failure_reference_binding",
                            side_effect=replace_source_after_existing,
                        ),
                    ):
                        with self.assertRaisesRegex(
                            FailureReferenceBindingError, "changed"
                        ):
                            publish_failure_reference_binding(audit, output)
                finally:
                    source.write_bytes(original)
            self.assertFalse(publish_failure_reference_binding(audit, output).created)

    def test_new_publish_rechecks_after_final_decision_load(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, *_rest = self.create_decided_audit(root)
            load_decisions = failure_audit_module.load_failure_reference_decisions
            for name in ("audit.json", ".blind-key.json", "decisions.json"):
                path = audit / name
                original = path.read_bytes()
                reads = 0
                output = root / ("binding-" + name)

                def replace_source_after_decisions(*args, **kwargs):
                    nonlocal reads
                    result = load_decisions(*args, **kwargs)
                    reads += 1
                    if reads == 2:
                        path.write_bytes(original + b"\n")
                    return result

                try:
                    with (
                        self.subTest(source=name),
                        patch.object(
                            failure_audit_module,
                            "load_failure_reference_decisions",
                            side_effect=replace_source_after_decisions,
                        ),
                    ):
                        with self.assertRaisesRegex(
                            FailureReferenceBindingError, "changed"
                        ):
                            publish_failure_reference_binding(audit, output)
                        self.assertFalse(output.exists())
                        self.assertEqual(
                            list(root.glob(f".{output.name}.staging-*")), []
                        )
                finally:
                    path.write_bytes(original)

    def test_captured_group_ids_are_validated_before_indexing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, *_rest = self.create_decided_audit(root)
            paths = [
                audit / name
                for name in ("audit.json", ".blind-key.json", "decisions.json")
            ]
            originals = [path.read_bytes() for path in paths]
            document, key, decisions = [json.loads(payload) for payload in originals]
            document["groups"][0]["group_id"] = []
            key["groups"][0]["group_id"] = []
            decisions["decisions"][0]["group_id"] = []
            document["blind_key_groups_sha256"] = canonical_document_sha256(
                key["groups"]
            )
            document["audit_id"] = canonical_document_sha256(
                {name: value for name, value in document.items() if name != "audit_id"}
            )
            key["audit_id"] = decisions["audit_id"] = document["audit_id"]
            decisions["decision_set_id"] = canonical_document_sha256(
                {
                    name: value
                    for name, value in decisions.items()
                    if name != "decision_set_id"
                }
            )
            captured = [
                json.dumps(value).encode() for value in (document, key, decisions)
            ]
            load_decisions = failure_audit_module.load_failure_reference_decisions

            def replace_after_initial_validation(*args, **kwargs):
                result = load_decisions(*args, **kwargs)
                for path, payload in zip(paths, captured, strict=True):
                    path.write_bytes(payload)
                return result

            try:
                with patch.object(
                    failure_audit_module,
                    "load_failure_reference_decisions",
                    side_effect=replace_after_initial_validation,
                ):
                    with self.assertRaisesRegex(
                        FailureReferenceBindingError, "group ID"
                    ):
                        publish_failure_reference_binding(audit, root / "binding")
                self.assertFalse((root / "binding").exists())
            finally:
                for path, payload in zip(paths, originals, strict=True):
                    path.write_bytes(payload)

    def test_publication_race_preserves_competing_output_and_domain_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, *_rest = self.create_decided_audit(root)
            output = root / "binding"
            rename = binding_module.rename_directory_no_replace

            def create_competing_output(staging, destination):
                destination.mkdir()
                (destination / "sentinel").write_bytes(b"competing output")
                rename(staging, destination)

            with patch.object(
                binding_module,
                "rename_directory_no_replace",
                side_effect=create_competing_output,
            ):
                with self.assertRaisesRegex(FailureReferenceBindingError, "exists"):
                    publish_failure_reference_binding(audit, output)
            self.assertEqual((output / "sentinel").read_bytes(), b"competing output")
            self.assertEqual(list(root.glob(".binding.staging-*")), [])

    def test_binding_schema_version_requires_exact_integer(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, _workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            output = root / "binding"
            publish_failure_reference_binding(audit, output)
            binding_path = output / "binding.json"
            document = json.loads(binding_path.read_text())
            legacy = {
                **document,
                "schema_version": 1,
                "groups": [
                    {
                        field: value
                        for field, value in group.items()
                        if field != "selection_authority"
                    }
                    for group in document["groups"]
                ],
            }
            legacy["binding_id"] = canonical_document_sha256(
                {
                    field: value
                    for field, value in legacy.items()
                    if field not in {"binding_id", "published_at"}
                }
            )
            binding_path.write_text(json.dumps(legacy), encoding="utf-8")
            self.assertEqual(
                load_failure_reference_binding_document(output)["schema_version"], 1
            )
            for version in (True, 1.0, [], {}):
                malformed = dict(document)
                malformed["schema_version"] = version
                binding_path.write_text(json.dumps(malformed), encoding="utf-8")
                with (
                    self.subTest(version=version),
                    self.assertRaisesRegex(
                        FailureReferenceBindingError,
                        "Unsupported reference binding schema",
                    ),
                ):
                    load_failure_reference_binding_document(output)

    def test_binding_loaders_reject_non_object_root_documents(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            binding_path = root / "binding.json"
            for value in ([], None, True, 1, "text"):
                payload = json.dumps(value)
                binding_path.write_text(payload, encoding="utf-8")
                with self.subTest(value=value):
                    for loader in (
                        load_failure_reference_binding,
                        load_failure_reference_binding_document,
                    ):
                        with self.assertRaisesRegex(
                            FailureReferenceBindingError,
                            "Reference binding document must be an object",
                        ):
                            loader(root)
                self.assertEqual(binding_path.read_text(encoding="utf-8"), payload)

    def test_publisher_rejects_malformed_audit_schema_versions(self):
        for filename, field, current in (
            ("audit.json", "schema_version", 2),
            (".blind-key.json", "schema_version", 2),
            ("decisions.json", "schema_version", 4),
        ):
            for version in (float(current), [], {}):
                with (
                    self.subTest(filename=filename, version=version),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    audit, *_ = self.create_decided_audit(root)
                    path = audit / filename
                    document = json.loads(path.read_text())
                    document[field] = version
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaisesRegex(
                        FailureReferenceBindingError,
                        (
                            "Reference audit decisions are malformed"
                            if filename == "decisions.json"
                            else "Unsupported reference audit schema"
                        ),
                    ):
                        publish_failure_reference_binding(audit, root / "binding")

    def test_incomplete_and_neither_decisions_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _queue_id = create_failed_reference_workspace(root)
            audit = root / "audit"
            publish_failure_reference_audit(workspace, audit)
            with self.assertRaisesRegex(
                FailureReferenceBindingError, "terminal decision"
            ):
                publish_failure_reference_binding(audit, root / "missing")

            document = json.loads((audit / "audit.json").read_text())
            record_failure_reference_decision(
                audit, document["groups"][0]["group_id"], "neither_acceptable"
            )
            with self.assertRaisesRegex(FailureReferenceBindingError, "rejected group"):
                publish_failure_reference_binding(audit, root / "rejected")

    def test_tampered_selected_reference_and_binding_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, _workspace, _queue_id, _group, candidate, _decisions = (
                self.create_decided_audit(root)
            )
            output = root / "binding"
            publish_failure_reference_binding(audit, output)
            selected = load_failure_reference_binding_document(output)["groups"][0]
            (output / selected["reference"]).write_bytes(b"changed")
            with self.assertRaisesRegex(FailureReferenceBindingError, "changed"):
                load_failure_reference_binding(output)

            (audit / candidate["audio"]).write_bytes(b"changed")
            with self.assertRaisesRegex(
                FailureReferenceBindingError,
                "audio changed|Selected reference authority",
            ):
                publish_failure_reference_binding(audit, root / "tampered-audit")

    def test_binding_rejects_symlinked_selected_reference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, _workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            output = root / "binding"
            publish_failure_reference_binding(audit, output)
            selected = load_failure_reference_binding_document(output)["groups"][0]
            reference = output / selected["reference"]
            payload = reference.read_bytes()
            replacement = root / "same-bytes.wav"
            replacement.write_bytes(payload)
            reference.unlink()
            symlink_or_skip(reference, replacement)

            with self.assertRaisesRegex(FailureReferenceBindingError, "unsafe"):
                load_failure_reference_binding(output)

    def test_successor_preserves_state_and_adds_exact_runtime_controls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            state_path = workspace / "generated-audio/generation-state.json"
            state_before = state_path.read_bytes()
            audio_before = {
                path.relative_to(
                    workspace / "generated-audio"
                ).as_posix(): path.read_bytes()
                for path in (workspace / "generated-audio").rglob("*")
                if path.is_file()
            }

            created = create_failure_reference_workspace(
                workspace,
                binding,
                root / "successors",
            )
            repeated = create_failure_reference_workspace(
                workspace,
                binding,
                root / "successors",
            )
            summary = inspect_workspace(created.directory)
            runtime = failure_reference_runtime_binding(created.directory)
            readiness = inspect_generation_readiness(
                created.directory,
                queue_ids=(queue_id,),
            )

            self.assertTrue(created.created)
            self.assertFalse(repeated.created)
            self.assertEqual(created.directory, repeated.directory)
            self.assertEqual(summary.failed, 1)
            self.assertEqual(readiness.queue_ids, (queue_id,))
            self.assertEqual(readiness.ready, 1)
            self.assertEqual(readiness.missing_voice, 0)
            self.assertIsNotNone(runtime)
            self.assertEqual(set(runtime.queue_voice_overrides), {queue_id})
            self.assertEqual(len(runtime.voices), 1)
            self.assertEqual(
                (
                    created.directory / "generated-audio/generation-state.json"
                ).read_bytes(),
                state_before,
            )
            self.assertEqual(
                {
                    path.relative_to(
                        created.directory / "generated-audio"
                    ).as_posix(): path.read_bytes()
                    for path in (created.directory / "generated-audio").rglob("*")
                    if path.is_file()
                },
                audio_before,
            )

    def test_successor_fingerprint_preserves_existing_config_rebase(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            workspace_document = json.loads((workspace / "workspace.json").read_text())
            config_rebase = {"test_only_authority": "preserved"}
            fingerprint = workbench_module._workspace_config_fingerprint(
                workspace_document["source"]["import_id"],
                workspace_document.get("story_index"),
                workspace_document.get("voice_manifest"),
                workspace_document["narrator_character"],
                workspace_document["run_config"],
                workspace_document.get("carry_forward"),
                workspace_document.get("outcome_merge"),
                workspace_document.get("failure_reference_binding"),
                workspace_document.get("terminal_conflict_merge"),
                config_rebase,
            )
            workspace_id = (
                "resume-"
                + workspace_document["source"]["import_id"].removeprefix("legacy-")
                + "-"
                + fingerprint[:16]
            )
            workspace_document.update(
                {
                    "workspace_id": workspace_id,
                    "config_fingerprint": fingerprint,
                    "config_rebase": config_rebase,
                }
            )
            (workspace / "workspace.json").write_text(
                json.dumps(workspace_document, sort_keys=True)
            )
            rebased_workspace = workspace.with_name(workspace_id)
            workspace.rename(rebased_workspace)

            with patch(
                "vntts.authoring.config_rebase.validate_config_rebase_workspace"
            ):
                successor = create_failure_reference_workspace(
                    rebased_workspace,
                    binding,
                    root / "successors",
                )
                summary = inspect_workspace(successor.directory)

            created_document = json.loads(
                (successor.directory / "workspace.json").read_text()
            )
            self.assertEqual(created_document["config_rebase"], config_rebase)
            self.assertEqual(summary.failed, 1)

    def test_successor_rejects_stale_base_and_tampered_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text())
            next(iter(state["items"].values()))["attempts"] += 1
            state_path.write_text(json.dumps(state, sort_keys=True))
            with self.assertRaisesRegex(AuthoringWorkbenchError, "authority is stale"):
                create_failure_reference_workspace(
                    workspace,
                    binding,
                    root / "stale-successors",
                )

            audit, workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root / "second")
            )
            binding = root / "second-binding"
            publish_failure_reference_binding(audit, binding)
            created = create_failure_reference_workspace(
                workspace,
                binding,
                root / "tamper-successors",
            )
            runtime = failure_reference_runtime_binding(created.directory)
            next(
                path for path in runtime.controls if path.name != "binding.json"
            ).write_bytes(b"changed")
            with self.assertRaisesRegex(AuthoringWorkbenchError, "modified|changed"):
                inspect_workspace(created.directory)

    def test_successor_rejects_base_mutation_during_snapshot_copy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            state_path = workspace / "generated-audio/generation-state.json"
            original_copy = workspace_creation_module._copy_workspace_tree_snapshot

            def copy_then_mutate(source, target, snapshots):
                original_copy(source, target, snapshots)
                if Path(source).resolve() == (workspace / "generated-audio").resolve():
                    document = json.loads(state_path.read_text())
                    document["active"] = {"phase": "changed-during-copy"}
                    state_path.write_text(json.dumps(document, sort_keys=True))

            with patch.object(
                workspace_creation_module,
                "_copy_workspace_tree_snapshot",
                copy_then_mutate,
            ):
                with self.assertRaisesRegex(
                    AuthoringWorkbenchError,
                    "source changed before workspace publication",
                ):
                    create_failure_reference_workspace(
                        workspace,
                        binding,
                        root / "successors",
                    )
            self.assertFalse(any((root / "successors").glob("resume-*")))

    def test_successor_holds_base_generation_lease_while_copying(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            original_copy = workspace_creation_module._copy_workspace_tree_snapshot

            def copy_while_locked(source, target, snapshots):
                original_copy(source, target, snapshots)
                if Path(source).resolve() == (workspace / "generated-audio").resolve():
                    with self.assertRaisesRegex(
                        workspace_creation_module.BulkGenerationError, "generation"
                    ):
                        with workspace_creation_module.GenerationLease(
                            workspace / "generated-audio",
                            workspace_creation_module.sha256_file(
                                workspace / "queue.jsonl"
                            ),
                            process_checker=workspace_creation_module.process_is_alive,
                        ):
                            pass

            with patch.object(
                workspace_creation_module,
                "_copy_workspace_tree_snapshot",
                copy_while_locked,
            ):
                created = create_failure_reference_workspace(
                    workspace, binding, root / "successors"
                )

        self.assertTrue(created.created)

    def test_successor_rejects_binding_mutation_during_snapshot_copy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            binding_document = load_failure_reference_binding_document(binding)
            reference = binding / binding_document["groups"][0]["reference"]
            original_copy = workspace_creation_module._copy_workspace_tree_snapshot

            def copy_then_mutate(source, target, snapshots):
                original_copy(source, target, snapshots)
                if Path(source).resolve() == binding.resolve():
                    reference.write_bytes(b"changed-during-copy")

            with patch.object(
                workspace_creation_module,
                "_copy_workspace_tree_snapshot",
                copy_then_mutate,
            ):
                with self.assertRaisesRegex(
                    AuthoringWorkbenchError,
                    "source changed before workspace publication",
                ):
                    create_failure_reference_workspace(
                        workspace,
                        binding,
                        root / "successors",
                    )
            self.assertFalse(any((root / "successors").glob("resume-*")))

    def test_child_uses_only_the_bound_synthetic_voice_and_controls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            created = create_failure_reference_workspace(
                workspace,
                binding,
                root / "successors",
            )
            command = generation_command(
                created.directory,
                queue_ids=(queue_id,),
                retries=0,
                seed=0,
            )
            renderers = []

            def create_backend(_name, registry, *_args, **options):
                renderer = SyntheticRenderer()
                renderer.name = "moss-tts"
                renderer.model_name = options["model_name"]
                renderer.registry = registry
                renderers.append(renderer)
                return renderer

            with patch("vntts.authoring.cli_generation.create_backend", create_backend):
                self.assertEqual(authoring_main(command[3:]), 0)

            runtime = failure_reference_runtime_binding(created.directory)
            state = json.loads(
                (
                    created.directory / "generated-audio/generation-state.json"
                ).read_text()
            )
            result = state["items"][queue_id]
            self.assertEqual(len(renderers), 1)
            self.assertEqual(len(renderers[0].requests), 1)
            self.assertEqual(
                renderers[0].requests[0].voice,
                runtime.queue_voice_overrides[queue_id],
            )
            self.assertEqual(result["voice_character"], renderers[0].requests[0].voice)
            bindings = generation_control_bindings(
                created.directory,
                queue=created.directory / "queue.jsonl",
                output=created.directory / "generated-audio",
                voice_manifest=created.directory / "inputs/voice/manifest.json",
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character=json.loads(
                    (created.directory / "workspace.json").read_text()
                )["narrator_character"],
            )
            self.assertIn(runtime.directory / "binding.json", bindings)
            self.assertEqual(
                result["source_reference_binding"]["synthesis_voice_character"],
                renderers[0].requests[0].voice,
            )

            plan = build_cohort_review_plan(
                created.directory,
                queue_ids=(queue_id,),
            )
            cohort = plan.document["cohorts"][0]
            decision = build_cohort_review_decision(
                plan,
                cohort["cohort_id"],
                "accepted",
                reviewed_queue_ids=[queue_id],
            )
            projection = apply_cohort_review_decision(
                created.directory,
                plan,
                decision,
            )
            reviewed_state = json.loads(
                (
                    created.directory / "generated-audio/generation-state.json"
                ).read_text()
            )

            self.assertEqual(projection.review_status, "approved")
            self.assertEqual(reviewed_state["items"][queue_id]["status"], "approved")

    def test_approved_failure_reference_rebases_through_its_source_voice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, base_workspace, queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            source = create_failure_reference_workspace(
                base_workspace,
                binding,
                root / "successors",
            ).directory

            def create_backend(_name, registry, *_args, **options):
                renderer = SyntheticRenderer()
                renderer.name = "moss-tts"
                renderer.model_name = options["model_name"]
                renderer.registry = registry
                return renderer

            command = generation_command(
                source,
                queue_ids=(queue_id,),
                retries=0,
                seed=0,
            )
            with patch("vntts.authoring.cli_generation.create_backend", create_backend):
                self.assertEqual(authoring_main(command[3:]), 0)
            plan = build_cohort_review_plan(source, queue_ids=(queue_id,))
            decision = build_cohort_review_decision(
                plan,
                plan.document["cohorts"][0]["cohort_id"],
                "accepted",
                reviewed_queue_ids=[queue_id],
            )
            apply_cohort_review_decision(source, plan, decision)

            base_document = json.loads((base_workspace / "workspace.json").read_text())
            imported = next((root / "imports").glob("legacy-*"))
            target = create_resume_workspace(
                imported,
                root / "target-workspaces",
                story_index=base_workspace / base_document["story_index"]["path"],
                voice_manifest=(
                    base_workspace / base_document["voice_manifest"]["path"]
                ),
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Rhiannon",
            ).directory
            target_state_path = target / "generated-audio/generation-state.json"
            target_state = json.loads(target_state_path.read_text())
            target_state["active"] = None
            target_state_path.write_text(json.dumps(target_state, sort_keys=True))
            rebased = rebase_workspace_config(
                source,
                target,
                root / "rebased-workspaces",
            )
            repeated = rebase_workspace_config(
                source,
                target,
                root / "rebased-workspaces",
            )
            state = json.loads(
                (
                    rebased.directory / "generated-audio/generation-state.json"
                ).read_text()
            )
            result = state["items"][queue_id]
            authority = result["config_rebase"]
            runtime = failure_reference_runtime_binding(source)
            selected_sha256 = runtime.document["groups"][0]["reference_sha256"]

            self.assertTrue(rebased.created)
            self.assertFalse(repeated.created)
            self.assertEqual(repeated.directory, rebased.directory)
            self.assertEqual(result["status"], "approved")
            self.assertEqual(result["review_status"], "approved")
            self.assertEqual(
                authority["source_effective_character"],
                runtime.queue_voice_overrides[queue_id],
            )
            self.assertEqual(authority["target_effective_character"], "Rhiannon")
            self.assertIn(selected_sha256, authority["source_reference_sha256s"])
            self.assertTrue(
                set(authority["source_reference_sha256s"]).issubset(
                    authority["target_reference_sha256s"]
                )
            )
            self.assertIsNotNone(inspect_workspace(rebased.directory))

    def test_same_backend_repair_successor_preserves_the_exact_overlay(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            successor = create_failure_reference_workspace(
                workspace,
                binding,
                root / "successors",
            ).directory
            runtime = failure_reference_runtime_binding(successor)
            state_path = successor / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text())
            failed = state["items"][queue_id]
            text_features = failed["failure"]["text_features"]
            failed.update(
                {
                    "attempts": 2,
                    "seed": 1,
                    "last_error": (
                        "MOSS generation hit the text-length audio limit before EOS"
                    ),
                    "provider": "moss-tts",
                    "model": "model with spaces",
                    "generation_profile": "stable",
                    "voice_character": runtime.queue_voice_overrides[queue_id],
                    "failure": {
                        "schema_version": 1,
                        "kind": "missed_eos_audio_limit",
                        "error_type": "SynthesisLimitedError",
                        "text_features": text_features,
                        "completion": "limited",
                    },
                }
            )
            state_path.write_text(json.dumps(state, sort_keys=True))
            imported = next((root / "imports").glob("legacy-*"))
            imported_state_path = imported / "generated-audio/generation-state.json"
            imported_state = json.loads(imported_state_path.read_text())
            imported_state["active"] = None
            imported_state_path.write_text(json.dumps(imported_state, sort_keys=True))
            import_path = imported / "import.json"
            import_document = json.loads(import_path.read_text())
            imported_state_sha256 = workbench_module.sha256_file(imported_state_path)
            next(
                item
                for item in import_document["artifacts"]
                if item["path"] == "generated-audio/generation-state.json"
            )["sha256"] = imported_state_sha256
            import_path.write_text(json.dumps(import_document, sort_keys=True))

            repair = create_resume_workspace(
                imported,
                root / "repairs",
                story_index=successor / "inputs/story-index.jsonl",
                voice_manifest=successor / "inputs/voice/manifest.json",
                narrator_character="Rhiannon",
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                failure_repair_policy=FailureRepairPolicy(
                    bounded_seed_retry_queue_ids=(queue_id,)
                ),
                carry_forward_from=successor,
            )
            repeated = create_resume_workspace(
                imported,
                root / "repairs",
                story_index=successor / "inputs/story-index.jsonl",
                voice_manifest=successor / "inputs/voice/manifest.json",
                narrator_character="Rhiannon",
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                failure_repair_policy=FailureRepairPolicy(
                    bounded_seed_retry_queue_ids=(queue_id,)
                ),
                carry_forward_from=successor,
            )

            repair_runtime = failure_reference_runtime_binding(repair.directory)
            repair_state = json.loads(
                (repair.directory / "generated-audio/generation-state.json").read_text()
            )
            readiness = inspect_generation_readiness(
                repair.directory,
                queue_ids=(queue_id,),
            )
            self.assertTrue(repair.created)
            self.assertFalse(repeated.created)
            self.assertEqual(repair.directory, repeated.directory)
            self.assertEqual(repair_runtime.document, runtime.document)
            self.assertEqual(
                repair_state["items"][queue_id]["voice_character"],
                runtime.queue_voice_overrides[queue_id],
            )
            self.assertEqual(readiness.selected, 1)
            self.assertEqual(readiness.ready, 1)
            self.assertEqual(readiness.missing_voice, 0)
            inspect_workspace(repair.directory)

            def create_backend(_name, registry, *_args, **options):
                renderer = SyntheticRenderer()
                renderer.name = "moss-tts"
                renderer.model_name = options["model_name"]
                renderer.registry = registry
                return renderer

            repair_command = generation_command(
                repair.directory,
                queue_ids=(queue_id,),
                retries=0,
                seed=0,
            )
            with patch("vntts.authoring.cli_generation.create_backend", create_backend):
                self.assertEqual(authoring_main(repair_command[3:]), 0)
            review_workspace_item(repair.directory, queue_id, "approved")
            reviewed_state = json.loads(
                (repair.directory / "generated-audio/generation-state.json").read_text()
            )
            self.assertEqual(reviewed_state["items"][queue_id]["status"], "approved")
            inspect_workspace(repair.directory)

            state = json.loads(state_path.read_text())
            state["items"][queue_id]["attempts"] = 3
            state["items"][queue_id]["seed"] = 2
            state_path.write_text(json.dumps(state, sort_keys=True))
            fallback = create_resume_workspace(
                imported,
                root / "fallbacks",
                story_index=successor / "inputs/story-index.jsonl",
                voice_manifest=successor / "inputs/voice/manifest.json",
                narrator_character="Rhiannon",
                backend="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                failure_repair_policy=FailureRepairPolicy(
                    offline_fallback_queue_ids=(queue_id,)
                ),
                carry_forward_from=successor,
            )
            fallback_runtime = failure_reference_runtime_binding(fallback.directory)
            fallback_readiness = inspect_generation_readiness(
                fallback.directory,
                queue_ids=(queue_id,),
            )
            self.assertEqual(fallback_runtime.document, runtime.document)
            self.assertEqual(fallback_readiness.selected, 1)
            self.assertEqual(fallback_readiness.ready, 1)
            self.assertEqual(fallback_readiness.missing_voice, 0)
            inspect_workspace(fallback.directory)


if __name__ == "__main__":
    unittest.main()
