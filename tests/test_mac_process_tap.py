"""No capture: compile and exercise the native recorder's bounded PCM admission."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(
    sys.platform == "darwin" and shutil.which("xcrun"), "macOS SDK required"
)
class ProcessTapRecorderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="vntts-process-tap-test-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binary = Path(cls.directory.name) / "process-tap"
        root = Path(__file__).resolve().parents[1]
        subprocess.run(
            [
                "xcrun",
                "clang",
                "-std=c11",
                "-fobjc-arc",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-mmacosx-version-min=14.2",
                "-framework",
                "Foundation",
                "-framework",
                "CoreAudio",
                "-framework",
                "AudioToolbox",
                "-Wl,-sectcreate,__TEXT,__info_plist,scripts/mac_live_recording/process_tap_info.plist",
                "scripts/mac_live_recording/process_tap.m",
                "-o",
                str(cls.binary),
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def run_helper(self, *arguments):
        return subprocess.run(
            [str(self.binary), *arguments],
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_bounded_callback_queue_and_pcm_layouts(self):
        result = self.run_helper("--selfcheck")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no capture or playback", result.stdout)
        self.assertIn("controlled Stop metadata", result.stdout)

    def test_capture_requires_explicit_authorization_and_valid_target_arguments(self):
        output = Path(self.directory.name) / "must-not-exist"
        for arguments in (
            (),
            ("--capture", "1", "/bin/launchd", "1", str(output)),
            (
                "--capture",
                "0",
                "/bin/launchd",
                "1",
                str(output),
                "--allow-audio-capture",
            ),
            (
                "--capture",
                "1",
                "/bin/launchd",
                "nan",
                str(output),
                "--allow-audio-capture",
            ),
            (
                "--capture",
                "1",
                "/bin/launchd",
                "601",
                str(output),
                "--allow-audio-capture",
            ),
            ("--inspect", "-1", "/bin/launchd"),
            ("--inspect", "1", "relative/path"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_helper(*arguments)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(output.exists())
