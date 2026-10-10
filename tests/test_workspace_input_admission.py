import builtins
import hashlib
import io
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.authoring_fixtures import (
    create_carry_source_workspace,
    create_specialist_failure_workspace,
    create_test_workspace,
    tree_hashes,
    write_carry_target_manifest,
)
from vntts.authoring import specialist_failure_plan as specialist
from vntts.authoring import workspace_creation as creation
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
            nonlocal saved
            if selected(path):
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertIsNone(saved, "source must stop after refused acquisition")
                # Save outside the open guard before swapping the admitted candidate.
                gate["armed"] = False
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
