import io
import json
import os
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import ANY, Mock, call, patch

import numpy as np
import soundfile as sf

from tests.test_pregeneration_voices import write_content, write_manifest
from vntts.pregeneration_audition import (
    VoiceAuditionCancelled,
    VoiceAuditionError,
    VoiceAuditionIncomplete,
    VoiceAuditionPreviewService,
)
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import VoiceCandidate, VoicePlanStore
from vntts.settings import AppSettings
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisResult,
    SynthesisTiming,
)


class CollectedResult:
    def __init__(self, result):
        self.result = result

    def collect(self):
        return self.result


class FakeBackend:
    def __init__(
        self,
        name,
        *,
        completion=SynthesisCompletion.COMPLETE,
        on_render=None,
        pcm=None,
    ):
        self.name = name
        self.completion = completion
        self.on_render = on_render
        self.pcm = pcm
        self.registry = None
        self.requests = []
        self.shutdown_count = 0

    def render(self, request):
        self.requests.append(request)
        if self.on_render is not None:
            self.on_render()
        return CollectedResult(
            SynthesisResult(
                pcm=(
                    np.full(1_600, 0.1, dtype=np.float32)
                    if self.pcm is None
                    else self.pcm
                ),
                sample_rate=16_000,
                completion=self.completion,
                limits=SynthesisLimits(None, None),
                timing=SynthesisTiming(10.0, 100.0),
                diagnostics=SynthesisDiagnostics(
                    backend=self.name,
                    cache_source="generated",
                    generation_profile=request.generation_profile,
                    seed=request.seed,
                    chunk_count=1,
                    sample_count=1_600,
                ),
            )
        )

    def shutdown(self):
        self.shutdown_count += 1


def clean_wav_bytes(*, amplitude=0.1, seconds=1.2, sample_rate=16_000):
    samples = np.full(round(seconds * sample_rate), amplitude, dtype=np.float32)
    samples[1::2] *= -1
    pcm = np.round(samples * 32767).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(pcm.tobytes())
    return output.getvalue()


def ambiguous_fixture(root):
    content = inspect_story_index(write_content(root / "content"))
    jobs = PregenerationJobStore(root / "jobs")
    job = jobs.create_or_resume(content, ("story",))
    manifest = write_manifest(root / "voices", rhiannon=clean_wav_bytes())
    plan = VoicePlanStore(jobs).create(
        job,
        AppSettings(speech_backend="moss-tts", tts_profile="stable"),
        manifest_path=manifest,
    )
    selected = next(group for group in plan.groups if group.character == "Rhiannon")
    ambiguous = replace(
        selected,
        route="needs-audition",
        resolution="ambiguous-voice-evidence",
    )
    plan = replace(
        plan,
        groups=tuple(
            ambiguous if group is selected else group for group in plan.groups
        ),
    )
    return plan, ambiguous, manifest


class VoiceAuditionPreviewServiceTest(unittest.TestCase):
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
                reference.resolve(),
            )
            service.close()

    @patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=True)
    def test_generates_one_exact_preview_then_reuses_persistent_wav(self, _native):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
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

    @patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=True)
    def test_native_quality_failure_refreshes_the_backend_cache(self, _native):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend(
                "moss-tts",
                pcm=np.concatenate(
                    (np.full(1_600, 0.1, dtype=np.float32), np.zeros(19_200))
                ),
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


if __name__ == "__main__":
    unittest.main()
