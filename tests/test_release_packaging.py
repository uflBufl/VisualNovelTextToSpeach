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

    def test_platform_specs_require_and_collect_staged_runtime(self):
        for relative_path in (
            "packaging/macos/vntts.spec",
            "packaging/windows/vntts.spec",
        ):
            with self.subTest(path=relative_path):
                spec = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn('os.environ["VNTTS_SPEECH_RUNTIMES_DIR"]', spec)
                self.assertIn('"runtime-manifest.json"', spec)
                self.assertIn('"speech-runtimes"', spec)


if __name__ == "__main__":
    unittest.main()
