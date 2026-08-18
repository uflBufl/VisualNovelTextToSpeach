import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav

import vntts.authoring.reference_selection as selection_module
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.reference_selection import (
    ReferenceSelectionError,
    inspect_voice_reference_candidates,
    select_voice_reference,
    validate_reference_selection_provenance,
)


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
    def test_reports_and_publishes_explicit_no_overwrite_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(root)
            source = manifest.read_bytes()
            report = inspect_voice_reference_candidates(manifest, "The Hero")
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
            linked.symlink_to(outside)
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
