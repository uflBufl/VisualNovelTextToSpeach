import hashlib
import io
import json
import os
import shutil
import stat
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests import test_workspace_input_admission as admission
from tests.pregeneration_fixtures import ambiguous_fixture
from tests.symlink_support import symlink_or_skip
from vntts import (
    assets,
    chapter_voice_preload,
    game_audio_decoder,
    game_content_importer,
    ocr_benchmark,
    pregeneration_audition,
    pregeneration_audition_ui,
    pregeneration_setup,
    support,
    versioned_json,
    voice_candidate_cache,
)
from vntts.authoring import (
    authority,
    bulk_generation,
    cohort_bundle,
    failure_reference_audit,
    legacy_import,
    silence_comparison,
    silence_evidence,
    workspace_foundation,
)
from vntts.authoring.generation_manifest import (
    BulkGenerationError,
    safe_generation_relative_path,
)
from vntts.live_replay import _read_contained_file
from vntts.live_replay_sequence_seal import SequenceReplaySealError, _read_regular_file
from vntts.live_speaker_corpus import LiveSpeakerCorpus
from vntts.path_safety import (
    contained_regular_file,
    copy_regular_file,
    open_regular_text,
    read_regular_bytes,
    read_regular_text,
    safe_relative_path,
)
from vntts.runtime_ownership import read_record
from vntts.voice_library import VoiceLibrary, VoiceLibraryError
from vntts.voices import VoiceManifestError, _read_owned_voice_reference


class PathBoundaryError(RuntimeError):
    pass


class PathSafetyTest(unittest.TestCase):
    def test_generation_relative_path_rejects_nul_with_its_domain_error(self):
        self.assertEqual(
            safe_generation_relative_path("./nested//voice.wav", "Audio"),
            Path("nested/voice.wav"),
        )
        for value in ("\x00", "nested/bad\x00.wav"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(BulkGenerationError, "relative POSIX path"),
            ):
                safe_generation_relative_path(value, "Audio")

    def test_embedded_nul_uses_requested_error_boundary(self):
        with TemporaryDirectory() as directory:
            for value in ("\x00", "nested/bad\x00.json", Path("bad\x00.json")):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(PathBoundaryError, "POSIX-relative"):
                        safe_relative_path(
                            str(value), "artifact", error_type=PathBoundaryError
                        )
                    with self.assertRaisesRegex(PathBoundaryError, "leaves its root"):
                        contained_regular_file(
                            directory, value, "artifact", error_type=PathBoundaryError
                        )


class RegularFileAdmissionTest(unittest.TestCase):
    def test_text_capture_matches_encoding_newlines_and_one_acquisition(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.txt"
            for encoding, payload in (
                (None, b"default\r\nsecond\rthird\n"),
                ("utf-8", "Русский café\r\nsecond\rthird\n".encode("utf-8")),
                ("utf-8-sig", b"\xef\xbb\xbftext\r\nsecond\rthird\n"),
                ("ascii", b"ASCII\r\nsecond\rthird\n"),
            ):
                with self.subTest(encoding=encoding):
                    path.write_bytes(payload)
                    expected = path.read_text(encoding=encoding)
                    with patch("vntts.path_safety.os.open", wraps=os.open) as acquire:
                        self.assertEqual(read_regular_text(path, encoding), expected)
                    self.assertEqual(acquire.call_count, 1)
                    self.assertEqual(path.read_bytes(), payload)
            path.unlink()
            with self.assertRaises(FileNotFoundError) as missing:
                read_regular_text(path, "utf-8")
            self.assertEqual(missing.exception.filename, str(path))
            error = PermissionError(13, "text denied", str(path))
            with (
                patch("vntts.path_safety.os.open", side_effect=error),
                self.assertRaises(PermissionError) as caught,
            ):
                read_regular_text(path, "utf-8")
            self.assertIs(caught.exception, error)

    def test_text_decoding_failure_closes_wrapper_and_source(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.txt"
            path.write_bytes(b"invalid \xff text")
            texts = []

            def text_wrapper(*args, **kwargs):
                text = io.TextIOWrapper(*args, **kwargs)
                texts.append(text)
                return text

            with (
                patch("vntts.path_safety.TextIOWrapper", side_effect=text_wrapper),
                self.assertRaises(UnicodeDecodeError) as caught,
            ):
                read_regular_text(path, "utf-8")
            with self.assertRaises(UnicodeDecodeError) as expected:
                path.read_text(encoding="utf-8")
            self.assertEqual(str(caught.exception), str(expected.exception))
            self.assertEqual(len(texts), 1)
            self.assertTrue(texts[0].closed)
            self.assertTrue(texts[0].buffer.closed)

    def test_text_stream_preserves_first_line_and_closes_after_early_exit(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.txt"
            path.write_bytes(b"{}\r\n" + b"remaining record\n" * 2000)
            with open_regular_text(path, "utf-8") as text:
                self.assertEqual(next(text), "{}\n")
                self.assertEqual(text.readline(), "remaining record\n")
                self.assertFalse(text.closed)
            self.assertTrue(text.closed)
            self.assertTrue(text.buffer.closed)

    def test_text_capture_follows_regular_aliases(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.txt"
            path.write_bytes("text café\r\n".encode("utf-8"))
            alias = path.with_name("alias.txt")
            symlink_or_skip(alias, path)
            self.assertEqual(read_regular_text(alias, "utf-8"), "text café\n")
            self.assertTrue(alias.is_symlink())

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_text_capture_and_stream_refuse_swapped_fifo_and_recover(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.txt"
            path.write_bytes(b"text\r\nsecond\r")
            for role in ("capture", "stream"):
                with self.subTest(role=role):
                    gate = {"path": path, "armed": True}
                    with (
                        admission.WorkspaceInputAdmissionTest.swap_at_acquisition(
                            self, gate
                        ),
                        self.assertRaisesRegex(OSError, "regular file"),
                    ):
                        if role == "capture":
                            read_regular_text(path, "utf-8")
                        else:
                            with open_regular_text(path, "utf-8"):
                                self.fail("refused source must never be yielded")
                    self.assertEqual(read_regular_text(path, "utf-8"), "text\nsecond\n")

    def test_full_byte_capture_retains_raw_errors_and_one_acquisition(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            for payload in (b"", b"\x00\xff\r\n" * 300000):
                source.write_bytes(payload)
                with patch("vntts.path_safety.os.open", wraps=os.open) as acquire:
                    self.assertEqual(read_regular_bytes(str(source)), payload)
                self.assertEqual(acquire.call_count, 1)
                self.assertEqual(source.read_bytes(), payload)
            source.unlink()
            with self.assertRaises(FileNotFoundError) as missing:
                read_regular_bytes(source)
            self.assertEqual(missing.exception.filename, str(source))
            self.assertIsNone(missing.exception.__cause__)
            denied = PermissionError(13, "denied", str(source))
            with (
                patch("vntts.path_safety.os.open", side_effect=denied),
                self.assertRaises(PermissionError) as caught,
            ):
                read_regular_bytes(source)
            self.assertIs(caught.exception, denied)

    def test_full_byte_capture_follows_regular_aliases(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            payload = b"\x00\xff\r\nalias payload"
            source.write_bytes(payload)
            alias = source.with_name("alias")
            symlink_or_skip(alias, source)
            self.assertEqual(read_regular_bytes(alias), payload)
            self.assertTrue(alias.is_symlink())
            self.assertEqual(source.read_bytes(), payload)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_full_byte_capture_refuses_swapped_fifo_and_recovers(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            payload = b"\x00\xff\r\ncaptured payload"
            source.write_bytes(payload)
            source.chmod(0o444)
            mode = source.stat().st_mode
            gate = {"path": source, "armed": True}
            with (
                admission.WorkspaceInputAdmissionTest.swap_at_acquisition(self, gate),
                self.assertRaisesRegex(OSError, "regular file"),
            ):
                read_regular_bytes(source)
            self.assertEqual(read_regular_bytes(source), payload)
            self.assertEqual(source.stat().st_mode, mode)
            self.assertEqual(list(Path(directory).iterdir()), [source])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO admission")
    def test_readers_reject_regular_path_swapped_to_fifo_before_open(self):
        native_open = os.open
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            reference = root / "voice.wav"
            fifo = root / "replacement"
            readers = (
                lambda: VoiceLibrary(root / "library").discover("Role", reference),
                lambda: _read_owned_voice_reference(root, reference),
                lambda: LiveSpeakerCorpus.load(reference),
                lambda: _read_regular_file(reference, "Sequence plan"),
                lambda: _read_contained_file(root, reference.name, "Replay item"),
            )
            for index, read in enumerate(readers):
                with self.subTest(reader=index):
                    reference.unlink(missing_ok=True)
                    reference.write_bytes(b"regular file before acquisition")
                    os.mkfifo(fifo)
                    descriptors = []

                    def swap_and_open(path, flags):
                        self.assertEqual(Path(path), reference)
                        self.assertTrue(
                            flags & os.O_NONBLOCK,
                            "reader must not wait for a FIFO writer",
                        )
                        fifo.replace(reference)
                        descriptor = native_open(path, flags)
                        descriptors.append(descriptor)
                        return descriptor

                    with patch("vntts.path_safety.os.open", side_effect=swap_and_open):
                        with self.assertRaisesRegex(
                            (ValueError, VoiceManifestError, SequenceReplaySealError),
                            "regular file",
                        ):
                            read()
                    self.assertEqual(len(descriptors), 1)
                    with self.assertRaises(OSError):
                        os.fstat(descriptors[0])


class RegularFileCopyTest(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_refused_source_does_not_create_or_truncate_destination(self):
        for existing in (False, True):
            with self.subTest(existing=existing), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source, target = root / "source", root / "target"
                payload = b"\x00\xff\r\nreference"
                source.write_bytes(payload)
                if existing:
                    target.write_bytes(b"prior destination")
                gate = {"path": source, "armed": True}
                with (
                    admission.WorkspaceInputAdmissionTest.swap_at_acquisition(
                        self, gate
                    ),
                    self.assertRaisesRegex(OSError, "regular file"),
                ):
                    copy_regular_file(source, target)
                self.assertEqual(source.read_bytes(), payload)
                self.assertEqual(target.exists(), existing)
                if existing:
                    self.assertEqual(target.read_bytes(), b"prior destination")
                copy_regular_file(source, target)
                self.assertEqual(target.read_bytes(), payload)

    def test_same_file_and_aliases_are_not_truncated(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"original reference")
            alias = root / "hardlink"
            os.link(source, alias)
            for target in (source, alias):
                with (
                    self.subTest(target=target),
                    self.assertRaises(shutil.SameFileError),
                ):
                    copy_regular_file(source, target)
                self.assertEqual(source.read_bytes(), b"original reference")

    def test_binary_copy_preserves_metadata_only_when_requested(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            payload = b"\x00\xff\r\nreference"
            source.write_bytes(payload)
            source.chmod(0o640)
            os.utime(source, ns=(1234567890000000000,) * 2)
            target.write_bytes(b"prior destination")
            target.chmod(0o600)
            copy_regular_file(source, target)
            self.assertEqual(target.read_bytes(), payload)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertNotEqual(target.stat().st_mtime_ns, source.stat().st_mtime_ns)
            copy_regular_file(source, target, preserve_metadata=True)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(
                stat.S_IMODE(target.stat().st_mode),
                stat.S_IMODE(source.stat().st_mode),
            )
            self.assertEqual(target.stat().st_mtime_ns, source.stat().st_mtime_ns)


class TextDocumentAdmissionTest(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_complete_text_consumers_preserve_errors_fallbacks_and_retry(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "document.json"
            cases = (
                (
                    lambda: ocr_benchmark.load_expectations(path),
                    {"sample": "expected"},
                    OSError,
                    None,
                ),
                (
                    lambda: workspace_foundation.load_json_object(path, "Document"),
                    {"sample": "expected"},
                    ValueError,
                    None,
                ),
                (
                    lambda: failure_reference_audit._read_json(path),
                    ["raw JSON value"],
                    OSError,
                    None,
                ),
                (
                    lambda: legacy_import._load_json_optional(path),
                    {"sample": "expected"},
                    None,
                    {},
                ),
            )
            for read, document, error_type, fallback in cases:
                with self.subTest(reader=read):
                    path.write_text(json.dumps(document), encoding="utf-8")
                    gate = {"path": path, "armed": True}
                    with admission.WorkspaceInputAdmissionTest.swap_at_acquisition(
                        self, gate
                    ):
                        if error_type is not None:
                            with self.assertRaises(error_type) as caught:
                                read()
                            if error_type is ValueError:
                                self.assertIsInstance(
                                    caught.exception.__cause__, OSError
                                )
                        else:
                            self.assertEqual(read(), fallback)
                    self.assertEqual(read(), document)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_voice_library_text_index_refuses_swapped_fifo_before_cache(self):
        with TemporaryDirectory() as directory:
            library = VoiceLibrary(Path(directory) / "library")
            document = library._load()
            library.path.parent.mkdir(parents=True, exist_ok=True)
            library.path.write_text(json.dumps(document), encoding="utf-8")
            gate = {"path": library.path, "armed": True}
            with (
                admission.WorkspaceInputAdmissionTest.swap_at_acquisition(self, gate),
                self.assertRaises(VoiceLibraryError) as caught,
            ):
                library._load()
            self.assertIsInstance(caught.exception.__cause__, OSError)
            self.assertEqual(library._load(), document)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_first_line_readers_refuse_swapped_fifo_and_recover(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "story-index.jsonl"
            metadata = {"game": "Reverse: 1999"}
            path.write_text(
                json.dumps(metadata) + "\nnot JSON body\n", encoding="utf-8"
            )
            cases = (
                (
                    lambda: game_content_importer._read_story_metadata(path),
                    metadata,
                    OSError,
                    None,
                ),
                (
                    lambda: pregeneration_setup._is_outdated_reverse1999_index(path),
                    True,
                    None,
                    False,
                ),
                (
                    lambda: legacy_import._load_jsonl_metadata_optional(path),
                    metadata,
                    None,
                    {},
                ),
            )
            for read, expected, error_type, fallback in cases:
                with self.subTest(reader=read):
                    gate = {"path": path, "armed": True}
                    with admission.WorkspaceInputAdmissionTest.swap_at_acquisition(
                        self, gate
                    ):
                        if error_type is not None:
                            with self.assertRaises(error_type):
                                read()
                        else:
                            self.assertEqual(read(), fallback)
                    self.assertEqual(read(), expected)

    def test_metadata_and_extension_readers_keep_streaming(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "story-index.jsonl"
            metadata = {"game": "Reverse: 1999"}
            path.write_text(
                json.dumps(metadata)
                + '\n{"line_id": "line-1"}\n{"line_id": "line-2"}\n',
                encoding="utf-8",
            )
            extensions = chapter_voice_preload._load_source_audio_extensions(path)
            self.assertEqual(set(extensions), {"line-1", "line-2"})
            cases = (
                (game_content_importer, "_read_story_metadata", metadata, 1),
                (pregeneration_setup, "_is_outdated_reverse1999_index", True, 1),
                (legacy_import, "_load_jsonl_metadata_optional", metadata, 1),
                (chapter_voice_preload, "_load_source_audio_extensions", extensions, 4),
            )
            for module, name, expected, steps in cases:
                with self.subTest(reader=name):
                    consumed = []
                    sources = []

                    class LinesOnly:
                        def __iter__(self):
                            return self

                        def __next__(self):
                            consumed.append("next")
                            return next(sources[-1])

                        def readline(self):
                            consumed.append("readline")
                            return sources[-1].readline()

                        def read(self, *_args, **_kwargs):
                            raise AssertionError("JSONL reader must remain streaming")

                    @contextmanager
                    def stream(candidate, encoding=None):
                        with open_regular_text(candidate, encoding) as source:
                            sources.append(source)
                            yield LinesOnly()

                    with patch.object(module, "open_regular_text", stream):
                        self.assertEqual(getattr(module, name)(path), expected)
                    self.assertEqual(len(consumed), steps)
                    self.assertEqual(len(sources), 1)
                    self.assertTrue(sources[0].closed)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_source_audio_extensions_refuse_swapped_fifo_and_recover(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "story-index.jsonl"
            path.write_text('{}\n{"line_id": "line-1"}\n', encoding="utf-8")
            expected = chapter_voice_preload._load_source_audio_extensions(path)
            self.assertEqual(set(expected), {"line-1"})
            gate = {"path": path, "armed": True}
            with admission.WorkspaceInputAdmissionTest.swap_at_acquisition(self, gate):
                self.assertEqual(
                    chapter_voice_preload._load_source_audio_extensions(path), {}
                )
            self.assertEqual(
                chapter_voice_preload._load_source_audio_extensions(path), expected
            )


class BoundedDocumentAdmissionTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO admission")
    def test_bounded_readers_reject_regular_path_swapped_to_fifo_with_own_fallbacks(
        self,
    ):
        native_open, native_path_open = os.open, Path.open
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, _group, _manifest = ambiguous_fixture(root)
            reference = root / "verified.json"
            fifo = root / "replacement"
            default = object()
            readers = (
                ("runtime", lambda: read_record(reference), None, {}),
                (
                    "snapshot",
                    lambda: versioned_json.read_versioned_json_snapshot(
                        reference, schema_version=1, document_name="test"
                    ),
                    OSError,
                    None,
                ),
                (
                    "revision",
                    lambda: versioned_json.file_revision(reference),
                    OSError,
                    None,
                ),
                (
                    "voice manifest",
                    lambda: assets.read_json(reference, default),
                    None,
                    default,
                ),
                (
                    "model checksum",
                    lambda: assets.ModelAssetManager._read_checksum_manifest(reference),
                    assets.ModelIntegrityError,
                    None,
                ),
                (
                    "support tail",
                    lambda: support._read_bounded_json_lines(reference, 128),
                    None,
                    (),
                ),
                (
                    "support timelines",
                    lambda: support._read_previous_generation_timelines(reference),
                    None,
                    None,
                ),
                (
                    "support startup",
                    lambda: support.PregenerationSupportState(reference).report(),
                    None,
                    {"available": False},
                ),
                (
                    "support summary",
                    lambda: support._pregeneration_state_summary(reference),
                    None,
                    {"available": False},
                ),
                (
                    "support pack",
                    lambda: support._active_pack_identity(str(reference), 0, 2),
                    None,
                    {"available": False},
                ),
                (
                    "support story",
                    lambda: support._active_story_ids(root, {"path": reference.name}),
                    None,
                    {"active_story_ids_available": False},
                ),
                (
                    "candidate references",
                    lambda: voice_candidate_cache._references_in_document(
                        root, reference
                    ),
                    None,
                    None,
                ),
                (
                    "decoder",
                    lambda: game_audio_decoder._probe_cached_decoder(
                        root / "decoder.exe", None
                    ),
                    None,
                    False,
                ),
                (
                    "preview sidecar",
                    lambda: pregeneration_audition._cached_preview_metadata(
                        root / "verified.wav", "identity", plan
                    ),
                    pregeneration_audition.VoiceAuditionError,
                    None,
                ),
            )
            for label, read, error_type, expected in readers:
                with self.subTest(reader=label):
                    reference.unlink(missing_ok=True)
                    reference.write_bytes(b"{}")
                    os.mkfifo(fifo)
                    descriptors = []

                    def swap_and_open(path, flags):
                        self.assertEqual(Path(path), reference)
                        self.assertTrue(
                            flags & os.O_NONBLOCK,
                            "reader must not wait for a FIFO writer",
                        )
                        fifo.replace(reference)
                        descriptor = native_open(path, flags)
                        descriptors.append(descriptor)
                        return descriptor

                    def reject_blocking_path_open(path, *args, **kwargs):
                        if Path(path) == reference:
                            self.fail("bounded input must not use blocking Path.open")
                        return native_path_open(path, *args, **kwargs)

                    with (
                        patch("vntts.path_safety.os.open", side_effect=swap_and_open),
                        patch.object(Path, "open", reject_blocking_path_open),
                    ):
                        if error_type is not None:
                            with self.assertRaisesRegex(error_type, "regular file"):
                                read()
                        else:
                            result = read()
                            if isinstance(expected, dict):
                                self.assertEqual(
                                    {key: result[key] for key in expected}, expected
                                )
                            else:
                                self.assertEqual(result, expected)
                    self.assertEqual(len(descriptors), 1)
                    with self.assertRaises(OSError):
                        os.fstat(descriptors[0])


class VerifiedEvidenceAdmissionTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO admission")
    def test_verified_readers_reject_a_path_swapped_to_fifo_before_content(self):
        native_open, native_path_open = os.open, Path.open
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "verified.wav"
            fifo = root / "replacement"
            content = io.BytesIO()
            with wave.open(content, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24_000)
                output.writeframes(b"\x01\x00\x02\x00")
            payload = content.getvalue()
            digest = hashlib.sha256(payload).hexdigest()
            snapshot = authority.AuthoritySnapshot(path, payload, digest)
            review = bulk_generation.ReviewAuthority(digest, digest, digest, digest)
            audit = failure_reference_audit.FailureReferenceAudit(
                root, "audit", 1, 1, 1
            )
            document = {
                "groups": [
                    {
                        "group_id": "group",
                        "synthesis_voice_character": "Ada",
                        "cases": [{"queue_id": "queue", "text": "Hello."}],
                        "decision_options": ["accept"],
                        "candidates": [
                            {
                                "candidate_id": "candidate",
                                "audio": path.name,
                                "sha256": digest,
                            }
                        ],
                    }
                ]
            }
            readers = (
                (
                    "capture",
                    lambda: authority.capture_authority_file(path, "input", root=root),
                    authority.AuthoringAuthorityError,
                ),
                (
                    "recheck",
                    lambda: authority.assert_authority_snapshot(snapshot),
                    authority.AuthoringAuthorityError,
                ),
                (
                    "custom recheck",
                    lambda: authority.assert_authority_snapshot(
                        snapshot, error_type=PathBoundaryError
                    ),
                    PathBoundaryError,
                ),
                (
                    "cohort WAV",
                    lambda: bulk_generation._review_snapshot_audio(
                        path, "queue", review
                    ),
                    BulkGenerationError,
                ),
                (
                    "silence evidence",
                    lambda: silence_evidence._validate_evidence_wav(path, digest),
                    silence_evidence.SilenceFailureEvidenceError,
                ),
                (
                    "failure candidate",
                    lambda: failure_reference_audit._prepare_failure_reference_audio(
                        audit, document, "group", "candidate"
                    ),
                    OSError,
                ),
                (
                    "cohort snapshot",
                    lambda: cohort_bundle._read_bytes(path, "cohort input"),
                    cohort_bundle.CohortReviewError,
                ),
                (
                    "silence WAV",
                    lambda: silence_comparison._read_source_wav(path, "source"),
                    silence_comparison.SilenceComparisonError,
                ),
                (
                    "silence plan",
                    lambda: silence_comparison._read_silence_comparison_input_plan(
                        path
                    ),
                    silence_comparison.SilenceComparisonError,
                ),
                (
                    "audition WAV",
                    lambda: pregeneration_audition_ui._read_verified_audio(
                        path, digest
                    ),
                    OSError,
                ),
            )
            for label, read, error_type in readers:
                with self.subTest(reader=label):
                    original = b"{}" if label == "silence plan" else payload
                    path.unlink(missing_ok=True)
                    path.write_bytes(original)
                    os.mkfifo(fifo)
                    descriptors = []

                    def swap_and_open(candidate, flags):
                        self.assertEqual(Path(candidate), path)
                        self.assertTrue(
                            flags & os.O_NONBLOCK,
                            "verified evidence must not wait for a writer",
                        )
                        fifo.replace(path)
                        descriptor = native_open(candidate, flags)
                        descriptors.append(descriptor)
                        return descriptor

                    def reject_blocking_path_open(candidate, *args, **kwargs):
                        if candidate == path:
                            self.fail("verified input must not use blocking Path.open")
                        return native_path_open(candidate, *args, **kwargs)

                    with (
                        patch("vntts.path_safety.os.open", side_effect=swap_and_open),
                        patch.object(Path, "open", reject_blocking_path_open),
                        self.assertRaisesRegex(error_type, "regular file"),
                    ):
                        read()
                    self.assertEqual(len(descriptors), 1)
                    with self.assertRaises(OSError):
                        os.fstat(descriptors[0])
                    path.unlink()
                    path.write_bytes(original)
                    read()


if __name__ == "__main__":
    unittest.main()
