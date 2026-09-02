import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReleasePackagingTest(unittest.TestCase):
    def test_platform_builds_stage_locked_pocket_runtime(self):
        for relative_path in (
            "scripts/build-macos.sh",
            "scripts/build-windows.ps1",
        ):
            with self.subTest(path=relative_path):
                script = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("vntts.release_runtime", script)
                self.assertIn("VNTTS_SPEECH_RUNTIMES_DIR", script)

    def test_platform_specs_require_staged_runtime(self):
        for relative_path in (
            "packaging/macos/vntts.spec",
            "packaging/windows/vntts.spec",
        ):
            with self.subTest(path=relative_path):
                spec = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn('os.environ["VNTTS_SPEECH_RUNTIMES_DIR"]', spec)
                self.assertIn('"runtime-manifest.json"', spec)

    def test_platform_bundles_include_the_reverse1999_content_provider(self):
        for relative_path in (
            "packaging/macos/vntts.spec",
            "packaging/windows/vntts.spec",
        ):
            with self.subTest(path=relative_path):
                spec = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn('collect_all("r1999extractor")', spec)
                self.assertIn('"reverse1999-extractor"', spec)

    def test_platform_specs_copy_torchcodec_metadata(self):
        for relative_path in (
            "packaging/macos/vntts.spec",
            "packaging/windows/vntts.spec",
        ):
            with self.subTest(path=relative_path):
                spec = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn('"torchcodec"', spec)

    def test_windows_spec_collects_staged_runtime(self):
        spec = (PROJECT_ROOT / "packaging/windows/vntts.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn('"speech-runtimes"', spec)
        self.assertIn('/ "pocket-tts" / "python.exe"', spec)
        self.assertNotIn('/ "Scripts" / "python.exe"', spec)

    def test_windows_build_discovers_tesseract_before_standard_locations(self):
        script = (PROJECT_ROOT / "scripts/build-windows.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('Get-Command "tesseract.exe"', script)
        self.assertIn('Join-Path $env:ProgramFiles "Tesseract-OCR"', script)
        self.assertLess(
            script.index('Get-Command "tesseract.exe"'),
            script.index('Join-Path $env:ProgramFiles "Tesseract-OCR"'),
        )

    def test_macos_runtime_is_injected_without_pyinstaller_reclassification(self):
        spec = (PROJECT_ROOT / "packaging/macos/vntts.spec").read_text(encoding="utf-8")
        script = (PROJECT_ROOT / "scripts/build-macos.sh").read_text(encoding="utf-8")

        self.assertNotIn(
            'datas.append((str(speech_runtimes_directory), "speech-runtimes"))',
            spec,
        )
        self.assertIn("Contents/Resources/speech-runtimes", script)
        self.assertIn("Contents/Frameworks/speech-runtimes", script)
        self.assertIn(
            'ln -s ../Resources/speech-runtimes "$runtime_bundle_link"', script
        )
        self.assertIn('find "$runtime_bundle_path" -type f -print0', script)
        self.assertIn('codesign "${app_codesign_arguments[@]}" "$app_path"', script)
        self.assertIn('target_arch != "$host_arch"', script)

    def test_bundle_verifiers_clear_developer_runtime_overrides(self):
        for relative_path in (
            "scripts/verify-macos-bundle.sh",
            "scripts/verify-windows-bundle.ps1",
        ):
            with self.subTest(path=relative_path):
                script = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                for name in (
                    "PYTHONHOME",
                    "PYTHONPATH",
                    "VIRTUAL_ENV",
                    "VNTTS_POCKET_TTS_RUNTIME",
                    "VNTTS_CHATTERBOX_RUNTIME",
                    "VNTTS_MOSS_RUNTIME",
                    "VNTTS_MOSS_DELAY_RUNTIME",
                ):
                    self.assertIn(name, script)

    def test_windows_release_gate_requires_production_auto_advance_acknowledgement(
        self,
    ):
        fixture = (PROJECT_ROOT / "scripts/windows-capture-fixture.ps1").read_text(
            encoding="utf-8"
        )
        verifier = (PROJECT_ROOT / "scripts/verify-windows-installer.ps1").read_text(
            encoding="utf-8"
        )
        qualification = (
            PROJECT_ROOT / "scripts/run-windows-release-test.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("Auto advance acknowledged.", fixture)
        self.assertIn("VerifyAutoAdvance", verifier)
        self.assertIn("AppController._auto_advance_dialog", verifier)
        self.assertIn("ElevatedSmokeTest", qualification)
        self.assertIn("auto_advance_acknowledged", qualification)
        self.assertIn("SmokeEvidenceReport", qualification)
        self.assertIn("PreviousInstallerPath", qualification)
        self.assertIn("installer_sha256", qualification)

        workflow = (
            PROJECT_ROOT / ".github/workflows/windows-release-test.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("previous_installer_run_id", workflow)


if __name__ == "__main__":
    unittest.main()
