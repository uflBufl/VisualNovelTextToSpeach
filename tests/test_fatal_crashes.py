import json
import os
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from unittest.mock import patch

from vntts import fatal_crashes


class FatalCrashesTest(unittest.TestCase):
    def run_child(self, root, name, source, *arguments):
        script = root / "vntts" / f"{name}.py"
        script.parent.mkdir(exist_ok=True)
        script.write_text(dedent(source), encoding="utf-8")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        environment["VNTTS_SETTINGS_FILE"] = str(root / "settings.json")
        return subprocess.run(
            [sys.executable, str(script), *map(str, arguments)],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_fatal_exit_survives_restart_and_sanitized_support_export(self):
        with TemporaryDirectory(prefix="private-crash-probe-") as directory:
            root = Path(directory)
            log = root / "fatal-crashes.log"
            crash = self.run_child(
                root,
                "fatal_probe",
                """
                import atexit, os, sys
                from pathlib import Path
                from vntts.fatal_crashes import configure_fatal_crash_log, fatal_crash_report
                if sys.platform == "win32":
                    import ctypes
                    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
                    ctypes.windll.kernel32.WerSetFlags(0x0020)  # WER_FAULT_REPORTING_NO_UI
                else:
                    import resource
                    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                configure_fatal_crash_log(Path(sys.argv[1]))
                fatal_crash_report()
                def crash_during_teardown():
                    private_text = "PRIVATE_DIALOGUE_SENTINEL"
                    os.abort()
                atexit.register(crash_during_teardown)
                """,
                log,
            )
            self.assertNotEqual(crash.returncode, 0)
            raw = log.read_bytes()
            reports = []
            for index in range(2):
                archive_path = root / f"support-{index}.zip"
                restart = self.run_child(
                    root,
                    "restart_probe",
                    """
                    import sys
                    from pathlib import Path
                    from vntts.fatal_crashes import configure_fatal_crash_log
                    from vntts.settings import AppSettings
                    from vntts.support import RuntimeSupportLog, SupportBundleBuilder
                    configure_fatal_crash_log(Path(sys.argv[1]))
                    SupportBundleBuilder(
                        AppSettings(), RuntimeSupportLog(), dependency_probe=lambda: {}
                    ).build(sys.argv[2])
                    """,
                    log,
                    archive_path,
                )
                self.assertEqual(restart.returncode, 0, restart.stderr)
                with zipfile.ZipFile(archive_path) as archive:
                    report = json.loads(archive.read("fatal-crashes.json"))
                    names = archive.namelist()
                self.assertNotIn("fatal-crashes.log", names)
                self.assertTrue(report["available"])
                self.assertTrue(report["recording_enabled"])
                frames = [
                    frame
                    for fault in report["faults"]
                    for thread in fault["threads"]
                    for frame in thread["frames"]
                ]
                self.assertTrue(
                    any(
                        frame["file"] == "vntts/fatal_probe.py"
                        and frame["function"] == "crash_during_teardown"
                        for frame in frames
                    ),
                    report,
                )
                serialized = json.dumps(report)
                self.assertNotIn(str(root), serialized)
                self.assertNotIn("PRIVATE_DIALOGUE_SENTINEL", serialized)
                reports.append(report["faults"])
                self.assertEqual(log.read_bytes(), raw)
            self.assertEqual(reports[0], reports[1])

    def test_export_accepts_locations_and_discards_private_dump_content(self):
        raw = "\r\n".join(
            (
                "Windows fatal exception: access violation",
                "Current thread 0xabc (most recent call first):",
                '  File "C:\\Users\\private-player\\project\\vntts\\playback.py", line 42 in play',
                '  File "/Volumes/private-player/source/scripts/probe.py", line 7 in <module>',
                '  File "/private/private-player/secret.py", line 9 in external_call',
                '  File "/private/secrets.py", line 10 in PRIVATE DIALOGUE',
                "  PRIVATE DIALOGUE = secret_local_value",
                "Current thread's C stack trace (most recent call first):",
                '  Binary file "/private/native_secret.dll", at secret_symbol+0x12',
                "Extension modules: private_extension",
            )
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fatal.log"
            path.write_bytes(raw.encode())
            with patch.object(fatal_crashes, "_path", path):
                report = fatal_crashes.fatal_crash_report()
        frames = report["faults"][0]["threads"][0]["frames"]
        self.assertEqual(
            frames,
            [
                {"file": "vntts/playback.py", "line": 42, "function": "play"},
                {"file": "scripts/probe.py", "line": 7, "function": "<module>"},
                {"file": "<external>", "line": 9, "function": "external_call"},
            ],
        )
        self.assertEqual(report["faults"][0]["reason"], "access violation")
        self.assertFalse(report["native_memory_dump_included"])
        serialized = json.dumps(report)
        for private in (
            "private-player",
            "PRIVATE DIALOGUE",
            "secret_local_value",
            "native_secret",
            "private_extension",
        ):
            self.assertNotIn(private, serialized)

    def test_report_bounds_input_faults_threads_and_frames(self):
        frame = '  File "/private/project/vntts/playback.py", line 42 in play\n'
        thread = "Thread 0xabc (most recent call first):\n" + frame * 80
        current = (
            "Current thread 0xdef (most recent call first):\n"
            + frame.replace("in play", "in crash_site") * 80
        )
        fault = "Fatal Python error: Aborted\n" + thread * 40 + current
        large_thread = thread.replace(
            "/private/project/", "/private/" + "x" * 200 + "/"
        )
        single_fault = "Fatal Python error: Aborted\n" + large_thread * 100 + current
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fatal.log"
            for contents in (fault * 8, single_fault):
                path.write_text(contents, encoding="utf-8")
                with patch.object(fatal_crashes, "_path", path):
                    report = fatal_crashes.fatal_crash_report()
                self.assertTrue(report["truncated"])
                self.assertTrue(report["available"])
                self.assertLessEqual(len(report["faults"]), 4)
                for record in report["faults"]:
                    self.assertLessEqual(len(record["threads"]), 32)
                    self.assertTrue(record["truncated"])
                    self.assertTrue(record["threads"][0]["current"])
                    self.assertEqual(
                        record["threads"][0]["frames"][0]["function"], "crash_site"
                    )
                    for thread in record["threads"]:
                        self.assertLessEqual(len(thread["frames"]), 64)
                        self.assertTrue(thread["truncated"])
                if contents == single_fault:
                    self.assertEqual(report["faults"][0]["reason"], "unknown")

    def test_unavailable_capture_does_not_abort_startup_or_expose_paths(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fatal.log"
            with (
                patch.object(fatal_crashes, "_descriptor", None),
                patch.object(fatal_crashes, "_path", None),
                patch.object(fatal_crashes, "_recording_error", None),
                patch(
                    "vntts.fatal_crashes.os.open",
                    side_effect=PermissionError("private-path"),
                ),
                patch("vntts.fatal_crashes.faulthandler.enable") as enable,
                self.assertLogs("vntts.fatal_crashes", level="WARNING") as logs,
            ):
                fatal_crashes.configure_fatal_crash_log(path)
                report = fatal_crashes.fatal_crash_report()
            enable.assert_not_called()
            self.assertFalse(report["recording_enabled"])
            self.assertEqual(report["recording_error"], "PermissionError")
            self.assertNotIn("private-path", json.dumps(report))
            self.assertNotIn("private-path", "\n".join(logs.output))
            self.assertIsNone(logs.records[0].exc_info)

    def test_missing_or_invalid_log_does_not_break_export(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for path in (root / "missing.log", root):
                with (
                    self.subTest(path=path),
                    patch.object(fatal_crashes, "_path", path),
                ):
                    report = fatal_crashes.fatal_crash_report()
                    self.assertFalse(report["available"])
                    self.assertEqual(report["faults"], [])
            if hasattr(os, "mkfifo"):
                pipe = root / "fatal.pipe"
                os.mkfifo(pipe)
                descriptor = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    result = self.run_child(
                        root,
                        "fifo_probe",
                        """
                        import json, sys
                        from pathlib import Path
                        from vntts.fatal_crashes import configure_fatal_crash_log, fatal_crash_report
                        configure_fatal_crash_log(Path(sys.argv[1]))
                        print(json.dumps(fatal_crash_report()))
                        """,
                        pipe,
                    )
                finally:
                    os.close(descriptor)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report["recording_enabled"])
                self.assertEqual(report["recording_error"], "OSError")
