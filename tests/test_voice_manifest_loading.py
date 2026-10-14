import builtins
import hashlib
import io
import json
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.voice_manifest import load_voice_manifest as dependency_loader

from tests.source_reference_fixtures import write_experimental_composite_voice_fixture
from tests.symlink_support import symlink_or_skip
from vntts.authoring import experimental_composite_voice, workspace_inspection
from vntts.authoring.bulk_generation import BulkGenerationError
from vntts.authoring.cli_generation import _load_stable_voice_registry
from vntts.authoring.experimental_composite_voice import (
    ExperimentalCompositeVoiceError,
    publish_experimental_composite_voice_input,
)
from vntts.voices import CharacterVoiceRegistry, VoiceManifestError, load_voice_manifest


class VoiceManifestLoadingTest(unittest.TestCase):
    @contextmanager
    def _swapped_file(self, path):
        payload = path.read_bytes()
        native_open, plain_open, text_open, path_open = (
            os.open,
            builtins.open,
            io.open,
            Path.open,
        )
        descriptors = []

        def swap(candidate, flags, mode=0o777, *, dir_fd=None):
            if Path(candidate) == path:
                self.assertTrue(flags & os.O_NONBLOCK)
                path.unlink()
                os.mkfifo(path)
            descriptor = native_open(candidate, flags, mode, dir_fd=dir_fd)
            if Path(candidate) == path:
                descriptors.append(descriptor)
            return descriptor

        def guarded_open(original, candidate, *args, **kwargs):
            if isinstance(candidate, (str, os.PathLike)) and Path(candidate) == path:
                self.assertIsNotNone(kwargs.get("opener"), "blocking input open")
            return original(candidate, *args, **kwargs)

        def guarded_path_open(candidate, *args, **kwargs):
            self.assertNotEqual(Path(candidate), path, "blocking input Path.open")
            return path_open(candidate, *args, **kwargs)

        try:
            with (
                patch("vntts.path_safety.os.open", side_effect=swap),
                patch(
                    "builtins.open",
                    side_effect=lambda *a, **k: guarded_open(plain_open, *a, **k),
                ),
                patch(
                    "io.open",
                    side_effect=lambda *a, **k: guarded_open(text_open, *a, **k),
                ),
                patch.object(Path, "open", guarded_path_open),
            ):
                yield
        finally:
            try:
                self.assertEqual(len(descriptors), 1)
                with self.assertRaises(OSError):
                    os.fstat(descriptors[0])
            finally:
                path.unlink(missing_ok=True)
                path.write_bytes(payload)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_loader_refuses_fifo_before_decoding_and_recovers(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "manifest.json"
            path.write_text('{"version":2,"voices":[]}', encoding="utf-8")
            with self._swapped_file(path):
                with self.assertRaisesRegex(
                    VoiceManifestError, "regular file"
                ) as raised:
                    load_voice_manifest(path)
                self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual(load_voice_manifest(path), dependency_loader(path))

    def test_dependency_parity_for_documents_and_error_causes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "manifest.json"
            document = {
                "version": 2,
                "unknown": {"retained": [1, None]},
                "voices": [
                    {
                        "character": "Name",
                        "speaker": "voice",
                        "aliases": [" Alias "],
                        "reference": "missing.wav",
                        "vntts.source_character": 42,
                    }
                ],
            }
            legacy = dict(document)
            legacy.pop("version")
            cases = [
                (json.dumps(document).encode(), True),
                (json.dumps(legacy).encode(), True),
                (json.dumps(legacy).encode(), False),
                (b'{\r\n"version":2,\r\n"voices":[]\r\n}', False),
                (b"[]", True),
                (b'{"version":true,"voices":[]}', True),
                (b"{}", True),
                (b"{", True),
                (b'\xef\xbb\xbf{"voices":[]}', True),
                (b"\xff", True),
            ]
            for reference in (
                "../outside.wav",
                "/absolute.wav",
                "back\\slash.wav",
                "bad\x00.wav",
            ):
                invalid = dict(
                    document, voices=[dict(document["voices"][0], reference=reference)]
                )
                cases.append((json.dumps(invalid).encode(), False))
            for index, (payload, allow_legacy) in enumerate(cases):
                with self.subTest(case=index):
                    path.write_bytes(payload)
                    outcomes = []
                    for loader in (dependency_loader, load_voice_manifest):
                        try:
                            outcomes.append(
                                ("success", loader(path, allow_legacy=allow_legacy))
                            )
                        except Exception as error:
                            cause = error.__cause__
                            outcomes.append(
                                (type(error), str(error), type(cause), str(cause))
                            )
                    self.assertEqual(outcomes[0], outcomes[1])
            path.unlink()
            for loader in (dependency_loader, load_voice_manifest):
                with self.assertRaises(VoiceManifestError) as raised:
                    loader(path)
                self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)

    def test_direct_loader_rejects_manifest_alias_but_registry_keeps_it(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, alias = root / "manifest.json", root / "alias.json"
            source.write_text('{"voices":[]}', encoding="utf-8")
            symlink_or_skip(alias, source)
            for loader in (dependency_loader, load_voice_manifest):
                with self.assertRaisesRegex(VoiceManifestError, "cannot be a symlink"):
                    loader(alias)
            self.assertEqual(
                CharacterVoiceRegistry.from_file(alias).unique_voices(), ()
            )
            source.write_text(
                '{"voices":[{"character":"A","speaker":"a","reference":"linked.wav"}]}',
                encoding="utf-8",
            )
            target = root / "outside.wav"
            target.write_bytes(b"audio")
            symlink_or_skip(root / "linked.wav", target)
            for loader in (dependency_loader, load_voice_manifest):
                with self.assertRaisesRegex(
                    VoiceManifestError, "Voice entry 0 reference must not use symlinks"
                ):
                    loader(source)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_cli_capture_refuses_before_staging_and_recovers(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "manifest.json"
            payload = b'{"voices":[{"character":"A","speaker":"a"}]}'
            path.write_bytes(payload)
            with self._swapped_file(path):
                with self.assertRaisesRegex(
                    BulkGenerationError, "regular file"
                ) as raised:
                    _load_stable_voice_registry(path)
                self.assertIsInstance(raised.exception.__cause__, OSError)
            registry, digest, document, entries = _load_stable_voice_registry(path)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            self.assertEqual(document, json.loads(payload))
            self.assertEqual(registry.resolve("A").speaker, entries[0].speaker)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_experimental_capture_and_bundle_read_refuse_and_recover(self):
        for phase in ("source", "bundle"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                manifest, composite, review, _ = (
                    write_experimental_composite_voice_fixture(root)
                )
                output = root / "output"
                arguments = (manifest, composite, review, "Experimental", output)
                if phase == "source":
                    with self._swapped_file(manifest):
                        with self.assertRaisesRegex(
                            ExperimentalCompositeVoiceError, "regular file"
                        ):
                            publish_experimental_composite_voice_input(*arguments)
                    self.assertFalse(output.exists())
                    publish_experimental_composite_voice_input(*arguments)
                else:
                    result = publish_experimental_composite_voice_input(*arguments)
                    original = experimental_composite_voice._validate_bundle_manifest

                    def refuse_capture(directory, authority):
                        with self._swapped_file(directory / "manifest.json"):
                            return original(directory, authority)

                    with patch.object(
                        experimental_composite_voice,
                        "_validate_bundle_manifest",
                        side_effect=refuse_capture,
                    ):
                        with self.assertRaisesRegex(
                            ExperimentalCompositeVoiceError, "regular file"
                        ):
                            publish_experimental_composite_voice_input(*arguments)
                    recovered = publish_experimental_composite_voice_input(*arguments)
                    self.assertFalse(recovered.created)
                    self.assertEqual(result.bundle_id, recovered.bundle_id)
                self.assertEqual(list(root.glob(".experimental-composite-*")), [])
                self.assertEqual(
                    load_voice_manifest(output / "manifest.json"),
                    dependency_loader(output / "manifest.json"),
                )

    def test_readiness_projects_one_document_for_registry_and_overrides(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "manifest.json"
            document = {
                "version": 2,
                "voices": [{"character": "A", "speaker": "before"}],
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            captured = dict(
                document, voices=[dict(document["voices"][0], speaker="captured")]
            )

            def capture_current(*args, **kwargs):
                path.write_text(json.dumps(captured), encoding="utf-8")
                return load_voice_manifest(*args, **kwargs)

            with patch.object(
                workspace_inspection, "load_voice_manifest", side_effect=capture_current
            ):
                registry, overrides = workspace_inspection._load_voice_routing(path)
            self.assertEqual(registry.resolve("A").speaker, "captured")
            self.assertEqual(overrides, {})
