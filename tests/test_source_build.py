import contextlib
import io
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import build


class SourceBuildTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.root = Path(directory) / "project with spaces"
        self.root.mkdir()
        self.enterContext(patch.object(build, "ROOT", self.root))
        self.enterContext(patch.object(build.sys, "argv", ["build.py"]))
        self.enterContext(patch.object(build.shutil, "which", return_value="tool"))
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.errors = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.host = "native-host"

    def run_command(self, command, **kwargs):
        if command[0] == "cargo":
            name = "vntts-audio-worker" + (
                ".exe" if build.sys.platform == "win32" else ""
            )
            binary = (
                self.root / "native/audio-worker/target" / self.host / "release" / name
            )
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(b"fresh host binary")
        return subprocess.CompletedProcess(
            command, 0, stdout=f"rustc version\nhost: {self.host}\n"
        )

    def test_builds_python_and_host_workers_from_project_root(self):
        for platform in ("win32", "darwin", "linux"):
            with (
                self.subTest(platform=platform),
                patch.object(build.sys, "platform", platform),
                patch.dict(os.environ, {"CARGO_BUILD_TARGET": "another-target"}),
                patch.object(build, "run_process", side_effect=self.run_command) as run,
            ):
                build.build()
                commands = [entry.args[0] for entry in run.call_args_list]
                self.assertEqual(
                    commands[:2],
                    [
                        ["rustc", "-vV"],
                        ["uv", "sync", "--frozen"],
                    ],
                )
                self.assertEqual(
                    commands[2],
                    [
                        "cargo",
                        "build",
                        "--release",
                        "--locked",
                        "--manifest-path",
                        str(self.root / "native/audio-worker/Cargo.toml"),
                        "--target-dir",
                        str(self.root / "native/audio-worker/target"),
                        "--target",
                        self.host,
                    ],
                )
                if platform == "darwin":
                    self.assertEqual(
                        commands[3],
                        ["bash", str(self.root / "scripts/build-macos-capture.sh")],
                    )
                elif platform == "win32":
                    self.assertEqual(
                        commands[3],
                        [
                            "powershell",
                            "-NoProfile",
                            "-File",
                            str(self.root / "scripts/build-windows-capture.ps1"),
                        ],
                    )
                else:
                    self.assertEqual(len(commands), 3)
                for entry in run.call_args_list:
                    self.assertEqual(entry.kwargs["cwd"], self.root)
                    self.assertTrue(entry.kwargs["check"])
                name = "vntts-audio-worker" + (".exe" if platform == "win32" else "")
                self.assertEqual(
                    (
                        self.root / "native/audio-worker/target/release" / name
                    ).read_bytes(),
                    b"fresh host binary",
                )

    def test_missing_tool_or_unsupported_host_fails_before_installing_dependencies(
        self,
    ):
        for platform, missing in (
            ("darwin", "cargo"),
            ("darwin", "rustc"),
            ("win32", "powershell"),
            ("linux", "uv"),
            ("freebsd", None),
        ):
            with (
                self.subTest(platform=platform, missing=missing),
                patch.object(build.sys, "platform", platform),
                patch.object(
                    build.shutil,
                    "which",
                    side_effect=lambda name: None if name == missing else "tool",
                ),
                patch.object(build, "run_process") as run,
                self.assertRaises(RuntimeError),
            ):
                build.build()
            run.assert_not_called()

    def test_failed_stage_stops_build_and_preserves_its_exit_status(self):
        for stage in ("rustc", "uv", "cargo", "bash"):

            def run_stage(command, **kwargs):
                if command[0] == stage:
                    raise subprocess.CalledProcessError(7, command)
                return self.run_command(command, **kwargs)

            with (
                self.subTest(stage=stage),
                patch.object(build.sys, "platform", "darwin"),
                patch.object(build, "run_process", side_effect=run_stage) as run,
            ):
                self.assertEqual(build.main(), 7)
                self.assertEqual(run.call_args.args[0][0], stage)
                self.assertIn("Source build failed", self.errors.getvalue())
                self.assertNotIn("Source build complete", self.output.getvalue())

    def test_missing_host_metadata_or_artifact_never_reports_success(self):
        for missing in ("host", "artifact"):
            with (
                self.subTest(missing=missing),
                patch.object(build.sys, "platform", "darwin"),
                patch.object(
                    build,
                    "run_process",
                    return_value=subprocess.CompletedProcess(
                        [],
                        0,
                        stdout="no host"
                        if missing == "host"
                        else f"host: {self.host}\n",
                    ),
                ) as run,
            ):
                self.assertEqual(build.main(), 1)
                self.assertEqual(
                    run.call_args.args[0][0], "rustc" if missing == "host" else "cargo"
                )
                self.assertNotIn("Source build complete", self.output.getvalue())
