import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class UiCatalogTest(unittest.TestCase):
    def test_catalog_renders_real_states_and_focused_astra_packet(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "voice-editor",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((output / "index.html").is_file())
            for story_id in (
                "dashboard.stories-ready",
                "dashboard.reading-active",
                "dashboard.setup-expanded",
                "voice-editor.narrator",
                "voice-editor.live-recovery",
                "voice-editor.preview-generating",
                "voice-editor.preview-failure",
                "voice-editor.long-values",
                "voice-editor.saved-return",
                "settings.speech-and-voices",
                "settings.validation-error",
                "unknown-speaker-prompt.awaiting-choice",
            ):
                self.assertGreater(
                    (output / "screenshots" / f"{story_id}.png").stat().st_size,
                    0,
                )

            packet = json.loads(
                (output / "review-packets" / "voice-editor.json").read_text()
            )
            self.assertEqual(packet["target"]["id"], "voice-editor")
            self.assertEqual(packet["target"]["canonical_owner"], "voice-editor")
            self.assertIn(
                "settings", {surface["id"] for surface in packet["related_surfaces"]}
            )
            self.assertNotIn("source_path", json.dumps(packet))
            self.assertEqual(
                set(packet["target"]),
                {
                    "id",
                    "title",
                    "family",
                    "audience",
                    "mission",
                    "canonical_owner",
                    "related",
                    "contracts",
                    "stories",
                },
            )

            obsolete = output / "screenshots" / "removed-story.png"
            obsolete.write_bytes(b"stale")

            second_result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "source-voice-mapping",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertFalse(obsolete.exists())
            self.assertEqual(list((output / "screenshots").glob("*.png")), [])


if __name__ == "__main__":
    unittest.main()
