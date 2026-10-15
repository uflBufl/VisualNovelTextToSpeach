import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

import psutil

from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from vntts.authoring import generation_lease as lease_module
from vntts.authoring.generation_lease import (
    BulkGenerationError,
    GenerationLease,
    inspect_process_status,
    is_native_process_id,
    is_process_id,
    process_is_alive,
    process_started_at,
)


class LegacyPid:
    def __int__(self):
        return 123


class ProcessInspectionTests(unittest.TestCase):
    def test_portable_and_native_pid_rules_reject_coercion_and_preserve_platform_bounds(
        self,
    ):
        for value in (1, 0x7FFFFFFF, 0xFFFFFFFF):
            self.assertTrue(is_process_id(value))
        for value in (True, False, 0, -1, 0x100000000, 42.0, "42", None, LegacyPid()):
            with self.subTest(value=value):
                self.assertFalse(is_process_id(value))
        for platform, maximum in (("darwin", 0x7FFFFFFF), ("win32", 0xFFFFFFFF)):
            with self.subTest(platform=platform), patch("sys.platform", platform):
                self.assertTrue(is_native_process_id(maximum))
                self.assertFalse(is_native_process_id(maximum + 1))
                self.assertFalse(is_native_process_id(True))

    def test_invalid_native_pids_never_reach_any_process_lookup(self):
        cases = [
            (platform, value)
            for platform, maximum in (("darwin", 0x7FFFFFFF), ("win32", 0xFFFFFFFF))
            for value in (
                True,
                False,
                0,
                -1,
                maximum + 1,
                10**100,
                float("nan"),
                float("inf"),
                float("-inf"),
                Decimal("Infinity"),
                "not-a-pid",
                b"nan",
                None,
                object(),
            )
        ]
        for platform, value in cases:
            with (
                self.subTest(platform=platform, value=value),
                patch("sys.platform", platform),
                patch.object(lease_module.os, "kill") as kill,
                patch.object(lease_module, "_inspect_windows_process") as windows,
                patch.object(lease_module.subprocess, "run") as ps,
                patch("psutil.Process") as psutil_process,
            ):
                self.assertEqual(inspect_process_status(value), "unknown")
                self.assertTrue(process_is_alive(value))
                self.assertIsNone(process_started_at(value))
                kill.assert_not_called()
                windows.assert_not_called()
                ps.assert_not_called()
                psutil_process.assert_not_called()

    def test_native_probes_keep_legacy_pid_normalization_and_maximum_values(self):
        cases = [
            (platform, value, expected)
            for platform, maximum in (("darwin", 0x7FFFFFFF), ("win32", 0xFFFFFFFF))
            for value, expected in (
                (1, 1),
                (maximum, maximum),
                (str(maximum), maximum),
                ("00123", 123),
                (b"123", 123),
                (bytearray(b"123"), 123),
                (123.9, 123),
                (Decimal("123.9"), 123),
                (LegacyPid(), 123),
            )
        ]
        for platform, value, expected in cases:
            with (
                self.subTest(platform=platform, value=value),
                patch("sys.platform", platform),
                patch.object(lease_module.os, "kill") as kill,
                patch.object(
                    lease_module, "_inspect_windows_process", return_value="live"
                ) as windows,
                patch.object(
                    lease_module.subprocess,
                    "run",
                    return_value=Mock(stdout="start identity\n"),
                ) as ps,
            ):
                self.assertEqual(inspect_process_status(value), "live")
                self.assertEqual(process_started_at(value), "start identity")
                self.assertEqual(
                    kill.call_args_list,
                    {"darwin": [call(expected, 0)], "win32": []}[platform],
                )
                self.assertEqual(
                    windows.call_args_list,
                    {"darwin": [], "win32": [call(expected)]}[platform],
                )
                self.assertEqual(
                    ps.call_args.args[0], ("ps", "-o", "lstart=", "-p", str(expected))
                )

    def test_malformed_generation_pid_blocks_without_recovery_or_lookup(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            path = output / ".generation-lease.json"
            guard = path.with_suffix(".guard")
            guard.touch()
            for pid in (True, False, 0, -1, 42.0, "42", None, 0x100000000, 10**100):
                payload = (
                    json.dumps(
                        {
                            "schema": lease_module.LEASE_SCHEMA,
                            "schema_version": lease_module.LEASE_VERSION,
                            "queue_sha256": "1" * 64,
                            "pid": pid,
                        },
                        indent=2,
                    ).encode()
                    + b"\n"
                )
                path.write_bytes(payload)
                checker = Mock(return_value=False)
                lease = GenerationLease(output, "1" * 64, process_checker=checker)
                with (
                    self.subTest(pid=pid),
                    patch.object(lease_module, "process_started_at") as started,
                    self.assertRaisesRegex(
                        BulkGenerationError, "Unrecognized generation lease"
                    ),
                ):
                    lease.__enter__()
                checker.assert_not_called()
                started.assert_not_called()
                self.assertIsNone(lease.document)
                self.assertEqual(path.read_bytes(), payload)
                self.assertEqual(set(output.iterdir()), {path, guard})

    def test_foreign_windows_dword_generation_lease_stays_recognized_and_blocks(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            path = output / ".generation-lease.json"
            payload = json.dumps(
                {
                    "schema": lease_module.LEASE_SCHEMA,
                    "schema_version": lease_module.LEASE_VERSION,
                    "queue_sha256": "1" * 64,
                    "pid": 0xFFFFFFFF,
                    "hostname": "foreign-" + socket.gethostname(),
                }
            ).encode()
            path.write_bytes(payload)
            checker = Mock(return_value=False)
            lease = GenerationLease(output, "1" * 64, process_checker=checker)
            with (
                patch("sys.platform", "darwin"),
                patch.object(lease_module, "process_started_at") as started,
                self.assertRaisesRegex(
                    BulkGenerationError, "Another generation process is active"
                ),
            ):
                lease.__enter__()
            checker.assert_not_called()
            started.assert_not_called()
            self.assertEqual(path.read_bytes(), payload)
            self.assertFalse((output / "interrupted").exists())

    def test_windows_start_identity_survives_missing_ps(self):
        with (
            patch("vntts.authoring.generation_lease.sys.platform", "win32"),
            patch(
                "vntts.authoring.generation_lease.subprocess.run",
                side_effect=FileNotFoundError,
            ),
            patch("psutil.Process") as process,
        ):
            process.return_value.create_time.return_value = 1720000000.125
            self.assertEqual(process_started_at(123), "psutil:1720000000.125")
            process.return_value.create_time.side_effect = psutil.AccessDenied(pid=123)
            self.assertIsNone(process_started_at(123))

    def test_unix_probe_preserves_unknown_state(self):
        if sys.platform == "win32":
            self.skipTest("Unix signal behavior")
        with patch(
            "vntts.authoring.generation_lease.os.kill", side_effect=PermissionError
        ):
            self.assertEqual(inspect_process_status(123), "unknown")
            self.assertTrue(process_is_alive(123))

    def test_live_child_survives_repeated_liveness_probes(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _attempt in range(3):
                self.assertEqual(inspect_process_status(child.pid), "live")
                self.assertTrue(process_is_alive(child.pid))
                self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=10)

        self.assertEqual(inspect_process_status(child.pid), "dead")
        self.assertFalse(process_is_alive(child.pid))

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_existing_generation_lease_capture_refuses_fifo_and_recovers(self):
        with TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            checker = Mock(return_value=False)
            lease = GenerationLease(output, "1" * 64, process_checker=checker)
            payload = json.dumps(
                {
                    "schema": lease_module.LEASE_SCHEMA,
                    "schema_version": lease_module.LEASE_VERSION,
                    "queue_sha256": "1" * 64,
                    "pid": 999999,
                }
            ).encode()
            lease.path.write_bytes(payload)
            lease.path.chmod(0o444)
            guard = lease.path.with_suffix(".guard")
            guard.touch()
            guard_inode = guard.stat().st_ino
            original = lease_module.read_regular_bytes
            refused = []

            def refuse_capture(path):
                self.assertEqual(path, lease.path)
                gate: RegularFileGate = {"path": path, "armed": True}
                with (
                    swap_at_acquisition(self, gate),
                    self.assertRaisesRegex(OSError, "regular file") as caught,
                ):
                    original(path)
                refused.append(caught.exception)
                raise caught.exception

            with (
                patch.object(lease_module, "read_regular_bytes", refuse_capture),
                self.assertRaisesRegex(
                    BulkGenerationError, "Unable to read generation lease"
                ) as caught,
            ):
                lease.__enter__()

            self.assertEqual(len(refused), 1)
            self.assertIs(caught.exception.__cause__, refused[0])
            checker.assert_not_called()
            self.assertIsNone(lease.document)
            self.assertEqual(lease.path.read_bytes(), payload)
            self.assertEqual(stat.S_IMODE(lease.path.stat().st_mode), 0o444)
            self.assertEqual(set(output.iterdir()), {lease.path, guard})
            self.assertEqual(guard.stat().st_ino, guard_inode)
            with lease:
                self.assertEqual(json.loads(lease.path.read_bytes()), lease.document)
            checker.assert_called_once_with(999999)
            self.assertFalse(lease.path.exists())
            archived = list((output / "interrupted").iterdir())
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), payload)
            self.assertEqual(guard.stat().st_ino, guard_inode)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_interrupted_archive_captures_refuse_fifo_and_recover(self):
        for phase in ("source", "candidate", "dedup-final", "move-final"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                output = Path(directory).resolve()
                source = output / "artifact.json"
                payload = b"interrupted payload"
                source.write_bytes(payload)
                source.chmod(0o444)
                archive_root = output / "interrupted"
                archive_root.mkdir()
                candidate = archive_root / (
                    "artifact-json-"
                    + hashlib.sha256(payload).hexdigest()[:12]
                    + ".json"
                )
                if phase in {"candidate", "dedup-final"}:
                    candidate.write_bytes(
                        b"foreign archive" if phase == "candidate" else payload
                    )
                    candidate.chmod(0o444)
                selected = candidate if phase == "candidate" else source
                selected_call = 2 if phase in {"dedup-final", "move-final"} else 1
                before = {
                    path: path.read_bytes()
                    for path in output.rglob("*")
                    if path.is_file()
                }
                original = lease_module.read_regular_bytes
                calls, refused = [], []

                def refuse_capture(path):
                    if path != selected:
                        return original(path)
                    calls.append(path)
                    if len(calls) != selected_call:
                        return original(path)
                    gate: RegularFileGate = {"path": path, "armed": True}
                    with (
                        swap_at_acquisition(self, gate),
                        self.assertRaisesRegex(OSError, "regular file") as caught,
                    ):
                        original(path)
                    refused.append(caught.exception)
                    raise caught.exception

                with (
                    patch.object(lease_module, "read_regular_bytes", refuse_capture),
                    self.assertRaisesRegex(OSError, "regular file") as caught,
                ):
                    lease_module.archive_interrupted_artifact(
                        output, source, expected_payload=payload
                    )

                self.assertEqual(len(calls), selected_call)
                self.assertEqual(len(refused), 1)
                self.assertIs(caught.exception, refused[0])
                self.assertEqual(stat.S_IMODE(selected.stat().st_mode), 0o444)
                self.assertEqual(
                    {
                        path: path.read_bytes()
                        for path in output.rglob("*")
                        if path.is_file()
                    },
                    before,
                )
                recovered = lease_module.archive_interrupted_artifact(
                    output, source, expected_payload=payload
                )
                self.assertFalse(source.exists())
                self.assertEqual(recovered.read_bytes(), payload)
                self.assertEqual(stat.S_IMODE(recovered.stat().st_mode), 0o444)
                if phase == "candidate":
                    self.assertNotEqual(recovered, candidate)
                    self.assertEqual(candidate.read_bytes(), before[candidate])
                else:
                    self.assertEqual(recovered, candidate)
                self.assertEqual(
                    len(list(archive_root.iterdir())), 2 if phase == "candidate" else 1
                )

    def test_deep_generation_lease_blocks_output_without_reclaiming_it(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            lease_path = output / ".generation-lease.json"
            payload = b'{"value": [0]}'
            lease_path.write_bytes(payload)
            guard = lease_path.with_suffix(".guard")
            guard.touch()
            process_checker = Mock(return_value=False)
            lease = GenerationLease(output, "1" * 64, process_checker=process_checker)
            decoder_error = RecursionError("native parser limit")
            with (
                patch(
                    "vntts.json_types.json.loads", side_effect=decoder_error
                ) as decode,
                self.assertRaisesRegex(
                    BulkGenerationError, "Unable to read generation lease .*nesting"
                ) as caught,
            ):
                lease.__enter__()
            decode.assert_called_once_with(payload.decode("utf-8"))
            cause = caught.exception.__cause__
            self.assertIsInstance(cause, json.JSONDecodeError)
            self.assertIs(cause.__cause__, decoder_error)
            process_checker.assert_not_called()
            self.assertIsNone(lease.document)
            self.assertEqual(lease_path.read_bytes(), payload)
            self.assertEqual(set(output.iterdir()), {lease_path, guard})

    def test_generation_lease_schema_version_requires_exact_integer(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            lease_path = output / ".generation-lease.json"
            for version in (True, 1.0):
                lease_path.write_bytes(
                    json.dumps(
                        {
                            "schema": "vntts.authoring-generation-lease",
                            "schema_version": version,
                            "queue_sha256": "1" * 64,
                            "pid": 123,
                        }
                    ).encode()
                )
                before = lease_path.read_bytes()
                lease = GenerationLease(
                    output, "1" * 64, process_checker=lambda _pid: False
                )
                with (
                    self.subTest(version=version),
                    self.assertRaisesRegex(
                        BulkGenerationError, "Unrecognized generation lease"
                    ),
                ):
                    lease.__enter__()
                self.assertEqual(lease_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
