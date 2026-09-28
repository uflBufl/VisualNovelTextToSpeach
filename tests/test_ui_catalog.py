import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class UiCatalogTest(unittest.TestCase):
    def test_authoring_review_catalog_covers_focus_and_long_names(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "authoring-workbench",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for state in (
                "review",
                "review-play-focus",
                "review-long-names",
                "review-long-names-second",
                "compact",
                "compact-actions",
            ):
                path = output / "screenshots" / f"authoring-workbench.{state}.png"
                self.assertGreater(path.stat().st_size, 0)

    def test_voice_route_catalog_covers_portraits_and_compact_actions(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "offline-preparation",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for state in (
                "voice-confirmation",
                "voice-confirmation-portraits",
                "voice-confirmation-mixed-portraits",
                "voice-confirmation-compact",
                "voice-confirmation-compact-scrolled",
                "voice-confirmation-compact-last-route",
            ):
                path = output / "screenshots" / f"offline-preparation.{state}.png"
                self.assertGreater(path.stat().st_size, 0)

    def test_legacy_reason_review_packet_covers_listening_failure_and_compact(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "legacy-reason-review",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "legacy-reason-review.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "legacy-reason-review.pending",
                    "legacy-reason-review.heard",
                    "legacy-reason-review.selected",
                    "legacy-reason-review.save-error",
                    "legacy-reason-review.publish-error",
                    "legacy-reason-review.compact",
                    "legacy-reason-review.compact-reasons",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_settings_packet_covers_sections_disclosure_and_compact_state(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "settings",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "settings.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "settings.speech-and-voices",
                    "settings.shortcuts",
                    "settings.capture",
                    "settings.capture-window",
                    "settings.advanced-capture",
                    "settings.playback",
                    "settings.application",
                    "settings.checkbox-focus",
                    "settings.advanced-speech",
                    "settings.compact-capture",
                    "settings.compact-advanced-speech",
                    "settings.compact-advanced-speech-scrolled",
                    "settings.validation-error",
                },
            )
            self.assertGreater(
                (output / "screenshots" / "settings.checkbox-focus.png").stat().st_size,
                0,
            )

    def test_readiness_packet_covers_distinct_recovery_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "readiness",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "readiness.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "readiness.loading",
                    "readiness.blocked",
                    "readiness.audio-error",
                    "readiness.warning-selected",
                    "readiness.warning-only",
                    "readiness.non-english-ocr",
                    "readiness.ready",
                    "readiness.long-error",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_onboarding_packet_covers_first_run_and_compact_recovery(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "onboarding",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "onboarding.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "onboarding.game-window",
                    "onboarding.screen-region",
                    "onboarding.game-window-error",
                    "onboarding.diagnostics-loading",
                    "onboarding.diagnostics-error",
                    "onboarding.calibration",
                    "onboarding.test",
                    "onboarding.test-success",
                    "onboarding.compact-error",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_macos_permission_packet_covers_known_unknown_and_compact_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "macos-permissions",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "macos-permissions.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "macos-permissions.denied",
                    "macos-permissions.granted",
                    "macos-permissions.unavailable",
                    "macos-permissions.compact",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_asset_packet_covers_voice_and_model_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "asset-manager",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "asset-manager.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "asset-manager.voices-empty",
                    "asset-manager.voices-manifest",
                    "asset-manager.voices-validation",
                    "asset-manager.voices-error",
                    "asset-manager.model-unchecked",
                    "asset-manager.model-verified",
                    "asset-manager.model-download",
                    "asset-manager.model-failure",
                    "asset-manager.compact-voices",
                    "asset-manager.compact-model",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_game_profile_packet_covers_selection_and_compact_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "game-profiles",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "game-profiles.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "game-profiles.empty",
                    "game-profiles.active",
                    "game-profiles.other-selected",
                    "game-profiles.compact",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_voice_import_packet_covers_empty_long_and_compact_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "voice-import",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "voice-import.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "voice-import.empty",
                    "voice-import.selected",
                    "voice-import.long-values",
                    "voice-import.compact",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_calibration_packet_covers_selection_and_review_outcomes(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "calibration",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "calibration.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "calibration.overlay-empty",
                    "calibration.overlay-selected",
                    "calibration.overlay-save-failure",
                    "calibration.review-loading",
                    "calibration.review-recognized",
                    "calibration.review-failure",
                    "calibration.review-compact",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

    def test_dashboard_packet_renders_active_and_recovery_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "dashboard",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "dashboard.json").read_text()
            )
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "dashboard.stories-ready",
                    "dashboard.reading-loading",
                    "dashboard.reading-active",
                    "dashboard.reading-waiting",
                    "dashboard.reading-paused",
                    "dashboard.reading-stopped",
                    "dashboard.reading-technical",
                    "dashboard.story-position-recovery",
                    "dashboard.reading-long-values",
                    "dashboard.reading-long-values-scrolled",
                    "dashboard.story-position-manual",
                    "dashboard.setup-expanded",
                },
            )
            for story_id in (
                "dashboard.reading-paused",
                "dashboard.reading-long-values",
                "compact-controller.sequence-recovery",
            ):
                self.assertGreater(
                    (output / "screenshots" / f"{story_id}.png").stat().st_size,
                    0,
                )

    def test_story_match_recovery_renders_both_states(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "catalog"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--output",
                    str(output),
                    "--surface",
                    "story-match-recovery",
                ],
                cwd=root,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packet = json.loads(
                (output / "review-packets" / "story-match-recovery.json").read_text()
            )
            self.assertEqual(packet["target"]["id"], "story-match-recovery")
            self.assertEqual(
                {story["id"] for story in packet["target"]["stories"]},
                {
                    "story-match-recovery.awaiting-choice",
                    "story-match-recovery.larger-text",
                },
            )
            for story in packet["target"]["stories"]:
                self.assertGreater(
                    (output / "screenshots" / f"{story['id']}.png").stat().st_size,
                    0,
                )

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
                "unknown-speaker-prompt.long-name",
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
                    "character-story-review",
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(second_result.returncode, 0, second_result.stderr)
            self.assertFalse(obsolete.exists())
            screenshots = {path.name for path in (output / "screenshots").glob("*.png")}
            self.assertEqual(
                {
                    name
                    for name in screenshots
                    if name.startswith("character-story-review.")
                },
                {
                    "character-story-review.pending.png",
                    "character-story-review.compact.png",
                    "character-story-review.compact-actions.png",
                },
            )
            self.assertFalse(
                any(name.startswith("source-voice-mapping.") for name in screenshots)
            )


if __name__ == "__main__":
    unittest.main()
