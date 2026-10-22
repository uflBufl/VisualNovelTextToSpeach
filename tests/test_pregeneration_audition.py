import builtins
import io
import json
import os
import shutil
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call, patch

import numpy as np
import soundfile as sf

import vntts.pregeneration_audition as audition_module
from tests.pregeneration_fixtures import FakeBackend, ambiguous_fixture, clean_wav_bytes
from tests.story_fixtures import write_content
from tests.voice_manifest_fixtures import write_manifest
from vntts.document_identity import canonical_document_sha256, sha256_file
from vntts.pregeneration_audition import (
    VoiceAuditionCancelled,
    VoiceAuditionError,
    VoiceAuditionIncomplete,
    VoiceAuditionPreviewService,
    VoiceAuditionReference,
    _staging_path,
    _validate_request,
    preview_identity,
)
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import VoiceCandidate, VoicePlanStore
from vntts.settings import AppSettings
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCompletion,
)


class VoiceAuditionPreviewServiceTest(unittest.TestCase):
    def test_warm_preview_reuses_registry_and_captured_checksum_preflight(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, group, manifest = ambiguous_fixture(root)
            source = group.candidates[0].source_id
            backend = FakeBackend("moss-tts")
            writer = VoiceAuditionPreviewService(
                root / "previews", backend_factory=lambda *_args, **_kwargs: backend
            )
            writer.generate(plan, group, source)
            writer.close()
            service = VoiceAuditionPreviewService(root / "previews")
            self.addCleanup(service.close)
            with (
                patch.object(
                    audition_module.CharacterVoiceRegistry,
                    "from_document",
                    wraps=audition_module.CharacterVoiceRegistry.from_document,
                ) as parse,
                patch.object(
                    audition_module,
                    "read_voice_reference_bytes",
                    wraps=audition_module.read_voice_reference_bytes,
                ) as read,
                patch.object(
                    audition_module,
                    "analyze_reference_bytes",
                    wraps=audition_module.analyze_reference_bytes,
                ) as preflight,
            ):
                first = service.lookup(plan, group, source)
                self.assertEqual(service.lookup(plan, group, source), first)
                parse.assert_called_once()
                preflight.assert_called_once()
                self.assertEqual(read.call_count, 2)
            reference = manifest.parent / "references" / "rhiannon.wav"
            original_stat = reference.stat()
            payload = bytearray(reference.read_bytes())
            payload[-1] ^= 1
            reference.write_bytes(payload)
            os.utime(
                reference, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns)
            )
            with self.assertRaisesRegex(VoiceAuditionError, "changed after planning"):
                service.lookup(plan, group, source)

    def test_cached_generate_rejects_mutation_and_cancel_during_admission(self):
        for action in ("reference", "manifest", "cancel"):
            with self.subTest(action=action), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                plan, group, manifest = ambiguous_fixture(root)
                backend = FakeBackend("moss-tts")
                service = VoiceAuditionPreviewService(
                    root / "previews", backend_factory=lambda *_args, **_kwargs: backend
                )
                self.addCleanup(service.close)
                source = group.candidates[0].source_id
                service.generate(plan, group, source)
                cancel = Event()
                cached = audition_module._cached_preview_for_request

                def changed(*args, **kwargs):
                    result = cached(*args, **kwargs)
                    if action == "cancel":
                        cancel.set()
                    elif action == "manifest":
                        manifest.write_bytes(manifest.read_bytes() + b" ")
                    else:
                        reference = manifest.parent / "references" / "rhiannon.wav"
                        reference.write_bytes(b"changed reference")
                    return result

                with patch.object(
                    audition_module, "_cached_preview_for_request", side_effect=changed
                ):
                    error = (
                        VoiceAuditionCancelled
                        if action == "cancel"
                        else VoiceAuditionError
                    )
                    with self.assertRaises(error):
                        service.generate(plan, group, source, cancel_event=cancel)
                self.assertEqual(len(backend.requests), 1)

    def test_warmup_reuses_the_same_backend_for_real_preview(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _ = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts")
            factory = Mock(return_value=backend)
            service = VoiceAuditionPreviewService(
                root / "previews", backend_factory=factory
            )
            self.addCleanup(service.close)
            settings = AppSettings(speech_backend="moss-tts", tts_profile="stable")
            service.warmup(settings, cancel_event=Event())
            self.assertFalse(factory.call_args.kwargs["allow_download"])
            self.assertEqual(backend.requests, [])
            self.assertTrue(service.can_prefetch(plan))
            service.generate(plan, group, group.candidates[0].source_id)
            factory.assert_called_once()
            self.assertEqual(len(backend.requests), 1)

    def test_cancelled_warmup_does_not_publish_backend(self):
        cancel = Event()
        backend = FakeBackend("moss-tts")

        def factory(*_args, **_kwargs):
            cancel.set()
            return backend

        service = VoiceAuditionPreviewService(backend_factory=factory)
        self.addCleanup(service.close)
        with self.assertRaises(VoiceAuditionCancelled):
            service.warmup(AppSettings(speech_backend="moss-tts"), cancel_event=cancel)
        self.assertIsNone(service.backend)
        self.assertEqual(backend.shutdown_count, 1)

    def test_new_preparation_reloads_an_explicitly_unloaded_native_lease(self):
        backend = FakeBackend("moss-tts")
        backend.runtime_status = "ready"
        replacement = FakeBackend("moss-tts")
        factory = Mock(side_effect=(backend, replacement))
        service = VoiceAuditionPreviewService(backend_factory=factory)
        self.addCleanup(service.close)
        settings = AppSettings(speech_backend="moss-tts")
        with patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=True):
            service.warmup(settings, cancel_event=Event())
            service.warmup(settings, cancel_event=Event())
            factory.assert_called_once()
            backend.runtime_status = None
            service.warmup(settings, cancel_event=Event())
        self.assertIs(service.backend, replacement)
        self.assertEqual(factory.call_count, 2)

    def test_local_moss_identity_keeps_snapshot_without_importing_huggingface(self):
        with TemporaryDirectory() as directory:
            model = Path(directory) / "local-model"
            model.mkdir()
            candidate = VoiceCandidate("preset:alba", "alba", "alba", ())
            original_import = builtins.__import__

            def reject_huggingface(name, *args, **kwargs):
                if name == "huggingface_hub":
                    raise AssertionError("Local models must not import Hugging Face")
                return original_import(name, *args, **kwargs)

            def identity():
                return preview_identity(
                    candidate,
                    "Preview text.",
                    backend="moss-tts",
                    model=str(model),
                    language="en",
                    profile="stable",
                )

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("vntts.runtime_paths.source_runtime_project", return_value=None),
                patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=False),
                patch("builtins.__import__", side_effect=reject_huggingface),
            ):
                with patch.object(
                    audition_module,
                    "canonical_document_sha256",
                    wraps=canonical_document_sha256,
                ) as hash_document:
                    identity()
                expected_document = hash_document.call_args.args[0]
                self.assertNotIn("model_snapshot", expected_document)
                config = model / "config.json"
                config.write_bytes(b'{"model_type":"moss_tts"}')
                expected_document["model_snapshot"] = (str(model), sha256_file(config))
                first = identity()
                self.assertEqual(first, canonical_document_sha256(expected_document))
                self.assertEqual(identity(), first)
                config.write_bytes(b'{"model_type":"moss_tts","revision":2}')
                self.assertNotEqual(identity(), first)

    def test_remote_moss_identity_resolves_and_hashes_cached_snapshot(self):
        with TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_bytes(b'{"model_type":"moss_tts"}')
            resolver = Mock(return_value=None)
            candidate = VoiceCandidate("preset:alba", "alba", "alba", ())

            def identity():
                return preview_identity(
                    candidate,
                    "Preview text.",
                    backend="moss-tts",
                    model="OpenMOSS/test-model",
                    language="en",
                    profile="stable",
                )

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("vntts.runtime_paths.source_runtime_project", return_value=None),
                patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=False),
                patch.dict(
                    "sys.modules",
                    {
                        "huggingface_hub": SimpleNamespace(
                            try_to_load_from_cache=resolver
                        )
                    },
                ),
            ):
                with patch.object(
                    audition_module,
                    "canonical_document_sha256",
                    wraps=canonical_document_sha256,
                ) as hash_document:
                    identity()
                expected_document = hash_document.call_args.args[0]
                self.assertNotIn("model_snapshot", expected_document)
                resolver.return_value = str(config)
                expected_document["model_snapshot"] = (
                    str(config.parent),
                    sha256_file(config),
                )
                self.assertEqual(
                    identity(), canonical_document_sha256(expected_document)
                )
                self.assertEqual(
                    resolver.call_args_list,
                    [call("OpenMOSS/test-model", "config.json")] * 2,
                )

    def test_request_prefers_inventory_and_retains_narrator_fallback(self):
        with TemporaryDirectory() as directory:
            plan, group, _manifest = ambiguous_fixture(Path(directory))
            candidate = group.candidates[0]
            inventoried = replace(candidate, recommendation="Reviewed inventory")
            narrator = VoiceCandidate("preset:alba", "alba", "alba", ())
            group = replace(
                group,
                candidates=(candidate, candidate),
                candidate_inventory=(inventoried,),
                narrator_candidate=narrator,
            )
            plan = replace(plan, groups=(group,))
            self.assertIs(
                _validate_request(plan, group, candidate.source_id), inventoried
            )
            self.assertIs(_validate_request(plan, group, narrator.source_id), narrator)
            for source_id in ("unavailable", [], None):
                with self.subTest(source_id=source_id):
                    with self.assertRaisesRegex(
                        VoiceAuditionError, "not uniquely available"
                    ):
                        _validate_request(plan, group, source_id)

    def test_each_preview_validation_phase_decodes_its_wav_once(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts")
            service = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: backend,
            )
            self.addCleanup(service.close)
            phases = []
            original_inspect = audition_module._inspect_preview

            def inspect_once(source, text):
                with patch.object(wave, "open", wraps=wave.open) as open_wav:
                    quality = original_inspect(source, text)
                decoded = [
                    call for call in open_wav.call_args_list if call.args[1] == "rb"
                ]
                self.assertEqual(len(decoded), 1)
                phases.append(source)
                return quality

            with patch.object(
                audition_module, "_inspect_preview", side_effect=inspect_once
            ):
                preview = service.generate(plan, group, group.candidates[0].source_id)
            self.assertEqual(len(phases), 2)
            self.assertIsInstance(phases[0], Path)
            self.assertTrue(phases[0].name.startswith("."))
            self.assertIsInstance(phases[1], io.BytesIO)
            self.assertEqual(phases[1].getvalue(), preview.path.read_bytes())
            phases.clear()
            with patch.object(
                audition_module, "_inspect_preview", side_effect=inspect_once
            ):
                cached = service.generate(plan, group, group.candidates[0].source_id)
            self.assertEqual(len(phases), 1)
            self.assertIsInstance(phases[0], io.BytesIO)
            self.assertEqual(phases[0].getvalue(), cached.path.read_bytes())
            self.assertTrue(cached.reused)
            self.assertEqual(cached.audio_sha256, preview.audio_sha256)
            self.assertEqual(len(backend.requests), 1)

    def test_rejects_changed_audio_between_preview_validation_phases(self):
        for cached in (False, True):
            with self.subTest(cached=cached), TemporaryDirectory() as directory:
                root = Path(directory)
                plan, group, _manifest = ambiguous_fixture(root)
                backend = FakeBackend("moss-tts")
                service = VoiceAuditionPreviewService(
                    root / "auditions",
                    backend_factory=lambda *_args, **_kwargs: backend,
                )
                self.addCleanup(service.close)
                if cached:
                    service.generate(plan, group, group.candidates[0].source_id)
                import vntts.pregeneration_audition as audition

                original_inspect = audition._inspect_preview

                def replace_before_inspection(source, text):
                    if isinstance(source, io.BytesIO):
                        target = next(
                            path
                            for path in service.root.glob("*.wav")
                            if not path.name.startswith(".")
                        )
                        target.write_bytes(clean_wav_bytes(amplitude=0.2, seconds=0.2))
                    return original_inspect(source, text)

                with patch.object(
                    audition, "_inspect_preview", side_effect=replace_before_inspection
                ):
                    with self.assertRaisesRegex(VoiceAuditionError, "changed while"):
                        service.generate(plan, group, group.candidates[0].source_id)
                self.assertEqual(len(backend.requests), 1)
                self.assertEqual(len(tuple(service.root.glob("*.wav"))), 1)

    def test_transient_cached_replacement_cannot_mix_metadata_and_hash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts")
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=lambda *_args, **_kwargs: backend
            )
            self.addCleanup(service.close)
            first = service.generate(plan, group, group.candidates[0].source_id)
            payload = first.path.read_bytes()
            original_inspect = audition_module._inspect_preview

            def temporary_replacement(source, text):
                first.path.write_bytes(clean_wav_bytes(amplitude=0.2, seconds=0.2))
                try:
                    return original_inspect(source, text)
                finally:
                    first.path.write_bytes(payload)

            with patch.object(
                audition_module, "_inspect_preview", side_effect=temporary_replacement
            ):
                cached = service.generate(plan, group, group.candidates[0].source_id)
            self.assertTrue(cached.reused)
            self.assertEqual(cached.audio_sha256, first.audio_sha256)
            self.assertEqual(cached.sample_rate, first.sample_rate)
            self.assertEqual(cached.duration_seconds, first.duration_seconds)
            self.assertEqual(first.path.read_bytes(), payload)
            self.assertEqual(len(backend.requests), 1)

    def test_legacy_preview_rejects_replacement_after_inspection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts")
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=lambda *_args, **_kwargs: backend
            )
            self.addCleanup(service.close)
            first = service.generate(plan, group, group.candidates[0].source_id)
            sidecar = first.path.with_suffix(".json")
            sidecar.unlink()
            original_inspect = audition_module._inspect_preview

            def replace_after_decode(source, text):
                quality = original_inspect(source, text)
                first.path.write_bytes(clean_wav_bytes(amplitude=0.2, seconds=0.2))
                return quality

            with patch.object(
                audition_module, "_inspect_preview", side_effect=replace_after_decode
            ):
                with self.assertRaisesRegex(VoiceAuditionError, "changed while"):
                    service.generate(plan, group, group.candidates[0].source_id)
            self.assertEqual(len(backend.requests), 1)
            self.assertEqual(tuple(service.root.glob("*.wav")), (first.path,))
            self.assertFalse(sidecar.exists())

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_preview_target_hashes_reject_fifo_swaps_and_cache_recovers(self):
        for phase in ("metadata", "post-inspection"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory)
                plan, group, _manifest = ambiguous_fixture(root)
                backend = FakeBackend("moss-tts")
                service = VoiceAuditionPreviewService(
                    root / "auditions",
                    backend_factory=lambda *_args, **_kwargs: backend,
                )
                self.addCleanup(service.close)
                first = service.generate(plan, group, group.candidates[0].source_id)
                payload = first.path.read_bytes()
                native_open = os.open
                descriptors = []
                target_opens = 0

                def swap_on_target_hash(candidate, flags, mode=0o777, *, dir_fd=None):
                    nonlocal target_opens
                    if Path(candidate) == first.path:
                        target_opens += 1
                        if target_opens == (1 if phase == "metadata" else 3):
                            self.assertTrue(flags & os.O_NONBLOCK)
                            first.path.unlink()
                            os.mkfifo(first.path)
                    descriptor = native_open(candidate, flags, mode, dir_fd=dir_fd)
                    descriptors.append(descriptor)
                    return descriptor

                with patch(
                    "vntts.path_safety.os.open", side_effect=swap_on_target_hash
                ):
                    with self.assertRaisesRegex(VoiceAuditionError, "regular file"):
                        service.generate(plan, group, group.candidates[0].source_id)
                self.assertEqual(target_opens, 1 if phase == "metadata" else 3)
                for descriptor in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)
                first.path.unlink()
                first.path.write_bytes(payload)
                cached = service.generate(plan, group, group.candidates[0].source_id)
                self.assertTrue(cached.reused)
                self.assertEqual(cached.audio_sha256, first.audio_sha256)
                self.assertEqual(len(backend.requests), 1)

    def test_reuses_legacy_preview_without_a_manifest(self):
        for backend_name, expected_seed in (("moss-tts", 0), ("pocket-tts", None)):
            with self.subTest(backend=backend_name), TemporaryDirectory() as directory:
                root = Path(directory)
                plan, group, _manifest = ambiguous_fixture(root)
                plan = replace(plan, synthesis_backend=backend_name)
                backend = FakeBackend(backend_name)
                service = VoiceAuditionPreviewService(
                    root / "auditions",
                    backend_factory=lambda *_args, **_kwargs: backend,
                )
                self.addCleanup(service.close)
                preview = service.generate(plan, group, group.candidates[0].source_id)
                preview.path.with_suffix(".json").unlink()
                cached = service.generate(plan, group, group.candidates[0].source_id)
                self.assertTrue(cached.reused)
                self.assertEqual(cached.seed, expected_seed)
                self.assertEqual(cached.audio_sha256, preview.audio_sha256)
                self.assertEqual(len(backend.requests), 1)

    def test_xtts_preview_uses_the_planned_language(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan = replace(
                plan,
                synthesis_backend="coqui-xtts",
                synthesis_language="ru",
                xtts_terms_accepted=True,
            )
            factory = Mock(return_value=FakeBackend("coqui-xtts"))
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=factory
            )
            try:
                service.generate(plan, group, group.candidates[0].source_id)
            finally:
                service.close()

            self.assertEqual(factory.call_args.kwargs["language"], "ru")

    def test_automatic_voice_route_can_be_previewed(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, ambiguous, _manifest = ambiguous_fixture(root)
            group = replace(
                ambiguous, route="voice", resolution="known-character-voice"
            )
            plan = replace(
                plan,
                groups=tuple(
                    group if value.group_id == group.group_id else value
                    for value in plan.groups
                ),
            )
            service = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: FakeBackend("moss-tts"),
            )
            try:
                preview = service.generate(plan, group, group.source_id)
                self.assertTrue(preview.path.is_file())
            finally:
                service.close()

    def test_pocket_permission_reaches_preview_and_invalidates_loaded_worker(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            plan = replace(
                plan, synthesis_backend="pocket-tts", synthesis_profile="default"
            )
            created = []

            def factory(_name, _registry, _root, **options):
                backend = FakeBackend("pocket-tts")
                created.append((backend, options["allow_gated_model_access"]))
                return backend

            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=factory
            )
            try:
                source = group.candidates[0].source_id
                service.generate(plan, group, source)
                enabled = replace(plan, pocket_voice_cloning=True)
                service.generate(
                    enabled, group, source, text=group.alternate_sample_text
                )
                self.assertEqual(
                    [permission for _, permission in created], [False, True]
                )
                self.assertEqual(created[0][0].shutdown_count, 1)
            finally:
                service.close()

    def test_returns_only_a_checksum_verified_playable_original_anchor(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_content(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(content, ("story",))
            manifest = write_manifest(root / "voices")
            reference = manifest.parent / "references" / "rhiannon.wav"
            sf.write(reference, np.tile((0.1, -0.1), 9_600), 16_000, subtype="PCM_16")
            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(speech_backend="moss-tts", tts_profile="stable"),
                manifest_path=manifest,
            )
            selected = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            group = replace(
                selected,
                route="needs-audition",
                resolution="ambiguous-voice-evidence",
                anchor_source_id=selected.candidates[0].source_id,
            )
            plan = replace(
                plan,
                groups=tuple(
                    group if value.group_id == group.group_id else value
                    for value in plan.groups
                ),
            )
            service = VoiceAuditionPreviewService(root / "auditions")

            self.assertEqual(
                service.reference_audio(plan, group, group.anchor_source_id),
                VoiceAuditionReference(reference.resolve(), reference.read_bytes()),
            )
            service.close()

    def test_imported_voice_display_identity_does_not_invalidate_candidate(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_content(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(content, ("story",))
            manifest = write_manifest(root / "voices")
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["voices"][0]["vntts.source_character"] = (
                "Player candidate Rhiannon 123abc"
            )
            manifest.write_text(json.dumps(document), encoding="utf-8")
            reference = manifest.parent / "references" / "rhiannon.wav"
            sf.write(reference, np.tile((0.1, -0.1), 9_600), 16_000, subtype="PCM_16")
            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(speech_backend="moss-tts", tts_profile="stable"),
                manifest_path=manifest,
            )
            group = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            candidate = next(
                candidate
                for candidate in group.candidates
                if candidate.source_character == "Player candidate Rhiannon 123abc"
            )
            backend = FakeBackend("moss-tts")
            service = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: backend,
            )
            try:
                self.assertEqual(
                    service.reference_audio(plan, group, candidate.source_id),
                    VoiceAuditionReference(reference.resolve(), reference.read_bytes()),
                )
                service.generate(plan, group, candidate.source_id)
                self.assertEqual(backend.requests[0].voice, "Rhiannon")
            finally:
                service.close()

    @patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=True)
    def test_generates_one_exact_preview_then_reuses_persistent_wav(self, _native):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            original_group = group
            group = replace(
                group,
                sample_text="Are you certain? We can leave before the storm arrives.",
            )
            plan = replace(
                plan,
                groups=tuple(
                    group if value is original_group else value for value in plan.groups
                ),
            )
            backend = FakeBackend("moss-tts")
            factory_calls = []
            progress = []

            def factory(name, registry, cache_root, **options):
                factory_calls.append((name, registry, cache_root, options))
                options["startup_progress"]("Loading native model...")
                backend.registry = registry
                return backend

            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=factory
            )
            source_id = group.candidates[0].source_id
            self.assertIsNone(service.backend)
            first = service.generate(plan, group, source_id, progress=progress.append)
            self.assertIs(service.backend, backend)
            self.assertEqual(
                progress,
                [
                    "Starting the preview model. First use also loads its weights...",
                    "Loading native model...",
                    "Generating preview audio with the loaded model...",
                    "Checking generated audio for silence and other failures...",
                ],
            )
            progress.clear()
            service.close()
            self.assertIsNone(service.backend)
            second_service = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: self.fail(
                    "A persisted preview must not restart the model"
                ),
            )
            with (
                patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=True),
                patch("vntts.support.record_native_speech") as native_event,
            ):
                second = second_service.generate(
                    plan, group, source_id, progress=progress.append
                )
                native_event.assert_has_calls(
                    [
                        call(
                            operation="cached-preview",
                            outcome="complete",
                            cache="preview-file",
                            reference="not-used",
                            audio_s=second.duration_seconds,
                            gen_s=None,
                            decode_s=None,
                        ),
                        call(
                            operation="preview-outcome",
                            backend="moss-tts",
                            profile="stable",
                            requested_seed=0,
                            outcome="success",
                            reason="accepted",
                            stage="cache",
                            cache_source="preview-file",
                            elapsed_ms=ANY,
                        ),
                    ]
                )
            self.assertEqual(progress, ["Checking the saved preview..."])
            second_service.close()

            self.assertTrue(first.path.is_file())
            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(first.audio_sha256, second.audio_sha256)
            self.assertEqual(first.text, group.sample_text)
            self.assertEqual(first.seed, 0)
            self.assertEqual(len(factory_calls), 1)
            self.assertEqual(len(backend.requests), 1)
            self.assertEqual(backend.requests[0].voice, "Rhiannon")
            self.assertEqual(backend.requests[0].text, group.sample_text)
            self.assertEqual(backend.shutdown_count, 1)

    def test_native_preview_does_not_reuse_old_sampling_caches(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts")
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=lambda *_args, **_kw: backend
            )
            self.addCleanup(service.close)
            source_id = group.candidates[0].source_id
            # MLX retains the pre-fix identity, previously shared with native.
            with patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=False):
                old = service.generate(plan, group, source_id)
            with patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=True):
                with patch(
                    "vntts.moss_cpp_backend.NATIVE_GENERATION_CONTRACT",
                    "nonzero-seed-v1",
                ):
                    old_native = service.generate(plan, group, source_id)
                native = service.generate(plan, group, source_id)
                repeated = service.generate(plan, group, source_id)
            self.assertNotEqual(old.identity, native.identity)
            self.assertNotEqual(old_native.identity, native.identity)
            self.assertTrue(old_native.path.is_file())
            self.assertTrue(old.path.is_file())
            self.assertFalse(native.reused)
            self.assertTrue(repeated.reused)
            self.assertEqual(len(backend.requests), 3)

    @patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=True)
    def test_native_limit_retry_uses_a_fresh_seed_and_persists_it(self, _native):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts", completion=SynthesisCompletion.LIMITED)
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=lambda *_args, **_kwargs: backend
            )
            source_id = group.candidates[0].source_id

            with self.assertRaises(VoiceAuditionIncomplete):
                service.generate(plan, group, source_id)
            backend.completion = SynthesisCompletion.COMPLETE
            preview = service.generate(plan, group, source_id)
            service.close()

            self.assertEqual([request.seed for request in backend.requests], [0, 2])
            self.assertEqual(
                [request.cache_policy for request in backend.requests],
                [SynthesisCachePolicy.USE, SynthesisCachePolicy.REFRESH],
            )
            self.assertEqual(preview.seed, 2)
            self.assertEqual(
                json.loads(preview.path.with_suffix(".json").read_text()),
                {
                    "audio_sha256": preview.audio_sha256,
                    "identity": preview.identity,
                    "seed": 2,
                },
            )

            restarted = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: self.fail(
                    "A saved retry must not restart the model"
                ),
            )
            self.addCleanup(restarted.close)
            cached = restarted.generate(plan, group, source_id)
            self.assertTrue(cached.reused)
            self.assertEqual(cached.seed, 2)

    def test_wav_publish_failure_leaves_no_cache_and_restart_reuses_retry(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts")
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=lambda *_args, **_kwargs: backend
            )
            source_id = group.candidates[0].source_id
            original_replace = os.replace

            def fail_wav_publish(source, destination):
                if Path(destination).suffix == ".wav":
                    raise OSError("publish failed")
                return original_replace(source, destination)

            with patch(
                "vntts.pregeneration_audition.os.replace",
                side_effect=fail_wav_publish,
            ):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    service.generate(plan, group, source_id)
            self.assertFalse(tuple((root / "auditions").glob("*.wav")))
            self.assertFalse(tuple((root / "auditions").glob("*.json")))

            preview = service.generate(plan, group, source_id)
            service.close()
            restarted = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: self.fail(
                    "A successful retry must remain cached"
                ),
            )
            self.addCleanup(restarted.close)
            cached = restarted.generate(plan, group, source_id)

            self.assertTrue(preview.path.is_file())
            self.assertTrue(cached.reused)
            self.assertEqual(cached.seed, preview.seed)

    def test_growing_cached_manifest_is_read_with_a_bound(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts")
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=lambda *_args, **_kwargs: backend
            )
            self.addCleanup(service.close)
            source_id = group.candidates[0].source_id
            preview = service.generate(plan, group, source_id)
            source = io.BytesIO(b"{}" + b" " * 4096)
            read = Mock(wraps=source.read)
            source.read = read

            with (
                patch(
                    "vntts.pregeneration_audition.open_regular_binary",
                    return_value=source,
                ),
                self.assertRaisesRegex(VoiceAuditionError, "manifest is too large"),
            ):
                service.generate(plan, group, source_id)
            read.assert_called_once_with(1025)
            self.assertTrue(preview.path.is_file())
            self.assertEqual(len(backend.requests), 1)

    def test_preview_manifest_write_failure_leaves_no_wav(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            service = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: FakeBackend("moss-tts"),
            )
            self.addCleanup(service.close)

            with patch(
                "vntts.pregeneration_audition._write_preview_manifest",
                side_effect=OSError("manifest failed"),
            ):
                with self.assertRaisesRegex(OSError, "manifest failed"):
                    service.generate(plan, group, group.candidates[0].source_id)

            self.assertFalse(tuple((root / "auditions").glob("*.wav")))

    def test_preview_stage_close_failure_cleans_up_before_handoff(self):
        for fail_cleanup in (False, True):
            with (
                self.subTest(fail_cleanup=fail_cleanup),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                target = root / "preview.wav"
                target.write_bytes(b"existing preview")
                primary = OSError("stage descriptor close failed")
                cleanup_error = OSError("stage unlink failed")
                cleanup_error.add_note("nested unlink note")
                original_close, original_unlink = os.close, Path.unlink
                cleanup_paths = []

                def close(descriptor):
                    original_close(descriptor)
                    raise primary

                def unlink(path, *, missing_ok=False):
                    cleanup_paths.append(path)
                    self.assertTrue(missing_ok)
                    if fail_cleanup:
                        raise cleanup_error
                    original_unlink(path, missing_ok=missing_ok)

                with (
                    patch(
                        "vntts.pregeneration_audition.os.close", side_effect=close
                    ) as close_mock,
                    patch.object(Path, "unlink", autospec=True, side_effect=unlink),
                    self.assertRaises(OSError) as raised,
                ):
                    _staging_path(target)

                self.assertIs(raised.exception, primary)
                close_mock.assert_called_once()
                self.assertEqual(len(cleanup_paths), 1)
                self.assertEqual(cleanup_paths[0].parent, root)
                self.assertTrue(cleanup_paths[0].name.startswith(".preview-"))
                self.assertEqual(
                    getattr(primary, "__notes__", []),
                    [
                        "Voice preview stage acquisition cleanup failed: stage unlink failed",
                        "nested unlink note",
                    ]
                    if fail_cleanup
                    else [],
                )
                self.assertEqual(
                    len(list(root.glob(".preview-*.wav"))), int(fail_cleanup)
                )
                self.assertEqual(target.read_bytes(), b"existing preview")

    def test_preview_cleanup_failures_keep_publish_error_and_attempt_all_owners(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            plan, group, _manifest = ambiguous_fixture(root)
            service = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: FakeBackend("moss-tts"),
            )
            self.addCleanup(service.close)
            original_unlink = type(root).unlink
            cleanup_attempts = []

            def fail_audition_cleanup(path, *args, **kwargs):
                if path.parent == root / "auditions":
                    cleanup_attempts.append(path)
                    raise OSError(f"cleanup failed for {path.name}")
                return original_unlink(path, *args, **kwargs)

            with (
                patch(
                    "vntts.pregeneration_audition._write_preview_manifest",
                    side_effect=OSError("manifest failed"),
                ),
                patch.object(
                    type(root / "auditions"),
                    "unlink",
                    autospec=True,
                    side_effect=fail_audition_cleanup,
                ),
            ):
                with self.assertRaisesRegex(OSError, "manifest failed") as raised:
                    service.generate(plan, group, group.candidates[0].source_id)

            self.assertEqual(len(cleanup_attempts), 3)
            self.assertGreaterEqual(
                sum("cleanup failed" in note for note in raised.exception.__notes__), 3
            )

    def test_competing_service_cannot_publish_the_same_preview(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            source_id = group.candidates[0].source_id
            first_rendering = Event()
            release_first = Event()

            def wait_during_render():
                first_rendering.set()
                self.assertTrue(release_first.wait(5))

            first_backend = FakeBackend("moss-tts", on_render=wait_during_render)
            second_backend = FakeBackend("moss-tts")
            first = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=lambda *_args, **_kw: first_backend
            )
            second = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=lambda *_args, **_kw: second_backend
            )
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            with ThreadPoolExecutor(max_workers=2) as executor:
                first_result = executor.submit(first.generate, plan, group, source_id)
                self.assertTrue(first_rendering.wait(5))
                second_result = executor.submit(second.generate, plan, group, source_id)
                try:
                    with self.assertRaisesRegex(
                        VoiceAuditionError, "already being generated"
                    ):
                        second_result.result(timeout=5)
                finally:
                    release_first.set()
                preview = first_result.result(timeout=5)

            self.assertTrue(preview.path.is_file())
            self.assertEqual(second_backend.requests, [])
            self.assertTrue(second.generate(plan, group, source_id).reused)

    @patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=True)
    def test_native_quality_failure_refreshes_the_backend_cache(self, _native):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts")
            # Deliberately return float64 PCM from a normally float32 provider.
            backend.pcm = np.concatenate(
                (np.full(1_600, 0.1, dtype=np.float32), np.zeros(19_200))
            )
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=lambda *_args, **_kwargs: backend
            )
            self.addCleanup(service.close)
            source_id = group.candidates[0].source_id

            with patch("vntts.support.record_native_speech") as native_event:
                with self.assertRaisesRegex(VoiceAuditionError, "silence"):
                    service.generate(plan, group, source_id)
                rejection = next(
                    item.kwargs
                    for item in native_event.call_args_list
                    if item.kwargs.get("operation") == "preview-quality"
                    and item.kwargs.get("outcome") == "rejected"
                )
            self.assertEqual(rejection["reason"], "speech-silence")
            self.assertGreater(rejection["quality"]["silence_ratio"], 0.5)
            self.assertNotIn("text", rejection)
            self.assertNotIn("path", rejection)
            self.assertNotIn("error", rejection)
            backend.pcm = None
            preview = service.generate(plan, group, source_id)

            self.assertEqual([request.seed for request in backend.requests], [0, 2])
            self.assertEqual(
                [request.cache_policy for request in backend.requests],
                [SynthesisCachePolicy.USE, SynthesisCachePolicy.REFRESH],
            )
            self.assertEqual(preview.seed, 2)

    def test_rejects_reference_changed_after_voice_plan(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, manifest = ambiguous_fixture(root)
            reference = manifest.parent / "references" / "rhiannon.wav"
            reference.write_bytes(b"changed")
            factory_called = False

            def factory(*_arguments, **_options):
                nonlocal factory_called
                factory_called = True
                return FakeBackend("moss-tts")

            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=factory
            )
            with self.assertRaisesRegex(VoiceAuditionError, "candidate changed"):
                service.generate(plan, group, group.candidates[0].source_id)
            service.close()

            self.assertFalse(factory_called)

    def test_optional_second_phrase_uses_a_separate_persistent_preview(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts")
            service = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: backend,
            )
            source_id = group.candidates[0].source_id

            first = service.generate(plan, group, source_id)
            alternate = service.generate(
                plan,
                group,
                source_id,
                text=group.alternate_sample_text,
            )
            with self.assertRaisesRegex(VoiceAuditionError, "text is invalid"):
                service.generate(plan, group, source_id, text="Unbound phrase")
            service.close()

            self.assertNotEqual(first.identity, alternate.identity)
            self.assertNotEqual(first.path, alternate.path)
            self.assertEqual(alternate.text, "Short.")
            self.assertEqual(
                [request.text for request in backend.requests],
                [group.sample_text, group.alternate_sample_text],
            )

    def test_rejects_objectively_bad_reference_before_starting_model(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_content(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(content, ("story",))
            manifest = write_manifest(
                root / "voices", rhiannon=clean_wav_bytes(amplitude=1.0)
            )
            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(speech_backend="moss-tts", tts_profile="stable"),
                manifest_path=manifest,
            )
            selected = next(
                group for group in plan.groups if group.character == "Rhiannon"
            )
            group = replace(selected, route="needs-audition")
            plan = replace(
                plan,
                groups=tuple(
                    group if value.group_id == group.group_id else value
                    for value in plan.groups
                ),
            )
            factory = Mock()
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=factory
            )

            with (
                patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=True),
                patch("vntts.support.record_native_speech") as native_event,
            ):
                with self.assertRaisesRegex(VoiceAuditionError, "excessive-clipping"):
                    service.generate(plan, group, group.candidates[0].source_id)
            service.close()

            factory.assert_not_called()
            native_event.assert_called_once_with(
                operation="preview-outcome",
                backend="moss-tts",
                profile="stable",
                requested_seed=None,
                outcome="reference_preflight_failed",
                reason="reference-preflight-failed",
                stage="reference-preflight",
                cache_source=None,
                elapsed_ms=ANY,
            )

    def test_rejects_silent_generated_preview_without_publishing_wav(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts", pcm=np.zeros(1_600, dtype=np.float32))
            service = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: backend,
            )

            with self.assertRaisesRegex(VoiceAuditionError, "effectively silent"):
                service.generate(plan, group, group.candidates[0].source_id)
            service.close()

            self.assertFalse(tuple((root / "auditions").glob("*.wav")))

    def test_embedded_pocket_candidate_needs_no_manifest_or_seed(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            candidate = VoiceCandidate("preset:alba", "alba", "alba", ())
            group = replace(group, candidates=(candidate,))
            plan = replace(
                plan,
                voice_manifest=None,
                voice_manifest_sha256=None,
                synthesis_backend="pocket-tts",
                synthesis_model=None,
                synthesis_profile="default",
                groups=tuple(
                    group if value.group_id == group.group_id else value
                    for value in plan.groups
                ),
            )
            backend = FakeBackend("pocket-tts")

            def factory(_name, registry, _cache_root, **_options):
                self.assertEqual(registry.resolve("alba").speaker, "alba")
                return backend

            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=factory
            )
            preview = service.generate(plan, group, candidate.source_id)
            service.close()

            self.assertIsNone(preview.seed)
            self.assertIsNone(backend.requests[0].seed)

    def test_rejects_incomplete_provider_result_without_publishing_wav(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts", completion=SynthesisCompletion.LIMITED)
            service = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: backend,
            )

            with self.assertRaises(VoiceAuditionIncomplete):
                service.generate(plan, group, group.candidates[0].source_id)
            service.close()

            self.assertFalse(tuple((root / "auditions").glob("*.wav")))

    def test_close_releases_backend_state_when_shutdown_fails(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts")
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=lambda *_args, **_kwargs: backend
            )
            preview = service.generate(plan, group, group.candidates[0].source_id)
            with patch.object(
                backend, "shutdown", side_effect=RuntimeError("shutdown failed")
            ) as shutdown:
                with self.assertRaisesRegex(RuntimeError, "shutdown failed"):
                    service.close()
                self.assertIsNone(service._backend)
                self.assertIsNone(service._backend_config)
                self.assertTrue(preview.path.exists())
                service.close()
                shutdown.assert_called_once()
            with self.assertRaisesRegex(VoiceAuditionError, "closed"):
                service.generate(plan, group, group.candidates[0].source_id)

    def test_invalid_provider_sample_rates_never_publish_preview(self):
        for rate in (True, 16000.5, "16000", None, 0, -1, float("nan"), float("inf")):
            with self.subTest(rate=rate), TemporaryDirectory() as directory:
                root = Path(directory)
                plan, group, _manifest = ambiguous_fixture(root)
                backend = FakeBackend("moss-tts")
                backend.result_sample_rate = (
                    rate  # Deliberately violate the provider contract.
                )
                service = VoiceAuditionPreviewService(
                    root / "auditions",
                    backend_factory=lambda *_args, **_kwargs: backend,
                )
                try:
                    with self.assertRaisesRegex(VoiceAuditionError, "sample rate"):
                        service.generate(plan, group, group.candidates[0].source_id)
                    self.assertEqual(len(backend.requests), 1)
                    self.assertFalse(tuple((root / "auditions").glob("*.wav")))
                finally:
                    service.close()

    def test_cooperative_cancellation_does_not_publish_preview(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            cancellation = Event()
            backend = FakeBackend(
                "moss-tts",
                completion=SynthesisCompletion.CANCELLED,
                on_render=cancellation.set,
            )
            service = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: backend,
            )

            with self.assertRaises(VoiceAuditionCancelled):
                service.generate(
                    plan,
                    group,
                    group.candidates[0].source_id,
                    cancel_event=cancellation,
                )
            backend.completion = SynthesisCompletion.COMPLETE
            preview = service.generate(plan, group, group.candidates[0].source_id)
            service.close()

            self.assertEqual(len(tuple((root / "auditions").glob("*.wav"))), 1)
            self.assertEqual([request.seed for request in backend.requests], [0, 0])
            self.assertEqual(preview.seed, 0)

    def test_reference_audio_uses_checksum_bound_payload(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, group, manifest = ambiguous_fixture(root)
            candidate = group.candidates[0]
            reference = manifest.parent / "references" / "rhiannon.wav"
            original_payload = reference.read_bytes()
            service = VoiceAuditionPreviewService(root / "auditions")
            probe = audition_module.probe_pcm16_mono_wav

            def replace_before_decode(source):
                reference.write_bytes(b"replaced after checksum")
                return probe(source)

            try:
                with patch.object(
                    audition_module,
                    "probe_pcm16_mono_wav",
                    side_effect=replace_before_decode,
                ):
                    self.assertEqual(
                        service.reference_audio(plan, group, candidate.source_id),
                        VoiceAuditionReference(reference, original_payload),
                    )
                self.assertEqual(reference.read_bytes(), b"replaced after checksum")
                with self.assertRaisesRegex(
                    VoiceAuditionError, "changed after planning"
                ):
                    service.reference_audio(plan, group, candidate.source_id)
            finally:
                service.close()

    def test_original_reads_once_reuses_registry_and_never_waits_for_render(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, group, _manifest = ambiguous_fixture(root)
            factory = Mock()
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=factory
            )
            self.addCleanup(service.close)
            with (
                patch.object(
                    audition_module,
                    "capture_authority_file",
                    wraps=audition_module.capture_authority_file,
                ) as capture,
                patch.object(
                    audition_module.CharacterVoiceRegistry,
                    "from_document",
                    wraps=audition_module.CharacterVoiceRegistry.from_document,
                ) as registry,
                patch.object(
                    audition_module,
                    "read_voice_reference_bytes",
                    wraps=audition_module.read_voice_reference_bytes,
                ) as read,
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                service._lock.acquire()
                try:
                    for _ in range(2):
                        reference = pool.submit(
                            service.reference_audio,
                            plan,
                            group,
                            group.candidates[0].source_id,
                        ).result(timeout=5)
                        self.assertIsInstance(reference, VoiceAuditionReference)
                finally:
                    service._lock.release()
                self.assertEqual(capture.call_count, 2)
                registry.assert_called_once()
                self.assertEqual(read.call_count, 2)
            factory.assert_not_called()

    def test_original_cached_registry_revalidates_changed_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, group, manifest = ambiguous_fixture(root)
            service = VoiceAuditionPreviewService(root / "auditions")
            self.addCleanup(service.close)
            source = group.candidates[0].source_id
            service.reference_audio(plan, group, source)
            document = json.loads(manifest.read_bytes())
            document["voices"][0]["speaker"] = "changed-speaker"
            manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(VoiceAuditionError, "Character voices changed"):
                service.reference_audio(plan, group, source)
            with self.assertRaisesRegex(VoiceAuditionError, "Voice candidate changed"):
                service.reference_audio(
                    replace(plan, voice_manifest_sha256=sha256_file(manifest)),
                    group,
                    source,
                )

    def test_original_registry_cache_keeps_identical_manifest_roots_separate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, group, manifest = ambiguous_fixture(root)
            copied_root = root / "copied-voices"
            shutil.copytree(manifest.parent, copied_root)
            service = VoiceAuditionPreviewService(root / "auditions")
            self.addCleanup(service.close)
            source = group.candidates[0].source_id
            service.reference_audio(plan, group, source)
            reference = service.reference_audio(
                replace(plan, voice_manifest=str(copied_root / manifest.name)),
                group,
                source,
            )
            self.assertIsNotNone(reference)
            self.assertEqual(
                reference.path, copied_root / "references" / "rhiannon.wav"
            )

    def test_generated_preview_rechecks_manifest_after_capturing_references(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, group, manifest = ambiguous_fixture(root)
            factory = Mock()
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=factory
            )
            self.addCleanup(service.close)
            read = audition_module.read_voice_reference_bytes

            def change_manifest_after_read(*args):
                payload = read(*args)
                manifest.write_bytes(manifest.read_bytes() + b" ")
                return payload

            with patch.object(
                audition_module,
                "read_voice_reference_bytes",
                side_effect=change_manifest_after_read,
            ):
                with self.assertRaisesRegex(
                    VoiceAuditionError, "changed while they were read"
                ):
                    service.generate(plan, group, group.candidates[0].source_id)
            factory.assert_not_called()

    def test_original_cached_registry_rechecks_all_selected_references(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, group, manifest = ambiguous_fixture(root)
            sibling = manifest.parent / "references" / "second.wav"
            sibling.write_bytes(clean_wav_bytes())
            document = json.loads(manifest.read_bytes())
            document["voices"][0]["references"].append("references/second.wav")
            manifest.write_text(json.dumps(document), encoding="utf-8")
            candidate = replace(
                group.candidates[0],
                reference_sha256s=(
                    *group.candidates[0].reference_sha256s,
                    sha256_file(sibling),
                ),
            )
            group = replace(group, candidates=(candidate,), candidate_inventory=())
            plan = replace(
                plan, groups=(group,), voice_manifest_sha256=sha256_file(manifest)
            )
            service = VoiceAuditionPreviewService(root / "auditions")
            self.addCleanup(service.close)
            with patch.object(
                audition_module,
                "read_voice_reference_bytes",
                wraps=audition_module.read_voice_reference_bytes,
            ) as read:
                service.reference_audio(plan, group, candidate.source_id)
                self.assertEqual(read.call_count, 2)
            sibling.write_bytes(b"changed sibling")
            with self.assertRaisesRegex(VoiceAuditionError, "Voice candidate changed"):
                service.reference_audio(plan, group, candidate.source_id)

    def test_original_cached_registry_refuses_new_reference_symlinks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, group, manifest = ambiguous_fixture(root)
            service = VoiceAuditionPreviewService(root / "auditions")
            self.addCleanup(service.close)
            source = group.candidates[0].source_id
            service.reference_audio(plan, group, source)
            references = manifest.parent / "references"
            moved = manifest.parent / "moved-references"
            references.rename(moved)
            try:
                references.symlink_to(moved, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"Directory symlinks are unavailable: {error}")
            with self.assertRaisesRegex(VoiceAuditionError, "symlinks"):
                service.reference_audio(plan, group, source)

    def test_original_rejects_checksum_matching_malformed_pcm(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, group, manifest = ambiguous_fixture(root)
            reference = manifest.parent / "references" / "rhiannon.wav"
            reference.write_bytes(b"not a WAV")
            candidate = replace(
                group.candidates[0], reference_sha256s=(sha256_file(reference),)
            )
            group = replace(group, candidates=(candidate,), candidate_inventory=())
            plan = replace(plan, groups=(group,))
            service = VoiceAuditionPreviewService(root / "auditions")
            self.addCleanup(service.close)
            with self.assertRaisesRegex(VoiceAuditionError, "not playable"):
                service.reference_audio(plan, group, candidate.source_id)

    def test_original_returns_none_for_presets_without_starting_tts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, group, _manifest = ambiguous_fixture(root)
            candidate = VoiceCandidate("preset:alba", "alba", "alba", ())
            group = replace(group, candidates=(candidate,), candidate_inventory=())
            plan = replace(plan, groups=(group,))
            factory = Mock()
            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=factory
            )
            self.addCleanup(service.close)
            self.assertIsNone(service.reference_audio(plan, group, candidate.source_id))
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
