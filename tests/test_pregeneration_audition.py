import io
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock

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

    def test_generates_one_exact_preview_then_reuses_persistent_wav(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend("moss-tts")
            factory_calls = []

            def factory(name, registry, cache_root, **options):
                factory_calls.append((name, registry, cache_root, options))
                backend.registry = registry
                return backend

            service = VoiceAuditionPreviewService(
                root / "auditions", backend_factory=factory
            )
            source_id = group.candidates[0].source_id
            first = service.generate(plan, group, source_id)
            service.close()
            second_service = VoiceAuditionPreviewService(
                root / "auditions",
                backend_factory=lambda *_args, **_kwargs: self.fail(
                    "A persisted preview must not restart the model"
                ),
            )
            second = second_service.generate(plan, group, source_id)
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

            with self.assertRaisesRegex(VoiceAuditionError, "excessive-clipping"):
                service.generate(plan, group, group.candidates[0].source_id)
            service.close()

            factory.assert_not_called()

    def test_rejects_silent_generated_preview_without_publishing_wav(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan, group, _manifest = ambiguous_fixture(root)
            backend = FakeBackend(
                "moss-tts", pcm=np.zeros(1_600, dtype=np.float32)
            )
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
            service.close()

            self.assertFalse(tuple((root / "auditions").glob("*.wav")))


if __name__ == "__main__":
    unittest.main()
