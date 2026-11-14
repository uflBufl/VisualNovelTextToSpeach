import ctypes
import os
import subprocess
import sys
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts.worker_stdio import restore_worker_stdio


@contextmanager
def inherited_windows_pipe(*, descriptor=None):
    kernel32 = SimpleNamespace(
        GetStdHandle=Mock(return_value=2**40),
        GetFileType=Mock(return_value=3),
    )
    native = SimpleNamespace(
        GetCurrentProcess=Mock(return_value=-1),
        DuplicateHandle=Mock(return_value=2**41),
        CloseHandle=Mock(),
        DUPLICATE_SAME_ACCESS=2,
    )
    runtime = SimpleNamespace(open_osfhandle=Mock(return_value=descriptor))
    with ExitStack() as stack:
        stack.enter_context(patch.object(sys, "platform", "win32"))
        stack.enter_context(patch.object(sys, "stdout", None))
        stack.enter_context(
            patch.object(ctypes, "WinDLL", return_value=kernel32, create=True)
        )
        stack.enter_context(
            patch.dict(sys.modules, {"_winapi": native, "msvcrt": runtime})
        )
        for name in ("O_BINARY", "O_NOINHERIT"):
            stack.enter_context(
                patch.object(os, name, getattr(os, name, 0), create=True)
            )
        yield kernel32, native, runtime


class WorkerStdioTest(unittest.TestCase):
    def test_other_platforms_and_existing_streams_do_not_open_handles(self):
        for platform, missing in (("linux", True), ("darwin", True), ("win32", False)):
            with (
                self.subTest(platform=platform, missing=missing),
                patch.object(sys, "platform", platform),
                patch.object(sys, "stdout", None if missing else sys.stdout),
                patch.object(ctypes, "WinDLL", create=True) as load_library,
            ):
                restore_worker_stdio()
                load_library.assert_not_called()

    def test_missing_invalid_and_nonpipe_handles_remain_unavailable(self):
        for handle, kind in ((None, 3), (ctypes.c_void_p(-1).value, 3), (2**40, 1)):
            with (
                self.subTest(handle=handle, kind=kind),
                inherited_windows_pipe() as handles,
            ):
                kernel32, native, runtime = handles
                kernel32.GetStdHandle.return_value = handle
                kernel32.GetFileType.return_value = kind
                restore_worker_stdio()
                self.assertIsNone(sys.stdout)
                native.DuplicateHandle.assert_not_called()
                runtime.open_osfhandle.assert_not_called()

    def test_restored_stream_owns_duplicate_descriptor_and_keeps_borrowed_pipe_open(
        self,
    ):
        read_fd, write_fd = os.pipe()
        duplicate_fd = os.dup(write_fd)
        try:
            stderr = sys.stderr
            with inherited_windows_pipe(descriptor=duplicate_fd) as handles:
                kernel32, native, runtime = handles
                restore_worker_stdio()
                self.assertIs(sys.stderr, stderr)
                stream = sys.stdout
                try:
                    print("progress event")
                    self.assertEqual(
                        os.read(read_fd, 256), b"progress event" + os.linesep.encode()
                    )
                finally:
                    stream.close()
                native.CloseHandle.assert_not_called()
                self.assertEqual(runtime.open_osfhandle.call_args.args[0], 2**41)
                self.assertFalse(native.DuplicateHandle.call_args.args[4])
                self.assertIs(kernel32.GetStdHandle.restype, ctypes.c_void_p)
            with self.assertRaises(OSError):
                os.fstat(duplicate_fd)
            os.write(write_fd, b"borrowed handle still open")
            self.assertEqual(os.read(read_fd, 256), b"borrowed handle still open")
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_failed_descriptor_transfer_closes_only_duplicate_and_preserves_error(self):
        failure = OSError("descriptor conversion failed")
        with inherited_windows_pipe() as handles:
            _kernel32, native, runtime = handles
            runtime.open_osfhandle.side_effect = failure
            native.CloseHandle.side_effect = OSError("cleanup failed")
            with self.assertLogs("vntts.cleanup"), self.assertRaises(OSError) as raised:
                restore_worker_stdio()
            self.assertIs(raised.exception, failure)
            native.CloseHandle.assert_called_once_with(2**41)
            self.assertIn("cleanup failed", failure.__notes__[0])
            self.assertIsNone(sys.stdout)

    def test_failed_text_stream_creation_closes_transferred_descriptor(self):
        read_fd, write_fd = os.pipe()
        duplicate_fd = os.dup(write_fd)
        failure = RuntimeError("text stream failed")
        try:
            with (
                inherited_windows_pipe(descriptor=duplicate_fd) as handles,
                patch("vntts.worker_stdio.os.fdopen", side_effect=failure),
                self.assertRaises(RuntimeError) as raised,
            ):
                restore_worker_stdio()
            self.assertIs(raised.exception, failure)
            handles[1].CloseHandle.assert_not_called()
            with self.assertRaises(OSError):
                os.fstat(duplicate_fd)
            os.write(write_fd, b"borrowed")
            self.assertEqual(os.read(read_fd, 256), b"borrowed")
        finally:
            os.close(read_fd)
            os.close(write_fd)

    @unittest.skipUnless(sys.platform == "win32", "requires inherited Windows handles")
    def test_windows_child_restores_both_redirected_streams(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        interpreter = str(pythonw) if pythonw.is_file() else sys.executable
        script = (
            "import sys; from vntts.worker_stdio import restore_worker_stdio; "
            "sys.stdout=sys.stderr=None; restore_worker_stdio(); "
            "print('R1999_PROGRESS {}',flush=True); "
            "print('worker diagnostic',file=sys.stderr,flush=True)"
        )
        result = subprocess.run(
            (interpreter, "-c", script), capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "R1999_PROGRESS {}\n")
        self.assertEqual(result.stderr, "worker diagnostic\n")


if __name__ == "__main__":
    unittest.main()
