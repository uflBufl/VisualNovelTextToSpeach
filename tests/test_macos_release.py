import unittest
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]


class MacOSReleaseTest(unittest.TestCase):
    def test_build_script_notarizes_and_verifies_app_and_dmg(self):
        script = (project_root / "scripts" / "build-macos.sh").read_text(
            encoding="utf-8"
        )

        self.assertEqual(script.count("xcrun notarytool submit"), 2)
        self.assertIn('xcrun stapler staple "$app_path"', script)
        self.assertIn('xcrun stapler staple "$dmg_path"', script)
        self.assertIn("spctl --assess --type execute", script)
        self.assertIn("spctl --assess --type open", script)

    def test_release_workflow_requires_signing_and_notary_secrets(self):
        workflow = (
            project_root / ".github" / "workflows" / "macos-release.yml"
        ).read_text(encoding="utf-8")

        for secret in (
            "MACOS_DEVELOPER_ID_CERTIFICATE_BASE64",
            "MACOS_DEVELOPER_ID_CERTIFICATE_PASSWORD",
            "MACOS_NOTARY_APPLE_ID",
            "MACOS_NOTARY_APP_PASSWORD",
            "MACOS_NOTARY_TEAM_ID",
        ):
            self.assertIn(secret, workflow)
        self.assertIn("scripts/build-macos.sh", workflow)
        self.assertIn("'macos-15'", workflow)
        self.assertIn("'macos-15-intel'", workflow)
        self.assertIn(
            "dist/VisualNovelTextToSpeech-macos-*-notarization.json", workflow
        )


if __name__ == "__main__":
    unittest.main()
