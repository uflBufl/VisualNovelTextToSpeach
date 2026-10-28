import importlib.util
import runpy
import sys
import tomllib
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY_COLLECTION_PATH = (
    PROJECT_ROOT / "packaging" / "pyinstaller" / "dependency_collection.py"
)
dependency_collection_spec = importlib.util.spec_from_file_location(
    "dependency_collection", DEPENDENCY_COLLECTION_PATH
)
dependency_collection = importlib.util.module_from_spec(dependency_collection_spec)
dependency_collection_spec.loader.exec_module(dependency_collection)


class ReleasePackagingTest(unittest.TestCase):
    def test_icons_are_package_data_for_wheel_and_source_distribution(self):
        project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())

        self.assertEqual(
            project["tool"]["setuptools"]["package-data"]["vntts"],
            ["icons/lucide/*.svg", "icons/lucide/LICENSE"],
        )

    def test_bundled_x11_worker_dispatches_before_desktop_imports(self):
        worker = ModuleType("vntts.x11_capture_worker")
        worker.main = Mock(return_value=0)
        with (
            patch.dict(sys.modules, {"vntts.x11_capture_worker": worker}),
            patch.object(
                sys, "argv", ["VisualNovelTextToSpeech", "--x11-capture-worker"]
            ),
        ):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(PROJECT_ROOT / "vntts/app.py"), run_name="__main__")
        self.assertEqual(stopped.exception.code, 0)
        worker.main.assert_called_once_with()

    def test_native_capture_helpers_are_built_bundled_and_verified(self):
        for platform, suffix in (("macos", "sh"), ("windows", "ps1")):
            with self.subTest(platform=platform):
                script = (
                    PROJECT_ROOT / f"scripts/build-{platform}.{suffix}"
                ).read_text()
                spec = (PROJECT_ROOT / f"packaging/{platform}/vntts.spec").read_text()
                self.assertIn(f"build-{platform}-capture.{suffix}", script)
                self.assertIn("capture_worker,", spec)
                self.assertIn('(str(capture_worker), "native-capture")', spec)
        macos_spec = (PROJECT_ROOT / "packaging/macos/vntts.spec").read_text()
        self.assertIn('"LSMinimumSystemVersion": "14.0"', macos_spec)
        macos_build = (PROJECT_ROOT / "scripts/build-macos.sh").read_text()
        self.assertIn('vntts-capture-worker" --self-test', macos_build)
        windows_build = (PROJECT_ROOT / "scripts/build-windows.ps1").read_text()
        self.assertIn(
            "VisualNovelTextToSpeech\\_internal\\native-capture\\vntts-capture-worker.exe",
            windows_build,
        )
        verifier = (PROJECT_ROOT / "scripts/verify-windows-bundle.ps1").read_text()
        self.assertIn('"Bundled native capture worker"', verifier)
        self.assertNotIn('"Import mss"', verifier)
        project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        self.assertFalse(
            any(item.startswith("mss") for item in project["project"]["dependencies"])
        )

    def test_windows_qualification_accepts_windows_10_build_19041(self):
        script = (PROJECT_ROOT / "scripts/run-windows-release-test.ps1").read_text()
        self.assertIn("$BuildNumber -lt 19041", script)
        self.assertIn("Windows (10|11)", script)
        workflow = (
            PROJECT_ROOT / ".github/workflows/windows-release-test.yml"
        ).read_text()
        self.assertNotIn("windows-11", workflow)

    def test_decoder_is_staged_with_native_libraries_and_licenses(self):
        for platform, suffix in (("macos", "sh"), ("windows", "ps1")):
            script = (PROJECT_ROOT / f"scripts/build-{platform}.{suffix}").read_text()
            spec = (PROJECT_ROOT / f"packaging/{platform}/vntts.spec").read_text()
            self.assertIn("vntts.game_audio_decoder", script)
            self.assertIn('os.environ["VNTTS_VGMSTREAM_DIR"]', spec)
            self.assertIn('"vgmstream/licenses"', spec)

    def test_platform_builds_stage_locked_pocket_runtime(self):
        for relative_path in (
            "scripts/build-macos.sh",
            "scripts/build-windows.ps1",
        ):
            with self.subTest(path=relative_path):
                script = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("vntts.release_runtime", script)
                self.assertIn("VNTTS_SPEECH_RUNTIMES_DIR", script)

        windows_script = (PROJECT_ROOT / "scripts/build-windows.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "--backend qwen-tts --python-version 3.14 --append", windows_script
        )

    def test_platform_specs_require_staged_runtime(self):
        for relative_path in (
            "packaging/macos/vntts.spec",
            "packaging/windows/vntts.spec",
        ):
            with self.subTest(path=relative_path):
                spec = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn('os.environ["VNTTS_SPEECH_RUNTIMES_DIR"]', spec)
                self.assertIn('"runtime-manifest.json"', spec)

    def test_shared_dependency_collector_preserves_inventory(self):
        def copy_distribution_metadata(distribution):
            if distribution == "torchcodec":
                raise RuntimeError("metadata unavailable")
            return [(f"{distribution}-metadata", "metadata")]

        packages = "r1999extractor TTS coqpit gruut ko_speech_tools trainer".split()
        distributions = (
            "coqui-tts coqpit gruut ko-speech-tools torch torchaudio torchcodec "
            "trainer transformers reverse1999-extractor"
        ).split()
        datas, binaries, hidden_imports = [], [], []
        with (
            patch.object(
                dependency_collection,
                "collect_data_files",
                return_value=[("vntts/icons/lucide/play.svg", "vntts/icons/lucide")],
            ) as collect_data_files,
            patch.object(
                dependency_collection,
                "collect_all",
                side_effect=lambda package: (
                    [(f"{package}-data", "data")],
                    [(f"{package}-binary", "binary")],
                    [f"{package}.import"],
                ),
            ) as collect_all,
            patch.object(
                dependency_collection,
                "copy_metadata",
                side_effect=copy_distribution_metadata,
            ) as copy_metadata,
        ):
            dependency_collection.collect_packaged_dependencies(
                datas, binaries, hidden_imports
            )

        collect_data_files.assert_called_once_with(
            "vntts", includes=["icons/lucide/*.svg", "icons/lucide/LICENSE"]
        )
        self.assertEqual(
            collect_all.call_args_list, [call(package) for package in packages]
        )
        self.assertEqual(
            copy_metadata.call_args_list, [call(item) for item in distributions]
        )
        self.assertEqual(
            datas,
            [("vntts/icons/lucide/play.svg", "vntts/icons/lucide")]
            + [(f"{name}-data", "data") for name in packages]
            + [
                (f"{name}-metadata", "metadata")
                for name in distributions
                if name != "torchcodec"
            ],
        )
        self.assertEqual(binaries, [(f"{name}-binary", "binary") for name in packages])
        self.assertEqual(hidden_imports, [f"{name}.import" for name in packages])

    def test_platform_specs_use_shared_dependency_collector(self):
        for relative_path in (
            "packaging/macos/vntts.spec",
            "packaging/windows/vntts.spec",
        ):
            with self.subTest(path=relative_path):
                spec = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(
                    "from dependency_collection import collect_packaged_dependencies",
                    spec,
                )
                self.assertIn(
                    "collect_packaged_dependencies(datas, binaries, hidden_imports)",
                    spec,
                )

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

    def test_chatterbox_windows_qualification_uses_locked_runtime_and_benchmark(self):
        script = (PROJECT_ROOT / "scripts/qualify-chatterbox-nano.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("uv sync --project backends/chatterbox-nano --frozen", script)
        self.assertIn("--backend chatterbox-nano", script)
        self.assertIn("samples\\speakers\\01.wav", script)
        self.assertIn("--narrator-reference $NarratorReference", script)
        self.assertIn("Test-Path $AudioPath -PathType Leaf", script)

    def test_adaptive_moss_qualification_requires_complete_shareable_evidence(self):
        script = (PROJECT_ROOT / "scripts/qualify-moss-adaptive-windows.ps1").read_text(
            encoding="utf-8"
        )

        for contract in (
            "ExpectedBuild = 'd029eb0'",
            "$BuildCommit = [string]$Manifest.source",
            "--require-changing-voice",
            ".all_requests_terminal -ne $true",
            "-notin @('complete', 'limited')",
            "@('request_s', 'prefill_s', 'gen_s', 'decode_s')",
            "$null -eq $Attempt.output_wav_validation_s",
            "$null -eq $Attempt.raw_wav_validation_s",
            "completion=$($Attempt.completion)",
            "Report: $ReportPath",
            "timing_eligible = $_.completion -eq 'complete'",
            "qualified = $true",
            "qualified = $false",
            "contains_generated_voice_audio",
            "Get-FileHash -LiteralPath $Archive",
            "$ArchiveFile.Length -gt $MaxArchiveBytes",
            "publication_seconds",
        ):
            self.assertIn(contract, script)
        self.assertNotIn("$Manifest.vntts", script)
        self.assertNotIn("artifact = @{ path = $Artifact", script)

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
                    "VNTTS_CAPTURE_WORKER",
                    "VNTTS_POCKET_TTS_RUNTIME",
                    "VNTTS_CHATTERBOX_RUNTIME",
                    "VNTTS_MOSS_RUNTIME",
                    "VNTTS_QWEN_RUNTIME",
                    "VNTTS_MOSS_DELAY_RUNTIME",
                ):
                    self.assertIn(name, script)

    def test_windows_release_gate_requires_production_auto_advance_acknowledgement(
        self,
    ):
        fixture = (PROJECT_ROOT / "scripts/windows-capture-fixture.ps1").read_text(
            encoding="utf-8"
        )
        verifier = (PROJECT_ROOT / "scripts/verify-windows-bundle.ps1").read_text(
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
        self.assertIn("BundleDirectory", qualification)
        self.assertIn("portable_archive_sha256", qualification)

        workflow = (
            PROJECT_ROOT / ".github/workflows/windows-release-test.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("bundle_run_id", workflow)


if __name__ == "__main__":
    unittest.main()
