import hashlib
import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav

import vntts.authoring.reference_selection as selection_module
from tests.authoring_fixtures import tree_hashes
from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from tests.symlink_support import symlink_or_skip
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.reference_selection import (
    ReferenceSelectionError,
    inspect_voice_reference_candidates,
    select_voice_reference,
    validate_reference_selection_provenance,
)
from vntts.reference_quality import analyze_reference


def write_reference(path, frequency):
    sample_rate = 16_000
    indexes = np.arange(sample_rate * 2, dtype=np.float32)
    samples = 0.2 * np.sin(2 * np.pi * frequency * indexes / sample_rate)
    write_pcm16_wav(path, samples, sample_rate)


def write_manifest(root):
    root.mkdir(parents=True, exist_ok=True)
    references = root / "references"
    references.mkdir()
    write_reference(references / "hero-1.wav", 220)
    write_reference(references / "hero-2.wav", 330)
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "game": "Synthetic Game",
                "language": "en",
                "voices": [
                    {
                        "character": "Hero",
                        "speaker": "hero-v1",
                        "aliases": ["The Hero"],
                        "references": [
                            "references/hero-1.wav",
                            "references/hero-2.wav",
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


class AuthoringReferenceSelectionTest(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_public_hash_acquisitions_refuse_fifo_preserve_inputs_and_recover(self):
        for phase in ("manifest recheck", "reference recheck", "provenance"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                manifest = write_manifest(root)
                output = root / "selected.json"
                selected = (
                    manifest
                    if phase == "manifest recheck"
                    else root / "references/hero-1.wav"
                )
                if phase == "provenance":
                    select_voice_reference(manifest, "Hero", 2, output)
                    document = json.loads(output.read_text(encoding="utf-8"))
                before = tree_hashes(root)
                original = selection_module.sha256_file
                refused = []

                def checksum(path):
                    if Path(path) != selected:
                        return original(path)
                    gate: RegularFileGate = {"path": selected, "armed": True}
                    with (
                        swap_at_acquisition(self, gate),
                        self.assertRaisesRegex(OSError, "regular file") as caught,
                    ):
                        original(path)
                    refused.append(caught.exception)
                    raise caught.exception

                with (
                    patch.object(selection_module, "sha256_file", checksum),
                    self.assertRaisesRegex(
                        ReferenceSelectionError, "regular file"
                    ) as caught,
                ):
                    if phase == "provenance":
                        validate_reference_selection_provenance(output, document)
                    else:
                        select_voice_reference(manifest, "Hero", 2, output)
                self.assertEqual(len(refused), 1)
                self.assertIs(caught.exception.__cause__, refused[0])
                self.assertEqual(tree_hashes(root), before)
                if phase != "provenance":
                    self.assertFalse(output.exists())
                    select_voice_reference(manifest, "Hero", 2, output)
                document = json.loads(output.read_text(encoding="utf-8"))
                recovered = tree_hashes(root)
                validate_reference_selection_provenance(output, document)
                validate_reference_selection_provenance(output, document)
                with self.assertRaisesRegex(ReferenceSelectionError, "output exists"):
                    select_voice_reference(manifest, "Hero", 2, output)
                self.assertEqual(tree_hashes(root), recovered)

    def test_relocated_selection_requires_the_same_reference_bytes(self):
        for destination in ("missing", "changed", "matching"):
            with (
                self.subTest(destination=destination),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                manifest = write_manifest(root / "source")
                output = root / "export" / "selected.json"
                if destination == "missing":
                    output.parent.mkdir()
                else:
                    write_manifest(output.parent)
                    if destination == "changed":
                        (output.parent / "references/hero-1.wav").write_bytes(
                            b"changed"
                        )
                before = tree_hashes(root)
                if destination == "matching":
                    result = select_voice_reference(manifest, "Hero", 2, output)
                    self.assertEqual(result.output, output.resolve())
                    validate_reference_selection_provenance(
                        output, json.loads(output.read_text(encoding="utf-8"))
                    )
                    output.unlink()
                else:
                    with self.assertRaises(ReferenceSelectionError):
                        select_voice_reference(manifest, "Hero", 2, output)
                    self.assertFalse(output.exists())
                self.assertEqual(tree_hashes(root), before)

    def test_dangling_symlink_output_is_not_redirected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(root)
            target = root / "redirected.json"
            output = root / "selected.json"
            symlink_or_skip(output, target)
            with self.assertRaisesRegex(ReferenceSelectionError, "output exists"):
                select_voice_reference(manifest, "Hero", 2, output)
            self.assertFalse(target.exists())

    def test_provenance_rejects_non_integer_schema_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(root)
            output = root / "selected.json"
            select_voice_reference(manifest, "Hero", 2, output)
            selected = json.loads(output.read_text(encoding="utf-8"))
            for version in (True, 1.0):
                with self.subTest(version=version):
                    selected["vntts.authoring.reference_selection"][
                        "schema_version"
                    ] = version
                    with self.assertRaisesRegex(
                        ReferenceSelectionError, "provenance is malformed"
                    ):
                        validate_reference_selection_provenance(output, selected)

    def test_temp_cleanup_failure_does_not_hide_published_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(root)
            output = root / "selected.json"
            real_unlink = Path.unlink

            def fail_temp_cleanup(path, *args, **kwargs):
                if path.suffix == ".tmp":
                    raise PermissionError("temporary file is still open")
                return real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_temp_cleanup):
                result = select_voice_reference(manifest, "Hero", 2, output)

            self.assertEqual(result.output, output.resolve())
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["voices"][0][
                    "references"
                ][0],
                "references/hero-2.wav",
            )

    def test_reports_and_publishes_explicit_no_overwrite_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(root)
            source = manifest.read_bytes()
            report = inspect_voice_reference_candidates(manifest, "The Hero")
            self.assertEqual(
                report,
                {
                    "schema": "vntts.authoring-reference-candidates",
                    "schema_version": 1,
                    "manifest": str(manifest.resolve()),
                    "manifest_sha256": hashlib.sha256(source).hexdigest(),
                    "character": "Hero",
                    "references": [
                        {
                            **analyze_reference(root / relative),
                            "path": relative,
                            "reference_number": number,
                        }
                        for number, relative in enumerate(
                            ("references/hero-1.wav", "references/hero-2.wav"),
                            start=1,
                        )
                    ],
                    "objective_ranking": [1, 2],
                    "manual_review_required": [
                        "speaker similarity",
                        "music or background contamination",
                        "spoken content and pronunciation",
                    ],
                },
            )
            output = root / "selected.json"
            result = select_voice_reference(manifest, "Hero", 2, output)
            selected = json.loads(output.read_text(encoding="utf-8"))
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "reference-report",
                        "--voice-manifest",
                        str(manifest),
                        "--character",
                        "Hero",
                    ]
                )
            with self.assertRaisesRegex(ReferenceSelectionError, "exists"):
                select_voice_reference(manifest, "Hero", 1, output)
            self.assertEqual(manifest.read_bytes(), source)
            validate_reference_selection_provenance(output, selected)
            (root / "references/hero-2.wav").write_bytes(b"mutated")
            with self.assertRaisesRegex(ReferenceSelectionError, "changed"):
                validate_reference_selection_provenance(output, selected)

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), report)
        self.assertEqual(result.selected_reference_number, 2)
        self.assertEqual(
            selected["voices"][0]["references"],
            ["references/hero-2.wav", "references/hero-1.wav"],
        )
        provenance = selected["vntts.authoring.reference_selection"]
        self.assertEqual(
            provenance["source_manifest_sha256"], report["manifest_sha256"]
        )
        self.assertEqual(
            provenance["selected_reference_sha256"], report["references"][1]["sha256"]
        )
        self.assertTrue(provenance["manual_review_required"])

    def test_rejects_symlink_escape_and_source_mutation_before_publish(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(root)
            outside = root / "outside.wav"
            write_reference(outside, 440)
            linked = root / "references/linked.wav"
            symlink_or_skip(linked, outside)
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["voices"][0]["references"] = ["references/linked.wav"]
            manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ReferenceSelectionError, "symlinks"):
                inspect_voice_reference_candidates(manifest, "Hero")

            linked.unlink()
            manifest = write_manifest(root / "fresh")
            reference = manifest.parent / "references/hero-1.wav"
            original_assert = selection_module._assert_snapshot_unchanged

            def mutate_then_assert(snapshot):
                reference.write_bytes(b"changed after comparison")
                return original_assert(snapshot)

            output = root / "should-not-exist.json"
            with patch.object(
                selection_module,
                "_assert_snapshot_unchanged",
                side_effect=mutate_then_assert,
            ):
                with self.assertRaisesRegex(ReferenceSelectionError, "changed"):
                    select_voice_reference(manifest, "Hero", 1, output)

        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
