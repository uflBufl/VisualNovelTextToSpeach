import builtins
import hashlib
import io
import json
import os
import stat
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.authoring_fixtures import (
    create_carry_source_workspace,
    create_failed_reference_workspace,
    create_specialist_failure_workspace,
    create_test_workspace,
    tree_hashes,
    write_carry_target_manifest,
)
from tests.missing_voice_reuse_fixtures import (
    create_missing_voice_live_fallback_fixture,
)
from tests.symlink_support import symlink_or_skip
from vntts.authoring import failure_reference_audit as audit
from vntts.authoring import known_role_reuse as known_role
from vntts.authoring import specialist_failure_plan as specialist
from vntts.authoring import workspace_creation as creation
from vntts.authoring import workspace_foundation as foundation
from vntts.authoring.cohort_review import CohortReviewError
from vntts.authoring.workbench import review_workspace_item
from vntts.authoring.workbench_contracts import AuthoringWorkbenchError


class WorkspaceInputAdmissionTest(unittest.TestCase):
    @contextmanager
    def swap_at_acquisition(self, gate):
        native_open = os.open
        native_builtin_open, native_io_open = builtins.open, io.open
        descriptors = []
        saved = None
        saved_mode = None

        def selected(path):
            return (
                gate["armed"]
                and isinstance(path, (str, Path))
                and (Path(path) == gate["path"])
            )

        def guarded_open(owner, path, *args, **kwargs):
            if selected(path):
                self.assertIsNotNone(kwargs.get("opener"), "blocking source open")
            return owner(path, *args, **kwargs)

        def swap_and_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal saved, saved_mode
            if selected(path):
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertIsNone(saved, "source must stop after refused acquisition")
                # Save outside the open guard before swapping the admitted candidate.
                gate["armed"] = False
                saved_mode = stat.S_IMODE(Path(path).stat().st_mode)
                saved = Path(path).read_bytes()
                gate["armed"] = True
                Path(path).unlink()
                os.mkfifo(path)
            descriptor = native_open(path, flags, mode, dir_fd=dir_fd)
            if selected(path):
                descriptors.append(descriptor)
            return descriptor

        try:
            with (
                patch("vntts.path_safety.os.open", side_effect=swap_and_open),
                patch(
                    "builtins.open",
                    side_effect=lambda *a, **k: guarded_open(
                        native_builtin_open, *a, **k
                    ),
                ),
                patch(
                    "io.open",
                    side_effect=lambda *a, **k: guarded_open(native_io_open, *a, **k),
                ),
            ):
                yield
            self.assertEqual(len(descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])
        finally:
            gate["armed"] = False
            if saved is not None:
                gate["path"].unlink()
                gate["path"].write_bytes(saved)
                gate["path"].chmod(saved_mode)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_resume_selected_inputs_refuse_acquisition_clean_up_and_recover(self):
        for phase in ("capture", "final"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                fixture, imported, expected = create_test_workspace(root)
                destination = root / "successors"
                gate = {"path": Path(fixture["job"]["story_index"]), "armed": False}
                boundary = (
                    "_read_source_bytes"
                    if phase == "capture"
                    else "_verify_selected_sources"
                )
                original = getattr(creation, boundary)

                def arm(*args, **kwargs):
                    gate["armed"] = True
                    return original(*args, **kwargs)

                def create():
                    return creation.create_resume_workspace(
                        imported,
                        destination,
                        story_index=fixture["job"]["story_index"],
                        voice_manifest=fixture["job"]["voice_manifest"],
                        backend="moss-tts",
                        model="model with spaces",
                        generation_profile="stable",
                        narrator_character="Rhiannon",
                    )

                before = tree_hashes(root)
                with (
                    self.swap_at_acquisition(gate),
                    patch.object(creation, boundary, side_effect=arm),
                    self.assertRaisesRegex(AuthoringWorkbenchError, "regular file"),
                ):
                    create()
                self.assertEqual(tree_hashes(root), before)
                self.assertEqual(list(destination.iterdir()), [])
                self.assertFalse(list(root.rglob(".generation-lease.json")))
                recovered = create()
                self.assertEqual(recovered.directory.name, expected.directory.name)
                self.assertEqual(
                    (recovered.directory / "inputs/story-index.jsonl").read_bytes(),
                    gate["path"].read_bytes(),
                )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_carry_forward_final_authorities_refuse_publication_and_recover(self):
        for authority in ("state", "wav"):
            with self.subTest(authority=authority), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                fixture, imported, source = create_carry_source_workspace(root)
                review_workspace_item(source.directory, fixture["queue_id"], "approved")
                manifest = write_carry_target_manifest(root)
                destination = root / "successors"

                def create(target):
                    return creation.create_resume_workspace(
                        imported,
                        target,
                        story_index=fixture["job"]["story_index"],
                        voice_manifest=manifest,
                        backend="moss-tts",
                        model="model with spaces",
                        generation_profile="stable",
                        narrator_character="Paper Heron",
                        carry_forward_from=source.directory,
                        carry_forward_characters=("Rhiannon",),
                    )

                expected = create(root / "expected")
                source_before = tree_hashes(source.directory)
                gate = {"path": None, "armed": False}
                original = creation._publish_carry_forward_staging

                def arm(target, state, captured, snapshots):
                    snapshots = tuple(snapshots)
                    gate["path"] = (
                        captured.state_path if authority == "state" else snapshots[0][0]
                    )
                    gate["armed"] = True
                    return original(target, state, captured, snapshots)

                before = tree_hashes(root)
                with (
                    self.swap_at_acquisition(gate),
                    patch.object(
                        creation, "_publish_carry_forward_staging", side_effect=arm
                    ),
                    self.assertRaisesRegex(AuthoringWorkbenchError, "regular file"),
                ):
                    create(destination)
                self.assertEqual(tree_hashes(root), before)
                self.assertEqual(list(destination.iterdir()), [])
                self.assertFalse(list(root.rglob(".generation-lease.json")))
                recovered = create(destination)
                self.assertEqual(recovered.directory.name, expected.directory.name)
                self.assertEqual(tree_hashes(source.directory), source_before)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_specialist_capture_recheck_and_final_hash_refuse_and_recover(self):
        for phase in ("capture", "recheck", "final"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                workspace = create_specialist_failure_workspace(
                    root, "sentence_boundary_segmentation", "a"
                )
                expected = specialist.build_specialist_failure_plan((workspace,))
                gate = {"path": workspace / "workspace.json", "armed": False}
                reads = 0
                original_read, original_body = (
                    specialist._read,
                    specialist._build_plan_body,
                )

                def read(path, label):
                    nonlocal reads
                    if path == gate["path"]:
                        reads += 1
                        gate["armed"] = reads == (1 if phase == "capture" else 2)
                    return original_read(path, label)

                def body(*args):
                    result = original_body(*args)
                    gate["armed"] = phase == "final"
                    return result

                before = tree_hashes(root)
                with (
                    self.swap_at_acquisition(gate),
                    patch.object(
                        specialist,
                        "_read",
                        side_effect=read if phase != "final" else original_read,
                    ),
                    patch.object(specialist, "_build_plan_body", side_effect=body),
                    self.assertRaisesRegex(CohortReviewError, "regular file"),
                ):
                    specialist.build_specialist_failure_plan((workspace,))
                self.assertEqual(tree_hashes(root), before)
                recovered = specialist.build_specialist_failure_plan((workspace,))
                self.assertEqual(recovered, expected)

    def test_binary_capture_digest_and_domain_decode_errors_are_preserved(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input"
            payload = b"\xff\x00raw bytes\r\n"
            path.write_bytes(payload)
            self.assertEqual(
                creation._read_source_bytes(path, "story index"),
                (payload, hashlib.sha256(payload).hexdigest()),
            )
            self.assertEqual(
                specialist._read(str(path), "workspace configuration"), payload
            )
            with self.assertRaisesRegex(
                CohortReviewError, "Unable to decode specialist"
            ):
                specialist._decode(payload, "workspace configuration")
            path.unlink()
            for read, error_type, label in (
                (creation._read_source_bytes, AuthoringWorkbenchError, "story index"),
                (specialist._read, CohortReviewError, "workspace configuration"),
            ):
                with (
                    self.subTest(label=label),
                    self.assertRaisesRegex(error_type, label) as caught,
                ):
                    read(path, label)
                self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)

    def test_foundation_text_snapshot_and_error_contracts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "document.json"
            payload = (
                b'{\r\n"text": "caf\xc3\xa9", "unknown": {"nested": true}\r\n}\r\n'
            )
            path.write_bytes(payload)
            expected = json.loads(payload)
            self.assertEqual(foundation.load_json_object(path, "test"), expected)
            self.assertEqual(
                foundation.load_json_object_snapshot(path, "test"),
                (expected, hashlib.sha256(payload).hexdigest(), payload),
            )
            self.assertEqual(foundation.read_regular_file(path, "test"), payload)
            alias = root / "alias.json"
            symlink_or_skip(alias, path)
            self.assertEqual(foundation.load_json_object(alias, "test"), expected)
            self.assertEqual(
                foundation.load_json_object_snapshot(alias, "test")[2], payload
            )
            with self.assertRaisesRegex(ValueError, "Test is missing or unsafe"):
                foundation.read_regular_file(alias, "test")
            for payload, cause in (
                (b"\xff", UnicodeDecodeError),
                (b"\xef\xbb\xbf{}", json.JSONDecodeError),
                (b"{\r\ninvalid", json.JSONDecodeError),
            ):
                path.write_bytes(payload)
                for loader in (
                    foundation.load_json_object,
                    foundation.load_json_object_snapshot,
                ):
                    with (
                        self.subTest(loader=loader.__name__, payload=payload[:12]),
                        self.assertRaisesRegex(
                            AuthoringWorkbenchError, "Unable to read test"
                        ) as caught,
                    ):
                        loader(path, "test", error_type=AuthoringWorkbenchError)
                    self.assertIsInstance(caught.exception.__cause__, cause)
                    if payload == b"{\r\ninvalid":
                        self.assertEqual(
                            caught.exception.__cause__.doc,
                            "{\ninvalid"
                            if loader is foundation.load_json_object
                            else "{\r\ninvalid",
                        )
            path.write_bytes(b"[]")
            with self.assertRaisesRegex(
                ValueError, "Named object must be a JSON object"
            ):
                foundation.load_json_object(path, "test", object_label="Named object")
            with self.assertRaisesRegex(ValueError, "Test must be a JSON object"):
                foundation.load_json_object_snapshot(path, "test")
            path.unlink()
            for loader in (
                foundation.load_json_object,
                foundation.load_json_object_snapshot,
            ):
                with self.assertRaises(ValueError) as caught:
                    loader(path, "test")
                self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
            with self.assertRaisesRegex(
                ValueError, "Test is missing or unsafe"
            ) as caught:
                foundation.read_regular_file(path, "test")
            self.assertIsNone(caught.exception.__cause__)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_foundation_readers_refuse_swapped_acquisition_and_recover(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "input.json"
            payload = b'{\r\n"value": 1\r\n}\r\n'
            for loader in (
                foundation.read_regular_file,
                foundation.load_json_object,
                foundation.load_json_object_snapshot,
            ):
                with self.subTest(loader=loader.__name__):
                    path.write_bytes(payload)
                    expected = loader(path, "test", error_type=AuthoringWorkbenchError)
                    gate = {"path": path, "armed": True}
                    with (
                        self.swap_at_acquisition(gate),
                        self.assertRaisesRegex(
                            AuthoringWorkbenchError, "regular file"
                        ) as caught,
                    ):
                        loader(path, "test", error_type=AuthoringWorkbenchError)
                    self.assertIsInstance(caught.exception.__cause__, OSError)
                    self.assertEqual(
                        loader(path, "test", error_type=AuthoringWorkbenchError),
                        expected,
                    )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_audit_initial_final_and_reference_captures_refuse_and_recover(self):
        for phase, name in (
            *(
                (phase, name)
                for phase in ("capture", "final")
                for name in (
                    "workspace.json",
                    "queue.jsonl",
                    "generated-audio/generation-state.json",
                    "inputs/voice/manifest.json",
                )
            ),
            ("reference", ""),
        ):
            with (
                self.subTest(phase=phase, name=name),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                workspace, _queue_id = create_failed_reference_workspace(root)
                expected = audit.publish_failure_reference_audit(
                    workspace, root / "expected", seed=7
                )
                output = root / "audit"
                gate = {"path": workspace / name, "armed": False}
                boundary = {
                    "capture": "load_workspace_authority",
                    "final": "_assert_audit_publication_sources_unchanged",
                    "reference": "_reference_candidate",
                }[phase]
                original = getattr(audit, boundary)

                def arm(*args, **kwargs):
                    if phase == "capture":
                        result = original(*args, **kwargs)
                        gate["armed"] = True
                        return result
                    if phase == "reference":
                        gate["path"] = (args[0].parent / args[1]).resolve()
                    gate["armed"] = True
                    return original(*args, **kwargs)

                before = tree_hashes(root)
                with (
                    self.swap_at_acquisition(gate),
                    patch.object(audit, boundary, side_effect=arm),
                    self.assertRaisesRegex(
                        audit.FailureReferenceAuditError, "regular file"
                    ),
                ):
                    audit.publish_failure_reference_audit(workspace, output, seed=7)
                self.assertEqual(tree_hashes(root), before)
                self.assertFalse(output.exists())
                self.assertFalse(list(root.glob(".audit.staging-*")))
                recovered = audit.publish_failure_reference_audit(
                    workspace, output, seed=7
                )
                self.assertEqual(recovered.audit_id, expected.audit_id)
                self.assertEqual(audit.load_failure_reference_audit(output), recovered)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_known_role_initial_state_capture_refuses_and_recovers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace, unresolved, _queue_id = (
                create_missing_voice_live_fallback_fixture(root)
            )
            output = root / "known-role"
            gate = {
                "path": workspace / "generated-audio/generation-state.json",
                "armed": False,
            }
            original = known_role.load_stable_generation_queue

            def arm(*args, **kwargs):
                result = original(*args, **kwargs)
                gate["armed"] = True
                return result

            def publish():
                return known_role.publish_known_role_reuse_binding(
                    workspace,
                    unresolved,
                    "Aderyn",
                    "Rhiannon",
                    output,
                    accept_known_role_reuse=True,
                )

            before = tree_hashes(root)
            with (
                self.swap_at_acquisition(gate),
                patch.object(
                    known_role, "load_stable_generation_queue", side_effect=arm
                ),
                self.assertRaisesRegex(known_role.KnownRoleReuseError, "regular file"),
            ):
                publish()
            self.assertEqual(tree_hashes(root), before)
            self.assertFalse(output.exists())
            recovered = publish()
            self.assertTrue(recovered.created)
            repeated = publish()
            self.assertFalse(repeated.created)
            self.assertEqual(recovered.decision_id, repeated.decision_id)

    def test_audit_json_capture_preserves_text_and_raw_error_contracts(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_bytes(b'{\r\n"text": "caf\xc3\xa9", "extra": [true]\r\n}\r\n')
            for encoding in (None, "utf-8"):
                with self.subTest(encoding=encoding):
                    self.assertEqual(
                        audit._read_json(path, encoding=encoding),
                        json.loads(path.read_text(encoding=encoding)),
                    )
            for payload, error_type in (
                (b"\xff", UnicodeDecodeError),
                (b"\xef\xbb\xbf{}", json.JSONDecodeError),
                (b"{\r\ninvalid", json.JSONDecodeError),
            ):
                path.write_bytes(payload)
                with (
                    self.subTest(payload=payload),
                    self.assertRaises(error_type) as caught,
                ):
                    audit._read_json(path, encoding="utf-8")
                if payload == b"{\r\ninvalid":
                    self.assertEqual(caught.exception.doc, "{\ninvalid")
            path.write_bytes(b"[]")
            self.assertEqual(audit._read_json(path), [])
            alias = path.with_name("alias.json")
            symlink_or_skip(alias, path)
            self.assertEqual(audit._read_json(alias), [])
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                audit._read_json(path)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_audit_text_phases_refuse_acquisition_and_recover(self):
        for phase in ("audit", "key", "state", "decisions", "inventory"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                workspace, _queue_id = create_failed_reference_workspace(root)
                output = root / "audit"
                expected = audit.publish_failure_reference_audit(workspace, output)
                document = json.loads((output / "audit.json").read_text())
                decisions = audit.record_failure_reference_decision(
                    output, document["groups"][0]["group_id"], "neither_acceptable"
                )
                gate = {
                    "path": {
                        "audit": output / "audit.json",
                        "key": output / ".blind-key.json",
                        "state": workspace / "generated-audio/generation-state.json",
                        "decisions": output / "decisions.json",
                        "inventory": output / "audit.json",
                    }[phase],
                    "armed": False,
                }
                boundary = {
                    "audit": "_read_failure_reference_audit",
                    "key": "_read_failure_reference_audit",
                    "state": "_validate_audit_source_authority",
                    "decisions": "_load_validated_audit",
                    "inventory": "_validate_decision_inventory",
                }[phase]
                original = getattr(audit, boundary)

                def arm(*args, **kwargs):
                    if phase == "decisions":
                        result = original(*args, **kwargs)
                        gate["armed"] = True
                        return result
                    gate["armed"] = True
                    return original(*args, **kwargs)

                loader = (
                    audit.load_failure_reference_decisions
                    if phase in {"decisions", "inventory"}
                    else audit.load_failure_reference_audit
                )
                error_type = (
                    OSError
                    if phase == "inventory"
                    else audit.FailureReferenceAuditError
                )
                before = tree_hashes(root)
                with (
                    self.swap_at_acquisition(gate),
                    patch.object(audit, boundary, side_effect=arm),
                    self.assertRaisesRegex(error_type, "regular file"),
                ):
                    loader(output)
                self.assertEqual(tree_hashes(root), before)
                self.assertEqual(audit.load_failure_reference_audit(output), expected)
                self.assertEqual(
                    audit.load_failure_reference_decisions(output), decisions
                )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_audit_file_phases_refuse_acquisition_and_recover(self):
        for phase in (
            "copy",
            "staged",
            "final",
            "candidate",
            "workspace",
            "queue",
            "manifest",
        ):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                workspace, _queue_id = create_failed_reference_workspace(root)
                output = root / "audit"
                expected = audit.publish_failure_reference_audit(
                    workspace, root / "expected", seed=7
                )
                gate = {"path": None, "armed": False}
                if phase in {"copy", "staged", "final"}:
                    if phase in {"copy", "staged"}:
                        boundary = (
                            "copy_regular_file" if phase == "copy" else "file_sha256"
                        )
                        original = getattr(audit, boundary)

                        def arm(path, *args, **kwargs):
                            gate["path"] = Path(path)
                            gate["armed"] = True
                            with (
                                self.swap_at_acquisition(gate),
                                self.assertRaises(OSError) as caught,
                            ):
                                original(path, *args, **kwargs)
                            raise caught.exception
                    else:
                        original = audit._assert_audit_publication_sources_unchanged

                        def arm(source, files):
                            files = list(files)
                            self.assertTrue(files)
                            gate["path"] = files[0][0]
                            gate["armed"] = True
                            return original(source, files)

                        boundary = "_assert_audit_publication_sources_unchanged"
                    before = tree_hashes(root)
                    with patch.object(audit, boundary, side_effect=arm):
                        if phase in {"copy", "staged"}:
                            with self.assertRaisesRegex(
                                audit.FailureReferenceAuditError, "regular file"
                            ):
                                audit.publish_failure_reference_audit(
                                    workspace, output, seed=7
                                )
                        else:
                            with (
                                self.swap_at_acquisition(gate),
                                self.assertRaisesRegex(
                                    audit.FailureReferenceAuditError, "regular file"
                                ),
                            ):
                                audit.publish_failure_reference_audit(
                                    workspace, output, seed=7
                                )
                    self.assertEqual(tree_hashes(root), before)
                    self.assertFalse(output.exists())
                    self.assertFalse(list(root.glob(".audit.staging-*")))
                    recovered = audit.publish_failure_reference_audit(
                        workspace, output, seed=7
                    )
                    self.assertEqual(recovered.audit_id, expected.audit_id)
                else:
                    audit.publish_failure_reference_audit(workspace, output, seed=7)
                    document = json.loads((output / "audit.json").read_text())
                    gate["path"] = {
                        "candidate": output
                        / document["groups"][0]["candidates"][0]["audio"],
                        "workspace": workspace / "workspace.json",
                        "queue": workspace / "queue.jsonl",
                        "manifest": workspace / "inputs/voice/manifest.json",
                    }[phase]
                    boundary = (
                        "_validate_audit_candidate_files"
                        if phase == "candidate"
                        else "_validate_audit_source_authority"
                    )
                    original = getattr(audit, boundary)

                    def arm(*args, **kwargs):
                        gate["armed"] = True
                        return original(*args, **kwargs)

                    before = tree_hashes(root)
                    with (
                        self.swap_at_acquisition(gate),
                        patch.object(audit, boundary, side_effect=arm),
                        self.assertRaisesRegex(OSError, "regular file"),
                    ):
                        audit.load_failure_reference_audit(output)
                    self.assertEqual(tree_hashes(root), before)
                    self.assertEqual(
                        audit.load_failure_reference_audit(output).audit_id,
                        expected.audit_id,
                    )
