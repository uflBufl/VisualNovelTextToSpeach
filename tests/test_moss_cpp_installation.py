import hashlib
import io
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import BadZipFile, ZipFile

from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from tests.symlink_support import symlink_or_skip
from vntts import moss_cpp_installation as setup
from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError


class Response(io.BytesIO):
    status = 200

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers: dict[str, str] = {}


def archive_bytes(entries):
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return stream.getvalue()


class MossCppInstallationTest(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(setup.os, "mkfifo") and hasattr(setup.os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_runtime_zip_refuses_fifo_before_publication_and_recovers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "runtime.zip"
            archive.write_bytes(archive_bytes({"moss-tts-server.exe": b"new exe"}))
            destination = root / "installed"
            destination.mkdir()
            server = destination / "moss-tts-server.exe"
            server.write_bytes(b"prior exe")
            gate: RegularFileGate = {"path": archive, "armed": True}
            with (
                swap_at_acquisition(self, gate),
                self.assertRaisesRegex(OSError, "not a regular file"),
            ):
                setup._extract_runtime(archive, destination, None)
            self.assertEqual(server.read_bytes(), b"prior exe")
            self.assertEqual(list(destination.iterdir()), [server])
            setup._extract_runtime(archive, destination, None)
            self.assertEqual(server.read_bytes(), b"new exe")

    def test_runtime_zip_keeps_source_open_through_decode_and_closes_on_failure(self):
        for valid in (False, True):
            with self.subTest(valid=valid), TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "runtime.zip"
                archive.write_bytes(
                    archive_bytes({"moss-tts-server.exe": b"exe"})
                    if valid
                    else b"invalid"
                )
                destination = root / "installed"
                streams = []
                opened = setup.open_regular_binary
                zip_file = setup.ZipFile

                @contextmanager
                def admitted(path):
                    with opened(path) as stream:
                        streams.append(stream)
                        yield stream

                def decoded(stream):
                    self.assertIs(stream, streams[-1])
                    self.assertFalse(stream.closed)
                    return zip_file(stream)

                with (
                    patch.object(setup, "open_regular_binary", admitted),
                    patch.object(setup, "ZipFile", side_effect=decoded),
                ):
                    if valid:
                        setup._extract_runtime(archive, destination, None)
                        self.assertEqual(
                            (destination / "moss-tts-server.exe").read_bytes(), b"exe"
                        )
                    else:
                        with self.assertRaises(BadZipFile):
                            setup._extract_runtime(archive, destination, None)
                        self.assertFalse(destination.exists())
                self.assertEqual(len(streams), 1)
                self.assertTrue(streams[0].closed)

    def test_first_install_reuse_and_corruption_repair(self):
        runtime = archive_bytes({"moss-tts-server.exe": b"exe", "ggml-cpu.dll": b"dll"})
        model, sidecar = b"GGUF model", b"GGUF codec"
        models = [
            (setup.MODEL_NAME, hashlib.sha256(model).hexdigest(), len(model)),
            (setup.MODELS[1][0], hashlib.sha256(sidecar).hexdigest(), len(sidecar)),
        ]
        requests = []

        def open_url(request, **kwargs):
            requests.append(request.full_url)
            data = (
                runtime
                if request.full_url == "https://example.test/runtime"
                else sidecar
                if ".extras.gguf" in request.full_url
                else model
            )
            return Response(data)

        with (
            TemporaryDirectory() as directory,
            patch.dict(setup.os.environ, {}, clear=True),
            patch.object(setup.sys, "platform", "win32"),
            patch.object(setup.platform, "machine", return_value="AMD64"),
            patch.object(
                setup,
                "ARCHIVE",
                (
                    "https://example.test/runtime",
                    hashlib.sha256(runtime).hexdigest(),
                    len(runtime),
                ),
            ),
            patch.object(setup, "MODELS", models),
            patch.object(setup, "urlopen", side_effect=open_url),
            patch.object(setup, "run_runtime_command") as probe,
        ):
            root = Path(directory)
            paths = setup.ensure_moss_cpp(root=root, allow_download=True)
            self.assertEqual([p.read_bytes() for p in paths], [b"exe", model, sidecar])
            self.assertEqual((paths[0].parent / "ggml-cpu.dll").read_bytes(), b"dll")
            self.assertEqual(len(requests), 3)
            setup.ensure_moss_cpp(root=root)
            self.assertEqual(len(requests), 3)
            paths[1].write_bytes(b"x" * len(model))
            (paths[0].parent / "ggml-cpu.dll").write_bytes(b"bad")
            setup.ensure_moss_cpp(root=root, allow_download=True)
            self.assertEqual(paths[1].read_bytes(), model)
            self.assertEqual((paths[0].parent / "ggml-cpu.dll").read_bytes(), b"dll")
            self.assertEqual(len(requests), 4)
            self.assertEqual(probe.call_count, 3)
            probe.assert_called_with(
                [str(paths[0]), "--help"], cancellation=None, timeout=30
            )

    @unittest.skipUnless(
        hasattr(setup.os, "mkfifo") and hasattr(setup.os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_hash_refuses_fifo_swap_and_recovers(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "model.gguf"
            path.write_bytes(b"regular seed")
            gate: RegularFileGate = {"path": path, "armed": True}
            with (
                swap_at_acquisition(self, gate),
                self.assertRaises(OSError),
            ):
                setup._hash(path, None)
            self.assertFalse(gate["armed"], "regular file must be swapped at open")
            self.assertEqual(
                setup._hash(path, None), hashlib.sha256(b"regular seed").hexdigest()
            )

    def test_hash_cancellation_stops_after_one_megabyte_chunk(self):
        cancellation = Event()
        data = b"A" * (2 * 1024 * 1024)
        original_admit = setup.open_regular_binary
        reads = []

        class CancellingStream:
            def __init__(self, stream):
                self.stream = stream

            def read(self, size):
                reads.append(size)
                chunk = self.stream.read(size)
                cancellation.set()
                return chunk

        @contextmanager
        def admit(path):
            with original_admit(path) as stream:
                yield CancellingStream(stream)

        with TemporaryDirectory() as directory:
            path = Path(directory) / "model.gguf"
            path.write_bytes(data)
            with patch.object(setup, "open_regular_binary", side_effect=admit):
                with self.assertRaises(TTSSynthesisError):
                    setup._hash(path, cancellation)
        self.assertEqual(reads, [1024 * 1024])

    def test_cancelled_download_resumes_and_verifies_before_publication(self):
        data = b"A" * (2 * 1024 * 1024)
        cancellation = Event()

        class Interrupting(Response):
            def read(self, size=-1):
                result = super().read(size)
                cancellation.set()
                return result

        with TemporaryDirectory() as directory:
            output = Path(directory) / "model.gguf"
            args = (
                "https://example.test/model",
                hashlib.sha256(data).hexdigest(),
                len(data),
                output,
                lambda _: None,
                cancellation,
            )
            with patch.object(setup, "urlopen", return_value=Interrupting(data)):
                with self.assertRaises(TTSSynthesisError):
                    setup._download(*args, allow_download=True)
            self.assertFalse(output.exists())
            partial = output.with_suffix(".gguf.part")
            start = partial.stat().st_size
            cancellation.clear()
            response = Response(data[start:])
            response.status = 206
            response.headers = {
                "Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"
            }
            with patch.object(setup, "urlopen", return_value=response) as opened:
                setup._download(*args, allow_download=True)
            self.assertEqual(
                opened.call_args.args[0].get_header("Range"), f"bytes={start}-"
            )
            self.assertEqual(output.read_bytes(), data)
            self.assertFalse(partial.exists())

    def test_bad_checksum_and_unsafe_archive_are_not_published(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "download"
            with patch.object(setup, "urlopen", return_value=Response(b"bad")):
                with self.assertRaisesRegex(TTSConfigurationError, "checksum"):
                    setup._download(
                        "https://example.test",
                        "0" * 64,
                        3,
                        output,
                        lambda _: None,
                        None,
                        allow_download=True,
                    )
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".part").exists())
            archive = root / "runtime.zip"
            archive.write_bytes(archive_bytes({"../escape.dll": b"bad"}))
            with self.assertRaisesRegex(TTSConfigurationError, "Unsafe"):
                setup._extract_runtime(archive, root / "installed", None)
            self.assertFalse((root / "escape.dll").exists())

    def test_explicit_paths_never_download(self):
        with (
            patch.dict(
                setup.os.environ,
                {
                    "VNTTS_MOSS_CPP_EXECUTABLE": "custom.exe",
                    "VNTTS_MOSS_GGUF": "custom.gguf",
                },
            ),
            patch.object(setup, "urlopen") as download,
        ):
            paths = setup.ensure_moss_cpp()
        self.assertEqual(paths[0], Path("custom.exe"))
        download.assert_not_called()

    def test_managed_install_requires_explicit_download_approval(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict(setup.os.environ, {}, clear=True),
            patch.object(setup.sys, "platform", "win32"),
            patch.object(setup.platform, "machine", return_value="AMD64"),
            patch.object(setup, "urlopen") as download,
            self.assertRaises(setup.MossCppInstallRequired) as raised,
        ):
            setup.ensure_moss_cpp(root=Path(directory))

        expected = setup.ARCHIVE[2] + sum(size for _name, _digest, size in setup.MODELS)
        self.assertEqual(raised.exception.download_space[0], expected)
        download.assert_not_called()

    def test_managed_install_rejects_aliased_root(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            outside = base / "outside"
            outside.mkdir()
            root = base / "managed"
            symlink_or_skip(root, outside, target_is_directory=True)
            with (
                patch.dict(setup.os.environ, {}, clear=True),
                patch.object(setup.sys, "platform", "win32"),
                patch.object(setup.platform, "machine", return_value="AMD64"),
                self.assertRaisesRegex(TTSConfigurationError, "alias"),
            ):
                setup.ensure_moss_cpp(root=root)

    def test_space_check_includes_partial_download_and_working_space(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict(setup.os.environ, {}, clear=True),
            patch.object(setup, "ARCHIVE", ("runtime", "digest", 5)),
            patch.object(
                setup,
                "MODELS",
                ((setup.MODEL_NAME, "digest", 10), ("codec.extras.gguf", "digest", 20)),
            ),
            patch.object(
                setup.shutil, "disk_usage", return_value=SimpleNamespace(free=100)
            ),
        ):
            root = Path(directory)
            model = setup.configured_paths(root=root)[1]
            model.parent.mkdir(parents=True)
            model.with_suffix(model.suffix + ".part").write_bytes(b"1234")

            remaining, required, free = setup.managed_download_space(root=root)

        self.assertEqual(remaining, 31)
        self.assertEqual(required, 31 + setup.DOWNLOAD_HEADROOM_BYTES)
        self.assertEqual(free, 100)

    def test_invalid_resume_range_preserves_partial(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "model.gguf"
            partial = output.with_suffix(".gguf.part")
            partial.write_bytes(b"a")
            response = Response(b"bc")
            response.status = 206
            response.headers = {"Content-Range": "bytes 0-1/3"}
            with (
                patch.object(setup, "urlopen", return_value=response),
                self.assertRaisesRegex(TTSConfigurationError, "resume range"),
            ):
                setup._download(
                    "https://example.test",
                    "0" * 64,
                    3,
                    output,
                    lambda _: None,
                    None,
                    allow_download=True,
                )
            self.assertEqual(partial.read_bytes(), b"a")
            self.assertFalse(output.exists())

    def test_native_probe_failure_prevents_model_download(self):
        def publish_download(_url, _digest, _size, output, *_args, **_kwargs):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"x")

        with (
            TemporaryDirectory() as directory,
            patch.dict(setup.os.environ, {}, clear=True),
            patch.object(setup.sys, "platform", "win32"),
            patch.object(setup.platform, "machine", return_value="AMD64"),
            patch.object(setup, "managed_download_space", return_value=(1, 1, 2)),
            patch.object(setup, "_download", side_effect=publish_download) as download,
            patch.object(setup, "_extract_runtime"),
            patch.object(
                setup,
                "run_runtime_command",
                side_effect=TTSConfigurationError("missing DLL"),
            ),
            self.assertRaisesRegex(TTSConfigurationError, "missing DLL") as raised,
        ):
            setup.ensure_moss_cpp(root=Path(directory), allow_download=True)
        self.assertEqual(download.call_count, 1)
        self.assertNotIn("Redistributable", str(raised.exception))

    def test_failed_managed_runtime_probe_requires_approval_then_repairs(self):
        runtime = archive_bytes({"moss-tts-server.exe": b"repaired"})
        model = Path("custom.gguf")
        with (
            TemporaryDirectory() as directory,
            patch.dict(setup.os.environ, {"VNTTS_MOSS_GGUF": str(model)}, clear=True),
            patch.object(setup.sys, "platform", "win32"),
            patch.object(setup.platform, "machine", return_value="AMD64"),
            patch.object(
                setup,
                "ARCHIVE",
                (
                    "https://example.test/runtime",
                    hashlib.sha256(runtime).hexdigest(),
                    len(runtime),
                ),
            ),
            patch.object(setup, "urlopen", return_value=Response(runtime)) as download,
            patch.object(
                setup,
                "run_runtime_command",
                side_effect=[
                    TTSConfigurationError("missing DLL"),
                    TTSConfigurationError("missing DLL"),
                    None,
                ],
            ) as probe,
        ):
            root = Path(directory)
            server = setup.configured_paths(root=root)[0]
            server.parent.mkdir(parents=True)
            server.write_bytes(b"broken")
            with self.assertRaises(setup.MossCppInstallRequired) as raised:
                setup.ensure_moss_cpp(root=root)
            self.assertEqual(raised.exception.download_space[0], len(runtime))
            download.assert_not_called()

            setup.ensure_moss_cpp(root=root, allow_download=True)

            self.assertEqual(server.read_bytes(), b"repaired")
            self.assertEqual(download.call_count, 1)
            self.assertEqual(probe.call_count, 3)
